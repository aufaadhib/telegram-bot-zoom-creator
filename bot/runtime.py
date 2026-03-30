from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading

from utils.config import Settings
from utils.job_store import JobStore, ProfileLockManager
from utils.voucher_store import VoucherStore


@dataclass
class Runtime:
    settings: Settings
    jobs: JobStore
    vouchers: VoucherStore
    profile_locks: ProfileLockManager
    driver_slots: threading.BoundedSemaphore
    executor: ThreadPoolExecutor


def build_runtime(settings: Settings) -> Runtime:
    executor_workers = max(settings.max_workers, settings.max_drivers)
    return Runtime(
        settings=settings,
        jobs=JobStore(),
        vouchers=VoucherStore(settings.voucher_db_path),
        profile_locks=ProfileLockManager(),
        driver_slots=threading.BoundedSemaphore(settings.max_drivers),
        executor=ThreadPoolExecutor(max_workers=executor_workers, thread_name_prefix="selenium"),
    )


def shutdown_runtime(runtime: Runtime) -> None:
    runtime.executor.shutdown(wait=False, cancel_futures=True)
