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

import argparse
from contextlib import contextmanager
from datetime import datetime
import errno
import json
import logging
import os
import subprocess
import sys
import traceback
from typing import Dict, List, Optional, Any, Type, Iterator, Tuple

from breezy.config import GlobalStack
from breezy.transport import Transport

from silver_platter.workspace import Workspace

from silver_platter.debian import (
    MissingUpstreamTarball,
    pick_additional_colocated_branches,
    control_files_in_root,
    control_file_present,
)
from silver_platter.changer import (
    ChangerError,
    ChangerResult,
    ChangerReporter,
    GenericChanger,
    changer_subcommand as _generic_changer_subcommand,
    )
from silver_platter.debian.changer import (
    DebianChanger,
    changer_subcommand as _debian_changer_subcommand,
)
from silver_platter.debian.debianize import (
    DebianizeChanger as ActualDebianizeChanger,
)

from silver_platter.debian.upstream import (
    NewUpstreamChanger as ActualNewUpstreamChanger,
)
from silver_platter.proposal import Hoster

from silver_platter.utils import (
    full_branch_url,
    run_pre_check,
    run_post_check,
    PreCheckFailed,
    PostCheckFailed,
    open_branch,
    BranchMissing,
    BranchUnavailable,
)

from ognibuild.debian.fix_build import build_incrementally
from ognibuild.debian.build import (
    build_once,
    MissingChangesFile,
    SbuildFailure,
)
from .debian import tree_set_changelog_version
from ognibuild import (
    DetailedFailure,
)
from ognibuild.buildsystem import (
    NoBuildToolsFound,
)
from ognibuild import (
    UnidentifiedError,
)
from ognibuild.dist import (
    create_dist_schroot,
    DistNoTarball,
)

from .vcs import (
    BranchOpenFailure,
    open_branch_ext,
)


# Whether to trust packages enough to run code from them,
# e.g. when guessing repo location.
TRUST_PACKAGE = False


DEFAULT_BUILD_COMMAND = "sbuild -A -s -v"


logger = logging.getLogger(__name__)


@contextmanager
def redirect_output(to_file):
    sys.stdout.flush()
    sys.stderr.flush()
    old_stdout = os.dup(sys.stdout.fileno())
    old_stderr = os.dup(sys.stderr.fileno())
    os.dup2(to_file.fileno(), sys.stdout.fileno())  # type: ignore
    os.dup2(to_file.fileno(), sys.stderr.fileno())  # type: ignore
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(old_stdout, sys.stdout.fileno())
        os.dup2(old_stderr, sys.stderr.fileno())


class NewUpstreamChanger(ActualNewUpstreamChanger):

    def create_dist(self, tree, package, version, target_dir):
        from silver_platter.debian.upstream import DistCommandFailed

        os.environ['SETUPTOOLS_SCM_PRETEND_VERSION'] = version

        with open(os.path.join(self.log_directory, 'dist.log'), 'wb') as distf, redirect_output(distf):
            try:
                return create_dist_schroot(
                    tree,
                    subdir=package,
                    target_dir=target_dir,
                    packaging_tree=self.packaging_tree,
                    packaging_subpath=self.packaging_debian_path,
                    chroot=self.schroot,
                )
            except NotImplementedError:
                return None
            except NoBuildToolsFound:
                logger.info("No build tools found, falling back to simple export.")
                return None
            except DetailedFailure as e:
                error_code = "dist-" + e.error.kind
                error_description = str(e.error)
                raise ChangerError(
                    summary=error_description, category=error_code, original=e
                )
            except DistNoTarball as e:
                raise ChangerError('dist-no-tarball', str(e))
            except UnidentifiedError as e:
                traceback.print_exc()
                lines = [line for line in e.lines if line]
                if e.secondary:
                    raise DistCommandFailed(e.secondary.line)
                elif len(lines) == 1:
                    raise DistCommandFailed(lines[0])
                else:
                    raise DistCommandFailed(
                        "command %r failed with unidentified error "
                        "(return code %d)" % (e.argv, e.retcode)
                    )
            except Exception as e:
                traceback.print_exc()
                raise DistCommandFailed(str(e))

    def make_changes(self, local_tree, subpath, *args, **kwargs):
        self.packaging_tree = local_tree
        self.packaging_debian_path = os.path.join(subpath, 'debian')
        return super(NewUpstreamChanger, self).make_changes(local_tree, subpath, *args, **kwargs)


