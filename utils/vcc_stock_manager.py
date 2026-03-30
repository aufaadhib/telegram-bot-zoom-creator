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
    return (BASE_DIR / os.getenv("VCC_STOCK_DB_PATH", "data/vcc_stock.json")).resolve()


def _ensure_db() -> None:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        _write_db({"vccs": []})


def _normalize_payload(data: dict[str, Any]) -> dict[str, Any]:
    vccs = data.get("vccs")
    if not isinstance(vccs, list):
        vccs = []
    clean = [str(item).strip() for item in vccs if isinstance(item, str) and str(item).strip()]
    return {"vccs": clean}


def _read_db() -> dict[str, Any]:
    _ensure_db()
    db_path = _get_db_path()
    with db_path.open("r", encoding="utf-8") as file:
        try:
            data = json.load(file)
        except json.JSONDecodeError:
            data = {"vccs": []}
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


def get_stock_vccs() -> list[str]:
    with _LOCK:
        payload = _read_db()
        return list(payload["vccs"])


def get_stock_count() -> int:
    with _LOCK:
        payload = _read_db()
        return len(payload["vccs"])


def pop_stock_vccs(qty: int) -> list[str]:
    target = int(qty)
    if target <= 0:
        return []

    with _LOCK:
        payload = _read_db()
        vccs: list[str] = list(payload["vccs"])
        if len(vccs) < target:
            return []
        reserved = vccs[:target]
        payload["vccs"] = vccs[target:]
        _write_db(payload)
        return reserved


def return_stock_vccs(vccs: list[str]) -> int:
    normalized: list[str] = []
    for raw in vccs:
        value = _normalize_vcc(raw)
        if value:
            normalized.append(value)

    if not normalized:
        return 0

    with _LOCK:
        payload = _read_db()
        current: list[str] = list(payload["vccs"])
        payload["vccs"] = current + normalized
        _write_db(payload)
        return len(normalized)


def add_stock_vccs(raw_lines: list[str]) -> tuple[int, int, int]:
    """
    Returns: (added_count, duplicate_count, invalid_count)
    """
    normalized: list[str] = []
    invalid_count = 0
    for line in raw_lines:
        value = _normalize_vcc(line)
        if not value:
            invalid_count += 1
            continue
        normalized.append(value)

    if not normalized:
        return 0, 0, invalid_count

    with _LOCK:
        payload = _read_db()
        vccs: list[str] = list(payload["vccs"])
        vccs.extend(normalized)
        payload["vccs"] = vccs
        _write_db(payload)
        return len(normalized), 0, invalid_count
