"""The snapshot: what each class's booking count was at the last look.

Event ids and integers only. There is no client data in the feed this app
reads, so there is none to keep here either.

Two backings, because the snapshot is the one piece of state the watch cannot
do without:

* a local file, for a laptop or a small always-on box;
* a Google Cloud Storage object, for Cloud Run -- whose containers are
  ephemeral, so a local file would vanish between executions and *every* run
  would look like a first run: baseline saved, nothing ever reported. That
  failure is silent, which is why it gets a real backend rather than a warning
  in the README.

Pick with STATE_URI: `gs://bucket/notify/state.json` or a filesystem path.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = APP_ROOT / "var" / "watch_state.json"


class StateConflict(RuntimeError):
    """Someone else wrote the snapshot while we were working. The caller must
    abort rather than clobber it -- the next run reads the winner and carries
    on."""


def _encode(counts: dict[str, int]) -> str:
    return json.dumps({"saved_at": datetime.now().isoformat(timespec="seconds"),
                       "booked": counts}, ensure_ascii=False, indent=2)


def _decode(raw: str) -> dict[str, int]:
    data = json.loads(raw)
    return {str(k): int(v) for k, v in (data.get("booked") or {}).items()}


class Store(Protocol):
    """Where the snapshot lives. `save` stores exactly the current reading, so
    ids that have left the window are pruned rather than accumulating."""

    name: str

    def load(self) -> dict[str, int]:
        ...

    def save(self, counts: dict[str, int]) -> None:
        ...


class FileStore:
    name = "file"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_FILE

    def __str__(self) -> str:
        return str(self.path)

    def load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        return _decode(self.path.read_text(encoding="utf-8"))

    def save(self, counts: dict[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_encode(counts), encoding="utf-8")


class GcsStore:
    """A single small JSON object in a bucket.

    Writes are guarded by the object's generation: two overlapping executions
    (Cloud Scheduler will happily start one while another is still running) must
    not silently overwrite each other's snapshot, because the loser's sign-ups
    would be lost with it.
    """

    name = "gcs"

    def __init__(self, bucket: str, blob: str) -> None:
        self.bucket_name, self.blob_name = bucket, blob
        self._generation: int | None = None
        self._client = None

    def __str__(self) -> str:
        return f"gs://{self.bucket_name}/{self.blob_name}"

    def _blob(self):
        if self._client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:      # pragma: no cover - deploy-time
                raise RuntimeError(
                    "STATE_URI is a gs:// URI but google-cloud-storage is not "
                    "installed. pip install -r requirements-cloud.txt") from exc
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name).blob(self.blob_name)

    def load(self) -> dict[str, int]:
        from google.api_core import exceptions

        blob = self._blob()
        try:
            raw = blob.download_as_text()
        except exceptions.NotFound:
            self._generation = 0           # 0 means "must not exist yet"
            return {}
        self._generation = blob.generation
        return _decode(raw)

    def save(self, counts: dict[str, int]) -> None:
        from google.api_core import exceptions

        try:
            self._blob().upload_from_string(
                _encode(counts), content_type="application/json",
                if_generation_match=self._generation)
        except exceptions.PreconditionFailed as exc:
            raise StateConflict(
                f"{self} changed while this run was working -- another "
                f"execution overlapped. Nothing written; the next run will "
                f"read the current snapshot.") from exc


def from_env(override: str | None = None) -> Store:
    """The store STATE_URI asks for, or a local file."""
    uri = (override if override is not None
           else os.getenv("STATE_URI", "")).strip()
    if uri.startswith("gs://"):
        rest = uri[len("gs://"):]
        bucket, _, blob = rest.partition("/")
        if not bucket or not blob:
            raise RuntimeError(
                f"STATE_URI={uri!r} needs both a bucket and an object name, "
                f"e.g. gs://my-bucket/notify/state.json")
        return GcsStore(bucket, blob)
    return FileStore(uri or None)
