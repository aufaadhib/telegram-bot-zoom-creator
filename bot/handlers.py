import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial
import html
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.jobs import process_selenium_job
from bot.runtime import Runtime
from utils.domain_manager import add_domains, get_domains, normalize_domain, remove_domains
from utils.password_validator import validate_password_rules
from utils.user_manager import (
    add_user_vccs,
    delete_user_vccs,
    ensure_user_exists,
    edit_user_vccs,
    get_global_account_count,
    get_total_users,
    get_user_account_count,
    get_user_custom_domain,
    get_user_password,
    get_user_vccs,
    pop_user_vccs,
    return_user_vccs,
    set_user_custom_domain,
    set_user_password,
)
from utils.vcc_stock_manager import (
    add_stock_vccs,
    get_stock_count,
    get_stock_vccs,
    pop_stock_vccs,
    return_stock_vccs,
)


_VOUCHER_CODE_PATTERN = re.compile(r"^\s*(VC-[A-Za-z0-9]{10})\s*$", re.IGNORECASE)
_COMMAND_PREFIX_PATTERN = re.compile(r"^/\w+(?:@\w+)?\s*", re.DOTALL)


def _start_keyboard(is_admin: bool = False, has_credits: bool = False) -> InlineKeyboardMarkup:
    buttons = []

    if is_admin:
        buttons.append([InlineKeyboardButton("ðŸ›  Admin Panel", callback_data="admin_panel")])

    if has_credits:
        buttons.append([InlineKeyboardButton("ðŸš€ Mulai Buat Akun", callback_data="create_account")])
        buttons.append([InlineKeyboardButton("ðŸ“… Schedule Meeting", callback_data="schedule_meeting")])
        buttons.append(
            [
                InlineKeyboardButton("ðŸ’³ Virtual Credit Card", callback_data="vcc_menu"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton("ðŸ”’ Set Password", callback_data="set_password"),
                InlineKeyboardButton("ðŸŒ Domain", callback_data="user_domain_menu"),
            ]
        )
    else:
        buttons.append([InlineKeyboardButton("ðŸŽ Redeem Voucher", callback_data="redeem_voucher")])

    buttons.append([InlineKeyboardButton("â„¹ï¸ Info", callback_data="info")])
    return InlineKeyboardMarkup(buttons)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("â†© Back", callback_data="back_to_start")]]
    )


def _menu_back_keyboard(back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("â†© Back", callback_data=back_callback)]]
    )


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎟 Generate Voucher", callback_data="gen_voucher")],
            [
                InlineKeyboardButton("🌐 Domain Menu", callback_data="admin_domain_menu"),
                InlineKeyboardButton("🔒 Admin Set Password", callback_data="admin_set_password"),
            ],
            [InlineKeyboardButton("💳 VCC Store", callback_data="admin_vcc_stock_menu")],
            [InlineKeyboardButton("🏠 Home", callback_data="back_to_start")],
        ]
    )


def _extract_command_payload(message_text: str) -> str:
    return _COMMAND_PREFIX_PATTERN.sub("", message_text or "", count=1).strip()


def _split_lines(payload: str) -> list[str]:
    return [line.strip() for line in (payload or "").replace("\r", "").split("\n") if line.strip()]


def _password_requirements_text() -> str:
    return (
        "<b>Ketentuan password:</b>\n"
        "- Minimal 8 karakter\n"
        "- Minimal 1 huruf\n"
        "- Minimal 1 angka\n"
        "- Minimal 1 huruf kapital\n"
        "- Minimal 1 huruf kecil\n"
        "- Tidak boleh ada 4 karakter atau lebih berurutan "
        "(contoh: <code>1111</code>, <code>1234</code>, <code>abcd</code>, <code>qwer</code>)"
    )


def _password_error_lines(errors: list[str]) -> str:
    return "\n".join(f"- {error}" for error in errors)


def _domain_list_text(domains: list[str], limit: int = 30) -> str:
    if not domains:
        return "  Belum ada"
    preview = domains[:limit]
    text = "\n".join(f"  {idx + 1}. <code>{value}</code>" for idx, value in enumerate(preview))
    hidden = len(domains) - len(preview)
    if hidden > 0:
        text += f"\n  ... dan {hidden} domain lain."
    return text


def _vcc_list_text(vccs: list[str], limit: int = 10) -> str:
    if not vccs:
        return "  Belum ada"
    preview = vccs[:limit]
    text = "\n".join(f"  {idx + 1}. <code>{value}</code>" for idx, value in enumerate(preview))
    hidden = len(vccs) - len(preview)
    if hidden > 0:
        text += f"\n  ... dan {hidden} VCC lain."
    return text


def _resolve_effective_domain(user_id: int) -> tuple[str, str, str]:
    custom_domain = get_user_custom_domain(user_id).strip().lower()
    defaults = get_domains()
    default_domain = defaults[0] if defaults else ""
    effective_domain = custom_domain or default_domain
    return custom_domain, default_domain, effective_domain


def _build_user_domain_summary(user_id: int) -> str:
    custom_domain, default_domain, effective_domain = _resolve_effective_domain(user_id)
    custom_text = custom_domain or "(belum diset)"
    default_text = default_domain or "(belum ada default domain)"
    effective_text = effective_domain or "(belum ada domain aktif)"
    source = "custom domain user" if custom_domain else ("default domain admin" if default_domain else "-")

    return (
        "ðŸŒ <b>Domain Saya</b>\n\n"
        f"Custom Domain: <code>{custom_text}</code>\n"
        f"Default Domain: <code>{default_text}</code>\n"
        f"Effective Domain: <code>{effective_text}</code>\n"
        f"Sumber efektif: <b>{source}</b>\n\n"
        "Pilih aksi domain:"
    )


def _user_domain_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("âž• Set/Update Custom Domain", callback_data="user_set_domain")],
            [InlineKeyboardButton("ðŸ§¹ Gunakan Default (Hapus Custom)", callback_data="user_clear_domain")],
            [InlineKeyboardButton("ðŸ  Home", callback_data="back_to_start")],
        ]
    )


def _create_account_source_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("ðŸ¦ Gunakan VCC Store", callback_data="create_account_vcc_store")],
            [InlineKeyboardButton("ðŸ’³ Gunakan VCC Pribadi", callback_data="create_account_vcc_personal")],
            [InlineKeyboardButton("â†© Back", callback_data="back_to_start")],
        ]
    )


def _create_account_duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("7 Hari", callback_data="create_account_duration_7"),
                InlineKeyboardButton("14 Hari", callback_data="create_account_duration_14"),
            ],
            [InlineKeyboardButton("â†© Back", callback_data="create_account")],
        ]
    )


def _create_account_qty_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1", callback_data="create_account_qty_1"),
                InlineKeyboardButton("2", callback_data="create_account_qty_2"),
                InlineKeyboardButton("3", callback_data="create_account_qty_3"),
            ],
            [InlineKeyboardButton("Custom", callback_data="create_account_qty_custom")],
            [InlineKeyboardButton("â†© Back", callback_data="create_account")],
        ]
    )


def _clear_create_account_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("create_account_vcc_mode", None)
    context.chat_data.pop("create_account_trial_days", None)
    context.chat_data.pop("create_account_qty", None)


def _can_bypass_credit_check(user_id: int, runtime: Runtime) -> bool:
    return _is_admin(user_id, runtime) or (user_id in runtime.settings.credited_user_ids)


def _has_minimum_credit(user_id: int, runtime: Runtime, required: int) -> bool:
    if _can_bypass_credit_check(user_id, runtime):
        return True
    return runtime.vouchers.get_balance(user_id) >= required


