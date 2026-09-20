from __future__ import annotations

import logging
import re
from statistics import mean, median

from funko_deal_bot.models import DealAlert, Listing
from funko_deal_bot.normalize import (
    PopRef,
    complete_pop_ref_from_ocr,
    extract_as_character,
    extract_bundle_count,
    extract_exclusive,
    extract_lot_character_names,
    extract_lot_members,
    extract_pop_id,
    format_comparable_query,
    comparable_search_query,
    is_bundle,
    is_multi_pop,
    is_usable_comp,
    looks_like_auction_start,
    name_mentioned,
    is_longer_name_completion,
    is_incomplete_comp_title,
    parse_pop_refs,
    token_similarity,
)

log = logging.getLogger(__name__)

_IMAGE_TITLE_OVERLAP = 0.2
_MAX_PHASH_HAMMING = 8
_CHEAPEST_COMPS = 3
_MAX_FIGURE_SEARCHES = 6


def listing_pop_id(listing: Listing) -> str | None:
    if listing.pop_number:
        return str(listing.pop_number)
    return extract_pop_id(listing.title)


def listing_pop_name(listing: Listing) -> str | None:
    if listing.pop_name:
        return listing.pop_name
    refs = parse_pop_refs(listing.title)
    if refs and refs[0].name:
        return refs[0].name
    return None


def _ref_from_member_row(row: dict) -> PopRef:
    number = row.get("number") or row.get("pop")
    return PopRef(
        name=(row.get("name") or None),
        number=(str(number) if number else None),
        exclusive=(row.get("exclusive") or None),
        series=(row.get("series") or None),
    )


def listing_refs(listing: Listing) -> list[PopRef]:
    stored = [_ref_from_member_row(row) for row in (listing.members or [])]
    if any(item.number for item in stored) or len(stored) >= 2:
        return stored
    lot = extract_lot_members(listing.title)
    if len(lot) >= 2:
        exclusive = extract_exclusive(listing.title)
        return [_ref_from_member_row({**row, "exclusive": exclusive}) for row in lot]
    if stored:
        return stored
    return parse_pop_refs(listing.title)


def comparable_search_plan(listing: Listing) -> list[tuple[str, PopRef]]:
    """Build a resilient, interleaved search plan for every validated figure.

    Search order is intentionally interleaved by figure: first one high-signal
    query for EACH member, then a second query for EACH member. With the engine's
    live-search budget this guarantees a 4-box lot does not spend the whole budget
    on the first figure.
    """
    count = extract_bundle_count(listing.title)
    if count is not None and count > _MAX_FIGURE_SEARCHES:
        return []
    refs = listing_refs(listing)
    photo_members = bool(listing.members)
    if listing.image_url and listing.vision_checked and not listing.members:
        return []
    complete = [item for item in refs if item.name and item.number]
    if photo_members and refs and any(item.name and not item.number for item in refs):
        return []
    if len(complete) > _MAX_FIGURE_SEARCHES:
        return []

    multi = is_multi_pop(listing.title, refs) or len([r for r in refs if r.name]) >= 2
    numbers = {item.number for item in refs if item.number}
    if multi and len(numbers) <= 1 and not is_bundle(listing.title) and not extract_lot_members(listing.title):
        multi = False

    refs_to_use: list[PopRef] = []
    if multi:
        refs_to_use = refs[:_MAX_FIGURE_SEARCHES]
    else:
        ref = refs[0] if refs else PopRef(name=listing.pop_name, number=listing.pop_number, exclusive=listing.exclusive)
        if listing.pop_name or listing.pop_number:
            ref = PopRef(
                name=listing.pop_name or ref.name,
                number=ref.number or listing.pop_number,
                exclusive=ref.exclusive or listing.exclusive,
                series=getattr(ref, "series", None),
            )
        if photo_members and ref.name and ref.number:
            completed = PopRef(name=ref.name, number=str(ref.number), exclusive=ref.exclusive, series=getattr(ref, "series", None))
        else:
            completed = complete_pop_ref_from_ocr(ref, listing.ocr_text, title=listing.title, title_number=extract_pop_id(listing.title) or listing.pop_number, from_title=True)
        refs_to_use = [completed]

    prepared: list[PopRef] = []
    for ref in refs_to_use:
        if ref.name and ref.number:
            prepared.append(PopRef(name=ref.name, number=str(ref.number), exclusive=ref.exclusive, series=getattr(ref, "series", None)))
        else:
            completed = complete_pop_ref_from_ocr(ref, listing.ocr_text, title=listing.title, from_title=False)
            prepared.append(completed)

    # One canonical query per validated figure. `fetch_live_comparables_for_query`
    # already searches eBay through its RSS/HTML/Browse sources for that single
    # query and only falls back to a `Funko Pop ...` spelling when the primary
    # query returns zero usable comps. Do not schedule multiple variants here: it
    # repeats the same marketplace search and wastes the live-search budget.
    out: list[tuple[str, PopRef]] = []
    seen: set[str] = set()
    for ref in prepared:
        if not (ref.name and ref.number):
            continue
        q = format_comparable_query(ref.name, ref.number) or ""
        q = re.sub(r"\s+", " ", q).strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append((q, ref))
    return out[:_MAX_FIGURE_SEARCHES]


