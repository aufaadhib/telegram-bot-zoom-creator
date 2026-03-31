from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    max_workers: int
    max_drivers: int
    cost_per_account: int
    selenium_headless: bool
    selenium_auto_close: bool
    selenium_locale: str
    selenium_timezone: str
    selenium_wait_timeout: int
    selenium_window_size: str
    payment_retry_on_card_error: bool
    payment_max_card_attempts: int
    selenium_profile_dir: Path
    voucher_db_path: Path
    chromedriver_path: str
    chrome_binary: str
    admin_user_ids: set[int]
    credited_user_ids: set[int]


def _parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        if token.lstrip("-").isdigit():
            values.add(int(token))
    return values


def load_settings(base_dir: Path) -> Settings:
    load_dotenv()

    max_drivers = max(1, int(os.getenv("MAX_DRIVERS", "5")))
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        max_workers=int(os.getenv("MAX_WORKERS", str(max_drivers))),
        max_drivers=max_drivers,
        cost_per_account=int(os.getenv("COST_PER_ACCOUNT", "2")),
        selenium_headless=os.getenv("SELENIUM_HEADLESS", "false").lower() == "true",
        selenium_auto_close=os.getenv("SELENIUM_AUTO_CLOSE", "true").lower() == "true",
        selenium_locale=os.getenv("SELENIUM_LOCALE", "id-ID").strip(),
        selenium_timezone=os.getenv("SELENIUM_TIMEZONE", "Asia/Jakarta").strip(),
        selenium_wait_timeout=int(os.getenv("SELENIUM_WAIT_TIMEOUT", "20")),
        selenium_window_size=os.getenv("SELENIUM_WINDOW_SIZE", "1366,900").strip(),
        payment_retry_on_card_error=os.getenv("PAYMENT_RETRY_ON_CARD_ERROR", "false").lower() == "true",
        payment_max_card_attempts=max(1, int(os.getenv("PAYMENT_MAX_CARD_ATTEMPTS", "3"))),
        selenium_profile_dir=(base_dir / os.getenv("SELENIUM_PROFILE_DIR", "data/driver_profiles")).resolve(),
        voucher_db_path=(base_dir / os.getenv("VOUCHER_DB_PATH", "data/vouchers.json")).resolve(),
        chromedriver_path=os.getenv("CHROMEDRIVER_PATH", "").strip(),
        chrome_binary=os.getenv("CHROME_BINARY", "").strip(),
        admin_user_ids=_parse_int_set(os.getenv("ADMIN_USER_IDS", "")),
        credited_user_ids=_parse_int_set(os.getenv("CREDIT_USER_IDS", "")),
    )
