from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from funko_deal_bot.browser import (
    DEFAULT_PW_TIMEOUT_MS,
    PlaywrightFetch,
    fetch_search_html,
    with_stpos,
)
from funko_deal_bot.config import normalize_proxy_url, proxy_enabled_flag
from funko_deal_bot.models import Listing
from funko_deal_bot.normalize import (
    comparable_search_query,
    comparable_search_query_from_ref,
    complete_name_from_ocr,
    extract_pop_id,
    format_comparable_query,
    infer_listing_type,
    is_bundle,
    is_usable_comp,
    looks_like_auction_start,
    name_mentioned,
    parse_pop_refs,
    parse_price,
    title_has_pop,
    parse_shipping,
    parse_shipping_amount,
    shipping_label,
)
from funko_deal_bot.polite import PoliteLimiter

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 FunkoDealBot/0.2"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
}
ITEM_ID_RE = re.compile(r"/itm/(?:[^/\s\"'<>]+/)?(\d{9,15})", re.I)
# Absolute eBay item URLs (com, de, co.uk, …) plus relative /itm/ ids.
ITM_URL_RE = re.compile(
    r"(?:https?://(?:www\.)?ebay\.[a-z.]+)?/itm/(?:[^/\s\"'<>]+/)?(\d{9,15})",
    re.I,
)
PAGE_SIZE = 40
NEARBY_BEFORE = 80
NEARBY_AFTER = 900
HTTPX_TIMEOUT = 28.0
SHIPPING_FETCH_ATTEMPTS = 3
SEARCH_PW_TIMEOUT_MS = DEFAULT_PW_TIMEOUT_MS
ITEM_PW_TIMEOUT_MS = DEFAULT_PW_TIMEOUT_MS

from pathlib import Path as _Path

SNIPPET_PATH = _Path("data/ebay_last_html_snippet.html")
NO_PROXY = (
    "Дома прокси не нужен: eBay идёт с вашего IP. "
    "PROXY_URL не задан. Укажите его в .env для eBay; бесплатные списки прокси не крутим."
)
PROXY_ON = "прокси включён."
PROXY_ON_403 = "прокси включён, eBay всё ещё 403"


def proxy_advice(proxy_url: str = "", *, status: int = 0) -> str:
    if normalize_proxy_url(proxy_url):
        if status == 403:
            return PROXY_ON_403
        return PROXY_ON
    return NO_PROXY
CHALLENGE_MARKERS = (
    "pardon our interruption",
    "px-captcha",
    "are you a robot",
    "access denied",
    "splashui",
    "cf-challenge",
    "datadome",
    "geo.captcha",
    "bot detection",
    "captcha",
)
CONSENT_MARKERS = ("cookie consent", "we use cookies", "consent-banner", "onetrust")




class EbayError(RuntimeError):
    pass

def classify_body(text: str) -> str:
    low = (text or "").lower()
    if any(marker in low for marker in CHALLENGE_MARKERS):
        return "challenge"
    if any(marker in low for marker in CONSENT_MARKERS) and "/itm/" not in low:
        return "consent"
    if "error page | ebay" in low or "something went wrong on our end" in low:
        return "error_page"
    return "ok"


def _save_snippet(html: str) -> None:
    try:
        SNIPPET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNIPPET_PATH.write_text((html or "")[:80_000], encoding="utf-8")
        log.info("Saved eBay HTML snippet to %s", SNIPPET_PATH)
    except OSError:
        log.exception("Could not save eBay HTML snippet")


def explain_empty(
    source: str,
    status: int,
    body: str,
    *,
    parsed: int = 0,
    proxy_url: str = "",
) -> str:
    advice = proxy_advice(proxy_url, status=status)
    kind = classify_body(body)
    if kind == "challenge":
        return f"{source} HTTP {status}: страница проверки/блока (бот-чек), не выдача. " + advice
    if kind == "consent":
        return f"{source} HTTP {status}: cookies/согласие вместо выдачи. " + advice
    if kind == "error_page":
        return f"{source} HTTP {status}: служебная ошибка eBay. " + advice
    if status == 200 and parsed == 0:
        return (
            f"{source} HTTP 200, карточек 0 — сбой разбора, не «датацентр-IP». "
            "Не нашли .s-item / li.s-card / JSON и ссылок /itm/ (9–15 цифр). "
            "Ждать бесполезно, пока так. Чиним разбор 200-страницы по /itm/, не прокси-ферму. "
            + advice
        )
    if status == 403:
        return f"{source} HTTP 403 (фильтр с этого IP). " + advice
    return f"{source} HTTP {status}. " + advice


_explain_empty = explain_empty




def _item_id_from_url(url: str) -> str | None:
    match = ITEM_ID_RE.search(url or "")
    return match.group(1) if match else None


def _tag_local(tag: str) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag.split(":")[-1]


def _child_text(el: ET.Element, name: str) -> str:
    want = name.lower()
    for child in list(el):
        if _tag_local(child.tag).lower() != want:
            continue
        href = (child.get("href") or child.get("url") or "").strip()
        text = "".join(child.itertext()).strip()
        return href or text
    return ""


def _parse_rss(xml_text: str, site: str) -> list[Listing]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[Listing] = []
    seen: set[str] = set()
    for item in root.iter():
        if _tag_local(item.tag).lower() != "item":
            continue
        title = _child_text(item, "title")
        link = _child_text(item, "link") or _child_text(item, "guid")
        desc = _child_text(item, "description")
        blob = " ".join(part for part in (title, link, desc, "".join(item.itertext())) if part)
        item_id = _item_id_from_url(link) or _item_id_from_url(blob)
        if not item_id or item_id in seen:
            continue
        if not title:
            title = f"eBay item {item_id}"
        price, currency = parse_price(title)
        if price is None:
            price, currency = parse_price(desc or blob)
        ship_cost, ship_label = parse_shipping(title)
        if ship_cost is None:
            ship_cost, ship_label = parse_shipping(desc)
        if ship_cost is None:
            ship_cost, ship_label = parse_shipping(blob)
        image = None
        soup = BeautifulSoup(desc, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            image = img["src"]
        if ship_cost is None:
            ship_cost, ship_label = parse_shipping(soup.get_text(" ", strip=True))
        seen.add(item_id)
        blob = " ".join(part for part in (title, desc) if part)
        items.append(
            Listing(
                item_id=item_id,
                title=title,
                url=link.split("?")[0] if link else urljoin(site, f"/itm/{item_id}"),
                price=price,
                currency=currency,
                image_url=image,
                listing_type=infer_listing_type(title, blob),
                source="rss",
                shipping_cost=ship_cost,
                shipping_label=ship_label,
                shipping_quoted=ship_cost is not None,
            )
        )
    return items


def _window(text: str, start: int, end: int) -> str:
    lo = max(0, start - NEARBY_BEFORE)
    hi = min(len(text), end + NEARBY_AFTER)
    return text[lo:hi]


def _nearby_price(text: str, start: int, end: int) -> tuple[float | None, str]:
    return parse_price(_window(text, start, end))


def _nearby_title(text: str, start: int, end: int, item_id: str) -> str:
    chunk = _window(text, start, end)
    chunk = re.sub(r"<[^>]+>", " ", chunk)
    chunk = re.sub(r"https?://\S+", " ", chunk, flags=re.I)
    chunk = chunk.replace(item_id, " ")
    chunk = _clean_title(chunk)
    if len(chunk) >= 8 and not chunk.lower().startswith("shop on ebay"):
        if len(chunk) > 140:
            chunk = chunk[:140].rsplit(" ", 1)[0]
        return chunk
    return f"eBay item {item_id}"


def extract_itm_listings(text: str, site: str, *, source: str = "html") -> list[Listing]:
    """Any HTTP 200 body: every eBay /itm/ 9–15 digit id plus nearby parse_price."""
    items: list[Listing] = []
    seen: set[str] = set()
    body = text or ""
    for match in ITM_URL_RE.finditer(body):
        item_id = match.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)
        raw = match.group(0)
        if raw.lower().startswith("http"):
            href = raw.split("?")[0]
        else:
            href = urljoin(site, raw.split("?")[0])
        price, currency = _nearby_price(body, match.start(), match.end())
        listing = _listing_from_card(
            item_id=item_id,
            title=_nearby_title(body, match.start(), match.end(), item_id),
            href=href or urljoin(site, f"/itm/{item_id}"),
            price_text="",
            ship_text=_window(body, match.start(), match.end()),
            image=None,
            source=source,
        )
        if listing is None:
            continue
        if price is not None:
            listing.price = price
            listing.currency = currency
        items.append(listing)
    return items


