from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

# Hard OCR noise markers: unlike the general title STOPWORDS set, these reject the
# ENTIRE OCR line instead of merely stripping individual words. This prevents
# packaging/legal text such as "BOBBLE-HEAD FIGURINE" from becoming a character
# name simply because the real name appears on the same detected line.
OCR_NAME_STOPWORDS = {
    "BOBBLE-HEAD", "FIGURINE", "TETE", "OSCILLANTE", "WARNING", "CHOKING",
    "HAZARD", "ATTENTION", "DANGER", "PELIGRO", "ADVERTENCIA", "VINYL",
}

def is_ocr_name_noise(text: str) -> bool:
    """Return True when an OCR line is packaging/legal/system text, not a name.

    Matching is intentionally tolerant of OCR damage: e.g. ARNING still contains
    the distinctive WARN root, while HUTTENTIODANGER contains DANGER.
    """
    raw = str(text or "").upper()
    if not raw.strip():
        return False
    compact = re.sub(r"[^A-Z]", "", raw)
    # Exact word/phrase detection first.
    for marker in OCR_NAME_STOPWORDS:
        m = re.sub(r"[^A-Z]", "", marker)
        if m and m in compact:
            return True
    # Common truncations/gluings from tiny legal text. Keep these specific and
    # conservative so normal character names are not rejected.
    return any(root in compact for root in (
        "WARN", "CHOK", "HAZARD", "ATTENT", "DANGER",
        "PELIGR", "ADVERT", "FIGURIN", "FIGURA", "OSCILL", "OSCIL", "TETE",
        "BOBBLEHEAD", "VINYL",
    ))


