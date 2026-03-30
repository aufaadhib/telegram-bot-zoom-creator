import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import io
import logging
import threading
from typing import Any

from telegram.ext import Application

from bot.runtime import Runtime
from lib.selenium.worker import run_visit_job, run_zoom_signup_initial_job
from utils.user_manager import pop_user_vccs, return_user_vccs
from utils.vcc_stock_manager import pop_stock_vccs, return_stock_vccs


logger = logging.getLogger("telegram-selenium-bot")


def send_from_thread(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
) -> None:
    future = asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode),
        loop,
    )
    try:
        future.result(timeout=30)
    except Exception:
        logger.exception("Gagal kirim pesan dari thread.")


def send_text_and_get_message_id_from_thread(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    chat_id: int,
    text: str,
) -> int:
    async def _send():
        return await app.bot.send_message(chat_id=chat_id, text=text)

    future = asyncio.run_coroutine_threadsafe(_send(), loop)
    try:
        msg = future.result(timeout=30)
        return int(msg.message_id)
    except Exception:
        logger.exception("Gagal kirim pesan progress awal.")
        return 0


def edit_message_from_thread(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    chat_id: int,
    message_id: int,
    text: str,
) -> None:
    if not message_id:
        return

    async def _edit() -> None:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)

    future = asyncio.run_coroutine_threadsafe(_edit(), loop)
    try:
        future.result(timeout=30)
    except Exception:
        # Ignore race / not modified / temp edit errors.
        pass


def delete_message_from_thread(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    chat_id: int,
    message_id: int,
) -> None:
    if not message_id:
        return

    async def _delete() -> None:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)

    future = asyncio.run_coroutine_threadsafe(_delete(), loop)
    try:
        future.result(timeout=30)
    except Exception:
        pass


def send_file_from_thread(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    chat_id: int,
    filename: str,
    content: str,
) -> None:
    async def _send() -> None:
        payload = io.BytesIO(content.encode("utf-8"))
        payload.name = filename
        await app.bot.send_document(
            chat_id=chat_id,
            document=payload,
            filename=filename,
        )

    future = asyncio.run_coroutine_threadsafe(_send(), loop)
    try:
        future.result(timeout=30)
    except Exception:
        logger.exception("Gagal kirim file dari thread.")


def _build_loading_bar(percent: int) -> str:
    bounded = max(0, min(100, int(percent)))
    total_slots = 14
    filled = int((bounded / 100) * total_slots)
    if filled > total_slots:
        filled = total_slots
    return ("█" * filled) + ("░" * (total_slots - filled))


class JobProgressTracker:
    def __init__(self, loop: asyncio.AbstractEventLoop, app: Application, chat_id: int) -> None:
        self._loop = loop
        self._app = app
        self._chat_id = chat_id
        self._lock = threading.Lock()
        self._message_id = send_text_and_get_message_id_from_thread(
            loop,
            app,
            chat_id,
            "Memulai proses...\nLoading ░░░░░░░░░░░░░░ 0%",
        )
        self._run_steps: dict[int, int] = {}
        self._total_steps = 16

    def update(self, run_index: int, stage: str) -> None:
        with self._lock:
            step = self._run_steps.get(run_index, 0) + 1
            if step > self._total_steps:
                step = self._total_steps
            self._run_steps[run_index] = step
            percent = int((step / self._total_steps) * 100)
            text = f"Run #{run_index}: {stage}\nLoading {_build_loading_bar(percent)} {percent}%"

        edit_message_from_thread(
            self._loop,
            self._app,
            self._chat_id,
            self._message_id,
            text,
        )

    def finalize(self, text: str) -> None:
        edit_message_from_thread(
            self._loop,
            self._app,
            self._chat_id,
            self._message_id,
            text,
        )

    def delete(self) -> None:
        delete_message_from_thread(
            self._loop,
            self._app,
            self._chat_id,
            self._message_id,
        )


def _return_failed_vccs(vcc_source: str, user_id: int, vccs: list[str]) -> int:
    if not vccs:
        return 0
    if vcc_source == "store":
        return return_stock_vccs(vccs)
    if vcc_source == "personal":
        return return_user_vccs(user_id, vccs)
    return 0