class DebianizeChanger(ActualDebianizeChanger):

    def create_dist(self, tree, package, version, target_dir):
        from silver_platter.debian.upstream import DistCommandFailed

        os.environ['SETUPTOOLS_SCM_PRETEND_VERSION'] = version

        with open(os.path.join(self.log_directory, 'dist.log'), 'wb') as distf, redirect_output(distf):
            try:
                return create_dist_schroot(
                    tree,
                    subdir=package,
                    target_dir=target_dir,
                    chroot=self.schroot,
                )
            except NotImplementedError:
                return None
            except NoBuildToolsFound:
                logger.info("No build tools found, falling back to simple export.")
                return None
            except DetailedFailure as e:
                error_code = "dist-" + e.error.kind
                error_description = str(e.error)
                raise ChangerError(
                    summary=error_description, category=error_code, original=e
                )
            except DistNoTarball as e:
                raise ChangerError('dist-no-tarball', str(e))
            except UnidentifiedError as e:
                traceback.print_exc()
                lines = [line for line in e.lines if line]
                if e.secondary:
                    raise DistCommandFailed(e.secondary.line)
                elif len(lines) == 1:
                    raise DistCommandFailed(lines[0])
                else:
                    raise DistCommandFailed(
                        "command %r failed with unidentified error "
                        "(return code %d)" % (e.argv, e.retcode)
                    )
            except Exception as e:
                traceback.print_exc()
                raise DistCommandFailed(str(e))


class DummyDebianChanger(DebianChanger):

    name = "just-build"

    @classmethod
    def setup_parser(cls, parser):
        parser.add_argument("--revision", type=str, help="Specific revision to build.")

    @classmethod
    def from_args(cls, args):
        return cls(revision=args.revision)

    def __init__(self, revision=None):
        self.revision = revision

    def suggest_branch_name(self):
        return "unchanged"

    def make_changes(
        self,
        local_tree,
        subpath,
        update_changelog,
        reporter,
        committer,
        base_proposal=None,
    ):
        if self.revision:
            local_tree.update(revision=self.revision.encode("utf-8"))
        if control_files_in_root(local_tree, subpath):
            raise ChangerError(
                "control-files-in-root",
                "control files live in root rather than debian/ " "(LarstIQ mode)",
            )

        return ChangerResult(
            description="Nothing changed", mutator=None, branches=[], tags=[]
        )

    @classmethod
    def describe_command(cls, command):
        return "Build without changes"


class DummyGenericChanger(DebianChanger):

    name = "just-build"

    @classmethod
    def setup_parser(cls, parser):
        parser.add_argument("--revision", type=str, help="Specific revision to build.")

    @classmethod
    def from_args(cls, args):
        return cls(revision=args.revision)

    def __init__(self, revision=None):
        self.revision = revision

    def suggest_branch_name(self):
        return "unchanged"

    def make_changes(
        self,
        local_tree,
        subpath,
        reporter,
        committer,
        base_proposal=None,
    ):
        if self.revision:
            local_tree.update(revision=self.revision.encode("utf-8"))
        return ChangerResult(
            description="Nothing changed", mutator=None, branches=[], tags=[]
        )

    @classmethod
    def describe_command(cls, command):
        return "Build without changes"


