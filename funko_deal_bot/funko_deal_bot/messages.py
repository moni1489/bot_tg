from __future__ import annotations

from funko_deal_bot.config import Settings
from funko_deal_bot.deals import listing_refs
from funko_deal_bot.models import SHIPPING_UNKNOWN, CheckResult, DealAlert
from funko_deal_bot.normalize import extract_bundle_count, is_multi_pop, shipping_label
from funko_deal_bot.store import Store

BOT_ENABLED = "🟢 Бот подключён, парсер готов"
PARSER_READY = "🔎 Парсер eBay готов. Жду новые лоты…"
BOT_DISABLED = "Бот выключен"
CHECK_LOOKING = "🔄 Подключаюсь к eBay и смотрю лот…"
CHECK_COMPARE = "📊 Сравниваю с найденными BIN…"

__all__ = [
    "BOT_DISABLED",
    "BOT_ENABLED",
    "PARSER_READY",
    "CHECK_COMPARE",
    "CHECK_LOOKING",
    "SHIPPING_UNKNOWN",
    "format_alert",
    "format_bro",
    "format_shutdown_stats",
    "format_check_bin_status",
    "format_check_vision_status",
    "format_check_parsed_status",
    "format_check_reply",
    "format_check_shipping_status",
    "format_scan_reply",
    "format_status",
]


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.2f}"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _shipping_line(listing) -> str:
    """Always a number. Missing ZIP rate is shown as Free after assume_free_shipping."""
    if getattr(listing, "listing", None) is not None and not hasattr(listing, "shipping_cost"):
        listing = listing.listing
    if listing.shipping_cost is None:
        return "Shipping: <b>—</b>"
    cost = float(listing.shipping_cost)
    label = (listing.shipping_label or shipping_label(cost) or "Free").strip()
    if cost == 0 or str(label).lower() in {"бесплатно", "free"}:
        return "Shipping: <b>Free</b>"
    return f"Shipping: <b>{label}</b>"


def _comp_link(url: str) -> str:
    href = (url or "").strip()
    if not href:
        return ""
    return f'<a href="{_esc(href)}">открыть</a>'


def _comp_price_ship_total(row: dict) -> str:
    price = row.get("price")
    ship = row.get("shipping")
    landed = row.get("cheapest", row.get("avg"))
    if price is None and ship is None:
        return ""
    if ship == 0:
        ship_s = "Free"
    elif isinstance(ship, (int, float)):
        ship_s = _money(ship)
    else:
        ship_s = "—"
    landed_s = _money(landed) if isinstance(landed, (int, float)) else "—"
    return f"Price {_money(price)} Shipping {ship_s} Total {landed_s}"


def _append_comp_row(bits: list[str], row: dict, *, headline: str) -> None:
    bits.append(headline)
    detail = _comp_price_ship_total(row)
    if detail:
        bits.append(detail)
    link = _comp_link(str(row.get("url") or ""))
    if link:
        bits.append(link)


def _display_name(listing) -> str:
    refs = listing_refs(listing)
    if len(refs) == 1:
        ref = refs[0]
        name = (getattr(listing, "pop_name", None) or ref.name or "").strip()
        number = getattr(listing, "pop_number", None) or ref.number
        if name and number:
            return f"{name} #{number}"
        if name:
            return name
        if number:
            return f"Funko Pop #{number}"
    if len(refs) >= 2:
        parts = []
        for ref in refs:
            name = (ref.name or "Funko Pop").strip()
            if ref.number:
                name += f" #{ref.number}"
            parts.append(name)
        return " + ".join(parts)
    return (listing.title or "eBay lot").strip()


def _deal_comp_block(alert: DealAlert) -> str:
    """Show up to three cheapest comps with clickable prices."""
    if not alert.top_comps:
        return ""
    links: list[str] = []
    for row in alert.top_comps[:3]:
        cost = row.get("cheapest", row.get("avg"))
        if not isinstance(cost, (int, float)):
            continue
        url = str(row.get("url") or "").strip()
        price = _money(cost)
        detail = _comp_price_ship_total(row)
        anchor = f'<a href="{_esc(url)}"><b>{price}</b></a>' if url else f"<b>{price}</b>"
        if detail:
            links.append(f"{anchor} ({_esc(detail)})")
        else:
            links.append(anchor)
    if not links:
        return ""
    return "\n📊 3 самых дешёвых: " + " · ".join(links)