def _format_indonesian_datetime() -> str:
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    months = [
        "Januari",
        "Februari",
        "Maret",
        "April",
        "Mei",
        "Juni",
        "Juli",
        "Agustus",
        "September",
        "Oktober",
        "November",
        "Desember",
    ]
    try:
        now = datetime.now(ZoneInfo("Asia/Jakarta"))
    except ZoneInfoNotFoundError:
        # Fallback for environments without IANA timezone database (common on Windows).
        now = datetime.now(timezone(timedelta(hours=7)))
    day_name = days[now.weekday()]
    month_name = months[now.month - 1]
    return f"{day_name}, {now.day:02d} {month_name} {now.year} - {now:%H:%M:%S}"


def _build_start_overview(update: Update, runtime: Runtime) -> str:
    user = update.effective_user
    if not user:
        return "Bot aktif."

    user_id = user.id
    ensure_user_exists(user_id)

    display_name = user.first_name or user.username or "User"
    username = f"@{user.username}" if user.username else "-"
    user_account_count = get_user_account_count(user_id)
    credits = runtime.vouchers.get_balance(user_id)
    global_accounts = get_global_account_count()
    total_users = get_total_users()
    password = get_user_password(user_id).strip() or "(belum diset)"
    custom_domain, _, effective_domain = _resolve_effective_domain(user_id)
    custom_domain_text = custom_domain or "(belum diset)"
    effective_domain_text = effective_domain or "(belum ada)"

    return (
        f"Halo {display_name} ðŸ‘‹\n"
        f"{_format_indonesian_datetime()}\n\n"
        "User Info\n"
        f"â”” ID: {user_id}\n"
        f"â”” Username: {username}\n"
        f"â”” Total Account (User): {user_account_count}\n"
        f"â”” Credits: {credits}\n\n"
        "BOT Stats\n"
        f"â”” Cost per Account: {runtime.settings.cost_per_account} Credits\n"
        f"â”” Total Account (Global): {global_accounts}\n"
        f"â”” Total User: {total_users}\n\n"
        "Configuration\n"
        f"â”” Password: {password}\n"
        f"â”” Custom Domain: {custom_domain_text}\n"
        f"â”” Effective Domain: {effective_domain_text}"
    )


def _resolve_user_flags(user_id: int, runtime: Runtime) -> tuple[bool, bool]:
    is_admin = user_id in runtime.settings.admin_user_ids
    has_credits = (
        is_admin
        or (user_id in runtime.settings.credited_user_ids)
        or (runtime.vouchers.get_balance(user_id) > 0)
    )
    return is_admin, has_credits


def _is_admin(user_id: int, runtime: Runtime) -> bool:
    return user_id in runtime.settings.admin_user_ids


def _extract_voucher_code(text: str) -> str | None:
    match = _VOUCHER_CODE_PATTERN.match(text or "")
    if not match:
        return None
    return match.group(1).upper()


def _redeem_instruction_text() -> str:
    return (
        "ðŸŽ <b>Redeem Voucher</b>\n\n"
        "Kirim kode voucher kamu dengan format:\n"
        "<code>/redeem KODE_VOUCHER</code>\n"
        "atau langsung kirim <code>KODE_VOUCHER</code>"
    )


