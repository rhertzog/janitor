#!/usr/bin/python
# Copyright (C) 2019 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Serve the janitor site."""

import asyncio
from aiohttp.web_urldispatcher import (
    PrefixResource,
    ResourceRoute,
    URL,
    UrlMappingMatchInfo,
    )
from ..config import get_suite_config
import gpg
import shutil
import tempfile
import time

from . import is_worker, html_template, update_vars_from_request


class ForwardedResource(PrefixResource):

    def __init__(self, prefix, location,
                 name=None,
                 expect_handler=None,
                 chunk_size=256 * 1024):
        while prefix.startswith('/'):
            prefix = prefix[1:]
        super().__init__('/' + prefix, name=name)
        self._chunk_size = chunk_size
        self._expect_handler = expect_handler
        self._location = location

        self._routes = {
            method: ResourceRoute(
                method, self._handle, self, expect_handler=expect_handler)
            for method in ['GET', 'POST', 'HEAD']}

    def url_for(self, path):
        while path.startswith('/'):
            path = path[1:]
        path = '/' + path
        return URL.build(path=self._prefix + path)

    def get_info(self):
        return {'location': self._location,
                'prefix': self._prefix}

    def set_options_route(self, handler):
        if 'OPTIONS' in self._routes:
            raise RuntimeError('OPTIONS route was set already')
        self._routes['OPTIONS'] = ResourceRoute(
            'OPTIONS', handler, self,
            expect_handler=self._expect_handler)

    async def resolve(self, request):
        path = request.rel_url.raw_path
        method = request.method
        allowed_methods = set(self._routes)
        if not path.startswith(self._prefix):
            return None, set()

        if method not in allowed_methods:
            return None, allowed_methods

        match_dict = {'path': URL.build(path=path[len(self._prefix)+1:],
                                        encoded=True).path}
        return (UrlMappingMatchInfo(match_dict, self._routes[method]),
                allowed_methods)

    def __len__(self):
        return len(self._routes)

    def __iter__(self):
        return iter(self._routes.values())

    async def _handle(self, request):
        rel_url = request.match_info['path']
        url = '%s/%s' % (self._location, rel_url)
        headers = {}
        for hdr in ['Accept', 'Pragma', 'Accept-Encoding', 'Content-Type']:
            value = request.headers.get(hdr)
            if value:
                headers[hdr] = value
        params = {}
        service = request.query.get('service')
        if service:
            params['service'] = service
        if await is_worker(request.app.database, request):
            params['allow_writes'] = '1'
        from janitor.trace import note, warning
        note('Forwarding: method: %s, url: %s, params: %r, headers: %r',
             request.method, url, params, headers)
        async with request.app.http_client_session.request(
                request.method, url, params=params, headers=headers,
                data=request.content) as client_response:
            status = client_response.status

            note('Forwarding result: %d', status)

            if status == 404:
                raise web.HTTPNotFound()

            if status == 401:
                raise web.HTTPUnauthorized(headers={
                    'WWW-Authenticate': 'Basic Realm="Debian Janitor"'})

            if status == 502:
                warning('Upstream URL %s returned 502', url)

            if status != 200:
                raise web.HTTPBadGateway(
                    text='Upstream server returned %d' % status)

            headers = {}
            for hdr in ['Expires', 'Pragma', 'Cache-Control']:
                value = client_response.headers.get(hdr)
                if value:
                    headers[hdr] = value

            response = web.StreamResponse(
                status=200,
                reason='OK',
                headers=headers,
            )

            response.content_type = client_response.content_type

            await response.prepare(request)
            while True:
                chunk = await client_response.content.read(self._chunk_size)
                if not chunk:
                    break
                await response.write(chunk)
        await response.write_eof()
        return response

    def __repr__(self):
        name = "'" + self.name + "'" if self.name is not None else ""
        return "<ForwardedResource {name} {prefix} -> {location!r}>".format(
            name=name, prefix=self._prefix, location=self._location)


@asyncio.coroutine
def debsso_middleware(app, handler):
    @asyncio.coroutine
    def wrapper(request):
        dn = request.headers.get('SSL_CLIENT_S_DN')
        request.debsso_email = None
        if dn and request.headers.get('SSL_CLIENT_VERIFY') == 'SUCCESS':
            m = re.match('.*CN=([^/,]+)', dn)
            if m:
                request.debsso_email = m.group(1)
        response = yield from handler(request)
        if request.debsso_email:
            response.headers['X-DebSSO-User'] = request.debsso_email
        return response
    return wrapper


def setup_debsso(app):
    app.middlewares.insert(0, debsso_middleware)