class WorkerResult(object):
    def __init__(
        self,
        description: Optional[str],
        value: Optional[int],
        branches: Optional[List[Tuple[str, str, bytes, bytes]]],
        tags: Optional[Dict[str, bytes]],
        target: str,
        target_details: Optional[Any],
    ) -> None:
        self.description = description
        self.value = value
        self.branches = branches
        self.tags = tags
        self.target = target
        self.target_details = target_details

    def json(self):
        return {
            "value": self.value,
            "description": self.description,
            "branches": [
                (f, n, br.decode("utf-8"), r.decode("utf-8"))
                for (f, n, br, r) in self.branches
            ],
            "tags": [(f, n, r.decode("utf-8")) for (f, n, r) in self.tags],
            "target": {
                "name": self.target,
                "details": self.target_details,
            },
        }


class WorkerFailure(Exception):
    """Worker processing failed."""

    def __init__(self, code: str, description: str, details: Optional[Any] = None) -> None:
        self.code = code
        self.description = description
        self.details = details


CUSTOM_DEBIAN_SUBCOMMANDS = {
    "just-build": DummyDebianChanger,
    "new-upstream": NewUpstreamChanger,
    "debianize": DebianizeChanger,
}


# TODO(jelmer): Just invoke the silver-platter subcommand
def debian_changer_subcommand(n):
    try:
        return CUSTOM_DEBIAN_SUBCOMMANDS[n]
    except KeyError:
        return _debian_changer_subcommand(n)


CUSTOM_GENERIC_SUBCOMMANDS = {
    "just-build": DummyGenericChanger,
}

def generic_changer_subcommand(n):
    try:
        return CUSTOM_GENERIC_SUBCOMMANDS[n]
    except KeyError:
        return _generic_changer_subcommand(n)


class WorkerReporter(ChangerReporter):
    def __init__(self, metadata_subworker, resume_result, provide_context, remotes):
        self.metadata_subworker = metadata_subworker
        self.resume_result = resume_result
        self.report_context = provide_context
        self.remotes = remotes

    def report_remote(self, name, url):
        self.remotes[name] = {"url": url}

    def report_metadata(self, key, value):
        self.metadata_subworker[key] = value

    def get_base_metadata(self, key, default_value=None):
        if not self.resume_result:
            return default_value
        return self.resume_result.get(key, default_value)


class Target(object):
    """A build target."""

    name: str

    def build(self, ws, subpath, output_directory, env):
        raise NotImplementedError(self.build)

    def additional_colocated_branches(self, main_branch):
        return []

    def directory_name(self) -> str:
        raise NotImplementedError(self.directory_name)