def _mask_vcc(vcc: str) -> str:
    raw = (vcc or "").strip()
    card = raw.split("|", 1)[0].strip()
    if not card:
        return "****"
    last4 = card[-4:] if len(card) >= 4 else card
    masked_len = max(0, len(card) - len(last4))
    return f"{'*' * masked_len}{last4}"


def _request_manual_otp_for_job(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    runtime: Runtime,
    chat_id: int,
    user_id: int,
    job_id: str,
    email: str,
    run_index: int,
) -> str | None:
    runtime.otp_manager.create_request(job_id=job_id, user_id=user_id, email=email)
    send_from_thread(
        loop,
        app,
        chat_id,
        (
            f"Run #{run_index} butuh OTP Zoom untuk {email}.\n"
            "Auto OTP tidak ditemukan.\n"
            "Kirim OTP 6 digit langsung, atau:\n"
            f"/otp {job_id} 123456\n"
            "Timeout: 180 detik."
        ),
    )
    otp_code = runtime.otp_manager.wait_for_otp(job_id=job_id, timeout_sec=180)
    if otp_code:
        send_from_thread(loop, app, chat_id, f"OTP diterima untuk job {job_id}. Lanjut proses.")
        return otp_code
    send_from_thread(loop, app, chat_id, f"OTP timeout untuk job {job_id}.")
    return None


def _pop_next_vcc(vcc_source: str, user_id: int) -> str:
    if vcc_source == "store":
        values = pop_stock_vccs(1)
        return values[0] if values else ""
    if vcc_source == "personal":
        values = pop_user_vccs(user_id, 1)
        return values[0] if values else ""
    return ""