def canonicalize_ocr_identity(
    raw_name: str | None,
    number: str | None,
    title: str | None = None,
    *,
    number_resolver=None,
) -> str:
    """Canonicalize a photo identity with a hard photo-number anchor.

    V44 contract:
      1) no photo-supplied number -> no identity;
      2) explicit title name+#same-number is the strongest canonical repair;
      3) an optional number resolver may supply a canonical name only after the
         photo number exists and no explicit same-number title pair was available;
      4) raw OCR is never an unchecked fallback;
      5) stop-word/legal/packaging OCR is rejected before fuzzy matching.
    """
    raw = str(raw_name or "").strip()
    pop = str(number or "").strip()
    title_text = str(title or "")
    if not pop:
        return ""

    def clean(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    # First use the robust explicit title number->name map. Do not rely on the
    # older parse_pop_refs representation here because it can truncate the first
    # word of a multi-item title (e.g. "Michael Corleone #404 Tony Soprano #1291").
    try:
        from funko_deal_bot.vision import _specific_name, _title_number_name_map
        explicit = clean(_specific_name(_title_number_name_map(title_text).get(pop, "")))
        explicit = re.sub(
            r"(?i)^(?:variety|lot|bundle|set|pack|collection)(?:\s+of\s+\d+)?\s+",
            "",
            explicit,
        ).strip()
    except Exception:
        explicit = ""
    # A clean name read from the photographed box is stronger than a title
    # canonicalization. If the OCR string is obviously far from an explicit same-
    # number title binding, however, treat it as OCR soup and use the exact binding.
    if raw and not is_ocr_name_noise(raw):
        try:
            from funko_deal_bot.vision import _specific_name
            direct = clean(_specific_name(raw))
        except Exception:
            direct = clean(raw)
        compact_direct = re.sub(r"[^a-z0-9]", "", direct.casefold())
        if explicit and direct:
            from difflib import SequenceMatcher
            a0 = re.sub(r"[^a-z0-9]", "", direct.casefold())
            b0 = re.sub(r"[^a-z0-9]", "", explicit.casefold())
            ratio0 = SequenceMatcher(None, a0, b0).ratio() if a0 and b0 else 0.0
            if ratio0 < 0.50 and len(compact_direct) >= 5:
                return explicit
        if direct and len(compact_direct) >= 4 and not is_ocr_name_noise(direct):
            return direct

    # Exact title binding is a repair only when the photographed name was
    # missing/noisy. The photo-provided Pop number is still mandatory.
    if explicit and not is_ocr_name_noise(explicit):
        return explicit

    # The optional resolver is still number-anchored: it cannot run unless the
    # photo number exists.
    if number_resolver:
        try:
            resolved = clean(number_resolver(pop))
        except Exception:
            resolved = ""
        if resolved and not is_ocr_name_noise(resolved):
            return resolved

    # Only a non-noisy OCR name may enter fuzzy matching. The comparator never gets
    # the raw string unless it matched a trusted title candidate.
    if raw and not is_ocr_name_noise(raw):
        clean_raw = clean(raw)
        # A clean, specific name read directly from the photographed nameplate is
        # stronger than an unrelated/garbled title candidate. Only use title
        # fuzzy matching when it actually supports the OCR string.
        try:
            from funko_deal_bot.vision import _specific_name
            direct = _specific_name(clean_raw)
        except Exception:
            direct = clean_raw
        if direct:
            compact_direct = re.sub(r"[^a-z0-9]", "", direct.casefold())
            if len(compact_direct) >= 4 and not is_ocr_name_noise(direct):
                # Keep the photographed spelling unless the title provides a
                # genuinely close candidate. This preserves e.g. SCARLET WITCH
                # when the title only mentions the broader Scarlet Witch item.
                pass
        try:
            from difflib import SequenceMatcher
            candidates = extract_title_character_names(title_text)
        except Exception:
            candidates = []
        a = re.sub(r"[^a-z0-9]", "", clean_raw.casefold())
        best = ""
        best_score = 0.0
        for candidate in candidates:
            candidate = clean(str(candidate))
            if not candidate or is_ocr_name_noise(candidate):
                continue
            b = re.sub(r"[^a-z0-9]", "", candidate.casefold())
            if not a or not b:
                continue
            score = SequenceMatcher(None, a, b).ratio()
            if a in b or b in a:
                score = max(score, min(len(a), len(b)) / max(len(a), len(b)))
            if score > best_score:
                best, best_score = candidate, score
        if best and best_score >= 0.70:
            return best
        # Do not discard a clear photo-read name merely because the title parser
        # produced weak/unrelated candidates. The photo is the source of truth.
        if direct and len(compact_direct) >= 4 and not is_ocr_name_noise(direct):
            return direct
    return ""


STOPWORDS = {
    "funko",
    "pop",
    "pops",
    "vinyl",
    "figure",
    "figures",
    "bobblehead",
    "bobble",
    "new",
    "nib",
    "nrfb",
    "mint",
    "box",
    "boxed",
    "damaged",
    "exclusive",
    "common",
    "official",
    "genuine",
    "authentic",
    "the",
    "and",
    "with",
    "mafia",
    "godfather",
    "movie",
    "tv",
    "television",
    "from",
    "for",
    "size",
    "inch",
    "in",
    "of",
    "a",
    "an",
    "lot",
    "lots",
    "mixed",
    "funkos",
    "funkon",
    "only",
    "at",
    "target",
    "random",
    "various",
    "assortment",
    "bulk",
    "bundle",
    "set",
    "pack",
    "collection",
    "collectible",
    "collectibles",
    "job",
    "wholesale",
    "mystery",
    "plus",
    "free",
    "shipping",
    "ship",
    "us",
    "uk",
    "seller",
    "hot",
    "topic",
    "game",
    "stop",
}

# Words that mean a multi-item listing. Do not treat "#479 Funko" / "#56 Pop"
# as a quantity: strip hash pop-numbers before matching "N funko" / "N pops".
# "1500 Pcs" / "3000 Pcs" / "1383 PCS" are production-run sizes, not figure counts.
# Years (SDCC 2024, Fundays 2026) and LE900 / Limited Edition 900 are not lots.
_BUNDLE_WORDS_RE = re.compile(
    r"\b(?:lots?|bundles?|job\s*lot|mystery\s*box)\b",
    re.I,
)
_HASH_POP_RE = re.compile(r"#\s*(?:[A-Za-z]{1,4}|\d{1,4})\b")
_PCS_RUN_RE = re.compile(r"\b\d+\s*(?:pcs?|pieces)\b", re.I)
_YEAR_RE = re.compile(r"\b(?:199\d|20[0-2]\d|203[0-5])\b")
_LE_RUN_RE = re.compile(
    r"\b(?:le|limited\s+edition)\s*#?\s*\d{2,5}\b",
    re.I,
)
_PROTECTOR_RE = re.compile(r"\bw\s*/\s*protectors?\b", re.I)
_AS_CHAR_RE = re.compile(
    r"\bas\s+(.+?)(?=\s+(?:le\d|\ble\b|limited|gid|sdcc|nycc|ecc|fundays|"
    r"exclusive|\d+\s*(?:pcs?|pieces)|#|w/|pop\b|vinyl|figure)|$)",
    re.I,
)
_BUNDLE_COUNT_RES = (
    re.compile(r"\blot of\s+(\d+)\b", re.I),
    re.compile(r"\bbundle of\s+(\d+)\b", re.I),
    re.compile(r"\bset of\s+(\d+)\b", re.I),
    re.compile(r"\bpack of\s+(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*-pack\b", re.I),
    re.compile(r"\b(\d+)\s+pack\b", re.I),
    re.compile(r"\b(\d+)\s+pops?\b", re.I),
    re.compile(r"\b(\d+)\s+funko\b", re.I),
    re.compile(r"\b(\d+)\s+figures?\b", re.I),
    re.compile(r"\b(\d+)\s+count\b", re.I),
)
_AFTER_LOT_QTY_RE = re.compile(
    r"\b(?:lot|bundle|set|pack)\s+of\s+\d+\b(.*)$",
    re.I,
)
_LOT_NAME_SPLIT_RE = re.compile(r"\s*(?:,|;|/|\+| and )\s*", re.I)
_LOT_TAIL_NOISE_RE = re.compile(
    r"(?i)\b(?:new\s+listing|funko|pops?|vinyl|figures?|toys|sealed|nib|nrfb)\b"
)
_LOT_NAME_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")
_LOT_NAME_SKIP = STOPWORDS | {
    "animation",
    "art",
    "avengers",
    "camepalace",
    "comics",
    "con",
    "disney",
    "edition",
    "endgame",
    "exc",
    "excl",
    "exclusive",
    "fundays",
    "galaxy",
    "gid",
    "guardians",
    "holiday",
    "hot",
    "le",
    "limited",
    "lotr",
    "marvel",
    "meteor",
    "bloody",
    "fate",
    "mini",
    "minis",
    "movies",
    "mutant",
    "ninja",
    "pc",
    "pcs",
    "pieces",
    "prlace",
    "protector",
    "rock",
    "rocks",
    "sdcc",
    "sealed",
    "series",
    "signed",
    "special",
    "star",
    "teenage",
    "tmnt",
    "topic",
    "toys",
    "turtles",
    "walmart",
    "wars",
    "wwe",
    "never",
    "opened",
    "open",
    "hocus",
    "pocus",
    "one",
    "piece",
    "roronoa",
    "death",
    "stranding",
    "duo",
    "bundle",
    "featuring",
    "playstation",
    "xbox",
    "nintendo",
    "switch",
    "both",
    "displayed",
    "out",
    "great",
    "condition",
    "minor",
    "wear",
    "see",
    "pictures",
    "only",
    "at",
    "target",
}
_NAME_NOISE = STOPWORDS | {
    "animation",
    "art",
    "camepalace",
    "avengers",
    "comics",
    "disney",
    "endgame",
    "exc",
    "excl",
    "exclusive",
    "fundays",
    "galaxy",
    "gid",
    "guardians",
    "holiday",
    "hot",
    "le",
    "limited",
    "listing",
    "marvel",
    "meteor",
    "bloody",
    "fate",
    "movies",
    "mutant",
    "ninja",
    "nycc",
    "prlace",
    "rock",
    "rocks",
    "sdcc",
    "sealed",
    "series",
    "signed",
    "special",
    "star",
    "teenage",
    "tmnt",
    "topic",
    "toys",
    "turtles",
    "walmart",
    "wars",
    "wwe",
}

NUMBER_PATTERNS = (
    re.compile(r"#\s*(\d{1,4})\b", re.I),
    re.compile(r"\bno\.?\s*(\d{1,4})\b", re.I),
    re.compile(r"\bpop!?[\s:-]+(\d{1,4})\b", re.I),
    re.compile(r"\b(\d{1,4})\s*$"),
)

_token_re = re.compile(r"[a-z0-9]+")
_price_re = re.compile(
    r"(?P<cur>US\s*\$|\$|£|€|EUR|USD|GBP)\s*(?P<val>\d{1,4}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)",
    re.I,
)


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title or "")
    text = text.replace("!", " ")
    return " ".join(text.lower().split())


def _is_year_number(value: int) -> bool:
    return 1990 <= value <= 2035


def extract_pop_number(title: str) -> str | None:
    """Numeric Pop # only (#426, #1107). Years and LE runs are ignored."""
    stripped = _LE_RUN_RE.sub(" ", title or "")
    stripped = _YEAR_RE.sub(" ", stripped)
    stripped = _PCS_RUN_RE.sub(" ", stripped)
    for pattern in NUMBER_PATTERNS:
        match = pattern.search(stripped)
        if not match:
            continue
        number = int(match.group(1))
        if number <= 0 or _is_year_number(number):
            continue
        return str(number)
    return None


def extract_pop_id(title: str) -> str | None:
    """Pop id as printed: #SE, #XX, #426, #1107."""
    match = re.search(r"#\s*([A-Za-z]{1,4}|\d{1,4})\b", title or "")
    if match:
        raw = match.group(1)
        if raw.isdigit():
            value = int(raw)
            if value <= 0 or _is_year_number(value):
                return None
            return str(value)
        return raw.upper()
    return extract_pop_number(title)


