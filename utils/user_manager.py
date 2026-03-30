from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()
_VCC_PATTERN = re.compile(r"^\s*(\d{12,19})\|(\d{1,2})\|(\d{2,4})\|(\d{3,4})\s*$")


def _get_db_path() -> Path:
    return (BASE_DIR / os.getenv("USER_DB_PATH", "data/users.json")).resolve()


def _ensure_db() -> None:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        _write_db({"users": {}})


def _normalize_payload(data: dict[str, Any]) -> dict[str, Any]:
    users = data.get("users")
    if not isinstance(users, dict):
        users = {}
    return {"users": users}


def _read_db() -> dict[str, Any]:
    _ensure_db()
    db_path = _get_db_path()
    with db_path.open("r", encoding="utf-8") as file:
        try:
            data = json.load(file)
        except json.JSONDecodeError:
            data = {"users": {}}
    return _normalize_payload(data)


def _write_db(payload: dict[str, Any]) -> None:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def _normalize_vcc(raw: str) -> str | None:
    match = _VCC_PATTERN.match(raw or "")
    if not match:
        return None
    card = match.group(1)
    month = int(match.group(2))
    if month < 1 or month > 12:
        return None
    yy = match.group(3)
    cvv = match.group(4)
    return f"{card}|{month:02d}|{yy}|{cvv}"


def _get_user_bucket(payload: dict[str, Any], chat_id: int) -> dict[str, Any]:
    users: dict[str, Any] = payload["users"]
    key = str(chat_id)
    bucket = users.get(key)
    if not isinstance(bucket, dict):
        bucket = {"vccs": []}
        users[key] = bucket
    vccs = bucket.get("vccs")
    if not isinstance(vccs, list):
        bucket["vccs"] = []
    password = bucket.get("password")
    if password is None:
        bucket["password"] = ""
    elif not isinstance(password, str):
        bucket["password"] = str(password)
    custom_domain = bucket.get("custom_domain")
    if custom_domain is None:
        bucket["custom_domain"] = ""
    elif not isinstance(custom_domain, str):
        bucket["custom_domain"] = str(custom_domain)
    account_count = bucket.get("account_count")
    if account_count is None:
        bucket["account_count"] = 0
    elif isinstance(account_count, (int, float)):
        bucket["account_count"] = int(account_count)
    else:
        value = str(account_count).strip()
        bucket["account_count"] = int(value) if value.isdigit() else 0
    return bucket


def _extract_account_count(bucket: dict[str, Any]) -> int:
    for key in ("account_count", "total_account", "total_accounts", "accounts_count"):
        value = bucket.get(key)
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())

    for key in ("accounts", "created_accounts", "zoom_accounts"):
        value = bucket.get(key)
        if isinstance(value, list):
            return len(value)

    return 0


def ensure_user_exists(chat_id: int) -> None:
    with _LOCK:
        payload = _read_db()
        _get_user_bucket(payload, chat_id)
        _write_db(payload)


def get_user_vccs(chat_id: int) -> list[str]:
    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        vccs = bucket.get("vccs", [])
        return [str(item) for item in vccs if isinstance(item, str)]


def pop_user_vccs(chat_id: int, qty: int) -> list[str]:
    target = int(qty)
    if target <= 0:
        return []

    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        vccs: list[str] = [str(item) for item in bucket.get("vccs", []) if isinstance(item, str)]
        if len(vccs) < target:
            return []
        reserved = vccs[:target]
        bucket["vccs"] = vccs[target:]
        _write_db(payload)
        return reserved


def return_user_vccs(chat_id: int, vccs: list[str]) -> int:
    normalized: list[str] = []
    for raw in vccs:
        value = _normalize_vcc(raw)
        if value:
            normalized.append(value)

    if not normalized:
        return 0

    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        current: list[str] = [str(item) for item in bucket.get("vccs", []) if isinstance(item, str)]
        bucket["vccs"] = current + normalized
        _write_db(payload)
        return len(normalized)


def get_user_account_count(chat_id: int) -> int:
    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        return _extract_account_count(bucket)


def get_global_account_count() -> int:
    with _LOCK:
        payload = _read_db()
        users = payload.get("users", {})
        if not isinstance(users, dict):
            return 0
        total = 0
        for value in users.values():
            if isinstance(value, dict):
                total += _extract_account_count(value)
        return total


def get_total_users() -> int:
    with _LOCK:
        payload = _read_db()
        users = payload.get("users", {})
        if not isinstance(users, dict):
            return 0
        return len(users)


