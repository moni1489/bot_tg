import re
import json
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 FunkoDealBot/0.2"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ITEM_ID_RE = re.compile(r"/itm/(?:[^/\s\"'<>]+/)?(\d{9,15})", re.I)
BARE_ITEM_ID_RE = re.compile(r"^\s*(\d{9,15})\s*$")

_ITEM_SHIP_SELS = (
    ".ux-labels-values--shipping",
    "[class*='ux-labels-values--shipping']",
    "#shippingModule",
    "[class*='shipping']",
    "[class*='logistics']",
    "[data-testid*='shipping']",
)

_free_ship_re = re.compile(
    r"\bfree\s+(?:international\s+)?(?:shipping|delivery|postage)\b|\bshipping:\s*free\b",
    re.I,
)
_ship_cost_re = re.compile(
    r"(?:\+|plus\s*)?(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)\s*"
    r"(?:estimated\s+)?(?:shipping|delivery|postage)",
    re.I,
)
_carrier_rate_re = re.compile(
    r"(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)\s+"
    r"(?:USPS|UPS|FedEx|DHL|Priority\s+Mail|Ground\s+Advantage)",
    re.I,
)
_shipping_colon_re = re.compile(
    r"(?:shipping|delivery|postage)\s*[:\-–]\s*(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)",
    re.I,
)
_delivery_lead_re = re.compile(
    r"(?:shipping|delivery|postage)\s+(?:\+|plus\s*)?(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)",
    re.I,
)
_price_re = re.compile(
    r"(?P<cur>US\s*\$|\$|£|€|EUR|USD|GBP)\s*(?P<val>\d{1,4}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)",
    re.I,
)

def extract_item_id(url: str) -> str | None:
    if not url:
        return None
    match = ITEM_ID_RE.search(url)
    if match:
        return match.group(1)
    match2 = BARE_ITEM_ID_RE.match(url)
    if match2:
        return match2.group(1)
    return None

def with_us_shipping(url: str, zip_code: str = "19801") -> str:
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    q['_stpos'] = [zip_code]
    q['_ul'] = ['US']
    return urlunparse(parsed._replace(query=urlencode(q, doseq=True)))

def parse_shipping(text: str) -> float | None:
    raw = (text or "").replace("\xa0", " ").replace("&nbsp;", " ")
    for pattern in (_ship_cost_re, _delivery_lead_re, _carrier_rate_re, _shipping_colon_re):
        match = pattern.search(raw)
        if match:
            return float(match.group("val"))
    if _free_ship_re.search(raw):
        return 0.0
    return None

def parse_item_shipping_html(soup: BeautifulSoup, html: str) -> float | None:
    chunks = []
    for sel in _ITEM_SHIP_SELS:
        for node in soup.select(sel):
            t = node.get_text(" ", strip=True)
            if t:
                chunks.append(t)
    if chunks:
        res = parse_shipping(" ".join(chunks))
        if res is not None:
            return res
    body = soup.get_text(" ", strip=True) if soup else (html or "")
    return parse_shipping(body)

def _extract_weight_from_html(soup: BeautifulSoup) -> float | None:
    """Try to find item weight in lbs or oz and convert to kg."""
    text = soup.get_text(" ", strip=True)
    # Search for "Item Weight: 8 oz" or "Weight: 0.5 lbs"
    m_lbs = re.search(r'(?:item\s+)?weight\s*[:\-–]\s*(\d+(?:\.\d+)?)\s*(?:lbs?|pounds?)\b', text, re.I)
    if m_lbs:
        try:
            return round(float(m_lbs.group(1)) * 0.453592, 2)
        except ValueError:
            pass
    m_oz = re.search(r'(?:item\s+)?weight\s*[:\-–]\s*(\d+(?:\.\d+)?)\s*(?:oz|ounces?)\b', text, re.I)
    if m_oz:
        try:
            return round(float(m_oz.group(1)) * 0.0283495, 2)
        except ValueError:
            pass
    m_kg = re.search(r'(?:item\s+)?weight\s*[:\-–]\s*(\d+(?:\.\d+)?)\s*(?:kg|kilograms?)\b', text, re.I)
    if m_kg:
        try:
            return round(float(m_kg.group(1)), 2)
        except ValueError:
            pass
    return None

def parse_ebay_html(html: str) -> dict | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    
    # 1. Product Title
    title = ""
    meta_title = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "twitter:title"})
    if meta_title and meta_title.get("content"):
        title = str(meta_title.get("content")).split("|")[0].strip()
    if not title:
        h1 = soup.select_one("h1.x-item-title__mainTitle") or soup.select_one("#itemTitle") or soup.select_one("h1")
        if h1:
            title = re.sub(r"^Details about\s+", "", h1.get_text(" ", strip=True), flags=re.I).strip()

    price: float | None = None
    currency = "USD"
    
    # 2. JSON-LD structured data (often the cleanest source on eBay)
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            t = str(item.get("@type", "")).lower()
            if "product" in t or "offer" in t:
                if not title and item.get("name"):
                    title = str(item.get("name")).split("|")[0].strip()
                offers = item.get("offers") or item.get("offer")
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    raw_price = offers.get("price") or offers.get("lowPrice")
                    if raw_price is not None:
                        try:
                            price = float(raw_price)
                        except (TypeError, ValueError):
                            pass
                    currency = str(offers.get("priceCurrency") or "USD")

    # 3. CSS Selector fallbacks for price
    if price is None:
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
            el = soup.select_one(selector)
            if not el:
                continue
            content = el.get("content") or el.get("value") or el.get_text(" ", strip=True)
            match = _price_re.search(str(content or ""))
            if match:
                try:
                    price = float(match.group("val").replace(",", ""))
                    break
                except ValueError:
                    pass

    # 4. Visible text fallback if selectors didn't match
    if price is None:
        visible = soup.get_text(" ", strip=True)
        candidates = list(_price_re.finditer(visible))
        for m in candidates:
            start = max(0, m.start() - 90)
            ctx = visible[start:m.start()].lower()
            if any(k in ctx for k in ("shipping", "delivery", "postage", "tax", "payments", "interest-free")):
                continue
            try:
                price = float(m.group("val").replace(",", ""))
                break
            except ValueError:
                continue

    if price is None:
        return None

    # 5. Shipping (domestic to 19801 Delaware)
    raw_shipping = parse_item_shipping_html(soup, html)
    if raw_shipping is None:
        raw_shipping = 0.0

    # User requirement: add +$2.00 surcharge to US shipping
    shipping = round(raw_shipping + 2.0, 2)
    
    # 6. Weight
    weight = _extract_weight_from_html(soup)

    return {
        "name": title or "Товар eBay",
        "price": price,
        "raw_shipping": raw_shipping,
        "shipping": shipping,
        "weight": weight,
        "currency": currency,
    }

async def fetch_and_parse_ebay(url: str, scraper_api_key: str | None = None) -> dict | None:
    """Fetch eBay item with ZIP 19801 and parse it using the FunkoDealBot engine."""
    target_url = with_us_shipping(url, "19801")
    html = ""
    
    # Priority 1: ScraperAPI if key available (handles residential IP & bot defense)
    if scraper_api_key:
        try:
            scraper_url = f"http://api.scraperapi.com?api_key={scraper_api_key}&url={target_url}&country_code=us"
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(scraper_url) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                    else:
                        log.warning(f"ScraperAPI returned status {resp.status} for {target_url}")
        except Exception as e:
            log.warning(f"ScraperAPI fetch failed: {e}")

    # Priority 2: Direct aiohttp request with eBay headers if ScraperAPI was not used or failed
    if not html:
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
                async with session.get(target_url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                    else:
                        log.warning(f"Direct eBay fetch status: {resp.status}")
        except Exception as e:
            log.warning(f"Direct eBay fetch failed: {e}")

    if not html:
        return None

    return parse_ebay_html(html)