def _same_as_character(new: Listing, other: Listing) -> bool:
    left = extract_as_character(new.title)
    right = extract_as_character(other.title)
    return bool(left and right and left.lower() == right.lower())


def _image_hashes_similar(new: Listing, other: Listing) -> bool:
    """Use a stored listing pHash if present — no cloud vision, no download here."""
    left = (new.image_hash or "").strip()
    right = (other.image_hash or "").strip()
    if not left or not right:
        return False
    try:
        distance = bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return False
    return distance <= _MAX_PHASH_HAMMING


def is_similar(new: Listing, other: Listing) -> bool:
    """Same Pop # / same 'as Character' / token overlap — never a global average."""
    if new.item_id == other.item_id:
        return False
    if not is_usable_comp(
        other.title,
        pop=listing_pop_id(new),
        item_id=other.item_id,
        exclude_id=new.item_id,
        listing_type=other.listing_type,
    ):
        return False
    n_id = listing_pop_id(new)
    o_id = listing_pop_id(other)
    n_name = (listing_pop_name(new) or "").lower()
    o_name = (listing_pop_name(other) or "").lower()
    sim = token_similarity(new.title, other.title)
    if n_name and o_name and n_name == o_name:
        if n_id and o_id and n_id != o_id:
            return False
        return True
    if n_id and o_id:
        if n_id != o_id:
            return False
        # #SE / #XX are shared-exclusive codes, not unique Pop numbers.
        if n_id.isalpha():
            return _same_as_character(new, other)
        if n_name and o_name and n_name == o_name:
            return True
        other_title = other.title or ""
        new_title = new.title or ""
        return (
            sim >= 0.28
            or _same_as_character(new, other)
            or bool(n_name and name_mentioned(n_name, other_title))
            or bool(o_name and name_mentioned(o_name, new_title))
        )
    if _same_as_character(new, other):
        return True
    # Without a shared Pop # or an explicit character match, do not compare
    # listings merely because their titles share generic words.
    if _image_hashes_similar(new, other) and sim >= _IMAGE_TITLE_OVERLAP:
        return True
    return False


def _cheapest_prices(prices: list[float], *, take: int = _CHEAPEST_COMPS) -> list[float]:
    return sorted(prices)[:take]


def match_named_catalog(title: str, catalog: list[Listing]) -> list[dict]:
    """Cheapest comparison cost per character name taken from after 'lot of N'."""
    names = extract_lot_character_names(title)
    if not names:
        return []
    named: list[dict] = []
    singles = [
        item
        for item in catalog
        if not is_bundle(item.title) and item.comparison_cost() is not None
    ]
    for name in names:
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.I)
        prices = [item.comparison_cost() for item in singles if pattern.search(item.title or "")]
        prices = [p for p in prices if p is not None]
        if not prices:
            continue
        cheap = _cheapest_prices(prices)
        named.append({"name": name, "avg": round(mean(cheap), 2)})
    named.sort(key=lambda row: row["avg"], reverse=True)
    return named