async def _safe_edit_menu(
    query,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    try:
        await query.edit_message_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return
    except BadRequest:
        pass

    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        raise


async def _safe_edit_plain_menu(
    query,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    try:
        await query.edit_message_caption(
            caption=text,
            reply_markup=keyboard,
        )
        return
    except BadRequest:
        pass

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        raise


async def _handle_redeem_code(update: Update, code: str, runtime: Runtime) -> None:
    if not update.effective_user:
        return

    user_id = update.effective_user.id
    try:
        credits, new_balance = runtime.vouchers.redeem_voucher(code=code, user_id=user_id)
    except ValueError as exc:
        if update.message:
            await update.message.reply_text(
                f"âŒ Redeem gagal: {exc}",
                parse_mode="HTML",
                reply_markup=_back_keyboard(),
            )
        return

    is_admin, has_credits = _resolve_user_flags(user_id, runtime)
    success_text = (
        "âœ… <b>Redeem berhasil</b>\n\n"
        f"Kode: <code>{code}</code>\n"
        f"Credits ditambahkan: <b>{credits}</b>\n"
        f"Saldo sekarang: <b>{new_balance}</b>"
    )

    if update.message:
        await update.message.reply_text(
            success_text,
            parse_mode="HTML",
            reply_markup=_start_keyboard(is_admin=is_admin, has_credits=has_credits),
        )


async def _handle_user_set_password_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not update.message or not update.effective_user:
        return False

    awaiting = context.chat_data.get("awaiting_input")
    if awaiting != "user_set_password":
        return False

    password = (update.message.text or "").strip()
    if not password:
        await update.message.reply_text(
            "Password kosong. Kirim password baru kamu.",
            reply_markup=_menu_back_keyboard("back_to_start"),
        )
        return True

    errors = validate_password_rules(password)
    if errors:
        await update.message.reply_text(
            (
                "âŒ <b>Password tidak valid</b>\n\n"
                f"{_password_requirements_text()}\n\n"
                "<b>Detail:</b>\n"
                f"{_password_error_lines(errors)}\n\n"
                "Kirim password lain."
            ),
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("back_to_start"),
        )
        return True

    set_user_password(update.effective_user.id, password)
    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    preview_password = f"<tg-spoiler>{html.escape(password)}</tg-spoiler>"
    await update.message.reply_text(
        (
            "ðŸ”’ <b>Password default tersimpan</b>\n\n"
            f"Preview: {preview_password}\n"
            "Password ini akan dipakai saat flow auto create dijalankan."
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("back_to_start"),
    )
    return True


async def _handle_admin_set_password_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> bool:
    if not update.message or not update.effective_user:
        return False

    awaiting = context.chat_data.get("awaiting_input")
    if awaiting != "admin_set_password":
        return False

    if not _is_admin(update.effective_user.id, runtime):
        context.chat_data.pop("awaiting_input", None)
        context.chat_data.pop("prompt_msg_id", None)
        await update.message.reply_text("Akses admin ditolak.")
        return True

    lines = _split_lines(update.message.text or "")
    if not lines:
        await update.message.reply_text(
            "Input kosong. Format: <code>USER_ID|PASSWORD</code>",
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("admin_panel"),
        )
        return True

    success = 0
    invalid_format = 0
    invalid_policy = 0
    policy_error_preview: list[str] = []
    for line in lines:
        if "|" not in line:
            invalid_format += 1
            continue
        user_id_raw, password_raw = line.split("|", 1)
        user_id_raw = user_id_raw.strip()
        password_raw = password_raw.strip()
        if not user_id_raw.isdigit() or not password_raw:
            invalid_format += 1
            continue
        errors = validate_password_rules(password_raw)
        if errors:
            invalid_policy += 1
            if len(policy_error_preview) < 5:
                policy_error_preview.append(f"{user_id_raw}: {errors[0]}")
            continue
        set_user_password(int(user_id_raw), password_raw)
        success += 1

    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    invalid_total = invalid_format + invalid_policy
    policy_preview_text = ""
    if policy_error_preview:
        policy_preview_text = (
            "\n\n<b>Contoh invalid policy:</b>\n"
            + "\n".join(f"- <code>{item}</code>" for item in policy_error_preview)
        )

    await update.message.reply_text(
        (
            "ðŸ”’ <b>Admin Set Password Result</b>\n\n"
            f"Berhasil set: <b>{success}</b>\n"
            f"Invalid format: <b>{invalid_format}</b>\n"
            f"Invalid policy: <b>{invalid_policy}</b>\n"
            f"Total invalid: <b>{invalid_total}</b>\n\n"
            "Password berhasil diset akan menjadi <b>password default user</b> saat user menjalankan <b>Create Account</b>."
            f"{policy_preview_text}"
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("admin_panel"),
    )
    return True


async def _handle_user_domain_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not update.message or not update.effective_user:
        return False

    awaiting = context.chat_data.get("awaiting_input")
    if awaiting != "user_set_domain":
        return False

    raw_domain = (update.message.text or "").strip()
    if not raw_domain:
        await update.message.reply_text(
            "Input domain kosong. Kirim domain valid, contoh: <code>example.com</code>",
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("user_domain_menu"),
        )
        return True

    domain = normalize_domain(raw_domain)
    if not domain:
        await update.message.reply_text(
            (
                "âŒ Domain tidak valid.\n"
                "Contoh format benar: <code>example.com</code>\n"
                "Bisa kirim dengan/without https, nanti akan dinormalisasi."
            ),
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("user_domain_menu"),
        )
        return True

    set_user_custom_domain(update.effective_user.id, domain)
    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    await update.message.reply_text(
        (
            "âœ… <b>Custom domain tersimpan</b>\n\n"
            f"Domain: <code>{domain}</code>\n"
            "Domain ini akan diprioritaskan saat auto create."
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("user_domain_menu"),
    )
    return True


async def _handle_admin_domain_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> bool:
    if not update.message or not update.effective_user:
        return False

    awaiting = context.chat_data.get("awaiting_input")
    if awaiting not in {"admin_add_domain", "admin_remove_domain"}:
        return False

    if not _is_admin(update.effective_user.id, runtime):
        context.chat_data.pop("awaiting_input", None)
        context.chat_data.pop("prompt_msg_id", None)
        await update.message.reply_text("Akses admin ditolak.")
        return True

    lines = _split_lines(update.message.text or "")
    if not lines:
        hint = "/add_default_domain DOMAIN" if awaiting == "admin_add_domain" else "/remove_default_domain DOMAIN"
        await update.message.reply_text(
            (
                "Input kosong.\n"
                f"Gunakan format per baris atau command <code>{hint}</code>"
            ),
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("admin_domain_menu"),
        )
        return True

    if awaiting == "admin_add_domain":
        added, duplicate, invalid = add_domains(lines)
        result_text = (
            "ðŸŒ <b>Add Default Domain Result</b>\n\n"
            f"Berhasil tambah: <b>{added}</b>\n"
            f"Duplikat: <b>{duplicate}</b>\n"
            f"Invalid: <b>{invalid}</b>"
        )
    else:
        removed, failed = remove_domains(lines)
        result_text = (
            "ðŸŒ <b>Remove Default Domain Result</b>\n\n"
            f"Berhasil hapus: <b>{removed}</b>\n"
            f"Tidak ditemukan/invalid: <b>{failed}</b>"
        )

    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    domains = get_domains()
    await update.message.reply_text(
        (
            f"{result_text}\n\n"
            f"<b>Default Domain ({len(domains)}):</b>\n"
            f"{_domain_list_text(domains)}"
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("admin_domain_menu"),
    )
    return True


async def _handle_admin_vcc_stock_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> bool:
    if not update.message or not update.effective_user:
        return False

    awaiting = context.chat_data.get("awaiting_input")
    if awaiting != "admin_vcc_stock_add":
        return False

    if not _is_admin(update.effective_user.id, runtime):
        context.chat_data.pop("awaiting_input", None)
        context.chat_data.pop("prompt_msg_id", None)
        await update.message.reply_text("Akses admin ditolak.")
        return True

    lines = _split_lines(update.message.text or "")
    if not lines:
        await update.message.reply_text(
            (
                "Input kosong.\n"
                "Kirim VCC per baris dengan format:\n"
                "<code>NomorKartu|MM|YY|CVV</code>"
            ),
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("admin_vcc_stock_menu"),
        )
        return True

    added, duplicate, invalid = add_stock_vccs(lines)
    stock_vccs = get_stock_vccs()

    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    await update.message.reply_text(
        (
            "💳 <b>VCC Store Update</b>\n\n"
            f"Berhasil tambah: <b>{added}</b>\n"
            f"Duplikat: <b>{duplicate}</b>\n"
            f"Invalid: <b>{invalid}</b>\n\n"
            f"<b>Total stok:</b> {len(stock_vccs)} VCC\n"
            f"{_vcc_list_text(stock_vccs)}"
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("admin_vcc_stock_menu"),
    )
    return True


async def _handle_vcc_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.effective_chat:
        return False

    awaiting = context.chat_data.get("awaiting_input")
    if awaiting not in {"vcc", "vcc_edit", "vcc_delete"}:
        return False

    chat_id = update.effective_chat.id
    lines = _split_lines(update.message.text or "")
    if not lines:
        await update.message.reply_text(
            "Input kosong. Kirim data sesuai format menu yang dipilih.",
            reply_markup=_menu_back_keyboard("vcc_menu"),
        )
        return True

    if awaiting == "vcc":
        added, duplicate, invalid = add_user_vccs(chat_id, lines)
        result_text = (
            "âž• <b>Add Vcc Result</b>\n\n"
            f"Berhasil tambah: <b>{added}</b>\n"
            f"Duplikat: <b>{duplicate}</b>\n"
            f"Invalid: <b>{invalid}</b>"
        )
    elif awaiting == "vcc_edit":
        edited, failed = edit_user_vccs(chat_id, lines)
        result_text = (
            "âœï¸ <b>Edit Vcc Result</b>\n\n"
            f"Berhasil edit: <b>{edited}</b>\n"
            f"Gagal/invalid: <b>{failed}</b>"
        )
    else:
        deleted, failed = delete_user_vccs(chat_id, lines)
        result_text = (
            "ðŸ—‘ <b>Delete Vcc Result</b>\n\n"
            f"Berhasil hapus: <b>{deleted}</b>\n"
            f"Tidak ditemukan/invalid: <b>{failed}</b>"
        )

    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    total_now = len(get_user_vccs(chat_id))
    await update.message.reply_text(
        f"{result_text}\n\nTotal tersimpan sekarang: <b>{total_now}</b> VCC",
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("vcc_menu"),
    )
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.message or not update.effective_user:
        return
    is_admin, has_credits = _resolve_user_flags(update.effective_user.id, runtime)
    await update.message.reply_text(
        _build_start_overview(update, runtime),
        reply_markup=_start_keyboard(is_admin=is_admin, has_credits=has_credits),
    )


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.message:
        return

    raw = _extract_command_payload(update.message.text or "")
    if not raw:
        await update.message.reply_text(
            _redeem_instruction_text(),
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    code = _extract_voucher_code(raw)
    if not code:
        await update.message.reply_text(
            "Format kode tidak valid. Contoh benar: <code>VC-ABCDEFG123</code>",
            parse_mode="HTML",
            reply_markup=_back_keyboard(),
        )
        return

    await _handle_redeem_code(update, code, runtime)


async def add_vcc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    raw = _extract_command_payload(update.message.text or "")
    if not raw:
        await update.message.reply_text(
            (
                "Format add VCC:\n"
                "<code>/add_vcc NomorKartu|MM|YY|CVV</code>\n"
                "Bisa multi-line untuk bulk."
            ),
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("vcc_menu"),
        )
        return

    lines = _split_lines(raw)
    added, duplicate, invalid = add_user_vccs(update.effective_chat.id, lines)
    total_now = len(get_user_vccs(update.effective_chat.id))
    await update.message.reply_text(
        (
            "âž• <b>Add Vcc Result</b>\n\n"
            f"Berhasil tambah: <b>{added}</b>\n"
            f"Duplikat: <b>{duplicate}</b>\n"
            f"Invalid: <b>{invalid}</b>\n\n"
            f"Total tersimpan sekarang: <b>{total_now}</b> VCC"
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("vcc_menu"),
    )


async def add_default_domain_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> None:
    if not update.message or not update.effective_user:
        return
    if not _is_admin(update.effective_user.id, runtime):
        await update.message.reply_text("Akses admin ditolak.")
        return

    payload = _extract_command_payload(update.message.text or "")
    if not payload:
        await update.message.reply_text(
            "Format: <code>/add_default_domain DOMAIN</code>",
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("admin_domain_menu"),
        )
        return

    lines = _split_lines(payload)
    added, duplicate, invalid = add_domains(lines)
    domains = get_domains()
    await update.message.reply_text(
        (
            "ðŸŒ <b>Add Default Domain Result</b>\n\n"
            f"Berhasil tambah: <b>{added}</b>\n"
            f"Duplikat: <b>{duplicate}</b>\n"
            f"Invalid: <b>{invalid}</b>\n\n"
            f"<b>Default Domain ({len(domains)}):</b>\n"
            f"{_domain_list_text(domains)}"
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("admin_domain_menu"),
    )


async def remove_default_domain_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> None:
    if not update.message or not update.effective_user:
        return
    if not _is_admin(update.effective_user.id, runtime):
        await update.message.reply_text("Akses admin ditolak.")
        return

    payload = _extract_command_payload(update.message.text or "")
    if not payload:
        await update.message.reply_text(
            "Format: <code>/remove_default_domain DOMAIN</code>",
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("admin_domain_menu"),
        )
        return

    lines = _split_lines(payload)
    removed, failed = remove_domains(lines)
    domains = get_domains()
    await update.message.reply_text(
        (
            "ðŸŒ <b>Remove Default Domain Result</b>\n\n"
            f"Berhasil hapus: <b>{removed}</b>\n"
            f"Tidak ditemukan/invalid: <b>{failed}</b>\n\n"
            f"<b>Default Domain ({len(domains)}):</b>\n"
            f"{_domain_list_text(domains)}"
        ),
        parse_mode="HTML",
        reply_markup=_menu_back_keyboard("admin_domain_menu"),
    )


async def text_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.message:
        return

    handled_user_password = await _handle_user_set_password_input(update, context)
    if handled_user_password:
        return

    handled_user_domain = await _handle_user_domain_input(update, context)
    if handled_user_domain:
        return

    handled_admin_password = await _handle_admin_set_password_input(update, context, runtime)
    if handled_admin_password:
        return

    handled_admin_domain = await _handle_admin_domain_input(update, context, runtime)
    if handled_admin_domain:
        return

    handled_admin_vcc_stock = await _handle_admin_vcc_stock_input(update, context, runtime)
    if handled_admin_vcc_stock:
        return

    handled_create_custom_qty = await _handle_create_account_custom_qty_input(update, context, runtime)
    if handled_create_custom_qty:
        return

    handled_vcc = await _handle_vcc_input(update, context)
    if handled_vcc:
        return

    code = _extract_voucher_code(update.message.text or "")
    if code:
        await _handle_redeem_code(update, code, runtime)


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return

    url = context.args[0].strip() if context.args else "https://example.com"
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("URL harus dimulai dengan http:// atau https://")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    job = runtime.jobs.add_job(user_id=user_id, url=url)
    if not job:
        active_job = runtime.jobs.get_active_job_for_user(user_id)
        if active_job:
            await update.message.reply_text(
                (
                    "Kamu masih punya job aktif.\n"
                    f"Job aktif: <code>{active_job.job_id}</code>\n"
                    f"Status: <b>{active_job.status}</b>\n\n"
                    "Tunggu sampai selesai dulu sebelum request job baru."
                ),
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("Masih ada job aktif. Tunggu sampai selesai dulu.")
        return

    await update.message.reply_text(
        f"Job diterima: {job.job_id}\n"
        "Diproses di background thread. User lain tetap bisa diproses tanpa menunggu job ini selesai."
    )

    loop = asyncio.get_running_loop()
    runtime.executor.submit(
        process_selenium_job,
        loop,
        context.application,
        runtime,
        chat_id,
        user_id,
        job.job_id,
        url,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Gunakan: /status <job_id>")
        return

    job_id = context.args[0].strip()
    job = runtime.jobs.get_job(job_id)
    if not job:
        await update.message.reply_text(f"Job {job_id} tidak ditemukan.")
        return

    response = f"Job {job.job_id}\nStatus: {job.status}\nURL: {job.url}"
    if job.error:
        response += f"\nError: {job.error}"
    await update.message.reply_text(response)


async def back_to_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.callback_query or not update.effective_user:
        return
    query = update.callback_query
    await query.answer()

    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    is_admin, has_credits = _resolve_user_flags(update.effective_user.id, runtime)
    await _safe_edit_plain_menu(
        query,
        _build_start_overview(update, runtime),
        _start_keyboard(is_admin=is_admin, has_credits=has_credits),
    )


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.callback_query or not update.effective_user:
        return
    query = update.callback_query

    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return

    await query.answer()
    await _safe_edit_menu(
        query,
        "ðŸ›  <b>Admin Panel</b>\n\nPilih aksi admin:",
        _admin_keyboard(),
    )


async def set_password_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()

    if not query.message:
        return

    current_password = get_user_password(update.effective_user.id)
    is_set = bool(current_password.strip())
    status_text = "sudah diset" if is_set else "belum diset"
    preview_password = (
        f"<tg-spoiler>{html.escape(current_password)}</tg-spoiler>"
        if is_set
        else "-"
    )

    context.chat_data["awaiting_input"] = "user_set_password"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    caption_text = (
        "ðŸ”’ <b>Set Password Auto Create</b>\n\n"
        f"Status saat ini: <b>{status_text}</b>\n"
        f"Preview: {preview_password}\n\n"
        "Kirim password baru kamu di chat ini.\n"
        "Password ini akan dipakai untuk flow auto create.\n\n"
        f"{_password_requirements_text()}"
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("back_to_start"),
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("back_to_start"),
            )
    except BadRequest:
        pass


async def admin_set_password_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return

    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return

    await query.answer()
    if not query.message:
        return

    context.chat_data["awaiting_input"] = "admin_set_password"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    caption_text = (
        "ðŸ”’ <b>Admin Set Password (Bulk)</b>\n\n"
        "Fungsi: set <b>password default user</b> untuk flow <b>Create Account</b>.\n\n"
        "Kirim data dengan format:\n"
        "<code>USER_ID|PASSWORD</code>\n\n"
        "Bisa banyak baris (bulk).\n"
        "Contoh:\n"
        "<code>123456789|PasswordBaru123!</code>\n\n"
        f"{_password_requirements_text()}"
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("admin_panel"),
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("admin_panel"),
            )
    except BadRequest:
        pass


async def admin_vcc_stock_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return
    await query.answer()

    stock_vccs = get_stock_vccs()
    caption_text = (
        "💳 <b>Admin VCC Store</b>\n\n"
        f"<b>Total stok:</b> {len(stock_vccs)} VCC\n"
        f"{_vcc_list_text(stock_vccs)}\n\n"
        "Pilih aksi:"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add Stok VCC", callback_data="admin_vcc_stock_add")],
            [InlineKeyboardButton("🏠 Home", callback_data="back_to_start")],
        ]
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    except BadRequest:
        pass


async def admin_vcc_stock_add_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return
    await query.answer()

    context.chat_data["awaiting_input"] = "admin_vcc_stock_add"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    stock_count = get_stock_count()
    caption_text = (
        "💳 <b>Add Stok VCC Store</b>\n\n"
        f"<b>Total stok saat ini:</b> {stock_count} VCC\n\n"
        "Kirim VCC per baris dengan format:\n"
        "<code>NomorKartu|MM|YY|CVV</code>\n\n"
        "Bisa banyak baris (bulk)."
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("admin_vcc_stock_menu"),
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("admin_vcc_stock_menu"),
            )
    except BadRequest:
        pass


async def user_domain_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    await query.answer()

    await _safe_edit_menu(
        query,
        _build_user_domain_summary(update.effective_user.id),
        _user_domain_keyboard(),
    )


async def user_set_domain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    await query.answer()

    context.chat_data["awaiting_input"] = "user_set_domain"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    caption_text = (
        "ðŸŒ <b>Set Custom Domain</b>\n\n"
        "Kirim domain custom kamu di chat ini.\n"
        "Contoh: <code>example.com</code>\n\n"
        "Domain custom akan diprioritaskan dari default domain admin."
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("user_domain_menu"),
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("user_domain_menu"),
            )
    except BadRequest:
        pass


async def user_clear_domain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    await query.answer()

    set_user_custom_domain(update.effective_user.id, "")
    context.chat_data.pop("awaiting_input", None)
    context.chat_data.pop("prompt_msg_id", None)

    await _safe_edit_menu(
        query,
        (
            "ðŸ§¹ <b>Custom domain dihapus</b>\n\n"
            "Sekarang sistem akan pakai default domain dari admin (jika tersedia).\n\n"
            f"{_build_user_domain_summary(update.effective_user.id)}"
        ),
        _user_domain_keyboard(),
    )


async def admin_domain_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return
    await query.answer()

    domains = get_domains()
    list_text = _domain_list_text(domains)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("âž• Add Domain", callback_data="admin_add_domain"),
                InlineKeyboardButton("ðŸ—‘ Remove Domain", callback_data="admin_remove_domain"),
            ],
            [InlineKeyboardButton("ðŸ  Home", callback_data="back_to_start")],
        ]
    )

    caption_text = (
        "ðŸŒ <b>Admin Default Domain Menu</b>\n\n"
        f"<b>Default Domain ({len(domains)}):</b>\n"
        f"{list_text}\n\n"
        "Default domain ini dipakai jika user belum set custom domain."
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    except BadRequest:
        pass


async def admin_add_domain_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return
    await query.answer()

    context.chat_data["awaiting_input"] = "admin_add_domain"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    domains = get_domains()
    d_list = _domain_list_text(domains)
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=(
                    "ðŸŒ <b>Add Default Domain</b>\n\n"
                    f"<b>Default Domain ({len(domains)}):</b>\n"
                    f"{d_list}\n\n"
                    "Kirim domain baru (boleh banyak baris):\n"
                    "<code>/add_default_domain DOMAIN</code>"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("â†© Back", callback_data="admin_domain_menu")]]
                ),
            )
        else:
            await query.edit_message_text(
                text=(
                    "ðŸŒ <b>Add Default Domain</b>\n\n"
                    f"<b>Default Domain ({len(domains)}):</b>\n"
                    f"{d_list}\n\n"
                    "Kirim domain baru (boleh banyak baris):\n"
                    "<code>/add_default_domain DOMAIN</code>"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("â†© Back", callback_data="admin_domain_menu")]]
                ),
            )
    except BadRequest:
        pass


async def admin_remove_domain_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return
    await query.answer()

    context.chat_data["awaiting_input"] = "admin_remove_domain"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    domains = get_domains()
    d_list = _domain_list_text(domains)
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=(
                    "ðŸŒ <b>Remove Default Domain</b>\n\n"
                    f"<b>Default Domain ({len(domains)}):</b>\n"
                    f"{d_list}\n\n"
                    "Kirim domain yang ingin dihapus (boleh banyak baris):\n"
                    "<code>/remove_default_domain DOMAIN</code>"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("â†© Back", callback_data="admin_domain_menu")]]
                ),
            )
        else:
            await query.edit_message_text(
                text=(
                    "ðŸŒ <b>Remove Default Domain</b>\n\n"
                    f"<b>Default Domain ({len(domains)}):</b>\n"
                    f"{d_list}\n\n"
                    "Kirim domain yang ingin dihapus (boleh banyak baris):\n"
                    "<code>/remove_default_domain DOMAIN</code>"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("â†© Back", callback_data="admin_domain_menu")]]
                ),
            )
    except BadRequest:
        pass