_SEARCH_SKIP = STOPWORDS | {
    "amazon",
    "animation",
    "art",
    "autographed",
    "camepalace",
    "boxed",
    "brand",
    "comics",
    "collector",
    "collectors",
    "condition",
    "disney",
    "edition",
    "exclusive",
    "exc",
    "excl",
    "figure",
    "flocked",
    "fye",
    "games",
    "gamestop",
    "gid",
    "glitch",
    "hot",
    "listing",
    "limited",
    "meteor",
    "bloody",
    "fate",
    "movies",
    "mutant",
    "ninja",
    "nycc",
    "prlace",
    "protector",
    "rock",
    "rocks",
    "sdcc",
    "series",
    "signature",
    "standard",
    "teenage",
    "television",
    "tmnt",
    "topic",
    "turtles",
    "vaulted",
    "vinyl",
}

_EXCLUSIVE_PATTERNS = (
    (re.compile(r"\bhot\s*topic\b", re.I), "Hot Topic"),
    (re.compile(r"\bgamestop\b|\bgame\s*stop\b", re.I), "GameStop"),
    (re.compile(r"\bwalmart\b", re.I), "Walmart"),
    (re.compile(r"\btarget\b", re.I), "Target"),
    (re.compile(r"\bchase\b", re.I), "Chase"),
    (re.compile(r"\bgitd\b|\bgid\b", re.I), "GID"),
    (re.compile(r"\bsdcc\b", re.I), "SDCC"),
    (re.compile(r"\bnycc\b", re.I), "NYCC"),
    (re.compile(r"\bexclusive\b", re.I), "Exclusive"),
)

_NAME_NUM_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9'-]{2,})\s*#\s*([A-Za-z]{1,4}|\d{1,4})\b"
)
_NUM_NAME_RE = re.compile(
    r"#\s*([A-Za-z]{1,4}|\d{1,4})\b\s+([A-Za-z][A-Za-z0-9'-]{2,})"
)
_BARE_NAME_NUM_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9'-]{2,})\s+(?:pop!?\s+)?(\d{2,4})\b",
    re.I,
)
_SPLIT_NAMES_RE = re.compile(r"\s*(?:,|/|&|\+|and)\s*", re.I)


@dataclass(frozen=True)
class PopRef:
    name: str | None = None
    number: str | None = None
    exclusive: str | None = None
    series: str | None = None

    def search_query(self) -> str | None:
        """One BIN query: Funko {Name} {number}, or Funko {Name}."""
        return format_comparable_query(self.name, self.number)

    def as_dict(self) -> dict:
        return asdict(self)


def extract_exclusive(title: str) -> str | None:
    for pattern, label in _EXCLUSIVE_PATTERNS:
        if pattern.search(title or ""):
            return label
    return None


def _clean_name_token(word: str) -> str | None:
    key = (word or "").lower().strip("-'")
    if not key or key.isdigit() or len(key) < 3:
        return None
    if any(ch.isdigit() for ch in key):
        return None
    if key in _SEARCH_SKIP or key in _LOT_NAME_SKIP or key in _NAME_NOISE:
        return None
    cleaned = word.strip("-'")
    if cleaned.isupper() and len(cleaned) > 2:
        return cleaned.title()
    return cleaned


_MAX_FIGURE_SEARCHES = 6
_BARE_SEARCH_WORDS = frozenset(
    {
        "art",
        "camepalace",
        "classic",
        "exc",
        "funkos",
    "funkon",
    "only",
    "at",
    "target",
        "globe",
        "hot",
        "magic",
        "gathering",
        "meteor",
        "mixed",
        "bloody",
        "fate",
        "mutant",
        "ninja",
        "prlace",
        "rock",
        "rocks",
        "series",
        "teenage",
        "topic",
        "turtles",
        "tmnt",
    }
)


def format_comparable_query(name: str | None, number: str | None = None) -> str | None:
    """One eBay query: Funko {First Last} {number} for the figure on this listing."""
    label = _name_for_search(name or "")
    if not label:
        return None
    pid = _valid_pop_id(number or "") if number else None
    if pid:
        return f"Funko {label} {pid}"
    return f"Funko {label}"


def _name_for_search(name: str) -> str | None:
    # Normalize a model/OCR name into up to four meaningful words and remove
    # accidental concatenations (`Tony SopranoPOP Silvio Dante` -> `Tony Soprano`).
    text = re.sub(r"(?i)(?<=\w)(?:pop|funko|vinyl|figure|figures|collectible|television|movies|games?)(?=\w)", " ", name or "")
    words: list[str] = []
    seen: set[str] = set()
    for word in _LOT_NAME_WORD_RE.findall(text):
        token = _clean_name_token(word)
        if not token or token.lower() in _BARE_SEARCH_WORDS:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        words.append(token)
        if len(words) >= 4:
            break
    if not words:
        return None
    return " ".join(words)


_MIN_COMPLETION_LEN = 3


def _alpha_key(token: str) -> str:
    return "".join(ch for ch in (token or "").lower() if ch.isalpha())


def is_longer_name_completion(short: str, longer: str) -> bool:
    """True when `longer` is the same token with a longer suffix (Butcher → Butcherson)."""
    a = _alpha_key(short)
    b = _alpha_key(longer)
    if len(a) < _MIN_COMPLETION_LEN or len(b) <= len(a):
        return False
    return b.startswith(a)