class DebianTarget(Target):
    """Debian target."""

    name = "debian"

    SUPPRESS_LINTIAN_TAGS = [
        'bad-distribution-in-changes-file',
        'no-nmu-in-changelog',
        'source-nmu-has-incorrect-version-number'
        'changelog-distribution-does-not-match-changes-file',
        'distribution-and-changes-mismatch',
        ]

    def __init__(self, env, build_command):
        self.build_distribution = env.get("BUILD_DISTRIBUTION")
        self.build_command = build_command
        self.build_suffix = env.get("BUILD_SUFFIX")
        self.last_build_version = env.get("LAST_BUILD_VERSION")
        self.package = env["PACKAGE"]
        self.chroot = env.get("CHROOT")
        self.lintian_profile = env.get("LINTIAN_PROFILE")
        self.committer = env.get("COMMITTER")

    def parse_args(self, argv):
        logging.info('Running %r', argv)
        changer_cls: Type[DebianChanger]
        try:
            changer_cls = debian_changer_subcommand(argv[0])
        except KeyError:
            raise WorkerFailure(
                "unknown-subcommand", "unknown subcommand %s" % argv[0],
                details={"subcommand": argv[0]})

        subparser = argparse.ArgumentParser(prog=changer_cls.name)
        subparser.add_argument(
            '--dry-run',
            action='store_true',
            help='Dry run.')
        subparser.add_argument(
            "--no-update-changelog",
            action="store_false",
            default=None,
            dest="update_changelog",
            help="do not update the changelog",
        )
        subparser.add_argument(
            "--update-changelog",
            action="store_true",
            dest="update_changelog",
            help="force updating of the changelog",
            default=None,
        )
        changer_cls.setup_parser(subparser)
        self.changer_args = subparser.parse_args(argv[1:])
        self.changer = changer_cls.from_args(self.changer_args)

    def make_changes(self, local_tree, subpath, reporter, log_directory, committer=None):
        self.changer.log_directory = log_directory
        try:
            return self.changer.make_changes(
                local_tree,
                subpath=subpath,
                committer=committer,
                update_changelog=self.changer_args.update_changelog,
                reporter=reporter,
            )
        except ChangerError as e:
            raise WorkerFailure(e.category, e.summary, details=e.details)

    def additional_colocated_branches(self, main_branch):
        return pick_additional_colocated_branches(main_branch)

    def build(self, ws, subpath, output_directory, env):
        from ognibuild.debian.apt import AptManager
        from ognibuild.session.plain import PlainSession
        from ognibuild.session.schroot import SchrootSession

        if self.chroot:
            session = SchrootSession(self.chroot)
        else:
            session = PlainSession()
        with session:
            apt = AptManager(session)
            if self.build_command:
                if self.last_build_version:
                    # Update the changelog entry with the previous build version;
                    # This allows us to upload incremented versions for subsequent
                    # runs.
                    tree_set_changelog_version(
                        ws.local_tree, self.last_build_version, subpath
                    )

                source_date_epoch = ws.local_tree.branch.repository.get_revision(
                    ws.main_branch.last_revision()
                ).timestamp
                try:
                    if not self.build_suffix:
                        (changes_name, cl_version) = build_once(
                            ws.local_tree,
                            self.build_distribution,
                            output_directory,
                            self.build_command,
                            subpath=subpath,
                            source_date_epoch=source_date_epoch,
                        )
                    else:
                        (changes_name, cl_version) = build_incrementally(
                            ws.local_tree,
                            apt,
                            "~" + self.build_suffix,
                            self.build_distribution,
                            output_directory,
                            build_command=self.build_command,
                            build_changelog_entry="Build for debian-janitor apt repository.",
                            committer=self.committer,
                            subpath=subpath,
                            source_date_epoch=source_date_epoch,
                            update_changelog=self.changer_args.update_changelog,
                        )
                except MissingUpstreamTarball:
                    raise WorkerFailure(
                        "build-missing-upstream-source", "unable to find upstream source"
                    )
                except MissingChangesFile as e:
                    raise WorkerFailure(
                        "build-missing-changes",
                        "Expected changes path %s does not exist." % e.filename,
                        details={'filename': e.filename}
                    )
                except SbuildFailure as e:
                    if e.error is not None:
                        if e.stage and not e.error.is_global:
                            code = "%s-%s" % (e.stage, e.error.kind)
                        else:
                            code = e.error.kind
                    elif e.stage is not None:
                        code = "build-failed-stage-%s" % e.stage
                    else:
                        code = "build-failed"
                    try:
                        if e.error is not None:
                            details = e.error.json()
                        else:
                            details = None
                    except NotImplementedError:
                        details = None
                    raise WorkerFailure(code, e.description, details=details)
                logger.info("Built %s", changes_name)
        lintian_result = self._run_lintian(output_directory, changes_name)
        return {'lintian': lintian_result}

    def _run_lintian(self, output_directory, changes_name):
        logger.info('Running lintian')
        args = [
            '--exp-output=format=json',
            '--suppress-tags=%s' % ','.join(self.SUPPRESS_LINTIAN_TAGS)]
        if self.lintian_profile:
            args.append('--profile=%s' % self.lintian_profile)
        try:
            lintian_output = subprocess.check_output(
                ['lintian'] + args + [os.path.join(output_directory, changes_name)])
        except subprocess.CalledProcessError:
            logger.warning('lintian failed to run.')
            return None
        try:
            return json.loads(lintian_output)
        except json.decoder.JSONDecodeError:
            logging.warning('Error parsing lintian output: %r', lintian_output)
            return None

    def directory_name(self):
        return self.package


