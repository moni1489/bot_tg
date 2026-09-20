import os
import re
import asyncio
import logging
import httpx
from funko_deal_bot.ebay import EbayClient, parse_item_page_html
from funko_deal_bot.check import parse_check_item_id

log = logging.getLogger(__name__)

def _get_ebay_client(proxy_url: str = "") -> EbayClient:
    return EbayClient(
        site="https://www.ebay.com",
        ship_to_zip="19801",
        proxy_url=proxy_url,
    )

async def fetch_and_parse_ebay(url: str, scraper_api_key: str | None = None) -> dict | None:
    """Fetch eBay item using FunkoDealBot's exact engine and apply +$2.00 surcharge."""
    item_id = parse_check_item_id(url, plain_message=False) or parse_check_item_id(url, plain_message=True)
    if not item_id:
        m = re.search(r'(\d{9,15})', url or '')
        item_id = m.group(1) if m else None

    if not item_id:
        return None

    api_key = scraper_api_key or os.getenv("SCRAPER_API_KEY", "").strip()

    # Step 1: Try FunkoDealBot EbayClient (direct and/or configured proxy)
    configured_proxy = os.getenv("PROXY_URL", "").strip()
    proxies_to_try = [configured_proxy] if configured_proxy else []
    if "" not in proxies_to_try:
        proxies_to_try.append("")

    for proxy in proxies_to_try:
        try:
            client = _get_ebay_client(proxy)
            listing, err = await asyncio.to_thread(client.fetch_item, item_id)
            if listing and listing.price is not None:
                price = float(listing.price)
                raw_shipping = float(listing.shipping_cost if listing.shipping_cost is not None else 0.0)
                shipping = round(raw_shipping + 2.0, 2)
                return {
                    "name": listing.title or "Товар eBay",
                    "price": price,
                    "raw_shipping": raw_shipping,
                    "shipping": shipping,
                    "weight": None,
                    "currency": listing.currency or "USD",
                }
            else:
                log.warning(f"EbayClient.fetch_item for {item_id} (proxy={bool(proxy)}): {err}")
        except Exception as e:
            log.warning(f"Error running EbayClient for {url} (proxy={bool(proxy)}): {e}")

    # Step 2: Fallback to ScraperAPI + FunkoDealBot parse_item_page_html
    if api_key:
        try:
            target_url = f"https://www.ebay.com/itm/{item_id}?_stpos=19801"
            scraper_url = f"http://api.scraperapi.com?api_key={api_key}&url={target_url}&country_code=us"
            async with httpx.AsyncClient(timeout=25.0) as http_client:
                resp = await http_client.get(scraper_url)
                if resp.status_code == 200 and resp.text:
                    listing = parse_item_page_html(
                        resp.text,
                        item_id=item_id,
                        url=f"https://www.ebay.com/itm/{item_id}",
                        ship_to_zip="19801",
                    )
                    if listing and listing.price is not None:
                        price = float(listing.price)
                        raw_shipping = float(listing.shipping_cost if listing.shipping_cost is not None else 0.0)
                        shipping = round(raw_shipping + 2.0, 2)
                        return {
                            "name": listing.title or "Товар eBay",
                            "price": price,
                            "raw_shipping": raw_shipping,
                            "shipping": shipping,
                            "weight": None,
                            "currency": listing.currency or "USD",
                        }
        except Exception as e:
            log.warning(f"ScraperAPI + parse_item_page_html failed for {item_id}: {e}")

    return None