async def redeem_voucher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(
                caption=_redeem_instruction_text(),
                parse_mode="HTML",
                reply_markup=_back_keyboard(),
            )
        else:
            await query.edit_message_text(
                text=_redeem_instruction_text(),
                parse_mode="HTML",
                reply_markup=_back_keyboard(),
            )
    except BadRequest:
        pass


async def vcc_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    chat_id = query.message.chat.id
    vccs = get_user_vccs(chat_id)
    caption_text = (
        "ðŸ’³ <b>Vcc Menu</b>\n\n"
        f"Total tersimpan: <b>{len(vccs)}</b> VCC\n\n"
        "Pilih aksi VCC:"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("âž• Add VCC", callback_data="vcc_add"),
            InlineKeyboardButton("âœï¸ Edit VCC", callback_data="vcc_edit"),
        ],
        [InlineKeyboardButton("ðŸ—‘ Delete VCC", callback_data="vcc_delete")],
        [InlineKeyboardButton("ðŸ  Home", callback_data="back_to_start")],
    ])
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=caption_text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await query.edit_message_text(text=caption_text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest:
        pass


async def vcc_add_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    context.chat_data["awaiting_input"] = "vcc"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    chat_id = query.message.chat.id
    vccs = get_user_vccs(chat_id)

    caption_text = (
        "âž• <b>Add Vcc</b>\n\n"
        f"<b>Total tersimpan:</b> {len(vccs)} VCC\n\n"
        "Kirimkan VCC baru kamu di sini.\n"
        "Format: <code>NomorKartu|MM|YY|CVV</code>\n"
        "<b>Atau kirim <code>/add_vcc VCC_CODE</code></b>\n\n"
        "Bisa banyak baris (bulk)."
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("vcc_menu"),
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("vcc_menu"),
            )
    except BadRequest:
        pass


async def vcc_edit_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    context.chat_data["awaiting_input"] = "vcc_edit"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    chat_id = query.message.chat.id
    vccs = get_user_vccs(chat_id)
    vcc_preview = "\n".join(f"  {i + 1}. <code>{v}</code>" for i, v in enumerate(vccs[:10])) if vccs else "  Belum ada"

    caption_text = (
        "âœï¸ <b>Edit Vcc</b>\n\n"
        f"<b>Total tersimpan:</b> {len(vccs)} VCC\n"
        f"{vcc_preview}\n\n"
        "Format edit:\n"
        "<code>VCC_LAMA => VCC_BARU</code>\n"
        "atau\n"
        "<code>old_card|old_mm|old_yy|old_cvv|new_card|new_mm|new_yy|new_cvv</code>\n\n"
        "Bisa banyak baris (bulk)."
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("vcc_menu"),
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("vcc_menu"),
            )
    except BadRequest:
        pass


async def vcc_delete_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    context.chat_data["awaiting_input"] = "vcc_delete"
    context.chat_data["prompt_msg_id"] = query.message.message_id

    chat_id = query.message.chat.id
    vccs = get_user_vccs(chat_id)
    vcc_preview = "\n".join(f"  {i + 1}. <code>{v}</code>" for i, v in enumerate(vccs[:10])) if vccs else "  Belum ada"

    caption_text = (
        "ðŸ—‘ <b>Delete Vcc</b>\n\n"
        f"<b>Total tersimpan:</b> {len(vccs)} VCC\n"
        f"{vcc_preview}\n\n"
        "Kirimkan VCC yang ingin dihapus (boleh banyak baris)."
    )
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("vcc_menu"),
            )
        else:
            await query.edit_message_text(
                text=caption_text,
                parse_mode="HTML",
                reply_markup=_menu_back_keyboard("vcc_menu"),
            )
    except BadRequest:
        pass