class GenericTarget(Target):
    """Generic build target."""

    name = "generic"

    def __init__(self, env):
        self.chroot = env.get("CHROOT")

    def parse_args(self, argv):
        changer_cls: Type[GenericChanger]
        try:
            changer_cls = generic_changer_subcommand(argv[0])
        except KeyError:
            raise WorkerFailure(
                "unknown-subcommand", "unknown subcommand %s" % argv[0],
                details={"subcommand": argv[0]})

        subparser = argparse.ArgumentParser(prog=changer_cls.name)
        subparser.add_argument(
            '--dry-run',
            action='store_true',
            help='Dry run.')
        changer_cls.setup_parser(subparser)
        self.changer_args = subparser.parse_args(argv[1:])
        self.changer = changer_cls.from_args(self.changer_args)

    def make_changes(self, local_tree, subpath, reporter, log_directory, committer=None):
        self.changer.log_directory = log_directory
        try:
            return self.changer.make_changes(
                local_tree,
                subpath=subpath,
                committer=committer,
                reporter=reporter,
            )
        except ChangerError as e:
            raise WorkerFailure(e.category, e.summary, details=e.details)

    def additional_colocated_branches(self, main_branch):
        return []

    def build(self, ws, subpath, output_directory, env):
        from ognibuild.build import run_build
        from ognibuild.test import run_test
        from ognibuild.buildlog import InstallFixer
        from ognibuild.session.plain import PlainSession
        from ognibuild.session.schroot import SchrootSession
        from ognibuild.buildsystem import NoBuildToolsFound, detect_buildsystems
        from ognibuild.resolver import auto_resolver

        if self.chroot:
            session = SchrootSession(self.chroot)
            logger.info('Using schroot %s', self.chroot)
        else:
            session = PlainSession()
        with session:
            resolver = auto_resolver(session)
            fixers = [InstallFixer(resolver)]
            external_dir, internal_dir = session.setup_from_vcs(ws.local_tree)
            bss = list(detect_buildsystems(os.path.join(external_dir, subpath)))
            session.chdir(os.path.join(internal_dir, subpath))
            try:
                run_build(session, buildsystems=bss, resolver=resolver, fixers=fixers)
                run_test(session, buildsystems=bss, resolver=resolver, fixers=fixers)
            except DetailedFailure as f:
                raise WorkerFailure(f.error.kind, str(f.error), details={'command': f.argv})
            except UnidentifiedError as e:
                raise WorkerFailure('build-failed', str(e))

        return {}

    def directory_name(self):
        return "package"