def _bundle_value_block(alert: DealAlert) -> str:
    if not alert.top_comps:
        return ""
    lines = ["", "📊 Синглы:"]
    for row in alert.top_comps:
        name = str(row.get("name") or "Funko Pop").strip()
        pop = row.get("pop") or row.get("number")
        if pop:
            name += f" #{pop}"
        cost = row.get("cheapest", row.get("avg"))
        comp = _money(cost if isinstance(cost, (int, float)) else None)
        detail = _comp_price_ship_total(row)
        link = _comp_link(str(row.get("url") or ""))
        if detail:
            lines.append(f"• {_esc(name)} — <b>{comp}</b> ({_esc(detail)}) {link}".rstrip())
        else:
            lines.append(f"• {_esc(name)} — <b>{comp}</b> {link}".rstrip())
    total = alert.bundle_value_sum or alert.average_price
    if total is not None:
        lines.append(f"Стоимость по синглам: <b>{_money(total)}</b>")
    return "\n".join(lines)


def format_alert(alert: DealAlert) -> str:
    listing = alert.listing
    title = _esc(_display_name(listing))
    price = _money(listing.price)
    landed = _money(listing.landed_cost())
    ship = _shipping_line(listing)

    if alert.kind == "no_comps":
        return (
            "ℹ️ <b>Нет нормальных компов</b>\n\n"
            f"{title}\n"
            f"💰 {_money(listing.price)} + {ship.removeprefix('Shipping: ')} = <b>{landed}</b>\n\n"
            f'<a href="{listing.url}">Открыть лот</a>'
        )

    if alert.kind == "not_deal":
        pct = alert.cheaper_pct or 0
        if pct >= 0:
            headline = f"📉 <b>НЕ ДЕАЛ −{pct:.0f}%</b>"
            reason = "Лот дешевле рынка, но не достигает порога."
        else:
            headline = f"❌ <b>ДОРОЖЕ РЫНКА +{abs(pct):.0f}%</b>"
            reason = "Лот дороже самого дешёвого подходящего сингла."
        return (
            f"{headline}\n\n"
            f"{title}\n"
            f"💰 Лот: {price} + {ship.removeprefix('Shipping: ')} = <b>{landed}</b>\n"
            f"📊 Самый дешёвый: <b>{_money(alert.average_price)}</b>\n"
            f"💸 Разница: <b>{_money(alert.savings_usd)}</b>\n"
            f"ℹ️ {reason}"
            f"\n📎 Дешевле:" + _deal_comp_block(alert) + "\n\n"
            f'<a href="{listing.url}">Открыть лот</a>'
        )

    if alert is not None and alert.kind in {"bundle", "bundle_partial"}:
        pct = alert.cheaper_pct or 0
        save = alert.savings_usd
        value = alert.bundle_value_sum or alert.average_price
        sign = "−" if pct >= 0 else "+"
        return (
            f"🔥 <b>ЛОТ {sign}{abs(pct):.0f}%</b>\n\n"
            f"{title}\n"
            f"💰 Лот: {price} + {ship.removeprefix('Shipping: ')} = <b>{landed}</b>\n"
            f"📈 Экономия: <b>{_money(save)}</b>\n"
            f"📊 Стоимость синглов: <b>{_money(value)}</b>"
            f"{_bundle_value_block(alert)}\n\n"
            f'<a href="{listing.url}">Открыть лот</a>'
        )

    pct = alert.cheaper_pct or 0
    bench = alert.average_price
    save = alert.savings_usd
    n = alert.comparable_count or 1
    prices = [
        row.get("cheapest", row.get("avg"))
        for row in alert.top_comps
        if isinstance(row.get("cheapest", row.get("avg")), (int, float))
    ]
    price_list = " · ".join(_money(v) for v in prices)
    market_line = f"📊 {n} самых дешёвых: <b>{price_list}</b>" if price_list else f"📊 Рынок: <b>{_money(bench)}</b>"
    return (
        f"🔥 <b>ДЕАЛ −{pct:.0f}%</b>\n\n"
        f"{title}\n"
        f"💰 Лот: {price} + {ship.removeprefix('Shipping: ')} = <b>{landed}</b>\n"
        f"{market_line}\n"
        f"💸 Экономия: <b>{_money(save)}</b>"
        f"{_deal_comp_block(alert)}\n\n"
        f'<a href="{listing.url}">Открыть лот</a>'
    )