def _run_one_visit_with_global_slot(
    loop: asyncio.AbstractEventLoop,
    app: Application,
    runtime: Runtime,
    chat_id: int,
    user_id: int,
    job_id: str,
    index: int,
    url: str,
    assigned_vcc: str = "",
    vcc_source: str = "",
    email_domain: str = "",
    signup_password: str = "",
    trial_days: int = 14,
    progress_callback=None,
) -> dict[str, Any]:
    # Global limiter: maksimal hanya N browser aktif di seluruh aplikasi.
    logger.info("Run start | job=%s | idx=%s | url=%s", job_id, index, url)
    runtime.driver_slots.acquire()
    try:
        current_vcc = assigned_vcc
        attempted_vccs: list[str] = []
        attempts = 0
        max_attempts = max(1, runtime.settings.payment_max_card_attempts)
        while True:
            attempts += 1
            if current_vcc:
                attempted_vccs.append(current_vcc)
            try:
                if "zoom.us/signup" in url and email_domain.strip():
                    result = run_zoom_signup_initial_job(
                        profile_root=runtime.settings.selenium_profile_dir,
                        profile_key=f"{user_id}_{job_id}_{index}",
                        url=url,
                        wait_timeout=runtime.settings.selenium_wait_timeout,
                        email_domain=email_domain,
                        signup_password=signup_password,
                        trial_days=trial_days,
                        payment_vcc=current_vcc,
                        otp_resolver=lambda generated_email: _request_manual_otp_for_job(
                            loop=loop,
                            app=app,
                            runtime=runtime,
                            chat_id=chat_id,
                            user_id=user_id,
                            job_id=job_id,
                            email=generated_email,
                            run_index=index,
                        ),
                        progress_callback=progress_callback,
                        headless=runtime.settings.selenium_headless,
                        auto_close=runtime.settings.selenium_auto_close,
                        locale=runtime.settings.selenium_locale,
                        timezone=runtime.settings.selenium_timezone,
                        chromedriver_path=runtime.settings.chromedriver_path,
                        chrome_binary=runtime.settings.chrome_binary,
                    )
                else:
                    result = run_visit_job(
                        profile_root=runtime.settings.selenium_profile_dir,
                        profile_key=f"{user_id}_{job_id}_{index}",
                        url=url,
                        wait_timeout=runtime.settings.selenium_wait_timeout,
                        headless=runtime.settings.selenium_headless,
                        auto_close=runtime.settings.selenium_auto_close,
                        locale=runtime.settings.selenium_locale,
                        timezone=runtime.settings.selenium_timezone,
                        chromedriver_path=runtime.settings.chromedriver_path,
                        chrome_binary=runtime.settings.chrome_binary,
                    )

                return {
                    "index": index,
                    "status": "success",
                    "vcc": current_vcc,
                    "vcc_returnable": False,
                    "retry_failed_vccs": [value for value in attempted_vccs[:-1] if value],
                    "title": result["title"],
                    "current_url": result["current_url"],
                    "body_preview": result["body_preview"],
                    "birth_year": result.get("birth_year", ""),
                    "generated_email": result.get("generated_email", ""),
                    "otp_source": result.get("otp_source", ""),
                }
            except Exception as exc:
                message = str(exc)
                if (
                    "CARD_INVALID:" in message
                    and runtime.settings.payment_retry_on_card_error
                    and attempts < max_attempts
                ):
                    next_vcc = _pop_next_vcc(vcc_source=vcc_source, user_id=user_id)
                    if next_vcc:
                        logger.warning(
                            "Card invalid, retrying with next VCC | job=%s | idx=%s | attempt=%s/%s",
                            job_id,
                            index,
                            attempts + 1,
                            max_attempts,
                        )
                        current_vcc = next_vcc
                        continue

                logger.exception("Run failed | job=%s | idx=%s", job_id, index)
                return {
                    "index": index,
                    "status": "failed",
                    "vcc": current_vcc,
                    "vcc_returnable": True,
                    "retry_failed_vccs": [value for value in attempted_vccs[:-1] if value],
                    "error": message,
                }
    finally:
        runtime.driver_slots.release()
        logger.info("Run end | job=%s | idx=%s", job_id, index)


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
    email_domain: str = "",
    signup_password: str = "",
    credit_per_success: int = 0,
) -> None:
    logger.info("Job start | job=%s | user=%s | url=%s", job_id, user_id, url)
    runtime.jobs.update_job(job_id, status="running")
    assigned_vccs = [str(item).strip() for item in (reserved_vccs or []) if str(item).strip()]
    runs: list[dict[str, Any]] = []
    progress_tracker = JobProgressTracker(loop=loop, app=app, chat_id=chat_id)
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
                    loop,
                    app,
                    runtime,
                    chat_id,
                    user_id,
                    job_id,
                    idx,
                    url,
                    assigned_vccs[idx - 1] if idx - 1 < len(assigned_vccs) else "",
                    vcc_source,
                    email_domain,
                    signup_password,
                    trial_days,
                    (lambda stage, run_idx=idx: progress_tracker.update(run_idx, stage)),
                )
                for idx in range(1, count + 1)
            ]
            for future in as_completed(futures):
                runs.append(future.result())

        runs.sort(key=lambda item: int(item.get("index", 0)))
        success_count = sum(1 for item in runs if item.get("status") == "success")
        failed_count = count - success_count
        successful_vccs = [str(item.get("vcc", "")).strip() for item in runs if item.get("status") == "success"]
        successful_vccs = [value for value in successful_vccs if value]
        failed_vccs_all: list[str] = []
        failed_vccs_to_return: list[str] = []
        for item in runs:
            retry_failed = [str(v).strip() for v in (item.get("retry_failed_vccs") or []) if str(v).strip()]
            failed_vccs_all.extend(retry_failed)
            failed_vccs_to_return.extend(retry_failed)

            if item.get("status") == "failed":
                v = str(item.get("vcc", "")).strip()
                if v:
                    failed_vccs_all.append(v)
                    if bool(item.get("vcc_returnable", True)):
                        failed_vccs_to_return.append(v)

        returned_count = _return_failed_vccs(vcc_source, user_id, failed_vccs_to_return)
        deducted_credits = 0
        new_balance = runtime.vouchers.get_balance(user_id)
        if (
            success_count > 0
            and credit_per_success > 0
            and user_id not in runtime.settings.admin_user_ids
        ):
            try:
                deducted_credits, new_balance = runtime.vouchers.consume_credits(
                    user_id=user_id,
                    amount=success_count * credit_per_success,
                )
            except ValueError:
                deducted_credits = 0
                new_balance = runtime.vouchers.get_balance(user_id)

        mode_line = f"Mode: {flow_mode}\n" if flow_mode else ""
        trial_line = f"Trial: {trial_days} Hari\n" if trial_days > 0 else ""
        summary = (
            f"Proses create selesai.\n"
            f"{mode_line}"
            f"{trial_line}"
            f"Total request: {count}\n"
            f"Berhasil: {success_count}\n"
            f"Gagal: {failed_count}\n"
            f"Credits terpotong: {deducted_credits}\n"
            f"Sisa credits: {new_balance}\n"
            f"VCC berhasil: {len(successful_vccs)}\n"
            f"VCC gagal: {len(failed_vccs_all)}\n"
            f"VCC dikembalikan: {returned_count}"
        )

        success_accounts = []
        for item in runs:
            if item.get("status") != "success":
                continue
            email = str(item.get("generated_email", "")).strip()
            if not email:
                continue
            success_accounts.append(f"{email}|{signup_password}")

        account_preview = success_accounts[:10]
        hidden_accounts = len(success_accounts) - len(account_preview)
        account_preview_text = "\n".join(account_preview) if account_preview else "-"
        if hidden_accounts > 0:
            account_preview_text += f"\n... dan {hidden_accounts} akun lainnya."

        success_vcc_preview = successful_vccs[:10]
        failed_vcc_preview = failed_vccs_all[:10]
        success_vcc_text = "\n".join(_mask_vcc(v) for v in success_vcc_preview) if success_vcc_preview else "-"
        failed_vcc_text = "\n".join(_mask_vcc(v) for v in failed_vcc_preview) if failed_vcc_preview else "-"
        hidden_success_vcc = len(successful_vccs) - len(success_vcc_preview)
        hidden_failed_vcc = len(failed_vccs_all) - len(failed_vcc_preview)
        if hidden_success_vcc > 0:
            success_vcc_text += f"\n... dan {hidden_success_vcc} VCC berhasil lainnya."
        if hidden_failed_vcc > 0:
            failed_vcc_text += f"\n... dan {hidden_failed_vcc} VCC gagal lainnya."

        runtime.jobs.update_job(job_id, status="success", result=summary)
        logger.info("Job success | job=%s | requested=%s | success=%s | failed=%s", job_id, count, success_count, failed_count)
        send_from_thread(
            loop,
            app,
            chat_id,
            (
                f"<blockquote>{html.escape(summary)}</blockquote>\n\n"
                "Akun berhasil (Email|Password):\n"
                f"<pre>{html.escape(account_preview_text)}</pre>\n\n"
                "VCC berhasil:\n"
                f"<pre>{html.escape(success_vcc_text)}</pre>\n\n"
                "VCC gagal:\n"
                f"<pre>{html.escape(failed_vcc_text)}</pre>"
            ),
            parse_mode="HTML",
        )

        if success_accounts:
            accounts_content = "\n".join(success_accounts) + "\n"
            send_file_from_thread(
                loop,
                app,
                chat_id,
                filename=f"accounts_{job_id}.txt",
                content=accounts_content,
            )

        vcc_report_lines = [
            f"Job: {job_id}",
            f"Total request: {count}",
            f"VCC berhasil ({len(successful_vccs)}):",
            *[_mask_vcc(v) for v in successful_vccs],
            "",
            f"VCC gagal ({len(failed_vccs_all)}):",
            *[_mask_vcc(v) for v in failed_vccs_all],
            "",
            f"VCC dikembalikan: {returned_count}",
        ]
        send_file_from_thread(
            loop,
            app,
            chat_id,
            filename=f"vcc_report_{job_id}.txt",
            content="\n".join(vcc_report_lines) + "\n",
        )
        progress_tracker.delete()
    except Exception as exc:
        success_vccs = {
            str(item.get("vcc", "")).strip()
            for item in runs
            if item.get("status") == "success" and str(item.get("vcc", "")).strip()
        }
        rollback_vccs = [value for value in assigned_vccs if value and value not in success_vccs]
        rolled_back = _return_failed_vccs(vcc_source, user_id, rollback_vccs)
        runtime.jobs.update_job(job_id, status="failed", error=str(exc))
        logger.exception("Job failed | job=%s", job_id)
        rollback_info = ""
        if rollback_vccs:
            rollback_info = f"\nRollback VCC: {rolled_back}/{len(rollback_vccs)}"
        send_from_thread(loop, app, chat_id, f"Job {job_id} gagal: {exc}{rollback_info}")
        progress_tracker.delete()
