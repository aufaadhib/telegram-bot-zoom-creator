import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from typing import Any

from telegram.ext import Application

from bot.runtime import Runtime
from lib.selenium.worker import run_visit_job
from utils.user_manager import return_user_vccs
from utils.vcc_stock_manager import return_stock_vccs


logger = logging.getLogger("telegram-selenium-bot")


def send_from_thread(loop: asyncio.AbstractEventLoop, app: Application, chat_id: int, text: str) -> None:
    future = asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id, text=text),
        loop,
    )
    try:
        future.result(timeout=30)
    except Exception:
        logger.exception("Gagal kirim pesan dari thread.")


def _return_failed_vccs(vcc_source: str, user_id: int, vccs: list[str]) -> int:
    if not vccs:
        return 0
    if vcc_source == "store":
        return return_stock_vccs(vccs)
    if vcc_source == "personal":
        return return_user_vccs(user_id, vccs)
    return 0


def _run_one_visit_with_global_slot(
    runtime: Runtime,
    user_id: int,
    job_id: str,
    index: int,
    url: str,
    assigned_vcc: str = "",
) -> dict[str, Any]:
    # Global limiter: maksimal hanya N browser aktif di seluruh aplikasi.
    runtime.driver_slots.acquire()
    try:
        result = run_visit_job(
            profile_root=runtime.settings.selenium_profile_dir,
            profile_key=f"{user_id}_{job_id}_{index}",
            url=url,
            wait_timeout=runtime.settings.selenium_wait_timeout,
            headless=runtime.settings.selenium_headless,
            chromedriver_path=runtime.settings.chromedriver_path,
            chrome_binary=runtime.settings.chrome_binary,
        )
        return {
            "index": index,
            "status": "success",
            "vcc": assigned_vcc,
            "title": result["title"],
            "current_url": result["current_url"],
            "body_preview": result["body_preview"],
        }
    except Exception as exc:
        return {
            "index": index,
            "status": "failed",
            "vcc": assigned_vcc,
            "error": str(exc),
        }
    finally:
        runtime.driver_slots.release()


def process_selenium_job(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    runtime: Runtime,
    chat_id: int,
    user_id: int,
    job_id: str,
    url: str,
    batch_count: int = 1,
    flow_mode: str = "",
    trial_days: int = 0,
    vcc_source: str = "",
    reserved_vccs: list[str] | None = None,
) -> None:
    runtime.jobs.update_job(job_id, status="running")
    assigned_vccs = [str(item).strip() for item in (reserved_vccs or []) if str(item).strip()]
    runs: list[dict[str, Any]] = []
    try:
        requested_count = max(1, int(batch_count))
        if assigned_vccs:
            count = len(assigned_vccs)
            if count != requested_count:
                logger.warning(
                    "Job %s count mismatch: requested=%s reserved_vccs=%s. Menggunakan reserved_vccs.",
                    job_id,
                    requested_count,
                    count,
                )
        else:
            count = requested_count

        worker_count = min(count, runtime.settings.max_drivers)

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"job-{job_id}") as pool:
            futures = [
                pool.submit(
                    _run_one_visit_with_global_slot,
                    runtime,
                    user_id,
                    job_id,
                    idx,
                    url,
                    assigned_vccs[idx - 1] if idx - 1 < len(assigned_vccs) else "",
                )
                for idx in range(1, count + 1)
            ]
            for future in as_completed(futures):
                runs.append(future.result())

        runs.sort(key=lambda item: int(item.get("index", 0)))
        success_count = sum(1 for item in runs if item.get("status") == "success")
        failed_count = count - success_count
        failed_vccs = [str(item.get("vcc", "")).strip() for item in runs if item.get("status") == "failed"]
        failed_vccs = [value for value in failed_vccs if value]
        returned_count = _return_failed_vccs(vcc_source, user_id, failed_vccs)

        vcc_summary = ""
        if assigned_vccs:
            consumed_count = len(assigned_vccs) - returned_count
            vcc_summary = (
                f"VCC allocated: {len(assigned_vccs)}\n"
                f"VCC returned: {returned_count}\n"
                f"VCC consumed: {consumed_count}\n"
            )

        if count == 1 and runs and runs[0].get("status") == "success":
            run = runs[0]
            summary = (
                f"Job {job_id} selesai.\n"
                f"URL: {run['current_url']}\n"
                f"Title: {run['title']}\n"
                f"Preview: {run['body_preview']}\n"
                f"{vcc_summary}".rstrip()
            )
        else:
            mode_line = f"Mode: {flow_mode}\n" if flow_mode else ""
            trial_line = f"Trial: {trial_days} Hari\n" if trial_days > 0 else ""
            preview_lines: list[str] = []
            for item in runs[:5]:
                if item["status"] == "success":
                    preview_lines.append(
                        f"{item['index']}. OK {item['title']} | {item['current_url']}"
                    )
                else:
                    preview_lines.append(f"{item['index']}. FAIL {item.get('error', 'Unknown error')}")

            hidden = len(runs) - len(preview_lines)
            hidden_line = f"\n... dan {hidden} run lain." if hidden > 0 else ""
            preview_text = "\n".join(preview_lines) if preview_lines else "-"
            summary = (
                f"Job {job_id} selesai.\n"
                f"{mode_line}"
                f"{trial_line}"
                f"Start URL: {url}\n"
                f"Requested: {count}\n"
                f"Success: {success_count}\n"
                f"Failed: {failed_count}\n\n"
                f"{vcc_summary}"
                f"Preview run:\n{preview_text}{hidden_line}"
            )

        runtime.jobs.update_job(job_id, status="success", result=summary)
        send_from_thread(loop, app, chat_id, summary)
    except Exception as exc:
        success_vccs = {
            str(item.get("vcc", "")).strip()
            for item in runs
            if item.get("status") == "success" and str(item.get("vcc", "")).strip()
        }
        rollback_vccs = [value for value in assigned_vccs if value and value not in success_vccs]
        rolled_back = _return_failed_vccs(vcc_source, user_id, rollback_vccs)
        runtime.jobs.update_job(job_id, status="failed", error=str(exc))
        rollback_info = ""
        if rollback_vccs:
            rollback_info = f"\nRollback VCC: {rolled_back}/{len(rollback_vccs)}"
        send_from_thread(loop, app, chat_id, f"Job {job_id} gagal: {exc}{rollback_info}")