def _comp_urls_block(alert: DealAlert | None) -> str:
    """Short tappable comps, not a dump of raw /itm/ URLs."""
    if alert is None:
        return ""
    urls = [str(u) for u in (alert.comparable_urls or []) if u]
    if not urls:
        urls = [str(row.get("url") or "") for row in (alert.top_comps or []) if row.get("url")]
    if not urls:
        return ""
    lines = "\n".join(_comp_link(u) for u in urls if _comp_link(u))
    return f"\n{lines}" if lines else ""


def format_check_shipping_status(zip_code: str | None = None) -> str:
    zip_code = str(zip_code or "19801").strip() or "19801"
    return f"Доставка ZIP {zip_code}…"


def format_check_bin_status(query: str) -> str:
    return f"Ищу BIN: {(query or '').strip()}"


def format_check_vision_status(text: str) -> str:
    return text


def _ru_figures(n: int) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "фигур"
    last = n % 10
    if last == 1:
        return "фигура"
    if 2 <= last <= 4:
        return "фигуры"
    return "фигур"


def format_check_parsed_status(listing) -> str:
    """Live /check line after title/OCR: lot count or single #{number}."""
    refs = listing_refs(listing)
    named = [item for item in refs if item.name]
    numbered = [item for item in refs if item.number]
    members = list(getattr(listing, "members", None) or [])
    count = extract_bundle_count(getattr(listing, "title", "") or "")
    if count is None and len(members) >= 2:
        count = len(members)
    if count is None and len(named) >= 2:
        count = len(named)
    multi = is_multi_pop(getattr(listing, "title", "") or "", refs) or (
        count is not None and count >= 2
    )
    if multi:
        n = count if count is not None and count >= 2 else max(len(named), len(members), 2)
        return f"Разобрал: {n} {_ru_figures(n)}"
    number = getattr(listing, "pop_number", None) or (
        numbered[0].number if numbered else None
    )
    if number:
        return f"Разобрал: сингл #{number}"
    return "Разобрал: сингл"


def format_check_reply(result: CheckResult) -> str:
    if result.listing is None:
        return _esc(result.error or "Не открыл лот")
    listing = result.listing
    alert = result.alert
    title = _esc(_display_name(listing))
    base = (
        f"{title}\n"
        f"💰 Лот: {_money(listing.price)} + {(_shipping_line(listing)).removeprefix('Shipping: ')}"
        f" = <b>{_money(listing.landed_cost())}</b>"
    )
    if alert is not None and alert.kind in {"bundle", "bundle_partial"}:
        bundle_n = alert.figure_count or len(listing.members or [])
        if bundle_n:
            base += f"\n🧩 лот из {bundle_n}"
    if alert is None or alert.kind == "no_comps":
        suffix = "\n\nℹ️ Нет нормальных компов"
    elif alert.kind == "unparsed":
        n = alert.figure_count or extract_bundle_count(listing.title)
        suffix = f"\n\nℹ️ Не удалось уверенно разобрать {n} фигур" if n else "\n\nℹ️ Не удалось уверенно разобрать фото"
    elif alert is not None and alert.kind == "not_deal":
        pct = alert.cheaper_pct or 0
        if pct >= 0:
            suffix = f"\n\n📉 <b>−{pct:.0f}%</b> vs самый дешёвый BIN · ниже порога"
        else:
            suffix = f"\n\n❌ <b>+{abs(pct):.0f}%</b> дороже самого дешёвого BIN"
        suffix += f" · разница <b>{_money(alert.savings_usd)}</b>"
        cheaper = _deal_comp_block(alert)
        if cheaper:
            suffix += "\n📎 Дешевле:" + cheaper.replace("\n📊 3 самых дешёвых: ", "\n", 1)
    elif alert is not None and alert.kind in {"bundle", "bundle_partial"}:
        pct = alert.cheaper_pct or 0
        if alert.kind == "bundle_partial":
            suffix = f"\n\n🧩 <b>ЧАСТИЧНОЕ СРАВНЕНИЕ</b> · распознанные фигурки vs синглы"
        elif pct >= 0:
            suffix = f"\n\n🔥 <b>−{pct:.0f}%</b> vs синглы · экономия <b>{_money(alert.savings_usd)}</b>"
        else:
            suffix = f"\n\n❌ <b>+{abs(pct):.0f}%</b> дороже суммы синглов · разница <b>{_money(alert.savings_usd)}</b>"
        suffix += _bundle_value_block(alert)
    else:
        pct = alert.cheaper_pct or 0
        prices = [
            row.get("cheapest", row.get("avg"))
            for row in alert.top_comps
            if isinstance(row.get("cheapest", row.get("avg")), (int, float))
        ]
        comp_line = f"\n📊 Самые дешёвые: <b>{' · '.join(_money(v) for v in prices)}</b>" if prices else ""
        suffix = f"\n\n🔥 <b>−{pct:.0f}%</b> · экономия <b>{_money(alert.savings_usd)}</b>" + comp_line
        suffix += _deal_comp_block(alert)
    return f"{base}{suffix}\n\n<a href=\"{listing.url}\">Открыть лот</a>"