async def gen_voucher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    """Step 1: pick credit count per voucher."""
    if not update.callback_query or not update.effective_user:
        return

    query = update.callback_query
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return

    await query.answer()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("5 Credits", callback_data="gen_v_5"),
                InlineKeyboardButton("10 Credits", callback_data="gen_v_10"),
            ],
            [
                InlineKeyboardButton("25 Credits", callback_data="gen_v_25"),
                InlineKeyboardButton("50 Credits", callback_data="gen_v_50"),
            ],
            [InlineKeyboardButton("â†© Back", callback_data="admin_panel")],
        ]
    )
    await _safe_edit_menu(
        query,
        "ðŸŽŸï¸ <b>Generate Voucher</b>\n\n<b>Step 1:</b> Pilih nominal Credits per voucher:",
        keyboard,
    )


async def gen_voucher_qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    """Step 2: pick how many vouchers to create."""
    if not update.callback_query or not update.effective_user:
        return

    query = update.callback_query
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split("_")
    if len(parts) != 3 or not parts[-1].isdigit():
        await query.answer("Format nominal tidak valid.", show_alert=True)
        return

    await query.answer()
    acct_count = int(parts[-1])
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1", callback_data=f"gen_vq_{acct_count}_1"),
                InlineKeyboardButton("3", callback_data=f"gen_vq_{acct_count}_3"),
                InlineKeyboardButton("5", callback_data=f"gen_vq_{acct_count}_5"),
            ],
            [
                InlineKeyboardButton("10", callback_data=f"gen_vq_{acct_count}_10"),
                InlineKeyboardButton("20", callback_data=f"gen_vq_{acct_count}_20"),
                InlineKeyboardButton("50", callback_data=f"gen_vq_{acct_count}_50"),
            ],
            [InlineKeyboardButton("â†© Back", callback_data="gen_voucher")],
        ]
    )
    await _safe_edit_menu(
        query,
        (
            "ðŸŽŸï¸ <b>Generate Voucher</b>\n\n"
            f"Nominal: <b>{acct_count} Credits</b> per voucher\n\n"
            "<b>Step 2:</b> Mau buat berapa voucher?"
        ),
        keyboard,
    )


