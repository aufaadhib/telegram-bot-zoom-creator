import logging
from pathlib import Path

from telegram.ext import Application

from bot.handlers import register_handlers
from bot.runtime import build_runtime, shutdown_runtime
from utils.config import load_settings


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-selenium-bot")


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    settings = load_settings(base_dir)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum diisi di .env")

    runtime = build_runtime(settings)
    try:
        application = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .concurrent_updates(True)
            .build()
        )
        register_handlers(application, runtime)
        logger.info("Bot running. max_workers=%s", settings.max_workers)
        application.run_polling(drop_pending_updates=True)
    finally:
        shutdown_runtime(runtime)


if __name__ == "__main__":
    main()
