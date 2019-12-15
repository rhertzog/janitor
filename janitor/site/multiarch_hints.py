#!/usr/bin/python3

from aiohttp import ClientConnectorError
import urllib.parse

from janitor import state
from janitor.site import (
    env,
    get_vcs_type,
    )


SUITE = 'multiarch-fixes'


async def render_start():
    template = env.get_template('multiarch-fixes-start.html')
    return await template.render_async()
