import requests
import time
import io
import re
import os
import json
import pandas as pd
from lxml import html
from os.path import basename
from fnmatch import fnmatch
import getpass

from .config import config
from .identifier import Identifier

from http.cookiejar import LWPCookieJar


requests.packages.urllib3.disable_warnings()


def _logout_at_exit(session, url, **args):
    session.get(url, **args)


_P_COLUMNS = [            'name', 'owner', 'editor',   'resource_profile',                                    'id',               'created', 'updated', 'url']  # noqa: E241
_R_COLUMNS = [            'name', 'owner', 'commands',                                                        'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241
_S_COLUMNS = [            'name', 'owner',             'resource_profile',                           'state', 'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241
_D_COLUMNS = ['endpoint', 'name', 'owner', 'command',  'resource_profile', 'project_name', 'public', 'state', 'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241
_J_COLUMNS = [            'name', 'owner', 'command',  'resource_profile', 'project_name',           'state', 'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241
_C_COLUMNS = ['id',  'permission', 'type', 'first name', 'last name', 'email']
_U_COLUMNS = ['username', 'email', 'firstName', 'lastName', 'id']
_A_COLUMNS = ['type', 'status', 'message', 'done', 'owner', 'id', 'description', 'created', 'updated']
_E_COLUMNS = ['id', 'owner', 'name', 'deployment_id', 'project_id', 'project_url']
_DTYPES = {'created': 'datetime', 'updated': 'datetime',
           'createdTimestamp': 'timestamp/ms', 'notBefore': 'timestamp/s'}


class AEAuthenticationError(RuntimeError):
    pass


class AEUnexpectedResponseError(RuntimeError):
    def __init__(self, response, method, url, **kwargs):
        msg = [f'Unexpected response: {response.status_code} {response.reason}',
               f'  {method.upper()} {url}']
        if 'params' in kwargs:
            msg.append(f'  params: {kwargs["params"]}')
        if 'data' in kwargs:
            msg.append(f'  data: {kwargs["data"]}')
        super(AEUnexpectedResponseError, self).__init__('\n'.join(msg))
    pass


class AESessionBase(object):
    '''Base class for AE5 API interactions.'''

    def __init__(self, hostname, username, password, prefix, dataframe, retry, password_prompt):
        '''Base class constructor.

        Args:
            hostname: The FQDN of the AE5 cluster
            username: The username associated with the connection.
            password (str, optional): the password to use to log in, if necessary.
            prefix (str): The URL prefix to prepend to all API calls.
            dataframe (bool, optional, default=False): if True, any
                API call made with a `columns` argument will be
                returned as a dataframe. If False, the raw JSON output
                will be returned instead.
            retry (bool, optional, default=True): if True, the constructor
                will attempt to establish a new connection if there is no saved
                session, or if the saved session has expired.
            password_prompt (function, optional): if not None, this will be used
                instead of the _password_prompt static method to request a password.
        '''
        if not hostname or not username:
            raise ValueError('Must supply hostname and username')
        self.hostname = hostname
        self.username = username
        self.prefix = prefix.lstrip('/')
        self.dataframe = dataframe
        if password_prompt:
            self._password_prompt = password_prompt
        if isinstance(password, requests.Session):
            self.session = password
            self.connected = True
            self._save()
        else:
            self.session = requests.Session()
            self.session.cookies = LWPCookieJar()
            self.session.verify = False
            self.connect(password, retry)

    @staticmethod
    def _password_prompt(key):
        while True:
            password = getpass.getpass(f'Password for {key}: ')
            if password:
                return password
            print('Must supply a password.')

    def connect(self, password=None, retry=True):
        self.connected = False
        self._load()
        if self._connected():
            self.connected = True
            self._set_header()
            return
        if not retry:
            return
        key = f'{self.username}@{self.hostname}'
        if password is None:
            password = self._password_prompt(key)
        self._connect(password)
        if not self._connected():
            raise RuntimeError(f'Failed to create session for {key}')
        self.connected = True
        self._set_header()
        self._save()

    def _format_kwargs(self, kwargs):
        dataframe = kwargs.pop('dataframe', None)
        format = kwargs.pop('format', None)
        if dataframe is not None:
            if format is None:
                format = 'dataframe' if dataframe else 'json'
            elif (format == 'dataframe') != dataframe:
                raise RuntimeError('Conflicting "format" and "dataframe" specifications')
        return format, kwargs.pop('columns', None)

    def _format_dataframe(self, response, columns):
        if isinstance(response, dict):
            is_series = True
        elif isinstance(response, list) and all(isinstance(x, dict) for x in response):
            is_series = False
        else:
            raise RuntimeError('Not a dataframe-compatible output')
        df = pd.DataFrame([response] if is_series else response)
        if len(df) == 0 and columns:
            df = pd.DataFrame(columns=columns)
        for col, dtype in _DTYPES.items():
            if col in df:
                if dtype == 'datetime':
                    df[col] = pd.to_datetime(df[col])
                elif dtype.startswith('timestamp'):
                    df[col] = pd.to_datetime(df[col], unit=dtype.rsplit('/', 1)[-1])
                else:
                    df[col] = df[col].astype(dtype)
        if columns:
            cols = ([c for c in columns if c in df.columns] +
                    [c for c in df.columns if c not in columns])
            if cols:
                df = df[cols]
        if is_series:
            df = df.iloc[0]
            df.name = None
        return df

    def _format_response(self, response, format, columns):
        if format == 'response':
            return response
        if format == 'text':
            return response.text
        if format == 'blob':
            return response.content
        if isinstance(response, requests.models.Response):
            response = response.json()
        if format is None and columns:
            format = 'dataframe' if self.dataframe else 'json'
        if format == 'json':
            return response
        return self._format_dataframe(response, columns)

    def _api(self, method, endpoint, **kwargs):
        pass_errors = kwargs.pop('pass_errors', False)
        fmt, cols = self._format_kwargs(kwargs)
        if endpoint.startswith('/'):
            endpoint = endpoint.lstrip('/')
        elif endpoint:
            endpoint = f'{self.prefix}/{endpoint}'
        else:
            endpoint = self.prefix
        url = f'https://{self.hostname}/{endpoint}'
        kwargs.update((('verify', False), ('allow_redirects', True)))
        response = getattr(self.session, method)(url, **kwargs)
        if 400 <= response.status_code and not pass_errors:
            raise AEUnexpectedResponseError(response, method, url, **kwargs)
        return self._format_response(response, fmt, cols)

    def _get(self, endpoint, **kwargs):
        return self._api('get', endpoint, **kwargs)

    def _delete(self, endpoint, **kwargs):
        return self._api('delete', endpoint, **kwargs)

    def _post(self, endpoint, **kwargs):
        return self._api('post', endpoint, **kwargs)

    def _put(self, endpoint, **kwargs):
        return self._api('put', endpoint, **kwargs)

    def _patch(self, endpoint, **kwargs):
        return self._api('patch', endpoint, **kwargs)


class AEUserSession(AESessionBase):
    def __init__(self, hostname, username, password=None, dataframe=False, retry=True, password_prompt=None):
        self._filename = os.path.join(config._path, 'cookies', f'{username}@{hostname}')
        super(AEUserSession, self).__init__(hostname, username, password, 'api/v2', dataframe, retry, password_prompt)

    def _set_header(self):
        s = self.session
        for cookie in s.cookies:
            if cookie.name == '_xsrf':
                s.headers['x-xsrftoken'] = cookie.value
                break

    def _load(self):
        s = self.session
        if os.path.exists(self._filename):
            s.cookies.load(self._filename)
            os.utime(self._filename)
            try:
                self._get('runs', format='response')
            except AEUnexpectedResponseError:
                s.cookies.clear()

    def _connected(self):
        return any(c.name == '_xsrf' for c in self.session.cookies)

    def _connect(self, password):
        params = {'client_id': 'anaconda-platform', 'scope': 'openid',
                  'response_type': 'code', 'redirect_uri': f'https://{self.hostname}/login'}
        text = self._get('/auth/realms/AnacondaPlatform/protocol/openid-connect/auth', params=params, format='text')
        tree = html.fromstring(text)
        form = tree.xpath("//form[@id='kc-form-login']")
        login_path = '/' + form[0].action.split('/', 3)[-1]
        resp = self._post(login_path, data={'username': self.username, 'password': password}, format='text')
        elems = html.fromstring(resp).find_class('kc-feedback-text')
        if elems:
            raise AEAuthenticationError(elems[0].text)

    def _save(self):
        os.makedirs(os.path.dirname(self._filename), mode=0o700, exist_ok=True)
        self.session.cookies.save(self._filename)
        os.chmod(self._filename, 0o600)

    def _id(self, type, ident, record=False, quiet=False):
        if isinstance(ident, str):
            ident = Identifier.from_string(ident)
        idtype = ident.id_type(ident.id) if ident.id else type
        if idtype not in ('projects', type):
            raise ValueError(f'Expected a {type} ID type, found a {idtype} ID: {ident}')
        matches = []
        # NOTE: we are retrieving all project records here, even if we have the unique
        # id and could potentially retrieve the individual record, because the full
        # listing includes a field the individual query does not (project_create_status)
        # Also, we're using our wrapper around the list API calls instead of the direct
        # call so we get the benefit of our record cleanup.
        records = getattr(self, type.rstrip('s') + '_list')(format='json')
        owner, name, id, pid = (ident.owner or '*', ident.name or '*',
                                ident.id if ident.id and type == idtype else '*',
                                ident.pid if ident.pid and type != 'projects' else '*')
        for rec in records:
            if (fnmatch(rec['owner'], owner) and fnmatch(rec['name'], name) and
                fnmatch(rec['id'], id) and fnmatch(rec.get('project_id', ''), pid)):
                matches.append(rec)
        if len(matches) == 1:
            rec = matches[0]
            id = rec['id']
        elif quiet:
            id, rec = None, None
        else:
            pfx = 'Multiple' if len(matches) else 'No'
            msg = f'{pfx} {type} found matching {owner}/{name}/{id}'
            if matches:
                matches = [str(Identifier.from_record(r, True)) for r in matches]
                msg += ':\n  - ' + '\n  - '.join(matches)
            raise ValueError(msg)
        if record:
            return rec['id'], rec
        else:
            return rec['id']

    def _revision(self, ident, record=False, quiet=False):
        if isinstance(ident, str):
            ident = Identifier.from_string(ident)
        id = self._id('projects', ident, record=record, quiet=quiet)
        if record:
            id, prec = id
        rrec, rev = None, None
        if id:
            revisions = self._get(f'projects/{id}/revisions', format='json')
            if not ident.revision or ident.revision == 'latest':
                matches = [revisions[0]]
            else:
                matches = []
                for response in revisions:
                    if fnmatch(response['name'], ident.revision):
                        matches.append(response)
            if len(matches) == 1:
                rrec = matches[0]
                rev = rrec['name']
            elif not quiet:
                pfx = 'Multiple' if len(matches) else 'No'
                msg = f'{pfx} revisions found matching {ident.revision}'
                if matches:
                    msg += ':\n  - ' + '\n  - '.join(matches)
                raise ValueError(msg)
        if record:
            return id, rev, prec, rrec
        else:
            return id, rev

    def project_list(self, collaborators=False, format=None):
        records = self._get('projects', format='json')
        if collaborators:
            self._join_collaborators(records)
        return self._format_response(records, format=format, columns=_P_COLUMNS)

    def project_info(self, ident, collaborators=False, format=None, quiet=False):
        id, record = self._id('projects', ident, record=True, quiet=quiet)
        if collaborators:
            self._join_collaborators(record)
        return self._format_response(record, format=format, columns=_P_COLUMNS)

    def project_collaborators(self, ident, format=None):
        id = self._id('projects', ident, record=False)
        return self._get(f'projects/{id}/collaborators', format=format, columns=_C_COLUMNS)

    def project_deployments(self, ident, format=None):
        id = self._id('projects', ident, record=False)
        return self._get(f'projects/{id}/deployments', format=format, columns=_D_COLUMNS)

    def project_jobs(self, ident, format=None):
        id = self._id('projects', ident, record=False)
        return self._get(f'projects/{id}/jobs', format=format, columns=_J_COLUMNS)

    def project_runs(self, ident, format=None):
        id = self._id('projects', ident, record=False)
        return self._get(f'projects/{id}/runs', format=format, columns=_R_COLUMNS)

    def project_activity(self, ident, limit=0, latest=False, format=None):
        id = self._id('projects', ident)
        limit = 1 if latest else (999999 if limit <= 0 else limit)
        params = {'sort': '-updated', 'page[size]': limit}
        response = self._get(f'projects/{id}/activity', params=params, format='json')['data']
        if latest:
            response = response[0]
        return self._format_response(response, format=format, columns=_A_COLUMNS)

    def revision_list(self, ident, format=None):
        id = self._id('projects', ident)
        response = self._get(f'projects/{id}/revisions', format='json')
        for rec in response:
            rec['project_id'] = 'a0-' + rec['url'].rsplit('/', 3)[-3]
        return self._format_response(response, format=format, columns=_R_COLUMNS)

    def revision_info(self, ident, format=None, quiet=False):
        id, rev, prec, rrec = self._revision(ident, record=True, quiet=quiet)
        if id:
            rrec['project_id'] = prec['id']
            return self._format_response(rrec, format=format, columns=_R_COLUMNS)

    def project_download(self, ident, filename=None):
        id, rev = self._revision(ident)
        response = self._get(f'projects/{id}/revisions/{rev}/archive', format='blob')
        if filename is None:
            return response
        with open(filename, 'wb') as fp:
            fp.write(response)

    def project_delete(self, ident):
        id = self._id('projects', ident)
        self._delete(f'projects/{id}', format='response')

    def _wait(self, id, status):
        index = 0
        while not status['done'] and not status['error']:
            time.sleep(5)
            params = {'sort': '-updated', 'page[size]': index + 1}
            activity = self._get(f'projects/{id}/activity', params=params, format='json')
            try:
                status = next(s for s in activity['data'] if s['id'] == status['id'])
            except StopIteration:
                index = index + 1
        return status

    def project_upload(self, project_archive, name, tag, wait=True, format=None):
        if not name:
            if type(project_archive) == bytes:
                raise RuntimeError('Project name must be supplied for binary input')
            name = basename(project_archive).split('.', 1)[0]
        try:
            if type(project_archive) == bytes:
                f = io.BytesIO(project_archive)
            else:
                f = open(project_archive, 'rb')
            data = {'name': name}
            if tag:
                data['tag'] = tag
            response = self._post('projects/upload', files={'project_file': f}, data=data, format='json')
        finally:
            f.close()
        if response.get('error'):
            raise RuntimeError('Error uploading project: {}'.format(response['error']['message']))
        if wait:
            response['action'] = self._wait(response['id'], response['action'])
        if response['action']['error']:
            raise RuntimeError('Error processing upload: {}'.format(response['action']['message']))
        return self._format_response(response, format, columns=_P_COLUMNS)

    def _join_projects(self, response, nameprefix=None):
        if isinstance(response, dict):
            pid = 'a0-' + response['project_url'].rsplit('/', 1)[-1]
            if nameprefix or 'name' not in response:
                project = self._get(f'projects/{pid}', format='json')
                if 'name' in response:
                    response[f'{nameprefix}_name'] = response['name']
                response['name'] = project['name']
            response['project_id'] = pid
        elif response:
            if nameprefix or 'name' not in response[0]:
                pnames = {x['id']: x['name'] for x in self._get('projects', format='json')}
            for rec in response:
                pid = 'a0-' + rec['project_url'].rsplit('/', 1)[-1]
                if nameprefix or 'name' not in rec:
                    if 'name' in rec:
                        rec[f'{nameprefix}_name'] = rec['name']
                    rec['name'] = pnames.get(pid, '')
                rec['project_id'] = pid

    def _join_collaborators(self, response):
        if isinstance(response, dict):
            collabs = self._get(f'projects/{response["id"]}/collaborators', format='json')
            response['collaborators'] = ', '.join(c['id'] for c in collabs)
        elif response:
            for rec in response:
                self._join_collaborators(rec)

    def session_list(self, format=None):
        response = self._get('sessions', format='json')
        self._join_projects(response, 'session')
        return self._format_response(response, format, _S_COLUMNS)

    def session_info(self, ident, format=None, quiet=False):
        id, record = self._id('sessions', ident, record=True, quiet=quiet)
        if id:
            self._join_projects(record, 'session')
            return self._format_response(record, format, columns=_S_COLUMNS)

    def session_start(self, ident, wait=True, format=None):
        id = self._id('projects', ident)
        response = self._post(f'projects/{id}/sessions', format='json')
        if response.get('error'):
            raise RuntimeError('Error starting project: {}'.format(response['error']['message']))
        if wait:
            response['action'] = self._wait(id, response['action'])
        if response['action'].get('error'):
            raise RuntimeError('Error completing session start: {}'.format(response['action']['message']))
        return self._format_response(response, format=format, columns=_S_COLUMNS)

    def session_stop(self, ident):
        id = self._id('sessions', ident)
        self._delete(f'sessions/{id}', format='response')

    def deployment_list(self, format=None):
        response = self._get('deployments', format='json')
        self._join_projects(response)
        for record in response:
            record['endpoint'] = record['url'].split('/', 3)[2].split('.', 1)[0]
        return self._format_response(response, format, _D_COLUMNS)

    def deployment_info(self, ident, format=None, quiet=False):
        id, record = self._id('deployments', ident, record=True, quiet=quiet)
        if id:
            self._join_projects(record)
            record['endpoint'] = record['url'].split('/', 3)[2].split('.', 1)[0]
            return self._format_response(record, format, _D_COLUMNS)

    def deployment_endpoints(self, format=None):
        response = self._get('/platform/deploy/api/v1/apps/static-endpoints', format='json')['data']
        self._join_projects(response, None)
        for rec in response:
            rec['deployment_id'] = 'a2-' + rec['deployment_id'] if rec['deployment_id'] else ''
        return self._format_response(response, format=format, columns=_E_COLUMNS)

    def deployment_collaborators(self, ident, format=None):
        id = self._id('deployments', ident)
        return self._get(f'deployments/{id}/collaborators', format=format, columns=_C_COLUMNS)

    def deployment_start(self, ident, endpoint=None, wait=True, format=None):
        id, rev, prec, rrec = self._revision(ident, record=True)
        data = {'name': prec['name'],
                'source': rrec['url'],
                'revision': rrec['id'],
                'resource_profile': prec['resource_profile'],
                'command': rrec['commands'][0]['id'],
                'target': 'deploy'}
        if endpoint:
            data['static_endpoint'] = endpoint
        response = self._post(f'projects/{id}/deployments', json=data, format='json')
        if response.get('error'):
            raise RuntimeError('Error starting deployment: {}'.format(response['error']['message']))
        # The _wait method doesn't work here. The action isn't even updated, it seems
        while wait and response['state'] in ('initial', 'starting'):
            time.sleep(5)
            response = self._get(f'deployments/{response["id"]}', format='json')
        if wait and response['state'] != 'started':
            raise RuntimeError(f'Error completing deployment start: {response["status_text"]}')
        response['project_id'] = id
        return self._format_response(response, format=format, columns=_S_COLUMNS)

    def deployment_stop(self, ident):
        id = self._id('deployments', ident)
        self._delete(f'deployments/{id}', format='response')

    def job_list(self, format=None):
        return self._get('jobs', format=format, columns=_J_COLUMNS)

    def job_info(self, ident, format=None, quiet=False):
        id, record = self._id('jobs', ident, record=True, quiet=quiet)
        if id:
            return self._format_response(record, format=format, columns=_J_COLUMNS)

    def job_stop(self, ident):
        id = self._id('jobs', ident)
        self._delete(f'jobs/{id}', format='response')

    def run_list(self, format=None):
        return self._get('runs', format=format, columns=_J_COLUMNS)

    def run_info(self, ident, format=None, quiet=False):
        id, record = self._id('runs', ident, record=True, quiet=quiet)
        if id:
            return self._format_response(record, format=format, columns=_J_COLUMNS)

    def job_stop(self, ident):
        id = self._id('runs', ident)
        self._delete(f'runs/{id}', format='response')


class AEAdminSession(AESessionBase):
    def __init__(self, hostname, username, password=None, dataframe=False, retry=True, password_prompt=None):
        self._sdata = None
        self._login_url = f'/auth/realms/master/protocol/openid-connect/token'
        super(AEAdminSession, self).__init__(hostname, username, password,
                                             'auth/admin/realms/AnacondaPlatform',
                                             dataframe, retry, password_prompt)

    def _load(self):
        self._filename = os.path.join(config._path, 'tokens', f'{self.username}@{self.hostname}')
        if os.path.exists(self._filename):
            with open(self._filename, 'r') as fp:
                sdata = json.load(fp)
            os.utime(self._filename)
            self._sdata = self._post(self._login_url,
                                     data={'refresh_token': sdata['refresh_token'],
                                           'grant_type': 'refresh_token',
                                           'client_id': 'admin-cli'},
                                     format='json', pass_errors=True)
            self._set_header()

    def _connected(self):
        return isinstance(self._sdata, dict) and 'access_token' in self._sdata

    def _set_header(self):
        if self._connected():
            self.session.headers['Authorization'] = f'Bearer {self._sdata["access_token"]}'

    def _connect(self, password):
        self._sdata = self._post(self._login_url,
                                 data={'username': self.username,
                                       'password': password,
                                       'grant_type': 'password',
                                       'client_id': 'admin-cli'},
                                 format='json', pass_errors=True)
        self._set_header()

    def _save(self):
        os.makedirs(os.path.dirname(self._filename), mode=0o700, exist_ok=True)
        with open(self._filename, 'w') as fp:
            json.dump(self._sdata, fp)

    def user_list(self, format=None):
        return self._get(f'users', format=format, columns=_U_COLUMNS)

    def user_info(self, user_or_id, format=None):
        if re.match(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', user_or_id):
            response = [self._get(f'users/{user_or_id}', format='json')]
        else:
            response = self._get(f'users?username={user_or_id}', format='json')
        if len(response) == 0:
            raise ValueError(f'Could not find user {user_or_id}')
        return self._format_response(response[0], format, _U_COLUMNS)

    def impersonate(self, user_or_id):
        record = self.user_info(user_or_id, format='json')
        old_headers = self.session.headers.copy()
        try:
            self._post(f'users/{record["id"]}/impersonation', format='response')
            params = {'client_id': 'anaconda-platform',
                      'scope': 'openid',
                      'response_type': 'code',
                      'redirect_uri': f'https://{self.hostname}/login'}
            self._get('/auth/realms/AnacondaPlatform/protocol/openid-connect/auth', params=params, format='response')
            nsession = requests.Session()
            nsession.cookies, self.session.cookies = self.session.cookies, LWPCookieJar()
            nsession.headers = self.session.headers.copy()
            del nsession.headers['Authorization']
            return AEUserSession(self.hostname, record["username"], nsession)
        finally:
            self.session.cookies.clear()
            self.session.headers = old_headers
