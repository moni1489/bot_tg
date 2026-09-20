from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from funko_deal_bot.config import Settings
from funko_deal_bot.deals import comparable_search_plan, evaluate_listing, listing_refs
from funko_deal_bot.demo_data import demo_catalog, demo_new_listings
from funko_deal_bot.ebay import EbayClient, EbayError
from funko_deal_bot.messages import (
    CHECK_COMPARE,
    format_check_bin_status,
    format_check_parsed_status,
    format_check_shipping_status,
)
from funko_deal_bot.models import CheckResult, DealAlert, Listing
from funko_deal_bot.normalize import PopRef, canonicalize_ocr_identity, is_ocr_name_noise
from funko_deal_bot.polite import PoliteLimiter
from funko_deal_bot.store import Store
from funko_deal_bot.vision import enrich_listing, expected_figure_count

_MAX_SCAN_COMP_SEARCHES = 6

log = logging.getLogger(__name__)

# High-confidence local mappings learned from verified Funko catalogue/eBay tests.
# They are used before any network-based number repair, so a bad OCR name cannot
# turn a known Pop number into an unrelated marketplace result.
_KNOWN_POP_NAMES: dict[str, str] = {}




@dataclass
class ScanResult:
    mode: str
    fetched: int
    new_count: int
    alerts: list[DealAlert]
    error: str | None = None
    seeded: bool = False
    processed_count: int = 0


def _coerce_recognized_items(recognized_items) -> list[PopRef]:
    """Normalize vision results from either PopRef objects or dict records.

    The vision layer has used both representations over time. Keeping the adapter
    here lets the engine consume every recognized box instead of accidentally
    treating a list as one legacy single-item result.
    """
    out: list[PopRef] = []
    if not recognized_items:
        return out
    if isinstance(recognized_items, dict):
        recognized_items = [recognized_items]
    for item in recognized_items:
        if isinstance(item, PopRef):
            ref = item
        elif isinstance(item, dict):
            number = item.get("number") or item.get("pop") or item.get("pop_number")
            name = item.get("name") or item.get("character") or item.get("title")
            exclusive = item.get("exclusive")
            series = item.get("series")
            ref = PopRef(name=(str(name).strip() if name else None), number=(str(number).strip() if number else None), exclusive=exclusive, series=(str(series).strip() if series else None))
        else:
            continue
        if not ref.name and not ref.number:
            continue
        if any((x.name or "").casefold() == (ref.name or "").casefold() and (x.number or "") == (ref.number or "") for x in out):
            continue
        out.append(ref)
    return out

def _apply_recognized_items(listing: Listing, recognized_items) -> list[PopRef]:
    refs = _coerce_recognized_items(recognized_items)
    if refs:
        listing.members = [ref.as_dict() for ref in refs]
        unique_numbers = {ref.number for ref in refs if ref.number}
        if len(refs) == 1:
            listing.pop_name = refs[0].name
            listing.pop_number = refs[0].number
            listing.exclusive = refs[0].exclusive
        else:
            listing.pop_name = None
            listing.pop_number = None
            listing.exclusive = refs[0].exclusive
    else:
        listing.members = []
        listing.pop_name = None
        listing.pop_number = None
    return refs


