import requests
import time
import io
import re
import os
import sys
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


_P_COLUMNS = [            'name', 'owner', 'editor',   'resource_profile',                                    'id',               'created', 'updated', 'url']  # noqa: E241, E201
_R_COLUMNS = [            'name', 'owner', 'commands',                                                        'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241, E201
_S_COLUMNS = [            'name', 'owner',             'resource_profile',                           'state', 'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241, E201
_D_COLUMNS = ['endpoint', 'name', 'owner', 'command',  'resource_profile', 'project_name', 'public', 'state', 'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241, E201
_J_COLUMNS = [            'name', 'owner', 'command',  'resource_profile', 'project_name',           'state', 'id', 'project_id', 'created', 'updated', 'url']  # noqa: E241, E201
_C_COLUMNS = ['id',  'permission', 'type', 'first name', 'last name', 'email']  # noqa: E241, E201
_U_COLUMNS = ['username', 'email', 'firstName', 'lastName', 'id']
_T_COLUMNS = ['name', 'id', 'description', 'is_template', 'is_default', 'download_url', 'owner', 'created', 'updated']
_A_COLUMNS = ['type', 'status', 'message', 'done', 'owner', 'id', 'description', 'created', 'updated']
_E_COLUMNS = ['id', 'owner', 'name', 'deployment_id', 'project_id', 'project_url']
_DTYPES = {'created': 'datetime', 'updated': 'datetime',
           'createdTimestamp': 'timestamp/ms', 'notBefore': 'timestamp/s'}


class AEException(RuntimeError):
    pass


class AEUnexpectedResponseError(AEException):
    def __init__(self, response, method, url, **kwargs):
        msg = [f'Unexpected response: {response.status_code} {response.reason}',
               f'  {method.upper()} {url}']
        if 'params' in kwargs:
            msg.append(f'  params: {kwargs["params"]}')
        if 'data' in kwargs:
            msg.append(f'  data: {kwargs["data"]}')
        if 'json' in kwargs:
            msg.append(f'  json: {kwargs["json"]}')
        super(AEUnexpectedResponseError, self).__init__('\n'.join(msg))
    pass


class AESessionBase(object):
    '''Base class for AE5 API interactions.'''

    def __init__(self, hostname, username, password, prefix, persist):
        '''Base class constructor.

        Args:
            hostname: The FQDN of the AE5 cluster
            username: The username associated with the connection.
            password (str, AEAdminSession, or None): nominally, this is
                the password used to log in, if it is necessary. If password=None, and
                the session has expired, it will prompt the user for a password. If
                password is an AEAdminSession, it will be used to impersonate the user.
            prefix (str): The URL prefix to prepend to all API calls.
            persist: if True, an attempt will be made to load the session from disk;
                and if a new login is required, it will save the session to disk. If
                false, session information will neither be loaded nor saved.
        '''
        if not hostname or not username:
            raise ValueError('Must supply hostname and username')
        self.hostname = hostname
        self.username = username
        self.password = password
        self.persist = persist
        self.prefix = prefix.lstrip('/')
        self.session = requests.Session()
        self.session.verify = False
        self.session.cookies = LWPCookieJar()
        if self.persist:
            self._load()
        self.connected = self._connected()
        if self.connected:
            self._set_header()

    @staticmethod
    def _auth_message(msg, nl=True):
        print(msg, file=sys.stderr, end='\n' if nl else '')

    @staticmethod
    def _password_prompt(key, last_valid=True):
        cls = AESessionBase
        if not last_valid:
            cls._auth_message('Invalid username or password; please try again.')
        while True:
            cls._auth_message(f'Password for {key}: ', False)
            password = getpass.getpass('')
            if password:
                return password
            cls._auth_message('Must supply a password.')

    def __del__(self):
        if not self.persist and self.connected:
            self.disconnect()

    def authorize(self):
        key = f'{self.username}@{self.hostname}'
        need_password = self.password is None
        last_valid = True
        while True:
            if need_password:
                password = self._password_prompt(key, last_valid)
            else:
                password = self.password
            self._connect(password)
            if self._connected():
                break
            if not need_password:
                raise AEException('Invalid username or password.')
            last_valid = False
        if self._connected():
            self.connected = True
            self._set_header()
            if self.persist:
                self._save()

    def disconnect(self):
        self._disconnect()
        self.session.headers.clear()
        self.session.cookies.clear()
        if self.persist:
            self._save()
        self.connected = False

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
        if isinstance(response, requests.models.Response):
            if format == 'response':
                return response
            if format == 'blob':
                return response.content
            if format == 'text':
                return response.text
            ctype = response.headers['content-type']
            if not ctype.endswith('json'):
                raise AEException(f'Content type {ctype} not compatible with json format')
            response = response.json()
        if columns and format == 'dataframe':
            return self._format_dataframe(response, columns)
        return response

    def _api(self, method, endpoint, **kwargs):
        fmt, cols = self._format_kwargs(kwargs)
        subdomain = kwargs.pop('subdomain', None)
        isabs, endpoint = endpoint.startswith('/'), endpoint.lstrip('/')
        if subdomain:
            subdomain += '.'
            isabs = True
        else:
            subdomain = ''
        if not isabs:
            endpoint = f'{self.prefix}/{endpoint}'
        url = f'https://{subdomain}{self.hostname}/{endpoint}'
        kwargs.update((('verify', False), ('allow_redirects', True)))
        if self.connected:
            response = getattr(self.session, method)(url, **kwargs)
        if not self.connected or response.status_code == 401:
            self.authorize()
            response = getattr(self.session, method)(url, **kwargs)
        if 400 <= response.status_code:
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
    def __init__(self, hostname, username, password=None, persist=True):
        self._filename = os.path.join(config._path, 'cookies', f'{username}@{hostname}')
        super(AEUserSession, self).__init__(hostname, username, password=password,
                                            prefix='api/v2', persist=persist)

    def _set_header(self):
        s = self.session
        for cookie in s.cookies:
            if cookie.name == '_xsrf':
                s.headers['x-xsrftoken'] = cookie.value
                break

    def _load(self):
        s = self.session
        if os.path.exists(self._filename):
            s.cookies.load(self._filename, ignore_discard=True)
            os.utime(self._filename)

    def _connected(self):
        return any(c.name == '_xsrf' for c in self.session.cookies)

    def _connect(self, password):
        if isinstance(password, AEAdminSession):
            self.session.cookies = password.impersonate(self.username)
        else:
            params = {'client_id': 'anaconda-platform', 'scope': 'openid',
                      'response_type': 'code', 'redirect_uri': f'https://{self.hostname}/login'}
            url = f'https://{self.hostname}/auth/realms/AnacondaPlatform/protocol/openid-connect/auth'
            resp = self.session.get(url, params=params)
            tree = html.fromstring(resp.text)
            form = tree.xpath("//form[@id='kc-form-login']")
            url = form[0].action
            self.session.post(url, data={'username': self.username, 'password': password})

    def _disconnect(self):
        # This will actually close out the session, so even if the cookie had
        # been captured for use elsewhere, it would no longer be useful.
        self._get('/logout', format='response')

    def _save(self):
        os.makedirs(os.path.dirname(self._filename), mode=0o700, exist_ok=True)
        self.session.cookies.save(self._filename, ignore_discard=True)
        os.chmod(self._filename, 0o600)

    def _id(self, type, ident, quiet=False):
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
        records = getattr(self, type.rstrip('s') + '_list')(internal=True, format='json')
        owner, name, id, pid = (ident.owner or '*', ident.name or '*',
                                ident.id if ident.id and type == idtype else '*',
                                ident.pid if ident.pid and type != 'projects' else '*')
        for rec in records:
            if (fnmatch(rec['owner'], owner) and fnmatch(rec['name'], name) and
                fnmatch(rec['id'], id) and fnmatch(rec.get('project_id', ''), pid)): # noqa
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
        return rec['id'], rec

    def _revision(self, ident, quiet=False):
        if isinstance(ident, str):
            ident = Identifier.from_string(ident)
        id, prec = self._id('projects', ident, quiet=quiet)
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
        return id, rev, prec, rrec

    def _id_or_name(self, type, ident, quiet=False):
        matches = []
        records = getattr(self, type.rstrip('s') + '_list')(format='json')
        for rec in records:
            if (fnmatch(rec['id'], ident) or fnmatch(rec['name'], ident)):
                matches.append(rec)
        if len(matches) > 1:
            attempt = [rec for rec in matches if fnmatch(rec['id'], ident)]
            if len(attempt) == 1:
                matches = attempt
        if len(matches) == 1:
            rec = matches[0]
            id = rec['id']
        elif quiet:
            id, rec = None, None
        else:
            pfx = 'Multiple' if len(matches) else 'No'
            msg = f'{pfx} {type}s found matching "{ident}"'
            if matches:
                matches = [f'{r["id"]}: {r["name"]}' for r in matches]
                msg += ':\n  - ' + '\n  - '.join(matches)
            raise ValueError(msg)
        return id, rec

    def project_list(self, collaborators=False, internal=False, format=None):
        records = self._get('projects', format='json')
        if collaborators:
            self._join_collaborators('projects', records)
        return self._format_response(records, format=format, columns=_P_COLUMNS)

    def project_info(self, ident, collaborators=False, format=None, quiet=False):
        id, record = self._id('projects', ident, quiet=quiet)
        if collaborators:
            self._join_collaborators('projects', record)
        return self._format_response(record, format=format, columns=_P_COLUMNS)

    def sample_list(self, format=None):
        result = []
        for sample in self._get('template_projects', format='json'):
            sample['is_template'] = True
            result.append(sample)
        for sample in self._get('sample_projects', format='json'):
            sample['is_template'] = sample['is_default'] = False
            result.append(sample)
        return self._format_response(result, format=format, columns=_T_COLUMNS)

    def sample_info(self, ident, format=None, quiet=False):
        id, record = self._id_or_name('sample', ident, quiet=quiet)
        return self._format_response(record, format=format, columns=_T_COLUMNS)

    def project_collaborator_list(self, ident, format=None):
        id, _ = self._id('projects', ident)
        return self._get(f'projects/{id}/collaborators', format=format, columns=_C_COLUMNS)

    def project_collaborator_info(self, ident, userid, format=None):
        collabs = self.project_collaborator_list(ident, format='json')
        for c in collabs:
            if userid == c['id']:
                return self._format_response(c, format=format, columns=_C_COLUMNS)
        else:
            raise AEException(f'Collaborator not found: {userid}')

    def project_collaborator_list_set(self, ident, collabs, format=None):
        id, _ = self._id('projects', ident)
        result = self._put(f'projects/{id}/collaborators', json=collabs, format='json')
        return self._format_response(result['collaborators'], format=format, columns=_C_COLUMNS)

    def project_collaborator_add(self, ident, userid, group=False, read_only=False, format='json'):
        id, _ = self._id('projects', ident)
        collabs = self.project_collaborator_list(id, format='json')
        ncollabs = len(collabs)
        if not isinstance(userid, tuple):
            userid = userid,
        collabs = [c for c in collabs if c['id'] not in userid]
        if len(collabs) != ncollabs:
            self.project_collaborator_list_set(id, collabs)
        type = 'group' if group else 'user'
        perm = 'r' if read_only else 'rw'
        collabs.extend({'id': u, 'type': type, 'permission': perm} for u in userid)
        return self.project_collaborator_list_set(id, collabs, format=format)

    def project_collaborator_remove(self, ident, userid, format='json'):
        id, _ = self._id('projects', ident)
        collabs = self.project_collaborator_list(id, format='json')
        if not isinstance(userid, tuple):
            userid = userid,
        missing = set(userid) - set(c['id'] for c in collabs)
        if missing:
            missing = ', '.join(missing)
            raise AEException(f'Collaborator(s) not found: {missing}')
        collabs = [c for c in collabs if c['id'] not in userid]
        return self.project_collaborator_list_set(id, collabs, format=format)

    def project_patch(self, ident, **kwargs):
        format = kwargs.pop('format', None)
        id, _ = self._id('projects', ident)
        data = {k: v for k, v in kwargs.items() if v is not None}
        if data:
            self._patch(f'projects/{id}', json=data, format='response')
        return self.project_info(id, format=format)

    def project_sessions(self, ident, format=None):
        id, _ = self._id('projects', ident)
        return self._get(f'projects/{id}/sessions', format=format, columns=_S_COLUMNS)

    def project_deployments(self, ident, format=None):
        id, _ = self._id('projects', ident)
        return self._get(f'projects/{id}/deployments', format=format, columns=_D_COLUMNS)

    def project_jobs(self, ident, format=None):
        id, _ = self._id('projects', ident)
        return self._get(f'projects/{id}/jobs', format=format, columns=_J_COLUMNS)

    def project_runs(self, ident, format=None):
        id, _ = self._id('projects', ident)
        return self._get(f'projects/{id}/runs', format=format, columns=_R_COLUMNS)

    def project_activity(self, ident, limit=0, latest=False, format=None):
        id, _ = self._id('projects', ident)
        limit = 1 if latest else (999999 if limit <= 0 else limit)
        params = {'sort': '-updated', 'page[size]': limit}
        response = self._get(f'projects/{id}/activity', params=params, format='json')['data']
        if latest:
            response = response[0]
        return self._format_response(response, format=format, columns=_A_COLUMNS)

    def revision_list(self, ident, format=None):
        id, _ = self._id('projects', ident)
        response = self._get(f'projects/{id}/revisions', format='json')
        for rec in response:
            rec['project_id'] = 'a0-' + rec['url'].rsplit('/', 3)[-3]
        return self._format_response(response, format=format, columns=_R_COLUMNS)

    def revision_info(self, ident, format=None, quiet=False):
        id, rev, prec, rrec = self._revision(ident, quiet=quiet)
        if id:
            rrec['project_id'] = prec['id']
            return self._format_response(rrec, format=format, columns=_R_COLUMNS)

    def project_download(self, ident, filename=None):
        id, rev, _, _ = self._revision(ident)
        response = self._get(f'projects/{id}/revisions/{rev}/archive', format='blob')
        if filename is None:
            return response
        with open(filename, 'wb') as fp:
            fp.write(response)

    def project_delete(self, ident):
        id, _ = self._id('projects', ident)
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

    def _join_collaborators(self, what, response):
        if isinstance(response, dict):
            collabs = self._get(f'{what}/{response["id"]}/collaborators', format='json')
            response['collaborators'] = ', '.join(c['id'] for c in collabs)
        elif response:
            for rec in response:
                self._join_collaborators(what, rec)

    def session_list(self, internal=False, format=None):
        response = self._get('sessions', format='json')
        # We need _join_projects even in internal mode to replace
        # the internal session name with the project name
        self._join_projects(response, 'session')
        return self._format_response(response, format, _S_COLUMNS)

    def session_info(self, ident, format=None, quiet=False):
        id, record = self._id('sessions', ident, quiet=quiet)
        if id:
            self._join_projects(record, 'session')
            return self._format_response(record, format, columns=_S_COLUMNS)

    def session_start(self, ident, wait=True, format=None):
        id, _ = self._id('projects', ident)
        response = self._post(f'projects/{id}/sessions', format='json')
        if response.get('error'):
            raise RuntimeError('Error starting project: {}'.format(response['error']['message']))
        if wait:
            response['action'] = self._wait(id, response['action'])
        if response['action'].get('error'):
            raise RuntimeError('Error completing session start: {}'.format(response['action']['message']))
        return self._format_response(response, format=format, columns=_S_COLUMNS)

    def session_stop(self, ident):
        id, _ = self._id('sessions', ident)
        self._delete(f'sessions/{id}', format='response')

    def deployment_list(self, collaborators=False, endpoints=True, internal=False, format=None):
        response = self._get('deployments', format='json')
        self._join_projects(response)
        if collaborators:
            self._join_collaborators('deployments', response)
        if endpoints and not internal:
            for record in response:
                if record.get('url'):
                    record['endpoint'] = record['url'].split('/', 3)[2].split('.', 1)[0]
        return self._format_response(response, format, _D_COLUMNS)

    def deployment_info(self, ident, collaborators=False, format=None, quiet=False):
        id, record = self._id('deployments', ident, quiet=quiet)
        self._join_projects(record)
        if collaborators:
            self._join_collaborators('deployments', record)
        if record.get('url'):
            record['endpoint'] = record['url'].split('/', 3)[2].split('.', 1)[0]
        return self._format_response(record, format, _D_COLUMNS)

    def endpoint_list(self, format=None):
        response = self._get('/platform/deploy/api/v1/apps/static-endpoints', format='json')
        response = response['data']
        self._join_projects(response, None)
        for rec in response:
            rec['deployment_id'] = 'a2-' + rec['deployment_id'] if rec['deployment_id'] else ''
        return self._format_response(response, format=format, columns=_E_COLUMNS)

    def endpoint_info(self, ident, quiet=False, format=None):
        id, rec = self._id_or_name('endpoint', ident, quiet=quiet)
        rec['deployment_id'] = 'a2-' + rec['deployment_id'] if rec['deployment_id'] else ''
        return self._format_response(rec, format=format, columns=_E_COLUMNS)

    def deployment_collaborators(self, ident, format=None):
        id, _ = self._id('deployments', ident)
        return self._get(f'deployments/{id}/collaborators', format=format, columns=_C_COLUMNS)

    def deployment_collaborator_list(self, ident, format=None):
        id, _ = self._id('deployments', ident)
        return self._get(f'deployments/{id}/collaborators', format=format, columns=_C_COLUMNS)

    def deployment_collaborator_info(self, ident, userid, format=None):
        collabs = self.deployment_collaborator_list(ident, format='json')
        for c in collabs:
            if userid == c['id']:
                return self._format_response(c, format=format, columns=_C_COLUMNS)
        else:
            raise AEException(f'Collaborator not found: {userid}')

    def deployment_collaborator_list_set(self, ident, collabs, format=None):
        id, _ = self._id('deployments', ident)
        result = self._put(f'deployments/{id}/collaborators', json=collabs, format='json')
        return self._format_response(result['collaborators'], format=format, columns=_C_COLUMNS)

    def deployment_collaborator_add(self, ident, userid, group=False, format='json'):
        id, _ = self._id('deployments', ident)
        collabs = self.deployment_collaborator_list(id, format='json')
        ncollabs = len(collabs)
        if not isinstance(userid, tuple):
            userid = userid,
        collabs = [c for c in collabs if c['id'] not in userid]
        if len(collabs) != ncollabs:
            self.deployment_collaborator_list_set(id, collabs)
        perm = 'r' if read_only else 'rw'
        collabs.extend({'id': u, 'type': 'r', 'permission': perm} for u in userid)
        return self.deployment_collaborator_list_set(id, collabs, format=format)

    def deployment_collaborator_remove(self, ident, userid, format='json'):
        id, _ = self._id('deployments', ident)
        collabs = self.deployment_collaborator_list(id, format='json')
        if not isinstance(userid, tuple):
            userid = userid,
        missing = set(userid) - set(c['id'] for c in collabs)
        if missing:
            missing = ', '.join(missing)
            raise AEException(f'Collaborator(s) not found: {missing}')
        collabs = [c for c in collabs if c['id'] not in userid]
        return self.deployment_collaborator_list_set(id, collabs, format=format)

    def deployment_start(self, ident, endpoint=None, command=None,
                         resource_profile=None, public=False,
                         collaborators=None, wait=True, format=None):
        id, rev, prec, rrec = self._revision(ident)
        data = {'name': prec['name'],
                'source': rrec['url'],
                'revision': rrec['id'],
                'resource_profile': resource_profile or prec['resource_profile'],
                'command': command or rrec['commands'][0]['id'],
                'public': bool(public),
                'target': 'deploy'}
        if endpoint:
            data['static_endpoint'] = endpoint
        response = self._post(f'projects/{id}/deployments', json=data, format='json')
        if response.get('error'):
            raise RuntimeError('Error starting deployment: {}'.format(response['error']['message']))
        if collaborators:
             self.deployment_set_collaborators(response['id'], collaborators)
        # The _wait method doesn't work here. The action isn't even updated, it seems
        while wait and response['state'] in ('initial', 'starting'):
            time.sleep(5)
            response = self._get(f'deployments/{response["id"]}', format='json')
        if wait and response['state'] != 'started':
            raise RuntimeError(f'Error completing deployment start: {response["status_text"]}')
        response['project_id'] = id
        return self._format_response(response, format=format, columns=_S_COLUMNS)

    def deployment_restart(self, ident, wait=True, format=None):
        id, record = self._id('deployments', ident)
        collab = self.deployment_collaborators(id, format='json')
        if record.get('url'):
            endpoint = record['url'].split('/', 3)[2].split('.', 1)[0]
            if id.endswith(endpoint):
                endpoint = None
        else:
            endpoint = None
        self._delete(f'deployments/{id}', format='response')
        return self.deployment_start(record['project_id'],
                                     endpoint=endpoint, command=record['command'],
                                     resource_profile=record['resource_profile'], public=record['public'],
                                     collaborators=collab, wait=wait, format=format)

    def deployment_patch(self, ident, **kwargs):
        format = kwargs.pop('format', None)
        id, _ = self._id('deployments', ident)
        data = {k: v for k, v in kwargs.items() if v is not None}
        if data:
            self._patch(f'deployments/{id}', json=data, format='response')
        return self.deployment_info(id, format=format)

    def deployment_stop(self, ident):
        id, _ = self._id('deployments', ident)
        self._delete(f'deployments/{id}', format='response')

    def job_list(self, internal=False, format=None):
        return self._get('jobs', format=format, columns=_J_COLUMNS)

    def job_info(self, ident, format=None, quiet=False):
        id, record = self._id('jobs', ident, quiet=quiet)
        if id:
            return self._format_response(record, format=format, columns=_J_COLUMNS)

    def job_stop(self, ident):
        id, _ = self._id('jobs', ident)
        self._delete(f'jobs/{id}', format='response')

    def run_list(self, internal=False, format=None):
        return self._get('runs', format=format, columns=_J_COLUMNS)

    def run_info(self, ident, format=None, quiet=False):
        id, record = self._id('runs', ident, quiet=quiet)
        if id:
            return self._format_response(record, format=format, columns=_J_COLUMNS)

    def run_stop(self, ident):
        id, _ = self._id('runs', ident)
        self._delete(f'runs/{id}', format='response')


class AEAdminSession(AESessionBase):
    def __init__(self, hostname, username, password=None, persist=True):
        self._sdata = None
        self._login_url = f'https://{hostname}/auth/realms/master/protocol/openid-connect/token'
        super(AEAdminSession, self).__init__(hostname, username, password,
                                             prefix='auth/admin/realms/AnacondaPlatform',
                                             persist=persist)

    def _load(self):
        self._filename = os.path.join(config._path, 'tokens', f'{self.username}@{self.hostname}')
        if os.path.exists(self._filename):
            with open(self._filename, 'r') as fp:
                sdata = json.load(fp)
            if isinstance(sdata, dict) and 'refresh_token' in sdata:
                resp = self.session.post(self._login_url,
                                         data={'refresh_token': sdata['refresh_token'],
                                               'grant_type': 'refresh_token',
                                               'client_id': 'admin-cli'})
                if resp.status_code == 200:
                    self._sdata = resp.json()

    def _connected(self):
        return isinstance(self._sdata, dict) and 'access_token' in self._sdata

    def _set_header(self):
        self.session.headers['Authorization'] = f'Bearer {self._sdata["access_token"]}'

    def _connect(self, password):
        resp = self.session.post(self._login_url,
                                 data={'username': self.username,
                                       'password': password,
                                       'grant_type': 'password',
                                       'client_id': 'admin-cli'})
        self._sdata = {} if resp.status_code == 401 else resp.json()

    def _disconnect(self):
        # There is currently no way to truly end an active admin session, but we
        # can clear all knowledge of it to force reauthentication.
        self._sdata.clear()

    def _save(self):
        os.makedirs(os.path.dirname(self._filename), mode=0o700, exist_ok=True)
        with open(self._filename, 'w') as fp:
            json.dump(self._sdata, fp)

    def user_list(self, internal=False, format=None):
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
            cookies, self.session.cookies = self.session.cookies, LWPCookieJar()
            return cookies
        finally:
            self.session.cookies.clear()
            self.session.headers = old_headers
