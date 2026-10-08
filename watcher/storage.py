"""
storage.py — state persistence for the watcher, S3 in Lambda / file locally.

State is a single JSON blob (see differ.py for its schema). We overwrite the
whole blob each run (state is small: a few hundred companies x their job-id
lists). A dated backup is written once per day so a bad run can't nuke history.
"""

from __future__ import annotations

import json
import logging
import os
import datetime as _dt

logger = logging.getLogger("ats-watcher.storage")


class LocalStorage:
    """State on the local filesystem (for --local dev)."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path) as f:
                return json.load(f)
        return {}

    def save(self, state: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, self.path)  # atomic on the same filesystem

    def backup(self, state: dict) -> None:
        date = _dt.date.today().isoformat()
        bpath = f"{self.path}.backup.{date}.json"
        if not os.path.exists(bpath):
            with open(bpath, "w") as f:
                json.dump(state, f)


class S3Storage:
    """State in S3. Daily backup under state.backup.<date>.json."""

    def __init__(self, bucket: str, key: str):
        import boto3
        self.bucket = bucket
        self.key = key
        self.s3 = boto3.client("s3")

    def load(self) -> dict:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        except self.s3.exceptions.NoSuchKey:
            logger.info("no state at s3://%s/%s — first run", self.bucket, self.key)
            return {}
        # Everything else (permissions, throttling, an S3 outage) must propagate.
        # Swallowing it here reads as "first run", which silently reseeds and
        # overwrites good state — losing dedup history with no visible error.
        # A corrupt body must fail the same way, so json.loads stays outside
        # the except block too.
        return json.loads(obj["Body"].read())

    def save(self, state: dict) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self.key,
                           Body=json.dumps(state).encode(), ContentType="application/json")

    def backup(self, state: dict) -> None:
        date = _dt.date.today().isoformat()
        base = self.key.rsplit(".json", 1)[0]
        bkey = f"{base}.backup.{date}.json"
        # one backup per day: only write if it doesn't exist yet
        try:
            self.s3.head_object(Bucket=self.bucket, Key=bkey)
            return
        except Exception:
            pass
        self.s3.put_object(Bucket=self.bucket, Key=bkey,
                           Body=json.dumps(state).encode(), ContentType="application/json")


def get_storage(local: bool, local_path: str = "state.local.json"):
    if local:
        return LocalStorage(local_path)
    bucket = os.environ["STATE_BUCKET"]
    key = os.environ.get("STATE_KEY", "ats-watcher/state.json")
    return S3Storage(bucket, key)