def add_user_vccs(chat_id: int, raw_lines: list[str]) -> tuple[int, int, int]:
    """
    Returns: (added_count, duplicate_count, invalid_count)
    """
    normalized: list[str] = []
    invalid_count = 0
    for line in raw_lines:
        line = (line or "").strip()
        if not line:
            continue
        value = _normalize_vcc(line)
        if not value:
            invalid_count += 1
            continue
        normalized.append(value)

    if not normalized:
        return 0, 0, invalid_count

    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        vccs: list[str] = [str(item) for item in bucket.get("vccs", []) if isinstance(item, str)]
        vccs.extend(normalized)
        bucket["vccs"] = vccs
        _write_db(payload)
        return len(normalized), 0, invalid_count


def delete_user_vccs(chat_id: int, raw_lines: list[str]) -> tuple[int, int]:
    """
    Returns: (deleted_count, not_found_or_invalid_count)
    """
    normalized: list[str] = []
    invalid_count = 0
    for line in raw_lines:
        line = (line or "").strip()
        if not line:
            continue
        value = _normalize_vcc(line)
        if not value:
            invalid_count += 1
            continue
        normalized.append(value)

    if not normalized:
        return 0, invalid_count

    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        vccs: list[str] = [str(item) for item in bucket.get("vccs", []) if isinstance(item, str)]
        original_len = len(vccs)
        remove_set = set(normalized)
        vccs = [item for item in vccs if item not in remove_set]
        deleted = original_len - len(vccs)
        bucket["vccs"] = vccs
        _write_db(payload)

    not_found = len(remove_set) - deleted
    if not_found < 0:
        not_found = 0
    return deleted, not_found + invalid_count


def edit_user_vccs(chat_id: int, raw_lines: list[str]) -> tuple[int, int]:
    """
    Supported line formats:
    - OLD_VCC => NEW_VCC
    - old_card|old_mm|old_yy|old_cvv|new_card|new_mm|new_yy|new_cvv
    Returns: (edited_count, invalid_or_not_found_count)
    """
    pairs: list[tuple[str, str]] = []
    invalid_count = 0

    for raw in raw_lines:
        line = (raw or "").strip()
        if not line:
            continue

        if "=>" in line:
            left, right = [part.strip() for part in line.split("=>", 1)]
            old_vcc = _normalize_vcc(left)
            new_vcc = _normalize_vcc(right)
            if not old_vcc or not new_vcc:
                invalid_count += 1
                continue
            pairs.append((old_vcc, new_vcc))
            continue

        parts = [token.strip() for token in line.split("|")]
        if len(parts) == 8:
            old_vcc = _normalize_vcc("|".join(parts[:4]))
            new_vcc = _normalize_vcc("|".join(parts[4:]))
            if not old_vcc or not new_vcc:
                invalid_count += 1
                continue
            pairs.append((old_vcc, new_vcc))
            continue

        invalid_count += 1

    if not pairs:
        return 0, invalid_count

    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        vccs: list[str] = [str(item) for item in bucket.get("vccs", []) if isinstance(item, str)]
        edited = 0
        failed = 0
        for old_vcc, new_vcc in pairs:
            if old_vcc not in vccs:
                failed += 1
                continue
            if new_vcc != old_vcc and new_vcc in vccs:
                failed += 1
                continue
            idx = vccs.index(old_vcc)
            vccs[idx] = new_vcc
            edited += 1
        bucket["vccs"] = vccs
        _write_db(payload)

    return edited, failed + invalid_count


def set_user_password(chat_id: int, password: str) -> None:
    clean_password = (password or "").strip()
    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        bucket["password"] = clean_password
        _write_db(payload)


def get_user_password(chat_id: int) -> str:
    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        password = bucket.get("password", "")
        if isinstance(password, str):
            return password
        return str(password)


def set_user_custom_domain(chat_id: int, domain: str) -> None:
    clean_domain = (domain or "").strip().lower()
    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        bucket["custom_domain"] = clean_domain
        _write_db(payload)


def get_user_custom_domain(chat_id: int) -> str:
    with _LOCK:
        payload = _read_db()
        bucket = _get_user_bucket(payload, chat_id)
        custom_domain = bucket.get("custom_domain", "")
        if isinstance(custom_domain, str):
            return custom_domain.strip().lower()
        return str(custom_domain).strip().lower()