def _name_tokens_all(text: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for word in _LOT_NAME_WORD_RE.findall(text or ""):
        token = _clean_name_token(word)
        if not token or token.lower() in _BARE_SEARCH_WORDS:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(token)
    return words


def name_mentioned(name: str, title: str) -> bool:
    """Word-boundary name match. Butcher is not Butcherson (no substring / prefix)."""
    raw = (name or "").strip()
    blob = title or ""
    if not raw or not blob:
        return False
    if re.search(rf"\b{re.escape(raw)}\b", blob, re.I):
        return True
    tokens = [
        tok
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", raw)
        if len(tok) >= 3
    ]
    if not tokens:
        return False
    long_tokens = [tok for tok in tokens if len(tok) >= 6]
    need = long_tokens or tokens
    for tok in need:
        if not re.search(rf"\b{re.escape(tok)}\b", blob, re.I):
            return False
        for other in _name_tokens_all(blob):
            if is_longer_name_completion(tok, other):
                return False
    return True


def complete_name_from_ocr(
    name: str | None,
    ocr_text: str | None,
    *,
    title: str | None = None,
    from_title: bool = False,
) -> str | None:
    """Replace a title token with a longer box/OCR completion. Never shorten a surname.

    Title `Billy Butcher` + box `BILLY BUTCHERSON` → `Billy Butcherson`.
    Unrelated OCR (Meteor/Rock/Hocus) is ignored. Numbered BIN uses the fuller last token.
    """
    # Numbered singles: complete the 1–2 title tokens next to the # (Billy Butcher).
    # Unnumbered / already-merged refs keep their name (Maximus, not Maximus Gladiator).
    if from_title and title and extract_pop_id(title):
        tokens = comparable_search_tokens(title, limit=2) or _name_tokens_all(name or "")
    else:
        tokens = _name_tokens_all(name or "")
        if not tokens and title:
            tokens = comparable_search_tokens(title, limit=2)
    ocr_tokens = _name_tokens_all(ocr_text or "")
    if not tokens:
        return None
    completed: list[str] = []
    for token in tokens:
        best = token
        candidates: list[str] = []
        for other in ocr_tokens:
            if not is_longer_name_completion(token, other):
                continue
            suffix = _alpha_key(other)[len(_alpha_key(token)) :]
            # Skip glued FirstLast ("Tony"+"Soprano" → Tonysoprano) when Last is its own OCR token.
            if suffix and any(_alpha_key(item) == suffix for item in ocr_tokens if item is not other):
                continue
            # Skip show plurals: Soprano is on the box, Sopranos is the series.
            if any(
                _alpha_key(item) + "s" == _alpha_key(other)
                or _alpha_key(item) + "es" == _alpha_key(other)
                for item in ocr_tokens
                if item is not other
            ):
                continue
            candidates.append(other)
        if candidates:
            # Soprano beats Sopranos; Butcherson is the only Butcher completion.
            best = min(candidates, key=lambda item: (len(_alpha_key(item)), item.lower()))
        completed.append(best)
    if len(completed) > 2:
        completed = [completed[0], completed[-1]]
    return " ".join(completed[:2])


def _number_near_name(text: str, name: str | None) -> str | None:
    """Box/OCR Pop # next to the last name token: `SOPRANO 1295` / `#1295 SOPRANO`."""
    label = _name_for_search(name or "")
    if not label:
        return None
    token = label.split()[-1]
    if len(token) < 3:
        return None
    pattern = re.compile(
        rf"\b{re.escape(token)}\b\s*#?\s*(\d{{2,4}})\b|"
        rf"\b(\d{{2,4}})\b\s*#?\s*\b{re.escape(token)}\b",
        re.I,
    )
    match = pattern.search(text or "")
    if not match:
        return None
    return _valid_pop_id(match.group(1) or match.group(2))


def complete_pop_ref_from_ocr(
    ref: PopRef,
    ocr_text: str | None,
    *,
    title: str | None = None,
    title_number: str | None = None,
    from_title: bool = False,
) -> PopRef:
    name = complete_name_from_ocr(
        ref.name, ocr_text, title=title, from_title=from_title
    )
    number = title_number or ref.number or _number_near_name(ocr_text or "", name or ref.name)
    return PopRef(
        name=name or ref.name,
        number=number,
        exclusive=ref.exclusive,
    )


def _fold_ascii(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch)
    )


def comparable_search_tokens(title: str, *, limit: int = 2) -> list[str]:
    """1–2 character tokens from the title. Numbered singles: words left of #1477.

    Skips Vinyl/Figure/Standard/Meteor/Rock/Series/Art. OCR watermarks never belong here.
    """
    folded = _fold_ascii(title or "")
    folded = re.sub(r"\([^)]*\)", " ", folded)
    number = extract_pop_number(folded)
    pid = number or extract_pop_id(folded)
    left = folded
    if pid:
        match = re.search(
            rf"#\s*{re.escape(pid)}\b|(?<![\w#]){re.escape(pid)}(?!\d)",
            folded,
            re.I,
        )
        if match:
            left = folded[: match.start()]
    words = _LOT_NAME_WORD_RE.findall(left)
    if pid:
        index = len(words) - 1
        while index >= 0 and _clean_name_token(words[index]) is None:
            index -= 1
        collected: list[str] = []
        while index >= 0 and len(collected) < limit:
            token = _clean_name_token(words[index])
            if token is None or token.lower() in _BARE_SEARCH_WORDS:
                break
            collected.append(token)
            index -= 1
        collected.reverse()
        # "Wonka Noodle Vinyl Figure #1477" → drop the Vinyl/Figure glue word.
        if len(collected) >= 2:
            last = collected[-1]
            if re.search(
                rf"\b{re.escape(last)}\s+(?:vinyl|figure|figures|standard)\b",
                left,
                re.I,
            ):
                collected = collected[:-1]
        return collected
    collected = []
    for word in words:
        token = _clean_name_token(word)
        if token is None or token.lower() in _BARE_SEARCH_WORDS:
            if collected:
                break
            continue
        collected.append(token)
        if len(collected) >= limit:
            break
    return collected


def _name_near_number(text: str, number: str) -> str | None:
    tokens = comparable_search_tokens(text, limit=2)
    if tokens:
        return " ".join(tokens)
    pattern = re.compile(
        rf"\b([A-Za-z][A-Za-z'-]{{2,}}(?:\s+[A-Za-z][A-Za-z'-]{{2,}})?)\s+#?\s*"
        rf"{re.escape(number)}\b",
        re.I,
    )
    match = pattern.search(text or "")
    if not match:
        return None
    return _name_for_search(match.group(1))


def _content_token_count(text: str) -> int:
    n = 0
    for word in _LOT_NAME_WORD_RE.findall(_identity_text(text)):
        key = word.lower().strip("-'")
        if key in STOPWORDS or len(key) < 3 or key.isdigit() or any(ch.isdigit() for ch in key):
            continue
        n += 1
    return n


def _best_character_name(names: list[str], *, text: str = "", number: str | None = None) -> str | None:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        token = _name_for_search(raw)
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(token)
    if number:
        near = _name_near_number(text, number)
        if near:
            return near
        if cleaned:
            return _name_for_search(cleaned[-1]) or cleaned[-1]
        return None
    if not cleaned:
        return None
    return " ".join(cleaned[:2])


def _valid_pop_id(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        if value <= 0 or _is_year_number(value):
            return None
        return str(value)
    if text.isalpha() and 1 <= len(text) <= 4:
        return text.upper()
    return None


def _identity_text(title: str) -> str:
    text = title or ""
    text = _PCS_RUN_RE.sub(" ", text)
    text = _LE_RUN_RE.sub(" ", text)
    text = _YEAR_RE.sub(" ", text)
    return text


def extract_all_pop_ids(title: str) -> list[str]:
    stripped = _identity_text(title)
    found: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"#\s*([A-Za-z]{1,4}|\d{1,4})\b", stripped):
        pid = _valid_pop_id(match.group(1))
        if pid and pid.lower() not in seen:
            seen.add(pid.lower())
            found.append(pid)
    if found:
        return found
    number = extract_pop_number(title)
    return [number] if number else []