def _merge_listings(*groups: list[Listing]) -> list[Listing]:
    by_id: dict[str, Listing] = {}
    order: list[str] = []
    for group in groups:
        for item in group:
            existing = by_id.get(item.item_id)
            if existing is None:
                by_id[item.item_id] = item
                order.append(item.item_id)
                continue
            if (existing.price is None) and (item.price is not None):
                existing.price = item.price
                existing.currency = item.currency
            if existing.title.startswith("eBay item ") and item.title and not item.title.startswith(
                "eBay item "
            ):
                existing.title = item.title
            if not existing.image_url and item.image_url:
                existing.image_url = item.image_url
            if item.shipping_cost is not None:
                # HTML/SRP cards usually carry the buyer-visible delivery amount.
                # Prefer that richer shipping evidence over an RSS/embedded value
                # when sources disagree, but otherwise preserve the first value.
                if existing.shipping_cost is None or (
                    str(item.source).startswith("html") and not str(existing.source).startswith("html")
                ):
                    existing.shipping_cost = item.shipping_cost
                    existing.shipping_label = item.shipping_label
                    existing.shipping_quoted = item.shipping_quoted
                    existing.ship_to_zip = item.ship_to_zip
            if str(item.source).startswith("html") and not str(existing.source).startswith("html"):
                existing.source = item.source
    return [by_id[item_id] for item_id in order]


def parse_http_200_body(
    text: str, site: str, *, source: str = "html", snippets_only: bool = False
) -> list[Listing]:
    """RSS or HTML 200: namespaced <item>, cards/JSON if present.

    snippets_only: SRP cards/RSS/JSON — not a regex pile of every /itm/ id.
    """
    rss_items = _parse_rss(text, site) if text and "<item" in text.lower() else []
    soup = BeautifulSoup(text or "", "html.parser")
    cards = _parse_s_item(soup, site)
    if not cards:
        cards = _parse_s_card(soup, site)
    embedded = _parse_embedded_json(text or "", site)
    if snippets_only:
        return _merge_listings(rss_items, cards, embedded)
    hrefs = _parse_itm_hrefs(text or "", site)
    raw = extract_itm_listings(text or "", site, source=source)
    return _merge_listings(rss_items, cards, embedded, hrefs, raw)


def _clean_title(text: str) -> str:
    return " ".join((text or "").replace("Opens in a new window or tab", " ").split())


def _title_from_node(node) -> str:
    if node is None:
        return ""
    for sel in (
        ".s-card__title",
        "[class*='s-card__title']",
        ".s-item__title",
        ".su-styled-text",
        "h3",
        "[role='heading']",
    ):
        el = node.select_one(sel) if hasattr(node, "select_one") else None
        if el:
            title = _clean_title(el.get_text(" ", strip=True))
            if title and not title.lower().startswith("shop on ebay"):
                return title
    img = node.select_one("img[alt]") if hasattr(node, "select_one") else None
    if img and img.get("alt"):
        title = _clean_title(str(img.get("alt")))
        if title:
            return title
    aria = node.get("aria-label") if hasattr(node, "get") else None
    if aria:
        title = _clean_title(str(aria))
        if title:
            return title
    if hasattr(node, "get_text"):
        return _clean_title(node.get_text(" ", strip=True))
    return ""


def _card_meta_text(card) -> str:
    """All SRP attribute rows: Buy It Now / or Best Offer / +$9.14 delivery."""
    chunks: list[str] = []
    seen: set[str] = set()
    if not hasattr(card, "select"):
        return ""
    for sel in (
        ".s-item__price",
        ".s-card__price",
        ".s-item__logisticsCost",
        ".s-item__shipping",
        ".s-item__purchase-options",
        ".s-item__purchaseOptions",
        ".s-item__format",
        ".s-card__attribute-row",
        ".s-card__shipping",
        "[class*='s-card__attribute']",
        ".s-item__subtitle",
        ".s-item__caption",
        ".s-item__dynamic",
        "[class*='delivery']",
    ):
        for el in card.select(sel):
            text = el.get_text(" ", strip=True)
            if text and text not in seen:
                seen.add(text)
                chunks.append(text)
    blob = " ".join(chunks)
    extra = _card_ship_text(card)
    joined = " ".join(part for part in (blob, extra) if part)
    if parse_shipping(joined)[0] is not None:
        return joined
    if hasattr(card, "get_text"):
        return f"{joined} {card.get_text(' ', strip=True)}".strip()
    return joined


def _listing_from_card(
    *,
    item_id: str,
    title: str,
    href: str,
    price_text: str,
    ship_text: str,
    image: str | None,
    source: str,
) -> Listing | None:
    title = _clean_title(title)
    if not title or title.lower().startswith("shop on ebay"):
        return None
    price, currency = parse_price(price_text) if price_text else parse_price(title)
    if price is None and price_text:
        stripped = str(price_text).replace(",", "").strip()
        try:
            price = float(stripped)
            currency = "USD"
        except ValueError:
            pass
    ship_cost, ship_label = parse_shipping(ship_text or title)
    listing_type = infer_listing_type(title, f"{price_text or ''} {ship_text or ''}")
    return Listing(
        item_id=item_id,
        title=title,
        url=href.split("?")[0],
        price=price,
        currency=currency,
        image_url=image,
        listing_type=listing_type,
        source=source,
        shipping_cost=ship_cost,
        shipping_label=ship_label,
        shipping_quoted=ship_cost is not None,
    )


_CARD_SHIP_SELS = (
    ".s-item__logisticsCost",
    ".s-item__shipping",
    ".s-item__dynamic",
    ".s-card__shipping",
    ".s-card__attribute-row",
    "[class*='logistics']",
    "[class*='shipping']",
    "[class*='delivery']",
)


def _card_ship_text(card) -> str:
    bits: list[str] = []
    if hasattr(card, "select"):
        for sel in _CARD_SHIP_SELS:
            for el in card.select(sel):
                text = el.get_text(" ", strip=True)
                if text:
                    bits.append(text)
    blob = " ".join(bits)
    if parse_shipping(blob)[0] is not None:
        return blob
    if hasattr(card, "get_text"):
        return card.get_text(" ", strip=True) or blob
    return blob


def _json_ship_text(node: dict) -> str:
    bits: list[str] = []
    for key in (
        "shippingCost",
        "shippingCostText",
        "logisticsCost",
        "deliveryCost",
        "shippingInfo",
        "currentPricePlusShipping",
    ):
        bits.append(_stringify_ship_value(node.get(key)))
    return " ".join(part for part in bits if part)


def _stringify_ship_value(val: Any) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, bool):
        return ""
    if isinstance(val, (int, float)):
        return f"${float(val):.2f} shipping"
    if isinstance(val, dict):
        for key in ("value", "__value__", "amount", "cost"):
            inner = val.get(key)
            if inner is None or inner == "":
                continue
            if isinstance(inner, (int, float)) or str(inner).replace(".", "", 1).isdigit():
                try:
                    return f"${float(inner):.2f} shipping"
                except (TypeError, ValueError):
                    pass
            text = _stringify_ship_value(inner)
            if text:
                return text
        for key in ("text", "label", "shippingCostText", "convertedFromValue"):
            if val.get(key) not in (None, ""):
                return _stringify_ship_value(val.get(key))
        return " ".join(filter(None, (_stringify_ship_value(v) for v in val.values())))
    if isinstance(val, list):
        return " ".join(filter(None, (_stringify_ship_value(v) for v in val)))
    return str(val)


def _parse_s_item(soup: BeautifulSoup, site: str) -> list[Listing]:
    items: list[Listing] = []
    seen: set[str] = set()
    for card in soup.select(".s-item"):
        link_el = card.select_one("a.s-item__link") or card.select_one("a[href*='/itm/']")
        if not link_el:
            continue
        title = _title_from_node(card) or _title_from_node(link_el)
        href = link_el.get("href") or ""
        item_id = _item_id_from_url(href)
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        price_el = card.select_one(".s-item__price")
        price_text = price_el.get_text(" ", strip=True) if price_el else ""
        ship_text = " ".join(
            part for part in (_card_meta_text(card), _card_ship_text(card)) if part
        )
        img = card.select_one("img")
        listing = _listing_from_card(
            item_id=item_id,
            title=title,
            href=href or urljoin(site, f"/itm/{item_id}"),
            price_text=price_text,
            ship_text=ship_text,
            image=img.get("src") if img else None,
            source="html",
        )
        if listing:
            items.append(listing)
    return items


def _parse_s_card(soup: BeautifulSoup, site: str) -> list[Listing]:
    items: list[Listing] = []
    seen: set[str] = set()
    for card in soup.select("li.s-card, .s-card"):
        link_el = card.select_one("a[href*='/itm/']")
        if not link_el:
            continue
        href = link_el.get("href") or ""
        item_id = _item_id_from_url(href)
        if not item_id or item_id in seen:
            continue
        title = _title_from_node(card) or _title_from_node(link_el)
        price_el = card.select_one(".s-card__price, [class*='s-card__price'], .su-styled-text.positive")
        img = card.select_one("img")
        listing = _listing_from_card(
            item_id=item_id,
            title=title,
            href=href or urljoin(site, f"/itm/{item_id}"),
            price_text=price_el.get_text(" ", strip=True) if price_el else "",
            ship_text=" ".join(
                part for part in (_card_meta_text(card), _card_ship_text(card)) if part
            ),
            image=(img.get("src") or img.get("data-src")) if img else None,
            source="html-modern",
        )
        if listing:
            seen.add(item_id)
            items.append(listing)
    return items


