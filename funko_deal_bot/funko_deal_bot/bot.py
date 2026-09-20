from __future__ import annotations

import asyncio
import logging
import threading
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, InvalidToken, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from funko_deal_bot.access import command_chat_allowed, is_group_chat, is_private_chat, user_is_allowed
from funko_deal_bot.check import CHECK_USAGE, parse_check_item_id
from funko_deal_bot.config import Settings
from funko_deal_bot.engine import Scanner
from funko_deal_bot.messages import (
    BOT_DISABLED,
    BOT_ENABLED,
    PARSER_READY,
    CHECK_LOOKING,
    format_alert,
    format_bro,
    format_shutdown_stats,
    format_check_reply,
    format_scan_reply,
    format_status,
)
from funko_deal_bot.store import Store
from funko_deal_bot.vision import ensure_vision_ready

log = logging.getLogger(__name__)

_disabled_lock = threading.Lock()
_disabled_sent = False

DENIED = "нет доступа"
TELEGRAM_BOOTSTRAP_RETRIES = 5
TELEGRAM_INIT_TIMEOUT = 25.0
TELEGRAM_FLUSH_SECONDS = 15
TELEGRAM_FLUSH_FIRST = 2.0
VISION_STARTUP_TIMEOUT = 120.0

# Tuples — PTB set_my_commands accepts (command, description) without a Bot API type import.
PUBLIC_COMMANDS: list[tuple[str, str]] = [
    ("start", "Старт"),
    ("help", "Справка"),
    ("status", "Статус"),
    ("bro", "Статистика парсера"),
    ("check", "Проверить лот"),
    ("scan", "Проверить сейчас"),
    ("demo", "Демо-алерты"),
    ("bind", "Привязать группу"),
    ("unbind", "Отвязать группу"),
]

HELP = (
    "Смотрю новые лоты Funko / Funko Pop / «Funko Pop!» на eBay. "
    "IP не жжём: опрос раз в 2 мин (POLL_SECONDS 60–180), дома без прокси, "
    "сначала RSS/httpx напрямую, при 403 следующий источник (Funko, Finding, HTML), "
    "Playwright по умолчанию выключен (EBAY_SKIP_PLAYWRIGHT=1); если включите — 45 с, "
    "TimedOut не роняет цикл. Паузы 2–5 сек. "
    "PROXY_URL пустой (eBay с вашего IP). Telegram — TELEGRAM_PROXY "
    "socks5://127.0.0.1:10808 (v2rayN mixed, Tun выкл.).\n\n"
    "В алерте: Price / Shipping / Total. Сингл — имя+номер (Maximus #860) из названия "
    "и фото коробки, не поиск только по номеру; алерт от 20% дешевле "
    "1–3 самых дешёвых той же фигуры (сколько нашлось, BIN + доставка). "
    "0 компараблов — «не с чем сравнить» (авто-скан и /check) после поиска "
    "Funko {имя} {#}. Стоп окна: Ctrl+O (не Ctrl+C). "
    "Лот из N (2–6): N поисков Funko {имя с этой коробки} {Pop #} "
    "(лот из 2 → два запроса, из 3 → три; не фиксированная пара). "
    "Для каждого самый дешёвый сингл, сумма; алерт если лот на 20%+ дешевле. "
    "Имена не разобрал — молчу, без свалки каталога. "
    "Год SDCC 2024 / LE900 / #SE / 1500 Pcs — не лот. "
    "Доставка с лота: ZIP 19801 + Update (`_stpos`). Если не разобралась — Free ($0), Total = цена, всё равно −20%. Всегда цифры Price / Shipping / Total.\n\n"
    "Доступ у @givingbadpeoplegoodidea и @goitislav. "
    "Алерты — в личку каждого, кто написал /start, и в одну группу.\n"
    "@goitislav должен написать боту /start в личке один раз, иначе чата нет.\n\n"
    "Как проверить:\n"
    "/start — ЛС для алертов или привязать первую группу\n"
    "/status — как работает: проанализировано / прислано, −20% к cheapest или лот vs синглы\n"
    "/check ссылка — любой лот (и старый): выгода vs 1–3 cheapest, лот >$500 тоже\n"
    "Просто кинь ebay.com/itm/ — то же, что /check\n"
    "/scan — сразу отвечает; если скан уже идёт — «скан уже идёт», иначе результат позже\n"
    "/demo — примеры deal + bundle один раз (повтор — «уже показывал»)\n"
    "/help — эта справка\n\n"
    "Демо-лоты в джобе каждую минуту не шлём. Живые — только новые.\n\n"
    "Группа: добавь бота и напиши /start в ней с @givingbadpeoplegoodidea или @goitislav.\n"
    "/bind — то же, что /start в группе. /unbind — отвязать."
)

