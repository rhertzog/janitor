#!/usr/bin/python3

import argparse
import asyncio
import os
import sys

from debian.changelog import Version
from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, os.path.dirname(__file__))

from janitor import state, udd  # noqa: E402

env = Environment(
    loader=FileSystemLoader('templates'),
    autoescape=select_autoescape(['html', 'xml'])
)


parser = argparse.ArgumentParser(prog='report-apt-repo')
parser.add_argument("suite")
args = parser.parse_args()


async def get_unstable_versions(present):
    unstable = {}
    if present:
        async for package in udd.UDD.public_udd_mirror().get_source_packages(
                packages=list(present), release='sid'):
            unstable[package.name] = Version(package.version)
    return unstable


async def gather_package_list():
    present = {}
    async for source, version in state.iter_published_packages(args.suite):
        present[source] = Version(version)

    unstable = await get_unstable_versions(present)

    for source in sorted(present):
        yield (
            source,
            present[source].upstream_version,
            unstable[source].upstream_version
            if source in unstable else '')


template = env.get_template(args.suite + '.html')
loop = asyncio.get_event_loop()
sys.stdout.write(
    template.render(packages=loop.run_until_complete(gather_package_list())))
