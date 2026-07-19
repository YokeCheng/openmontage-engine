"""Crash-safe filesystem store for engine jobs and append-only events."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL = {"succeeded", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EngineStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs_dir = root / "jobs"
        self.index_path = root / "idempotency.json"
        self._lock = threading.RLock()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._atomic_json(self.index_path, {})

    def _atomic_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _snapshot_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _events_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "events.jsonl"

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        path = self._snapshot_path(job_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job["updated_at"] = utc_now()
            self._atomic_json(self._snapshot_path(job["job_id"]), job)
            return deepcopy(job)

    def save_job_if_active(self, job: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """Persist a worker update without overwriting a concurrent terminal state.

        Render threads work outside the store lock. Cancellation can therefore
        win while a thread is preparing media or starting Remotion. The final
        status check and write must be one atomic operation; otherwise a stale
        ``rendering`` snapshot can resurrect a cancelled job.
        """

        with self._lock:
            current = self.load_job(str(job["job_id"]))
            if current is None or current.get("status") in TERMINAL:
                return deepcopy(current), False
            return self.save_job(job), True

    def list_jobs(self, tenant_id: str) -> list[dict[str, Any]]:
        return [job for job in self.list_all_jobs() if job.get("tenant_id") == tenant_id]

    def list_all_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            jobs.append(job)
        return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)

    def append_event(
        self,
        job: dict[str, Any],
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self.events(job["job_id"])
            event = {
                "schema_version": "1.0",
                "event_id": new_id("evt"),
                "job_id": job["job_id"],
                "tenant_id": job["tenant_id"],
                "sequence": len(existing) + 1,
                "type": event_type,
                "occurred_at": utc_now(),
                "correlation_id": job.get("correlation_id"),
                "data": data or {},
            }
            path = self._events_path(job["job_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def events(self, job_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        path = self._events_path(job_id)
        if not path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(event.get("sequence", 0)) > after_sequence:
                result.append(event)
        return result

    def create_job(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        correlation_id: str,
    ) -> tuple[dict[str, Any], bool]:
        with self._lock:
            safe_body = deepcopy(body)
            safe_body.pop("credential_grants", None)
            digest = canonical_digest(safe_body)
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
            index_key = f"{safe_body['tenant_id']}:{idempotency_key}"
            existing = index.get(index_key)
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("IDEMPOTENCY_KEY_REUSED")
                job = self.load_job(existing["job_id"])
                if job is None:
                    raise RuntimeError("idempotency index points to a missing job")
                return job, False

            now = utc_now()
            job_id = new_id("job")
            job = {
                "schema_version": "1.0",
                "job_id": job_id,
                "request_id": safe_body["request_id"],
                "tenant_id": safe_body["tenant_id"],
                "created_by": safe_body["created_by"],
                "pipeline": safe_body["pipeline"],
                "status": "created",
                "stage": "accepted",
                "progress": {"percent": 0, "message": "Job accepted", "updated_at": now},
                "input": safe_body["input"],
                "config_version": safe_body["config_version"],
                "execution_mode": safe_body.get("execution_mode", "engine_managed"),
                "defer_start": bool(safe_body.get("defer_start", False)),
                "inputs": [],
                "approval": None,
                "actions": [],
                "artifacts": [],
                "error": None,
                "correlation_id": correlation_id,
                "created_at": now,
                "updated_at": now,
            }
            self.save_job(job)
            index[index_key] = {"digest": digest, "job_id": job_id}
            self._atomic_json(self.index_path, index)
            self.append_event(job, "job.created", {"status": "created"})
            return job, True

    def artifact_path(self, job_id: str, artifact_id: str) -> Path | None:
        job = self.load_job(job_id)
        if not job:
            return None
        artifact = next(
            (item for item in job.get("artifacts", []) if item.get("artifact_id") == artifact_id),
            None,
        )
        if not artifact:
            return None
        candidate = (self._job_dir(job_id) / artifact["storage_name"]).resolve()
        if self._job_dir(job_id).resolve() not in candidate.parents:
            return None
        return candidate