GROUP_BOUND = (
    "Группа привязана. Алерты будут сюда и в личку владельца. "
    "Другие группы игнорирую, пока не сделаешь /unbind."
)
GROUP_ALREADY = "Уже привязана другая группа. Напиши /unbind, если хочешь сменить."
DM_SAVED = "Личка сохранена. Алерты буду слать сюда (и в привязанную группу, если есть).\n\n"
UNBOUND = "Группа отвязана. Чтобы привязать снова, напиши /start в нужной группе."
NO_GROUP = "Привязанной группы нет."
WRONG_GROUP = "Эта группа не привязана. Команды работают только в ЛС и в одной группе после /start."
DEMO_ALREADY = "уже показывал эти демо-лоты. Живые лоты — только новые."
APOLOGY = (
    "извини, это были одни и те же демо-лоты каждую минуту; "
    "сейчас один раз и всё; живые лоты только новые."
)


def telegram_httpx_request(
    *,
    get_updates: bool = False,
    proxy: str | None = None,
) -> HTTPXRequest:
    """Bot API via TELEGRAM_PROXY only. Never PROXY_URL / HTTP_PROXY (eBay)."""
    socks = (proxy or "").strip() or None
    kwargs: dict = {
        "proxy": socks,
        "httpx_kwargs": {"trust_env": False},
        "connect_timeout": 12.0,
        "read_timeout": 25.0 if get_updates else 15.0,
        "write_timeout": 20.0,
        "pool_timeout": 5.0,
    }
    if get_updates:
        kwargs["connection_pool_size"] = 1
    return HTTPXRequest(**kwargs)


def is_telegram_transient(exc: BaseException) -> bool:
    if isinstance(exc, TimedOut):
        return True
    if isinstance(exc, NetworkError) and not isinstance(
        exc, (BadRequest, Forbidden, Conflict, InvalidToken)
    ):
        return True
    return False


