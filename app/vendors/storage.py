"""Cloudflare R2 storage (guide 6.5), with a local-directory backend for DRY_RUN.

Vendors read inputs through presigned GET URLs (6 hours). Keys are stored in the database;
URLs never are.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from app.config import get_settings

DEFAULT_PRESIGN_SECONDS = 21600  # 6 hours (R2 allows up to 7 days)

_client = None
_client_key: tuple | None = None


def _check_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError(f"invalid storage key: {key!r}")
    return key


def _backend() -> str:
    s = get_settings()
    if s.r2_configured:
        return "r2"
    if s.dry_run:
        return "local"
    raise RuntimeError("R2_ACCOUNT_ID (or R2_ENDPOINT), R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET must be set")


def local_root() -> Path:
    return Path(get_settings().media_tmp_dir) / "r2"


def _local_path(key: str) -> Path:
    return local_root() / _check_key(key)


def _s3():
    global _client, _client_key
    import boto3
    from botocore.config import Config

    s = get_settings()
    ident = (s.r2_endpoint, s.r2_access_key_id, s.r2_secret_access_key)
    if _client is None or _client_key != ident:
        _client = boto3.client(
            "s3",
            endpoint_url=s.r2_endpoint,
            aws_access_key_id=s.r2_access_key_id,
            aws_secret_access_key=s.r2_secret_access_key,
            region_name="auto",
            # Newer boto3 adds checksum headers that R2 can reject (community report; see VENDOR_NOTES.md).
            config=Config(request_checksum_calculation="when_required", response_checksum_validation="when_required"),
        )
        _client_key = ident
    return _client


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    _check_key(key)
    if _backend() == "local":
        path = _local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return
    _s3().put_object(Bucket=get_settings().r2_bucket, Key=key, Body=data, ContentType=content_type)


def put_file(key: str, path: str | Path) -> None:
    _check_key(key)
    if _backend() == "local":
        dest = _local_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        return
    _s3().upload_file(str(path), get_settings().r2_bucket, key)


def get_to_file(key: str, path: str | Path) -> None:
    _check_key(key)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if _backend() == "local":
        src = _local_path(key)
        if not src.exists():
            raise FileNotFoundError(key)
        shutil.copyfile(src, path)
        return
    _s3().download_file(get_settings().r2_bucket, key, str(path))


def presign_get(key: str, seconds: int = DEFAULT_PRESIGN_SECONDS) -> str:
    _check_key(key)
    if _backend() == "local":
        return _local_path(key).resolve().as_uri()
    return _s3().generate_presigned_url(
        "get_object", Params={"Bucket": get_settings().r2_bucket, "Key": key}, ExpiresIn=seconds
    )


def is_local() -> bool:
    """True when objects live in the local directory (DRY_RUN without R2 settings)."""
    return _backend() == "local"


def local_file(key: str) -> Path:
    return _local_path(key)


def list_objects(prefix: str):
    """Yields (key, last_modified (aware datetime), size) for every object under `prefix`."""
    from datetime import datetime, timezone

    if _backend() == "local":
        root = local_root()
        base = root / prefix if prefix else root
        if not base.exists():
            return
        for path in ([base] if base.is_file() else base.rglob("*")):   # a prefix may name one file, as in S3
            if path.is_file():
                st = path.stat()
                yield str(path.relative_to(root)), datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), st.st_size
        return
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=get_settings().r2_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"], obj["LastModified"], obj["Size"]


def delete(key: str) -> None:
    _check_key(key)
    if _backend() == "local":
        _local_path(key).unlink(missing_ok=True)
        return
    _s3().delete_object(Bucket=get_settings().r2_bucket, Key=key)
