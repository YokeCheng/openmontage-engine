"""Bounded, restart-aware execution scheduler for OpenMontage jobs."""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from .renderer import render_job
from .store import EngineStore


class ExecutionScheduler:
    def __init__(self, store: EngineStore, repo_root: Path) -> None:
        workers = max(1, int(os.getenv("OPENMONTAGE_ENGINE_WORKERS", "2")))
        self._store = store
        self._repo_root = repo_root
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="openmontage-render")
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self.max_workers = workers

    def submit(self, job_id: str) -> bool:
        with self._lock:
            active = self._futures.get(job_id)
            if active is not None and not active.done():
                return False
            future = self._executor.submit(render_job, self._store, job_id, self._repo_root)
            self._futures[job_id] = future
            future.add_done_callback(lambda _: self._discard(job_id))
            return True

    def _discard(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    def recover(self) -> int:
        count = 0
        for job in self._store.list_all_jobs():
            if job.get("status") in {"queued", "running", "rendering"}:
                if self.submit(str(job["job_id"])):
                    count += 1
        return count

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
