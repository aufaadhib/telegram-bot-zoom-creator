from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time


@dataclass
class _PendingOtp:
    user_id: int
    email: str
    created_at: float = field(default_factory=time.time)
    otp_code: str = ""
    event: threading.Event = field(default_factory=threading.Event)


class OtpManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingOtp] = {}

    def create_request(self, job_id: str, user_id: int, email: str) -> None:
        with self._lock:
            self._pending[job_id] = _PendingOtp(user_id=user_id, email=email)

    def has_pending_for_user(self, user_id: int) -> bool:
        with self._lock:
            return any(item.user_id == user_id for item in self._pending.values())

    def submit_for_job(self, user_id: int, job_id: str, otp_code: str) -> bool:
        clean = (otp_code or "").strip()
        if len(clean) != 6 or not clean.isdigit():
            return False
        with self._lock:
            pending = self._pending.get(job_id)
            if not pending or pending.user_id != user_id:
                return False
            pending.otp_code = clean
            pending.event.set()
            return True

    def submit_for_user_active_job(self, user_id: int, otp_code: str) -> str | None:
        clean = (otp_code or "").strip()
        if len(clean) != 6 or not clean.isdigit():
            return None
        with self._lock:
            active = [(job_id, item) for job_id, item in self._pending.items() if item.user_id == user_id]
            if not active:
                return None
            active.sort(key=lambda pair: pair[1].created_at)
            target_job_id, pending = active[0]
            pending.otp_code = clean
            pending.event.set()
            return target_job_id

    def wait_for_otp(self, job_id: str, timeout_sec: int = 180) -> str | None:
        with self._lock:
            pending = self._pending.get(job_id)
            if not pending:
                return None
            event = pending.event

        event.wait(timeout=max(1, int(timeout_sec)))

        with self._lock:
            current = self._pending.get(job_id)
            if not current:
                return None
            code = current.otp_code.strip()
            self._pending.pop(job_id, None)
            if len(code) == 6 and code.isdigit():
                return code
            return None

    def clear_request(self, job_id: str) -> None:
        with self._lock:
            self._pending.pop(job_id, None)