def extract_name_number_pairs(title: str) -> list[tuple[str, str]]:
    stripped = _identity_text(title)
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str | None, number: str | None) -> None:
        clean = _clean_name_token(name or "")
        pid = _valid_pop_id(number or "")
        if not clean or not pid:
            return
        key = f"{clean.lower()}|{pid.lower()}"
        if key in seen:
            return
        seen.add(key)
        pairs.append((clean, pid))

    for match in _NAME_NUM_RE.finditer(stripped):
        add(match.group(1), match.group(2))
    for match in _NUM_NAME_RE.finditer(stripped):
        pid = _valid_pop_id(match.group(1))
        if pid and any(existing == pid for _name, existing in pairs):
            continue
        add(match.group(2), match.group(1))
    for match in _BARE_NAME_NUM_RE.finditer(stripped):
        pid = _valid_pop_id(match.group(2))
        if pid and any(existing == pid for _name, existing in pairs):
            continue
        add(match.group(1), match.group(2))
    return pairs


def extract_title_character_names(title: str) -> list[str]:
    as_char = extract_as_character(title)
    if as_char:
        return [as_char]
    lot_names = [str(row["name"]) for row in _lot_members_from_qty_title(title)]
    if lot_names:
        return lot_names
    stripped = _identity_text(title)
    stripped = re.sub(r"(?i)\bfunko\b|\bpop!?\b|#\s*[A-Za-z0-9]{1,4}\b", " ", stripped)
    parts = [p for p in _SPLIT_NAMES_RE.split(stripped) if p.strip()]
    names: list[str] = []
    seen: set[str] = set()
    if len(parts) >= 2:
        for part in parts:
            token = None
            for word in _LOT_NAME_WORD_RE.findall(part):
                token = _clean_name_token(word)
                if token:
                    break
            if token and token.lower() not in seen:
                seen.add(token.lower())
                names.append(token)
        if len(names) >= 2:
            return names
    for word in _LOT_NAME_WORD_RE.findall(stripped):
        token = _clean_name_token(word)
        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())
        names.append(token)
    return names


def parse_pop_refs(text: str) -> list[PopRef]:
    """Name, number, exclusive from a title or OCR dump. Number-only is incomplete."""
    exclusive = extract_exclusive(text)
    qty = _lot_members_from_qty_title(text)
    if len(qty) >= 2:
        return [
            PopRef(name=str(row["name"]), number=row.get("pop"), exclusive=exclusive)
            for row in qty[:_MAX_FIGURE_SEARCHES]
        ]
    as_char = extract_as_character(text)
    pairs = extract_name_number_pairs(text)
    numbers = extract_all_pop_ids(text)
    names = extract_title_character_names(text)
    split_lot = bool(_SPLIT_NAMES_RE.search(_identity_text(text))) and len(names) >= 2
    bundle = is_bundle(text)
    if as_char:
        number = pairs[0][1] if pairs else (numbers[0] if numbers else None)
        return [PopRef(name=as_char, number=number, exclusive=exclusive)]
    # Name+# pairs first so "MAXIMUS 860 STORM 80" is two figures, not one soup.
    if len(pairs) >= 2:
        return [
            PopRef(name=_name_for_search(n) or n, number=num, exclusive=exclusive)
            for n, num in pairs[:_MAX_FIGURE_SEARCHES]
        ]
    if len(pairs) == 1:
        name = _name_for_search(pairs[0][0]) or pairs[0][0]
        return [PopRef(name=name, number=pairs[0][1], exclusive=exclusive)]
    if len(numbers) >= 2:
        refs = []
        for i, number in enumerate(numbers[:_MAX_FIGURE_SEARCHES]):
            raw = names[i] if i < len(names) else (names[-1] if names else None)
            name = (_name_for_search(raw) or raw) if raw else None
            refs.append(PopRef(name=name, number=number, exclusive=exclusive))
        return refs
    if len(numbers) == 1 and not bundle:
        tokens = comparable_search_tokens(text, limit=2)
        name = " ".join(tokens) if tokens else None
        if not name:
            name = _best_character_name(names, text=text, number=numbers[0])
        return [PopRef(name=name, number=numbers[0], exclusive=exclusive)]
    if len(names) >= 2:
        soup = _content_token_count(text) >= 4
        if bundle or split_lot or not soup:
            return [
                PopRef(name=_name_for_search(name) or name, number=None, exclusive=exclusive)
                for name in names[:_MAX_FIGURE_SEARCHES]
            ]
    if names:
        name = _best_character_name(names, text=text)
        return [PopRef(name=name, number=None, exclusive=exclusive)]
    if numbers:
        return [PopRef(name=None, number=numbers[0], exclusive=exclusive)]
    if exclusive:
        return [PopRef(exclusive=exclusive)]
    return []


def merge_pop_refs(title_refs: list[PopRef], photo_refs: list[PopRef]) -> list[PopRef]:
    """Photos win for 2+ figures; one complete box-art pop beats extra title words."""

    def combine(left: PopRef, right: PopRef | None) -> PopRef:
        if right is None:
            return left
        return PopRef(
            name=left.name or right.name,
            number=left.number or right.number,
            exclusive=left.exclusive or right.exclusive,
        )

    complete_photo = [item for item in photo_refs if item.name and item.number]
    numbered_photo = [item for item in photo_refs if item.number]
    complete_title = [item for item in title_refs if item.name and item.number]
    named_title = [item for item in title_refs if item.name]
    numbered_title = [item for item in title_refs if item.number]
    if len(named_title) >= 2 and (len(complete_photo) >= 2 or len(numbered_photo) >= 2):
        # Lot names came from the title. Attach box numbers; do not replace with show titles (Sopranos).
        photos = complete_photo or numbered_photo or photo_refs
        out: list[PopRef] = []
        used: set[int] = set()
        for ref in title_refs[:_MAX_FIGURE_SEARCHES]:
            last = ((_name_for_search(ref.name) or ref.name or "").split() or [""])[-1].lower()
            match = None
            for i, photo in enumerate(photos):
                if i in used:
                    continue
                plast = ((_name_for_search(photo.name) or photo.name or "").split() or [""])[-1].lower()
                if last and plast and (plast.startswith(last) or last.startswith(plast)):
                    match = photo
                    used.add(i)
                    break
            if match is None:
                for i, photo in enumerate(photos):
                    if i not in used:
                        match = photo
                        used.add(i)
                        break
            out.append(combine(ref, match))
        return out
    if len(complete_photo) >= 2 or len(numbered_photo) >= 2:
        base, extra = complete_photo or numbered_photo, title_refs
    elif (
        len(complete_photo) == 1
        and len(named_title) >= 2
        and not any(item.number for item in title_refs)
    ):
        # Title is extra words (Maximus Gladiator), photo is the one Pop.
        extra = next((item for item in named_title if item.name), None)
        return [combine(complete_photo[0], extra)]
    elif len(named_title) >= 2:
        # Lot already identified in the title — don't collapse to one numbered name.
        base, extra = title_refs, photo_refs
    elif len(complete_title) == 1 or (len(numbered_title) == 1 and len(named_title) <= 1):
        extra = photo_refs[0] if photo_refs else None
        if extra and extra.number and numbered_title and extra.number != numbered_title[0].number:
            extra = PopRef(name=extra.name, number=None, exclusive=extra.exclusive)
        return [combine(complete_title[0] if complete_title else numbered_title[0], extra)]
    elif len(complete_photo) == 1 or len(numbered_photo) == 1:
        photo = (complete_photo or numbered_photo)[0]
        extra = title_refs[0] if title_refs else None
        return [combine(photo, extra)]
    elif photo_refs and title_refs:
        base, extra = title_refs, photo_refs
    else:
        base, extra = (photo_refs or title_refs), []

    if not base:
        return extra

    if len(base) == 1 and extra:
        filled = combine(base[0], extra[0])
        if len(extra) >= 2 and not (filled.name and filled.number):
            return [combine(item, base[0]) for item in extra]
        return [filled]
    out: list[PopRef] = []
    for i, item in enumerate(base[:_MAX_FIGURE_SEARCHES]):
        other = extra[i] if i < len(extra) else (extra[0] if len(extra) == 1 else None)
        out.append(combine(item, other))
    return out


