from __future__ import annotations

import argparse
import asyncio
import logging
import re
import socket
import sys
from threading import Thread

import uvicorn

from funko_deal_bot.archive import write_source_archive
from funko_deal_bot.bot import build_telegram, request_bot_stop, run_telegram
from funko_deal_bot.config import load_settings
from funko_deal_bot.engine import Scanner
from funko_deal_bot.stopkey import install_stop_key
from funko_deal_bot.store import Store
from funko_deal_bot.web import create_app

log = logging.getLogger(__name__)
_TELEGRAM_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


class _RedactTelegramToken(logging.Filter):
    """httpx logs Bot API URLs; never keep the token in the console."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TELEGRAM_TOKEN_RE.sub("bot***", record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: _redact_token(value) for key, value in record.args.items()
                }
            else:
                record.args = tuple(_redact_token(arg) for arg in record.args)
        return True


def _redact_token(value: object) -> object:
    if isinstance(value, str):
        return _TELEGRAM_TOKEN_RE.sub("bot***", value)
    return value


def _ensure_single_instance(port: int) -> object | None:
    """Prevent two local bot instances from fighting over Telegram polling.

    On Windows use a named kernel mutex; on other platforms fall back to a small
    non-blocking TCP bind check on the dashboard port. The returned mutex handle
    is intentionally kept alive by the caller until process exit.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, True, "Global\\FinderFunko_SingleInstance")
            if not handle:
                return None
            if kernel32.GetLastError() == 183:
                log.error("Another FunkoDealBot instance is already running; exiting")
                kernel32.CloseHandle(handle)
                return None
            return handle
        except Exception:
            log.debug("Windows mutex unavailable; using port check", exc_info=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(port)))
    except OSError:
        sock.close()
        log.error("Dashboard port %s is already in use; another bot instance may be running", port)
        return None
    # Keep socket open as the process guard. Dashboard runs on 0.0.0.0 and will
    # then fail to bind, so use only as a preflight guard and close immediately.
    sock.close()
    return object()


def main() -> None:
    parser = argparse.ArgumentParser(description="Funko Pop eBay deal bot")
    parser.add_argument("--demo", action="store_true", help="Принудительно использовать демо-лоты")
    parser.add_argument("--once", action="store_true", help="Один скан и выход")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _redactor = _RedactTelegramToken()
    root_logger = logging.getLogger()
    root_logger.addFilter(_redactor)
    for _handler in root_logger.handlers:
        _handler.addFilter(_redactor)
    settings = load_settings()
    instance_guard = _ensure_single_instance(settings.port)
    if instance_guard is None:
        return
    if args.demo:
        settings.ebay_mode = "demo"

    _ = instance_guard
    archive = write_source_archive()
    log.info("Source archive: %s", archive)

    store = Store(settings.database_path)
    scanner = Scanner(settings, store)

    if args.once:
        result = scanner.demo_examples() if args.demo else scanner.scan()
        print(f"mode={result.mode} fetched={result.fetched} new={result.new_count} alerts={len(result.alerts)}")
        for alert in result.alerts:
            print(f"- {alert.kind}: {alert.listing.title} @ {alert.listing.price} ({alert.reason})")
        return

    app = create_app(settings, store, scanner)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)
    # Uvicorn stays on a daemon thread. Telegram+PTB keep the main thread/loop.
    # Blocking uvicorn.serve() on the same loop would freeze getMe/getUpdates.
    thread = Thread(target=server.run, daemon=True, name="dashboard")
    thread.start()
    log.info("Dashboard: http://127.0.0.1:%s (background / в фоне)", settings.port)

    interval = settings.poll_interval()
    log.info(
        "First eBay scan now (not waiting %ss) / Первый скан сразу, не жду %s сек",
        interval,
        interval,
    )
    Thread(target=scanner.run_loop, kwargs={"interval": interval}, daemon=True, name="ebay-scan").start()

    telegram = build_telegram(settings, store, scanner)
    holder: dict = {"app": telegram, "store": store}
    install_stop_key(
        on_stop=lambda: request_bot_stop(holder["app"], holder["store"]),
        on_window_close=lambda: request_bot_stop(holder["app"], holder["store"]),
    )
    if telegram:
        run_telegram(telegram)
    else:
        log.info("Telegram token missing; dashboard-only mode / нет токена")
        asyncio.run(_wait())


async def _wait() -> None:
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    main()