def iter_accept(request):
    return [
        h.strip() for h in request.headers.get('Accept', '*/*').split(',')]


async def get_credentials(session, publisher_url):
    url = urllib.parse.urljoin(publisher_url, 'credentials')
    async with session.get(url=url) as resp:
        if resp.status != 200:
            raise Exception('unexpected response')
        return await resp.json()


if __name__ == '__main__':
    import argparse
    import functools
    from gpg import Context
    import os
    import re
    from janitor import state
    from janitor.config import read_config
    from janitor.logs import get_log_manager
    from janitor.policy import read_policy
    from janitor.prometheus import setup_metrics
    from aiohttp import web, ClientSession
    from aiohttp.web_middlewares import normalize_path_middleware
    from ..pubsub import pubsub_reader, pubsub_handler, Topic
    import urllib.parse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, help='Host to listen on')
    parser.add_argument(
        '--port',
        type=int, help='Port to listen on', default=8080)
    parser.add_argument(
        '--publisher-url', type=str,
        default='http://localhost:9912/',
        help='URL for publisher.')
    parser.add_argument(
        '--runner-url', type=str,
        default='http://localhost:9911/',
        help='URL for runner.')
    parser.add_argument(
        '--archiver-url', type=str,
        default='http://localhost:9914/',
        help='URL for runner.')
    parser.add_argument(
        "--policy",
        help="Policy file to read.", type=str,
        default=os.path.join(
            os.path.dirname(__file__), '..', '..', 'policy.conf'))
    parser.add_argument(
        '--config', type=str, default='janitor.conf',
        help='Path to configuration.')
    parser.add_argument(
        '--debug', action='store_true',
        help='Enable debugging mode. For example, avoid minified JS.')
    parser.add_argument(
        '--no-external-workers', action='store_true', default=False,
        help='Disable support for external workers.')
    parser.add_argument(
        '--external-url', type=str, default=None, 
        help='External URL')

    args = parser.parse_args()

    if args.debug:
        minified = ''
    else:
        minified = 'min.'

    with open(args.config, 'r') as f:
        config = read_config(f)

    logfile_manager = get_log_manager(config.logs_location)

    async def handle_simple(templatename, request):
        from .generate import render_simple
        return web.Response(
            content_type='text/html', text=await render_simple(templatename),
            headers={'Cache-Control': 'max-age=3600'})

    @html_template(
        'lintian-fixes-start.html', headers={'Cache-Control': 'max-age=3600'})
    async def handle_lintian_fixes(request):
        from .lintian_fixes import render_start
        return await render_start()

    @html_template(
        'multiarch-fixes-start.html',
        headers={'Cache-Control': 'max-age=3600'})
    async def handle_multiarch_fixes(request):
        from .multiarch_hints import render_start
        return await render_start()

    @html_template('orphan-start.html')
    async def handle_orphan_start(request):
        return {}

    @html_template(
        'orphan-candidates.html', headers={'Cache-Control': 'max-age=3600'})
    async def handle_orphan_candidates(request):
        from .orphan import generate_candidates
        return await generate_candidates(request.app.database)

    @html_template(
        'merge-proposals.html',
        headers={'Cache-Control': 'max-age=60'})
    async def handle_merge_proposals(request):
        from .merge_proposals import write_merge_proposals
        suite = request.match_info['suite']
        return await write_merge_proposals(request.app.database, suite)

    async def handle_apt_repo(request):
        suite = request.match_info['suite']
        from .apt_repo import write_apt_repo
        async with request.app.database.acquire() as conn:
            template = request.app.jinja_env.get_template(suite + '.html')
            vs = await write_apt_repo(conn, suite)
            vs['suite_config'] = get_suite_config(request.app.config, suite)
            update_vars_from_request(vs, request)
            text = await template.render_async(**vs)
            return web.Response(
                content_type='text/html',
                text=text,
                headers={'Cache-Control': 'max-age=60'})

    async def handle_history(request):
        limit = int(request.query.get('limit', '100'))
        worker = request.query.get('worker', None)
        from .history import write_history
        async with request.app.database.acquire() as conn:
            return web.Response(
                content_type='text/html',
                text=await write_history(conn, worker=worker, limit=limit),
                headers={'Cache-Control': 'max-age=60'})

    @html_template(
        'credentials.html', headers={'Cache-Control': 'max-age=10'})
    async def handle_credentials(request):
        credentials = await get_credentials(
            request.app.http_client_session,
            request.app.publisher_url)
        pgp_fprs = []
        for keydata in credentials['pgp_keys']:
            result = request.app.gpg.key_import(
                keydata.encode('utf-8'))
            pgp_fprs.extend([i.fpr for i in result.imports])

        pgp_validity = {
            gpg.constants.VALIDITY_FULL: 'full',
            gpg.constants.VALIDITY_MARGINAL: 'marginal',
            gpg.constants.VALIDITY_NEVER: 'never',
            gpg.constants.VALIDITY_ULTIMATE: 'ultimate',
            gpg.constants.VALIDITY_UNDEFINED: 'undefined',
            gpg.constants.VALIDITY_UNKNOWN: 'unknown',
        }

        return {
            'format_pgp_date': lambda ts: time.strftime(
                '%Y-%m-%d', time.localtime(ts)),
            'pgp_validity': pgp_validity.get,
            'pgp_algo': gpg.core.pubkey_algo_name,
            'ssh_keys': credentials['ssh_keys'],
            'pgp_keys': request.app.gpg.keylist('\0'.join(pgp_fprs)),
            'hosting': credentials['hosting'],}

    async def handle_ssh_keys(request):
        credentials = await get_credentials(
            request.app.http_client_session,
            request.app.publisher_url)
        return web.Response(
            text='\n'.join(credentials['ssh_keys']),
            content_type='text/plain')

    async def handle_pgp_keys(request):
        credentials = await get_credentials(
            request.app.http_client_session,
            request.app.publisher_url)
        armored = request.match_info['extension'] == '.asc'
        if armored:
            return web.Response(
                text='\n'.join(credentials['pgp_keys']),
                content_type='application/pgp-keys')
        else:
            fprs = []
            for keydata in credentials['pgp_keys']:
                result = request.app.gpg.key_import(
                    keydata.encode('utf-8'))
                fprs.extend([i.fpr for i in result.imports])
            return web.Response(
                body=request.app.gpg.key_export_minimal('\0'.join(fprs)),
                content_type='application/pgp-keys')

    @html_template(
        'publish-history.html', headers={'Cache-Control': 'max-age=10'})
    async def handle_publish_history(request):
        limit = int(request.query.get('limit', '100'))
        from .publish import write_history
        async with request.app.database.acquire() as conn:
            return await write_history(conn, limit=limit)

    @html_template('queue.html', headers={'Cache-Control': 'max-age=10'})
    async def handle_queue(request):
        limit = int(request.query.get('limit', '100'))
        from .queue import write_queue
        return await write_queue(
            request.app.http_client_session, request.app.database,
            queue_status=app.runner_status, limit=limit)

    @html_template(
        'maintainer-stats.html',
        headers={'Cache-Control': 'max-age=60'})
    async def handle_cupboard_maintainer_stats(request):
        from .stats import write_maintainer_stats
        async with request.app.database.acquire() as conn:
            return await write_maintainer_stats(conn)

    @html_template(
        'maintainer-overview.html', headers={'Cache-Control': 'max-age=60'})
    async def handle_maintainer_overview(request):
        from .stats import write_maintainer_overview
        async with request.app.database.acquire() as conn:
            return await write_maintainer_overview(
                conn, request.match_info['maintainer'])

    async def handle_result_codes(request):
        from .result_codes import (
            generate_result_code_index,
            generate_result_code_page)
        from .. import state
        suite = request.query.get('suite')
        if suite is not None and suite.lower() == '_all':
            suite = None
        code = request.match_info.get('code')
        all_suites = [s.name for s in config.suite]
        async with request.app.database.acquire() as conn:
            if not code:
                stats = await state.stats_by_result_codes(conn, suite=suite)
                if suite:
                    suites = [suite]
                else:
                    suites = None
                never_processed = sum(dict(
                    await state.get_never_processed(conn, suites)).values())
                text = await generate_result_code_index(
                    stats, never_processed, suite, all_suites=all_suites)
            else:
                runs = [run async for run in state.iter_last_runs(
                    conn, code, suite=suite)]
                text = await generate_result_code_page(
                    code, runs, suite, all_suites=all_suites)
        return web.Response(
            content_type='text/html', text=text,
            headers={'Cache-Control': 'max-age=600'})

    async def handle_login(request):
        return web.Response(
            content_type='text/plain',
            text=repr(request.debsso_email))

    async def handle_static_file(path, request):
        return web.FileResponse(path)

    @html_template(
        'package-name-list.html', headers={'Cache-Control': 'max-age=600'})
    async def handle_pkg_list(request):
        # TODO(jelmer): The javascript plugin thingy should just redirect to
        # the right URL, not rely on query parameters here.
        pkg = request.query.get('package')
        if pkg:
            return web.HTTPFound(pkg)
        from .pkg import generate_pkg_list
        from .. import state
        async with request.app.database.acquire() as conn:
            packages = [
                (item.name, item.maintainer_email)
                for item in await state.iter_packages(conn)
                if not item.removed]
        return await generate_pkg_list(packages)

    @html_template(
        'by-maintainer-package-list.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_maintainer_list(request):
        from .pkg import generate_maintainer_list
        from .. import state
        async with request.app.database.acquire() as conn:
            packages = [
                (item.name, item.maintainer_email)
                for item in await state.iter_packages(conn)
                if not item.removed]
        return await generate_maintainer_list(packages)

    @html_template(
        'package-overview.html', 
        headers={'Cache-Control': 'max-age=600'})
    async def handle_pkg(request):
        from .pkg import generate_pkg_file
        from .. import state
        package_name = request.match_info['pkg']
        async with request.app.database.acquire() as conn:
            package = await state.get_package(conn, package_name)
            if package is None:
                raise web.HTTPNotFound(
                    text='No package with name %s' % package_name)
            merge_proposals = []
            async for (run, url, status) in state.iter_proposals_with_run(
                    conn, package=package.name):
                merge_proposals.append((url, status, run))
            runs = state.iter_runs(conn, package=package.name)
            return await generate_pkg_file(
                request.app.database, request.app.config,
                package, merge_proposals, runs)

    @html_template(
            'lintian-fixes-failed-list.html',
            headers={'Cache-Control': 'max-age=600'})
    async def handle_failed_lintian_brush_fixers_list(request):
        from .lintian_fixes import generate_failing_fixers_list
        return await generate_failing_fixers_list(request.app.database)

    @html_template(
            'lintian-fixes-failed.html',
            headers={'Cache-Control': 'max-age=600'})
    async def handle_failed_lintian_brush_fixers(request):
        from .lintian_fixes import generate_failing_fixer
        fixer = request.match_info['fixer']
        return await generate_failing_fixer(request.app.database, fixer)

    @html_template(
        'lintian-fixes-regressions.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_lintian_brush_regressions(request):
        from .lintian_fixes import generate_regressions_list
        return await generate_regressions_list(request.app.database)

    @html_template(
        'vcs-regressions.html', headers={'Cache-Control': 'max-age=600'})
    async def handle_vcs_regressions(request):
        async with request.app.database.acquire() as conn:
            regressions = await state.iter_vcs_regressions(conn)
        return {'regressions': regressions}

    @html_template(
        'run.html', headers={'Cache-Control': 'max-age=3600'})
    async def handle_run(request):
        from .pkg import generate_run_file
        from .. import state
        run_id = request.match_info['run_id']
        pkg = request.match_info.get('pkg')
        async with request.app.database.acquire() as conn:
            run = await state.get_run(conn, run_id, pkg)
            if run is None:
                raise web.HTTPNotFound(text='No run with id %r' % run_id)
        return await generate_run_file(
            request.app.database,
            request.app.http_client_session,
            request.app.config,
            request.app.archiver_url,
            logfile_manager, run, request.app.publisher_url,
            is_admin=is_admin(request))

    async def handle_log(request):
        pkg = request.match_info['pkg']
        filename = request.match_info['log']
        run_id = request.match_info['run_id']
        if not re.match('^[a-z0-9+-\\.]+$', pkg) or len(pkg) < 2:
            raise web.HTTPNotFound(
                text='No log file %s for run %s' % (filename, run_id))
        if not re.match('^[a-z0-9-]+$', run_id) or len(run_id) < 5:
            raise web.HTTPNotFound(
                text='No log file %s for run %s' % (filename, run_id))
        if not re.match('^[a-z0-9\\.]+$', filename) or len(filename) < 3:
            raise web.HTTPNotFound(
                text='No log file %s for run %s' % (filename, run_id))
        try:
            logfile = await logfile_manager.get_log(pkg, run_id, filename)
        except FileNotFoundError:
            raise web.HTTPNotFound(
                text='No log file %s for run %s' % (filename, run_id))
        else:
            with logfile as f:
                text = f.read().decode('utf-8', 'replace')
        return web.Response(
            content_type='text/plain', text=text,
            headers={'Cache-Control': 'max-age=3600'})

    @html_template(
        'ready-list.html',
        headers={'Cache-Control': 'max-age=60'})
    async def handle_ready_proposals(request):
        from .pkg import generate_ready_list
        suite = request.match_info.get('suite')
        review_status = request.query.get('review_status')
        return await generate_ready_list(
            request.app.database, suite, review_status)

    @html_template(
        'lintian-fixes-package.html',
         headers={'Cache-Control': 'max-age=600'})
    async def handle_lintian_fixes_pkg(request):
        from .lintian_fixes import generate_pkg_file
        # TODO(jelmer): Handle Accept: text/diff
        pkg = request.match_info['pkg']
        run_id = request.match_info.get('run_id')
        try:
            return await generate_pkg_file(
                request.app.database,
                request.app.config,
                request.app.policy,
                request.app.http_client_session,
                request.app.archiver_url,
                request.app.publisher_url, pkg, run_id)
        except KeyError:
            raise web.HTTPNotFound()

    @html_template(
        'orphan-package.html', headers={'Cache-Control': 'max-age=600'})
    async def handle_orphan_pkg(request):
        from .orphan import generate_pkg_file
        # TODO(jelmer): Handle Accept: text/diff
        pkg = request.match_info['pkg']
        run_id = request.match_info.get('run_id')
        try:
            return await generate_pkg_file(
                request.app.database,
                request.app.config,
                request.app.policy,
                request.app.http_client_session,
                request.app.archiver_url,
                request.app.publisher_url, pkg, run_id)
        except KeyError:
            raise web.HTTPNotFound()

    @html_template(
        'multiarch-fixes-package.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_multiarch_fixes_pkg(request):
        from .multiarch_hints import generate_pkg_file
        # TODO(jelmer): Handle Accept: text/diff
        pkg = request.match_info['pkg']
        run_id = request.match_info.get('run_id')
        try:
            return await generate_pkg_file(
                request.app.database,
                request.app.config,
                request.app.policy,
                request.app.http_client_session,
                request.app.archiver_url,
                request.app.publisher_url, pkg, run_id)
        except KeyError:
            raise web.HTTPNotFound()

    async def handle_lintian_fixes_tag_list(request):
        from .lintian_fixes import generate_tag_list
        async with request.app.database.acquire() as conn:
            text = await generate_tag_list(conn)
        return web.Response(
            content_type='text/html', text=text,
            headers={'Cache-Control': 'max-age=600'})

    @html_template(
        'multiarch-fixes-hint-list.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_multiarch_fixes_hint_list(request):
        from .multiarch_hints import generate_hint_list
        async with request.app.database.acquire() as conn:
            return await generate_hint_list(conn)

    @html_template(
        'lintian-fixes-tag.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_lintian_fixes_tag_page(request):
        from .lintian_fixes import generate_tag_page
        return await generate_tag_page(
            request.app.database, request.match_info['tag'])

    @html_template(
        'multiarch-fixes-hint.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_multiarch_fixes_hint_page(request):
        from .multiarch_hints import generate_hint_page
        return await generate_hint_page(
            request.app.database, request.match_info['hint'])

    async def handle_lintian_fixes_developer_table_page(request):
        from .lintian_fixes import generate_developer_table_page
        try:
            developer = request.match_info['developer']
        except KeyError:
            developer = request.query.get('developer')
        if developer and '@' not in developer:
            developer = '%s@debian.org' % developer
        text = await generate_developer_table_page(
            request.app.database, developer)
        return web.Response(
            content_type='text/html', text=text,
            headers={'Cache-Control': 'max-age=30'})

    @html_template(
        'new-upstream-package.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_new_upstream_pkg(request):
        from .new_upstream import generate_pkg_file
        suite = request.match_info['suite']
        pkg = request.match_info['pkg']
        run_id = request.match_info.get('run_id')
        try:
            return await generate_pkg_file(
                request.app.database,
                request.app.config,
                request.app.http_client_session,
                request.app.archiver_url,
                pkg, suite, run_id)
        except KeyError:
            raise web.HTTPNotFound()

    @html_template(
        'lintian-fixes-candidates.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_lintian_fixes_candidates(request):
        from .lintian_fixes import generate_candidates
        return await generate_candidates(request.app.database)

    @html_template(
        'multiarch-fixes-candidates.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_multiarch_fixes_candidates(request):
        from .multiarch_hints import generate_candidates
        return await generate_candidates(request.app.database)

    @html_template(
        'lintian-fixes-stats.html', headers={'Cache-Control': 'max-age=600'})
    async def handle_lintian_fixes_stats(request):
        from .lintian_fixes import generate_stats
        return await generate_stats(request.app.database)

    @html_template(
        'new-upstream-candidates.html',
        headers={'Cache-Control': 'max-age=600'})
    async def handle_new_upstream_candidates(request):
        from .new_upstream import generate_candidates
        suite = request.match_info['suite']
        return await generate_candidates(request.app.database, suite)

    @html_template('rejected.html')
    async def handle_rejected(request):
        from .review import generate_rejected
        suite = request.query.get('suite')
        async with request.app.database.acquire() as conn:
            return await generate_rejected(conn, suite=suite)

    async def handle_review_post(request):
        from .review import generate_review
        post = await request.post()
        async with request.app.database.acquire() as conn:
            run = await state.get_run(conn, post['run_id'])
            review_status = post['review_status'].lower()
            if review_status == 'reschedule':
                review_status = 'rejected'
                from ..schedule import do_schedule
                await do_schedule(
                    conn, run.package, run.suite,
                    refresh=True, requestor='reviewer')
            await state.set_run_review_status(
                conn, post['run_id'], review_status)
            text = await generate_review(
                conn, request.app.http_client_session,
                request.app.archiver_url, request.app.publisher_url,
                suites=[run.suite])
        return web.Response(content_type='text/html', text=text)

    async def handle_review(request):
        from .review import generate_review
        suites = request.query.getall('suite', None)
        async with request.app.database.acquire() as conn:
            text = await generate_review(
                conn, request.app.http_client_session,
                request.app.archiver_url, request.app.publisher_url,
                suites=suites)
        return web.Response(content_type='text/html', text=text)

    async def start_gpg_context(app):
        gpg_home = tempfile.TemporaryDirectory()
        gpg_context = gpg.Context(home_dir=gpg_home.name)
        app.gpg = gpg_context.__enter__()

        async def cleanup_gpg(app):
            gpg_context.__exit__(None, None, None)
            shutil.rmtree(gpg_home)

        app.on_cleanup.append(cleanup_gpg)

    async def start_pubsub_forwarder(app):

        async def listen_to_publisher_publish(app):
            url = urllib.parse.urljoin(app.publisher_url, 'ws/publish')
            async for msg in pubsub_reader(app.http_client_session, url):
                app.topic_notifications.publish(['publish', msg])

        async def listen_to_publisher_mp(app):
            url = urllib.parse.urljoin(app.publisher_url, 'ws/merge-proposal')
            async for msg in pubsub_reader(app.http_client_session, url):
                app.topic_notifications.publish(['merge-proposal', msg])

        app.runner_status = None

        async def listen_to_runner(app):
            url = urllib.parse.urljoin(app.runner_url, 'ws/queue')
            async for msg in pubsub_reader(app.http_client_session, url):
                app.runner_status = msg
                app.topic_notifications.publish(['queue', msg])

        for cb in [listen_to_publisher_publish, listen_to_publisher_mp,
                   listen_to_runner]:
            listener = app.loop.create_task(cb(app))

            async def stop_listener(app):
                listener.cancel()
                await listener
            app.on_cleanup.append(stop_listener)

    trailing_slash_redirect = normalize_path_middleware(append_slash=True)
    app = web.Application(middlewares=[trailing_slash_redirect])
    for path, templatename in [
            ('/', 'index'),
            ('/contact', 'contact'),
            ('/about', 'about'),
            ('/apt', 'apt'),
            ('/cupboard/', 'cupboard')]:
        app.router.add_get(
            path, functools.partial(handle_simple, templatename + '.html'),
            name=templatename)
    app.router.add_get(
        '/credentials', handle_credentials,
        name='credentials')
    app.router.add_get(
        '/ssh_keys', handle_ssh_keys,
        name='ssh-keys')
    app.router.add_get(
        '/pgp_keys{extension:(\.asc)?}', handle_pgp_keys,
        name='pgp-keys')
    app.router.add_get(
        '/lintian-fixes/', handle_lintian_fixes,
        name='lintian-fixes-start')
    app.router.add_get(
        '/multiarch-fixes/', handle_multiarch_fixes,
        name='multiarch-fixes-start')
    app.router.add_get(
        '/multiarch-fixes/by-hint/', handle_multiarch_fixes_hint_list,
        name='multiarch-fixes-hint-list')
    app.router.add_get(
        '/multiarch-fixes/by-hint/{hint}', handle_multiarch_fixes_hint_page,
        name='multiarch-fixes-hint')
    app.router.add_get(
        '/multiarch-fixes/candidates', handle_multiarch_fixes_candidates,
        name='multiarch-fixes-candidates')
    app.router.add_get(
        '/orphan/', handle_orphan_start,
        name='orphan-start')
    app.router.add_get(
        '/orphan/candidates', handle_orphan_candidates,
        name='orphan-candidates')
    SUITE_REGEX = '|'.join(
            ['lintian-fixes', 'fresh-snapshots', 'fresh-releases',
             'multiarch-fixes', 'orphan'])
    app.router.add_get(
        '/{suite:%s}/merge-proposals' % SUITE_REGEX,
        handle_merge_proposals,
        name='suite-merge-proposals')
    app.router.add_get(
        '/{suite:%s}/ready' % SUITE_REGEX,
        handle_ready_proposals,
        name='suite-ready')
    app.router.add_get(
        '/{suite:%s}/maintainer' % SUITE_REGEX, handle_maintainer_list,
        name='suite-maintainer-list')
    app.router.add_get(
        '/{suite:%s}/pkg/' % SUITE_REGEX, handle_pkg_list,
        name='suite-package-list')
    apt_location = config.apt_location or args.archiver_url
    app.router.register_resource(
        ForwardedResource('dists', apt_location.rstrip('/') + '/dists'))
    app.router.register_resource(
        ForwardedResource('pool', apt_location.rstrip('/') + '/pool'))
    app.router.register_resource(
        ForwardedResource(
            'bzr', args.publisher_url.rstrip('/') + '/bzr'))
    app.router.register_resource(
        ForwardedResource(
            'git', args.publisher_url.rstrip('/') + '/git'))
    app.router.add_get(
        '/orphan/pkg/{pkg}/', handle_orphan_pkg,
        name='orphan-package')
    app.router.add_get(
        '/multiarch-fixes/pkg/{pkg}/', handle_multiarch_fixes_pkg,
        name='multiarch-fixes-package')
    app.router.add_get(
        '/multiarch-fixes/pkg/{pkg}/{run_id}', handle_multiarch_fixes_pkg,
        name='multiarch-fixes-package-run')
    app.router.add_get(
        '/{suite:unchanged}', handle_apt_repo,
        name='unchanged-start')
    app.router.add_get(
        '/lintian-fixes/pkg/{pkg}/', handle_lintian_fixes_pkg,
        name='lintian-fixes-package')
    app.router.add_get(
        '/lintian-fixes/pkg/{pkg}/{run_id}', handle_lintian_fixes_pkg,
        name='lintian-fixes-package-run')
    app.router.add_get(
        '/lintian-fixes/by-tag/', handle_lintian_fixes_tag_list,
        name='lintian-fixes-tag-list')
    app.router.add_get(
        '/lintian-fixes/by-tag/{tag}', handle_lintian_fixes_tag_page,
        name='lintian-fixes-tag')
    app.router.add_get(
        '/lintian-fixes/by-developer',
        handle_lintian_fixes_developer_table_page,
        name='lintian-fixes-developer-list')
    app.router.add_get(
        '/lintian-fixes/by-developer/{developer}',
        handle_lintian_fixes_developer_table_page,
        name='lintian-fixes-developer')
    app.router.add_get(
        '/lintian-fixes/candidates', handle_lintian_fixes_candidates,
        name='lintian-fixes-candidates')
    app.router.add_get(
        '/lintian-fixes/stats', handle_lintian_fixes_stats,
        name='lintian-fixes-stats')
    NEW_UPSTREAM_REGEX = 'fresh-(releases|snapshots)'
    app.router.add_get(
        '/{suite:%s}/' % NEW_UPSTREAM_REGEX, handle_apt_repo,
        name='new-upstream-start')
    app.router.add_get(
        '/{suite:%s}/pkg/{pkg}/' % NEW_UPSTREAM_REGEX,
        handle_new_upstream_pkg,
        name='new-upstream-package')
    app.router.add_get(
        '/{suite:%s}/pkg/{pkg}/{run_id}' % NEW_UPSTREAM_REGEX,
        handle_new_upstream_pkg,
        name='new-upstream-run')
    app.router.add_get(
        '/{suite:%s}/candidates' % NEW_UPSTREAM_REGEX,
        handle_new_upstream_candidates,
        name='new-upstream-candidates')

    app.router.add_get('/cupboard/history', handle_history, name='history')
    app.router.add_get('/cupboard/queue', handle_queue, name='queue')
    app.router.add_get('/cupboard/result-codes/', handle_result_codes,
                       name='result-code-list')
    app.router.add_get('/cupboard/result-codes/{code}', handle_result_codes,
                       name='result-code')
    app.router.add_get(
        '/cupboard/maintainer-stats', handle_cupboard_maintainer_stats,
        name='cupboard-maintainer-stats')
    app.router.add_get(
        '/cupboard/maintainer', handle_maintainer_list, name='maintainer-list')
    app.router.add_get(
        '/cupboard/maintainer/{maintainer}', handle_maintainer_overview,
        name='maintainer-overview')
    app.router.add_get(
        '/cupboard/publish', handle_publish_history, name='publish-history')
    app.router.add_get(
        '/cupboard/ready', handle_ready_proposals,
        name='cupboard-ready')
    app.router.add_get(
        '/cupboard/pkg/', handle_pkg_list, name='package-list')
    app.router.add_get(
        '/cupboard/pkg/{pkg}/', handle_pkg, name='cupboard-package')
    app.router.add_get(
        '/cupboard/pkg/{pkg}/{run_id}/', handle_run,
        name='cupboard-run')
    app.router.add_get(
        '/cupboard/review', handle_review,
        name='cupboard-review')
    app.router.add_get(
        '/cupboard/rejected', handle_rejected,
        name='cupboard-rejected')
    app.router.add_post(
        '/cupboard/review', handle_review_post,
        name='cupboard-review-post')
    app.router.add_get(
        '/cupboard/failed-lintian-brush-fixers/',
        handle_failed_lintian_brush_fixers_list,
        name='failed-lintian-brush-fixer-list')
    app.router.add_get(
        '/cupboard/failed-lintian-brush-fixers/{fixer}',
        handle_failed_lintian_brush_fixers,
        name='failed-lintian-brush-fixer')
    app.router.add_get(
        '/cupboard/lintian-brush-regressions/',
        handle_lintian_brush_regressions,
        name='lintian-brush-regressions')
    app.router.add_get(
        '/cupboard/pkg/{pkg}/{run_id}/{log:.*\\.log(\\.[0-9]+)?}', handle_log,
        name='logfile')
    app.router.add_get(
        '/cupboard/vcs-regressions/',
        handle_vcs_regressions,
        name='vcs-regressions')
    app.router.add_get(
        '/login', handle_login,
        name='login')
    for entry in os.scandir(
            os.path.join(os.path.dirname(__file__), '_static')):
        app.router.add_get(
            '/_static/%s' % entry.name,
            functools.partial(handle_static_file, entry.path))
    app.router.add_static(
        '/_static/images/datatables', '/usr/share/javascript/jquery-datatables/images')
    app.router.add_get(
        '/janitor.asc', functools.partial(
            handle_static_file,
            os.path.join(
                os.path.dirname(__file__), '..', '..', 'janitor.asc')),
        name='gpg-key')
    for (name, kind, basepath) in [
            ('chart', 'js', '/usr/share/javascript/chart.js/Chart'),
            ('chart', 'css', '/usr/share/javascript/chart.js/Chart'),
            ('jquery', 'js', '/usr/share/javascript/jquery/jquery'),
            ('jquery.typeahead', 'js',
             '/usr/share/javascript/jquery-typeahead/jquery.typeahead'),
            ('jquery.datatables', 'js',
             '/usr/share/javascript/jquery-datatables/jquery.dataTables'),
            ('moment', 'js', '/usr/share/javascript/moment/moment'),
            ]:
        if not os.path.exists(basepath + '.' + kind):
            continue
        app.router.add_get(
            '/_static/%s.%s' % (name, kind), functools.partial(
                handle_static_file,
                '%s.%s%s' % (basepath, minified, kind)))

    from .api import create_app as create_api_app
    with open(args.policy, 'r') as f:
        policy_config = read_policy(f)

    app.http_client_session = ClientSession()
    app.topic_notifications = Topic()
    app.runner_url = args.runner_url
    app.archiver_url = args.archiver_url
    app.policy = policy_config
    app.publisher_url = args.publisher_url
    app.on_startup.append(start_pubsub_forwarder)
    app.on_startup.append(start_gpg_context)
    if args.external_url:
        app.external_url = URL(args.external_url)
    else:
        app.external_url = None
    database = state.Database(config.database_location)
    app.database = database
    from .stats import stats_app
    app.add_subapp('/cupboard/stats', stats_app(database))
    app.config = config
    from janitor.site import env, is_admin
    app.jinja_env = env
    setup_debsso(app)
    setup_metrics(app)
    app.router.add_get(
        '/ws/notifications',
        functools.partial(pubsub_handler,
                          app.topic_notifications),  # type: ignore
        name='ws-notifications')
    app.add_subapp(
        '/api', create_api_app(
            app.database, args.publisher_url, args.runner_url,  # type: ignore
            args.archiver_url, policy_config,
            enable_external_workers=(not args.no_external_workers),
            external_url=(
                app.external_url.join(URL('api')
                if app.external_url else None))))
    web.run_app(app, host=args.host, port=args.port)