def comparable_search_query(title: str) -> str | None:
    """eBay query: Funko {Name} {number}. Never Rock/Meteor/Series/Art, never Funko Pop."""
    tokens = comparable_search_tokens(title, limit=2)
    number = extract_pop_number(title) or extract_pop_id(title)
    query = format_comparable_query(" ".join(tokens) if tokens else None, number)
    if query:
        return query
    for ref in parse_pop_refs(title):
        fallback = ref.search_query()
        if fallback:
            return fallback
    return None


def comparable_search_query_from_ref(ref: PopRef | dict | None) -> str | None:
    if ref is None:
        return None
    if isinstance(ref, dict):
        ref = PopRef(
            name=ref.get("name"),
            number=ref.get("number"),
            exclusive=ref.get("exclusive"),
        )
    return format_comparable_query(ref.name, ref.number)


def is_multi_pop(title: str, refs: list[PopRef] | None = None) -> bool:
    """Conservative bundle detection. Generic franchise words are never a lot."""
    parsed = refs if refs is not None else parse_pop_refs(title)
    if is_bundle(title):
        return True
    # Multiple explicitly numbered figures are strong evidence.
    numbered = [item for item in parsed if item.number]
    if len(numbered) >= 2:
        return True
    # Two named refs without numbers are only a bundle when the title explicitly
    # signals a lot/set/pack; otherwise titles such as "Dungeons & Dragons"
    # must remain a single franchise/character listing.
    return False


def member_search_query(name: str, pop: str | None = None) -> str | None:
    """BIN query for one figure: Funko {Name} {number}."""
    return format_comparable_query(name, pop)


def extract_as_character(title: str) -> str | None:
    """'Freddy Funko as Cyclops' / 'as Gill-Man' → character after 'as'."""
    match = _AS_CHAR_RE.search(title or "")
    if not match:
        return None
    name = " ".join(match.group(1).split()).strip(" -")
    if len(name) < 3:
        return None
    return name


def _strip_non_lot_noise(title: str) -> str:
    text = title or ""
    text = _HASH_POP_RE.sub(" ", text)
    text = _PCS_RUN_RE.sub(" ", text)
    text = _LE_RUN_RE.sub(" ", text)
    text = _PROTECTOR_RE.sub(" ", text)
    text = _YEAR_RE.sub(" ", text)
    return text


def significant_tokens(title: str) -> set[str]:
    tokens = set(_token_re.findall(normalize_title(title)))
    cleaned: set[str] = set()
    for token in tokens:
        if token in STOPWORDS:
            continue
        if token.isdigit() and len(token) > 4:
            continue
        if len(token) < 3 and not token.isdigit():
            continue
        cleaned.add(token)
    pop_id = extract_pop_id(title)
    if pop_id:
        cleaned.add(pop_id.lower())
    return cleaned


def product_key(title: str) -> str:
    number = extract_pop_id(title) or "x"
    tokens = sorted(t for t in significant_tokens(title) if not t.isdigit())
    head = "-".join(tokens[:4]) or "unknown"
    return f"{head}:{number}"


def token_similarity(a: str, b: str) -> float:
    left = significant_tokens(a)
    right = significant_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def extract_bundle_count(title: str) -> int | None:
    """How many figures the title claims.

    Ignores pop # (#SE / #426), Pcs runs, LE900 / Limited Edition 900,
    years 1990–2035 (SDCC 2024, Fundays 2026), and 'w/ Protector'.
    """
    stripped = _strip_non_lot_noise(title)
    for pattern in _BUNDLE_COUNT_RES:
        match = pattern.search(stripped)
        if not match:
            continue
        count = int(match.group(1))
        if _is_year_number(count):
            continue
        if count >= 2:
            return count
    return None


def extract_lot_character_names(title: str) -> list[str]:
    """Character names after 'lot of N' / listed names (no Pop #)."""
    return [str(row["name"]) for row in extract_lot_members(title)]


def extract_lot_members(title: str) -> list[dict]:
    """Who is in the lot: [{name, pop}] from lot-of-N, two names, or commas."""
    members = _lot_members_from_qty_title(title)
    if members:
        return members
    refs = parse_pop_refs(title)
    named = [item for item in refs if item.name]
    if len(named) >= 2:
        return [{"name": item.name, "pop": item.number} for item in named]
    return []


def _paired_names_hash_numbers(title: str) -> list[dict]:
    """Parse titles like `Doctor Octopus & Black Cat (#957, #958)`.

    The number list in parentheses belongs positionally to the names immediately
    before it.  This must run before the generic lot parser so suffix metadata such
    as `Target Exclusive` can never become a character name.
    """
    raw = title or ""
    match = re.search(
        r"(?P<names>[^()]{2,160}?)\s*\(\s*(?P<numbers>#\s*\d{1,4}(?:\s*[,;/&+]\s*#?\s*\d{1,4})+)\s*\)",
        raw,
        re.I,
    )
    if not match:
        return []
    name_text = re.sub(r"(?i)\b(?:funko|pop!?|vinyl)\b", " ", match.group("names"))
    name_text = re.sub(r"^[^A-Za-z]+", " ", name_text)
    name_text = _clean_lot_tail(name_text)
    # Split only on explicit name separators. Commas inside the parenthetical
    # belong to the Pop numbers, not to the names.
    parts = [p.strip(" -,:;.") for p in re.split(r"\s*(?:&|\+|/|\band\b)\s*", name_text, flags=re.I) if p.strip()]
    numbers = [m.group(1) for m in re.finditer(r"#?\s*(\d{1,4})\b", match.group("numbers"))]
    clean_numbers = []
    for raw_num in numbers:
        value = int(raw_num)
        if value <= 0 or _is_year_number(value):
            continue
        clean_numbers.append(str(value))
    if len(parts) < 2 or len(parts) != len(clean_numbers):
        return []
    members = []
    for name, pop in zip(parts, clean_numbers):
        words = _lot_name_words(name)
        if not words:
            return []
        members.append({"name": " ".join(words), "pop": pop})
    return _dedupe_lot_members(members)


