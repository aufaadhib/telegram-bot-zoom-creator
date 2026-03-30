from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()
_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)


def _get_db_path() -> Path:
    return (BASE_DIR / os.getenv("DOMAIN_DB_PATH", "data/domains.json")).resolve()


def _ensure_db() -> None:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        _write_db({"domains": []})


def _normalize_payload(data: dict[str, Any]) -> dict[str, Any]:
    domains = data.get("domains")
    if not isinstance(domains, list):
        domains = []
    return {"domains": [str(item).strip().lower() for item in domains if str(item).strip()]}


def _read_db() -> dict[str, Any]:
    _ensure_db()
    db_path = _get_db_path()
    with db_path.open("r", encoding="utf-8") as file:
        try:
            data = json.load(file)
        except json.JSONDecodeError:
            data = {"domains": []}
    return _normalize_payload(data)


def _write_db(payload: dict[str, Any]) -> None:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def _sanitize_domain(raw: str) -> str | None:
    value = (raw or "").strip().lower()
    if not value:
        return None

    if value.startswith(("http://", "https://")):
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    value = value.split("?", 1)[0]
    value = value.split("#", 1)[0]
    if ":" in value:
        value = value.split(":", 1)[0]
    value = value.strip(".")

    if not _DOMAIN_PATTERN.fullmatch(value):
        return None
    return value


def normalize_domain(raw: str) -> str | None:
    return _sanitize_domain(raw)


def get_domains() -> list[str]:
    with _LOCK:
        payload = _read_db()
        return list(payload["domains"])


def add_domains(raw_lines: list[str]) -> tuple[int, int, int]:
    """
    Returns: (added_count, duplicate_count, invalid_count)
    """
    normalized: list[str] = []
    invalid_count = 0
    for line in raw_lines:
        value = _sanitize_domain(line)
        if not value:
            invalid_count += 1
            continue
        normalized.append(value)

    if not normalized:
        return 0, 0, invalid_count

    with _LOCK:
        payload = _read_db()
        domains: list[str] = list(payload["domains"])
        existing = set(domains)
        added_count = 0
        duplicate_count = 0

        for value in normalized:
            if value in existing:
                duplicate_count += 1
                continue
            domains.append(value)
            existing.add(value)
            added_count += 1

        payload["domains"] = sorted(domains)
        _write_db(payload)
        return added_count, duplicate_count, invalid_count


def remove_domains(raw_lines: list[str]) -> tuple[int, int]:
    """
    Returns: (removed_count, not_found_or_invalid_count)
    """
    normalized: list[str] = []
    invalid_count = 0
    for line in raw_lines:
        value = _sanitize_domain(line)
        if not value:
            invalid_count += 1
            continue
        normalized.append(value)

    if not normalized:
        return 0, invalid_count

    remove_set = set(normalized)
    with _LOCK:
        payload = _read_db()
        domains: list[str] = list(payload["domains"])
        original_len = len(domains)
        domains = [item for item in domains if item not in remove_set]
        removed_count = original_len - len(domains)
        payload["domains"] = domains
        _write_db(payload)

    not_found = len(remove_set) - removed_count
    if not_found < 0:
        not_found = 0
    return removed_count, not_found + invalid_count