def _walk_json_items(node: Any, site: str, seen: set[str], out: list[Listing]) -> None:
    if isinstance(node, list):
        for child in node:
            _walk_json_items(child, site, seen, out)
        return
    if not isinstance(node, dict):
        return
    item_id = node.get("itemId") or node.get("item_id") or node.get("legacyItemId") or node.get("listingId")
    if isinstance(item_id, list):
        item_id = item_id[0] if item_id else None
    item_id = str(item_id).strip() if item_id else ""
    title = node.get("title") or node.get("name")
    if isinstance(title, list):
        title = title[0] if title else ""
    title = str(title).strip() if title else ""
    url = node.get("itemWebUrl") or node.get("viewItemURL") or node.get("url") or ""
    if isinstance(url, list):
        url = url[0] if url else ""
    url = str(url)
    if not item_id:
        item_id = _item_id_from_url(url) or ""
    if item_id and title and item_id not in seen:
        price_node = node.get("price") or node.get("currentPrice") or node.get("offers") or {}
        currency = "USD"
        price_text = ""
        if isinstance(price_node, dict):
            price_text = str(price_node.get("value") or price_node.get("__value__") or price_node.get("price") or "")
            currency = str(price_node.get("currency") or price_node.get("@currencyId") or "USD")
        elif price_node:
            price_text = str(price_node)
        listing = _listing_from_card(
            item_id=item_id,
            title=title,
            href=url or urljoin(site, f"/itm/{item_id}"),
            price_text=price_text or title,
            ship_text=_json_ship_text(node),
            image=(node.get("image") or {}).get("imageUrl") if isinstance(node.get("image"), dict) else None,
            source="html-json",
        )
        if listing:
            if price_text:
                parsed, cur = parse_price(f"${price_text}" if price_text.replace(".", "", 1).isdigit() else price_text)
                if parsed is not None:
                    listing.price = parsed
                    listing.currency = cur or currency
            seen.add(item_id)
            out.append(listing)
    for child in node.values():
        if isinstance(child, (dict, list)):
            _walk_json_items(child, site, seen, out)


def _parse_embedded_json(html: str, site: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[Listing] = []
    for script in soup.find_all("script"):
        raw = (script.string or script.get_text() or "").strip()
        if not raw or ("itemId" not in raw and "/itm/" not in raw):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _walk_json_items(data, site, seen, out)
        if len(out) >= PAGE_SIZE:
            break
    return out


def parse_search_html(html: str, site: str) -> list[Listing]:
    return parse_http_200_body(html, site, source="html")


def _parse_itm_hrefs(html: str, site: str) -> list[Listing]:
    """Anchor tags with /itm/ — titles from nearby nodes, price from nearby text."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    fallback: list[Listing] = []
    for link in soup.select('a[href*="/itm/"]'):
        href = link.get("href") or ""
        item_id = _item_id_from_url(href)
        if not item_id or item_id in seen:
            continue
        parent = link.parent
        title = _title_from_node(link)
        blob = ""
        node = link
        for _ in range(4):
            if node is None:
                break
            if hasattr(node, "get_text"):
                blob = node.get_text(" ", strip=True) or blob
            if not title:
                title = _title_from_node(node)
            node = getattr(node, "parent", None)
        img = link.select_one("img") or (link.parent.select_one("img") if link.parent else None)
        listing = _listing_from_card(
            item_id=item_id,
            title=title or f"eBay item {item_id}",
            href=href or urljoin(site, f"/itm/{item_id}"),
            price_text=blob,
            ship_text=blob,
            image=(img.get("src") or img.get("data-src")) if img else None,
            source="html",
        )
        if listing:
            seen.add(item_id)
            fallback.append(listing)
    return fallback


parse_html = parse_search_html

_ITEM_SHIP_SELS = (
    ".ux-labels-values--shipping",
    "[class*='ux-labels-values--shipping']",
    "#shippingModule",
    "[class*='shipping']",
    "[class*='logistics']",
    "[data-testid*='shipping']",
)


def _meta_content(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        node = soup.find("meta", attrs={"property": key}) or soup.find(
            "meta", attrs={"name": key}
        )
        if node and node.get("content"):
            return str(node.get("content") or "").strip()
    return ""


def _json_ld_blobs(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    blobs: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, list):
            blobs.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                blobs.extend(item for item in graph if isinstance(item, dict))
            blobs.append(data)
    return blobs


def _promote_ebay_image_url(url: str) -> str:
    """Prefer eBay's highest practical image derivative for OCR."""
    text = str(url or "").strip()
    if not text:
        return text
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    if "ebayimg.com" not in host:
        return text
    path = parsed.path or ""
    if re.search(r"/s-l\d+\.jpg$", path, flags=re.I):
        path = re.sub(r"/s-l\d+\.jpg$", "/s-l1600.jpg", path, flags=re.I)
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))
    return text


def _image_url_from_value(value: object) -> str | None:
    """Normalize eBay image fields (string, ImageObject dict, or list) to an HTTP URL."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            m = re.search(r"['\"]url['\"]\s*:\s*['\"](https?://[^'\"]+)['\"]", text)
            if m:
                text = m.group(1)
        if text.startswith(("http://", "https://")):
            return text
        return None
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "imageUrl", "src"):
            out = _image_url_from_value(value.get(key))
            if out:
                return out
    if isinstance(value, (list, tuple)):
        for item in value:
            out = _image_url_from_value(item)
            if out:
                return out
    return None

def parse_item_page_html(
    html: str,
    *,
    item_id: str,
    url: str,
    ship_to_zip: str = "19801",
) -> Listing | None:
    """Title/price/photo/shipping from a real view-item HTML page."""
    if classify_body(html or "") == "challenge":
        return None
    soup = BeautifulSoup(html or "", "html.parser")
    title = _meta_content(soup, "og:title", "twitter:title")
    if "|" in title:
        title = title.split("|", 1)[0].strip()
    if not title:
        h1 = (
            soup.select_one("h1.x-item-title__mainTitle")
            or soup.select_one("#itemTitle")
            or soup.select_one("h1")
        )
        if h1:
            title = h1.get_text(" ", strip=True)
            title = re.sub(r"^Details about\s+", "", title, flags=re.I).strip()
    price: float | None = None
    currency = "USD"
    image_url = _meta_content(soup, "og:image", "twitter:image") or None
    for blob in _json_ld_blobs(html):
        types = blob.get("@type")
        type_l = (
            " ".join(str(t) for t in types).lower()
            if isinstance(types, list)
            else str(types or "").lower()
        )
        if "product" not in type_l and "offer" not in type_l:
            continue
        name = str(blob.get("name") or "").strip()
        if name and not title:
            title = name.split("|", 1)[0].strip()
        offers = blob.get("offers") or blob.get("offer")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            raw_price = offers.get("price") or offers.get("lowPrice")
            try:
                if raw_price is not None and price is None:
                    price = float(raw_price)
            except (TypeError, ValueError):
                pass
            currency = str(offers.get("priceCurrency") or currency)
        img = blob.get("image")
        if isinstance(img, list) and img and not image_url:
            image_url = _image_url_from_value(img[0]) or image_url
        elif isinstance(img, str) and not image_url:
            image_url = _image_url_from_value(img) or image_url
    if price is None:
        # eBay has several concurrent price renderers. Keep the stable legacy
        # selectors, then cover the newer data-testid/class variants and
        # itemprop/meta price attributes.
        price_selectors = (
            ".x-price-primary",
            "#prcIsum",
            "[itemprop='price']",
            "meta[itemprop='price']",
            "meta[property='product:price:amount']",
            ".x-bin-price",
            "[data-testid='x-price-primary']",
            "[data-test-id='x-price-primary']",
            "[class*='x-price-primary']",
            "[class*='price-primary']",
            "[data-testid*='price']",
        )
        for selector in price_selectors:
            price_el = soup.select_one(selector)
            if not price_el:
                continue
            content = (
                price_el.get("content")
                or price_el.get("value")
                or price_el.get_text(" ", strip=True)
            )
            parsed, cur = parse_price(str(content))
            if parsed is None:
                # Some price nodes expose a bare numeric content attribute.
                try:
                    parsed = float(str(content).replace(",", "").strip())
                except (TypeError, ValueError):
                    parsed = None
            if parsed is not None:
                price = parsed
                if cur:
                    currency = cur
                break

    if price is None:
        # Final fallback: parse the visible item-page text. On current eBay pages
        # the primary price is usually the first `US $x.xx` before shipping,
        # taxes, or installment text. We deliberately reject payment/shipping
        # contexts so `$10.00` from "4 payments of $10" cannot become the item price.
        visible = soup.get_text(" ", strip=True) if soup else ""
        visible = re.sub(r"\s+", " ", visible)
        candidates = list(
            re.finditer(
                r"(?:US\s*)?\$\s*(?P<val>\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)",
                visible,
                re.I,
            )
        )
        for match in candidates:
            start = max(0, match.start() - 90)
            context = visible[start:match.start()].lower()
            if any(
                marker in context
                for marker in (
                    "shipping",
                    "delivery",
                    "postage",
                    "tax",
                    "interest-free",
                    "payments",
                    "import fee",
                )
            ):
                continue
            try:
                price = float(match.group("val").replace(",", ""))
                currency = "USD"
                break
            except ValueError:
                continue
    image_url = _image_url_from_value(image_url)
    if not image_url:
        img_el = soup.select_one("#icImg") or soup.select_one("img#icImg")
        if img_el and img_el.get("src"):
            image_url = str(img_el.get("src"))
    ship_cost, ship_label = parse_item_shipping_html(html)
    if not title:
        return None
    href = (url or "").split("?")[0] or urljoin("https://www.ebay.com", f"/itm/{item_id}")
    return Listing(
        item_id=item_id,
        title=title,
        url=href,
        price=price,
        currency=currency,
        image_url=image_url,
        listing_type="bin",
        source="item",
        shipping_cost=ship_cost,
        shipping_label=ship_label,
        ship_to_zip=ship_to_zip,
        shipping_quoted=ship_cost is not None,
    )


def parse_item_shipping_html(html: str) -> tuple[float | None, str]:
    """Item page shipping after ZIP + Update — amount comes from HTML, not a constant.

    Free ($0) only from the shipping module. A stray 'Free shipping' on the rest of
    the page must not wipe an SRP ``+$9.67 delivery`` snippet.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    chunks: list[str] = []
    for sel in _ITEM_SHIP_SELS:
        for node in soup.select(sel):
            text = node.get_text(" ", strip=True)
            if text:
                chunks.append(text)
    if chunks:
        return parse_shipping(" ".join(chunks))
    body = soup.get_text(" ", strip=True) if soup else (html or "")
    return parse_shipping_amount(body)


