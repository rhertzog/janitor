#!/usr/bin/python3
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
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

from aiohttp import web
import asyncio
from datetime import datetime
import json
import os
import shutil
import signal
import sys
import tempfile
import urllib.parse
import uuid

from debian.deb822 import Changes

from breezy.branch import Branch
from breezy.controldir import ControlDir, format_registry
from breezy.errors import (
    NotBranchError,
    NoRepositoryPresent,
    InvalidHttpResponse,
    )
from breezy.git.remote import RemoteGitError
from breezy.plugins.debian.util import (
    debsign,
    dget_changes,
    )

from prometheus_client import (
    Counter,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

from silver_platter.debian.lintian import (
    create_mp_description,
    parse_mp_description,
    update_proposal_commit_message,
    )
from silver_platter.proposal import (
    find_existing_proposed,
    enable_tag_pushing,
    UnsupportedHoster,
    NoSuchProject,
    get_hoster,
    )
from silver_platter.utils import (
    open_branch,
    BranchUnavailable,
    )

from . import state
from .publish import (
    PublishFailure,
    Publisher,
    )
from .trace import note, warning

packages_processed_count = Counter(
    'package_count', 'Number of packages processed.')
queue_length = Gauge(
    'queue_length', 'Number of items in the queue.')
last_success_gauge = Gauge(
    'job_last_success_unixtime',
    'Last time a batch job successfully finished')


ADDITIONAL_COLOCATED_BRANCHES = ['pristine-tar', 'upstream']


class NoChangesFile(Exception):
    """No changes file found."""


def get_vcs_abbreviation(branch):
    vcs = getattr(branch.repository, 'vcs', None)
    if vcs:
        return vcs.abbreviation
    return 'bzr'


def get_own_resume_branch(vcs_type, package, branch_name):
    if vcs_type == 'git':
        url = 'https://janitor.debian.net/git/%s,branch=%s' % (package, branch_name)
    elif vcs_type == 'bzr':
        url = 'https://janitor.debian.net/bzr/%s/%s' % (package, branch_name)
    else:
        raise AssertionError('unknown vcs type %r' % vcs_type)
    try:
        return Branch.open(url)
    except NotBranchError:
        return None
    except RemoteGitError:
        return None
    except InvalidHttpResponse:
        return None


class LintianBrushRunner(object):

    def __init__(self, args):
        self.args = args

    def branch_name(self):
        return "lintian-fixes"

    def get_proposal_description(self, existing_description):
        if existing_description:
            existing_lines = parse_mp_description(existing_description)
        else:
            existing_lines = []
        return create_mp_description(
            existing_lines + [l['summary'] for l in self.applied])

    def get_proposal_commit_message(self, existing_commit_message):
        fixed_tags = set()
        for result in self.applied:
            fixed_tags.update(result['fixed_lintian_tags'])
        return update_proposal_commit_message(
            existing_commit_message, fixed_tags)

    def read_worker_result(self, result):
        self.applied = result['applied']
        self.failed = result['failed']
        self.add_on_only = result['add_on_only']

    def allow_create_proposal(self):
        return self.applied and not self.add_on_only


class NewUpstreamRunner(object):

    def __init__(self, args):
        self.args = args

    def branch_name(self):
        if '--snapshot' in self.args:
            return "new-upstream-snapshot"
        else:
            return "new-upstream"

    def read_worker_result(self, result):
        self._upstream_version = result['upstream_version']

    def get_proposal_description(self, existing_description):
        return "New upstream version %s" % self._upstream_version

    def get_proposal_commit_message(self, existing_commit_message):
        return self.get_proposal_description(None)

    def allow_create_proposal(self):
        # No upstream release too small...
        return True


class JanitorResult(object):

    def __init__(self, pkg, log_id, description=None,
                 code=None, proposal=None, is_new=None,
                 build_distribution=None, build_version=None,
                 changes_filename=None, worker_result=None):
        self.package = pkg
        self.log_id = log_id
        self.description = description
        self.code = code
        self.proposal = proposal
        self.is_new = is_new
        self.build_distribution = build_distribution
        self.build_version = build_version
        self.changes_filename = changes_filename
        self.branch_name = None
        self.revision = None
        if worker_result:
            self.context = worker_result.context
            if self.code is None:
                self.code = worker_result.code
            if self.description is None:
                self.description = worker_result.description
            self.main_branch_revision = worker_result.main_branch_revision
        else:
            self.context = None
            self.main_branch_revision = None


def find_changes(path, package):
    for name in os.listdir(path):
        if name.startswith('%s_' % package) and name.endswith('.changes'):
            break
    else:
        raise NoChangesFile(path, package)

    with open(os.path.join(path, name), 'r') as f:
        changes = Changes(f)
        return (name, changes["Version"], changes["Distribution"])


class WorkerResult(object):

    def __init__(self, code, description, context=None, subworker=None,
                 main_branch_revision=None):
        self.code = code
        self.description = description
        self.context = context
        self.subworker = subworker
        self.main_branch_revision = main_branch_revision

    @classmethod
    def from_file(cls, path):
        with open(path, 'r') as f:
            worker_result = json.load(f)
        return cls(
                worker_result.get('code'), worker_result.get('description'),
                worker_result.get('context'), worker_result.get('subworker'),
                worker_result.get('main_branch_revision'))


async def invoke_subprocess_worker(
        worker_kind, main_branch, env, command, output_directory,
        resume_branch=None, pre_check=None, post_check=None,
        build_command=None, log_path=None):
    subprocess_env = dict(os.environ.items())
    for k, v in env.items():
        if v is not None:
            subprocess_env[k] = v
    worker_module = {
        'local': 'janitor.worker',
        'gcb': 'janitor.gcb_worker',
        }[worker_kind]
    args = [sys.executable, '-m', worker_module,
            '--branch-url=%s' % main_branch.user_url,
            '--output-directory=%s' % output_directory]
    if resume_branch:
        args.append('--resume-branch-url=%s' % resume_branch.user_url)
    if pre_check:
        args.append('--pre-check=%s' % pre_check)
    if post_check:
        args.append('--post-check=%s' % post_check)
    if build_command:
        args.append('--build-command=%s' % build_command)

    args.extend(command)

    if log_path:
        read, write = os.pipe()
        p = await asyncio.create_subprocess_exec(
            *args, env=subprocess_env, stdout=write, stderr=write)
        os.close(write)
        tee = await asyncio.create_subprocess_exec('tee', log_path, stdin=read)
        os.close(read)
        await tee.wait()
        return await p.wait()
    else:
        p = await asyncio.create_subprocess_exec(
            *args, env=subprocess_env)
        return await p.wait()


def copy_vcs_dir(main_branch, local_branch, vcs_result_dir, pkg, name,
                 additional_colocated_branches=None):
    """Publish resulting changes in VCS form.

    This creates a repository with the following branches:
     * master - the original Debian packaging branch
     * KIND - whatever command was run
     * upstream - the upstream branch (optional)
     * pristine-tar the pristine tar packaging branch (optional)
    """
    vcs = get_vcs_abbreviation(main_branch)
    if vcs == 'git':
        path = os.path.join(vcs_result_dir, 'git', pkg)
        os.makedirs(path, exist_ok=True)
        try:
            vcs_result_controldir = ControlDir.open(path)
        except NotBranchError:
            vcs_result_controldir = ControlDir.create(
                path, format=format_registry.get('git-bare')())
        for (from_branch, target_branch_name) in [
                (main_branch, 'master'),
                (local_branch, name)]:
            try:
                target_branch = vcs_result_controldir.open_branch(
                    name=target_branch_name)
            except NotBranchError:
                target_branch = vcs_result_controldir.create_branch(
                    name=target_branch_name)
            # TODO(jelmer): Set depth
            from_branch.push(target_branch, overwrite=True)
        for branch_name in (additional_colocated_branches or []):
            try:
                from_branch = local_branch.controldir.open_branch(
                    name=branch_name)
            except NotBranchError:
                continue
            try:
                target_branch = vcs_result_controldir.open_branch(
                    name=branch_name)
            except NotBranchError:
                target_branch = vcs_result_controldir.create_branch(
                    name=branch_name)
            from_branch.push(target_branch, overwrite=True)
    elif vcs == 'bzr':
        path = os.path.join(vcs_result_dir, 'bzr', pkg)
        os.makedirs(path, exist_ok=True)
        try:
            vcs_result_controldir = ControlDir.open(path)
        except NotBranchError:
            vcs_result_controldir = ControlDir.create(
                path, format=format_registry.get('bzr')())
        try:
            vcs_result_controldir.open_repository()
        except NoRepositoryPresent:
            vcs_result_controldir.create_repository(shared=True)
        for (from_branch, target_branch_name) in [
                (local_branch, name),
                (main_branch, 'master')]:
            target_branch_path = os.path.join(path, target_branch_name)
            try:
                target_branch = Branch.open(target_branch_path)
            except NotBranchError:
                target_branch = ControlDir.create_branch_convenience(
                    target_branch_path)
            target_branch.set_stacked_on_url(main_branch.user_url)
            from_branch.push(target_branch, overwrite=True)
    else:
        raise AssertionError('unsupported vcs %s' % vcs.abbreviation)


async def process_one(
        worker_kind, vcs_url, mode, env, command,
        build_command, publisher,
        pre_check=None, post_check=None,
        dry_run=False, incoming=None, log_dir=None,
        debsign_keyid=None, vcs_result_dir=None,
        possible_transports=None, possible_hosters=None):
    maintainer_email = env['MAINTAINER_EMAIL']
    pkg = env['PACKAGE']
    note('Running %r on %s', command, pkg)
    packages_processed_count.inc()
    log_id = str(uuid.uuid4())

    if command[0] == "new-upstream":
        subrunner = NewUpstreamRunner(command[1:])
    elif command[0] == "lintian-brush":
        subrunner = LintianBrushRunner(command[1:])
    else:
        raise AssertionError('Unknown command %s' % command[0])

    try:
        main_branch = open_branch(
            vcs_url, possible_transports=possible_transports)
    except BranchUnavailable as e:
        if str(e).startswith('Unsupported protocol for url '):
            code = 'unsupported-vcs-protocol'
        elif 'http code 429: Too Many Requests' in str(e):
            code = 'too-many-requests'
        elif str(e).startswith('Branch does not exist: Not a branch: '
                               '"https://anonscm.debian.org'):
            code = 'hosted-on-alioth'
        else:
            code = 'branch-unavailable'
        return JanitorResult(
            pkg, log_id=log_id, description=str(e), code=code)
    except KeyError as e:
        if e.args == ('www-authenticate not found',):
            return JanitorResult(
                pkg, log_id=log_id, description=str(e),
                code='401-without-www-authenticate')
        else:
            raise

    branch_name = subrunner.branch_name()
    try:
        hoster = get_hoster(main_branch, possible_hosters=possible_hosters)
    except UnsupportedHoster as e:
        if mode not in ('push', 'build-only'):
            netloc = urllib.parse.urlparse(main_branch.user_url).netloc
            return JanitorResult(
                pkg, log_id, description='Hoster unsupported: %s.' % netloc,
                code='hoster-unsupported')
        # We can't figure out what branch to resume from when there's no hoster
        # that can tell us.
        resume_branch = None
        existing_proposal = None
        if mode == 'push':
            warning('Unsupported hoster (%s), will attempt to push to %s',
                    e, main_branch.user_url)
    else:
        try:
            (resume_branch, overwrite, existing_proposal) = (
                find_existing_proposed(
                    main_branch, hoster, branch_name))
        except NoSuchProject as e:
            if mode not in ('push', 'build-only'):
                return JanitorResult(
                    pkg, log_id,
                    description='Project %s not found.' % e.project,
                    code='project-not-found')
            resume_branch = None
            existing_proposal = None

    if resume_branch is None:
        resume_branch = get_own_resume_branch(
            get_vcs_abbreviation(main_branch), pkg, branch_name)

    if resume_branch is not None:
        note('Resuming from %s', resume_branch.user_url)

    with tempfile.TemporaryDirectory() as output_directory:
        log_path = os.path.join(output_directory, 'worker.log')
        retcode = await invoke_subprocess_worker(
                worker_kind, main_branch, env, command, output_directory,
                resume_branch=resume_branch, pre_check=pre_check,
                post_check=post_check, build_command=build_command,
                log_path=log_path)

        if retcode != 0:
            return JanitorResult(
                pkg, log_id=log_id,
                code='worker-failure',
                description='Worker exited with return code %d' % retcode)

        for name in ['build.log', 'worker.log', 'result.json']:
            src_build_log_path = os.path.join(output_directory, name)
            if os.path.exists(src_build_log_path):
                dest_build_log_path = os.path.join(
                    log_dir, pkg, log_id)
                os.makedirs(dest_build_log_path, exist_ok=True)
                shutil.copy(src_build_log_path, dest_build_log_path)

        json_result_path = os.path.join(output_directory, 'result.json')
        if os.path.exists(json_result_path):
            worker_result = WorkerResult.from_file(json_result_path)
        else:
            worker_result = WorkerResult(
                'worker-missing-result',
                'Worker failed and did not write a result file.')
        if worker_result.subworker:
            subrunner.read_worker_result(worker_result.subworker)

        if worker_result.code is not None:
            return JanitorResult(
                pkg, log_id=log_id, worker_result=worker_result)

        result = JanitorResult(
            pkg, log_id=log_id,
            code='success', worker_result=worker_result)

        try:
            (result.changes_filename, result.build_version,
             result.build_distribution) = find_changes(
                 output_directory, result.package)
        except NoChangesFile as e:
            # Oh, well.
            note('No changes file found: %s', e)

        try:
            local_branch = open_branch(os.path.join(output_directory, pkg))
        except BranchUnavailable as e:
            return JanitorResult(
                pkg, log_id,
                description='result branch missing: %s' % e,
                code='result-branch-unavailable',
                worker_result=worker_result)

        result.revision = local_branch.last_revision().decode('utf-8')
        enable_tag_pushing(local_branch)
        if mode != 'build-only':
            try:
                result.proposal, result.is_new = publisher.publish(
                    pkg, maintainer_email,
                    subrunner, mode, hoster, main_branch, local_branch,
                    resume_branch,
                    dry_run=dry_run, log_id=log_id,
                    existing_proposal=existing_proposal)
            except PublishFailure as e:
                return JanitorResult(
                    pkg, log_id,
                    code=e.code,
                    description=e.description,
                    worker_result=worker_result)

        if vcs_result_dir:
            copy_vcs_dir(
                main_branch, local_branch,
                vcs_result_dir, pkg, branch_name,
                additional_colocated_branches=(
                    ADDITIONAL_COLOCATED_BRANCHES))
            result.branch_name = branch_name

        if result.changes_filename:
            changes_path = os.path.join(
                output_directory, result.changes_filename)
            debsign(changes_path, debsign_keyid)
            if incoming is not None:
                dget_changes(changes_path, incoming)

    return result


async def export_queue_length():
    while True:
        queue_length.set(state.queue_length())
        await asyncio.sleep(60)


async def process_queue(
        worker_kind, build_command, publisher,
        pre_check=None, post_check=None,
        dry_run=False, incoming=None, log_dir=None,
        debsign_keyid=None, vcs_result_dir=None,
        concurrency=1):
    async def process_queue_item(item):
        (queue_id, vcs_url, mode, env, command) = item
        start_time = datetime.now()
        result = await process_one(
            worker_kind, vcs_url, mode, env, command,
            pre_check=pre_check,
            build_command=build_command, publisher=publisher,
            post_check=post_check,
            dry_run=dry_run, incoming=incoming,
            debsign_keyid=debsign_keyid, vcs_result_dir=vcs_result_dir,
            log_dir=log_dir)
        finish_time = datetime.now()
        if not dry_run:
            state.store_run(
                result.log_id, env['PACKAGE'], vcs_url,
                env['MAINTAINER_EMAIL'],
                start_time, finish_time, command,
                result.description,
                result.context,
                result.main_branch_revision,
                result.code,
                result.proposal.url if result.proposal else None,
                build_version=result.build_version,
                build_distribution=result.build_distribution,
                branch_name=result.branch_name,
                revision=result.revision)

            state.drop_queue_item(queue_id)
        last_success_gauge.set_to_current_time()

    started = set()
    todo = set()
    for item in state.iter_queue(limit=concurrency):
        todo.add(process_queue_item(item))
        started.add(item[0])

    def handle_sigterm():
        global concurrency
        concurrency = None
        note('Received SIGTERM; not starting new jobs.')

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_sigterm)
    try:
        while True:
            done, pending = await asyncio.wait(
                todo, return_when='FIRST_COMPLETED')
            for task in done:
                task.result()
            todo = pending
            if concurrency:
                for i in enumerate(done):
                    for item in state.iter_queue(limit=concurrency):
                        if item[0] in started:
                            continue
                        todo.add(process_queue_item(item))
                        started.add(item[0])
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def run_web_server(listen_addr, port):
    async def metrics(request):
        resp = web.Response(body=generate_latest())
        resp.content_type = CONTENT_TYPE_LATEST
        return resp

    app = web.Application()
    app.router.add_get("/metrics", metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, listen_addr, port)
    await site.start()


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='janitor.runner')
    parser.add_argument(
        '--listen-address', type=str,
        help='Listen address', default='localhost')
    parser.add_argument(
        '--port', type=int,
        help='Listen port', default=9911)
    parser.add_argument(
        '--max-mps-per-maintainer',
        default=0,
        type=int,
        help='Maximum number of open merge proposals per maintainer.')
    parser.add_argument(
        '--pre-check',
        help='Command to run to check whether to process package.',
        type=str)
    parser.add_argument(
        '--post-check',
        help='Command to run to check package before pushing.',
        type=str)
    parser.add_argument(
        '--build-command',
        help='Build package to verify it.', type=str,
        default=None)
    parser.add_argument(
        "--dry-run",
        help="Create branches but don't push or propose anything.",
        action="store_true", default=False)
    parser.add_argument(
        '--log-dir', help='Directory to store logs in.',
        type=str, default='site/pkg')
    parser.add_argument(
        '--incoming', type=str,
        help='Path to copy built Debian packages into.')
    parser.add_argument(
        '--debsign-keyid', type=str,
        help='GPG key to sign Debian package with.')
    parser.add_argument(
        '--vcs-result-dir', type=str,
        help='Directory to store VCS repositories in.')
    parser.add_argument(
        '--worker', type=str,
        default='janitor.worker',
        choices=['local', 'gcb'],
        help='Worker to use.')
    parser.add_argument(
        '--concurrency', type=int, default=1,
        help='Number of workers to run in parallel.')

    args = parser.parse_args()

    publisher = Publisher(args.max_mps_per_maintainer)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(
        loop.create_task(process_queue(
            args.worker,
            args.build_command, publisher,
            args.pre_check, args.post_check,
            args.dry_run, args.incoming, args.log_dir,
            args.debsign_keyid,
            args.vcs_result_dir,
            args.concurrency)),
        loop.create_task(export_queue_length()),
        loop.create_task(run_web_server(args.listen_address, args.port)),
        ))


if __name__ == '__main__':
    sys.exit(main(sys.argv))