def _lot_members_from_qty_title(title: str) -> list[dict]:
    """[{name, pop}] from explicit bundle syntax / lot-of-N. No photo."""
    paired = _paired_names_hash_numbers(title)
    if paired:
        return paired
    raw = title or ""
    match = _AFTER_LOT_QTY_RE.search(raw)
    tail = match.group(1) if match else ""
    tail = _clean_lot_tail(tail)
    if not tail:
        # Explicit `Name #123 & Name #456` / `Name 123 & Name 456` syntax is a
        # bundle even without the words `lot of`. Only activate this fallback
        # when at least two explicit members have numeric Pop-looking IDs.
        explicit_clean = _clean_lot_tail(raw)
        explicit_raw = [p.strip() for p in re.split(r"\s*(?:&|\+|;|\band\b)\s*", explicit_clean, flags=re.I) if p.strip()]
        explicit_with_numbers = [
            part for part in explicit_raw
            if re.search(r"#\s*\d{1,4}|\b\d{2,4}\s*$", part)
        ]
        if len(explicit_with_numbers) >= 2:
            tail = raw
    if not tail and "," in raw:
        tail = _clean_lot_tail(raw)
    if not tail:
        return []
    explicit_chunks = [p.strip() for p in re.split(r"\s*(?:&|\+|;|\band\b)\s*", _clean_lot_tail(tail), flags=re.I) if p.strip()]
    if len(explicit_chunks) >= 2 and sum(1 for chunk in explicit_chunks if re.search(r"(?:#\s*\d{1,4}|\b\d{2,4}\s*$)", chunk)) >= 2:
        members = []
        for chunk in explicit_chunks:
            row = _member_from_chunk(chunk)
            if row:
                members.append(row)
        if len(members) >= 2:
            return _dedupe_lot_members(members)
    if _LOT_NAME_SPLIT_RE.search(tail):
        members = []
        for chunk in _LOT_NAME_SPLIT_RE.split(tail):
            row = _member_from_chunk(chunk)
            if row:
                members.append(row)
        return _dedupe_lot_members(members)
    return _members_from_tokens(tail)


def _clean_lot_tail(text: str) -> str:
    cleaned = _LOT_TAIL_NOISE_RE.sub(" ", text or "")
    return " ".join(cleaned.split())


def _member_from_chunk(chunk: str) -> dict | None:
    text = (chunk or "").strip(" -")
    if not text:
        return None
    pop: str | None = None
    hashed = re.search(r"#\s*(\d{1,4})\b", text)
    if hashed:
        number = int(hashed.group(1))
        if not _is_year_number(number):
            pop = str(number)
        text = f"{text[: hashed.start()]} {text[hashed.end() :]}"
    else:
        trailing = re.search(r"\b(\d{2,4})\s*$", text)
        if trailing:
            number = int(trailing.group(1))
            if not _is_year_number(number):
                pop = str(number)
                text = text[: trailing.start()]
    words = _lot_name_words(text)
    if not words:
        return None
    return {"name": " ".join(words), "pop": pop}


def _lot_name_words(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for word in _LOT_NAME_WORD_RE.findall(text or ""):
        key = word.lower().strip("-'")
        if key in _LOT_NAME_SKIP or len(key) < 3 or key.isdigit() or key in seen:
            continue
        seen.add(key)
        names.append(word)
    return names


def _members_from_tokens(tail: str) -> list[dict]:
    members: list[dict] = []
    buf: list[str] = []
    numbered = False
    for token in _LOT_NAME_WORD_RE.findall(tail or ""):
        if token.isdigit() and 2 <= len(token) <= 4:
            value = int(token)
            if _is_year_number(value):
                continue
            if buf:
                members.append({"name": " ".join(buf), "pop": str(value)})
                buf = []
                numbered = True
            continue
        key = token.lower().strip("-'")
        if key in _LOT_NAME_SKIP or len(key) < 3:
            continue
        buf.append(token)
    if buf:
        if numbered:
            members.extend(_group_leftover_lot_names(buf))
        else:
            members.extend({"name": word, "pop": None} for word in buf)
    return _dedupe_lot_members(members)


def _group_leftover_lot_names(words: list[str]) -> list[dict]:
    """After a numbered name, leftover 'Silvio Dante Christopher' → 2-word + single."""
    out: list[dict] = []
    index = 0
    total = len(words)
    while index < total:
        left = total - index
        if left == 3:
            out.append({"name": f"{words[index]} {words[index + 1]}", "pop": None})
            out.append({"name": words[index + 2], "pop": None})
            break
        if left >= 2 and left % 2 == 0:
            out.append({"name": f"{words[index]} {words[index + 1]}", "pop": None})
            index += 2
            continue
        out.append({"name": words[index], "pop": None})
        index += 1
    return out


def _dedupe_lot_members(members: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for row in members:
        name = str(row.get("name") or "").strip()
        pop = row.get("pop")
        pop_s = str(pop) if pop else None
        key = (name.lower(), pop_s)
        if not name or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "pop": pop_s})
    return out


def is_bundle(title: str) -> bool:
    if extract_bundle_count(title) is not None:
        return True
    text = normalize_title(title or "")
    return _BUNDLE_WORDS_RE.search(text) is not None


# Loose / incomplete singles are not boxed BIN comps.
_INCOMPLETE_COMP_RE = re.compile(
    r"(?i)(?:"
    r"\bno\s*box\b|\bnobox\b|\bunboxed\b|\bout\s*of\s*box\b|\boob\b|"
    r"\bloose\b|\bincomplete\b|\bwithout\s+(?:the\s+)?box\b|"
    r"\bw\s*/\s*o\s*box\b|\bmissing\s+box\b|\bempty\s+box\b|"
    r"\bbox\s+only\b|\bno\s+packaging\b|\bopened\s+box\b"
    r")"
)
_OBO_RE = re.compile(r"(?i)\bor\s+best\s+offer\b|\bbest\s+offer\b|\bobo\b")
_BIN_MARK_RE = re.compile(r"(?i)\bbuy\s*it\s*now\b|\bBIN\b")
_OCR_GARBAGE_RE = re.compile(
    r"(?i)\b(?:camepalace|prlace|yourself|cuphead\d{2,})\b"
)
_JUNK_MERCH_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:t-?shirts?|shirts?|hoodies?|posters?|mugs?|keychains?|stickers?)\b|"
    r"\b(?:protector|protectors|popshield|case|cases|display\s+case|display\s+box|box\s+protector|protective\s+(?:case|cover|protector)|sleeve|stack)\b|"
    r"\b(?:protector\s+only|case\s+only|insert\s+only|compatible\s+with\s+funko|for\s+funko\s+pop)\b|"
    r"\bpins?\b"
    r")"
)
_NON_BIN_TYPES = frozenset(
    {
        "auction",
        "bid",
        "offer",
        "auctionwithbin",
        "auction_with_bin",
    }
)
_POP_IN_TITLE_RE = re.compile(
    r"(?:#\s*|pop!?\s+|no\.?\s+)(?P<pop>[A-Za-z]{1,4}|\d{1,4})\b",
    re.I,
)