async def _telegram_call(op, *, what: str, attempts: int = 8) -> None:
    delay = 1.0
    last: BaseException | None = None
    for attempt in range(max(1, attempts)):
        log.info("Telegram %s... / Telegram %s (попытка %s)", what, what, attempt + 1)
        try:
            await asyncio.wait_for(op(), timeout=TELEGRAM_INIT_TIMEOUT)
            log.info("Telegram %s OK", what)
            return
        except TimeoutError:
            last = TimedOut(f"{what} timeout {TELEGRAM_INIT_TIMEOUT:.0f}s")
            log.warning(
                "Telegram %s hung %.0fs — eBay scan keeps going / %s завис, скан не жду",
                what,
                TELEGRAM_INIT_TIMEOUT,
                what,
            )
        except (TimedOut, NetworkError) as exc:
            if not is_telegram_transient(exc):
                raise
            last = exc
            log.warning("Telegram %s: %s — retry in %.0fs (scheduler stays up)", what, exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
    log.warning("Telegram %s still failing after retries, continue: %s", what, last)


def run_telegram(app: Application) -> None:
    """Keep the process alive across getUpdates TimedOut / NetworkError.

    Do not assign Application.initialize (instance or class): PTB 22 slots make
    it read-only and raise AttributeError.
    """
    delay = 2.0
    while True:
        try:
            log.info("Telegram connecting... / Telegram подключаюсь (getMe)...")
            log.info("Telegram polling start (deleteWebhook + getUpdates) / опрос Telegram...")
            app.run_polling(
                drop_pending_updates=True,
                bootstrap_retries=TELEGRAM_BOOTSTRAP_RETRIES,
                close_loop=False,
                stop_signals=None,
            )
            return
        except (TimedOut, NetworkError) as exc:
            if not is_telegram_transient(exc):
                raise
            log.warning("Telegram polling crashed (%s); retry in %.0fs", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)


def _seed_owner_chat(settings: Settings, store: Store) -> None:
    """Seed DM and/or group from .env so «Бот включён» works on a fresh sqlite."""
    owner = str(settings.telegram_owner_chat_id or "").strip()
    chat = str(settings.telegram_chat_id or "").strip()
    for cid in (owner, chat):
        if cid and not cid.startswith("-"):
            store.set_owner_dm(cid)
            break
    group = chat if chat.startswith("-") else (owner if owner.startswith("-") else "")
    if group and not store.get_bound_group():
        store.set_bound_group(group)


async def _post_init(app: Application) -> None:
    bot = app.bot
    username = getattr(bot, "username", None) or "?"
    log.info("Telegram getMe OK @%s / Telegram подключен", username)
    try:
        app.bot_data["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass
    log.info("Telegram post_init: startup status + commands")
    store = app.bot_data.get("store")
    if isinstance(store, Store):
        await _notify_plain(bot, store, "🟡 Бот подключён. Запускаю парсер…")
        await _notify_plain(bot, store, "⚡ OCR: подготавливаю быстрый RapidOCR/ONNX Runtime…")
    await _telegram_call(lambda: bot.set_my_description(""), what="set_my_description")
    await _telegram_call(lambda: bot.set_my_short_description(""), what="set_my_short_description")
    await _telegram_call(
        lambda: bot.set_my_commands(list(PUBLIC_COMMANDS)),
        what="set_my_commands",
    )
    log.info("Public Telegram description cleared")
    if isinstance(store, Store):
        try:
            ready = await asyncio.wait_for(
                asyncio.to_thread(ensure_vision_ready),
                timeout=VISION_STARTUP_TIMEOUT,
            )
        except Exception:
            ready = False
            log.exception("Vision warmup failed during Telegram startup")
        await _notify_plain(
            bot,
            store,
            "✅ OCR: RapidOCR/ONNX Runtime готов. Фото распознаются в быстром режиме." if ready
            else "⚠️ OCR: RapidOCR/ONNX Runtime не загрузился. Проверь зависимости; медленный Florence fallback выключен.",
        )
        await _notify_plain(bot, store, PARSER_READY)
        await _notify_plain(bot, store, BOT_ENABLED)

    if isinstance(store, Store) and not store.get_meta("demo_spam_apology_sent"):

        for chat_id in store.alert_chat_ids():
            try:
                await bot.send_message(chat_id=chat_id, text=APOLOGY)
            except (TimedOut, NetworkError) as exc:
                log.warning("Failed to send apology to %s: %s", chat_id, exc)
            except Exception:
                log.exception("Failed to send apology to %s", chat_id)
        store.set_meta("demo_spam_apology_sent", "1")
    log.info("Telegram post_init done / startup statuses sent")


async def _post_stop(app: Application) -> None:
    store = app.bot_data.get("store")
    if isinstance(store, Store):
        await notify_bot_disabled(app, store)


async def _notify_plain(bot, store: Store, text: str) -> None:
    for chat_id in store.alert_chat_ids():
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception("Failed to notify %s", chat_id)


def _claim_disabled_send() -> bool:
    global _disabled_sent
    with _disabled_lock:
        if _disabled_sent:
            return False
        _disabled_sent = True
        return True


async def notify_bot_disabled(app: Application, store: Store) -> None:
    if not _claim_disabled_send():
        return
    await _notify_shutdown(app.bot, store)


async def _notify_shutdown(bot, store: Store) -> None:
    recipients = store.alert_chat_ids()
    for chat_id in recipients:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_shutdown_stats(None, store),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await bot.send_message(chat_id=chat_id, text=BOT_DISABLED)
        except Exception:
            log.exception("Failed to send shutdown stats to %s", chat_id)


def notify_bot_disabled_blocking(
    app: Application | None,
    store: Store | None,
    *,
    timeout: float = 20.0,
) -> None:
    """Best-effort «Бот выключен» from Ctrl+O / window close (sync thread)."""
    if app is None or store is None:
        return
    if not _claim_disabled_send():
        return

    async def _send() -> None:
        await _notify_shutdown(app.bot, store)

    loop = None
    try:
        loop = app.bot_data.get("loop")
    except Exception:
        loop = None
    try:
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=timeout)
            return
    except Exception:
        log.exception("BOT_DISABLED on running loop failed")
        return
    try:
        asyncio.run(_send())
    except Exception:
        log.exception("BOT_DISABLED asyncio.run failed")


def request_bot_stop(app: Application | None, store: Store | None) -> None:
    if app is not None:
        scanner = app.bot_data.get("scanner")
        if isinstance(scanner, Scanner):
            scanner.suppress_outbound()
        try:
            jobs = app.job_queue.jobs() if app.job_queue else []
            for job in list(jobs):
                if getattr(job, "name", None) == "alert-flush":
                    job.schedule_removal()
        except Exception:
            log.exception("alert-flush job removal failed")
    notify_bot_disabled_blocking(app, store)
    if app is None:
        return
    try:
        loop = app.bot_data.get("loop")
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
            log.info("Telegram loop stop requested from stop-key thread")
            return
        log.info("Telegram loop is not running; stop-key cleanup finished")
    except Exception:
        log.exception("Telegram loop stop request failed")


def build_telegram(settings: Settings, store: Store, scanner: Scanner) -> Application | None:
    token = settings.telegram_bot_token.strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN пустой — бот в Telegram не запустится")
        return None

    _seed_owner_chat(settings, store)

    # PROXY_URL must not touch Bot API. TELEGRAM_PROXY is SOCKS only (v2rayN).
    tg_proxy = settings.telegram_proxy.strip() or None
    app = (
        Application.builder()
        .token(token)
        .request(telegram_httpx_request(proxy=tg_proxy))
        .get_updates_request(telegram_httpx_request(get_updates=True, proxy=tg_proxy))
        .post_init(_post_init)
        .post_stop(_post_stop)
        .build()
    )
    app.bot_data["store"] = store
    app.bot_data["scanner"] = scanner

    async def acl_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user_is_allowed(user, settings):
            return
        if is_private_chat(chat) and user is not None:
            if store.first_denied_notice(user.id):
                message = update.effective_message
                if message:
                    await message.reply_text(DENIED)
        log.info("ACL drop user=%s chat=%s", getattr(user, "id", None), getattr(chat, "id", None))
        raise ApplicationHandlerStop

    def _command_name(update: Update) -> str:
        text = (update.effective_message.text or "") if update.effective_message else ""
        part = text.split()[0] if text else ""
        return part.lstrip("/").split("@")[0].lower()

    async def _guard_group(update: Update) -> bool:
        chat = update.effective_chat
        command = _command_name(update)
        if command_chat_allowed(chat, store.get_bound_group(), command):
            return True
        if update.message and is_group_chat(chat):
            await update.message.reply_text(WRONG_GROUP)
        return False

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not chat or not update.message:
            return
        if is_private_chat(chat):
            store.set_owner_dm(chat.id)
            await update.message.reply_text(DM_SAVED + HELP)
            return
        if is_group_chat(chat):
            bound = store.get_bound_group()
            if bound and bound != str(chat.id):
                await update.message.reply_text(GROUP_ALREADY)
                return
            store.set_bound_group(chat.id)
            await update.message.reply_text(GROUP_BOUND)
            return

    async def bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await start(update, context)

    async def unbind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        previous = store.clear_bound_group()
        await update.message.reply_text(UNBOUND if previous else NO_GROUP)

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard_group(update):
            return
        if update.message:
            await update.message.reply_text(HELP)

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard_group(update):
            return
        if update.message:
            await update.message.reply_text(format_status(settings, store))

    async def bro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard_group(update):
            return
        if update.message:
            await update.message.reply_text(
                format_bro(settings, store),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard_group(update):
            return
        if not update.message:
            return
        if scanner.scan_in_progress():
            await update.message.reply_text("скан уже идёт")
            return
        await update.message.reply_text("Проверяю eBay…")
        message = update.message

        async def _scan_and_reply() -> None:
            try:
                result = await asyncio.to_thread(scanner.scan)
                await _broadcast(app, store, result.alerts)
                if result.error == "скан уже идёт":
                    await message.reply_text("скан уже идёт")
                    return
                stats = store.stats()
                await message.reply_text(
                    format_scan_reply(
                        mode=result.mode,
                        new_count=result.new_count,
                        processed=result.processed_count,
                        alerts=len(result.alerts),
                        error=result.error,
                    )
                )
            except Exception:
                log.exception("Background /scan failed")
                try:
                    await message.reply_text("скан не удался")
                except Exception:
                    log.exception("Failed to report /scan error")

        asyncio.create_task(_scan_and_reply())

    async def demo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard_group(update):
            return
        result = scanner.demo_examples()
        if not result.alerts:
            if update.message:
                await update.message.reply_text(DEMO_ALREADY)
            return
        await _broadcast(app, store, result.alerts)
        if update.message:
            await update.message.reply_text(f"Демо: {len(result.alerts)} алерта. Повторно те же не шлю.")

    async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard_group(update):
            return
        if not update.message:
            return
        text = update.message.text or ""
        item_id = parse_check_item_id(text)
        if not item_id:
            await update.message.reply_text(CHECK_USAGE)
            return
        await _run_check(update, item_id)

    async def ebay_link_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard_group(update):
            return
        if not update.message:
            return
        text = update.message.text or ""
        item_id = parse_check_item_id(text, plain_message=True)
        if not item_id:
            return
        await _run_check(update, item_id)

    async def _run_check(update: Update, item_id: str) -> None:
        if not update.message:
            return
        placeholder = await update.message.reply_text(CHECK_LOOKING)
        chat_id = placeholder.chat_id
        message_id = placeholder.message_id
        bot = app.bot

        async def _check_and_edit() -> None:
            loop = asyncio.get_running_loop()
            edit_lock = asyncio.Lock()
            last = {"text": CHECK_LOOKING}

            async def _edit_plain(text: str) -> None:
                if not text or text == last["text"]:
                    return
                last["text"] = text
                async with edit_lock:
                    try:
                        await bot.edit_message_text(
                            text,
                            chat_id=chat_id,
                            message_id=message_id,
                        )
                    except BadRequest:
                        log.debug("Telegram status edit skipped: %s", text[:80])

            def on_status(text: str) -> None:
                asyncio.run_coroutine_threadsafe(_edit_plain(text), loop)

            try:
                result = await asyncio.to_thread(scanner.check_item, item_id, on_status)
                async with edit_lock:
                    await bot.edit_message_text(
                        format_check_reply(result),
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False,
                    )
            except Exception:
                log.exception("Background /check failed")
                try:
                    async with edit_lock:
                        await bot.edit_message_text(
                            "не разобрал лот",
                            chat_id=chat_id,
                            message_id=message_id,
                        )
                except Exception:
                    log.exception("Failed to report /check error")

        asyncio.create_task(_check_and_edit())

    async def job_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
        # Scans run on a dedicated thread from main.py (first scan immediately).
        # This job only flushes alerts + heartbeat so getMe hang cannot delay eBay.
        if not scanner.outbound_enabled:
            scanner.drop_pending_alerts()
            return
        alerts = scanner.drain_alerts()
        if alerts:
            await _broadcast(app, store, alerts)
        log.info("Telegram heartbeat getUpdates / Telegram жив, жду апдейты")

    app.add_handler(TypeHandler(Update, acl_gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bind", bind))
    app.add_handler(CommandHandler("unbind", unbind))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("bro", bro))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("demo", demo_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ebay_link_check))

    async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if err is not None and is_telegram_transient(err):
            log.warning("Telegram network while polling (keep running): %s", err)
            return
        log.exception("Telegram handler error", exc_info=err)

    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(
            job_scan,
            interval=TELEGRAM_FLUSH_SECONDS,
            first=TELEGRAM_FLUSH_FIRST,
            name="alert-flush",
        )
    return app


async def _broadcast(app: Application, store: Store, alerts) -> None:
    scanner = app.bot_data.get("scanner")
    if isinstance(scanner, Scanner) and not scanner.outbound_enabled:
        return
    chats = store.alert_chat_ids()
    allow = set(chats)
    for alert in alerts:
        if isinstance(scanner, Scanner) and not scanner.outbound_enabled:
            return
        if getattr(alert, "kind", None) == "no_comps":
            continue
        text = format_alert(alert)
        for chat_id in chats:
            if str(chat_id) not in allow:
                continue
            if isinstance(scanner, Scanner) and not scanner.outbound_enabled:
                return
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
            except Exception:
                log.exception("Failed to send alert to %s", chat_id)