def looks_like_proxy_timeout(err: str | None, status: int = 0) -> bool:
    """WinError 10060 / CONNECT hang — not HTTP 403."""
    if status not in {0, None}:
        return False
    text = (err or "").lower()
    if not text:
        return False
    if "10060" in text or "winerror 10060" in text:
        return True
    if "connecttimeout" in text:
        return True
    if "timed out" in text or "timeout" in text:
        return True
    if "connecterror" in text or "connect error" in text:
        return True
    if "err_timed_out" in text:
        return True
    return False


def _first(node: Any, key: str, default: Any = None) -> Any:
    if not isinstance(node, dict):
        return default
    val = node.get(key, default)
    if isinstance(val, list):
        return val[0] if val else default
    return val


def _finding_items(data: Any, operation: str) -> list[dict]:
    if not isinstance(data, dict):
        return []
    keys = (
        f"{operation}Response",
        "findItemsAdvancedResponse",
        "findItemsByKeywordsResponse",
    )
    block: Any = None
    for key in keys:
        block = data.get(key)
        if block:
            break
    if isinstance(block, list):
        block = block[0] if block else {}
    if not isinstance(block, dict):
        return []
    result = _first(block, "searchResult", {}) or {}
    if isinstance(result, list):
        result = result[0] if result else {}
    items = result.get("item") if isinstance(result, dict) else None
    if items is None:
        return []
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [row for row in items if isinstance(row, dict)]
    return []


def _listing_from_finding_item(item: dict, ship_to_zip: str) -> Listing | None:
    raw_id = _first(item, "itemId", "")
    item_id = str(raw_id or "").strip()
    title = str(_first(item, "title", "") or "").strip()
    url_item = str(_first(item, "viewItemURL", "") or "")
    if not item_id or not title:
        return None
    selling = _first(item, "sellingStatus", {}) or {}
    current = _first(selling, "currentPrice", {}) or {}
    if not isinstance(current, dict):
        current = {}
    try:
        price = float(current.get("__value__", 0) or 0) or None
    except (TypeError, ValueError):
        price = None
    currency = str(current.get("@currencyId") or "USD")
    ship_info = _first(item, "shippingInfo", {}) or {}
    if not isinstance(ship_info, dict):
        ship_info = {}
    ship_cost = None
    ship_label = ""
    cost_raw = ship_info.get("shippingServiceCost") or []
    if isinstance(cost_raw, list):
        cost_node = cost_raw[0] if cost_raw else {}
    elif isinstance(cost_raw, dict):
        cost_node = cost_raw
    else:
        cost_node = {}
    if isinstance(cost_node, dict) and "__value__" in cost_node:
        try:
            ship_cost = float(cost_node.get("__value__", 0) or 0)
        except (TypeError, ValueError):
            ship_cost = None
        ship_label = shipping_label(ship_cost)
    elif str(_first(ship_info, "shippingType", "") or "").lower() == "free":
        ship_cost = 0.0
        ship_label = shipping_label(0.0)
    gallery = _first(item, "galleryURL", None)
    info = _first(item, "listingInfo", {}) or {}
    if not isinstance(info, dict):
        info = {}
    best = str(_first(info, "bestOfferEnabled", "") or "").lower()
    raw_type = str(_first(info, "listingType", "") or "").lower()
    if best in {"true", "1"}:
        listing_type = "obo"
    elif "auction" in raw_type:
        listing_type = "auction"
    else:
        listing_type = "bin"
    return Listing(
        item_id=item_id,
        title=title,
        url=url_item or urljoin("https://www.ebay.com", f"/itm/{item_id}"),
        price=price,
        currency=currency,
        image_url=str(gallery) if gallery else None,
        listing_type=listing_type,
        source="finding",
        shipping_cost=ship_cost,
        shipping_label=ship_label,
        ship_to_zip=ship_to_zip,
        shipping_quoted=ship_cost is not None,
    )


def _make_http_client(proxy_url: str) -> httpx.Client:
    # trust_env=False: Windows HTTP_PROXY/system proxy must not hijack eBay/Telegram.
    kwargs: dict = {
        "headers": HEADERS,
        "timeout": HTTPX_TIMEOUT,
        "follow_redirects": True,
        "trust_env": False,
    }
    proxy = normalize_proxy_url(proxy_url)
    if proxy:
        kwargs["proxy"] = proxy
    log.info("httpx client proxy=%s", proxy_enabled_flag(proxy_url))
    return httpx.Client(**kwargs)




