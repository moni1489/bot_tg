from __future__ import annotations

import re

# Command + args, or a pasted eBay /itm/ URL, or a bare 9–15 digit item id.
_CHECK_CMD_RE = re.compile(r"^/check(?:@\S+)?(?:\s+|$)", re.I)
_EBAY_COM_ITM_RE = re.compile(
    r"(?:https?://)?(?:www\.)?ebay\.com/(?:itm|p)/"
    r"(?:[^/\s?#]+/)?(\d{9,15})",
    re.I,
)
_EBAY_ANY_ITM_RE = re.compile(
    r"(?:https?://)?(?:www\.)?ebay\.[a-z.]+/(?:itm|p)/"
    r"(?:[^/\s?#]+/)?(\d{9,15})",
    re.I,
)
_BARE_ITEM_ID_RE = re.compile(r"^\s*(\d{9,15})\s*$")
_PLAIN_EBAY_HOST_RE = re.compile(r"(?:https?://)?(?:www\.)?ebay\.com/", re.I)

CHECK_USAGE = (
    "Нужна ссылка eBay /itm/ или номер лота.\n"
    "/check https://www.ebay.com/itm/123456789012"
)


def strip_check_command(text: str) -> str:
    raw = (text or "").strip()
    match = _CHECK_CMD_RE.match(raw)
    if not match:
        return raw
    return raw[match.end() :].strip()


def parse_check_item_id(text: str, *, plain_message: bool = False) -> str | None:
    """Pull an eBay item id from /check args, a URL, or a bare id.

    Plain chat messages must contain ebay.com/itm/ (or /p/). /check also
    accepts other eBay hosts and a 9–15 digit id.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if plain_message:
        if raw.startswith("/"):
            return None
        if not _PLAIN_EBAY_HOST_RE.search(raw):
            return None
        match = _EBAY_COM_ITM_RE.search(raw)
        return match.group(1) if match else None

    if raw.startswith("/"):
        cmd = raw.split()[0].lstrip("/").split("@")[0].lower()
        if cmd != "check":
            return None
        payload = strip_check_command(raw)
        if not payload:
            return None
    else:
        payload = raw
    match = _EBAY_ANY_ITM_RE.search(payload) or _EBAY_COM_ITM_RE.search(payload)
    if match:
        return match.group(1)
    bare = _BARE_ITEM_ID_RE.match(payload)
    return bare.group(1) if bare else None


def is_plain_ebay_itm_message(text: str) -> bool:
    return parse_check_item_id(text, plain_message=True) is not None
