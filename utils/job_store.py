from dataclasses import dataclass
import threading
from typing import Dict, Optional
from uuid import uuid4


@dataclass
class JobInfo:
    job_id: str
    user_id: int
    url: str
    status: str = "queued"
    result: str = ""
    error: str = ""


class JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, JobInfo] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _is_active_status(status: str) -> bool:
        return status in {"queued", "running"}

    def _get_active_job_for_user_locked(self, user_id: int) -> Optional[JobInfo]:
        active: Optional[JobInfo] = None
        for job in self._jobs.values():
            if job.user_id != user_id:
                continue
            if self._is_active_status(job.status):
                active = job
        return active

    def add_job(self, user_id: int, url: str) -> Optional[JobInfo]:
        with self._lock:
            active_job = self._get_active_job_for_user_locked(user_id)
            if active_job:
                return None
            job = JobInfo(job_id=uuid4().hex[:10], user_id=user_id, url=url)
            self._jobs[job.job_id] = job
        return job

    def update_job(self, job_id: str, status: str, result: str = "", error: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = status
            if result:
                job.result = result
            if error:
                job.error = error

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_active_job_for_user(self, user_id: int) -> Optional[JobInfo]:
        with self._lock:
            return self._get_active_job_for_user_locked(user_id)


class ProfileLockManager:
    def __init__(self) -> None:
        self._profile_locks: Dict[int, threading.Lock] = {}
        self._lock = threading.Lock()

    def get_lock(self, user_id: int) -> threading.Lock:
        with self._lock:
            if user_id not in self._profile_locks:
                self._profile_locks[user_id] = threading.Lock()
            return self._profile_locks[user_id]
