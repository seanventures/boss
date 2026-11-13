"""BOSS's own object store client (BOSSAPP-826): S3-compatible storage for platform
artefacts — today, workspace snapshots across ephemeral sandboxes.

It reads the S3_* settings in config.py (S3_ENDPOINT, S3_ACCESS_KEY_ID,
S3_SECRET_ACCESS_KEY, S3_BUCKET, S3_REGION), which were dormant until now. It is
the CONTROL PLANE's store: it never touches the buckets the MinIO infra plugin
(infra_plugins/minio_s3.py) provisions for apps. It follows that plugin's
conventions so one MinIO serves either role: path-style addressing (MinIO and
most self-hosted S3s do not route virtual-host buckets), an http(s) endpoint
URL, and the same bucket-name rule.

boto3 is already an engine dependency (engine/requirements.txt), so the S3 client
is boto3 — imported lazily so an install that never configures a store never
loads it. `DirectoryObjectStore` is the same interface over a local directory:
the tests' fake store, and a `file://` endpoint for a single-box install.

Keys are relative, '/'-separated and never escape the bucket/directory root.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Protocol, runtime_checkable

#: The bucket-name rule infra_plugins/minio_s3.py enforces (S3's own rule, minus dots).
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
#: One key component: what a run id, a repo slug or a digest can contain.
_KEY_PART_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ObjectStoreError(RuntimeError):
    """The store could not do what was asked — phrased for the operator."""


class ObjectNotFound(ObjectStoreError, LookupError):
    """The key does not exist (already reaped, or never written)."""


@runtime_checkable
class ObjectStore(Protocol):
    """The interface every store speaks. Files, not bytes: a dependency
    snapshot is hundreds of MB, so nothing here holds one in memory."""

    def put_file(self, key: str, path: Path) -> None: ...

    def get_file(self, key: str, path: Path) -> None: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...


def key_part(value: Any) -> str:
    """Make one key component safe (no '/', no '..', nothing exotic)."""
    cleaned = _KEY_PART_UNSAFE.sub("-", str(value or "")).strip(".-") or "_"
    return cleaned[:120]


def check_key(key: str) -> str:
    """A key is relative and stays inside the root — refused otherwise."""
    if not key or key.startswith("/") or "\\" in key:
        raise ObjectStoreError(f"object key {key!r} must be a relative '/'-separated path")
    parts = PurePosixPath(key).parts
    if any(part in ("", ".", "..") for part in parts):
        raise ObjectStoreError(f"object key {key!r} may not contain '.' or '..' parts")
    return key


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DirectoryObjectStore:
    """The ObjectStore interface over a local directory. Writes land in a temp
    file first and are renamed into place, so a reader never sees half an
    object."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*PurePosixPath(check_key(key)).parts)

    def put_file(self, key: str, path: Path) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, staging = tempfile.mkstemp(dir=target.parent, prefix=".put-")
        os.close(fd)
        try:
            shutil.copyfile(path, staging)
            os.replace(staging, target)
        finally:
            if os.path.exists(staging):
                os.unlink(staging)

    def get_file(self, key: str, path: Path) -> None:
        source = self._path(key)
        if not source.is_file():
            raise ObjectNotFound(f"no object at {key!r}")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3ObjectStore:
    """The ObjectStore interface over an S3-compatible bucket (boto3)."""

    def __init__(self, *, bucket: str, endpoint: Optional[str] = None,
                 access_key_id: Optional[str] = None,
                 secret_access_key: Optional[str] = None,
                 region: Optional[str] = None, client: Any = None):
        if not BUCKET_RE.fullmatch(bucket or ""):
            raise ObjectStoreError(
                f"S3_BUCKET {bucket!r} is not a valid bucket name "
                "(3-63 lower-case letters, digits, dots or hyphens)")
        self.bucket = bucket
        self._client = client
        self._endpoint = endpoint or None
        self._access_key_id = access_key_id or None
        self._secret_access_key = secret_access_key or None
        self._region = region or "us-east-1"

    def _s3(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name=self._region,
                # Path-style, as the MinIO plugin's endpoints need; retries bounded.
                config=Config(s3={"addressing_style": "path"},
                              retries={"max_attempts": 3, "mode": "standard"}),
            )
        return self._client

    @staticmethod
    def _missing(exc: Exception) -> bool:
        response = getattr(exc, "response", None) or {}
        code = str((response.get("Error") or {}).get("Code") or "")
        return code in ("404", "NoSuchKey", "NotFound")

    def put_file(self, key: str, path: Path) -> None:
        try:
            self._s3().upload_file(str(path), self.bucket, check_key(key))
        except ObjectStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 — boto/network errors, phrased once
            raise ObjectStoreError(f"could not store {key!r}: {exc}"[:400]) from exc

    def get_file(self, key: str, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._s3().download_file(self.bucket, check_key(key), str(path))
        except ObjectStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._missing(exc):
                raise ObjectNotFound(f"no object at {key!r}") from exc
            raise ObjectStoreError(f"could not fetch {key!r}: {exc}"[:400]) from exc

    def delete(self, key: str) -> None:
        try:
            self._s3().delete_object(Bucket=self.bucket, Key=check_key(key))
        except ObjectStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._missing(exc):
                return
            raise ObjectStoreError(f"could not delete {key!r}: {exc}"[:400]) from exc

    def exists(self, key: str) -> bool:
        try:
            self._s3().head_object(Bucket=self.bucket, Key=check_key(key))
            return True
        except ObjectStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._missing(exc):
                return False
            raise ObjectStoreError(f"could not check {key!r}: {exc}"[:400]) from exc


def object_store_from_settings(settings: Any) -> Optional[ObjectStore]:
    """The configured store, or None when S3_BUCKET is unset (no store).

    S3_ENDPOINT may be `file:///some/dir` for a single-box install: the bucket
    is then a sub-directory of that path."""
    bucket = (getattr(settings, "s3_bucket", None) or "").strip()
    if not bucket:
        return None
    endpoint = (getattr(settings, "s3_endpoint", None) or "").strip()
    if endpoint.startswith("file://"):
        return DirectoryObjectStore(Path(endpoint[len("file://"):]) / key_part(bucket))
    return S3ObjectStore(
        bucket=bucket,
        endpoint=endpoint or None,
        access_key_id=getattr(settings, "s3_access_key_id", None),
        secret_access_key=getattr(settings, "s3_secret_access_key", None),
        region=getattr(settings, "s3_region", None),
    )