async def gen_voucher_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    """Step 3: generate voucher codes and store them."""
    if not update.callback_query or not update.effective_user:
        return

    query = update.callback_query
    if not _is_admin(update.effective_user.id, runtime):
        await query.answer("Akses admin ditolak.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split("_")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await query.answer("Format generate voucher tidak valid.", show_alert=True)
        return

    credits = int(parts[2])
    qty = int(parts[3])
    if qty > 200:
        await query.answer("Maksimum 200 voucher per generate.", show_alert=True)
        return

    await query.answer()
    try:
        codes = runtime.vouchers.create_vouchers(
            credits=credits,
            qty=qty,
            created_by=update.effective_user.id,
        )
    except Exception as exc:
        await _safe_edit_menu(
            query,
            (
                "ðŸŽŸï¸ <b>Generate Voucher</b>\n\n"
                "<b>Status:</b> Gagal generate voucher.\n"
                f"<b>Error:</b> <code>{exc}</code>"
            ),
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Coba Lagi", callback_data="gen_voucher")],
                    [InlineKeyboardButton("â†© Back", callback_data="admin_panel")],
                ]
            ),
        )
        return

    preview_limit = 30
    code_lines = "\n".join([f"<code>{code}</code>" for code in codes[:preview_limit]])
    hidden_count = len(codes) - preview_limit
    overflow_note = ""
    if hidden_count > 0:
        overflow_note = f"\n... dan <b>{hidden_count}</b> kode lain tersimpan di database."

    await _safe_edit_menu(
        query,
        (
            "ðŸŽŸï¸ <b>Voucher Berhasil Dibuat</b>\n\n"
            f"Nominal: <b>{credits} Credits</b>\n"
            f"Jumlah: <b>{qty}</b>\n"
            f"DB: <code>{runtime.settings.voucher_db_path}</code>\n\n"
            "<b>Kode Voucher:</b>\n"
            f"{code_lines}{overflow_note}"
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("âž• Generate Lagi", callback_data="gen_voucher")],
                [InlineKeyboardButton("â†© Back", callback_data="admin_panel")],
            ]
        ),
    )


