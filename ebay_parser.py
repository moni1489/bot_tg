import os
import re
import asyncio
import logging
from funko_deal_bot.ebay import EbayClient
from funko_deal_bot.check import parse_check_item_id

log = logging.getLogger(__name__)

def _get_ebay_client() -> EbayClient:
    proxy_url = os.getenv("PROXY_URL", "http://14af3f0ca177a:9868adb61a@168.158.238.96:12323")
    return EbayClient(
        site="https://www.ebay.com",
        ship_to_zip="19801",
        proxy_url=proxy_url,
    )

async def fetch_and_parse_ebay(url: str, scraper_api_key: str | None = None) -> dict | None:
    """Fetch eBay item using FunkoDealBot's exact client and apply +$2.00 surcharge."""
    item_id = parse_check_item_id(url, plain_message=False) or parse_check_item_id(url, plain_message=True)
    if not item_id:
        m = re.search(r'(\d{9,15})', url or '')
        item_id = m.group(1) if m else None

    if not item_id:
        return None

    try:
        client = _get_ebay_client()
        listing, err = await asyncio.to_thread(client.fetch_item, item_id)
        if listing and listing.price is not None:
            price = float(listing.price)
            raw_shipping = float(listing.shipping_cost if listing.shipping_cost is not None else 0.0)
            # User requirement: add +$2.00 surcharge on top
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
            log.warning(f"EbayClient.fetch_item error for {item_id}: {err}")
    except Exception as e:
        log.exception(f"Error running EbayClient for {url}: {e}")

    return None
