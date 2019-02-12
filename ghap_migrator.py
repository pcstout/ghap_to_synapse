#!/usr/bin/env python

# Copyright 2017-present, Bill & Melinda Gates Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import argparse
import logging
import getpass
import sh
import time
import random
import csv
import string
import unicodedata
import concurrent.futures
import threading
from scandir import scandir
import synapseclient
from synapseclient import Project, Folder, File
from io import StringIO


class GhapMigrator:

    def __init__(self, csv_filename, username=None, password=None, admin_team_id=None, storage_location_id=None,
                 skip_md5=False, max_threads=None):
        self._csv_filename = csv_filename
        self._username = username
        self._password = password
        self._admin_team_id = admin_team_id
        self._admin_team = None
        self._storage_location_id = storage_location_id
        self._storage_location = None
        self._skip_md5 = skip_md5
        self._work_dir = os.path.join(os.path.expanduser('~'), 'tmp', 'ghap')
        self._synapse_client = None
        self._script_user = None
        self._synapse_parents = {}
        self._git_to_syn_mappings = []
        self._errors = []
        self._thread_lock = threading.Lock()
        self._max_threads = max_threads

    def log_error(self, msg):
        self._errors.append(msg)
        logging.error(msg)

    def start(self):
        if not os.path.exists(self._work_dir):
            os.makedirs(self._work_dir)

        logging.info('CSV File: {0}'.format(self._csv_filename))
        logging.info('Temp Directory: {0}'.format(self._work_dir))

        if self._skip_md5:
            logging.info('Skipping MD5 Checks')

        self.synapse_login()
        self._script_user = self._synapse_client.getUserProfile()

        if self._admin_team_id and self._admin_team_id.strip() != '':
            logging.info('Loading Admin Team ID: {0}'.format(self._admin_team_id))
            self._admin_team = self._synapse_client.getTeam(self._admin_team_id)
            logging.info('Admin Team Loaded: {0}'.format(self._admin_team.name))
        else:
            self._admin_team_id = None

        if self._storage_location_id and self._storage_location_id.strip() != '':
            logging.info('Loading Storage Location ID: {0}'.format(self._storage_location_id))
            self._storage_location = self._synapse_client.getMyStorageLocationSetting(self._storage_location_id)
            logging.info('Storage Location: {0}'.format(self._storage_location['bucket']))
        else:
            self._storage_location_id = None

        self.process_csv()

        if len(self._git_to_syn_mappings) > 0:
            logging.info('Synapse Projects:')
            for line in self._git_to_syn_mappings:
                logging.info(' - {0}'.format(line))

        if len(self._errors) > 0:
            self.log_error('Completed with Errors:')
            for line in self._errors:
                self.log_error(' - {0}'.format(line))
        else:
            logging.info('Completed Successfully.')

    def synapse_login(self):
        logging.info('Logging into Synapse...')
        self._username = self._username or os.getenv('SYNAPSE_USERNAME')
        self._password = self._password or os.getenv('SYNAPSE_PASSWORD')

        if not self._username:
            self._username = input('Synapse username: ')

        if not self._password:
            self._password = getpass.getpass(prompt='Synapse password: ')

        try:
            self._synapse_client = synapseclient.Synapse()
            self._synapse_client.login(self._username, self._password, silent=True)
        except Exception as ex:
            self._synapse_client = None
            self.log_error('Synapse login failed: {0}'.format(str(ex)))

    def process_csv(self):
        """
        Process the CSV file.
        The format CSV format is: "git_url,synapse_project_id"
          git_url:            The full GIT URL of the repository to migrate.
          synapse_project_id: The Synapse Project to migrate the repository into.
                              Blank = Create a new Project.
                              SynID = Use an existing Project and upload into a new Folder in the Project.
          synapse_path:       The folder path in Synapse to store the files (e.g., EDD/common)
        """
        with open(self._csv_filename) as csvfile:
            reader = csv.DictReader(csvfile, delimiter=',')
            for row in reader:
                git_url = row['git_url'].strip()
                synapse_project_id = row['synapse_project_id'].strip()
                synapse_path = row['synapse_path'].replace(' ', '').lstrip(os.sep).rstrip(os.sep)
                self.migrate(git_url, synapse_project_id, synapse_path)

    def migrate(self, git_url, synapse_project_id, synapse_path):
        logging.info('Processing {0}'.format(git_url))

        repo_name = git_url.split('/')[-1].replace('.git', '')
        repo_path = os.path.join(self._work_dir, repo_name)

        if os.path.exists(repo_path):
            # Pull
            logging.info(' Pulling Repo into {0}'.format(repo_path))
            sh.git.bake(_cwd=repo_path).pull('--rebase')
        else:
            # Checkout
            logging.info(' Checking Out into {0}'.format(repo_path))
            sh.git.bake(_cwd=self._work_dir).clone(git_url)

        self.push_to_synapse(git_url, repo_name, repo_path, synapse_project_id, synapse_path)

    def push_to_synapse(self, git_url, repo_name, repo_path, synapse_project_id, synapse_path):
        project = None

        if synapse_project_id and synapse_project_id != '':
            # Find or create a Folder in the Project to store the repo.
            project = self.find_or_create_project(synapse_project_id)
        else:
            # Find or Create the Project.
            project_name = 'GHAP - {0}'.format(repo_name)
            project = self.find_or_create_project(project_name)

        if not project:
            self.log_error('Could not get project for {0}.'.format(git_url))
            return
        else:
            self._git_to_syn_mappings.append('{0} -> {1}'.format(git_url, project.id))

        parent = project

        # Create the folders if specified.
        if synapse_path:
            full_path = ''
            for folder in filter(None, synapse_path.split(os.sep)):
                full_path = os.path.join(full_path, folder)
                parent = self.find_or_create_folder(full_path, parent)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_threads) as executor:
            self.upload_folder(executor, repo_path, parent)

    def get_dirs_and_files(self, local_path):
        dirs = []
        files = []

        for entry in scandir(local_path):
            if entry.is_dir(follow_symlinks=False):
                # Do not include .git
                if os.path.basename(entry.path) == '.git':
                    logging.info('Skipping GIT Directory: {0}'.format(entry.path))
                    continue

                dirs.append(entry)
            else:
                # Skip the *.gitlog files since they will be created during upload.
                if os.path.basename(entry.path).endswith('.gitlog'):
                    continue

                files.append(entry)

        dirs.sort(key=lambda f: f.name)
        files.sort(key=lambda f: f.name)

        return dirs, files

    def upload_folder(self, executor, local_path, synapse_parent):
        parent = synapse_parent

        dirs, files = self.get_dirs_and_files(local_path)

        # Upload the files
        for file_entry in files:
            # Create the GIT log for the file.
            filename = os.path.basename(file_entry.path)
            dirpath = os.path.dirname(file_entry.path)
            git_log_filename = os.path.join(dirpath, '{0}.gitlog'.format(filename))
            sh.git.bake('--no-pager', _cwd=dirpath).log(filename, _out=git_log_filename, _tty_out=False)

            for upload_filename in [file_entry.path, git_log_filename]:
                if os.path.getsize(upload_filename) > 0:
                    executor.submit(self.find_or_upload_file, upload_filename, parent)
                else:
                    logging.info('Skipping Empty File: {0}'.format(upload_filename))

        # Upload the directories.
        for dir_entry in dirs:
            syn_dir = self.find_or_create_folder(dir_entry.path, parent)
            self.upload_folder(executor, dir_entry.path, syn_dir)

    def find_or_create_project(self, project_name_or_id):
        project = None

        try:
            if project_name_or_id.lower().startswith('syn'):
                project = self._synapse_client.get(project_name_or_id)
            else:
                project_id = self._synapse_client.findEntityId(project_name_or_id)
                project = self._synapse_client.get(project_id)
        except synapseclient.exceptions.SynapseHTTPError as ex:
            if ex.response.status_code >= 400:
                self.log_error('Script user does not have READ permission to Project: {0}'.format(project_name_or_id))
                return None
        except Exception as ex:
            # Project doesn't exist.
            pass

        if project:
            logging.info('Found Project: {0}: {1}'.format(project.id, project.name))
            if not self.has_write_permissions(project):
                self.log_error('Script user does not have WRITE permission to Project: {0}'.format(project_name_or_id))
                return None
        else:
            project = self._synapse_client.store(Project(project_name_or_id))
            logging.info('Created Project: {0}: {1}'.format(project.id, project.name))
            if self._storage_location_id:
                logging.info('Setting storage location for project: {0}: {1}'.format(project.id, project.name))
                self._synapse_client.setStorageLocation(project, self._storage_location_id)

            if self._admin_team:
                logging.info('Granting admin permissions to team on Project: {0}: {1}'.format(project.id, project.name))
                self.grant_admin_access(project, self._admin_team.id)

        if project:
            self.set_synapse_parent(project)

        return project

    def has_write_permissions(self, project):
        # Check for user specific permissions.
        user_perms = set(self._synapse_client.getPermissions(project, self._script_user.ownerId))
        if ('CREATE' in user_perms) and ('UPDATE' in user_perms):
            return True

        # Check for team permissions.
        acl = self._synapse_client._getACL(project)

        for resourceAccess in acl['resourceAccess']:
            principalId = resourceAccess['principalId']
            try:
                team = self._synapse_client.getTeam(principalId)
                team_members = self._synapse_client.getTeamMembers(team)
                for team_member in team_members:
                    if team_member['member']['ownerId'] == self._script_user.ownerId:
                        team_perms = set(resourceAccess['accessType'])
                        if ('CREATE' in team_perms) and ('UPDATE' in team_perms):
                            return True
            except synapseclient.exceptions.SynapseHTTPError as ex:
                # This will 404 when fetching a User instead of a Team.
                if ex.response.status_code != 404:
                    raise ex

    def grant_admin_access(self, project, grantee_id):
        accessType = ['UPDATE', 'DELETE', 'CHANGE_PERMISSIONS',
                      'CHANGE_SETTINGS', 'CREATE', 'DOWNLOAD', 'READ', 'MODERATE']
        self._synapse_client.setPermissions(project, grantee_id, accessType=accessType, warn_if_inherits=False)

    def find_or_create_folder(self, path, synapse_parent):
        if not synapse_parent:
            self.log_error('Parent not found, cannot create folder: {0}'.format(path))
            return

        folder_name = os.path.basename(path)

        sanitized_folder_name = self.sanitize_name(folder_name)
        if sanitized_folder_name != folder_name:
            logging.info('Sanitizing folder name: {0} -> {1}'.format(folder_name, sanitized_folder_name))

        full_synapse_path = self.get_synapse_path(sanitized_folder_name, synapse_parent)

        syn_folder_id = self._synapse_client.findEntityId(sanitized_folder_name, parent=synapse_parent)

        synapse_folder = None

        if syn_folder_id:
            synapse_folder = self._synapse_client.get(syn_folder_id, downloadFile=False)
            self.set_synapse_parent(synapse_folder)
            logging.info('Folder Already Exists: {0}{1}  -> {2}'.format(path, os.linesep, full_synapse_path))
        else:
            synapse_folder = Folder(name=sanitized_folder_name, parent=synapse_parent)
            max_attempts = 10
            attempt_number = 0

            while attempt_number < max_attempts and not synapse_folder.get('id', None):
                try:
                    attempt_number += 1
                    synapse_folder = self._synapse_client.store(synapse_folder, forceVersion=False)
                except Exception as ex:
                    self.log_error(
                        'Error creating folder: {0}{1}  -> {2}{1}  -> {3}'.format(path, os.linesep, full_synapse_path,
                                                                                  str(ex)))
                    if attempt_number < max_attempts:
                        sleep_time = random.randint(1, 5)
                        logging.info('Retrying folder: {0}{1}  -> {2}{1}  -> in {3} seconds'.format(path, os.linesep,
                                                                                                    full_synapse_path,
                                                                                                    sleep_time))
                        time.sleep(sleep_time)

            if not synapse_folder.get('id', None):
                self.log_error('Failed to create folder: {0}{1}  -> {2}'.format(path, os.linesep, full_synapse_path))
            else:
                logging.info('Folder created: {0}{1}  -> {2}'.format(path, os.linesep, full_synapse_path))
                self.set_synapse_parent(synapse_folder)

        return synapse_folder

    def find_or_upload_file(self, local_file, synapse_parent):
        if not synapse_parent:
            self.log_error('Parent not found, cannot upload file: {0}'.format(local_file))
            return None

        filename = os.path.basename(local_file)

        sanitized_filename = self.sanitize_name(filename)
        if sanitized_filename != filename:
            logging.info('Sanitizing file: {0} -> {1}'.format(filename, sanitized_filename))

        full_synapse_path = self.get_synapse_path(sanitized_filename, synapse_parent)

        needs_upload = True
        synapse_file = None

        if not self._skip_md5:
            # Check if the file has already been uploaded and has not changed since being uploaded.
            syn_file_id = self._synapse_client.findEntityId(sanitized_filename, parent=synapse_parent)

            if syn_file_id:
                synapse_file = self._synapse_client.get(syn_file_id, downloadFile=False)

                synapse_file_md5 = synapse_file._file_handle['contentMd5']
                local_md5 = self.get_local_file_md5(local_file)
                if local_md5 == synapse_file_md5:
                    needs_upload = False
                    logging.info(
                        'File Already Uploaded and Current: {0}{1}  -> {2}'.format(local_file, os.linesep,
                                                                                   full_synapse_path))
                else:
                    synapse_file = None
                    logging.info('File Already Uploaded but has changes: {0}{1}  -> {2}'.format(local_file, os.linesep,
                                                                                                full_synapse_path))

        if needs_upload:
            synapse_file = File(path=local_file, name=sanitized_filename, parent=synapse_parent)

            max_attempts = 10
            attempt_number = 0

            while attempt_number < max_attempts and not synapse_file.get('id', None):
                try:
                    attempt_number += 1
                    synapse_file = self._synapse_client.store(synapse_file, forceVersion=False)
                except Exception as ex:
                    self.log_error('Error uploading file: {0}{1}  -> {2}{1}  -> {3}'.format(local_file, os.linesep,
                                                                                            full_synapse_path, str(ex)))
                    if attempt_number < max_attempts:
                        sleep_time = random.randint(1, 5)
                        logging.info(
                            'Retrying in {0} seconds: {1}{2}  -> {3}'.format(sleep_time, local_file, os.linesep,
                                                                             full_synapse_path))
                        time.sleep(sleep_time)

            if not synapse_file.get('id', None):
                self.log_error(
                    'Failed to upload file: {0}{1}  -> {2}'.format(local_file, os.linesep, full_synapse_path))
            else:
                logging.info('File uploaded: {0}{1}  -> {2}'.format(local_file, os.linesep, full_synapse_path))

        return synapse_file

    def get_local_file_md5(self, filename):
        out_buffer = StringIO()
        sh.md5sum(filename, _out=out_buffer)
        local_file_md5 = out_buffer.getvalue().split()[0]
        return local_file_md5

    def set_synapse_parent(self, parent):
        with self._thread_lock:
            self._synapse_parents[parent.id] = parent

    def get_synapse_parent(self, parent_id):
        with self._thread_lock:
            return self._synapse_parents.get(parent_id, None)

    def get_synapse_path(self, folder_or_filename, parent):
        segments = []

        if isinstance(parent, Project):
            segments.insert(0, parent.name)
        else:
            next_parent = parent
            while next_parent:
                segments.insert(0, next_parent.name)
                next_parent = self.get_synapse_parent(next_parent.parentId)

        segments.append(folder_or_filename)

        return os.path.join(*segments)

    VALID_FILENAME_CHARS = frozenset("-_.() %s%s" % (string.ascii_letters, string.digits))

    def sanitize_name(self, name):
        cleaned_filename = unicodedata.normalize('NFKD', u'{0}'.format(name)).encode('ASCII', 'ignore')
        return ''.join(c for c in cleaned_filename if c in self.VALID_FILENAME_CHARS)


