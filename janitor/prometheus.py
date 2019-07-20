#!/usr/bin/python3
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

from aiohttp import web
import asyncio

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
    )

request_counter = Counter(
    'requests_total', 'Total Request Count', ['method', 'path', 'status'])

request_latency_hist = Histogram(
    'request_latency_seconds', 'Request latency', ['path'])

requests_in_progress_gauge = Gauge(
    'requests_in_progress_total', 'Requests currently in progress',
    ['method', 'path'])


async def metrics(request):
    resp = web.Response(body=generate_latest())
    resp.content_type = CONTENT_TYPE_LATEST
    return resp


@asyncio.coroutine
def metrics_middleware(app, handler):
    @asyncio.coroutine
    def wrapper(request):
        start_time = time.time()
        requests_in_progress.labels(request.method, request.path).inc()
        response = yield from handler(request)
        resp_time = time.time() - start_time
        request_latency_hist.labels(request.path).observe(resp_time)
        requests_in_progress_gauge.labels(request.method, request.path).dec()
        request_counter.labels(request.method, request.path, response.status).inc()
        return response
    return wrapper


def setup_metrics(app):
    app.middlewares.insert(0, metrics_middleware)
    app.router.add_get("/metrics", metrics)