async def _start_create_account_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
    *,
    use_store_vcc: bool,
    trial_days: int,
    account_qty: int,
) -> str:
    if not update.effective_user or not update.effective_chat:
        return "User/chat context tidak ditemukan."

    user_id = update.effective_user.id
    required_per_account = 2 if use_store_vcc else 1
    total_required_credits = required_per_account * account_qty
    mode_text = "VCC Store" if use_store_vcc else "VCC Pribadi"
    vcc_source = "store" if use_store_vcc else "personal"

    if not _has_minimum_credit(user_id, runtime, total_required_credits):
        balance = runtime.vouchers.get_balance(user_id)
        return (
            f"Credits tidak cukup untuk mode <b>{mode_text}</b>.\n"
            f"Biaya per akun: <b>{required_per_account}</b> Credits\n"
            f"Jumlah akun: <b>{account_qty}</b>\n"
            f"Total biaya: <b>{total_required_credits}</b> Credits\n"
            f"Saldo kamu: <b>{balance}</b>"
        )

    password = get_user_password(user_id).strip()
    if not password:
        return (
            "Password auto create belum diset.\n"
            "Klik tombol <b>Set Password</b> dulu, lalu kirim password kamu."
        )

    errors = validate_password_rules(password)
    if errors:
        return (
            "Password default kamu belum memenuhi ketentuan.\n"
            "Klik tombol <b>Set Password</b> lalu set ulang password.\n\n"
            "<b>Detail:</b>\n"
            f"{_password_error_lines(errors)}"
        )

    custom_domain, _, effective_domain = _resolve_effective_domain(user_id)
    if not effective_domain:
        return (
            "Domain belum tersedia untuk auto create.\n\n"
            "- Set custom domain di menu <b>Domain</b>\n"
            "- Atau minta admin set default domain di <b>Admin Panel</b>"
        )

    reserved_vccs: list[str] = []
    if use_store_vcc:
        reserved_vccs = pop_stock_vccs(account_qty)
        if len(reserved_vccs) < account_qty:
            # Safety rollback if partial data is returned by storage layer.
            if reserved_vccs:
                return_stock_vccs(reserved_vccs)
            stock_count = get_stock_count()
            return (
                "Stok <b>VCC Store</b> tidak cukup.\n"
                f"Kebutuhan: <b>{account_qty}</b> VCC\n"
                f"Stok tersedia: <b>{stock_count}</b> VCC\n\n"
                "Silakan minta admin tambah stok di <b>Admin Panel</b>."
            )
    else:
        reserved_vccs = pop_user_vccs(user_id, account_qty)
        if len(reserved_vccs) < account_qty:
            if reserved_vccs:
                return_user_vccs(user_id, reserved_vccs)
            vcc_count = len(get_user_vccs(user_id))
            return (
                "VCC pribadi kamu tidak cukup.\n"
                f"Kebutuhan: <b>{account_qty}</b> VCC\n"
                f"Tersimpan: <b>{vcc_count}</b> VCC\n\n"
                "Silakan tambah VCC dulu di menu <b>Vcc</b>."
            )

    signup_url = "https://zoom.us/signup"
    job = runtime.jobs.add_job(user_id=user_id, url=signup_url)
    if not job:
        rolled_back = (
            return_stock_vccs(reserved_vccs)
            if use_store_vcc
            else return_user_vccs(user_id, reserved_vccs)
        )
        rollback_line = f"VCC dikembalikan: <b>{rolled_back}</b>\n\n" if reserved_vccs else ""
        active_job = runtime.jobs.get_active_job_for_user(user_id)
        if active_job:
            return (
                "Kamu masih punya request create yang berjalan.\n"
                f"Job aktif: <code>{active_job.job_id}</code>\n"
                f"Status: <b>{active_job.status}</b>\n\n"
                f"{rollback_line}"
                "Tunggu proses selesai, lalu coba lagi."
            )
        return (
            "Masih ada job aktif. Tunggu proses selesai lalu coba lagi.\n\n"
            f"{rollback_line}".strip()
        )

    context.chat_data["create_account_vcc_mode"] = "vcc_store" if use_store_vcc else "vcc_personal"
    context.chat_data["create_account_trial_days"] = trial_days
    context.chat_data["create_account_qty"] = account_qty

    loop = asyncio.get_running_loop()
    try:
        runtime.executor.submit(
            process_selenium_job,
            loop,
            context.application,
            runtime,
            update.effective_chat.id,
            user_id,
            job.job_id,
            signup_url,
            account_qty,
            mode_text,
            trial_days,
            vcc_source,
            reserved_vccs,
        )
    except Exception as exc:
        runtime.jobs.update_job(job.job_id, status="failed", error=str(exc))
        rolled_back = (
            return_stock_vccs(reserved_vccs)
            if use_store_vcc
            else return_user_vccs(user_id, reserved_vccs)
        )
        return (
            "Gagal memulai job create.\n"
            f"Error: <code>{exc}</code>\n"
            f"VCC dikembalikan: <b>{rolled_back}</b>"
        )

    domain_source = "custom domain kamu" if custom_domain else "default domain admin"
    balance = runtime.vouchers.get_balance(user_id)
    mode_extra = f"VCC reserved di awal: <b>{len(reserved_vccs)}</b>\n"
    if use_store_vcc:
        mode_extra += f"Stok VCC Store setelah reserve: <b>{get_stock_count()}</b>\n"
    else:
        mode_extra += f"Sisa VCC pribadi setelah reserve: <b>{len(get_user_vccs(user_id))}</b>\n"

    return (
        "Create Account dipilih.\n"
        f"Mode: <b>{mode_text}</b>\n"
        f"Durasi trial: <b>{trial_days} Hari</b>\n"
        f"Jumlah akun: <b>{account_qty}</b>\n"
        f"Biaya per akun: <b>{required_per_account}</b> Credits\n"
        f"Total biaya: <b>{total_required_credits}</b> Credits\n"
        f"Saldo saat ini: <b>{balance}</b>\n"
        f"{mode_extra}"
        f"Domain aktif: <code>{effective_domain}</code>\n"
        f"Sumber domain: <b>{domain_source}</b>\n\n"
        f"Job ID: <code>{job.job_id}</code>\n"
        f"Start URL: <code>{signup_url}</code>\n"
        "Selenium sedang dijalankan di background thread dengan profile terpisah per request."
    )


