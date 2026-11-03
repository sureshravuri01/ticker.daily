"""Append-only data lake: date-partitioned Parquet files, mirrored to Cloudflare R2.

Layout in the private data bucket:
    raw/<dataset>/dt=YYYY-MM-DD/<run_id>.parquet   immutable ingested records
    posts/dt=YYYY-MM-DD/<post_id>.json              full generation record per post
Rendered images go to the public media bucket under posts/<date>/<post_id>/.

Nothing is ever overwritten: every run writes new files keyed by run_id.
"""
from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from . import config


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _r2_client():
    import boto3  # imported lazily so tests run without it

    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def upload(local: Path, key: str, bucket: str) -> str:
    """Upload a file to R2. Returns the key (or public URL for the media bucket)."""
    if not config.r2_enabled():
        return str(local)
    ctype = mimetypes.guess_type(str(local))[0] or "application/octet-stream"
    _r2_client().upload_file(str(local), bucket, key, ExtraArgs={"ContentType": ctype})
    if bucket == config.R2_MEDIA_BUCKET and config.R2_PUBLIC_URL:
        return f"{config.R2_PUBLIC_URL}/{key}"
    return key


def save_records(dataset: str, records: list[dict], rid: str, day: str) -> Path | None:
    """Write one batch of records for a dataset, locally and to R2."""
    if not records:
        return None
    import pandas as pd

    rows = []
    fetched_at = datetime.now(timezone.utc).isoformat()
    for r in records:
        row = {k: v for k, v in r.items() if k != "payload"}
        # Keep the untouched source response so we can re-derive anything later.
        row["payload"] = json.dumps(r.get("payload", r), default=str)
        row["fetched_at"] = fetched_at
        row["run_id"] = rid
        row["schema_version"] = config.SCHEMA_VERSION
        rows.append(row)
    df = pd.DataFrame(rows)

    rel = Path("raw") / dataset / f"dt={day}"
    out_dir = config.LOCAL_DATA_DIR / rel
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        path = out_dir / f"{rid}.parquet"
        df.to_parquet(path, index=False)
    except ImportError:  # pyarrow missing (local dev only): fall back to JSONL
        path = out_dir / f"{rid}.jsonl"
        df.to_json(path, orient="records", lines=True)
    upload(path, f"{rel.as_posix()}/{path.name}", config.R2_DATA_BUCKET)
    return path


def sync_recent_posts(days: int = 4) -> int:
    """Pull the last few days of post records from R2 (CI runners start empty)."""
    if not config.r2_enabled():
        return 0
    from datetime import date, timedelta

    client, n = _r2_client(), 0
    for i in range(days):
        prefix = f"posts/dt={(config.market_date() - timedelta(days=i)).isoformat()}/"
        for obj in client.list_objects_v2(Bucket=config.R2_DATA_BUCKET, Prefix=prefix).get("Contents", []):
            dest = config.LOCAL_DATA_DIR / obj["Key"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(config.R2_DATA_BUCKET, obj["Key"], str(dest))
            n += 1
    return n


def save_post_record(record: dict, day: str) -> Path:
    rel = Path("posts") / f"dt={day}"
    out_dir = config.LOCAL_DATA_DIR / rel
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{record['post_id']}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    upload(path, f"{rel.as_posix()}/{path.name}", config.R2_DATA_BUCKET)
    return path


def load_post_record(post_id: str) -> dict | None:
    """Find a post record locally or in R2. post_id starts with its date (YYYY-MM-DD-...)."""
    day = post_id[:10]
    rel = f"posts/dt={day}/{post_id}.json"
    local = config.LOCAL_DATA_DIR / rel
    if not local.exists() and config.r2_enabled():
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            _r2_client().download_file(config.R2_DATA_BUCKET, rel, str(local))
        except Exception:
            return None
    return json.loads(local.read_text()) if local.exists() else None


def save_decision(decision: dict, day: str) -> Path:
    """Append-only log of approve/reject/regen decisions: training data for the learning loop."""
    rel = Path("decisions") / f"dt={day}"
    out_dir = config.LOCAL_DATA_DIR / rel
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{decision['post_id']}-{run_id()}.json"
    path.write_text(json.dumps(decision, indent=2))
    upload(path, f"{rel.as_posix()}/{path.name}", config.R2_DATA_BUCKET)
    return path


# ---------- small JSON helpers for the private data bucket ----------
def get_json(key: str) -> dict | None:
    local = config.LOCAL_DATA_DIR / key
    if config.r2_enabled():
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            _r2_client().download_file(config.R2_DATA_BUCKET, key, str(local))
        except Exception:
            return None
    return json.loads(local.read_text()) if local.exists() else None


def put_json(key: str, obj: dict) -> None:
    """Overwrites. Only for mutable state (e.g. the current Instagram token), never for history."""
    local = config.LOCAL_DATA_DIR / key
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(obj, indent=2, default=str))
    upload(local, key, config.R2_DATA_BUCKET)


def list_keys(prefix: str) -> list[str]:
    if config.r2_enabled():
        keys, token = [], None
        while True:
            kw = {"Bucket": config.R2_DATA_BUCKET, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = _r2_client().list_objects_v2(**kw)
            keys += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return keys
            token = page["NextContinuationToken"]
    root = config.LOCAL_DATA_DIR
    return sorted(p.relative_to(root).as_posix() for p in root.glob(prefix + "**/*") if p.is_file())


def fetch(key: str) -> Path | None:
    """Local copy of any object in the data bucket (downloaded from R2 when needed)."""
    local = config.LOCAL_DATA_DIR / key
    if not local.exists() and config.r2_enabled():
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            _r2_client().download_file(config.R2_DATA_BUCKET, key, str(local))
        except Exception:
            return None
    return local if local.exists() else None