@contextmanager
def process_package(
    vcs_url: str,
    subpath: str,
    env: Dict[str, str],
    command: List[str],
    output_directory: str,
    target: str,
    metadata: Any,
    build_command: Optional[str] = None,
    pre_check_command: Optional[str] = None,
    post_check_command: Optional[str] = None,
    possible_transports: Optional[List[Transport]] = None,
    possible_hosters: Optional[List[Hoster]] = None,
    resume_branch_url: Optional[str] = None,
    cached_branch_url: Optional[str] = None,
    extra_resume_branches: Optional[List[Tuple[str, str]]] = None,
    resume_subworker_result: Any = None,
) -> Iterator[Tuple[Workspace, WorkerResult]]:
    committer = env.get("COMMITTER")

    metadata["command"] = command

    if target == "debian":
        build_target = DebianTarget(env, build_command=build_command)
    elif target == "generic":
        build_target = GenericTarget(env)
    else:
        raise WorkerFailure(
            'target-unsupported', 'The target %r is not supported' % target)

    build_target.parse_args(command)

    logger.info("Opening branch at %s", vcs_url)
    try:
        main_branch = open_branch_ext(vcs_url, possible_transports=possible_transports)
    except BranchOpenFailure as e:
        raise WorkerFailure(
            "worker-%s" % e.code, e.description, details={'url': vcs_url})

    if cached_branch_url:
        try:
            cached_branch = open_branch(
                cached_branch_url, possible_transports=possible_transports
            )
        except BranchMissing as e:
            logger.info("Cached branch URL %s missing: %s", cached_branch_url, e)
            cached_branch = None
        except BranchUnavailable as e:
            logger.warning(
                "Cached branch URL %s unavailable: %s", cached_branch_url, e
            )
            cached_branch = None
        else:
            logger.info("Using cached branch %s", full_branch_url(cached_branch))
    else:
        cached_branch = None

    if resume_branch_url:
        # TODO(jelmer): Use extra_resume_branches
        try:
            resume_branch = open_branch(
                resume_branch_url, possible_transports=possible_transports
            )
        except BranchUnavailable as e:
            logger.info('Resume branch URL: %s', e.url)
            traceback.print_exc()
            raise WorkerFailure(
                "worker-resume-branch-unavailable", str(e),
                details={'url': e.url})
        except BranchMissing as e:
            raise WorkerFailure(
                "worker-resume-branch-missing", str(e),
                details={'url': e.url})
        else:
            logger.info("Resuming from branch %s", full_branch_url(resume_branch))
    else:
        resume_branch = None

    with Workspace(
        main_branch,
        resume_branch=resume_branch,
        cached_branch=cached_branch,
        path=os.path.join(output_directory, build_target.directory_name()),
        additional_colocated_branches=(
            build_target.additional_colocated_branches(main_branch)
        ),
    ) as ws:
        logger.info('Workspace ready - starting.')

        if ws.local_tree.has_changes():
            if list(ws.local_tree.iter_references()):
                raise WorkerFailure(
                    "requires-nested-tree-support",
                    "Missing support for nested trees in Breezy.",
                )
            raise AssertionError

        metadata["revision"] = metadata[
            "main_branch_revision"
        ] = ws.main_branch.last_revision().decode()

        try:
            run_pre_check(ws.local_tree, pre_check_command)
        except PreCheckFailed as e:
            raise WorkerFailure(
                "pre-check-failed", str(e), details={'command': pre_check_command})

        metadata["subworker"] = {}
        metadata["remotes"] = {}

        def provide_context(c):
            metadata["context"] = c

        if ws.resume_branch is None:
            # If the resume branch was discarded for whatever reason, then we
            # don't need to pass in the subworker result.
            resume_subworker_result = None

        reporter = WorkerReporter(
            metadata["subworker"],
            resume_subworker_result,
            provide_context,
            metadata["remotes"],
        )

        reporter.report_remote("origin", main_branch.user_url)

        try:
            changer_result = build_target.make_changes(
                ws.local_tree, subpath, reporter, output_directory,
                committer=committer
            )
        except WorkerFailure as e:
            if e.code == "nothing-to-do" and resume_subworker_result is not None:
                e = WorkerFailure("nothing-new-to-do", e.description)
                raise e
            else:
                raise
        finally:
            metadata["revision"] = ws.local_tree.branch.last_revision().decode()

        if command[0] != "just-build":
            if not changer_result.branches:
                raise WorkerFailure("nothing-to-do", "Nothing to do.")

        try:
            run_post_check(ws.local_tree, post_check_command, ws.orig_revid)
        except PostCheckFailed as e:
            raise WorkerFailure("post-check-failed", str(e), details={
                'command': post_check_command})

        build_target_details = build_target.build(
            ws, subpath, output_directory, env)

        branches: Optional[List[Tuple[str, str, bytes, bytes]]]
        if changer_result.branches is not None:
            branches = [
                (f, n or main_branch.name, br, r)
                for (f, n, br, r) in changer_result.branches
            ]
        else:
            branches = None

        wr = WorkerResult(
            changer_result.description,
            changer_result.value,
            branches,
            changer_result.tags,
            build_target.name, build_target_details,
        )
        yield ws, wr


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="janitor-worker", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--output-directory", type=str, help="Output directory", default="."
    )
    parser.add_argument("--branch-url", type=str, help="URL of branch to build.")
    parser.add_argument(
        "--resume-branch-url",
        type=str,
        help="URL of resume branch to continue on (if any).",
    )
    parser.add_argument(
        "--resume-result-path",
        type=str,
        help=(
            "Path to a JSON file with the results for "
            "the last run on the resumed branch."
        ),
    )
    parser.add_argument(
        "--cached-branch-url", type=str, help="URL of cached branch to start from."
    )
    parser.add_argument(
        "--pre-check",
        help="Command to run to check whether to process package.",
        type=str,
    )
    parser.add_argument(
        "--post-check",
        help="Command to run to check package before pushing.",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--subpath",
        type=str,
        help="Path in the branch under which the package lives.",
        default="",
    )
    parser.add_argument(
        "--build-command",
        help="Build package to verify it.",
        type=str,
        default=DEFAULT_BUILD_COMMAND,
    )
    parser.add_argument(
        "--tgz-repo",
        help="Whether to create a tgz of the VCS repo.",
        action="store_true",
    )
    parser.add_argument(
        "--extra-resume-branch",
        default=[],
        help="Colocated branches to resume as well",
        action="append",
        type=str,
    )
    parser.add_argument(
        "--target",
        type=str,
        help="Build target")
    parser.add_argument("--build-distribution", type=str, help="Build distribution.")

    parser.add_argument("command", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.branch_url is None:
        parser.print_usage()
        return 1

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    output_directory = os.path.abspath(args.output_directory)

    global_config = GlobalStack()
    global_config.set("branch.fetch_tags", True)

    if args.resume_result_path:
        with open(args.resume_result_path, "r") as f:
            resume_subworker_result = json.load(f)
    else:
        resume_subworker_result = None

    extra_resume_branches = [b.split(":", 1) for b in args.extra_resume_branch]

    metadata = {}
    start_time = datetime.now()
    metadata["start_time"] = start_time.isoformat()
    try:
        with process_package(
            args.branch_url,
            args.subpath,
            os.environ,
            args.command,
            output_directory,
            metadata=metadata,
            target=args.target,
            build_command=args.build_command,
            pre_check_command=args.pre_check,
            post_check_command=args.post_check,
            resume_branch_url=args.resume_branch_url,
            cached_branch_url=args.cached_branch_url,
            extra_resume_branches=extra_resume_branches,
            resume_subworker_result=resume_subworker_result,
        ) as (ws, result):
            if args.tgz_repo:
                subprocess.check_call(
                    [
                        "tar",
                        "czf",
                        os.environ["PACKAGE"] + ".tgz",
                        os.environ["PACKAGE"],
                    ],
                    cwd=output_directory,
                )
            else:
                ws.defer_destroy()
    except WorkerFailure as e:
        metadata["code"] = e.code
        metadata["description"] = e.description
        metadata['details'] = e.details
        logger.info("Worker failed (%s): %s", e.code, e.description)
        return 0
    except OSError as e:
        if e.errno == errno.ENOSPC:
            metadata["code"] = "no-space-on-device"
            metadata["description"] = str(e)
        else:
            metadata["code"] = "worker-exception"
            metadata["description"] = str(e)
            raise
    except BaseException as e:
        metadata["code"] = "worker-exception"
        metadata["description"] = str(e)
        raise
    else:
        metadata["code"] = None
        metadata.update(result.json())
        logger.info("%s", result.description)
        return 0
    finally:
        finish_time = datetime.now()
        logger.info("Elapsed time: %s", finish_time - start_time)
        with open(os.path.join(output_directory, "result.json"), "w") as f:
            try:
                json.dump(metadata, f, indent=2)
            except TypeError:
                raise TypeError(metadata)


if __name__ == "__main__":
    sys.exit(main())