def is_incomplete_comp_title(title: str) -> bool:
    """No box / loose / incomplete — not a complete boxed Funko Pop."""
    return bool(_INCOMPLETE_COMP_RE.search(title or ""))


def is_obo_without_bin(text: str) -> bool:
    """SRP 'or Best Offer' without Buy It Now / BIN."""
    raw = text or ""
    if not _OBO_RE.search(raw):
        return False
    return _BIN_MARK_RE.search(raw) is None


def is_ocr_garbage_title(title: str) -> bool:
    return bool(_OCR_GARBAGE_RE.search(title or ""))


def is_unboxed_comp(title: str) -> bool:
    return is_incomplete_comp_title(title)


def is_junk_comp(title: str) -> bool:
    """Shirt/poster/OCR garbage / OBO-only — not a boxed BIN Pop comp."""
    return (
        is_ocr_garbage_title(title)
        or bool(_JUNK_MERCH_RE.search(title or ""))
        or is_obo_without_bin(title)
    )


def is_bin_listing_type(listing_type: str | None) -> bool:
    kind = (listing_type or "unknown").strip().lower().replace(" ", "_")
    return kind not in _NON_BIN_TYPES


def infer_listing_type(title: str, price_text: str = "") -> str:
    """Infer fixed-price vs auction from an eBay SRP card.

    Best Offer listings are still valid fixed-price comparables because the
    displayed price is the seller's current Buy It Now/asking price.
    """
    blob = f"{title or ''} {price_text or ''}"
    if _OBO_RE.search(blob):
        return "obo"
    if _BIN_MARK_RE.search(blob):
        return "bin"
    return "unknown"


def title_has_pop(title: str, pop: str | None) -> bool:
    """Same Pop # as the search (Funko Pop 1069 … / #1069). Missing # is not a match."""
    want = str(pop or "").strip()
    if not want:
        return True
    found = extract_pop_id(title)
    if found:
        return found.lower() == want.lower()
    for match in _POP_IN_TITLE_RE.finditer(title or ""):
        pid = _valid_pop_id(match.group("pop"))
        if pid and pid.lower() == want.lower():
            return True
    return False


def is_usable_comp(
    title: str,
    *,
    pop: str | None = None,
    item_id: str | None = None,
    exclude_id: str | None = None,
    listing_type: str = "",
) -> bool:
    """Boxed BIN single of the same #. Skip junk, lots, and the subject listing."""
    if exclude_id and item_id and str(item_id) == str(exclude_id):
        return False
    if is_bundle(title):
        return False
    if len(extract_all_pop_ids(title)) >= 2:
        return False
    if is_incomplete_comp_title(title) or is_junk_comp(title):
        return False
    kind = (listing_type or "").strip().lower()
    if not is_bin_listing_type(kind):
        return False
    if _OBO_RE.search(title or ""):
        return False
    if pop and not title_has_pop(title, pop):
        return False
    return True


def name_tokens(title: str) -> set[str]:
    tokens: set[str] = set()
    for token in significant_tokens(title):
        if token.isdigit() or len(token) < 4 or token in _NAME_NOISE:
            continue
        tokens.add(token)
    return tokens


def short_pop_name(title: str) -> str:
    text = re.sub(r"(?i)\bfunko\b|\bpop!?\b|#\s*\d{1,4}\b", " ", title or "")
    text = " ".join(text.split())
    return (text[:48] or (title or "")[:48]).strip()


_free_ship_re = re.compile(
    r"\bfree\s+(?:international\s+)?(?:shipping|delivery|postage)\b|\bshipping:\s*free\b",
    re.I,
)
_ship_cost_re = re.compile(
    r"(?:\+|plus\s*)?(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)\s*"
    r"(?:estimated\s+)?(?:shipping|delivery|postage)",
    re.I,
)
# Item page after ZIP Update: "US $12.34 USPS Ground Advantage"
_carrier_rate_re = re.compile(
    r"(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)\s+"
    r"(?:USPS|UPS|FedEx|DHL|Priority\s+Mail|Ground\s+Advantage)",
    re.I,
)
# "Shipping: US $12.34" without requiring the word shipping after the amount
_shipping_colon_re = re.compile(
    r"(?:shipping|delivery|postage)\s*[:\-–]\s*(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)",
    re.I,
)
# "delivery +$6.10" / "shipping $4.47"
_delivery_lead_re = re.compile(
    r"(?:shipping|delivery|postage)\s+(?:\+|plus\s*)?(?:US\s*)?\$\s*(?P<val>\d+(?:\.\d{1,2})?)",
    re.I,
)


def shipping_label(cost: float | None) -> str:
    if cost is None:
        return ""
    if cost == 0:
        return "Free"
    return f"${cost:.2f}"


def parse_shipping_amount(text: str) -> tuple[float | None, str]:
    """Paid shipping/delivery only. Does not treat a stray 'Free shipping' as $0."""
    raw = (text or "").replace("\xa0", " ").replace("&nbsp;", " ")
    for pattern in (_ship_cost_re, _delivery_lead_re, _carrier_rate_re, _shipping_colon_re):
        match = pattern.search(raw)
        if match:
            value = float(match.group("val"))
            return value, shipping_label(value)
    return None, ""


def parse_shipping(text: str, *, allow_free: bool = True) -> tuple[float | None, str]:
    """Prefer a real +$x.xx from SRP. $0 only when the text says Free (no paid rate)."""
    paid, paid_label = parse_shipping_amount(text)
    if paid is not None:
        return paid, paid_label
    raw = (text or "").replace("\xa0", " ").replace("&nbsp;", " ")
    if allow_free and _free_ship_re.search(raw):
        return 0.0, shipping_label(0.0)
    return None, ""


def parse_price(text: str) -> tuple[float | None, str]:
    match = _price_re.search(text or "")
    if not match:
        return None, "USD"
    raw = match.group("val").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None, "USD"
    cur = match.group("cur").upper()
    if "£" in cur or "GBP" in cur:
        currency = "GBP"
    elif "€" in cur or "EUR" in cur:
        currency = "EUR"
    else:
        currency = "USD"
    return value, currency


def looks_like_auction_start(title: str, price: float | None) -> bool:
    lowered = normalize_title(title)
    if any(word in lowered for word in ("auction", "bid", "offers")):
        return True
    return price is not None and price < 3