def format_scan_reply(*, mode: str, new_count: int, processed: int, alerts: int, error: str | None) -> str:
    extra = f"\n⚠️ {error[:300]}" if error else ""
    return (
        f"🔎 Скан: {mode}\n"
        f"🆕 Новых лотов: <b>{new_count}</b>\n"
        f"✅ Проанализировано и сравнено: <b>{processed}</b>\n"
        f"🔥 Алертов: <b>{alerts}</b>{extra}"
    )


def format_bro(settings: Settings, store: Store) -> str:
    stats = store.stats()
    total_new = int(stats.get("total_new") or 0)
    total_scans = int(stats.get("total_scans") or 0)
    total_alerts = int(stats.get("total_alerts") or 0)
    last_title = (stats.get("last_new_title") or "—").strip()
    last_id = (stats.get("last_new_item_id") or "—").strip()
    last_url = (stats.get("last_new_url") or "").strip()
    last_link = f'<a href="{_esc(last_url)}">открыть последний</a>' if last_url else ""
    err = (stats.get("last_error") or "").strip()
    lines = [
        "📊 <b>Статистика FinderFunko</b>",
        f"🔄 Сканов: <b>{total_scans}</b>",
        f"📥 Всего получено карточек из eBay: <b>{stats.get('total_fetched') or 0}</b>",
        f"🆕 Новых лотов из Newly Listed обнаружено: <b>{total_new}</b>",
        f"📍 В последнем скане: <b>{stats.get('last_new_count') or 0}</b> новых",
        f"📌 В последний скан поставлено в очередь: <b>{stats.get('last_new_count') or 0}</b>",
        f"✅ Реально обработано Vision+comparator: <b>{stats.get('total_processed') or 0}</b>",
        f"⏳ Очередь Vision: <b>{stats.get('queue_pending') or 0}</b> · ⚙️ в работе: <b>{stats.get('queue_processing') or 0}</b>",
        f"⚠️ Ошибок в обработке: <b>{stats.get('last_failed_count') or 0}</b>",
        f"🔥 Алертов отправлено: <b>{total_alerts}</b>",
        "",
        f"🕒 Последний скан: <b>{stats.get('last_scan') or '—'}</b>",
        f"🆕 Последний новый лот: <b>{_esc(last_title)}</b>",
        f"🆔 Item ID: <code>{_esc(last_id)}</code>",
    ]
    if last_link:
        lines.append(last_link)
    if err:
        lines += ["", f"⚠️ Последняя ошибка: {_esc(err[:300])}"]
    return "\n".join(lines)


def format_shutdown_stats(settings: Settings | None, store: Store) -> str:
    stats = store.stats()
    lines = [
        "🛑 <b>Парсер остановлен</b>",
        "",
        "📊 <b>Итог работы</b>",
        f"🔄 Сканов: <b>{stats.get('total_scans') or 0}</b>",
        f"🆕 Новых лотов из Newly Listed: <b>{stats.get('total_new') or 0}</b>",
        f"📥 Всего получено карточек из ленты: <b>{stats.get('total_fetched') or 0}</b>",
        f"🔎 Полностью проанализировано и сравнено: <b>{stats.get('total_processed') or 0}</b>",
        f"🔥 Алертов: <b>{stats.get('total_alerts') or 0}</b>",
        f"🕒 Последний скан: <b>{stats.get('last_scan') or '—'}</b>",
        f"🆕 Последний новый лот: <b>{_esc((stats.get('last_new_title') or '—').strip())}</b>",
    ]
    url = (stats.get("last_new_url") or "").strip()
    if url:
        lines.append(f'<a href="{_esc(url)}">Открыть последний новый лот</a>')
    return "\n".join(lines)