def _title_fallback_refs(listing: Listing) -> list[PopRef]:
    """Return only explicit name+Pop-number pairs from the eBay title.

    This is a deterministic recovery source for text-only listings. For pictured
    listings the caller intentionally does NOT use it to create missing members.
    """
    try:
        refs = [ref for ref in listing_refs(listing) if ref.name and ref.number]
    except Exception:
        return []
    out: list[PopRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        name = ref.name.strip()
        number = str(ref.number).strip()
        key = (name.casefold(), number)
        if not name or not number or key in seen:
            continue
        seen.add(key)
        out.append(PopRef(name=name, number=number, exclusive=ref.exclusive, series=getattr(ref, "series", None)))
    return out[:12]


def _merge_explicit_title_refs(listing: Listing, recognized_items: list[PopRef]) -> list[PopRef]:
    """Fill/repair visual refs from explicit title name+# pairs only.

    Exact numbered pairs are much safer than vague title text. Pictured listings
    use this helper only for same-number name repair; they never append missing
    boxes from title.
    """
    title_refs = _title_fallback_refs(listing)
    if not title_refs:
        return recognized_items
    by_num = {str(ref.number): ref for ref in title_refs}
    merged: list[PopRef] = []
    seen_numbers: set[str] = set()
    for ref in recognized_items:
        num = str(ref.number or '').strip()
        exact = by_num.get(num) if num else None
        chosen = exact or ref
        if chosen.number and str(chosen.number) in seen_numbers:
            continue
        merged.append(chosen)
        if chosen.number:
            seen_numbers.add(str(chosen.number))
    for ref in title_refs:
        num = str(ref.number)
        if num not in seen_numbers:
            merged.append(ref)
            seen_numbers.add(num)
    return merged[:12]


class Scanner:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.client = EbayClient(
            settings.ebay_site,
            settings.ebay_app_id,
            settings.ship_to_zip,
            page_size=getattr(settings, "page_size", 40),
            proxy_url=settings.proxy_url,
            oauth_token=getattr(settings, "ebay_oauth_token", ""),
            enable_playwright=settings.playwright_allowed(),
            limiter=PoliteLimiter(1.5, 2.5),
        )
        self._scan_lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_alerts: list[DealAlert] = []
        self._outbound_enabled = True
        self._worker_started = False
        self._worker_start_lock = threading.Lock()

    def scan_in_progress(self) -> bool:
        return self._scan_lock.locked()

    def drop_pending_alerts(self) -> None:
        with self._pending_lock:
            self._pending_alerts.clear()

    def suppress_outbound(self) -> None:
        """Ctrl+O / BOT_DISABLED: drop queued Telegram sends, do not flush later."""
        self._outbound_enabled = False
        self.drop_pending_alerts()

    @property
    def outbound_enabled(self) -> bool:
        return self._outbound_enabled

    def drain_alerts(self) -> list[DealAlert]:
        with self._pending_lock:
            if not self._outbound_enabled:
                self._pending_alerts.clear()
                return []
            items = list(self._pending_alerts)
            self._pending_alerts.clear()
            return items

    def queue_alerts(self, alerts: list[DealAlert]) -> None:
        if not alerts or not self._outbound_enabled:
            return
        kept = [alert for alert in alerts if _auto_alert_sendable(alert)]
        if not kept:
            return
        with self._pending_lock:
            if not self._outbound_enabled:
                return
            self._pending_alerts.extend(kept)

    def run_loop(self, interval: int, *, stop: threading.Event | None = None) -> None:
        """Fetch eBay on schedule while a separate worker performs slow Vision/comps."""
        self.start_processing_worker(stop=stop)
        n = 0
        while True:
            n += 1
            log.info("Scan %s starting... / Скан %s начинаю (eBay RSS)...", n, n)
            try:
                result = self.scan()
                self.queue_alerts(result.alerts)
                log.info(
                    "Scan %s done mode=%s feed=%s new=%s processed=%s alerts=%s / Скан %s готов, лента=%s новых=%s проверено=%s алертов=%s; следующий через %ss",
                    n,
                    result.mode,
                    result.fetched,
                    result.new_count,
                    result.processed_count,
                    len(result.alerts),
                    n,
                    result.fetched,
                    result.new_count,
                    result.processed_count,
                    len(result.alerts),
                    interval,
                )
            except Exception:
                log.exception("Scan %s failed / Скан %s ошибка — цикл продолжается", n, n)
            if stop is not None:
                if stop.wait(interval):
                    return
            else:
                time.sleep(interval)

    def start_processing_worker(self, *, stop: threading.Event | None = None) -> None:
        with self._worker_start_lock:
            if self._worker_started:
                return
            self._worker_started = True
        Thread = threading.Thread
        Thread(target=self.process_queue_loop, kwargs={"stop": stop}, daemon=True, name="vision-worker").start()
        log.info("Vision/comparison worker started in background")

    def process_queue_loop(self, *, stop: threading.Event | None = None) -> None:
        self.store.requeue_stale()
        while True:
            if stop is not None and stop.is_set():
                return
            listing = self.store.claim_next_queue_item()
            if listing is None:
                if stop is not None:
                    if stop.wait(1.0):
                        return
                else:
                    time.sleep(1.0)
                continue
            self._process_one_queued(listing)

    def _process_one_queued(self, listing: Listing) -> None:
        try:
            catalog = self.store.catalog()
            alert = self._maybe_alert(
                listing, catalog, search_budget=[_MAX_SCAN_COMP_SEARCHES]
            )
            self.store.upsert_listing(listing)
            self.store.finish_queue_item(listing.item_id, listing)
            self.store.increment_meta_int("total_processed", 1)
            self.store.set_meta("last_processed_count", "1")
            if alert:
                self.store.increment_meta_int("total_alerts", 1)
                self.queue_alerts([alert])
            log.info("Queue item processed item=%s alert=%s", listing.item_id, bool(alert))
        except Exception as exc:
            self.store.finish_queue_item(listing.item_id, listing, str(exc))
            self.store.increment_meta_int("total_process_failed", 1)
            self.store.set_meta("last_failed_count", str(self.store.get_meta("last_failed_count", "0")))
            log.exception("Queued listing failed item=%s; continuing", listing.item_id)

    def scan(self, *, force_demo: bool = False) -> ScanResult:
        if not self._scan_lock.acquire(blocking=False):
            log.info("Scan skipped: another scan is already running")
            mode = "demo" if (force_demo or self.settings.ebay_mode.lower() == "demo") else "live"
            return ScanResult(
                mode=mode,
                fetched=0,
                new_count=0,
                alerts=[],
                error="скан уже идёт",
            )
        try:
            return self._scan_unlocked(force_demo=force_demo)
        finally:
            self._scan_lock.release()

    def _scan_unlocked(self, *, force_demo: bool = False) -> ScanResult:
        want_demo = force_demo or self.settings.ebay_mode.lower() == "demo"
        mode = "demo" if want_demo else "live"
        error: str | None = None
        listings: list[Listing] = []

        if mode == "live":
            try:
                listings, error, sources = self.client.fetch_queries(
                    self.settings.resolved_queries(),
                    include_baseline=False,
                )
                listings = _dedupe(listings)
                if listings:
                    log.info("Live eBay: %s lots from %s", len(listings), ",".join(sources) or "?")
                    if error:
                        error = f"частично: {error}"
                else:
                    # Stay live and quiet — canned demo lots are /demo only.
                    log.warning("Live eBay returned 0 lots (no demo fallback): %s", error)
            except EbayError as exc:
                error = str(exc)
                log.warning("Live eBay fetch failed (no demo fallback): %s", exc)

        if mode == "demo":
            listings = _dedupe(demo_catalog() + demo_new_listings())

        known = self.store.known_ids()
        baseline_seeded = self.store.get_meta("baseline_seeded", "0") == "1"
        seeded = False
        new_items = [listing for listing in listings if listing.item_id not in known]
        # `_sop=10` is eBay's Newly Listed ordering, but it is still a search
        # page and can contain listings that pre-date this bot. On the first live
        # poll, seed the current page into the catalog and start notifications
        # from the next poll. This prevents an old listing from being reported as
        # "new" simply because the bot was started today.
        if (mode == "live" and self.settings.seed_existing_on_start and not baseline_seeded):
            seeded = True
            for listing in listings:
                self.store.add_listing(listing)
            self.store.set_meta("baseline_seeded", "1")
            new_items = []
            log.info("Initial Newly Listed baseline: seeded %s existing results; no historical alerts", len(listings))

        # Fetch thread does ONLY cheap work. Persist/enqueue immediately, so a
        # 40-result feed cannot block the next eBay poll for minutes while
        # Florence runs on CPU. Vision/comps happen in process_queue_loop().
        self.client.enrich_shipping(new_items, min_price=self.settings.min_price_usd)
        for listing in new_items:
            listing.assume_free_shipping()
        self.store.enqueue_listings(new_items)

        scan_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.set_meta("last_scan", scan_now)
        self.store.set_meta("last_mode", mode)
        self.store.set_meta("last_error", error or "")
        self.store.set_meta("last_fetched", str(len(listings)))
        self.store.set_meta("last_new_count", str(len(new_items)))
        self.store.set_meta("last_processed_count", "0")
        self.store.set_meta("last_failed_count", "0")
        self.store.increment_meta_int("total_scans", 1)
        self.store.increment_meta_int("total_fetched", len(listings))
        self.store.increment_meta_int("total_new", len(new_items))
        if new_items:
            last_new = new_items[0]
            self.store.set_meta("last_new_item_id", last_new.item_id)
            self.store.set_meta("last_new_title", last_new.title)
            self.store.set_meta("last_new_url", last_new.url)
        log.info("Scan fetch complete: feed=%s new=%s queued=%s; Vision continues in background", len(listings), len(new_items), self.store.queue_stats().get('pending',0))
        return ScanResult(
            mode=mode,
            fetched=len(listings),
            new_count=len(new_items),
            alerts=[],
            error=error,
            seeded=seeded,
            processed_count=0,
        )

    def demo_examples(self) -> ScanResult:
        """Explicit /demo: canned deal+bundle at most once per item_id."""
        listings = _dedupe(demo_catalog() + demo_new_listings())
        known = self.store.known_ids()
        for listing in listings:
            if listing.item_id in known:
                continue
            self.store.add_listing(listing)
        catalog = _dedupe(demo_catalog() + self.store.catalog())
        alerts: list[DealAlert] = []
        for listing in demo_new_listings():
            alert = self._maybe_alert(listing, catalog, live_comps=False)
            if alert:
                alerts.append(alert)
        self.store.set_meta("last_scan", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.store.set_meta("last_mode", "demo")
        self.store.set_meta("last_error", "")
        self.store.set_meta("last_fetched", str(len(listings)))
        self.store.set_meta("last_new_count", "0")
        return ScanResult(
            mode="demo",
            fetched=len(listings),
            new_count=0,
            alerts=alerts,
            seeded=False,
        )

    def _title_name_for_number(self, title: str, number: str) -> str | None:
        try:
            from funko_deal_bot.vision import _title_name_for_number as vision_title_name
            return vision_title_name(title, number)
        except Exception:
            return None

    def _recover_missing_photo_names(self, listing: Listing, refs: list[PopRef]) -> list[PopRef]:
        """Recover names individually from eBay after a number was proven by photo.

        The photographed Pop number remains the anchor. This is intentionally a
        separate recovery step for generic bundle titles (e.g. "Anime Funko Bundle")
        where the listing title names none of the characters. A marketplace number
        lookup is never allowed to create a number or replace a good photographed
        identity.
        """
        if not refs or not getattr(listing, "image_url", None):
            return refs
        from funko_deal_bot.vision import is_suspicious_ocr_name
        recovered: list[PopRef] = []
        try:
            from funko_deal_bot.vision import _title_name_candidates
            generic_title = not _title_name_candidates(listing.title or "")
        except Exception:
            generic_title = False
        for ref in refs:
            number = str(ref.number or "").strip()
            current = str(ref.name or "").strip()
            if not number:
                recovered.append(ref)
                continue
            candidate = None
            # Only search when the title does not already bind this number to a
            # specific character. Explicit title bindings are safer and cheaper.
            need_lookup = generic_title or (not current) or is_suspicious_ocr_name(current)
            if not need_lookup:
                recovered.append(ref)
                continue
            try:
                series = str(getattr(ref, "series", None) or "").strip()
                if not series:
                    try:
                        from funko_deal_bot.culture import resolve_work
                        # Only probe obvious title words/phrases; this is contextual
                        # recovery after the photo already supplied the number.
                        title_words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", listing.title or "")
                        probes = []
                        for i in range(len(title_words)):
                            probes.append(" ".join(title_words[i:i+4]))
                        for probe in probes[:12]:
                            canonical_series = resolve_work(probe)
                            if canonical_series:
                                series = canonical_series
                                break
                    except Exception:
                        pass
                if series and hasattr(self.client, "resolve_pop_name_by_series_number"):
                    candidate = self.client.resolve_pop_name_by_series_number(series, number, exclude_id=listing.item_id)
                elif not series and not hasattr(self.client, "resolve_pop_name_by_series_number"):
                    # Compatibility with legacy test doubles/older clients only.
                    # The real V44 client always has the contextual resolver and
                    # therefore never performs an unsafe global number lookup.
                    candidate = self.client.resolve_pop_name_by_number(number, exclude_id=listing.item_id)
                else:
                    candidate = None
            except Exception:
                log.debug("Photo name recovery failed item=%s #%s", listing.item_id, number, exc_info=True)
            if candidate:
                candidate = str(candidate).strip()
                if candidate and not is_suspicious_ocr_name(candidate):
                    log.info("V47 contextual photo-name recovery item=%s series=%r #%s -> %r", listing.item_id, series, number, candidate)
                    recovered.append(PopRef(name=candidate, number=number, exclusive=ref.exclusive, series=getattr(ref, "series", None)))
                    continue
            recovered.append(ref)
        return recovered

    def _canonicalize_photo_names(self, listing: Listing, refs: list[PopRef]) -> list[PopRef]:
        """Final immutable validation barrier before any comparator search."""
        if not refs:
            return []
        from funko_deal_bot.vision import (
            _name_matches_title,
            _specific_name,
            _title_number_name_map,
            _validate_photo_name,
            is_suspicious_ocr_name,
        )

        title = listing.title or ""
        exact_map = _title_number_name_map(title)
        out: list[PopRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            number = str(ref.number or "").strip()
            if not number:
                continue
            raw = str(ref.name or "").strip()
            # For pictured listings the photo is the source of truth. Never run
            # a global eBay number->name resolver here: Pop numbers alone are not
            # sufficiently unique and previously produced false identities such as
            # #404 -> Star Wars Mythrol. Exact title name+# repair remains allowed
            # inside canonicalize_ocr_identity because the number already came from
            # the photograph.
            # A photo-confirmed number may be safely paired with an explicit
            # number->name binding already present in the listing title. This is
            # the strongest recovery path for bundles whose OCR name is noisy.
            canonical = _specific_name(exact_map.get(number, ""))
            if not canonical:
                canonical = canonicalize_ocr_identity(
                    raw,
                    number,
                    title,
                    number_resolver=None,
                )

            # General culture knowledge is a validator, not a generator. If the
            # OCR candidate looks generic (or conflicts with a known franchise),
            # try a canonical character label from Wikidata. A failed network
            # lookup never invalidates an otherwise clean photo name.
            try:
                from funko_deal_bot.culture import choose_character
                series_ctx = str(getattr(ref, "series", None) or "").strip()
                if canonical and series_ctx:
                    resolved_name, culture_score = choose_character(canonical, series_ctx)
                    if resolved_name and culture_score >= 0.80:
                        canonical = resolved_name
                elif canonical and (is_suspicious_ocr_name(canonical) or len(canonical.split()) == 1):
                    resolved_name, culture_score = choose_character(canonical, None)
                    if resolved_name and culture_score >= 0.92:
                        canonical = resolved_name
            except Exception:
                pass

            if not canonical or is_ocr_name_noise(canonical) or is_suspicious_ocr_name(canonical):
                log.info("V47 final validation rejected item=%s number=%s raw=%r", listing.item_id, number, raw)
                # Preserve the physical number for /check display, but name=None
                # makes this impossible to send as a comparator query.
                out.append(PopRef(name=None, number=number, exclusive=ref.exclusive, series=getattr(ref, "series", None)))
                continue
            key = (canonical.casefold(), number)
            if key in seen:
                continue
            seen.add(key)
            out.append(PopRef(name=canonical, number=number, exclusive=ref.exclusive, series=getattr(ref, "series", None)))
        return out[:12]

    def _maybe_alert(
        self,
        listing: Listing,
        catalog: list[Listing],
        *,
        live_comps: bool | None = None,
        search_budget: list[int] | None = None,
    ) -> DealAlert | None:
        if self.store.has_alert(listing.item_id):
            return None
        if listing.price is None:
            return None
        use_live = (
            self.settings.ebay_mode.lower() == "live" if live_comps is None else live_comps
        )
        pooled = catalog
        if use_live:
            recognized_items = enrich_listing(
                listing,
                downloader=self.client.download_bytes,
                openai_api_key=self.settings.openai_api_key,
            )
            # Consume the complete recognition list explicitly. Older engine code
            # assumed a single ref and silently dropped bundle members.
            # Pictured lots: title may repair a photo-read number/name pair but cannot add members.
            if getattr(listing, "image_url", None):
                recognized_items = self._recover_missing_photo_names(listing, recognized_items)
                recognized_items = self._canonicalize_photo_names(listing, recognized_items)
            else:
                recognized_items = _merge_explicit_title_refs(listing, recognized_items)
            _apply_recognized_items(listing, recognized_items)
            if recognized_items:
                title_refs = _title_fallback_refs(listing)
                if title_refs:
                    recognized_numbers = {str(ref.number) for ref in recognized_items if ref.number}
                    title_numbers = {str(ref.number) for ref in title_refs if ref.number}
                    if recognized_numbers != title_numbers:
                        log.info(
                            "Vision/title recovery item=%s recognized=%s title_explicit=%s",
                            listing.item_id,
                            sorted(recognized_numbers),
                            sorted(title_numbers),
                        )
            expected = getattr(listing, "vision_expected_count", None) or expected_figure_count(listing.title)
            named_count = sum(1 for ref in recognized_items if ref.name and ref.number)
            if expected and named_count < expected:
                log.warning(
                    "Bundle recognition incomplete item=%s expected=%s numbered=%s named=%s; auto comparator blocked until every box is fully identified",
                    listing.item_id, expected, len(recognized_items), named_count,
                )
                recognized_items = []
            if recognized_items and (search_budget is None or search_budget[0] > 0):
                pooled = self._live_comp_catalog(
                    listing, catalog, search_budget=search_budget
                )
        alert = evaluate_listing(
            listing,
            pooled,
            threshold_pct=self.settings.deal_threshold_pct,
            min_comparables=self.settings.min_comparables,
            min_price=self.settings.min_price_usd,
            ship_to_zip=self.settings.ship_to_zip,
            max_lot_usd=self.settings.max_lot_usd,
        )
        if not _auto_alert_sendable(alert):
            return None
        if alert and self.store.save_alert(alert):
            return alert
        return None

    def check_item(
        self,
        item_id: str,
        on_status: Callable[[str], None] | None = None,
    ) -> CheckResult:
        """On-demand /check: does not hold the auto-scan lock."""
        with self._check_lock:
            return self._check_item_unlocked(item_id, on_status=on_status)

    def _check_item_unlocked(
        self,
        item_id: str,
        on_status: Callable[[str], None] | None = None,
    ) -> CheckResult:
        def status(text: str) -> None:
            if on_status:
                on_status(text)

        listing, err = self.client.fetch_item(item_id)
        if listing is None:
            return CheckResult(error=err or "не открыл лот")
        recognized_items = enrich_listing(
            listing,
            downloader=self.client.download_bytes,
            openai_api_key=self.settings.openai_api_key,
            status=status,
        )
        # /check is an operator-facing recovery path. Photo OCR remains primary,
        # but when it returns zero complete identities, an explicit eBay title
        # pair such as `Zoro #327 & Reiju #1741` is safe to use as a deterministic
        # fallback. This fixes the old failure where vision_checked=True + no
        # members caused the comparator to be skipped before the title parser ran.
        if getattr(listing, "image_url", None):
            recognized_items = self._recover_missing_photo_names(listing, recognized_items)
            merged_items = self._canonicalize_photo_names(listing, recognized_items)
        else:
            merged_items = _merge_explicit_title_refs(listing, recognized_items)
        _apply_recognized_items(listing, merged_items)
        if not getattr(listing, "image_url", None) and not recognized_items and merged_items:
            status("⚠️ Фото отсутствует — использую точные имя+номер из eBay title")
        recognized_items = merged_items
        status(format_check_parsed_status(listing))
        catalog = self.store.catalog()
        pooled = catalog
        expected = getattr(listing, "vision_expected_count", None) or expected_figure_count(listing.title)
        named_recognized = [ref for ref in recognized_items if ref.name and ref.number]
        pictured_bundle_incomplete = bool(getattr(listing, "image_url", None) and expected and len(named_recognized) < expected)
        try:
            status(format_check_shipping_status(self.settings.ship_to_zip))
            if pictured_bundle_incomplete:
                missing = max(0, expected - len(named_recognized)) if expected else 0
                status(f"⚠️ Не удалось уверенно восстановить {missing} фигурку(и); сравниваю все распознанные отдельно")
            if named_recognized:
                # listing.members may contain number-only placeholders. The live
                # comparator must receive only canonical name+number pairs.
                listing.members = [ref.as_dict() for ref in named_recognized]
                if len(named_recognized) == 1:
                    listing.pop_name = named_recognized[0].name
                    listing.pop_number = named_recognized[0].number
                else:
                    listing.pop_name = None
                    listing.pop_number = None
                pooled = self._live_comp_catalog(listing, catalog, on_status=status)
                status(CHECK_COMPARE)
            else:
                status("⛔ Не удалось получить ни одной пары имя + номер для безопасного сравнения")
        except Exception:
            log.exception("Manual check comps failed for %s", listing.item_id)
        alert = evaluate_listing(
            listing,
            pooled,
            threshold_pct=self.settings.deal_threshold_pct,
            min_comparables=self.settings.min_comparables,
            min_price=self.settings.min_price_usd,
            ship_to_zip=self.settings.ship_to_zip,
            max_lot_usd=None,
            manual=True,
        )
        return CheckResult(listing=listing, alert=alert)

    def _live_comp_catalog(
        self,
        listing: Listing,
        catalog: list[Listing],
        *,
        search_budget: list[int] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> list[Listing]:
        extra: list[Listing] = []
        try:
            for query, ref in comparable_search_plan(listing):
                if search_budget is not None:
                    if search_budget[0] <= 0:
                        break
                    search_budget[0] -= 1
                if on_status:
                    on_status(format_check_bin_status(query))
                comps = self.client.fetch_live_comparables_for_query(
                    query,
                    exclude_id=listing.item_id,
                    pop=ref.number,
                    name=ref.name,
                )
                extra.extend(comps)
                # Keep the exact comps attached to the exact visual member that
                # produced this query. This avoids re-matching a live result by
                # title text later and makes bundle valuation deterministic.
                for member in listing.members or []:
                    m_name = str(member.get("name") or "").strip().casefold()
                    m_num = str(member.get("number") or member.get("pop") or "").strip()
                    if m_name == str(ref.name or "").strip().casefold() and m_num == str(ref.number or "").strip():
                        member["live_comps"] = [
                            {
                                "item_id": c.item_id,
                                "title": c.title,
                                "url": c.url,
                                "price": c.price,
                                "shipping": c.shipping_cost,
                                "shipping_label": c.shipping_label,
                                "landed": c.landed_cost(),
                                "comparison": c.comparison_cost(),
                            }
                            for c in comps
                        ]
                        break
        except Exception:
            log.exception("Live comparable search failed for %s", listing.item_id)
            return catalog
        if not extra:
            return catalog
        return merge_listings(catalog, extra)


def merge_listings(*groups: list[Listing]) -> list[Listing]:
    return _dedupe([item for group in groups for item in group])


def _auto_alert_sendable(alert: DealAlert | None) -> bool:
    """Auto-scan never sends «не с чем сравнить» or Total — cards."""
    if alert is None:
        return False
    if alert.kind in {"no_comps", "unparsed"}:
        return False
    if alert.listing.landed_cost() is None:
        return False
    return True


def _dedupe(items: list[Listing]) -> list[Listing]:
    seen: set[str] = set()
    out: list[Listing] = []
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        out.append(item)
    return out
