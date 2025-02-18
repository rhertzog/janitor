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

from aiohttp import (
    ClientSession,
    ClientResponseError,
    ClientTimeout,
    ServerDisconnectedError,
)
from datetime import datetime
import gzip
from io import BytesIO
import os
from yarl import URL


class ServiceUnavailable(Exception):
    """The remote server is temporarily unavailable."""


class LogFileManager(object):
    async def has_log(self, pkg: str, run_id: str, name: str, timeout=None):
        raise NotImplementedError(self.has_log)

    async def get_log(self, pkg: str, run_id: str, name: str, timeout=None):
        raise NotImplementedError(self.get_log)

    async def import_log(self, pkg: str, run_id: str, orig_path: str, timeout=None, mtime=None):
        raise NotImplementedError(self.import_log)

    async def iter_logs(self):
        raise NotImplementedError(self.iter_logs)

    async def get_ctime(self, pkg: str, run_id: str, name: str) -> datetime:
        raise NotImplementedError(self.get_ctime)


class FileSystemLogFileManager(LogFileManager):
    def __init__(self, log_directory):
        self.log_directory = log_directory

    def _get_paths(self, pkg, run_id, name):
        if "/" in pkg or "/" in run_id or "/" in name:
            return []
        return [
            os.path.join(self.log_directory, pkg, run_id, name),
            os.path.join(self.log_directory, pkg, run_id, name) + ".gz",
        ]

    async def iter_logs(self):
        for pkg in os.scandir(self.log_directory):
            for entry in os.scandir(pkg.path):
                yield pkg.name, entry.name, os.listdir(entry.path)

    async def has_log(self, pkg, run_id, name):
        return any(map(os.path.exists, self._get_paths(pkg, run_id, name)))

    async def get_ctime(self, pkg: str, run_id: str, name: str) -> datetime:
        for p in self._get_paths(pkg, run_id, name):
            try:
                return datetime.fromtimestamp(os.stat(p).st_ctime)
            except FileNotFoundError:
                continue
        raise FileNotFoundError(name)

    async def get_log(self, pkg, run_id, name, timeout=None):
        for path in self._get_paths(pkg, run_id, name):
            if not os.path.exists(path):
                continue
            if path.endswith(".gz"):
                return gzip.GzipFile(path, mode="rb")
            else:
                return open(path, "rb")
        raise FileNotFoundError(name)

    async def import_log(self, pkg, run_id, orig_path, timeout=None, mtime=None):
        dest_dir = os.path.join(self.log_directory, pkg, run_id)
        os.makedirs(dest_dir, exist_ok=True)
        with open(orig_path, "rb") as inf:
            dest_path = os.path.join(dest_dir, os.path.basename(orig_path) + ".gz")
            with gzip.GzipFile(dest_path, mode="wb", mtime=mtime) as outf:
                outf.write(inf.read())

    async def delete_log(self, pkg, run_id, name):
        for path in self._get_paths(pkg, run_id, name):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


class LogRetrievalError(Exception):
    """Unable to retrieve log file."""