def format_status(settings: Settings, store: Store) -> str:
    stats = store.stats()
    dests = store.alert_chat_ids()
    last_err = stats.get("last_error") or ""
    last_mode = stats.get("last_mode") or "—"
    fetched = stats.get("last_fetched") or ""
    live_note = ""
    if last_mode == "live" and last_err and fetched in {"", "0"}:
        live_note = " (eBay не отдал лоты — бот живой, /demo работает)"
    elif last_mode == "live" and last_err:
        live_note = " (есть лоты, часть запросов с ошибкой)"

    lines = [
        "Как работает",
        "• Смотрю только НОВЫЕ лоты. Уже виденные не сравниваю снова и не шлю повторно.",
        "• Сингл: Funko {имя} {номер} (Funko Wonka 1477). OCR с фото, если имя на коробке полнее названия (Billy Butcher → Funko Billy Butcherson 773). Не ищу Funko Pop / номер сначала / Meteor/Rock/Series/Art.",
        "• Алерт от −20% vs 1–3 cheapest. Авто-скан без компараблов молчит. «не с чем сравнить» только в /check после поиска Funko {имя} {номер}. Стоп: Ctrl+O (не Ctrl+C).",
        "• Лот из N (2–6): N запросов Funko {имя с этой коробки} {Pop номер}, сумма самых дешёвых синглов vs лот, тоже 20%+. Другой лот из 2 — другие имена, не Corleone/Soprano по умолчанию.",
        "• Не разобрал имена (lot of 6 / 15 Funko toys без имён) — молчу, без свалки каталога.",
        "• #479 Funko / LE900 / 1500 Pcs / SDCC 2024 / #SE — не лот.",
        "• Лоты дороже $500 пока не трогаю (кроме /check).",
        f"• Доставка ZIP {settings.ship_to_zip}: SRP +$x.xx важнее. Free = $0. "
        "Не разобралась — Free $0 и всё равно сравниваю. /itm Update только в /check. "
        "Price / Shipping / Total и ссылка /itm/ компов.",
        "• Авто-скан сам, команду писать не надо. Первый скан не является seed: каждый новый лот проходит полный анализ и сравнение.",
        "",
        f"проанализировано: {stats['listings']}",
        f"прислано: {stats['alerts']}",
        f"Новых в последнем проходе: {stats.get('last_new_count') or '—'}",
        f"Куда алерты: {', '.join(dests) or 'ещё нет (нужен /start в ЛС и в группе)'}",
        f"ЛС: {', '.join(store.dm_chat_ids()) or 'нет'}",
        f"Группа: {store.get_bound_group() or 'не привязана'}",
        f"Последний скан: {stats['last_scan'] or 'ещё не было'}",
        f"Режим: {last_mode}{live_note}",
        f"Порог: от {settings.deal_threshold_pct:.0f}% и выше, без потолка",
        f"Теги: {settings.query_label()}",
        f"Интервал: {settings.poll_interval()}с. RSS ~80 лотов / ~25с; паузы 2–5с только на HTML /itm. Страница {settings.page_size}",
        "Источники: RSS BIN каждые 120с (только новые id) → 1 BIN RSS на фигуру из сниппета. "
        "Playwright выключен. /check не блокирует авто-скан.",
        "eBay использует PROXY_URL из .env. Telegram использует TELEGRAM_PROXY отдельно.",
    ]
    top = _top_deal_lines(store)
    if top:
        lines.append("")
        lines.append("Топ выгодных:")
        lines.extend(top)
    if last_err:
        lines.append(f"eBay: {last_err}")
        lines.append(
            "eBay использует заданный PROXY_URL; Telegram — отдельный TELEGRAM_PROXY."
        )
    return "\n".join(lines)


def _top_deal_lines(store: Store, limit: int = 3) -> list[str]:
    ranked: list[tuple[float, str]] = []
    for row in store.recent_alerts(20):
        if row.get("kind") != "deal":
            continue
        pct = row.get("cheaper_pct")
        listing = row.get("listing") or {}
        title = str(listing.get("title") or "").strip()
        if pct is None or not title:
            continue
        ranked.append((float(pct), title))
    ranked.sort(key=lambda item: item[0], reverse=True)
    lines: list[str] = []
    for pct, title in ranked[:limit]:
        short = title if len(title) <= 72 else title[:69] + "…"
        lines.append(f"• −{pct:.0f}%  {short}")
    return lines