class LogFilter(logging.Filter):
    FILTERS = [
        '##################################################',
        'Uploading file to Synapse storage',
        'Connection pool is full, discarding connection:'
    ]

    def filter(self, record):
        for filter in self.FILTERS:
            if filter in record.msg:
                return False
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'csv', help='CSV file with GIT repository URLs to process.')
    parser.add_argument('-u', '--username',
                        help='Synapse username.', default=None)
    parser.add_argument('-p', '--password',
                        help='Synapse password.', default=None)
    parser.add_argument('-a', '--admin-team-id',
                        help='The Team ID to add to each Project.', default=None)
    parser.add_argument('-s', '--storage-location-id',
                        help='The Storage location ID for projects that are created.', default=None)
    parser.add_argument('-m', '--skip-md5', help='Skip md5 checks.',
                        default=False, action='store_true')
    parser.add_argument('-t', '--threads',
                        help='Set the maximum number of threads to run.', type=int, default=None)
    parser.add_argument('-l', '--log-level',
                        help='Set the logging level.', default='INFO')

    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper())
    log_filename = 'log.txt'

    logging.basicConfig(
        filename=log_filename,
        filemode='w',
        format='%(asctime)s %(levelname)s: %(message)s',
        level=log_level
    )

    # Add console logging.
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter('%(message)s'))
    logging.getLogger().addHandler(console)

    # Filter logs
    log_filter = LogFilter()
    for logger in [logging.getLogger(name) for name in logging.root.manager.loggerDict]:
        logger.addFilter(log_filter)

    # Silence sh logging
    logging.getLogger("sh").setLevel(logging.ERROR)

    GhapMigrator(
        args.csv,
        username=args.username,
        password=args.password,
        admin_team_id=args.admin_team_id,
        storage_location_id=args.storage_location_id,
        skip_md5=args.skip_md5,
        max_threads=args.threads
    ).start()


if __name__ == "__main__":
    main()