class S3LogFileManager(LogFileManager):
    def __init__(self, endpoint_url, bucket_name="debian-janitor", trace_configs=None):
        import boto3

        self.base_url = endpoint_url + ("/%s/" % bucket_name)
        self.session = ClientSession(trace_configs=trace_configs)
        self.s3 = boto3.resource("s3", endpoint_url=endpoint_url)
        self.s3_bucket = self.s3.Bucket(bucket_name)

    def _get_key(self, pkg, run_id, name):
        return "logs/%s/%s/%s.gz" % (pkg, run_id, name)

    def _get_url(self, pkg, run_id, name):
        return "%s%s" % (self.base_url, self._get_key(pkg, run_id, name))

    async def has_log(self, pkg, run_id, name):
        url = self._get_url(pkg, run_id, name)
        async with self.session.head(url) as resp:
            if resp.status == 404:
                return False
            if resp.status == 200:
                return True
            if resp.status == 403:
                return False
            raise LogRetrievalError(
                "Unexpected response code %d: %s" % (resp.status, await resp.text())
            )

    async def get_log(self, pkg, run_id, name, timeout=10):
        url = self._get_url(pkg, run_id, name)
        client_timeout = ClientTimeout(timeout)
        async with self.session.get(url, timeout=client_timeout) as resp:
            if resp.status == 404:
                raise FileNotFoundError(name)
            if resp.status == 200:
                return BytesIO(gzip.decompress(await resp.read()))
            if resp.status == 403:
                raise PermissionError(await resp.text())
            raise LogRetrievalError(
                "Unexpected response code %d: %s" % (resp.status, await resp.text())
            )

    async def import_log(self, pkg, run_id, orig_path, timeout=360, mtime=None):
        with open(orig_path, "rb") as f:
            data = gzip.compress(f.read(), mtime=mtime)

        key = self._get_key(pkg, run_id, os.path.basename(orig_path))
        self.s3_bucket.put_object(Key=key, Body=data, ACL="public-read")

    async def delete_log(self, pkg, run_id, name):
        key = self._get_key(pkg, run_id, name)
        self.s3_bucket.delete_objects(Delete={"Objects": [{"Key": key}]})


class GCSLogFilemanager(LogFileManager):
    def __init__(self, location, creds_path=None, trace_configs=None):
        from gcloud.aio.storage import Storage

        self.bucket_name = URL(location).host
        self.session = ClientSession(trace_configs=trace_configs)
        self.storage = Storage(service_file=creds_path, session=self.session)
        self.bucket = self.storage.get_bucket(self.bucket_name)

    async def iter_logs(self):
        seen = {}
        for name in await self.bucket.list_blobs():
            pkg, log_id, lfn = name.split("/")
            seen.setdefault((pkg, log_id), []).append(lfn)
        for (pkg, log_id), lfns in seen.items():
            yield pkg, log_id, lfns

    def _get_object_name(self, pkg, run_id, name):
        return "%s/%s/%s.gz" % (pkg, run_id, name)

    async def has_log(self, pkg, run_id, name):
        object_name = self._get_object_name(pkg, run_id, name)
        return await self.bucket.blob_exists(object_name, self.session)

    async def get_ctime(self, pkg, run_id, name):
        from iso8601 import parse_date
        object_name = self._get_object_name(pkg, run_id, name)
        try:
            blob = await self.bucket.get_blob(object_name, self.session)
        except ClientResponseError as e:
            if e.status == 404:
                raise FileNotFoundError(name)
            raise ServiceUnavailable()
        except ServerDisconnectedError:
            raise ServiceUnavailable()
        return parse_date(blob.timeCreated)

    async def get_log(self, pkg, run_id, name, timeout=30):
        object_name = self._get_object_name(pkg, run_id, name)
        try:
            data = await self.storage.download(
                self.bucket_name, object_name, session=self.session, timeout=timeout
            )
            return BytesIO(gzip.decompress(data))
        except ClientResponseError as e:
            if e.status == 404:
                raise FileNotFoundError(name)
            raise ServiceUnavailable()
        except ServerDisconnectedError:
            raise ServiceUnavailable()

    async def import_log(self, pkg, run_id, orig_path, timeout=360, mtime=None):
        object_name = self._get_object_name(pkg, run_id, os.path.basename(orig_path))
        with open(orig_path, "rb") as f:
            uploaded_data = gzip.compress(f.read(), mtime=mtime)
        try:
            await self.storage.upload(
                self.bucket_name, object_name, uploaded_data, timeout=timeout
            )
        except ClientResponseError as e:
            if e.status == 503:
                raise ServiceUnavailable()
            if e.status == 403:
                raise PermissionError(e.message)
            raise


def get_log_manager(location, trace_configs=None):
    if location.startswith("gs://"):
        return GCSLogFilemanager(location, trace_configs=trace_configs)
    if location.startswith("http:") or location.startswith("https:"):
        return S3LogFileManager(location, trace_configs=trace_configs)
    return FileSystemLogFileManager(location)
