# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
from notebook.utils import url_path_join, url_unescape
from notebook.base.handlers import IPythonHandler, path_regex
from tornado import web, gen
from requests.exceptions import RequestException
from contextlib import closing
import requests
import tempfile
import re
import os
import urlparse

class BaseFetcher(object):
    '''BaseHandler for downloading content from external URLs.'''

    supported_content_types = ('text/plain', 'text/csv', 'application/json')
    max_content_length = 20480000 # 20 MB

    def get(self, url, dst, *args, **kwargs):
        '''
        Gets a resource at `url` and saves it to `dst` directory.

        :param url: url of resource to download
        :param dst: destination directory
        '''
        # default parameters
        timeout = kwargs.pop('timeout', 3.05)
        stream = kwargs.pop('stream', True)
        verify_ssl_certs = kwargs.pop('verify_ssl_certs', True)
        try:
            # current_app.logger.info('Requesting resource at url {}'.format(url))
            response = requests.get(url, *args,
                timeout=timeout, stream=stream, verify=verify_ssl_certs,
                **kwargs
            )
        except RequestException as exc:
            raise web.HTTPError(400, 'Unable to retrieve resource at {}: {}.'.format(url, exc.message))
        return self.process_response(response, dst)

    def process_response(self, response, dst):
        '''
        Handle the response from making the request.

        :param response: `requests.Response` object
        :param dst: destination directory
        '''
        with closing(response) as resp:
            if not resp.ok:
                raise web.HTTPError(resp.status_code,
                    'Unable to retrieve resource at {}: {}.'
                        .format(response.url, resp.reason or resp.text)
                )
            self.check_content_type(resp)
            self.check_content_length(resp)
            with tempfile.NamedTemporaryFile(mode='w+b', dir=dst, delete=False) as dst_file:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        dst_file.write(chunk)
                        dst_file.flush()
            return dst_file.name

    def check_content_type(self, response, supported_content_types=None):
        '''
        Validate whether response has supported `content-type` header.
        '''
        allowed = supported_content_types or self.supported_content_types
        content_type = response.headers.get('content-type', None)
        if (content_type is None or
            content_type.startswith(allowed) == False):
            raise web.HTTPError(415,
                'Error retrieving resource: Content-Type {} not supported.'
                    .format(content_type)
            )

    def check_content_length(self, response, max_length=None):
        '''
        Validate that response `content-length` is below threshold.
        '''
        max_len = max_length or self.max_content_length
        content_length = response.headers.get('content-length', None)
        if content_length is None:
            return
        try:
            content_length = int(content_length)
        except ValueError:
            return
        if content_length > max_len:
            raise web.HTTPError(413,
                'Error retrieving resource: content length {} exceeds maximum allowed {}.'
                    .format(content_length, max_len)
            )

class NBViewerHandler(object):
    '''
    Handles nbviewer.ipython.org URLs pointing to notebooks on GitHub, Gists,
    and raw web URLs.
    '''
    def get(self, url, dst, *args, **kwargs):
        # parse the url
        url_parts = urlparse.urlparse(url)
        path_segs = url_parts.path.split('/')
        # extract the main nbserver REST resource name: url, gist, github
        kind = path_segs[1]
        if kind == 'gist':
            url = 'https://gist.githubusercontent.com/{}/raw'.format('/'.join(path_segs[2:]))
        elif kind == 'github':
            url = 'https://raw.githubusercontent.com/{}/{}'.format('/'.join(path_segs[2:4]), '/'.join(path_segs[5:]))
        elif kind == 'url':
            url = 'http://{}'.format('/'.join(path_segs[2:]))
        else:
            raise web.HTTPError(400, 'Unknown nbviewer URL type {}'.format(kind))

        # delegate to the basehandler with our new URL
        bh = BaseFetcher()
        return bh.get(url, dst, *args, **kwargs)

class GitHubHandler(object):
    '''
    Handles URLs pointing to notebooks on GitHub and in Gists.
    '''
    def get(self, url, dst, *args, **kwargs):
        # parse the url
        url_parts = urlparse.urlparse(url)
        path_segs = url_parts.path.split('/')
        # extract the main nbserver REST resource name: url, gist, github
        site = url_parts.netloc
        if len(path_segs) > 2:
            if site == 'gist.github.com':
                url = 'https://gist.githubusercontent.com/{}/raw'.format('/'.join(path_segs[1:3]))
            elif site == 'github.com':
                url = 'https://raw.githubusercontent.com/{}/{}'.format('/'.join(path_segs[1:3]), '/'.join(path_segs[4:]))
            else:
                raise web.HTTPError(400, 'Unknown github URL type {}'.format(url))
        else:
            raise web.HTTPError(400, 'Unknown github URL type {}'.format(url))

        # delegate to the basehandler with our new URL
        bh = BaseFetcher()
        return bh.get(url, dst, *args, **kwargs)


# Associate fetcher classes to url patterns
__fetchers = [
    (NBViewerHandler, r'^http://nbviewer.ipython.org/.*'),
    (GitHubHandler, r'^https?://(gist\.)?github.com/.*'),
    (BaseFetcher, r'.*')
]

class FetchesHandler(IPythonHandler):
    def initialize(self, work_dir):
        self.work_dir = work_dir

    @web.authenticated
    @gen.coroutine
    def post(self, path):
        '''
        Fetch files from a URL and write them to disk.

        :param path:
        '''
        src_url = self.get_query_argument('url')
        dst_path = os.path.join(self.work_dir, path.strip('/'))

        for fetcher_cls, regex in __fetchers:
            match = re.match(regex, src_url)
            if match:
                fetcher = fetcher_cls()
                break
        else:
            fetcher = BaseFetcher()
        yield fetcher.get(src_url, dst_path)

        # files = self.request.files
        # if not len(files):
        #     raise web.HTTPError(400, 'missing files to upload')
        # root_path = os.path.join(self.work_dir, path.strip('/'))
        # written_paths = []
        # for filename, metas in files.items():
        #     path = url_unescape(os.path.join(root_path, filename))
        #     with open(path, 'wb') as fh:
        #         fh.write(metas[0].body)
        #     written_paths.append(path)
        # self.finish({
        #     'files' : written_paths,
        #     'path' : url_unescape(root_path)
        # })

def load_jupyter_server_extension(nb_app):
    web_app = nb_app.web_app
    host_pattern = '.*$'
    route_pattern = url_path_join(web_app.settings['base_url'],
        '/api/fetches%s' % path_regex)
    handler_kwargs = dict(work_dir=nb_app.notebook_dir)
    web_app.add_handlers(host_pattern, [
        (route_pattern, FetchesHandler, handler_kwargs)
    ])