def is_valid_comp(new: Listing, other: Listing) -> bool:
    """BIN boxed same-number comparable; sticker price is allowed when shipping is hidden."""
    if other.comparison_cost() is None:
        return False
    if other.members and len(other.members) >= 2:
        return False
    if not is_usable_comp(
        other.title,
        pop=listing_pop_id(new),
        item_id=other.item_id,
        exclude_id=new.item_id,
        listing_type=other.listing_type,
    ):
        return False
    return is_similar(new, other)


def _name_completion_in_title(expected: str, title: str) -> bool:
    """Match a full expected name to a title where the final token is extended."""
    exp = [t.casefold() for t in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", expected or "") if len(t) >= 2]
    words = [t.casefold() for t in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", title or "") if len(t) >= 2]
    if len(exp) < 2 or len(words) < len(exp):
        return False
    for i in range(0, len(words) - len(exp) + 1):
        if words[i:i + len(exp) - 1] != exp[:-1]:
            continue
        return is_longer_name_completion(exp[-1], words[i + len(exp) - 1])
    return False


def _name_matches_with_completion(expected: str, actual: str) -> bool:
    """Conservative name match for same Pop #: allow a longer final surname token."""
    expected = " ".join((expected or "").split())
    actual = " ".join((actual or "").split())
    if not expected or not actual:
        return False
    if name_mentioned(expected, actual):
        return True
    left = [t.casefold() for t in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", expected)]
    right = [t.casefold() for t in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", actual)]
    if len(left) != len(right) or len(left) < 2:
        return False
    if left[:-1] != right[:-1]:
        return False
    return is_longer_name_completion(left[-1], right[-1]) or is_longer_name_completion(right[-1], left[-1])


def comps_for_ref(ref: PopRef, catalog: list[Listing], *, exclude_id: str | None = None) -> list[Listing]:
    matches: list[Listing] = []
    name = (ref.name or "").strip()
    number = (ref.number or "").strip()
    for item in catalog:
        if exclude_id and str(item.item_id) == str(exclude_id):
            continue
        if item.comparison_cost() is None:
            continue
        if not is_usable_comp(
            item.title,
            pop=number or None,
            item_id=item.item_id,
            listing_type=item.listing_type,
        ):
            continue
        other_id = listing_pop_id(item)
        other_name = (listing_pop_name(item) or "").lower()
        if number and other_id and other_id != number:
            continue
        if name:
            in_title = name_mentioned(name, item.title or "")
            if not in_title:
                # Common eBay titles abbreviate a final surname/name token
                # (`Billy Butcher` vs boxed `Billy Butcherson`). The Pop # must
                # still match, and all preceding name tokens must match exactly.
                compact_name = re.sub(r"[^a-z]", "", name.lower())
                compact_other = re.sub(r"[^a-z]", "", other_name.lower())
                completion_ok = bool(compact_name and compact_other and (
                    compact_other.startswith(compact_name) and len(compact_other) <= len(compact_name) + 4
                    or _name_matches_with_completion(name, other_name)
                    or _name_completion_in_title(name, item.title or "")
                ))
                if not completion_ok and other_name != name.lower():
                    # A matching Pop # alone is not enough: different characters
                    # can share a number/partial number in bad eBay snippets.
                    continue
        elif not number:
            continue
        matches.append(item)
    matches.sort(key=lambda item: item.comparison_cost() or 0)
    return matches


def looks_cheaper_than_hint(
    listing: Listing,
    catalog: list[Listing],
    *,
    threshold_pct: float = 20.0,
) -> bool:
    """True when sticker/landed already looks ≥threshold vs cheapest in-memory BIN."""
    comps = find_comparables(listing, catalog)
    prices = [
        cost
        for cost in (item.comparison_cost() for item in comps)
        if cost is not None
    ]
    if not prices:
        return False
    hint = min(prices)
    if hint <= 0:
        return False
    cost = listing.landed_cost()
    if cost is None:
        cost = listing.price
    if cost is None:
        return False
    return (hint - cost) / hint * 100.0 >= float(threshold_pct)


def find_comparables(listing: Listing, catalog: list[Listing]) -> list[Listing]:
    """Valid BIN boxed same-number hits, cheapest comparison cost first."""
    matches = [item for item in catalog if is_valid_comp(listing, item)]
    matches.sort(key=lambda item: item.comparison_cost() or 0)
    return matches


def _priced_member_rows(refs: list[PopRef], catalog: list[Listing], *, exclude_id: str | None = None, listing: Listing | None = None) -> list[dict]:
    rows: list[dict] = []
    live_map: dict[tuple[str, str], list[dict]] = {}
    for member in (listing.members if listing is not None else []) or []:
        key = (str(member.get("name") or "").strip().casefold(), str(member.get("number") or member.get("pop") or "").strip())
        if key != ("", "") and member.get("live_comps"):
            live_map[key] = list(member.get("live_comps") or [])

    for ref in refs:
        if not ref.name:
            continue
        key = (ref.name.strip().casefold(), str(ref.number or "").strip())
        live = live_map.get(key, [])
        cost = None
        url = None
        price = None
        shipping = None
        shipping_label = ""
        if live:
            valid = [row for row in live if row.get("comparison") is not None]
            valid.sort(key=lambda row: float(row.get("comparison")))
            if valid:
                pick = valid[0]
                cost = round(float(pick.get("comparison")), 2)
                url = str(pick.get("url") or "") or None
                price = pick.get("price")
                shipping = pick.get("shipping")
                shipping_label = str(pick.get("shipping_label") or "")
        if cost is None:
            comps = comps_for_ref(ref, catalog, exclude_id=exclude_id)
            if comps and comps[0].comparison_cost() is not None:
                pick = comps[0]
                cost = round(float(pick.comparison_cost()), 2)
                url = pick.url
                price = pick.price
                shipping = pick.shipping_cost
                shipping_label = pick.shipping_label
        raw_name = ref.name
        name = raw_name.title() if raw_name.isupper() else raw_name
        rows.append(
            {
                "name": name,
                "number": ref.number,
                "pop": ref.number,
                "exclusive": ref.exclusive,
                "avg": cost,
                "cheapest": cost,
                "price": price,
                "shipping": shipping,
                "shipping_label": shipping_label,
                "url": url,
            }
        )
    return rows


def _no_comps_alert(listing: Listing, ship_to_zip: str) -> DealAlert:
    return DealAlert(
        listing=listing,
        kind="no_comps",
        cheaper_pct=None,
        average_price=None,
        median_price=None,
        comparable_count=0,
        reason="не с чем сравнить",
        comparable_urls=[],
        savings_usd=None,
        ship_to_zip=ship_to_zip,
        top_comps=[],
    )


def _auto_or_no_comps(listing: Listing, ship_to_zip: str, *, manual: bool) -> DealAlert | None:
    """«не с чем сравнить» is /check only. Auto-scan skips silently."""
    if not manual:
        return None
    return _no_comps_alert(listing, ship_to_zip)


def _bundle_alert(
    listing: Listing,
    *,
    ship_to_zip: str,
    figure_count: int | None,
    named: list[dict],
    names_unparsed: bool,
    cheaper: float | None,
    savings: float | None,
    value_sum: float | None,
    reason: str,
) -> DealAlert:
    return DealAlert(
        listing=listing,
        kind="bundle",
        cheaper_pct=cheaper,
        average_price=value_sum,
        median_price=None,
        comparable_count=sum(1 for row in named if row.get("avg") is not None),
        reason=reason,
        comparable_urls=[row["url"] for row in named if row.get("url")],
        savings_usd=savings,
        ship_to_zip=ship_to_zip,
        figure_count=figure_count,
        bundle_names_unparsed=names_unparsed,
        bundle_value_sum=value_sum,
        top_comps=named,
    )


def _unparsed_alert(listing: Listing, ship_to_zip: str, *, figure_count: int | None = None) -> DealAlert:
    return DealAlert(
        listing=listing,
        kind="unparsed",
        cheaper_pct=None,
        average_price=None,
        median_price=None,
        comparable_count=0,
        reason="имена не разобрал",
        comparable_urls=[],
        savings_usd=None,
        ship_to_zip=ship_to_zip,
        figure_count=figure_count,
        bundle_names_unparsed=True,
        top_comps=[],
    )


def evaluate_listing(
    listing: Listing,
    catalog: list[Listing],
    *,
    threshold_pct: float,
    min_comparables: int,
    min_price: float,
    ship_to_zip: str = "19801",
    max_lot_usd: float | None = 500.0,
    manual: bool = False,
) -> DealAlert | None:
    listing.ship_to_zip = ship_to_zip
    oversized_bundle_count = extract_bundle_count(listing.title)
    if oversized_bundle_count is not None and oversized_bundle_count > _MAX_FIGURE_SEARCHES:
        return _unparsed_alert(listing, ship_to_zip, figure_count=oversized_bundle_count) if manual else None
    # Never alert on titles that explicitly describe a loose/unboxed/incomplete figure.
    # Vision can still override this only when it positively identifies a boxed figure
    # from the photo; otherwise the title-level exclusion is safest.
    if is_incomplete_comp_title(listing.title) and not listing.vision_checked:
        return _no_comps_alert(listing, ship_to_zip) if manual else None
    # A successful Vision pass with zero boxed figures means this listing photo
    # is not a boxed Funko lot. Do not let title/OCR rescue a loose figure.
    if listing.vision_checked and not listing.members:
        return _no_comps_alert(listing, ship_to_zip) if manual else None
    if listing.shipping_cost is None and not manual:
        listing.assume_free_shipping()
    landed = listing.landed_cost()
    refs = listing_refs(listing)
    multi = (len(listing.members) > 1) if listing.members else is_multi_pop(listing.title, refs)
    numbers = {item.number for item in refs if item.number}
    if (
        multi
        and listing.pop_number
        and len(numbers) <= 1
        and not is_bundle(listing.title)
        and not extract_lot_members(listing.title)
    ):
        multi = False

    if multi:
        sticker = listing.price if listing.price is not None else landed
        # Auto-scan skips lots over $500. Manual /check does not — user asked on purpose.
        if (
            not manual
            and max_lot_usd is not None
            and sticker is not None
            and sticker > max_lot_usd
        ):
            return None
        named_refs = [item for item in refs if item.name]
        figure_count = extract_bundle_count(listing.title)
        if figure_count is not None and figure_count > _MAX_FIGURE_SEARCHES:
            return _unparsed_alert(listing, ship_to_zip, figure_count=figure_count) if manual else None
        if not named_refs:
            return _unparsed_alert(listing, ship_to_zip, figure_count=figure_count) if manual else None
        if figure_count is None and len(named_refs) < 2:
            return _unparsed_alert(listing, ship_to_zip) if manual else None
        if figure_count is None and len(named_refs) > _MAX_FIGURE_SEARCHES:
            return _unparsed_alert(listing, ship_to_zip, figure_count=len(named_refs)) if manual else None
        member_rows = _priced_member_rows(named_refs, catalog, exclude_id=listing.item_id, listing=listing)
        priced = [row for row in member_rows if row.get("cheapest") is not None]
        # Manual /check should still report and show comparisons for the boxes
        # that were confidently recognized, even when a bundle member is missing
        # or has no comp. Auto-scan remains strict to avoid valuing an incomplete
        # bundle as though it were complete.
        complete_expected = figure_count is None or len(named_refs) == figure_count
        if manual and priced and landed is not None and (not complete_expected or len(priced) < len(named_refs)):
            value_sum = round(sum(float(row["cheapest"]) for row in priced), 2)
            comparable_rows = [row for row in member_rows if row.get("cheapest") is not None]
            missing = max((figure_count or len(named_refs)) - len(named_refs), 0)
            reason = "Есть сравнения только для распознанных фигурок."
            if missing:
                reason += f" Не распознано: {missing}."
            partial_alert = _bundle_alert(
                listing,
                ship_to_zip=ship_to_zip,
                figure_count=figure_count or len(named_refs),
                named=member_rows,
                names_unparsed=not complete_expected or len(priced) < len(named_refs),
                cheaper=round((value_sum - landed) / value_sum * 100, 1) if value_sum > 0 else None,
                savings=round(value_sum - landed, 2),
                value_sum=value_sum,
                reason=reason,
            )
            partial_alert.kind = "bundle_partial"
            return partial_alert
        if figure_count is not None and len(named_refs) != figure_count:
            return _unparsed_alert(listing, ship_to_zip, figure_count=figure_count) if manual else None
        if not priced:
            return _auto_or_no_comps(listing, ship_to_zip, manual=manual)
        if len(priced) != len(named_refs):
            return _no_comps_alert(listing, ship_to_zip) if manual else None
        if landed is None:
            return _no_comps_alert(listing, ship_to_zip) if manual else None
        value_sum = round(sum(float(row["cheapest"]) for row in member_rows), 2)
        if value_sum <= 0:
            return _no_comps_alert(listing, ship_to_zip) if manual else None
        cheaper = (value_sum - landed) / value_sum * 100
        if cheaper < threshold_pct and not manual:
            return None
        savings = round(value_sum - landed, 2)
        alert = _bundle_alert(
            listing,
            ship_to_zip=ship_to_zip,
            figure_count=figure_count or len(named_refs),
            named=member_rows,
            names_unparsed=False,
            cheaper=round(cheaper, 1),
            savings=savings,
            value_sum=value_sum,
            reason=(
                f"Лот на {cheaper:.0f}% дешевле суммы самых дешёвых синглов "
                f"(${savings:.2f})."
            ),
        )
        # Manual bundle checks keep the `bundle` kind even when the bundle is
        # more expensive than the sum of its singles; the Telegram renderer
        # can then show the positive premium explicitly.
        return alert

    floor = 0.0 if manual else min_price
    if listing.price is None or listing.price < floor:
        return None
    if not manual and looks_like_auction_start(listing.title, listing.price):
        return None

    comps = find_comparables(listing, catalog)
    cheapest = comps[:_CHEAPEST_COMPS]
    if cheapest:
        log.info(
            "Cheapest BIN landed comps for %s: %s",
            listing.item_id,
            ", ".join(
                f"{c.url} (${c.comparison_cost()})" for c in cheapest if c.url
            ),
        )
    _ = min_comparables
    prices = [
        cost
        for cost in (c.comparison_cost() for c in cheapest)
        if cost is not None and (manual or cost >= min_price)
    ]
    if landed is None:
        return _no_comps_alert(listing, ship_to_zip) if manual else None
    if not prices:
        return _auto_or_no_comps(listing, ship_to_zip, manual=manual)

    # The user asked for the cheapest comparable, not an average of several
    # random listings. Keep the top three only for context in Telegram; the
    # actual deal percentage and savings are measured against the cheapest BIN.
    bench = min(prices)
    med = median(prices)
    if bench <= 0:
        return _auto_or_no_comps(listing, ship_to_zip, manual=manual)
    cheaper = (bench - landed) / bench * 100
    # 20% vs however many cheapest exist (1–3). No upper cap. Manual always reports.
    if cheaper < threshold_pct and not manual:
        return None

    savings = round(bench - landed, 2)
    n = len(prices)
    return DealAlert(
        listing=listing,
        kind=("deal" if cheaper >= threshold_pct else "not_deal"),
        cheaper_pct=round(cheaper, 1),
        average_price=round(bench, 2),
        median_price=round(med, 2),
        comparable_count=n,
        reason=(
            f"На {cheaper:.0f}% дешевле самого дешёвого BIN той же фигуры "
            f"(${savings:.2f}) с доставкой."
        ),
        comparable_urls=[c.url for c in cheapest],
        savings_usd=savings,
        ship_to_zip=ship_to_zip,
        top_comps=[
            {
                "name": c.title[:48],
                "avg": c.comparison_cost(),
                "cheapest": c.comparison_cost(),
                "price": c.price,
                "shipping": c.shipping_cost,
                "shipping_label": c.shipping_label,
                "url": c.url,
            }
            for c in cheapest
        ],
    )
