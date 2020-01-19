#!/usr/bin/python3

from aiohttp import ClientConnectorError
import urllib.parse

from janitor import state
from janitor.site import (
    env,
    get_archive_diff,
    DebdiffRetrievalError,
    )


async def generate_rejected(conn, suite=None):
    entries = [
        entry async for entry in
        state.iter_publish_ready(
            conn, review_status=['rejected'], suite=suite)]

    def entry_key(entry):
        return entry[0].times[1]
    entries.sort(key=entry_key, reverse=True)
    template = env.get_template('rejected.html')
    kwargs = {'entries': entries, 'suite': suite}
    return await template.render_async(**kwargs)


async def generate_review(conn, client, archiver_url, publisher_url,
                          suite=None):
    entries = [entry async for entry in
               state.iter_publish_ready(
                       conn, review_status=['unreviewed'], limit=40,
                       suite=suite)]
    if not entries:
        template = env.get_template('review-done.html')
        return await template.render_async()

    (run, maintainer_email, uploader_emails, branch_url,
     publish_mode, changelog_mode,
     command) = entries.pop(0)

    async def show_diff():
        if not run.revision or run.revision == run.main_branch_revision:
            return ''
        url = urllib.parse.urljoin(publisher_url, 'diff/%s' % run.id)
        try:
            async with client.get(url) as resp:
                if resp.status == 200:
                    return (await resp.read()).decode('utf-8', 'replace')
                else:
                    return (
                        'Unable to retrieve diff; error %d' % resp.status)
        except ClientConnectorError as e:
            return 'Unable to retrieve diff; error %s' % e

    async def show_debdiff():
        unchanged_run = await state.get_unchanged_run(
            conn, run.main_branch_revision)
        if unchanged_run is None:
            return '<p>No control run</p>'
        try:
            text, content_type = await get_archive_diff(
                client, archiver_url, run, unchanged_run,
                kind='debdiff', filter_boring=True, accept='text/html')
            return text.decode('utf-8', 'replace')
        except DebdiffRetrievalError as e:
            return 'Unable to retrieve debdiff: %r' % e
        except FileNotFoundError:
            return '<p>No debdiff generated</p>'

    kwargs = {
        'show_diff': show_diff,
        'show_debdiff': show_debdiff,
        'package_name': run.package,
        'run_id': run.id,
        'suite': run.suite,
        'todo': [(entry[0].package, entry[0].id) for entry in entries],
        }
    template = env.get_template('review.html')
    return await template.render_async(**kwargs)