def _comp_name_match(name: str | None, title: str) -> bool:
    """Conservative name evidence check; Pop number is validated separately."""
    if not name:
        return True
    target = [w.casefold() for w in re.findall(r"[A-Za-z0-9'’-]+", name) if len(w) >= 2]
    blob = (title or "").casefold()
    compact_name = re.sub(r"[^a-z0-9]", "", name.casefold())
    compact_title = re.sub(r"[^a-z0-9]", "", blob)
    if compact_name and compact_name in compact_title:
        return True
    hits = 0
    for word in target:
        compact_word = re.sub(r"[^a-z0-9]", "", word)
        if compact_word and (word in blob or compact_word in compact_title):
            hits += 1
    needed = 1 if len(target) <= 1 else max(2, (len(target) + 1) // 2)
    return hits >= min(needed, len(target))

class EbayClient:
    _NUMBER_NAME_CACHE: dict[str, str] = {}
    _NUMBER_NAME_LOCK = threading.Lock()

    def __init__(
        self,
        site: str,
        app_id: str = "",
        ship_to_zip: str = "19801",
        *,
        page_size: int = PAGE_SIZE,
        limiter: PoliteLimiter | None = None,
        client: httpx.Client | None = None,
        proxy_url: str = "",
        oauth_token: str = "",
        enable_playwright: bool = False,
        playwright_fetch: PlaywrightFetch | None = None,
    ) -> None:
        self.site = site.rstrip("/")
        self.app_id = app_id
        self.ship_to_zip = ship_to_zip
        self.page_size = max(10, min(int(page_size or PAGE_SIZE), 60))
        self.limiter = limiter or PoliteLimiter()
        self.proxy_url = normalize_proxy_url(proxy_url)
        self.oauth_token = (oauth_token or "").strip()
        self.enable_playwright = bool(enable_playwright)
        self._playwright_fetch = playwright_fetch
        self._owns_client = client is None
        # RSS/HTML/Finding use the configured eBay PROXY_URL via httpx; Telegram has its own proxy.
        self._client = client or _make_http_client(self.proxy_url)
        self._http_lock = threading.Lock()
        self._direct_rss_this_cycle = False
        self._etags: dict[str, str] = {}
        self._bodies: dict[str, str] = {}
        self.last_sources: list[str] = []
        self.last_error: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(self, query: str, *, newly_listed: bool = True, limit: int | None = None) -> list[Listing]:
        listings, err, _src = self.search_one(query, newly_listed=newly_listed, allow_html=True)
        cap = limit or self.page_size
        if listings:
            return listings[:cap]
        raise EbayError(err or ("eBay не отдал выдачу. " + proxy_advice(self.proxy_url)))

    def fetch_queries(
        self,
        queries: list[str],
        *,
        include_baseline: bool = True,
    ) -> tuple[list[Listing], str | None, list[str]]:
        self.limiter.reset_cycle()
        self._direct_rss_this_cycle = False
        self.last_sources = []
        log.info("eBay RSS cycle starting / RSS запрос (без Playwright по умолчанию)...")
        started = time.monotonic()
        errors: list[str] = []
        merged: list[Listing] = []
        seen: set[str] = set()

        def absorb(items: list[Listing], src: str | None) -> None:
            if src:
                self.last_sources.append(src)
            for item in items:
                if item.item_id in seen:
                    continue
                seen.add(item.item_id)
                merged.append(item)

        for query in queries:
            # Each query gets its own HTML fallback. A 403 on `Funko Pop` must
            # never reduce the discovery feed to one broad query.
            items, err, src = self.search_one(query, newly_listed=True, allow_html=True)
            if err:
                errors.append(f"{query}: {err}")
            absorb(items, src)

        if merged:
            log.info("Discovery feed ready from RSS/HTML/Browse: %s unique listings", len(merged))

        if merged:
            errors = [
                e
                for e in errors
                if "карточек 0" not in e and "не отдал выдачу" not in e
            ]

        error = "; ".join(errors) if errors else None
        self.last_error = error
        secs = int(round(max(0.0, time.monotonic() - started)))
        log.info("RSS %s lots in %ss", len(merged), secs)
        return merged, error, list(dict.fromkeys(self.last_sources))

    def download_bytes(self, url: str) -> bytes | None:
        """Download highest practical eBay image for OCR, without disk temp files."""
        raw_url = str(url or "").strip()
        if not raw_url:
            return None
        promoted = _promote_ebay_image_url(raw_url)
        candidates = [promoted] if promoted and promoted != raw_url else []
        candidates.append(raw_url)
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                resp = self._client.get(candidate)
            except Exception as exc:  # noqa: BLE001
                log.warning("Image download failed for %s: %s", candidate, exc)
                continue
            if resp.status_code == 200 and resp.content:
                log.info("OCR image downloaded: %s (%s bytes)", candidate, len(resp.content))
                return resp.content
            log.warning("Image download status=%s for %s", resp.status_code, candidate)
        return None

    def fetch_item(
        self, item_id: str, *, quote_shipping: bool = True
    ) -> tuple[Listing | None, str | None]:
        """Load one view-item page by id. Works on old listings, not only new RSS hits."""
        item_id = str(item_id or "").strip()
        if not re.fullmatch(r"\d{9,15}", item_id):
            return None, "не похоже на номер лота eBay"
        href = urljoin(self.site + "/", f"itm/{item_id}")
        url = with_stpos(href, self.ship_to_zip)
        body, err, status = self._get_text(url, expect_xml=False)
        listing = None
        if status == 200 and body:
            if classify_body(body) == "challenge":
                log.info("eBay item page %s returned a challenge page; never parse it as a listing", item_id)
                err = explain_empty("eBay item", 200, body, parsed=0, proxy_url=self.proxy_url)
            else:
                listing = parse_item_page_html(
                    body, item_id=item_id, url=href, ship_to_zip=self.ship_to_zip
                )

        # eBay can serve a bot-check on /itm/ while the normal search endpoint
        # still returns the real card. Recover the exact item from search by id
        # before giving up or using a browser. This keeps /check usable with
        # EBAY_SKIP_PLAYWRIGHT=1.
        if listing is None:
            try:
                recovered, search_err, search_src = self.search_one(
                    item_id, newly_listed=False, allow_html=True
                )
                exact = next((item for item in recovered if item.item_id == item_id), None)
                if exact is not None:
                    listing = exact
                    err = None
                    log.info(
                        "Recovered item %s from eBay search (%s): price=%s shipping=%s",
                        item_id,
                        search_src or "unknown",
                        listing.price,
                        listing.shipping_cost,
                    )
                elif search_err:
                    log.info("Exact item recovery for %s failed: %s", item_id, search_err)
            except Exception as exc:  # noqa: BLE001
                log.warning("Exact item recovery failed for %s: %s", item_id, exc)

        if listing is None and self.enable_playwright:
            html, pw_err, pw_status = self._playwright_item(url)
            err = err or pw_err
            status = pw_status or status
            if pw_status == 200 and html:
                listing = parse_item_page_html(
                    html, item_id=item_id, url=href, ship_to_zip=self.ship_to_zip
                )
        if listing is None:
            return None, err or f"не открыл лот {item_id} (HTTP {status})"
        if quote_shipping and listing.shipping_cost is None:
            self.enrich_shipping([listing], min_price=0.0)
        listing.ship_to_zip = self.ship_to_zip
        log.info(
            "Item parsed %s: price=%s shipping=%s (%s)",
            item_id,
            listing.price,
            listing.shipping_cost,
            listing.shipping_label or "unknown",
        )
        return listing, None

    def resolve_pop_name_by_series_number(self, series: str | None, pop_number: str | None, *, exclude_id: str | None = None) -> str | None:
        """Resolve a photographed Pop number using its photographed series context.

        This is intentionally safer than a global number-only lookup. It searches
        eBay for the series + exact number, then extracts names from result titles.
        """
        ser = re.sub(r"\s+", " ", str(series or "").strip())
        pop = str(pop_number or "").strip()
        if not ser or not pop.isdigit() or not (2 <= len(pop) <= 4):
            return None
        key = f"{ser.casefold()}::{pop}"
        with self._NUMBER_NAME_LOCK:
            cached = self._NUMBER_NAME_CACHE.get(key)
        if cached:
            return cached
        query = f"Funko Pop {ser} {pop}"
        try:
            items, err, src = self.search_one(query, newly_listed=False, allow_html=False, sort=15)
        except Exception:
            log.exception("Context Pop-name lookup failed for %r #%s", ser, pop)
            return None
        if not items:
            return None
        from collections import Counter
        counts: Counter[str] = Counter()
        stop = {"funko","pop","vinyl","figure","figures","animation","television","movies","tv","exclusive","new","nib","the","lot","bundle","set","of"}
        series_words={w.casefold() for w in re.findall(r"[A-Za-z]+", ser)}
        for item in items:
            if exclude_id and str(item.item_id) == str(exclude_id):
                continue
            title = item.title or ""
            if not title_has_pop(title, pop) or ser.casefold() not in title.casefold():
                continue
            # Prefer a compact phrase immediately before the Pop number after
            # removing the series itself. This fixes titles such as
            # ``The Sopranos Tony Soprano #1291`` where parse_pop_refs sees only
            # the final word ``Soprano``.
            m=re.search(rf"(.{{0,120}}?)#?\s*{re.escape(pop)}\b", title, re.I)
            cand=""
            if m:
                words=re.findall(r"[A-Za-z][A-Za-z'-]*", m.group(1))
                kept=[]
                for w in reversed(words):
                    lw=w.casefold()
                    if lw in series_words or lw in stop:
                        if kept: break
                        continue
                    kept.append(w)
                    if len(kept)>=4: break
                cand=" ".join(reversed(kept)).strip()
            if len(cand)>=3:
                counts[cand]+=2
                continue
            for ref in parse_pop_refs(title):
                if str(ref.number or "").strip() == pop and ref.name:
                    name = str(ref.name).strip()
                    if name and name.casefold() not in series_words:
                        counts[name] += 1
        if not counts:
            return None
        best, votes = counts.most_common(1)[0]
        if votes < 1:
            return None
        with self._NUMBER_NAME_LOCK:
            self._NUMBER_NAME_CACHE[key] = best
        log.info("Context Pop-name repair %r #%s -> %r (votes=%s source=%s)", ser, pop, best, votes, src or "?")
        return best

    def resolve_pop_name_by_number(self, pop_number: str | None, *, exclude_id: str | None = None) -> str | None:
        """Resolve a Funko character name from an exact Pop number using eBay titles.

        This is a repair step, not the source of the Pop number: the number must
        already have been obtained from the photograph. We only accept result titles
        that explicitly contain the exact number and aggregate the most common parsed
        name, which makes OCR name garbage (e.g. HOHIXIHOIW) harmless.
        """
        pop = str(pop_number or '').strip()
        if not pop or not pop.isdigit() or not (3 <= len(pop) <= 4):
            return None
        with self._NUMBER_NAME_LOCK:
            cached = self._NUMBER_NAME_CACHE.get(pop)
        if cached:
            return cached
        query = f"Funko Pop {pop}"
        try:
            items, err, src = self.search_one(query, newly_listed=False, allow_html=False, sort=15)
        except Exception:
            log.exception("Pop-number name lookup failed for #%s", pop)
            return None
        if not items:
            log.debug("Pop-number name lookup empty #%s: %s", pop, err or src or "no results")
            return None
        from collections import Counter
        counts: Counter[str] = Counter()
        examples: dict[str, str] = {}
        for item in items:
            title = item.title or ""
            if exclude_id and str(item.item_id) == str(exclude_id):
                continue
            if not title_has_pop(title, pop):
                continue
            refs = parse_pop_refs(title)
            parsed_name = None
            for ref in refs:
                if str(ref.number or "").strip() == pop and ref.name:
                    parsed_name = str(ref.name).strip()
                    break

            # The generic title parser intentionally removes franchise words and
            # can shorten names such as "Mighty Thor" to "Thor". For number repair
            # we prefer the literal words immediately before the exact #number, then
            # strip marketplace/franchise noise.
            marker = re.search(rf"(?<!\d)#?\s*{re.escape(pop)}\b", title, re.I)
            if marker:
                prefix = title[:marker.start()]
                words = re.findall(r"[A-Za-z][A-Za-z0-9'’-]*", prefix)
                noise = {
                    "funko", "pop", "vinyl", "figure", "figures", "bobblehead",
                    "exclusive", "new", "nib", "nrfb", "boxed", "box", "lot",
                    "marvel", "dc", "comics", "television", "movies", "movie",
                    "avengers", "endgame", "series", "only", "at", "target",
                    "hot", "topic", "creation", "creations", "the", "and", "of",
                    "for", "with", "collectible", "collectibles", "exclusive",
                }
                clean_words = [w for w in words if w.casefold() not in noise]
                literal_name = " ".join(clean_words[-4:]).strip()
            else:
                literal_name = ""
            name = literal_name or parsed_name or ""
            name = re.sub(r"\s+", " ", name).strip()
            if len(re.sub(r"[^A-Za-z]", "", name)) < 4:
                continue
            key = name.casefold()
            counts[key] += 1
            examples[key] = name
        if not counts:
            # Fallback: use exact-number titles and remove common marketplace noise.
            for item in items:
                title = item.title or ""
                if not title_has_pop(title, pop):
                    continue
                clean = re.sub(r"(?i)\b(?:funko|pop!?|vinyl|figure|figures|bobblehead|exclusive|new|nib|nrfb|boxed|box|lot)\b", " ", title)
                clean = re.sub(rf"(?i)#?\s*{re.escape(pop)}\b", " ", clean)
                clean = re.sub(r"[^A-Za-z0-9'&+\- ]+", " ", clean)
                clean = re.sub(r"\s+", " ", clean).strip()
                if len(clean) >= 4:
                    key = clean.casefold()
                    counts[key] += 1
                    examples[key] = clean
        if not counts:
            return None
        best_key, best_count = counts.most_common(1)[0]
        # Require two independent titles when the search pool is small; otherwise
        # a single odd marketplace title could become the canonical name.
        if best_count < 2 and len(items) > 3:
            return None
        name = examples[best_key]
        with self._NUMBER_NAME_LOCK:
            self._NUMBER_NAME_CACHE[pop] = name
        log.info("Pop-number name repair #%s -> %r (votes=%s)", pop, name, best_count)
        return name

    def fetch_live_comparables(self, listing: Listing, *, limit: int = 16) -> list[Listing]:
        """BIN search for the same name+number, cheapest+shipping first (_sop=15)."""
        ref = None
        if listing.pop_name or listing.pop_number:
            ref = {
                "name": listing.pop_name,
                "number": listing.pop_number,
                "exclusive": listing.exclusive,
            }
        elif listing.members:
            ref = listing.members[0]
        else:
            parsed = parse_pop_refs(listing.title)
            if parsed:
                ref = parsed[0].as_dict()
        pop = extract_pop_id(listing.title) or (ref or {}).get("number") or listing.pop_number
        name = listing.pop_name or (ref or {}).get("name")
        name = complete_name_from_ocr(
            name,
            listing.ocr_text,
            title=listing.title,
            from_title=True,
        ) or name
        query = (
            format_comparable_query(name, pop)
            or comparable_search_query_from_ref(ref)
            or comparable_search_query(listing.title)
        )
        return self.fetch_live_comparables_for_query(
            query,
            exclude_id=listing.item_id,
            pop=pop,
            name=name,
            limit=limit,
        )

    def fetch_live_comparables_for_query(
        self,
        query: str | None,
        *,
        exclude_id: str | None = None,
        pop: str | None = None,
        name: str | None = None,
        limit: int = 3,
    ) -> list[Listing]:
        """Find real fixed-price single-figure comps with robust query fallbacks.

        eBay sometimes returns a useful candidate with shipping omitted from the
        search card. That is not free shipping, but it is still a valid lower-bound
        comparison, so the candidate is retained using Listing.comparison_cost().
        """
        if not query:
            return []

        cap = max(1, min(3, int(limit)))
        primary = str(query).strip()
        clean_name = re.sub(r"\s+", " ", str(name or "")).strip()
        # One canonical search first. eBay itself supports a "Price + Shipping: lowest"
        # sort in the buyer UI; our search_one(sort=15) combines the RSS/HTML/Browse
        # views and we then rank the full candidate pool by landed cost. This avoids
        # wasting 2-4 extra full eBay searches for every figure.
        queries: list[str] = [primary]
        fallback_query = ""
        if clean_name and pop:
            words = [w for w in re.findall(r"[A-Za-z0-9'’-]+", clean_name) if len(w) >= 2]
            fallback_query = f"Funko Pop {clean_name} {pop}".strip()
            if not fallback_query:
                fallback_query = ""

        all_candidates: dict[str, Listing] = {}
        sources_seen: list[str] = []

        def collect(q: str) -> None:
            log.info("Comparable eBay search / ищу ту же фигуру: %s", q)
            items, err, src = self.search_one(
                q, newly_listed=False, allow_html=False, sort=15
            )
            if src and src not in sources_seen:
                sources_seen.append(src)
            if err and not items:
                log.debug("Comparable search empty query=%s: %s", q, err)
            for item in items:
                if not is_usable_comp(
                    item.title, pop=str(pop) if pop else None, item_id=item.item_id,
                    exclude_id=exclude_id, listing_type=item.listing_type,
                ):
                    continue
                if item.price is None or not _comp_name_match(name, item.title or ""):
                    continue
                all_candidates[item.item_id] = item

        collect(primary)

        # Only spend another complete eBay search when the primary query returned
        # nothing. A primary query yielding 1-2 valid comps is still useful and is
        # much faster than launching another full SRP search for every figure.
        if not all_candidates and fallback_query and fallback_query.casefold() != primary.casefold():
            collect(fallback_query)

        if sources_seen:
            self.last_sources.append("comps:" + "+".join(sources_seen))

        candidates = list(all_candidates.values())
        # Search pages are already sorted by price+shipping, but different source
        # parsers may omit shipping. Enrich only the cheapest sticker-price window
        # needed to establish the top-3 landed-cost order.
        candidates.sort(key=lambda item: (float(item.price or 0), item.item_id))
        quote_pool = [item for item in candidates if item.shipping_cost is None][:12]
        if quote_pool:
            self.enrich_shipping(
                quote_pool, min_price=0.0, max_items=len(quote_pool), attempts=1,
                use_playwright=False, use_shopping=False,
            )

        priced = [item for item in candidates if item.comparison_cost() is not None]
        priced.sort(
            key=lambda item: (item.comparison_cost() is None,
                              item.comparison_cost() or float("inf"),
                              float(item.price or 0), item.item_id)
        )
        log.info(
            "Comparable candidates accepted=%s priced=%s name=%r pop=%s sources=%s",
            len(candidates), len(priced), name or "", pop or "", "+".join(sources_seen) or "?",
        )
        take = priced[:cap]
        if take:
            pretty = []
            for item in take:
                comp_cost = item.comparison_cost()
                suffix = " shipping unknown" if item.shipping_cost is None else ""
                pretty.append(f"{item.url} (${comp_cost:.2f}{suffix})")
            log.info("Cheapest BIN comps: %s", ", ".join(pretty))
        else:
            log.info("No comparable BINs for query=%s", primary)
        return take


    def search_one(
        self,
        query: str,
        *,
        newly_listed: bool = True,
        allow_html: bool = True,
        sort: int | None = None,
    ) -> tuple[list[Listing], str | None, str | None]:
        if sort is not None:
            sop = sort
        else:
            sop = 10 if newly_listed else 12
        zip_q = quote_plus(self.ship_to_zip)
        nkw = quote_plus(query)
        ipg = self.page_size
        # eBay's "Price + Shipping: Lowest" sort (_sop=15) can silently
        # filter/streamline results. _blrs=recall_filtering asks eBay for the
        # full result set again, otherwise the "cheapest" comp can be missing.
        comps_sort = sort == 15
        recall = "&_blrs=recall_filtering" if comps_sort else ""
        if comps_sort:
            ipg = max(ipg, 50)
        rss_url = f"{self.site}/sch/i.html?_nkw={nkw}&_sop={sop}&LH_BIN=1&_ipg={ipg}&_stpos={zip_q}&_rss=1{recall}"
        html_url = f"{self.site}/sch/i.html?_nkw={nkw}&_sop={sop}&LH_BIN=1&_ipg={ipg}&_stpos={zip_q}{recall}"
        last_error: str | None = None
        self.limiter.wait(kind="search")

        rss_items, rss_err, rss_status = self._get_rss_text(rss_url)
        rss_parsed: list[Listing] = []
        if rss_status == 200 and rss_items:
            rss_parsed = parse_http_200_body(
                rss_items, self.site, source="rss", snippets_only=False if comps_sort else True
            )
            if not rss_parsed:
                last_error = explain_empty(
                    "RSS", 200, rss_items or "", parsed=0, proxy_url=self.proxy_url
                )
        elif rss_err:
            last_error = rss_err

        # Finding API was decommissioned by eBay on 2025-02-04. For comparator
        # searches we intentionally combine every available modern/HTML source
        # before choosing the cheapest listing; relying on the first RSS body can
        # miss shipping-bearing cards that appear lower in the real SRP.
        browse_items: list[Listing] = []
        if self.oauth_token:
            browse_items = self._browse_search(
                query, ipg, newly_listed=newly_listed, cheapest=comps_sort
            )

        html_items: list[Listing] = []
        if comps_sort or allow_html:
            if not self.limiter.is_blocked(html_url):
                html, html_err, html_status = self._get_text(html_url, expect_xml=False)
                if html_status == 200 and html:
                    html_items = parse_http_200_body(
                        html, self.site, source="html", snippets_only=False
                    )
                    if not html_items:
                        _save_snippet(html)
                        last_error = explain_empty(
                            "HTML", 200, html, parsed=0, proxy_url=self.proxy_url
                        )
                elif html_err:
                    last_error = html_err

        if comps_sort:
            combined = _merge_listings(rss_parsed, browse_items, html_items)
            if combined:
                src_parts = []
                if rss_parsed:
                    src_parts.append("rss")
                if browse_items:
                    src_parts.append("browse")
                if html_items:
                    src_parts.append("html")
                # Keep a larger merged candidate window for the comparator. The
                # buyer-facing page is price+shipping sorted, but source order can
                # differ after merging; returning the whole merged window prevents
                # a cheap HTML result from being cut off by RSS-first ordering.
                combined.sort(
                    key=lambda item: (
                        item.comparison_cost() is None,
                        item.comparison_cost() if item.comparison_cost() is not None else float(item.price or 0),
                        float(item.price or 0),
                        item.item_id,
                    )
                )
                return self._stamp_zip(combined[:max(ipg, 120)]), None, "+".join(src_parts) or "rss"

        if rss_parsed:
            return self._stamp_zip(rss_parsed[:ipg]), None, "rss"
        if browse_items:
            return self._stamp_zip(browse_items[:ipg]), None, "browse"
        if html_items:
            return self._stamp_zip(html_items[:ipg]), None, "html"

        if not allow_html:
            return [], last_error or "eBay не отдал выдачу", None

        if comps_sort:
            return [], last_error or "eBay не отдал выдачу", None

        if self.enable_playwright:
            html, html_err, html_status = self._playwright_text(html_url)
            if html:
                parsed = parse_http_200_body(
                    html, self.site, source="playwright", snippets_only=False
                )
                if parsed:
                    return self._stamp_zip(parsed[:ipg]), None, "playwright"
                _save_snippet(html)
                last_error = explain_empty(
                    "Playwright", html_status or 200, html, parsed=0, proxy_url=self.proxy_url
                )
            elif html_status == 403:
                last_error = html_err or explain_empty(
                    "Playwright", 403, html or "", parsed=0, proxy_url=self.proxy_url
                )
            elif html_err:
                log.warning("Playwright failed, continue scan: %s", html_err)
                last_error = html_err
        elif last_error is None:
            last_error = "eBay не отдал выдачу"

        return [], last_error or "eBay не отдал выдачу", None

    def _playwright_text(self, url: str) -> tuple[str | None, str | None, int]:
        if self.limiter.should_skip_playwright():
            return None, "пропуск Playwright: уже был 403 Playwright в этом цикле", 403
        self.limiter.wait(kind="search")
        if self._playwright_fetch is not None:
            html, err, status = self._playwright_fetch(url)
        else:
            html, err, status = fetch_search_html(
                url,
                proxy_url=self.proxy_url,
                timeout_ms=SEARCH_PW_TIMEOUT_MS,
                wait_until="commit",
            )
        self.limiter.mark_playwright_status(status)
        if status == 403:
            log.warning(
                "eBay 403 via Playwright on %s — proxy=%s — no Playwright retries this cycle",
                url.split("?")[0],
                proxy_enabled_flag(self.proxy_url),
            )
            return (
                None,
                explain_empty(
                    "Playwright", 403, html or "", parsed=0, proxy_url=self.proxy_url
                ),
                403,
            )
        if status == 429:
            return None, "HTTP 429, пауза до следующего цикла", 429
        if status not in {0, 200}:
            return html, err or f"Playwright HTTP {status}", status
        if err:
            return html, err, status
        # HTTP 200 (including challenge HTML): caller parses /itm/; not a datacenter-IP label.
        return html, None, status or 200

    def _get_rss_text(self, url: str) -> tuple[str | None, str | None, int]:
        """Fetch RSS through the configured eBay proxy; never silently bypass it."""
        log.info("RSS/httpx proxy=%s", proxy_enabled_flag(self.proxy_url))
        body, err, status = self._get_text(url, expect_xml=True)
        if self.proxy_url and looks_like_proxy_timeout(err, status):
            log.warning(
                "RSS proxy connection timed out; keeping the configured proxy for this cycle"
            )
        return body, err, status

    def _get_text(
        self,
        url: str,
        *,
        expect_xml: bool,
        client: httpx.Client | None = None,
    ) -> tuple[str | None, str | None, int]:
        if self.limiter.should_skip_retries(url):
            return None, "пропуск: этот URL уже дал 403; другие источники продолжаются", 403
        headers = dict(HEADERS)
        if expect_xml:
            headers["Accept"] = "application/rss+xml, application/xml, text/xml;q=0.9,*/*;q=0.8"
        etag = self._etags.get(url)
        if etag:
            headers["If-None-Match"] = etag
        http = client or self._client
        try:
            with self._http_lock:
                resp = http.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc), 0
        self.limiter.mark_status(url, resp.status_code)
        if resp.status_code == 403:
            log.warning(
                "eBay 403 on %s — proxy=%s — no retries of this URL; other sources continue",
                url.split("?")[0],
                proxy_enabled_flag(self.proxy_url),
            )
            return (
                None,
                explain_empty(
                    "eBay", 403, resp.text or "", parsed=0, proxy_url=self.proxy_url
                ),
                403,
            )
        if resp.status_code == 429:
            self.limiter.mark_status(url, 429)
            return None, "HTTP 429, пауза до следующего цикла", 429
        if resp.status_code == 304 and url in self._bodies:
            return self._bodies[url], None, 200
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}", resp.status_code
        etag_val = resp.headers.get("ETag")
        if etag_val:
            self._etags[url] = etag_val
            self._bodies[url] = resp.text
        return resp.text, None, 200

    def _browse_search(self, query: str, limit: int, *, newly_listed: bool, cheapest: bool) -> list[Listing]:
        """Modern eBay Browse API parser; enabled only with EBAY_OAUTH_TOKEN."""
        if not self.oauth_token:
            return []
        url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
        params = {
            "q": query,
            "limit": str(min(max(limit, 1), 200)),
            "filter": "buyingOptions:{FIXED_PRICE}",
        }
        # eBay documents sort values such as newlyListed and price.
        if newly_listed:
            params["sort"] = "newlyListed"
        elif cheapest:
            params["sort"] = "price"
        headers = {**HEADERS, "Authorization": f"Bearer {self.oauth_token}", "Accept": "application/json"}
        self.limiter.wait(kind="search")
        try:
            resp = self._client.get(url, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("Browse API request failed, continue RSS/HTML: %s", exc)
            return []
        self.limiter.mark_status(url, resp.status_code)
        if resp.status_code != 200:
            log.warning("Browse API HTTP %s, continue RSS/HTML", resp.status_code)
            return []
        try:
            payload = resp.json()
        except Exception:
            return []
        out: list[Listing] = []
        for row in payload.get("itemSummaries") or []:
            try:
                rid = str(row.get("itemId") or "")
                item_id = rid.split("|")[1] if "|" in rid else rid
                if not re.fullmatch(r"\d{9,15}", item_id):
                    item_id = _item_id_from_url(str(row.get("itemWebUrl") or "")) or ""
                if not item_id:
                    continue
                price_obj = row.get("price") or {}
                value = float(price_obj.get("value")) if price_obj.get("value") is not None else None
                currency = str(price_obj.get("currency") or "USD")
                ship = (row.get("shippingOptions") or [{}])[0]
                ship_obj = ship.get("shippingCost") or {}
                shipping = float(ship_obj.get("value")) if ship_obj.get("value") is not None else None
                image = ((row.get("image") or {}).get("imageUrl") or None)
                title = _clean_title(str(row.get("title") or f"eBay item {item_id}"))
                out.append(Listing(
                    item_id=item_id, title=title, url=str(row.get("itemWebUrl") or f"{self.site}/itm/{item_id}"),
                    price=value, currency=currency, image_url=image, listing_type=infer_listing_type(title, title),
                    source="browse", shipping_cost=shipping, shipping_label=shipping_label(shipping),
                    shipping_quoted=shipping is not None, ship_to_zip=self.ship_to_zip,
                ))
            except Exception:
                continue
        return self._stamp_zip(out)

    def _finding(self, query: str, limit: int, *, sort_order: str = "StartTimeNewest") -> list[Listing]:
        if not (self.app_id or "").strip():
            return []
        url = "https://svcs.ebay.com/services/search/FindingService/v1"
        if self.limiter.is_blocked(url):
            return []
        for operation in ("findItemsAdvanced", "findItemsByKeywords"):
            listings = self._finding_operation(
                url, operation, query, limit, sort_order=sort_order
            )
            if listings:
                return listings
        return []

    def _finding_operation(
        self, url: str, operation: str, query: str, limit: int, *, sort_order: str = "StartTimeNewest"
    ) -> list[Listing]:
        params = {
            "OPERATION-NAME": operation,
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self.app_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": query,
            "paginationInput.entriesPerPage": str(min(limit, 100)),
            "buyerPostalCode": self.ship_to_zip,
            "itemFilter(0).name": "ListingType",
            "itemFilter(0).value": "FixedPrice",
            "sortOrder": sort_order,
        }
        self.limiter.wait(kind="search")
        try:
            resp = self._client.get(
                url, params=params, headers={**HEADERS, "Accept": "application/json"}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Finding request failed, continue other sources: %s", exc)
            return []
        self.limiter.mark_status(url, resp.status_code)
        if resp.status_code == 403:
            log.warning(
                "Finding HTTP 403 — proxy=%s — continue RSS/HTML/Playwright",
                proxy_enabled_flag(self.proxy_url),
            )
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return []
        items = _finding_items(data, operation)
        listings: list[Listing] = []
        for item in items:
            listing = _listing_from_finding_item(item, self.ship_to_zip)
            if listing:
                listings.append(listing)
        return self._stamp_zip(listings)

    def _stamp_zip(self, listings: list[Listing]) -> list[Listing]:
        for listing in listings:
            listing.ship_to_zip = self.ship_to_zip
        return listings

    def enrich_shipping(
        self,
        listings: list[Listing],
        *,
        min_price: float = 3.0,
        max_items: int | None = None,
        force: bool = False,
        attempts: int | None = None,
        use_playwright: bool | None = None,
        use_shopping: bool = True,
    ) -> None:
        """Open /itm/{id}?_stpos=ZIP only when the snippet has no shipping (or force)."""
        pending = [
            item
            for item in listings
            if (item.shipping_cost is None or force)
            and (force or self._wants_item_shipping(item, min_price))
        ]
        if not pending:
            return
        cap = len(pending) if max_items is None else max(0, int(max_items))
        for listing in pending[:cap]:
            self._fill_one_shipping(
                listing,
                attempts=attempts,
                use_playwright=use_playwright,
                use_shopping=use_shopping,
            )

    def _wants_item_shipping(self, listing: Listing, min_price: float) -> bool:
        if is_bundle(listing.title):
            return True
        if listing.price is None or listing.price < min_price:
            return False
        if looks_like_auction_start(listing.title, listing.price):
            return False
        return True

    def _apply_ship(self, listing: Listing, cost: float) -> None:
        listing.shipping_cost = float(cost)
        listing.shipping_label = shipping_label(listing.shipping_cost)
        listing.ship_to_zip = self.ship_to_zip
        listing.shipping_quoted = True

    def _item_stpos_url(self, listing: Listing) -> str:
        href = (listing.url or "").split("?")[0].strip()
        if not href and listing.item_id:
            href = urljoin(self.site + "/", f"itm/{listing.item_id}")
        return with_stpos(href, self.ship_to_zip) if href else ""

    def _fill_one_shipping(
        self,
        listing: Listing,
        *,
        attempts: int | None = None,
        use_playwright: bool | None = None,
        use_shopping: bool = True,
    ) -> None:
        """Quote shipping via /itm?_stpos=ZIP. Keep SRP snippet if the item page has no rate."""
        log.info(
            "new listing %s fetching /itm for shipping / новый лот: %s",
            listing.item_id,
            (listing.title or "")[:70],
        )
        prior = listing.shipping_cost
        url = self._item_stpos_url(listing)
        tries = SHIPPING_FETCH_ATTEMPTS if attempts is None else max(1, int(attempts))
        pw = self.enable_playwright if use_playwright is None else bool(use_playwright)
        for attempt in range(1, tries + 1):
            if not url or self.limiter.should_skip_retries(url):
                break
            body, _err, status = self._get_text(url, expect_xml=False)
            if status == 200 and body:
                cost, _label = parse_item_shipping_html(body)
                if cost is not None:
                    self._apply_ship(listing, cost)
                    return
                break
            if status in {403, 429}:
                break
            if attempt >= tries:
                break
            log.info(
                "Shipping GET retry %s/%s item=%s status=%s",
                attempt,
                tries,
                listing.item_id,
                status,
            )
        if pw and url:
            html, _err, status = self._playwright_item(url)
            if status == 200 and html:
                cost, _label = parse_item_shipping_html(html)
                if cost is not None:
                    self._apply_ship(listing, cost)
                    return
        if use_shopping and self.app_id and not self.limiter.is_blocked("https://open.api.ebay.com/shopping"):
            cost = self._shipping_costs(listing.item_id)
            if cost is not None:
                self._apply_ship(listing, cost)
                return
        if prior is not None:
            listing.shipping_cost = prior
            listing.shipping_label = shipping_label(prior)
            listing.ship_to_zip = self.ship_to_zip
            listing.shipping_quoted = True
            return
        log.info(
            "Shipping unknown after ZIP quote item=%s zip=%s — leave unset (no Total)",
            listing.item_id,
            self.ship_to_zip,
        )

    def _playwright_item(self, url: str) -> tuple[str | None, str | None, int]:
        if self.limiter.should_skip_playwright():
            return None, "пропуск Playwright: уже был 403 Playwright в этом цикле", 403
        self.limiter.wait(kind="item")
        if self._playwright_fetch is not None:
            html, err, status = self._playwright_fetch(url)
        else:
            html, err, status = fetch_search_html(
                url,
                proxy_url=self.proxy_url,
                timeout_ms=ITEM_PW_TIMEOUT_MS,
                zip_code=self.ship_to_zip,
            )
        self.limiter.mark_playwright_status(status)
        if status == 403:
            log.warning(
                "eBay 403 via Playwright on item %s — proxy=%s — no Playwright retries this cycle",
                url.split("?")[0],
                proxy_enabled_flag(self.proxy_url),
            )
            return (
                None,
                explain_empty(
                    "Playwright", 403, html or "", parsed=0, proxy_url=self.proxy_url
                ),
                403,
            )
        if status == 429:
            return None, "HTTP 429, пауза до следующего цикла", 429
        if status not in {0, 200}:
            return html, err or f"Playwright HTTP {status}", status
        if err:
            return html, err, status
        return html, None, status or 200

    def _shipping_costs(self, item_id: str) -> float | None:
        params = {
            "callname": "GetShippingCosts",
            "responseencoding": "JSON",
            "appid": self.app_id,
            "siteid": "0",
            "version": "967",
            "ItemID": item_id,
            "DestinationCountryCode": "US",
            "DestinationPostalCode": self.ship_to_zip,
            "IncludeDetails": "true",
            "QuantitySold": "1",
        }
        url = "https://open.api.ebay.com/shopping"
        try:
            self.limiter.wait(html=False)
            resp = self._client.get(url, params=params)
            self.limiter.mark_status(url, resp.status_code)
            if resp.status_code != 200:
                return None
            data = resp.json()
            node = data.get("ShippingCostSummary") or data.get("shippingCostSummary") or {}
            amount = (node.get("ShippingServiceCost") or {}).get("Value") or (
                node.get("ShippingServiceCost") or {}
            ).get("__value__")
            if amount is None:
                return None
            return float(amount)
        except Exception:  # noqa: BLE001
            return None