async def _handle_create_account_custom_qty_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    runtime: Runtime,
) -> bool:
    if not update.message or not update.effective_user:
        return False

    awaiting = context.chat_data.get("awaiting_input")
    if awaiting != "create_account_qty_custom":
        return False

    payload = (update.message.text or "").strip()
    if not payload.isdigit():
        await update.message.reply_text(
            "Input harus angka. Kirim jumlah akun custom, contoh: <code>5</code>",
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("create_account"),
        )
        return True

    qty = int(payload)
    if qty <= 3:
        await update.message.reply_text(
            "Untuk 1-3 akun, gunakan tombol pilihan langsung. Custom dipakai jika > 3.",
            reply_markup=_menu_back_keyboard("create_account"),
        )
        return True
    if qty > 100:
        await update.message.reply_text(
            "Maksimum custom saat ini 100 akun per request.",
            reply_markup=_menu_back_keyboard("create_account"),
        )
        return True

    mode = str(context.chat_data.get("create_account_vcc_mode", "")).strip()
    trial_days = context.chat_data.get("create_account_trial_days")
    if mode not in {"vcc_store", "vcc_personal"} or not isinstance(trial_days, int):
        context.chat_data.pop("awaiting_input", None)
        _clear_create_account_flow(context)
        await update.message.reply_text(
            "State create account tidak lengkap. Mulai lagi dari tombol <b>Mulai Buat Akun</b>.",
            parse_mode="HTML",
            reply_markup=_menu_back_keyboard("back_to_start"),
        )
        return True

    context.chat_data.pop("awaiting_input", None)
    use_store_vcc = mode == "vcc_store"
    result_text = await _start_create_account_request(
        update,
        context,
        runtime,
        use_store_vcc=use_store_vcc,
        trial_days=trial_days,
        account_qty=qty,
    )
    _clear_create_account_flow(context)
    await update.message.reply_text(result_text, parse_mode="HTML")
    return True


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: Runtime) -> None:
    if not update.callback_query or not update.effective_user:
        return

    query = update.callback_query
    if not query.message:
        await query.answer("Message context tidak ditemukan.", show_alert=True)
        return

    data = query.data or ""
    user_id = update.effective_user.id
    _, has_credits = _resolve_user_flags(user_id, runtime)

    credit_required_actions = {
        "create_account",
        "create_account_vcc_store",
        "create_account_vcc_personal",
        "create_account_duration_7",
        "create_account_duration_14",
        "create_account_qty_1",
        "create_account_qty_2",
        "create_account_qty_3",
        "create_account_qty_custom",
        "schedule_meeting",
        "vcc_menu",
        "vcc_add",
        "vcc_edit",
        "vcc_delete",
        "set_password",
        "domain_menu",
        "user_domain_menu",
        "user_set_domain",
        "user_clear_domain",
    }
    if data in credit_required_actions and not has_credits:
        await query.answer("Credits tidak cukup.", show_alert=True)
        return

    if data == "back_to_start":
        await back_to_start_callback(update, context, runtime)
        return

    if data == "admin_panel":
        await admin_panel_callback(update, context, runtime)
        return

    if data == "set_password":
        await set_password_callback(update, context, runtime)
        return

    if data == "admin_set_password":
        await admin_set_password_callback(update, context, runtime)
        return

    if data == "admin_vcc_stock_menu":
        await admin_vcc_stock_menu_callback(update, context, runtime)
        return

    if data == "admin_vcc_stock_add":
        await admin_vcc_stock_add_callback(update, context, runtime)
        return

    if data == "domain_menu":
        if _is_admin(user_id, runtime):
            await admin_domain_menu_callback(update, context, runtime)
        else:
            await user_domain_menu_callback(update, context, runtime)
        return

    if data == "user_domain_menu":
        await user_domain_menu_callback(update, context, runtime)
        return

    if data == "user_set_domain":
        await user_set_domain_callback(update, context, runtime)
        return

    if data == "user_clear_domain":
        await user_clear_domain_callback(update, context, runtime)
        return

    if data == "admin_domain_menu":
        await admin_domain_menu_callback(update, context, runtime)
        return

    if data == "admin_add_domain":
        await admin_add_domain_callback(update, context, runtime)
        return

    if data == "admin_remove_domain":
        await admin_remove_domain_callback(update, context, runtime)
        return

    if data == "redeem_voucher":
        await redeem_voucher_callback(update, context, runtime)
        return

    if data == "vcc_menu":
        await vcc_menu_callback(update, context)
        return

    if data == "vcc_add":
        await vcc_add_menu_callback(update, context)
        return

    if data == "vcc_edit":
        await vcc_edit_menu_callback(update, context)
        return

    if data == "vcc_delete":
        await vcc_delete_menu_callback(update, context)
        return

    if data == "gen_voucher":
        await gen_voucher_callback(update, context, runtime)
        return

    if data.startswith("gen_vq_"):
        await gen_voucher_confirm_callback(update, context, runtime)
        return

    if data.startswith("gen_v_"):
        await gen_voucher_qty_callback(update, context, runtime)
        return

    if data == "create_account":
        if not has_credits:
            await query.answer("Credits tidak cukup.", show_alert=True)
            return
        context.chat_data.pop("awaiting_input", None)
        _clear_create_account_flow(context)
        await query.answer()
        await _safe_edit_menu(
            query,
            (
                "ðŸš€ <b>Pilih Sumber VCC</b>\n\n"
                "Pilih metode pembuatan akun:\n"
                "- Gunakan VCC Store: <b>2 Credits</b>\n"
                "- Gunakan VCC Pribadi: <b>1 Credit</b>"
            ),
            _create_account_source_keyboard(),
        )
        return

    if data in {"create_account_vcc_store", "create_account_vcc_personal"}:
        mode = "vcc_store" if data == "create_account_vcc_store" else "vcc_personal"
        mode_text = "VCC Store" if mode == "vcc_store" else "VCC Pribadi"
        context.chat_data["create_account_vcc_mode"] = mode
        context.chat_data.pop("create_account_trial_days", None)
        context.chat_data.pop("create_account_qty", None)
        await query.answer()
        await _safe_edit_menu(
            query,
            (
                "ðŸ—“ <b>Pilih Durasi Trial</b>\n\n"
                f"Mode terpilih: <b>{mode_text}</b>\n\n"
                "Step 1: Pilih durasi trial Zoom:"
            ),
            _create_account_duration_keyboard(),
        )
        return

    if data in {"create_account_duration_7", "create_account_duration_14"}:
        mode = str(context.chat_data.get("create_account_vcc_mode", "")).strip()
        if mode not in {"vcc_store", "vcc_personal"}:
            await query.answer("Pilih sumber VCC dulu.", show_alert=True)
            return
        trial_days = 7 if data.endswith("_7") else 14
        mode_text = "VCC Store" if mode == "vcc_store" else "VCC Pribadi"
        context.chat_data["create_account_trial_days"] = trial_days
        await query.answer()
        await _safe_edit_menu(
            query,
            (
                "ðŸ”¢ <b>Pilih Jumlah Akun</b>\n\n"
                f"Mode: <b>{mode_text}</b>\n"
                f"Durasi trial: <b>{trial_days} Hari</b>\n\n"
                "Step 2: Mau membuat berapa akun?"
            ),
            _create_account_qty_keyboard(),
        )
        return

    if data in {"create_account_qty_1", "create_account_qty_2", "create_account_qty_3"}:
        mode = str(context.chat_data.get("create_account_vcc_mode", "")).strip()
        trial_days = context.chat_data.get("create_account_trial_days")
        if mode not in {"vcc_store", "vcc_personal"} or not isinstance(trial_days, int):
            _clear_create_account_flow(context)
            await query.answer("State create belum lengkap. Ulangi dari awal.", show_alert=True)
            return
        account_qty = int(data.rsplit("_", 1)[1])
        await query.answer()
        result_text = await _start_create_account_request(
            update,
            context,
            runtime,
            use_store_vcc=(mode == "vcc_store"),
            trial_days=trial_days,
            account_qty=account_qty,
        )
        _clear_create_account_flow(context)
        await query.message.reply_text(result_text, parse_mode="HTML")
        return

    if data == "create_account_qty_custom":
        mode = str(context.chat_data.get("create_account_vcc_mode", "")).strip()
        trial_days = context.chat_data.get("create_account_trial_days")
        if mode not in {"vcc_store", "vcc_personal"} or not isinstance(trial_days, int):
            _clear_create_account_flow(context)
            await query.answer("State create belum lengkap. Ulangi dari awal.", show_alert=True)
            return
        context.chat_data["awaiting_input"] = "create_account_qty_custom"
        context.chat_data["prompt_msg_id"] = query.message.message_id
        await query.answer()
        await _safe_edit_menu(
            query,
            (
                "ðŸ”¢ <b>Jumlah Akun Custom</b>\n\n"
                "Kirim jumlah akun custom di chat ini.\n"
                "Ketentuan: angka bulat <b>> 3</b>."
            ),
            _menu_back_keyboard("create_account"),
        )
        return

    callback_messages = {
        "schedule_meeting": "Auto Invite dipilih. Handler belum dihubungkan.",
        "info": (
            "Info Bot:\n"
            "- /run <url> untuk eksekusi Selenium di thread\n"
            "- /status <job_id> untuk cek status job\n"
            "- /redeem KODE_VOUCHER untuk redeem credits\n"
            "- /add_vcc NomorKartu|MM|YY|CVV untuk simpan VCC\n"
            "- Set Password menyimpan password default untuk auto create\n"
            "- Menu Domain (start) untuk custom domain per-user\n"
            "- Admin bisa bulk set password user via Admin Panel\n"
            "- Admin kelola default domain di Admin Panel\n"
            "- Admin bisa tambah stok VCC Store di Admin Panel"
        ),
    }

    message = callback_messages.get(data)
    if not message:
        await query.answer()
        await query.message.reply_text(f"Action `{data}` belum dikenal.")
        return

    await query.answer()
    await query.message.reply_text(message)


def register_handlers(application: Application, runtime: Runtime) -> None:
    application.add_handler(CommandHandler("start", partial(start_command, runtime=runtime)))
    application.add_handler(CommandHandler("run", partial(run_command, runtime=runtime)))
    application.add_handler(CommandHandler("status", partial(status_command, runtime=runtime)))
    application.add_handler(CommandHandler("redeem", partial(redeem_command, runtime=runtime)))
    application.add_handler(CommandHandler("add_vcc", add_vcc_command))
    application.add_handler(CommandHandler("add_default_domain", partial(add_default_domain_command, runtime=runtime)))
    application.add_handler(
        CommandHandler("remove_default_domain", partial(remove_default_domain_command, runtime=runtime))
    )
    application.add_handler(CallbackQueryHandler(partial(menu_callback, runtime=runtime)))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            partial(text_message_router, runtime=runtime),
        )
    )


