from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import string
import threading
from typing import Any


class VoucherStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._ensure_db()

    def _ensure_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._db_path.exists():
            self._write({"vouchers": [], "balances": {}})

    @staticmethod
    def _normalize_payload(data: dict[str, Any]) -> dict[str, Any]:
        vouchers = data.get("vouchers")
        balances = data.get("balances")
        if not isinstance(vouchers, list):
            vouchers = []
        if not isinstance(balances, dict):
            balances = {}
        return {"vouchers": vouchers, "balances": balances}

    def _read(self) -> dict[str, Any]:
        if not self._db_path.exists():
            return {"vouchers": [], "balances": {}}
        with self._db_path.open("r", encoding="utf-8") as file:
            try:
                data = json.load(file)
            except json.JSONDecodeError:
                data = {"vouchers": [], "balances": {}}
        return self._normalize_payload(data)

    def _write(self, payload: dict[str, Any]) -> None:
        with self._db_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)

    @staticmethod
    def _make_code() -> str:
        alphabet = string.ascii_uppercase + string.digits
        suffix = "".join(secrets.choice(alphabet) for _ in range(10))
        return f"VC-{suffix}"

    def create_vouchers(self, credits: int, qty: int, created_by: int) -> list[str]:
        if credits <= 0:
            raise ValueError("credits harus lebih dari 0.")
        if qty <= 0:
            raise ValueError("qty harus lebih dari 0.")

        with self._lock:
            payload = self._read()
            vouchers: list[dict[str, Any]] = payload["vouchers"]
            existing_codes = {item.get("code") for item in vouchers}
            created_codes: list[str] = []

            for _ in range(qty):
                code = self._make_code()
                while code in existing_codes:
                    code = self._make_code()

                now_iso = datetime.now(timezone.utc).isoformat()
                vouchers.append(
                    {
                        "code": code,
                        "credits": credits,
                        "is_used": False,
                        "created_at": now_iso,
                        "created_by": created_by,
                        "used_at": None,
                        "used_by": None,
                    }
                )
                existing_codes.add(code)
                created_codes.append(code)

            self._write(payload)
            return created_codes

    def get_balance(self, user_id: int) -> int:
        with self._lock:
            payload = self._read()
            balances: dict[str, Any] = payload["balances"]
            current = balances.get(str(user_id), 0)
            try:
                return int(current)
            except (TypeError, ValueError):
                return 0

    def redeem_voucher(self, code: str, user_id: int) -> tuple[int, int]:
        normalized_code = code.strip().upper()
        if not normalized_code:
            raise ValueError("Kode voucher kosong.")

        with self._lock:
            payload = self._read()
            vouchers: list[dict[str, Any]] = payload["vouchers"]
            balances: dict[str, Any] = payload["balances"]

            selected: dict[str, Any] | None = None
            for voucher in vouchers:
                if str(voucher.get("code", "")).upper() == normalized_code:
                    selected = voucher
                    break

            if not selected:
                raise ValueError("Kode voucher tidak ditemukan.")
            if selected.get("is_used"):
                raise ValueError("Kode voucher sudah dipakai.")

            credits_raw = selected.get("credits", 0)
            try:
                credits = int(credits_raw)
            except (TypeError, ValueError):
                raise ValueError("Nominal voucher tidak valid.") from None
            if credits <= 0:
                raise ValueError("Nominal voucher tidak valid.")

            selected["is_used"] = True
            selected["used_at"] = datetime.now(timezone.utc).isoformat()
            selected["used_by"] = user_id

            old_balance_raw = balances.get(str(user_id), 0)
            try:
                old_balance = int(old_balance_raw)
            except (TypeError, ValueError):
                old_balance = 0
            new_balance = old_balance + credits
            balances[str(user_id)] = new_balance

            self._write(payload)
            return credits, new_balance
