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
import os
import sys

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    push_to_gateway,
    REGISTRY,
)

sys.path.insert(0, os.path.dirname(__file__))

from janitor import state  # noqa: E402


parser = argparse.ArgumentParser(prog='refresh-proposal-status')
parser.add_argument(
    '--prometheus', type=str,
    help='Prometheus push gateway to export to.')
args = parser.parse_args()

run_count = Counter(
    'run_count', 'Number of total runs.',
    labelnames=('command', ))
run_result_count = Counter(
    'run_result_count', 'Number of runs by code.',
    labelnames=('command', 'result_code'))
run_with_build_count = Counter(
    'run_with_build_count', 'Number of total runs with package built.',
    labelnames=('command', ))
run_with_proposal_count = Counter(
    'run_with_proposal_count', 'Number of total runs with merge proposal.',
    labelnames=('command', ))
duration = Histogram(
    'duration', 'Build duration',
    labelnames=('package', 'command', 'result_code'))
last_success_gauge = Gauge(
    'job_last_success_unixtime',
    'Last time a batch job successfully finished')


for package_name, command, result_code, log_id, description, run_duration in (
        state.iter_last_runs()):
    run_count.labels(command=command).inc()
    run_result_count.labels(command=command, result_code=result_code).inc()
    duration.labels(
        command=command, package=package_name,
        result_code=result_code).observe(run_duration.total_seconds())


last_success_gauge.set_to_current_time()
if args.prometheus:
    push_to_gateway(
        args.prometheus, job='janitor.export-stats',
        registry=REGISTRY)
