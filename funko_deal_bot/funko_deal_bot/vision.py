"""Local Florence-2 vision backend for Funko box recognition.

V44: adaptive per-listing/per-box OCR; photographed Pop number is the identity anchor.

The vision path is intentionally local and photo-first. There is no OpenAI API and
RapidOCR/ONNX Runtime is the primary fast OCR backend; Florence-2 remains a compatibility fallback.

Install the local vision stack with:
    pip install transformers==4.49.0 torch torchvision timm einops

Florence-2-base is downloaded from Hugging Face on first use and cached locally.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from collections import OrderedDict
from urllib.parse import urlparse

from PIL import Image

from funko_deal_bot.models import Listing
import gc

from funko_deal_bot.normalize import (
    PopRef,
    is_ocr_name_noise,
    complete_pop_ref_from_ocr,
    extract_bundle_count,
    extract_lot_members,
    extract_lot_character_names,
    extract_pop_id,
    extract_title_character_names,
    parse_pop_refs,
)

log = logging.getLogger(__name__)

Downloader = Callable[[str], bytes | None]
StatusCallback = Callable[[str], None]

_MODEL_NAME = "microsoft/Florence-2-base"
_OCR_REGION_TASK = "<OCR_WITH_REGION>"
# Backward-compatible alias used by older tests/imports.
_TASK = _OCR_REGION_TASK
_OCR_TASK = "<OCR>"
_BOX_TASK = "<OPEN_VOCABULARY_DETECTION>"
_DENSE_TASK = "<DENSE_REGION_CAPTION>"
_MAX_FIGURES = 12
_MODEL = None
_PROCESSOR = None
_DEVICE = None
_MODEL_LOCK = threading.Lock()
_VISION_CACHE_LOCK = threading.Lock()
_VISION_CACHE: OrderedDict[str, list[dict]] = OrderedDict()
_VISION_CACHE_MAX = 2048
_VISION_CACHE_NAMESPACE = "v48.0"
_VISION_CACHE_FILE = Path(os.getenv("VISION_CACHE_FILE", "data/vision_cache.json"))
_VISION_MAX_PASS_SECONDS = float(os.getenv("VISION_MAX_PASS_SECONDS", os.getenv("FLORENCE_MAX_PASS_SECONDS", "12")) or 12)
_RAPIDOCR = None
_RAPIDOCR_EN = None
_RAPIDOCR_LOCK = threading.Lock()
_RAPIDOCR_INFER_LOCK = threading.Lock()
_FAST_OCR_BACKEND = None
_RAPIDOCR_SCORE_THRESHOLD = float(os.getenv("RAPIDOCR_TEXT_SCORE", "0.45") or 0.45)
_FAST_OCR_MAX_SIDE = int(os.getenv("RAPIDOCR_MAX_SIDE", "1536") or 1536)
_FAST_OCR_THREADS = int(os.getenv("RAPIDOCR_CPU_THREADS", "2") or 2)

_GENERIC_NAME_ONLY = {
    "playstation", "xbox", "nintendo", "switch", "games", "game",
    "marvel", "dc", "disney", "pokemon", "star wars", "funko",
}

_NAME_EMBEDDED_NOISE = (
    "playstationonly", "playstation", "xboxonly", "xbox", "nintendoonly", "nintendo",
    "switchonly", "switch", "only", "gamestop", "gameonly", "animation",
    "fallconvention", "convention", "exclusive", "hotopic", "hottopic",
    "vinylfigure", "vinyl",
    "figure", "figures", "collectible", "collectibles", "television", "movies",
    "movie", "games", "game", "funko", "pop", "onepiece",
)

_NAME_NOISE = {
    "pop", "funko", "vinyl", "figure", "figures", "collectible", "collectibles",
    "television", "movies", "movie", "animation", "heroes", "exclusive", "chase",
    "age", "games", "game", "pokemon", "sopranos", "godfather", "the", "new",
    "boxed", "box", "lot", "lots", "pack", "bundle", "set", "series", "standard",
    "warning", "attention", "target", "only", "at", "14+", "tv", "pop!",
    "listing", "item", "seller", "shipping", "delivery", "ebay",
    "super", "limited", "edition", "original", "deluxe", "common", "rare", "of",
}

# Known detector hallucinations observed on Funko nameplates. This is deliberately
# a garbage blocklist, not a Pop-number->name catalogue.
_OCR_GARBAGE_NAMES = {
    "hohixihow", "hohixihoiw", "hoytopc", "xotkoxi", "yalomacon", "eyessnx",
}


@dataclass(frozen=True)
class OCRToken:
    text: str
    cx: float
    cy: float
    quad: tuple[float, ...]
    score: float = 1.0


def _notify(status: StatusCallback | None, text: str) -> None:
    if status:
        try:
            status(text)
        except Exception:  # noqa: BLE001
            log.debug("vision status callback failed", exc_info=True)


def _normalize_number(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lstrip("#").strip()
    text = re.sub(r"\s+", "", text)
    # Conservative OCR confusions that are common on the big boxed number.
    if len(text) in {2, 3, 4} and not text.isdigit():
        text = text.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "i": "1", "l": "1", "S": "5", "s": "5"}))
    m = re.fullmatch(r"(\d{2,4})", text)
    if not m:
        return None
    number = int(m.group(1))
    if number <= 0 or number > 9999:
        return None
    return str(number)


def _number_fragments(text: str) -> list[str]:
    """Extract 2-4 digit fragments even when OCR glues them to letters/punctuation.

    Examples: POP404 -> 404, 1291. -> 1291, #05 -> 05. This helper is only
    applied to candidate Pop-number OCR text, never to a title to invent a number.
    """
    raw = str(text or "")
    out: list[str] = []
    # First grab literal 2-4 digit runs anywhere in the token.
    for match in re.findall(r"\d{2,4}", raw):
        normalized = _normalize_number(match)
        if normalized and normalized not in out:
            out.append(normalized)
    # Then handle a small, conservative set of OCR digit substitutions only when
    # a compact alphanumeric token otherwise looks like a number badge.
    compact = re.sub(r"[^0-9A-Za-z#]", "", raw)
    if len(compact) >= 2:
        translated = compact.translate(str.maketrans({"O":"0","o":"0","I":"1","i":"1","l":"1","S":"5","s":"5"}))
        for match in re.findall(r"\d{2,4}", translated):
            normalized = _normalize_number(match)
            if normalized and normalized not in out:
                out.append(normalized)
    return out


def _clean_name(text: str) -> str:
    """Clean OCR labels into a compact character/name candidate.

    Florence often glues storefront/branding text to the actual character
    name (for example ``ATOBLACK CAT`` or ``PlayStation OnlySAM``).  The
    cleaner deliberately removes those fragments before tokenization.
    """
    text = re.sub(r"[“”\"]", "", text or "")
    for marker in sorted(_NAME_EMBEDDED_NOISE, key=len, reverse=True):
        text = re.sub(rf"(?i){re.escape(marker)}", " ", text)
    # Target exclusivity stickers may arrive as a glued fragment.
    text = re.sub(r"(?i)only\s*at\s*target", " ", text)
    text = re.sub(r"(?i)onlyattarget|attarget|targetexclusive", " ", text)
    # Common OCR prefix artifact from the red Target sticker / ATO boundary.
    text = re.sub(r"(?i)^ato(?=[a-z])", " ", text)
    # Common Florence OCR prefix artifact on all-caps labels: eSHIGGS -> SHIGGS.
    text = re.sub(r"(?<![A-Za-z])e(?=[A-Z]{3,}\b)", "", text)
    # A frequent single-word OCR hallucination for Doctor is DICTOR.
    text = re.sub(r"(?i)\bdictor\b", "doctor", text)
    text = re.sub(r"[^A-Za-z0-9'’+\- ]+", " ", text)
    words: list[str] = []
    seen: set[str] = set()
    for raw in text.split():
        word = raw.strip("-_ ")
        key = word.casefold()
        if not key or key in _NAME_NOISE or key.isdigit() or len(key) < 2:
            continue
        if key in seen:
            continue
        seen.add(key)
        words.append(word)
    return " ".join(words[:6]).strip()


def _specific_name(text: str) -> str:
    """Return a specific character/name label, not a franchise/platform label."""
    cleaned = _clean_name(text)
    if not cleaned:
        return ""
    key = re.sub(r"\s+", " ", cleaned.casefold()).strip()
    if key in _GENERIC_NAME_ONLY:
        return ""
    cleaned = re.sub(r"(?i)^one\s+piec(?:e)?\s+", "", cleaned).strip()
    if not cleaned:
        return ""
    key = re.sub(r"\s+", " ", cleaned.casefold()).strip()
    key_nospace = key.replace(" ", "")
    for platform in ("playstation", "xbox", "nintendo", "switch"):
        if key_nospace.startswith(platform):
            remainder = key_nospace[len(platform):]
            if remainder and remainder not in _GENERIC_NAME_ONLY:
                cleaned = _clean_name(remainder)
                if cleaned:
                    return cleaned
            return ""
    return cleaned


def _coerce_image_url(value: object) -> str | None:
    """Normalize eBay image values, including JSON-LD ImageObject/list forms."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            match = re.search(r"['\"](?:url|contentUrl|imageUrl|src)['\"]\s*:\s*['\"](https?://[^'\"]+)", text)
            if match:
                text = match.group(1)
        elif text.startswith("["):
            match = re.search(r"https?://[^'\"\s]+", text)
            if match:
                text = match.group(0)
        parsed = urlparse(text)
        return text if parsed.scheme in {"http", "https"} and parsed.netloc else None
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "imageUrl", "src"):
            candidate = _coerce_image_url(value.get(key))
            if candidate:
                return candidate
    if isinstance(value, (list, tuple)):
        for item in value:
            candidate = _coerce_image_url(item)
            if candidate:
                return candidate
    return None


def _load_image(source: bytes | bytearray | memoryview | str | Path | Image.Image) -> Image.Image | None:
    try:
        if isinstance(source, Image.Image):
            return source.convert("RGB")
        if isinstance(source, (bytes, bytearray, memoryview)):
            return Image.open(io.BytesIO(bytes(source))).convert("RGB")
        path = Path(source)
        if path.exists():
            return Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.warning("Florence image load failed: %s", exc)
    return None



def _rapidocr_params(*, nameplate: bool = False) -> dict:
    """RapidOCR configuration for Funko's English-only OCR path.

    Important: RapidOCR 3.9.x does not ship a PP-OCRv5 English detector;
    the supported English detector is the PP-OCRv4 English model, while the
    English recognizer is PP-OCRv5.  Keeping Det=EN and Rec=EN prevents the
    package from silently downloading the Chinese ch_PP-OCRv5 models.
    Orientation classification is disabled because Funko labels are handled
    as horizontal text and the classifier is a Chinese model.
    """
    from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion
    return {
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.lang_type": LangDet.EN,
        "Det.model_type": ModelType.MOBILE,
        "Det.ocr_version": OCRVersion.PPOCRV4,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.lang_type": LangRec.EN,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "EngineConfig.onnxruntime.intra_op_num_threads": max(1, min(_FAST_OCR_THREADS, 4)),
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Global.use_det": True,
        "Global.use_cls": False,
        "Global.use_rec": True,
        "Global.max_side_len": (2048 if nameplate else max(_FAST_OCR_MAX_SIDE, 2048)),
        "Global.text_score": (0.25 if nameplate else _RAPIDOCR_SCORE_THRESHOLD),
    }


def _load_rapidocr(status: StatusCallback | None = None):
    """Load the general PP-OCRv5 mobile pipeline once."""
    global _RAPIDOCR, _FAST_OCR_BACKEND
    if _RAPIDOCR is not None:
        return _RAPIDOCR
    with _RAPIDOCR_LOCK:
        if _RAPIDOCR is not None:
            return _RAPIDOCR
        try:
            from rapidocr import RapidOCR
            _RAPIDOCR = RapidOCR(params=_rapidocr_params())
            _FAST_OCR_BACKEND = "rapidocr-en"
            log.info("RapidOCR ready: English EN detector + PP-OCRv5 EN recognition + ONNX Runtime CPU")
            _notify(status, "⚡ OCR: RapidOCR CPU готов")
            return _RAPIDOCR
        except Exception as exc:  # noqa: BLE001
            log.warning("RapidOCR unavailable; trying local OCR fallback: %s", exc)
            return None


def _load_rapidocr_en(status: StatusCallback | None = None):
    """Return the single English RapidOCR engine used by both detection and recognition."""
    global _RAPIDOCR_EN
    if _RAPIDOCR_EN is not None:
        return _RAPIDOCR_EN
    engine = _load_rapidocr(status=status)
    if engine is not None:
        _RAPIDOCR_EN = engine
    return engine


def _rapidocr_tokens(image: Image.Image, *, status: StatusCallback | None = None, english: bool = True, min_score: float | None = None) -> list[OCRToken]:
    # Funko packaging is English; keep the primary OCR path English-only.
    engine = _load_rapidocr(status=status)
    if engine is None:
        return []
    try:
        import numpy as np
        img = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
        # ONNX Runtime object is shared by the scan worker and /check.
        # Serialize inference to avoid intermittent empty results observed when
        # two requests hit the same RapidOCR instance concurrently.
        with _RAPIDOCR_INFER_LOCK:
            result = engine(img)
        boxes = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if boxes is None or txts is None:
            return []
        out: list[OCRToken] = []
        for i, txt in enumerate(txts):
            if i >= len(boxes):
                break
            text = str(txt or "").strip()
            if not text:
                continue
            score = float(scores[i]) if scores is not None and i < len(scores) else 1.0
            threshold = _RAPIDOCR_SCORE_THRESHOLD if min_score is None else min_score
            if score < threshold:
                continue
            pts = np.asarray(boxes[i], dtype=float).reshape(-1, 2)
            if pts.shape[0] != 4:
                continue
            center = _quad_center(tuple(float(v) for v in pts.reshape(-1).tolist()))
            if center is None:
                continue
            cx, cy, quad = center
            out.append(OCRToken(text=text, cx=cx, cy=cy, quad=quad, score=score))
        return _dedupe_tokens(out)
    except Exception as exc:  # noqa: BLE001
        log.warning("RapidOCR inference failed: %s", exc)
        return []


def _tesseract_tokens(image: Image.Image) -> list[OCRToken]:
    try:
        import pytesseract
        data = pytesseract.image_to_data(image, config="--oem 3 --psm 11", output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    out: list[OCRToken] = []
    for i, raw in enumerate(data.get("text", [])):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i]) / 100.0
            x, y, w, h = [int(data[k][i]) for k in ("left", "top", "width", "height")]
        except Exception:
            continue
        if conf < 0.30 or w <= 2 or h <= 2:
            continue
        quad = (float(x), float(y), float(x+w), float(y), float(x+w), float(y+h), float(x), float(y+h))
        out.append(OCRToken(text=text, cx=x+w/2, cy=y+h/2, quad=quad, score=max(0.3, min(1.0, conf))))
    return _dedupe_tokens(out)


def _fast_ocr_tokens(image: Image.Image, *, status: StatusCallback | None = None, min_score: float | None = None) -> list[OCRToken]:
    tokens = _rapidocr_tokens(image, status=status, min_score=min_score)
    if tokens:
        return tokens
    use_tesseract = os.getenv("VISION_TESSERACT_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
    if use_tesseract:
        fallback = _tesseract_tokens(image)
        if fallback:
            global _FAST_OCR_BACKEND
            _FAST_OCR_BACKEND = "tesseract"
            return fallback
    # Florence is opt-in in production. A compatibility escape hatch remains when
    # the RapidOCR package is literally absent, so older environments/tests can still
    # use the previous local backend. With RapidOCR installed, it is never consulted
    # unless VISION_FLORENCE_FALLBACK=1.
    use_florence = os.getenv("VISION_FLORENCE_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
    if _RAPIDOCR is None:
        try:
            import importlib.util
            use_florence = use_florence or importlib.util.find_spec("rapidocr") is None
        except Exception:
            pass
    if use_florence:
        try:
            result = _invoke_florence(image, _OCR_REGION_TASK, status=status)
            tokens = _dedupe_tokens(_ocr_tokens(result))
            if tokens:
                _FAST_OCR_BACKEND = "florence"
                return tokens
        except Exception:
            log.debug("Florence compatibility OCR failed", exc_info=True)
    return []


def _direct_name_recognition(image: Image.Image, *, status: StatusCallback | None = None) -> list[OCRToken]:
    """Recognize one already-isolated horizontal nameplate without text detection.

    Funko nameplates are a single printed line. Running the detector on a tiny
    label is the main reason V30 produced empty detection results. RapidOCR
    explicitly supports use_det=False/use_rec=True for this case, so use the
    English recognizer directly and avoid detector misses.
    """
    engine = _load_rapidocr_en(status=status)
    if engine is None:
        return []
    try:
        import numpy as np
        from PIL import ImageEnhance, ImageOps
        base = image.convert("RGB")
        # Keep the line wide and sufficiently tall for the recognizer.
        target_h = 96
        if base.height < target_h:
            scale = target_h / max(1, base.height)
            base = base.resize((max(32, int(base.width * scale)), target_h), Image.Resampling.LANCZOS)
        variants = [base]
        gray = ImageOps.grayscale(base)
        gray = ImageEnhance.Contrast(gray).enhance(1.35).convert("RGB")
        variants.append(gray)
        out: list[OCRToken] = []
        for variant in variants:
            arr = np.asarray(variant)[:, :, ::-1].copy()
            with _RAPIDOCR_INFER_LOCK:
                result = engine(arr, use_det=False, use_cls=False, use_rec=True)
            txts = getattr(result, "txts", None) or ()
            scores = getattr(result, "scores", None) or ()
            for i, txt in enumerate(txts):
                text = str(txt or "").strip()
                if not text:
                    continue
                score = float(scores[i]) if i < len(scores) else 1.0
                if score < 0.18:
                    continue
                out.append(OCRToken(text=text, cx=variant.width / 2, cy=variant.height / 2,
                                    quad=(0.0, 0.0, float(variant.width), 0.0, float(variant.width), float(variant.height), 0.0, float(variant.height)),
                                    score=score))
        return out
    except Exception:
        log.debug("Direct English name recognition failed", exc_info=True)
        return []


def _fast_name_ocr_tokens(image: Image.Image, *, status: StatusCallback | None = None) -> list[OCRToken]:
    """OCR intended for English Funko nameplates.

    Try direct recognition first because the caller already isolated one line;
    only fall back to detector-based OCR when direct recognition yields nothing.
    """
    direct = _direct_name_recognition(image, status=status)
    if direct:
        return direct
    tokens = _rapidocr_tokens(image, status=status, min_score=0.20)
    if tokens:
        return tokens
    return _fast_ocr_tokens(image, status=status)


def _load_florence(status: StatusCallback | None = None) -> tuple[object, object, object] | None:
    """Load Florence-2 once, lazily, and keep it in memory for the bot lifetime."""
    global _MODEL, _PROCESSOR, _DEVICE
    if _MODEL is not None and _PROCESSOR is not None:
        return _MODEL, _PROCESSOR, _DEVICE
    with _MODEL_LOCK:
        if _MODEL is not None and _PROCESSOR is not None:
            return _MODEL, _PROCESSOR, _DEVICE
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Florence-2 dependencies are missing. Install: "
                "transformers==4.49.0 torch torchvision timm einops: %s",
                exc,
            )
            _notify(status, "❌ ИИ: не установлены зависимости Florence-2")
            return None

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if device.type == "cuda" else torch.float32
        # CPU optimization: cap thread fan-out to the actual VM/host allocation.
        # On tiny 2-vCPU VMs, uncontrolled inter-op/intra-op pools can add more
        # scheduling overhead than useful work.
        if device.type == "cpu":
            try:
                requested = int(os.getenv("FLORENCE_CPU_THREADS", os.getenv("VISION_CPU_THREADS", "0")) or 0)
            except ValueError:
                requested = 0
            if requested <= 0:
                requested = os.cpu_count() or 2
            requested = max(1, min(requested, 8))
            try:
                torch.set_num_threads(requested)
                torch.set_num_interop_threads(1)
            except RuntimeError:
                pass
            log.info("Florence CPU threads: intra=%s interop=1", requested)
        cache_dir = os.getenv("HF_HOME") or os.getenv("HUGGINGFACE_HUB_CACHE") or None
        kwargs = {"trust_remote_code": True, "torch_dtype": dtype}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir

        try:
            _notify(status, f"🤖 ИИ: загружаю Florence-2 ({device.type.upper()})…")
            log.info("Loading local vision model %s on %s", _MODEL_NAME, device)
            processor = AutoProcessor.from_pretrained(
                _MODEL_NAME,
                trust_remote_code=True,
                cache_dir=cache_dir,
            )
            model = AutoModelForCausalLM.from_pretrained(_MODEL_NAME, **kwargs)
            model.to(device)
            model.eval()
        except Exception as exc:  # noqa: BLE001
            log.exception("Florence-2 initialization failed: %s", exc)
            _notify(status, "❌ ИИ: Florence-2 не удалось загрузить")
            return None

        _MODEL = model
        _PROCESSOR = processor
        _DEVICE = device
        log.info("Florence-2 ready: %s", _MODEL_NAME)
        _notify(status, "✅ ИИ: Florence-2 полностью готов")
        return _MODEL, _PROCESSOR, _DEVICE


def ensure_vision_ready(status: StatusCallback | None = None) -> bool:
    """Warm the configured fast OCR backend; Florence is optional fallback."""
    engine = _load_rapidocr(status=status)
    if engine is not None:
        return True
    use_florence = os.getenv("VISION_FLORENCE_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
    if use_florence:
        return _load_florence(status=status) is not None
    _notify(status, "❌ OCR: RapidOCR/ONNX Runtime не загружен")
    return False


def _run_florence(
    image: Image.Image,
    task: str,
    text_input: str | None = None,
    *,
    status: StatusCallback | None = None,
) -> dict:
    runtime = _load_florence(status=status)
    if runtime is None:
        return {}
    model, processor, device = runtime
    try:
        import torch

        prompt = task if not text_input else task + text_input
        inputs = processor(text=prompt, images=image, return_tensors="pt")
        prepared = {}
        for key, value in inputs.items():
            if not hasattr(value, "to"):
                prepared[key] = value
                continue
            value = value.to(device)
            if key == "pixel_values" and device.type == "cuda":
                value = value.to(dtype=torch.float16)
            prepared[key] = value
        inputs = prepared
        try:
            beams = max(1, int(os.getenv("FLORENCE_NUM_BEAMS", os.getenv("VISION_NUM_BEAMS", "1")) or 1))
        except ValueError:
            beams = 1
        try:
            if task in {_OCR_TASK, _OCR_REGION_TASK}:
                max_tokens = max(48, int(os.getenv("FLORENCE_OCR_MAX_TOKENS", os.getenv("VISION_OCR_MAX_TOKENS", "96")) or 96))
            else:
                max_tokens = max(64, int(os.getenv("FLORENCE_DETECT_MAX_TOKENS", "128") or 128))
        except ValueError:
            max_tokens = 96 if task in {_OCR_TASK, _OCR_REGION_TASK} else 128
        with torch.inference_mode():
            generated_ids = model.generate(
                input_ids=inputs.get("input_ids"),
                pixel_values=inputs.get("pixel_values"),
                max_new_tokens=max_tokens,
                num_beams=beams,
                do_sample=False,
                early_stopping=False,
                use_cache=True,
            )
        generated_text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
        return processor.post_process_generation(
            generated_text,
            task=task,
            image_size=image.size,
        ) or {}
    except Exception as exc:  # noqa: BLE001
        log.exception("Florence-2 inference failed for %s: %s", task, exc)
        return {}


def _quad_center(quad: object) -> tuple[float, float, tuple[float, ...]] | None:
    try:
        if isinstance(quad, (list, tuple)) and len(quad) == 4 and all(
            isinstance(point, (list, tuple)) and len(point) == 2 for point in quad
        ):
            values = tuple(float(v) for point in quad for v in point)
        else:
            values = tuple(float(v) for v in quad)
        if len(values) != 8:
            return None
        xs = values[0::2]
        ys = values[1::2]
        return sum(xs) / 4.0, sum(ys) / 4.0, values
    except Exception:
        return None


def _ocr_tokens(result: dict) -> list[OCRToken]:
    payload = result.get(_OCR_REGION_TASK) if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return []
    labels = payload.get("labels") or []
    quads = payload.get("quad_boxes") or []
    scores = payload.get("scores") or []
    tokens: list[OCRToken] = []
    for index, label in enumerate(labels):
        if index >= len(quads):
            continue
        center = _quad_center(quads[index])
        text = str(label or "").strip()
        if not text or center is None:
            continue
        score = float(scores[index]) if index < len(scores) and isinstance(scores[index], (int, float)) else 1.0
        cx, cy, quad = center
        tokens.append(OCRToken(text=text, cx=cx, cy=cy, quad=quad, score=score))
    return tokens


def _plain_ocr(result: dict) -> str:
    payload = result.get(_OCR_TASK) if isinstance(result, dict) else None
    return str(payload or "").strip() if payload else ""


def _dedupe_tokens(tokens: list[OCRToken]) -> list[OCRToken]:
    out: list[OCRToken] = []
    for token in sorted(tokens, key=lambda item: (item.cy, item.cx)):
        norm = re.sub(r"\W+", "", token.text.casefold())
        duplicate = False
        for existing in out:
            same_text = norm == re.sub(r"\W+", "", existing.text.casefold())
            close = abs(token.cx - existing.cx) < 32 and abs(token.cy - existing.cy) < 32
            if same_text and close:
                duplicate = True
                break
        if not duplicate:
            out.append(token)
    return out


def _box_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = max((ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter, 1.0)
    return inter / union


def _detection_boxes(result: dict, image_size: tuple[int, int]) -> list[tuple[float, float, float, float]]:
    payload = result.get(_BOX_TASK) if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        payload = result.get(_DENSE_TASK) if isinstance(result, dict) else None
    if not isinstance(payload, dict):
        return []
    raw = payload.get("bboxes") or []
    labels = [str(v or "").casefold() for v in (payload.get("labels") or [])]
    width, height = image_size
    candidates: list[tuple[float, float, float, float]] = []
    for idx, bbox in enumerate(raw):
        try:
            x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        except Exception:
            continue
        x1, x2 = max(0.0, min(x1, x2)), min(float(width), max(x1, x2))
        y1, y2 = max(0.0, min(y1, y2)), min(float(height), max(y1, y2))
        bw, bh = x2 - x1, y2 - y1
        area = bw * bh
        if bw < width * 0.12 or bh < height * 0.12 or area < width * height * 0.025:
            continue
        label = labels[idx] if idx < len(labels) else ""
        # OPEN_VOCABULARY_DETECTION with "Funko Pop box" usually labels the
        # physical boxes. Dense captions can be noisier, so only accept captions
        # that look like an actual package/box/figure region.
        if label and not any(word in label for word in ("funko", "pop", "box", "figure", "vinyl", "collectible", "package")):
            continue
        candidates.append((x1, y1, x2, y2))

    deduped: list[tuple[float, float, float, float]] = []
    for box in sorted(candidates, key=lambda b: (-(b[2] - b[0]) * (b[3] - b[1]), b[1], b[0])):
        if any(_box_iou(box, existing) > 0.55 for existing in deduped):
            continue
        deduped.append(box)
    return sorted(deduped, key=lambda b: (b[1], b[0]))[:_MAX_FIGURES]


def _expand_box(box: tuple[float, float, float, float], image_size: tuple[int, int], pad_ratio: float = 0.05) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    width, height = image_size
    pad_x = (x2 - x1) * pad_ratio
    pad_y = (y2 - y1) * pad_ratio
    return (
        max(0, int(x1 - pad_x)),
        max(0, int(y1 - pad_y)),
        min(width, int(x2 + pad_x)),
        min(height, int(y2 + pad_y)),
    )


def _grid_crops(image: Image.Image, expected_count: int | None) -> list[tuple[Image.Image, tuple[int, int, int, int]]]:
    """Build overlapping rescue tiles sized for the expected number of boxes.

    The full-image Florence pass is kept as the cheap first pass.  When it misses
    boxes, these tiles increase text resolution without requiring a heavier model.
    """
    width, height = image.size
    if expected_count == 2:
        cols, rows = 2, 1
    elif expected_count in {3, 4}:
        cols, rows = 2, 2
    elif expected_count in {5, 6}:
        cols, rows = 3, 2
    elif expected_count in {7, 8}:
        cols, rows = 3, 3
    else:
        cols, rows = 2, 2
    overlap = 0.10
    results: list[tuple[Image.Image, tuple[int, int, int, int]]] = []
    cell_w, cell_h = width / cols, height / rows
    for row in range(rows):
        for col in range(cols):
            x1 = int(max(0, col * cell_w - (cell_w * overlap if col else 0)))
            y1 = int(max(0, row * cell_h - (cell_h * overlap if row else 0)))
            x2 = int(min(width, (col + 1) * cell_w + (cell_w * overlap if col < cols - 1 else 0)))
            y2 = int(min(height, (row + 1) * cell_h + (cell_h * overlap if row < rows - 1 else 0)))
            if x2 > x1 and y2 > y1:
                results.append((image.crop((x1, y1, x2, y2)), (x1, y1, x2, y2)))
    return results


def _map_tokens_to_global(tokens: list[OCRToken], crop_box: tuple[int, int, int, int], crop_size: tuple[int, int]) -> list[OCRToken]:
    xa, ya, xb, yb = crop_box
    tw, th = crop_size
    sx = tw / max(xb - xa, 1)
    sy = th / max(yb - ya, 1)
    mapped: list[OCRToken] = []
    for token in tokens:
        gx = xa + token.cx / max(sx, 1e-6)
        gy = ya + token.cy / max(sy, 1e-6)
        quad = tuple(
            xa + token.quad[i] / max(sx, 1e-6) if i % 2 == 0 else ya + token.quad[i] / max(sy, 1e-6)
            for i in range(8)
        )
        mapped.append(OCRToken(token.text, gx, gy, quad, token.score))
    return mapped


def _cluster_name_tokens(tokens: list[OCRToken], image_height: int, image_width: int) -> list[tuple[str, float, float]]:
    candidates: list[OCRToken] = []
    for token in tokens:
        if not any(ch.isalpha() for ch in token.text):
            continue
        # Hard-reject packaging/legal/system OCR lines before generic cleanup.
        # Otherwise "Bobble-Head Figurine ... Scarlet Witch" can win because it
        # is a longer string than the actual character name.
        if is_ocr_name_noise(token.text):
            continue
        cleaned = _specific_name(token.text)
        if not cleaned:
            continue
        candidates.append(OCRToken(cleaned, token.cx, token.cy, token.quad, token.score))
    if not candidates:
        return []

    line_tolerance = max(12.0, image_height * 0.035)
    candidates.sort(key=lambda item: (item.cy, item.cx))
    lines: list[list[OCRToken]] = []
    for token in candidates:
        if not lines or abs(token.cy - sum(t.cy for t in lines[-1]) / len(lines[-1])) > line_tolerance:
            lines.append([token])
        else:
            lines[-1].append(token)

    result: list[tuple[str, float, float]] = []
    max_horizontal_gap = max(35.0, float(image_width) * 0.08)
    for line in lines:
        line.sort(key=lambda item: item.cx)
        group: list[OCRToken] = []
        previous_right = None
        groups: list[list[OCRToken]] = []
        for token in line:
            left = min(token.quad[0], token.quad[2], token.quad[4], token.quad[6])
            right = max(token.quad[0], token.quad[2], token.quad[4], token.quad[6])
            if group and previous_right is not None and left - previous_right > max_horizontal_gap:
                groups.append(group)
                group = []
            group.append(token)
            previous_right = right
        if group:
            groups.append(group)
        for group in groups:
            text = " ".join(item.text for item in group)
            cleaned = _specific_name(text)
            if not cleaned:
                continue
            result.append((cleaned, sum(item.cx for item in group) / len(group), sum(item.cy for item in group) / len(group)))
    return result


def _numeric_tokens(tokens: list[OCRToken]) -> list[OCRToken]:
    numbers: list[OCRToken] = []
    for token in tokens:
        for number in _number_fragments(token.text):
            numbers.append(OCRToken(number, token.cx, token.cy, token.quad, token.score))
    # OCR can split a 4-digit box number into two adjacent numeric tokens. Join
    # close fragments on the same line before giving up.
    ordered = sorted(
        [t for t in tokens if re.fullmatch(r"[#\s0-9]{1,4}", str(t.text or "").strip())],
        key=lambda t: (t.cy, t.cx),
    )
    for left, right in zip(ordered, ordered[1:]):
        if abs(left.cy - right.cy) > 28 or right.cx - left.cx > 140:
            continue
        left_digits = "".join(re.findall(r"\d", left.text))
        right_digits = "".join(re.findall(r"\d", right.text))
        combined = _normalize_number(left_digits + right_digits)
        if combined and not any(
            abs(t.cx - ((left.cx + right.cx) / 2)) < 28 and abs(t.cy - left.cy) < 28 and t.text == combined
            for t in numbers
        ):
            numbers.append(
                OCRToken(
                    combined,
                    (left.cx + right.cx) / 2,
                    (left.cy + right.cy) / 2,
                    left.quad,
                    min(left.score, right.score),
                )
            )
    return numbers


def _pair_tokens(tokens: list[OCRToken], image_size: tuple[int, int], expected_count: int | None) -> list[dict]:
    """Pair Funko's top-right Pop number with the bottom nameplate by geometry.

    Florence-2's OCR_WITH_REGION task returns independent text regions.  For Funko
    boxes the reliable invariant is structural, not semantic: the Pop number is
    near the upper-right of a box and the character name is on the lower/front
    nameplate. We therefore pair number/name regions by X position and vertical
    bands instead of requiring Florence to emit a ready-made identity pair.
    """
    width, height = image_size
    numbers = _numeric_tokens(tokens)
    if not numbers:
        return []

    # Names from the lower/front nameplate only. This excludes franchise banners,
    # logos and retailer stickers that occupy the upper/middle portion.
    lower_tokens = [t for t in tokens if t.cy >= height * 0.52 and any(ch.isalpha() for ch in t.text)]
    names = _cluster_name_tokens(lower_tokens, height, width)
    # Unit/integration OCR streams can occasionally report all text on a
    # compressed Y-scale. If the lower-band filter would discard everything,
    # fall back to the full OCR token stream; the geometric X assignment below
    # still prefers the structurally correct pairing.
    if not names:
        names = _cluster_name_tokens(tokens, height, width)

    def dedupe_names(rows: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
        out: list[tuple[str, float, float]] = []
        for row in rows:
            key = re.sub(r"[^a-z0-9]", "", row[0].casefold())
            if not key:
                continue
            # Same nameplate can be returned as overlapping OCR fragments.
            if any(key == re.sub(r"[^a-z0-9]", "", old[0].casefold()) and abs(row[1]-old[1]) < width*0.035 and abs(row[2]-old[2]) < height*0.045 for old in out):
                continue
            out.append(row)
        return out

    names = dedupe_names(names)
    if not names:
        return []

    # Prefer numbers in the upper 45% of the photo. If Florence returns a number
    # slightly lower because of perspective, keep it as a secondary candidate.
    upper_numbers = [n for n in numbers if n.cy <= height * 0.45]
    if upper_numbers:
        numbers = upper_numbers

    numbers = sorted(numbers, key=lambda n: (n.cx, n.cy))
    names = sorted(names, key=lambda row: (row[1], row[2]))

    # When the expected box count is known, keep the strongest top candidates by
    # vertical placement and spatial distribution, not by OCR order.
    if expected_count:
        numbers = sorted(numbers, key=lambda n: (n.cy, -n.score, n.cx))[:max(expected_count, len(numbers))]
        # Never invent extra names: at most expected_count physical boxes.
        if len(names) > expected_count:
            # Select a spread across X that best matches the sorted Pop numbers.
            if len(numbers) >= expected_count:
                target_x = [n.cx for n in numbers[:expected_count]]
                picked: list[tuple[str, float, float]] = []
                used: set[int] = set()
                for x in sorted(target_x):
                    cand = sorted(
                        ((abs(row[1]-x), i, row) for i, row in enumerate(names) if i not in used),
                        key=lambda z: (z[0], z[2][1]),
                    )
                    if cand:
                        _d, i, row = cand[0]
                        used.add(i)
                        picked.append(row)
                if len(picked) == expected_count:
                    names = sorted(picked, key=lambda row: row[1])
            if len(names) > expected_count:
                names = names[:expected_count]

    # Build a minimum-cost one-to-one assignment. Cost is primarily X distance;
    # Y distance is only a weak tie breaker because box photos can be perspective-skewed.
    candidate_pairs: list[tuple[float, int, int]] = []
    for ni, num in enumerate(numbers):
        for mi, (name, cx, cy) in enumerate(names):
            dx = abs(cx - num.cx) / max(width, 1)
            dy = max(0.0, (cy - num.cy) / max(height, 1))
            # A nameplate should be below its number. Penalize impossible reverse pairs.
            reverse_penalty = 4.0 if cy < num.cy else 0.0
            cost = dx * 10.0 + dy * 0.75 + reverse_penalty
            candidate_pairs.append((cost, ni, mi))
    candidate_pairs.sort()

    used_n: set[int] = set()
    used_m: set[int] = set()
    pairs: list[dict] = []
    max_dx = 0.68 if (expected_count or 0) <= 1 else 0.32
    max_pairs = expected_count or min(len(numbers), len(names))
    for cost, ni, mi in candidate_pairs:
        if ni in used_n or mi in used_m:
            continue
        num = numbers[ni]
        name, cx, cy = names[mi]
        dx = abs(cx - num.cx) / max(width, 1)
        if dx > max_dx:
            continue
        used_n.add(ni)
        used_m.add(mi)
        pairs.append({"name": name, "number": num.text, "exclusive": None})
        if len(pairs) >= max_pairs:
            break

    # Return in visual left-to-right order, which also gives stable member order.
    pairs.sort(key=lambda item: next((n.cx for n in numbers if n.text == item["number"]), 0.0))
    return pairs[:_MAX_FIGURES]


def _spatial_title_name_recovery(tokens: list[OCRToken], title: str, expected_count: int | None, image_size: tuple[int, int]) -> list[dict]:
    """Use photo-read Pop numbers plus title names when Florence misses nameplate text.

    Numbers still must come from the photo. Title is used only for the character
    names, so this remains photo-first and costs zero extra network/model passes.
    """
    if not expected_count or not title:
        return []
    numbers = _numeric_tokens(tokens)
    if not numbers:
        return []
    numbers = sorted([n for n in numbers if n.cy <= image_size[1] * 0.50], key=lambda n: n.cx)
    if len(numbers) < expected_count:
        return []
    names = _title_fallback_names(title, expected_count)
    if len(names) != expected_count:
        return []
    return [
        {"name": _specific_name(name), "number": numbers[i].text, "exclusive": None}
        for i, name in enumerate(sorted(names, key=lambda n: names.index(n)))
        if _specific_name(name)
    ][:expected_count]

def _title_fallback_names(title: str, expected_count: int | None) -> list[str]:
    """Cheap, no-network name fallback used only when photo OCR found numbers."""
    if not title or not expected_count:
        return []
    names = [str(row.get("name") or "").strip() for row in extract_lot_members(title) if row.get("name")]
    names = [n for n in names if _specific_name(n)]
    if len(names) >= expected_count:
        return names[:expected_count]
    # Capitalized runs recover titles such as `Judomaster Captain Marvel Annabelle
    # Hot Topic` without involving any extra HTTP request.
    raw = re.sub(r"(?i)\bfunko\b|\bpop!?\b|\bvinyl\b|\blot\s+of\s+\d+\b|\blot\s+\d+\b", " ", title)
    raw = re.sub(r"[^A-Za-z0-9&/+ ]+", " ", raw)
    words = raw.split()
    groups: list[str] = []
    current: list[str] = []
    stop = {
        "Hot", "Topic", "Exclusive", "Target", "Amazon", "Walmart", "BoxLunch",
        "GameStop", "Only", "New", "Box", "Vinyl", "Figure", "Figures",
    }
    for word in words:
        clean = re.sub(r"[^A-Za-z0-9'’]", "", word)
        if not clean:
            continue
        looks_cap = clean[0].isupper() or clean.isupper()
        if looks_cap and clean not in stop and len(clean) >= 2:
            current.append(clean)
        else:
            if current:
                groups.append(" ".join(current)); current=[]
    if current:
        groups.append(" ".join(current))
    groups = [n for n in groups if _specific_name(n)]
    if len(groups) >= expected_count:
        return groups[:expected_count]
    return names[:expected_count]


def _pair_numbers_to_title_names(tokens: list[OCRToken], title: str, expected_count: int | None) -> list[dict]:
    if not expected_count:
        return []
    numbers = sorted(_numeric_tokens(tokens), key=lambda item: item.cx)
    names = _title_fallback_names(title, expected_count)
    if len(numbers) < 1 or len(names) != expected_count or len(numbers) < expected_count:
        return []
    return [
        {"name": _specific_name(name), "number": numbers[idx].text, "exclusive": None}
        for idx, name in enumerate(names) if _specific_name(name)
    ][:expected_count]


def _invoke_florence(
    image: Image.Image,
    task: str,
    *,
    text_input: str | None = None,
    status: StatusCallback | None = None,
) -> dict:
    try:
        return _run_florence(image, task, text_input=text_input, status=status)
    except TypeError:
        # Compatibility with older monkeypatched/test callables that accept
        # only (image, task). The real implementation never needs this path.
        return _run_florence(image, task)


def _plain_text_candidates(text: str) -> tuple[list[str], list[str]]:
    """Extract likely 3-4 digit Pop IDs and human names from plain OCR text.

    This is only used inside a crop that is expected to contain ONE physical box.
    It is therefore safe to use the plain OCR text as a secondary fallback: there
    is no neighboring box whose name/number could be cross-paired.
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return [], []
    numbers: list[str] = []
    for match in re.finditer(r"(?<!\d)(?:#\s*)?(\d{3,4})(?!\d)", raw):
        number = _normalize_number(match.group(1))
        if number and number not in numbers:
            numbers.append(number)
    # Remove technical / branding fragments before collecting name candidates.
    cleaned = re.sub(r"#?\s*\d{3,4}", " ", raw)
    words = []
    for chunk in re.split(r"[|\n;,/:]+", cleaned):
        candidate = _clean_name(chunk)
        if candidate and len(candidate.split()) <= 6:
            # Require at least one alphabetic word and avoid obvious product noise.
            if any(ch.isalpha() for ch in candidate):
                words.append(candidate)
    # Prefer longer human-readable phrases and deduplicate.
    names: list[str] = []
    for candidate in sorted(words, key=lambda x: (-len(x.split()), -len(x))):
        key = candidate.casefold()
        if key not in {n.casefold() for n in names}:
            names.append(candidate)
    return names[:8], numbers[:8]


def _plain_crop_identity(crop: Image.Image, plain_text: str) -> list[dict]:
    names, numbers = _plain_text_candidates(plain_text)
    if not names or not numbers:
        return []
    # One physical crop -> one physical figure. Pick the most informative name.
    name = _specific_name(names[0])
    number = numbers[0]
    if not name:
        return []
    if not name or not number:
        return []
    return [{"name": name, "number": number, "exclusive": None}]


def _bottom_label_identity(crop: Image.Image, number: str | None, *, status: StatusCallback | None = None) -> list[dict]:
    """Read the character name plate from one physical Funko box with one fallback pass."""
    if not number:
        return []
    w, h = crop.size
    # The large blue/white name plate is normally in the lower-middle band.
    # One tight crop avoids PlayStation/GameStop/franchise text above it.
    left, top, right, bottom = (0.10, 0.64, 0.94, 0.90)
    plate = crop.crop((int(w*left), int(h*top), int(w*right), int(h*bottom)))
    scale = 2.25 if plate.width < 1400 else 1.25
    enlarged = plate.resize((int(plate.width*scale), int(plate.height*scale)), Image.Resampling.LANCZOS)
    result = _invoke_florence(enlarged, _OCR_REGION_TASK, status=status)
    tokens = _ocr_tokens(result)
    candidates: list[str] = []
    if tokens:
        names = _cluster_name_tokens(tokens, enlarged.size[1], enlarged.size[0])
        for name, _cx, cy in names:
            if 0.08 <= cy / max(1, enlarged.size[1]) <= 0.88:
                clean = _specific_name(name)
                if clean and 1 <= len(clean.split()) <= 5:
                    candidates.append(clean)
    plain = _plain_ocr(result)
    if not candidates and plain:
        names, _numbers = _plain_text_candidates(plain)
        for name in names:
            clean = _specific_name(name)
            if clean:
                candidates.append(clean)
    if candidates:
        candidates.sort(key=lambda x: (-len(x.split()), -len(x)))
        return [{"name": candidates[0], "number": number, "exclusive": None}]
    # A single wider fallback is cheaper than the previous 3x2 OCR cascade.
    fallback = crop.crop((int(w*0.03), int(h*0.58), int(w*0.97), int(h*0.94)))
    result2 = _invoke_florence(fallback, _OCR_TASK, status=status)
    plain2 = _plain_ocr(result2)
    if plain2:
        names, _numbers = _plain_text_candidates(plain2)
        cleaned = [_specific_name(x) for x in names if _specific_name(x)]
        if cleaned:
            cleaned.sort(key=lambda x: (-len(x.split()), -len(x)))
            return [{"name": cleaned[0], "number": number, "exclusive": None}]
    return []


def _recognize_crop(crop: Image.Image, expected: int | None, *, status: StatusCallback | None = None) -> list[dict]:
    result = _invoke_florence(crop, _OCR_REGION_TASK, status=status)
    tokens = _ocr_tokens(result)
    number_tokens = _numeric_tokens(tokens)
    number = number_tokens[0].text if number_tokens else None
    # The name plate is the authoritative name source for a physical box.
    bottom = _bottom_label_identity(crop, number, status=status)
    if bottom:
        return bottom
    pairs = _pair_tokens(tokens, crop.size, expected)
    if pairs:
        return pairs
    plain = _plain_ocr(_invoke_florence(crop, _OCR_TASK, status=status))
    if plain:
        log.debug("Florence plain OCR produced %d chars", len(plain))
        fallback = _plain_crop_identity(crop, plain)
        if fallback:
            _notify(status, "🔤 ИИ: запасной OCR уверенно нашёл номер и имя коробки")
            return fallback[:1]
    return []


def _title_name_for_number(title: str, number: str) -> str | None:
    """Extract a likely product-name phrase immediately before a matching Pop number.

    This is a correction layer only: it runs after photo OCR already found the same
    number and only uses nearby title words to repair obvious OCR typos/truncation.
    """
    raw = title or ""
    m = re.search(rf"(?is)(.{{0,120}})(?:#\s*)?{re.escape(str(number))}\b", raw)
    if not m:
        return None
    tail = m.group(1)
    tail = re.split(r"(?i)\b(?:featuring|with|includes?|and|plus)\b", tail)[-1]
    tail = re.split(r"[+|/,;]", tail)[-1]
    tail = re.sub(r"(?i)\b(?:Funko|Pop!?|Vinyl|Figure|Exclusive|Only|GameStop|PlayStation|Xbox|Nintendo|Animation|Dragon Ball|Star Wars|One Piece|Death Stranding)\b", " ", tail)
    tail = re.sub(r"[^A-Za-z'’\- ]+", " ", tail)
    words = [w for w in tail.split() if len(w) >= 2]
    if not words:
        return None
    # Keep at most the last five meaningful title words.
    candidate = _specific_name(" ".join(words[-5:]))
    return candidate or None


def _compact(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").casefold())


def _title_number_name_map(title: str, expected_count: int | None = None) -> dict[str, str]:
    """Map explicit Pop numbers to the character names printed in the title.

    This is a correction layer only: the photo must already supply the same Pop
    number. The title is never used to invent a missing visual identity.
    """
    raw = str(title or "")
    if not raw:
        return {}
    # First try the simplest explicit-name+#number grammar. This is deliberately
    # positional and handles common eBay bundle titles such as
    # "Michael Corleone #404 Tony Soprano #1291" without relying on a lossy
    # marketplace-title parser.
    explicit_nums = [(m.start(), _normalize_number(m.group(1))) for m in re.finditer(r"#\s*(\d{2,4})\b", raw)]
    explicit_nums = [(pos, n) for pos, n in explicit_nums if n]
    if explicit_nums:
        direct: dict[str, str] = {}
        for idx_num, (pos, number) in enumerate(explicit_nums):
            start_pos = explicit_nums[idx_num - 1][0] + 1 if idx_num else 0
            segment = raw[start_pos:pos]
            # Drop marketplace delimiters and generic listing words, but keep the
            # actual multi-word character name intact.
            segment = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", segment)
            segment = re.sub(
                r"(?is)\b(?:funko|pop!?|vinyl|figure|figures|exclusive|only|at|target|playstation|xbox|nintendo|animation|games?|lot|bundle|set|variety|of|marvel|dc|disney)\b",
                " ",
                segment,
            )
            segment = re.sub(r"^[\s,;:+|/-]+|[\s,;:+|/-]+$", "", segment)
            candidate = _specific_name(segment)
            if candidate and len(candidate.split()) <= 6:
                direct[number] = candidate
        if len(direct) == len(explicit_nums):
            return direct

    # Prefer the normalized lot parser when the title explicitly binds names to
    # Pop numbers. This avoids accidentally including leading words such as
    # "Variety Marvel" in the name before the first number.
    try:
        parsed_members = extract_lot_members(raw)
        parsed_map = {}
        for row in parsed_members:
            pop = _normalize_number(row.get("pop")) if isinstance(row, dict) else None
            name = _specific_name(str(row.get("name") or "")) if isinstance(row, dict) else ""
            if pop and name:
                parsed_map[pop] = name
        if parsed_map:
            # Only trust the normalized parser when it produced a complete
            # number->name mapping. Partial mappings can incorrectly glue two
            # names together (e.g. "Doctor Octopus & Black Cat #957, #958").
            explicit_nums = [m.group(1) for m in re.finditer(r"#\s*(\d{3,4})\b", raw)]
            if len(parsed_map) >= len(explicit_nums) or (not explicit_nums and len(parsed_map) == 1):
                return parsed_map
    except Exception:
        pass
    nums = [(m.start(), _normalize_number(m.group(1))) for m in re.finditer(r"#\s*(\d{3,4})\b", raw)]
    nums = [(pos, n) for pos, n in nums if n]
    if nums:
        # For explicit multi-item titles, normalized title character names can be
        # aligned with the explicit Pop-number sequence without using the raw text
        # segment before the first number (which may contain "Variety Marvel").
        try:
            title_names = [_specific_name(str(x)) for x in extract_title_character_names(raw)]
            title_names = [x for x in title_names if x]
            if len(nums) >= 2 and len(title_names) >= len(nums):
                return {number: name for (_pos, number), name in zip(nums, title_names)}
        except Exception:
            pass
    if not nums:
        # Some eBay titles omit # but place the numeric IDs at the end.
        nums = [(m.start(), _normalize_number(m.group(1))) for m in re.finditer(r"(?<!\d)(\d{3,4})(?!\d)", raw)]
        nums = [(pos, n) for pos, n in nums if n]
    if expected_count and len(nums) > expected_count:
        nums = nums[:expected_count]
    # Another common eBay form is one shared name prefix followed by several
    # numbers, e.g. "Doctor Octopus & Black Cat (Target Exclusive) #957, #958".
    # Split that prefix before applying per-number segment logic.
    if len(nums) >= 2:
        try:
            prefix = raw[:nums[0][0]]
            prefix = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", prefix)
            prefix = re.sub(r"(?is)\b(?:funko|pop!?|vinyl|figure|exclusive|only|at|target|playstation|xbox|nintendo|animation|games?)\b", " ", prefix)
            parts = [p.strip() for p in re.split(r"\s*(?:&|\+|/|,|\band\b)\s*", prefix, flags=re.I) if p.strip()]
            names = [_specific_name(part) for part in parts]
            names = [name for name in names if name]
            if len(names) >= len(nums):
                return {number: name for (_pos, number), name in zip(nums, names)}
        except Exception:
            pass
    out: dict[str, str] = {}
    # First trust the normalized title parser for explicit bundles. This handles
    # `Doctor Octopus & Black Cat (#957, #958)` positionally and prevents suffix
    # metadata such as `Target Exclusive` from leaking into the second name.
    try:
        positional_bundle = bool(re.search(r"\(\s*#?\s*\d{1,4}(?:\s*[,;/&+]\s*#?\s*\d{1,4})+\s*\)", raw, re.I))
        if positional_bundle:
            parsed_members = extract_lot_members(raw)
            for row in parsed_members:
                pop = _normalize_number(row.get("pop")) if isinstance(row, dict) else None
                name = _specific_name(str(row.get("name") or "")) if isinstance(row, dict) else ""
                if pop and name:
                    out[pop] = name
    except Exception:
        pass
    # Names that appear immediately before a number are the strongest signal.
    for idx, (pos, number) in enumerate(nums):
        prev_pos = nums[idx - 1][0] if idx else 0
        segment = raw[prev_pos:pos]
        segment = re.sub(r"(?is)\b(?:funko|pop!?|vinyl|figure|exclusive|only|at|target|playstation|xbox|nintendo|animation|games?)\b", " ", segment)
        segment = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", segment)
        segment = re.sub(r"[^A-Za-z'’&+/,\- ]+", " ", segment)
        parts = [p.strip() for p in re.split(r"\s*(?:&|\+|/|,|\band\b)\s*", segment, flags=re.I) if p.strip()]
        candidates = []
        for part in parts:
            clean = _specific_name(part)
            if clean and len(clean.split()) <= 5:
                candidates.append(clean)
        # If there is exactly one candidate in this segment, bind it directly.
        if len(candidates) == 1:
            out[number] = candidates[0]
    # If several names precede a tail of several numbers, align by order.
    if len(out) < len(nums):
        first_num_pos = nums[0][0]
        prefix = raw[:first_num_pos]
        prefix = re.sub(r"(?is)\b(?:funko|pop!?|vinyl|figure|exclusive|only|at|target|playstation|xbox|nintendo|animation|games?)\b", " ", prefix)
        prefix = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", prefix)
        chunks = [p.strip() for p in re.split(r"\s*(?:&|\+|/|,|\band\b)\s*", prefix, flags=re.I) if p.strip()]
        names = []
        for chunk in chunks:
            clean = _specific_name(chunk)
            if clean and len(clean.split()) <= 5:
                names.append(clean)
        if len(names) >= len(nums):
            for (_pos, number), name in zip(nums, names):
                out[number] = name
    return out


def _repair_with_title(items: list[dict], title: str | None) -> list[dict]:
    if not items:
        return []
    title = str(title or "")
    repaired: list[dict] = []
    exact_map = _title_number_name_map(title, expected_count=len(items) if len(items) > 1 else None)
    for item in items:
        number = _normalize_number(item.get("number"))
        if not number:
            continue
        raw_name = str(item.get("name") or "")
        exact = _specific_name(exact_map.get(number, ""))
        if exact:
            name = exact
            if raw_name and raw_name.casefold() != exact.casefold():
                log.info("V36 exact-number title repair %s: %r -> %r", number, raw_name, exact)
        else:
            name = _validate_photo_name(raw_name, title, number)
            if not name and raw_name and not is_ocr_name_noise(raw_name):
                matched, score = _name_matches_title(raw_name, title)
                if score >= 0.70:
                    name = matched
        repaired.append({**item, "name": name, "number": number})
    return repaired

def _dedupe_pairs(items: list[dict], *, unique_numbers: bool = False) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    seen_numbers: set[str] = set()
    for item in items:
        name = _clean_name(str(item.get("name") or ""))
        number = _normalize_number(item.get("number"))
        if not name or not number:
            continue
        key = (name.casefold(), number)
        if key in seen:
            continue
        # For a multi-box photo, two different names claiming the exact same
        # Pop number are almost always a cross-box OCR error. Keep one and force
        # the pipeline to try the per-box grid before accepting the result.
        if unique_numbers and number in seen_numbers:
            continue
        seen.add(key)
        seen_numbers.add(number)
        out.append({"name": name, "number": number, "exclusive": item.get("exclusive") or None})
    return out[:_MAX_FIGURES]


def _prepare_vision_crop(crop: Image.Image) -> Image.Image:
    """Lightweight preprocessing for tiny/photographed box labels."""
    from PIL import ImageEnhance, ImageOps, ImageFilter
    img = ImageOps.exif_transpose(crop).convert("RGB")
    # Keep OCR text large enough for the Florence processor while avoiding huge
    # CPU tensors.  1400px on the long edge is a good CPU compromise.
    long_edge = max(img.size)
    if long_edge < 1400:
        scale = 1400 / max(1, long_edge)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    # Mild contrast/sharpening helps glossy, angled eBay photos without changing
    # the geometry used by the OCR-region task.
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _candidate_grid(expected_count: int | None) -> tuple[int, int]:
    if expected_count == 2:
        return 2, 1
    if expected_count == 3:
        # Common eBay layout: one box on top, two on the bottom.
        return 2, 2
    if expected_count == 4:
        return 2, 2
    if expected_count in {5, 6}:
        return 3, 2
    if expected_count in {7, 8}:
        return 3, 3
    return 2, 2


def _fast_grid_crops(image: Image.Image, expected_count: int | None) -> list[Image.Image]:
    width, height = image.size
    # Three-box listings are often arranged as one box above two boxes. This
    # layout keeps the top box intact instead of slicing it through a 2x2 grid.
    if expected_count == 3:
        gap = int(height * 0.50)
        overlap = int(height * 0.08)
        left_w = int(width * 0.56)
        top = image.crop((0, 0, width, min(height, gap + overlap)))
        left = image.crop((0, max(0, gap - overlap), left_w, height))
        right = image.crop((max(0, width-left_w), max(0, gap - overlap), width, height))
        return [top, left, right]

    cols, rows = _candidate_grid(expected_count)
    overlap = 0.18 if expected_count and expected_count <= 4 else 0.12
    out: list[Image.Image] = []
    cw, ch = width / cols, height / rows
    for r in range(rows):
        for c in range(cols):
            x1 = int(max(0, c*cw - (cw*overlap if c else 0)))
            y1 = int(max(0, r*ch - (ch*overlap if r else 0)))
            x2 = int(min(width, (c+1)*cw + (cw*overlap if c < cols-1 else 0)))
            y2 = int(min(height, (r+1)*ch + (ch*overlap if r < rows-1 else 0)))
            out.append(image.crop((x1, y1, x2, y2)))
    return out


def _title_numbers(title: str) -> list[str]:
    nums = []
    for m in re.finditer(r"(?<!\d)#?\s*(\d{3,4})(?!\d)", str(title or "")):
        n = _normalize_number(m.group(1))
        if n and n not in nums:
            nums.append(n)
    return nums


def _number_distance(a: str, b: str) -> int:
    a, b = str(a), str(b)
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


def _repair_number_from_title(number: str | None, title: str | None) -> str | None:
    """Repair one OCR digit only when the title has exactly the same-length ID.

    This can correct a visual OCR typo such as 1042 -> 1041. It never invents an
    absent number and never changes more than one digit.
    """
    n = _normalize_number(number)
    if not n or not title:
        return n
    candidates = [x for x in _title_numbers(title) if len(x) == len(n)]
    if not candidates:
        return n
    best = min(candidates, key=lambda x: _number_distance(n, x))
    return best if _number_distance(n, best) == 1 else n


def _is_retailer_sticker_name(text: str) -> bool:
    compact = re.sub(r"[^a-z]", "", str(text or "").casefold())
    if not compact:
        return False
    known = (
        "hottopic", "target", "walmart", "gamestop", "amazon",
        "boxlunch", "barnesnoble", "booksamillion", "exclusive",
    )
    from difflib import SequenceMatcher
    return compact in known or any(SequenceMatcher(None, compact, k).ratio() >= 0.78 for k in known)


def _text_region_variants(region: Image.Image) -> list[Image.Image]:
    """Small, high-signal augmentation set for Funko's printed nameplate.

    V30 deliberately avoids the old 3x3 OCR fan-out. The nameplate is a known
    region, so we use one large natural crop first and only one contrast variant
    if the first pass fails. The English PP-OCRv5 model is configured separately
    with a lower detection threshold for small white/blue/black labels.
    """
    from PIL import ImageEnhance, ImageOps, ImageFilter
    base = _prepare_vision_crop(region)
    # Nameplate text is often only a few pixels high in an eBay image. Give the
    # detector a genuinely large crop rather than repeatedly rescanning the box.
    scale = 1.6 if max(base.size) < 2200 else 1.0
    if scale > 1.0:
        base = base.resize((int(base.width * scale), int(base.height * scale)), Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(base)
    gray = ImageEnhance.Contrast(gray).enhance(1.45)
    gray = gray.filter(ImageFilter.SHARPEN)
    return [base, gray.convert("RGB")]


def _title_name_candidates(title: str) -> list[str]:
    if not title:
        return []
    try:
        return [x for x in (extract_title_character_names(title) or []) if x]
    except Exception:
        return []


def _canonical_name_from_ocr(raw_name: str, title: str) -> str:
    """Snap a readable-but-glued OCR result to a title name when clearly supported."""
    raw = _specific_name(raw_name)
    if not raw:
        return ""
    candidates = _title_name_candidates(title)
    if not candidates:
        return raw
    from difflib import SequenceMatcher
    compact_raw = re.sub(r"[^a-z0-9]", "", raw.casefold())
    best = None
    best_ratio = 0.0
    for candidate in candidates:
        cc = _specific_name(candidate)
        compact_c = re.sub(r"[^a-z0-9]", "", cc.casefold())
        if not compact_c:
            continue
        ratio = SequenceMatcher(None, compact_raw, compact_c).ratio()
        if compact_raw in compact_c or compact_c in compact_raw:
            ratio = max(ratio, min(len(compact_raw), len(compact_c)) / max(len(compact_raw), len(compact_c)))
        if ratio > best_ratio:
            best_ratio, best = ratio, cc
    # ~70% character-level agreement is enough for missing spaces / small OCR
    # errors, but intentionally too high for random blobs such as HOHIXIHOIW.
    return best if best and best_ratio >= 0.68 else raw


def _candidate_from_nameplate(region: Image.Image, *, title: str = "") -> list[dict]:
    rows: list[tuple[str, float]] = []
    for variant_index, variant in enumerate(_text_region_variants(region)):
        tokens = _fast_name_ocr_tokens(variant)
        names = _cluster_name_tokens(tokens, variant.size[1], variant.size[0])
        for name, cx, cy in names:
            clean = _specific_name(name)
            if not clean or _is_retailer_sticker_name(clean):
                continue
            compact = re.sub(r"[^a-z0-9]", "", clean.casefold())
            alpha = len(re.findall(r"[a-z]", clean.casefold()))
            if alpha < 4 or len(compact) < 4:
                continue
            # A real nameplate normally contains a multi-letter alphabetic phrase.
            # Penalize implausibly consonant-heavy OCR blobs such as HOHIXIHOIW.
            vowels = sum(ch in "aeiouy" for ch in compact)
            vowel_ratio = vowels / max(1, alpha)
            gibberish_penalty = 0.55 if vowel_ratio < 0.12 and len(compact) >= 7 else 0.0
            center_bonus = 1.0 - abs((cx / max(variant.size[0], 1)) - 0.50)
            score = alpha * 0.18 + center_bonus * 1.5 + (1.0 if variant_index == 0 else 0.4) - gibberish_penalty
            rows.append((clean, score))
    if not rows:
        return []
    votes: dict[str, list[float]] = {}
    examples: dict[str, str] = {}
    for name, score in rows:
        key = re.sub(r"[^a-z0-9]", "", name.casefold())
        votes.setdefault(key, []).append(score)
        examples[key] = name
    best = max(votes, key=lambda k: (len(votes[k]), sum(votes[k]), len(k)))
    return [{"name": examples[best], "number": None, "exclusive": None}]


def _bottom_fast_name(crop: Image.Image, number: str | None, *, title: str = "") -> list[dict]:
    """Read the Funko nameplate the old, broad way that worked better in practice.

    V31-V32 over-tightened the crop and repeatedly invoked the tiny-line detector. That
    made readable labels turn into empty detections or blobs. The nameplate is
    already known to be in the lower/front part of the box, so do ONE broad OCR
    pass with enough surrounding context, then only one alternate if necessary.
    """
    if not number:
        return []
    w, h = crop.size
    plate = _prepare_vision_crop(
        crop.crop((int(w * 0.03), int(h * 0.58), int(w * 0.99), int(h * 0.98)))
    )
    tokens = _fast_ocr_tokens(plate, status=None)
    names = _cluster_name_tokens(tokens, plate.size[1], plate.size[0])
    candidates = [_specific_name(row[0]) for row in names if _specific_name(row[0])]
    if not candidates:
        plate2 = _prepare_vision_crop(
            crop.crop((int(w * 0.02), int(h * 0.48), int(w * 0.99), int(h * 0.99)))
        )
        tokens2 = _fast_ocr_tokens(plate2, status=None)
        names2 = _cluster_name_tokens(tokens2, plate2.size[1], plate2.size[0])
        candidates = [_specific_name(row[0]) for row in names2 if _specific_name(row[0])]
    if not candidates:
        return []
    # If the marketplace title exposes character names, a long single-word OCR
    # blob must agree with one of them. This keeps the practical broad OCR flow
    # while preventing strings like HOHIXIHOIW from ever reaching eBay.
    title_candidates = _title_name_candidates(title)
    if title_candidates:
        from difflib import SequenceMatcher
        filtered = []
        for candidate in candidates:
            cc = re.sub(r"[^a-z0-9]", "", candidate.casefold())
            if len(cc) >= 7 and len(candidate.split()) == 1:
                ok = False
                for tc in title_candidates:
                    tt = re.sub(r"[^a-z0-9]", "", _specific_name(tc).casefold())
                    if tt and (cc == tt or cc in tt or tt in cc or SequenceMatcher(None, cc, tt).ratio() >= 0.62):
                        ok = True
                        break
                if not ok:
                    continue
            filtered.append(candidate)
        candidates = filtered
    if not candidates:
        return []
    candidates.sort(key=lambda x: (-len(x.split()), -len(x)))
    fixed_number = _repair_number_from_title(number, title) or number
    name = _canonical_name_from_ocr(candidates[0], title) or candidates[0]
    return [{"name": name, "number": fixed_number, "exclusive": None}]


def _pair_fast_tiles(tiles: list[Image.Image], expected_count: int | None, *, title: str = "", status: StatusCallback | None = None) -> list[dict]:
    """Recognize each spatial tile with one normal OCR pass before fallbacks."""
    results: list[dict] = []
    seen_numbers: set[str] = set()
    for tile in tiles:
        prepared = _prepare_vision_crop(tile)
        tokens = _fast_ocr_tokens(prepared, status=status)
        local = _pair_tokens(tokens, prepared.size, 1)
        if not local:
            nums = _numeric_tokens(tokens)
            if nums:
                local = _bottom_fast_name(tile, nums[0].text, title=title)
        if not local:
            w, h = prepared.size
            number_crop = _prepare_vision_crop(
                prepared.crop((int(w * 0.52), 0, w, int(h * 0.42)))
            )
            number_tokens = _fast_ocr_tokens(number_crop, status=status)
            nums = _numeric_tokens(number_tokens)
            if nums:
                num = _normalize_number(nums[0].text)
                local = _bottom_fast_name(tile, num, title=title)
        for row in local:
            number = _normalize_number(row.get("number"))
            name = _specific_name(str(row.get("name") or ""))
            if number and name and number not in seen_numbers:
                results.append({"name": name, "number": number, "exclusive": row.get("exclusive") or None})
                seen_numbers.add(number)
        if expected_count and len(results) >= expected_count:
            break
    return _dedupe_pairs(results, unique_numbers=True)

def _dedupe_numbered_pairs(items: list[dict]) -> list[dict]:
    """Dedupe exact name+number pairs; different names may share a number."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        n = _normalize_number(item.get("number"))
        name = _specific_name(str(item.get("name") or ""))
        if not n:
            continue
        key = (name.casefold(), n)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "number": n, "exclusive": item.get("exclusive") or None})
    return out[:_MAX_FIGURES]


def _number_candidates_from_crop(crop: Image.Image, *, status: StatusCallback | None = None) -> list[OCRToken]:
    # Pop numbers are large but stylized; use a lower OCR acceptance threshold
    # for this targeted region so a valid 3/4-digit number is not discarded
    # merely because the banner is glossy or angled.
    try:
        tokens = _fast_ocr_tokens(crop, status=status, min_score=0.20)
    except TypeError:
        # Backward-compatible test/custom OCR adapters that only accept the old
        # signature still work; their default threshold remains the fallback.
        tokens = _fast_ocr_tokens(crop, status=status)
    return sorted(_numeric_tokens(tokens), key=lambda n: (-n.score, -len(_normalize_number(n.text) or ""), abs(n.cx - crop.size[0] * 0.72)))


def _number_first(tile: Image.Image, *, title: str = "", status: StatusCallback | None = None) -> str | None:
    """Read the big top-right Pop number with several cheap, overlapping crops.

    Funko photos frequently contain stickers/text in the upper half. A single
    50%-width crop can therefore miss or confuse the actual number. We vote across
    three right/top windows and only accept a number seen with strong evidence.
    """
    w, h = tile.size
    crops = [
        tile.crop((int(w * .66), 0, w, int(h * .42))),
        tile.crop((int(w * .48), 0, w, int(h * .55))),
        tile.crop((int(w * .58), int(h * .02), w, int(h * .32))),
    ]
    votes: dict[str, float] = {}
    counts: dict[str, int] = {}
    for crop in crops:
        prepared = _prepare_vision_crop(crop)
        candidates = _number_candidates_from_crop(prepared, status=status)
        for token in candidates[:3]:
            number = _normalize_number(token.text)
            if not number:
                continue
            # Prefer high-confidence, 3/4 digit candidates toward the right side.
            right_bonus = 1.0 + 0.7 * (token.cx / max(prepared.size[0], 1))
            score = max(0.1, token.score) * right_bonus * (1.15 if len(number) == 4 else 1.0)
            votes[number] = votes.get(number, 0.0) + score
            counts[number] = counts.get(number, 0) + 1
    if not votes:
        return None
    best = max(votes, key=lambda n: (counts[n] >= 2, counts[n], votes[n]))
    # A single high-confidence hit is acceptable; agreement gets priority.
    # Never mutate a number actually read from the photo. The title may only
    # help choose among multiple photo candidates that are already present.
    return best


def _pair_fast_tiles(tiles: list[Image.Image], expected_count: int | None, *, title: str = "", status: StatusCallback | None = None) -> list[dict]:
    results: list[dict] = []
    seen_numbers: set[str] = set()
    for tile in tiles:
        prepared = _prepare_vision_crop(tile)
        local: list[dict] = []
        num = _number_first(prepared, title=title, status=status) if _FAST_OCR_BACKEND == "rapidocr" else None
        if num:
            local = _bottom_fast_name(prepared, num, title=title)
            # Keep the number even when the nameplate is unreadable. The engine
            # will perform a safe title/local-catalog recovery instead of dropping
            # the physical box.
            if not local:
                local = [{"name": "", "number": num, "exclusive": None}]
        if not local:
            tokens = _fast_ocr_tokens(prepared, status=status)
            local = _pair_tokens(tokens, prepared.size, 1)
            if not local:
                nums2 = _numeric_tokens(tokens)
                if nums2:
                    n2 = _normalize_number(nums2[0].text)
                    if n2:
                        local = _bottom_fast_name(prepared, n2, title=title) or [{"name": "", "number": n2, "exclusive": None}]
        for row in local:
            number = _normalize_number(row.get("number"))
            name = _specific_name(str(row.get("name") or ""))
            if number and number not in seen_numbers:
                results.append({"name": name, "number": number, "exclusive": row.get("exclusive") or None})
                seen_numbers.add(number)
        if expected_count and len(results) >= expected_count:
            break
    return _dedupe_numbered_pairs(results)


def _name_matches_title(name: str, title: str) -> tuple[str, float]:
    """Return the closest title name and similarity for a photo OCR candidate."""
    clean = _specific_name(name)
    if not clean:
        return "", 0.0
    candidates = _title_name_candidates(title)
    if not candidates:
        return clean, 0.0
    from difflib import SequenceMatcher
    a = re.sub(r"[^a-z0-9]", "", clean.casefold())
    best_name, best_score = clean, 0.0
    for candidate in candidates:
        c = _specific_name(candidate)
        b = re.sub(r"[^a-z0-9]", "", c.casefold())
        if not b:
            continue
        score = SequenceMatcher(None, a, b).ratio()
        if a and (a in b or b in a):
            score = max(score, min(len(a), len(b)) / max(len(a), len(b)))
        if score > best_score:
            best_name, best_score = c, score
    return best_name, best_score


def _validate_photo_name(name: str, title: str = "", number: str | None = None) -> str:
    """Final photo-name gate: canonicalize or reject OCR garbage."""
    raw = str(name or "").strip()
    clean = _specific_name(raw)
    if not clean:
        return ""

    # Hard stop-word barrier before fuzzy matching. Packaging/legal/system text
    # must never become a character name merely because it is a long OCR line.
    if is_ocr_name_noise(raw) or is_ocr_name_noise(clean):
        if number:
            exact = _specific_name(_title_number_name_map(title).get(str(number), ""))
            if exact:
                return exact
        return ""

    compact = re.sub(r"[^a-z0-9]", "", clean.casefold())
    if compact in _OCR_GARBAGE_NAMES:
        return ""
    alpha = sum(ch.isalpha() for ch in compact)
    if alpha < 4 or len(compact) < 4:
        return ""

    # Exact name+number in the title is the strongest repair signal because the
    # number itself came from the photograph.
    if number:
        exact = _specific_name(_title_number_name_map(title).get(str(number), ""))
        if exact:
            exact_compact = re.sub(r"[^a-z0-9]", "", exact.casefold())
            from difflib import SequenceMatcher
            ratio = SequenceMatcher(None, compact, exact_compact).ratio() if compact and exact_compact else 0.0
            if compact == exact_compact or ratio >= 0.55 or compact in exact_compact or exact_compact in compact:
                return exact
            # Explicit title pair exists for the photo-provided number: do not
            # allow a conflicting OCR string through to the comparator.
            return exact if is_suspicious_ocr_name(clean) else ""

    candidates = _title_name_candidates(title)
    if candidates:
        matched, similarity = _name_matches_title(clean, title)
        # Mild OCR damage / missing spaces is acceptable when title candidates
        # support the same character identity.
        if similarity >= 0.58:
            return matched
        if is_suspicious_ocr_name(clean):
            return ""

    # Without title candidates, only allow clearly readable names. Long single-
    # token OCR hallucinations with implausible vowel structure are rejected.
    vowels = sum(ch in "aeiouy" for ch in compact)
    vowel_ratio = vowels / max(alpha, 1)
    if len(compact) >= 9 and len(clean.split()) == 1 and vowel_ratio < 0.20:
        return ""
    return clean


def is_suspicious_ocr_name(text: str) -> bool:
    """Cheap deterministic detector for OCR soup such as ``IE FASFIOA``."""
    clean = _specific_name(str(text or ""))
    if not clean:
        return True
    compact = re.sub(r"[^a-z0-9]", "", clean.casefold())
    if compact in _OCR_GARBAGE_NAMES:
        return True
    words = clean.split()
    if any(len(w) <= 2 for w in words) and len(words) >= 2:
        return True
    alpha = sum(ch.isalpha() for ch in compact)
    vowels = sum(ch in "aeiouy" for ch in compact)
    if alpha >= 7 and vowels / max(alpha, 1) < 0.22:
        return True
    if len(words) >= 4 and sum(len(w) <= 4 for w in words) >= max(3, len(words) - 1):
        return True
    return False

def _virtual_box_regions_for_numbers(
    numbers: list[OCRToken], image_size: tuple[int, int]
) -> list[tuple[OCRToken, tuple[int, int, int, int]]]:
    """Build coarse physical-box regions from Pop-number anchors.

    Funko's printed number is normally near the upper-right of its own box. The
    regions are only used for a single nameplate recognition-only fallback; they
    are not a second detector and do not attempt object detection.
    """
    width, height = image_size
    if not numbers:
        return []
    # Most lots are horizontal. If anchors form a vertical stack, split on Y.
    xs = sorted(n.cx for n in numbers)
    ys = sorted(n.cy for n in numbers)
    horizontal_span = (max(xs) - min(xs)) / max(width, 1)
    vertical_span = (max(ys) - min(ys)) / max(height, 1)
    horizontal = len(numbers) == 1 or horizontal_span >= vertical_span * 0.8
    ordered = sorted(numbers, key=lambda n: (n.cx, n.cy)) if horizontal else sorted(numbers, key=lambda n: (n.cy, n.cx))
    regions: list[tuple[OCRToken, tuple[int, int, int, int]]] = []
    for i, number in enumerate(ordered):
        if horizontal:
            left = 0 if i == 0 else int((ordered[i-1].cx + number.cx) / 2)
            right = width if i == len(ordered)-1 else int((number.cx + ordered[i+1].cx) / 2)
            # Number is at the right side of its box; give the first box enough
            # left context even when there are only two anchors.
            box = (max(0, left), 0, min(width, max(right, int(number.cx + width * 0.04))), height)
        else:
            top = 0 if i == 0 else int((ordered[i-1].cy + number.cy) / 2)
            bottom = height if i == len(ordered)-1 else int((number.cy + ordered[i+1].cy) / 2)
            box = (0, max(0, top), width, min(height, max(bottom, int(number.cy + height * 0.04))))
        regions.append((number, box))
    return regions


def _recover_nameplate_once(
    crop: Image.Image,
    number: OCRToken,
    box: tuple[int, int, int, int],
    title: str,
    *,
    status: StatusCallback | None = None,
) -> str:
    """One recognition-only attempt on the already-known physical box."""
    x1, y1, x2, y2 = box
    region = crop.crop((x1, y1, x2, y2))
    rw, rh = region.size
    # Nameplate is on the lower/front part. Keep a generous band: perspective
    # and retailer photos make hard-coded micro-crops unreliable.
    plate = region.crop((int(rw * 0.04), int(rh * 0.55), int(rw * 0.98), int(rh * 0.98)))
    tokens = _direct_name_recognition(plate, status=status)
    candidates: list[tuple[str, float]] = []
    for token in tokens:
        clean = _validate_photo_name(token.text, title, str(number.text))
        if clean:
            candidates.append((clean, token.score))
    if not candidates:
        return ""
    # Prefer title-supported candidates, then OCR confidence.
    candidates.sort(key=lambda item: (1 if _name_matches_title(item[0], title)[1] >= 0.68 else 0, item[1], len(item[0])), reverse=True)
    return candidates[0][0]



def _v34_grid_dimensions(n: int, image_size: tuple[int, int]) -> tuple[int, int]:
    """Choose columns/rows for a portrait-box bundle without looking at OCR."""
    n = max(1, min(int(n), _MAX_FIGURES))
    if n == 1:
        return 1, 1
    width, height = image_size
    aspect = max(0.25, min(5.0, width / max(height, 1)))
    import math
    ideal_cols = math.sqrt(n * aspect / 0.72)
    cols = max(1, min(n, int(round(ideal_cols))))
    rows = int(math.ceil(n / cols))
    # Keep tiles from becoming implausibly wide/tall for a portrait package.
    while cols > 1 and (width / cols) < (height / rows) * 0.38:
        cols -= 1
        rows = int(math.ceil(n / cols))
    while rows > 1 and (height / rows) < (width / cols) * 0.34 and cols < n:
        cols += 1
        rows = int(math.ceil(n / cols))
    return cols, rows


def _v34_tile_activity(tile: Image.Image) -> float:
    """Cheap CV-only occupancy score; never invokes OCR or a semantic model."""
    try:
        import numpy as np
        import cv2
        arr = np.asarray(tile.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 70, 160)
        return float(np.mean(edges > 0)) * 4.0 + min(2.0, float(np.std(gray)) / 64.0)
    except Exception:
        try:
            import numpy as np
            return float(np.asarray(tile.convert("RGB"), dtype=np.float32).std()) / 64.0
        except Exception:
            return 1.0


def _v34_isolated_box_tiles(image: Image.Image, expected_count: int) -> list[Image.Image]:
    """Slice a multi-box photo into spatially independent physical-box crops.

    V34 deliberately does not run any OCR on the whole photograph. Known bundle
    counts are partitioned geometrically first; only the resulting box crops are
    passed into RapidOCR.
    """
    n = max(1, min(int(expected_count), _MAX_FIGURES))
    width, height = image.size
    if n == 1:
        return [image]

    # Common Funko eBay 3-box layout: one on top, two on bottom. This prevents the
    # old full-width 2x2 split from cutting/mixing the physical boxes.
    if n == 3 and (width / max(height, 1)) >= 2.0:
        # Three boxes in one horizontal row.
        cw = width / 3
        ox = cw * 0.045
        return [
            image.crop((max(0, int(0*cw-ox)), 0, min(width, int(1*cw+ox)), height)),
            image.crop((max(0, int(1*cw-ox)), 0, min(width, int(2*cw+ox)), height)),
            image.crop((max(0, int(2*cw-ox)), 0, width, height)),
        ]

    if n == 3:
        # Common eBay layout: one centered box above two boxes below. The old
        # implementation gave the upper box the FULL photo width, making its
        # top-right Pop number relatively tiny and easy for OCR to miss. Keep the
        # physical box isolated but tighten the upper crop horizontally.
        y = int(height * 0.54)
        yo = int(height * 0.035)
        cx1 = int(width * 0.10)
        cx2 = int(width * 0.90)
        x = int(width * 0.50)
        xo = int(width * 0.035)
        regions = [
            (max(0, cx1), 0, min(width, cx2), min(height, y + yo)),
            (0, max(0, y - yo), min(width, x + xo), height),
            (max(0, x - xo), max(0, y - yo), width, height),
        ]
        return [image.crop(r) for r in regions]

    cols, rows = _v34_grid_dimensions(n, image.size)
    ox, oy = 0.06, 0.06
    cells: list[tuple[float, int, int, Image.Image]] = []
    cw, ch = width / cols, height / rows
    for r in range(rows):
        for c in range(cols):
            x1 = int(max(0, c*cw - (cw*ox if c else 0)))
            y1 = int(max(0, r*ch - (ch*oy if r else 0)))
            x2 = int(min(width, (c+1)*cw + (cw*ox if c < cols-1 else 0)))
            y2 = int(min(height, (r+1)*ch + (ch*oy if r < rows-1 else 0)))
            if x2 <= x1 or y2 <= y1:
                continue
            tile = image.crop((x1,y1,x2,y2))
            cells.append((_v34_tile_activity(tile), r, c, tile))

    # Non-perfect grids (5/7/10/11) can contain an empty cell. Use cheap visual
    # occupancy to discard only the emptiest candidates, then restore spatial order.
    cells.sort(key=lambda item: item[0], reverse=True)
    selected = cells[:n]
    selected.sort(key=lambda item: (item[1], item[2]))
    return [item[3] for item in selected]


def _v34_prepare_box_for_ocr(tile: Image.Image) -> Image.Image:
    """Normalize the lower Funko nameplate before the single OCR pass."""
    prepared = _prepare_vision_crop(tile)
    # Large lots can create narrow cells. Upscale before the single detector pass
    # so the printed Pop number has enough pixels for recognition.
    if prepared.width < 600:
        scale = 2.5 if prepared.width < 400 else 2.0
        prepared = prepared.resize(
            (int(prepared.width * scale), int(prepared.height * scale)),
            Image.Resampling.LANCZOS,
        )
    try:
        import cv2
        import numpy as np
        arr = np.asarray(prepared.convert("RGB"))[:, :, ::-1].copy()
        h, w = arr.shape[:2]
        y1, y2 = int(h * 0.72), int(h * 0.98)
        band = arr[y1:y2, :]
        if band.size == 0:
            return prepared
        gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
        edge_mean = float(np.mean(np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])))
        if edge_mean < 125.0:
            gray = cv2.bitwise_not(gray)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thresh = cv2.medianBlur(thresh, 3)
        rgb_band = cv2.cvtColor(thresh, cv2.COLOR_GRAY2RGB)
        out = np.asarray(prepared.convert("RGB")).copy()
        out[y1:y2, :] = rgb_band
        return Image.fromarray(out)
    except Exception:
        log.debug("V44 preprocessing failed", exc_info=True)
        return prepared


def _pop_badge_number_candidates(tokens: list[OCRToken], image_size: tuple[int, int]) -> list[tuple[float, OCRToken, str]]:
    """Return number candidates that are structurally attached to the POP! badge.

    The critical V44 rule is that arbitrary 2-4 digit substrings from background
    text are NOT Pop numbers. Prefer a numeric token that sits on the same header
    line as the POP! logo and to its right. Glued forms such as ``BATMAN152`` or
    ``FUSION1006`` are accepted only when they are in that same badge area.
    """
    width, height = image_size
    # RapidOCR may return boxes in its internally resized coordinate space.
    # Normalize geometry against the observed token canvas so a valid top-right
    # badge is not rejected simply because its coordinates exceed the PIL crop.
    max_x = max((abs(float(t.cx)) for t in tokens), default=float(width))
    max_y = max((abs(float(t.cy)) for t in tokens), default=float(height))
    eff_width = max(float(width), max_x * 1.02)
    eff_height = max(float(height), max_y * 1.02)
    pop_tokens = [
        t for t in tokens
        if re.search(r"(?i)\bPOP!?\b", str(t.text or ""))
    ]
    # The detector occasionally returns coordinates slightly outside the nominal
    # resized image. Ratios are therefore used instead of hard clipping.
    raw_candidates: list[tuple[OCRToken, str]] = []
    for token in tokens:
        raw = str(token.text or "").strip()
        if not raw:
            continue
        compact = re.sub(r"[^A-Za-z0-9#]", "", raw).upper()
        # Never extract fragments from long bare serials, UPCs, copyright years,
        # or storefront codes. Short glued product labels remain allowed.
        digit_runs = re.findall(r"\d{2,4}", raw)
        if not digit_runs:
            translated = compact.translate(str.maketrans({"O":"0","I":"1","L":"1","S":"5"}))
            digit_runs = re.findall(r"\d{2,4}", translated)
        if not digit_runs:
            continue
        if len(re.sub(r"\D", "", raw)) > 4 and not re.search(r"(?i)(?:pop|#)", raw):
            # Example: 61175983, 518927, 0000028857F2. These are not badge ids.
            continue
        for run in digit_runs:
            number = _normalize_number(run)
            if number:
                raw_candidates.append((token, number))

    scored: list[tuple[float, OCRToken, str]] = []
    if pop_tokens:
        for token, number in raw_candidates:
            for pop in pop_tokens:
                dy = abs(token.cy - pop.cy) / max(eff_height, 1)
                dx = (token.cx - pop.cx) / max(eff_width, 1)
                if dx < -0.03:
                    continue
                if dy > 0.13:
                    continue
                # Number badge is normally to the right of POP! and in the same
                # upper header band. Give those structural facts most of the score.
                score = 6.0
                score += 3.5 * max(0.0, min(1.2, dx / 0.5))
                score += 2.0 * token.score
                score += 1.6 * max(0.0, 0.13 - dy) / 0.13
                if "#" in token.text:
                    score += 1.5
                if re.search(r"(?i)POP", token.text):
                    score += 1.25
                if len(number) == 4:
                    score += 0.8
                # If the candidate is implausibly far right of POP!, still allow
                # it (some crops are skewed), but don't let it dominate on distance.
                score -= 0.6 * max(0.0, dx - 0.85)
                scored.append((score, token, number))
    if scored:
        scored.sort(key=lambda item: (item[0], item[1].score, len(item[2])), reverse=True)
        return scored

    # Secondary structural fallback: upper-right header only. This is deliberately
    # strict and never scans arbitrary background numbers across the whole photo.
    for token, number in raw_candidates:
        x_ratio = token.cx / max(eff_width, 1)
        y_ratio = token.cy / max(eff_height, 1)
        if x_ratio < 0.25 or y_ratio > 0.50:
            continue
        score = token.score * 2.0 + 2.0 * x_ratio + (0.7 if len(number) == 4 else 0.0)
        if "#" in token.text:
            score += 1.0
        if re.search(r"(?i)POP", token.text):
            score += 1.0
        scored.append((score, token, number))
    scored.sort(key=lambda item: (item[0], item[1].score, len(item[2])), reverse=True)
    return scored


def _v34_pick_pop_number(tokens: list[OCRToken], image_size: tuple[int, int], title: str = "") -> str | None:
    """Pick only the number physically attached to the Funko POP! header.

    ``title`` is intentionally ignored for choosing the number. A title can repair
    a *name* after the photo proves a number, but it must never decide which of
    several OCR numbers is the Pop number.
    """
    candidates = _pop_badge_number_candidates(tokens, image_size)
    if not candidates:
        return None
    return _normalize_number(candidates[0][2])



def _header_badge_anchors(tokens: list[OCRToken], image_size: tuple[int, int]) -> list[tuple[OCRToken, str]]:
    """Find distinct physical Pop! badge anchors on a full listing photo.

    Unlike the old count heuristic, this counts POP! logos first and only accepts
    a number when it sits immediately to the right of that same logo. This avoids
    counting background/DVD/UPC numbers as extra Funko boxes.
    """
    width, height = image_size
    pop_tokens = [
        t for t in tokens
        if re.search(r"(?i)\bPOP!?\b", str(t.text or ""))
        and (t.cy / max(height, 1)) < 0.93
    ]
    pop_tokens.sort(key=lambda t: (t.cy, t.cx))
    # Deduplicate repeated POP! artwork / overlapping detections.
    pops: list[OCRToken] = []
    for pop in pop_tokens:
        if any(abs(pop.cx - q.cx) < width * 0.07 and abs(pop.cy - q.cy) < height * 0.07 for q in pops):
            continue
        pops.append(pop)

    def candidate_numbers(pop: OCRToken) -> list[tuple[float, OCRToken, str]]:
        out: list[tuple[float, OCRToken, str]] = []
        for tok in tokens:
            nums = _number_fragments(tok.text)
            if not nums:
                continue
            for number in nums:
                dx = (tok.cx - pop.cx) / max(width, 1)
                dy = abs(tok.cy - pop.cy) / max(height, 1)
                if dx < 0.03 or dx > 0.72 or dy > 0.12:
                    continue
                # Reject obvious serial/UPC strings even if OCR extracted a
                # fragment. Standalone 2-4 digit badge text is allowed.
                digits = re.sub(r"\D", "", str(tok.text or ""))
                if len(digits) > 4 and not re.search(r"(?i)(?:#|pop)", str(tok.text or "")):
                    continue
                score = tok.score * 3.0 + 5.0 * max(0.0, 1.0 - dx / 0.72) + 4.0 * max(0.0, 1.0 - dy / 0.12)
                if len(number) == 4:
                    score += 0.15
                out.append((score, tok, number))
        return sorted(out, key=lambda x: (x[0], x[1].score, len(x[2])), reverse=True)

    anchors: list[tuple[OCRToken, str]] = []
    for pop in pops:
        nums = candidate_numbers(pop)
        if nums:
            # One physical badge -> one best number. The same number cannot be
            # reused for another POP! anchor at almost identical coordinates.
            best = nums[0]
            if not any(abs(pop.cx - q.cx) < width * 0.07 and abs(pop.cy - q.cy) < height * 0.07 for q, _ in anchors):
                anchors.append((pop, best[2]))
    return anchors[:_MAX_FIGURES]


def _adaptive_box_regions(image_size: tuple[int, int], anchors: list[tuple[OCRToken, str]], expected_count: int | None) -> list[tuple[int, int, int, int, OCRToken, str]]:
    """Build physical-box crop regions from detected POP! anchors.

    The anchor geometry selects the layout dynamically: horizontal rows, vertical
    stacks, 2x2 grids, or the common one-above-two-below Funko arrangement.
    """
    width, height = image_size
    if not anchors:
        return []
    # Deduplicate and stabilize by visual position.
    points = sorted(anchors, key=lambda a: (a[0].cy, a[0].cx))
    n = len(points)
    if expected_count and expected_count > 1 and n > expected_count:
        points = points[:expected_count]
        n = len(points)
    if n == 1:
        return [(0, 0, width, height, points[0][0], points[0][1])]

    xs = [p[0].cx for p in points]
    ys = [p[0].cy for p in points]
    y_span = (max(ys) - min(ys)) / max(height, 1)
    x_span = (max(xs) - min(xs)) / max(width, 1)

    def split_bounds(vals: list[float], total: int) -> list[tuple[int, int]]:
        vals = sorted(vals)
        if len(vals) == 1:
            return [(0, total)]
        mids = [int((vals[i] + vals[i+1]) / 2) for i in range(len(vals)-1)]
        out=[]; start=0
        for mid in mids:
            out.append((start, max(start+1, mid))); start=max(start+1, mid)
        out.append((start,total))
        return out

    regions: list[tuple[int,int,int,int,OCRToken,str]] = []
    # 3-box classic stack: one above, two below.
    if n == 3:
        top = [p for p in points if p[0].cy < sum(ys)/len(ys) - height*0.08]
        bottom = [p for p in points if p not in top]
        if len(top) == 1 and len(bottom) == 2:
            top_p = top[0]
            min_bottom_y = min(p[0].cy for p in bottom)
            boundary = int((top_p[0].cy + min_bottom_y) / 2)
            bx = sorted(bottom, key=lambda p:p[0].cx)
            left_mid = int((bx[0][0].cx + bx[1][0].cx)/2)
            regions.append((0,0,width,max(1,boundary),top_p[0],top_p[1]))
            regions.append((0,max(0,boundary-int(height*0.02)),min(width,left_mid),height,bx[0][0],bx[0][1]))
            regions.append((max(0,left_mid-int(width*0.02)),max(0,boundary-int(height*0.02)),width,height,bx[1][0],bx[1][1]))
            return regions
        if y_span < 0.18 and x_span > 0.25:
            xb = split_bounds(xs, width)
            ordered = sorted(points, key=lambda p:p[0].cx)
            for (x1,x2), p in zip(xb, ordered):
                regions.append((max(0,x1-int(width*0.015)),0,min(width,x2+int(width*0.015)),height,p[0],p[1]))
            return regions

    # 2-box / all-one-row layouts.
    if y_span < 0.16:
        ordered = sorted(points, key=lambda p:p[0].cx)
        xb = split_bounds([p[0].cx for p in ordered], width)
        for (x1,x2), p in zip(xb, ordered):
            regions.append((max(0,x1-int(width*0.02)),0,min(width,x2+int(width*0.02)),height,p[0],p[1]))
        return regions
    if x_span < 0.16:
        ordered = sorted(points, key=lambda p:p[0].cy)
        yb = split_bounds([p[0].cy for p in ordered], height)
        for (y1,y2), p in zip(yb, ordered):
            regions.append((0,max(0,y1-int(height*0.02)),width,min(height,y2+int(height*0.02)),p[0],p[1]))
        return regions

    # General grid: cluster into rows using a tolerance derived from the median
    # vertical spacing, then split each row horizontally.
    row_tol = max(height*0.12, height*0.12 if n <= 4 else height*0.08)
    rows: list[list[tuple[OCRToken,str]]] = []
    for p in points:
        placed = False
        for row in rows:
            if abs(p[0].cy - sum(q[0].cy for q in row)/len(row)) <= row_tol:
                row.append(p); placed=True; break
        if not placed: rows.append([p])
    rows.sort(key=lambda row: sum(p[0].cy for p in row)/len(row))
    row_centers=[sum(p[0].cy for p in row)/len(row) for row in rows]
    y_bounds=[]
    if len(row_centers)==1: y_bounds=[(0,height)]
    else:
        y_bounds=split_bounds(row_centers, height)
    for ri,row in enumerate(rows):
        row.sort(key=lambda p:p[0].cx)
        xb=split_bounds([p[0].cx for p in row], width)
        y1,y2=y_bounds[min(ri,len(y_bounds)-1)]
        for (x1,x2),p in zip(xb,row):
            regions.append((max(0,x1-int(width*0.015)), max(0,y1-int(height*0.02)), min(width,x2+int(width*0.015)), min(height,y2+int(height*0.02)),p[0],p[1]))
    return regions[:_MAX_FIGURES]


def _infer_photo_layout(image: Image.Image, expected_count: int | None = None, *, status: StatusCallback | None = None) -> tuple[int | None, list[tuple[int,int,int,int,OCRToken,str]]]:
    # V48: Run badge detection for ALL multi-box listings including 2-box.
    # Previously 2-box was skipped and got no anchor_num, so Pop numbers had to
    # be found purely inside each half-crop — unreliable for compressed eBay photos.
    # Now badge detection runs for 2-box too; if it succeeds we get exact #358/#359
    # anchors; if it fails we fall back to the geometric splitter as before.
    try:
        tokens = _fast_ocr_tokens(_prepare_vision_crop(image), status=status, min_score=0.35)
    except Exception:
        log.debug("V44 full-photo OCR layout pass failed", exc_info=True)
        if expected_count:
            return expected_count, []
        return None, []
    anchors = _header_badge_anchors(tokens, image.size)
    regions = _adaptive_box_regions(image.size, anchors, expected_count)
    if anchors:
        log.info("V44 photo layout: badges=%s numbers=%s", len(anchors), [a[1] for a in anchors])
        if expected_count and len(regions) < expected_count:
            # We did not get enough reliable physical anchors. Fall back to the
            # known-count geometric splitter rather than pretending one giant crop
            # is a single box.
            return expected_count, []
        return len(regions), regions
    if expected_count:
        return expected_count, []
    return None, []

def _infer_photo_box_count(image: Image.Image, *, status: StatusCallback | None = None) -> int | None:
    """Infer how many physical Funko boxes are visible when the title is generic.

    We use a single full-image OCR pass only to count *POP!*+number badge pairs.
    The full-image pass never supplies an identity to the comparator; it only tells
    the per-box slicer whether a supposedly generic "Anime Funko Bundle" is really
    a multi-box photograph.
    """
    try:
        tokens = _fast_ocr_tokens(_prepare_vision_crop(image), status=status, min_score=0.35)
    except Exception:
        log.debug("V44 photo box-count OCR failed", exc_info=True)
        return None
    candidates = _pop_badge_number_candidates(tokens, image.size)
    if not candidates:
        return None
    width, height = image.size
    # Keep the best candidate for each spatially distinct badge/number pair.
    chosen: list[tuple[float, float, float, str]] = []
    for score, token, number in sorted(candidates, key=lambda x: (x[0], x[1].score), reverse=True):
        # Full-image OCR can contain repeated POP! artwork. Require a genuinely
        # distinct location before counting another physical box.
        too_close = False
        for _, cx, cy, existing_number in chosen:
            if existing_number == number and abs(token.cx - cx) < width * 0.10 and abs(token.cy - cy) < height * 0.12:
                too_close = True
                break
            if abs(token.cx - cx) < width * 0.08 and abs(token.cy - cy) < height * 0.10:
                too_close = True
                break
        if not too_close:
            chosen.append((score, token.cx, token.cy, number))
        if len(chosen) >= _MAX_FIGURES:
            break
    count = len(chosen)
    if count >= 2:
        log.info("V44 photo box-count inference: badges=%s numbers=%s", count, [x[3] for x in chosen])
        return count
    return 1 if count == 1 else None

def _nameplate_candidates(tokens: list[OCRToken], image_size: tuple[int, int]) -> list[tuple[float, str, OCRToken]]:
    """Rank character names from the printed front nameplate, not legal copy."""
    width, height = image_size
    max_x = max((abs(float(t.cx)) for t in tokens), default=float(width))
    max_y = max((abs(float(t.cy)) for t in tokens), default=float(height))
    eff_width = max(float(width), max_x * 1.02)
    eff_height = max(float(height), max_y * 1.02)

    service_tokens: list[OCRToken] = []
    for t in tokens:
        low = re.sub(r"[^a-z]+", " ", str(t.text or "").casefold()).strip()
        if re.search(r"\b(?:bobble|head|vinyl|figure|figurine|figura|warning|attention|danger|choking|made|china|months|under)\b", low):
            service_tokens.append(t)

    candidates: list[tuple[float, str, OCRToken]] = []
    for token in tokens:
        raw_text = str(token.text or "")
        clean = _specific_name(raw_text)
        if (not clean or is_ocr_name_noise(raw_text) or is_suspicious_ocr_name(clean)
                or _is_retailer_sticker_name(clean)):
            continue
        low = re.sub(r"[^a-z]+", " ", clean.casefold()).strip()
        if re.search(r"\b(?:vinyl|figure|figurine|warning|attention|danger|choking|bobble|head|made|china|months|under|age|only|exclusive|at|the office|television|animation)\b", low):
            continue
        compact = re.sub(r"[^a-z0-9]", "", clean.casefold())
        alpha = sum(ch.isalpha() for ch in compact)
        if alpha < 4 or alpha > 42 or any(ch.isdigit() for ch in compact):
            continue
        words = clean.split()
        if len(words) > 6:
            continue
        # Penalize low-quality lower-case fragments and sentence-like OCR.
        letters = re.sub(r"[^A-Za-z]", "", clean)
        upper_ratio = sum(c.isupper() for c in letters) / max(1, len(letters))
        lower_words = sum(1 for w in words if w.islower())
        gibberish_penalty = 0.0
        if len(words) >= 3 and lower_words >= 2:
            gibberish_penalty += 2.5
        if len(words) >= 4:
            gibberish_penalty += 1.5
        if any(len(w) <= 2 for w in words) and len(words) >= 3:
            gibberish_penalty += 1.0
        vowels = sum(ch in "aeiouy" for ch in compact)
        if alpha >= 7 and vowels / max(alpha, 1) < 0.20:
            gibberish_penalty += 2.0
        y = token.cy / max(eff_height, 1.0)
        if y < 0.12 or y > 0.94:
            continue
        score = token.score * 3.0
        score += min(3.0, alpha * 0.10)
        score += max(0.0, 1.4 - abs(y - 0.75) * 2.0)
        score += max(0.0, 0.8 - abs((token.cx / max(eff_width, 1)) - 0.58))
        if upper_ratio >= 0.45:
            score += 0.9
        score -= gibberish_penalty

        # Most Funko boxes print the character name immediately above the
        # multilingual "VINYL FIGURE / BOBBLE-HEAD / FIGURA ..." line. This
        # relationship is much stronger than an absolute y-coordinate.
        best_gap = None
        for marker in service_tokens:
            dy = marker.cy - token.cy
            if dy <= 0 or dy > eff_height * 0.16:
                continue
            dx = abs(marker.cx - token.cx)
            if dx <= eff_width * 0.30:
                best_gap = dy if best_gap is None else min(best_gap, dy)
        if best_gap is not None:
            score += 4.0 + max(0.0, 1.5 - best_gap / max(eff_height * 0.08, 1.0))
        candidates.append((score, clean, token))

    # Combine adjacent tokens only when they sit on the same lower nameplate line.
    if len(candidates) >= 2:
        base_tokens = [row[2] for row in candidates]
        for name, cx, cy in _cluster_name_tokens(base_tokens, int(eff_height), int(eff_width)):
            clean = _specific_name(name)
            if not clean or is_ocr_name_noise(clean) or is_suspicious_ocr_name(clean) or _is_retailer_sticker_name(clean):
                continue
            compact = re.sub(r"[^a-z0-9]", "", clean.casefold())
            if len(compact) < 4 or len(compact) > 42:
                continue
            pseudo = OCRToken(clean, cx, cy, (0,0,0,0,0,0,0,0), 0.95)
            score = 4.0 + min(3.0, len(compact) * 0.10)
            candidates.append((score, clean, pseudo))

    out: list[tuple[float, str, OCRToken]] = []
    seen: set[str] = set()
    for row in sorted(candidates, key=lambda x: (x[0], x[2].score), reverse=True):
        key = re.sub(r"[^a-z0-9]", "", row[1].casefold())
        if key and key not in seen:
            seen.add(key)
            out.append(row)
    return out

def _title_member_map(title: str) -> dict[str, str]:
    """Return safe Pop-number -> title-name bindings from normalized lot syntax.

    This is deliberately a local title parser, not a marketplace search. It is
    used only after the photograph has already supplied the Pop number.
    """
    out: dict[str, str] = {}
    try:
        for row in extract_lot_members(title or ""):
            if not isinstance(row, dict):
                continue
            num = _normalize_number(row.get("pop"))
            name = _specific_name(str(row.get("name") or ""))
            if num and name and not is_suspicious_ocr_name(name):
                out[num] = name
    except Exception:
        pass
    return out


def _title_name_fallback_for_box(title: str, number: str, box_index: int, expected_count: int | None) -> str:
    """Safely recover a missing photo name after the photo supplied the number."""
    num = _normalize_number(number) or str(number or "").strip()
    # 1) Exact same-number binding in the title.
    exact = _specific_name(_title_member_map(title).get(num, ""))
    if exact:
        return exact
    exact = _specific_name(_title_number_name_map(title).get(num, ""))
    if exact:
        return exact

    # 2) Normalized lot members, by physical order only when the title gives
    # exactly one clean member per physical box. This covers titles such as
    # "Lot Of 2 ... Michael Corleone ... And Tony Soprano".
    try:
        lot_names = [_specific_name(str(n)) for n in extract_lot_character_names(title or "")]
        lot_names = [n for n in lot_names if n and not is_suspicious_ocr_name(n)]
        if expected_count and len(lot_names) == expected_count and 0 <= box_index < len(lot_names):
            return lot_names[box_index]
    except Exception:
        pass
    return ""



def _infer_series_from_tokens(tokens: list[OCRToken], number: str) -> str:
    """Infer franchise/series from header text around a proven Pop number."""
    if not tokens or not number:
        return ""
    num = next((t for t in tokens if number in _number_fragments(t.text)), None)
    if not num:
        return ""
    header=[]
    for t in tokens:
        if abs(t.cy-num.cy) > max(90.0, abs(num.cy)*0.16):
            continue
        if t.cx >= num.cx:
            continue
        txt=_specific_name(t.text)
        if not txt:
            continue
        low=txt.casefold()
        if low in {"pop","funko","animation","television","movies","tv"}:
            continue
        header.append(t)
    header.sort(key=lambda t:t.cx)
    if not header:
        return ""
    text=" ".join(t.text for t in header)
    series = _specific_name(text)
    if not series:
        return ""
    # Reject obvious cross-photo/OCR soup. A real franchise/work that Wikidata
    # recognizes is retained as canonical context; unknown context is retained
    # only when it is short enough to remain useful.
    try:
        from funko_deal_bot.culture import resolve_work
        canonical = resolve_work(series[:72])
        if canonical:
            return canonical
    except Exception:
        pass
    if len(series.split()) <= 4:
        return series
    return ""

def _v34_recognize_isolated_box(
    tile: Image.Image,
    *,
    title: str = "",
    box_index: int = 0,
    expected_count: int | None = None,
    status: StatusCallback | None = None,
) -> dict:
    """Read one physical box with structural number + nameplate extraction."""
    prepared = _v34_prepare_box_for_ocr(tile)
    if max(prepared.size) < 2600:
        prepared = prepared.resize((prepared.width * 2, prepared.height * 2), Image.Resampling.BICUBIC)
    tokens = _fast_ocr_tokens(prepared, status=status)
    raw_diag = [
        {"text": t.text, "score": round(t.score, 3), "cx": round(t.cx, 1), "cy": round(t.cy, 1)}
        for t in tokens[:60]
    ]
    log.info("V47 OCR raw box tokens=%s", raw_diag)

    number_candidates = _pop_badge_number_candidates(tokens, prepared.size)
    num = _normalize_number(number_candidates[0][2]) if number_candidates else None
    if not num:
        # Target only the upper-right header, not the whole picture. One retry is
        # allowed because eBay compression can make the badge detector miss once.
        w, h = prepared.size
        number_crop = prepared.crop((int(w * 0.42), 0, w, int(h * 0.42)))
        try:
            number_tokens = _fast_ocr_tokens(number_crop, status=status, min_score=0.20)
        except TypeError:
            number_tokens = _fast_ocr_tokens(number_crop, status=status)
        number_candidates2 = _pop_badge_number_candidates(number_tokens, number_crop.size)
        if number_candidates2:
            num = _normalize_number(number_candidates2[0][2])

    if not num:
        # V48: Third attempt — horizontal header strip across FULL width with high
        # contrast enhancement. Catches stylized numbers (e.g. orange #358/#359
        # on Clockwork Orange boxes) that the default detector misses on compressed
        # eBay thumbnails. No POP! anchor required: the strip is narrow enough
        # (top 28% of box) that stray numbers from the figure body are impossible.
        from PIL import ImageEnhance, ImageFilter, ImageOps
        w, h = prepared.size
        header_strip = prepared.crop((0, 0, w, int(h * 0.28)))
        # Upscale small strips; enhance contrast to make stylized digits pop.
        if header_strip.width < 900:
            scale = max(2.0, 900 / max(header_strip.width, 1))
            header_strip = header_strip.resize(
                (int(header_strip.width * scale), int(header_strip.height * scale)),
                Image.Resampling.LANCZOS,
            )
        header_strip = ImageEnhance.Contrast(header_strip).enhance(2.0)
        header_strip = header_strip.filter(ImageFilter.SHARPEN)
        try:
            header_tokens = _fast_ocr_tokens(header_strip, status=status, min_score=0.15)
        except TypeError:
            header_tokens = _fast_ocr_tokens(header_strip, status=status)
        # Accept any strong 3-4 digit candidate in the right half of the strip.
        hs_w = header_strip.size[0]
        for ht in sorted(header_tokens, key=lambda t: (-t.score, -len(_normalize_number(t.text) or ""))):
            candidate = _normalize_number(ht.text)
            if candidate and ht.cx > hs_w * 0.30:
                num = candidate
                log.info("V48 header-strip number recovery: %s (score=%.2f)", num, ht.score)
                break

    if not num:
        return {"name": "", "number": "", "exclusive": None}

    # The photo-proven number is the anchor. An explicit same-number title pair
    # may repair a missing/garbled name, but must never overwrite a strong photo
    # name with a weaker broad title label such as "Orange".
    exact_from_title = _specific_name(_title_member_map(title).get(str(num), ""))
    if not exact_from_title:
        exact_from_title = _specific_name(_title_number_name_map(title).get(str(num), ""))
    name_rows = _nameplate_candidates(tokens, prepared.size)
    raw_name = name_rows[0][1] if name_rows else ""
    name = _validate_photo_name(raw_name, "", num)
    # When the title supplies exactly one clean character per expected physical
    # box, a one-word OCR fragment such as NOBLE must not beat that deterministic
    # order binding. This is especially important for retailer text bleeding into
    # a neighboring box crop.
    ordered_title_name = _title_name_fallback_for_box(title, num, box_index, expected_count)
    if ordered_title_name and name and expected_count and expected_count >= 2:
        try:
            title_names = [_specific_name(str(n)) for n in extract_lot_character_names(title or "")]
            title_names = [n for n in title_names if n and not is_suspicious_ocr_name(n)]
        except Exception:
            title_names = []
        from difflib import SequenceMatcher
        if len(title_names) == expected_count:
            a = re.sub(r"[^a-z0-9]", "", name.casefold())
            b = re.sub(r"[^a-z0-9]", "", ordered_title_name.casefold())
            if a and b and SequenceMatcher(None, a, b).ratio() < 0.60:
                name = ordered_title_name
    if exact_from_title:
        # When the title explicitly binds this *photo-confirmed* number to a name,
        # it is a safe repair source. Prefer it over a clearly unrelated OCR blob;
        # retain the photo text when the two labels actually agree.
        from difflib import SequenceMatcher
        a = re.sub(r"[^a-z0-9]", "", name.casefold())
        b = re.sub(r"[^a-z0-9]", "", exact_from_title.casefold())
        ratio = SequenceMatcher(None, a, b).ratio() if a and b else 0.0
        if (not name) or is_suspicious_ocr_name(name) or ratio < 0.55:
            name = exact_from_title
        elif ratio >= 0.88:
            name = exact_from_title
    if not name or is_suspicious_ocr_name(name):
        name = _title_name_fallback_for_box(title, num, box_index, expected_count)
    if name and is_suspicious_ocr_name(name):
        name = ""
    # For a single photographed box, an explicit title `Character #Number` pair
    # is a strong repair signal when OCR has produced an impossible/conflicting
    # badge number. Only apply it when the recognized name agrees with that exact
    # title name; never let an unrelated title number override photo identity.
    try:
        from difflib import SequenceMatcher
        pairs = extract_name_number_pairs(title or "")
        if len(pairs) == 1 and name:
            title_name, title_num = pairs[0]
            a = re.sub(r"[^a-z0-9]", "", str(name).casefold())
            b = re.sub(r"[^a-z0-9]", "", str(title_name).casefold())
            ratio = SequenceMatcher(None, a, b).ratio() if a and b else 0.0
            if a == b or ratio >= 0.88:
                if title_num != num:
                    log.info("V47 single-box title number repair #%s -> #%s for %r", num, title_num, title_name)
                    num = title_num
    except Exception:
        pass
    series = _infer_series_from_tokens(tokens, num)
    return {"name": name, "number": num, "exclusive": None, "series": series}

def recognize_boxes(
    image: bytes | bytearray | memoryview | str | Path | Image.Image,
    expected_count: int | None = None,
    *,
    title: str = "",
    status: StatusCallback | None = None,
) -> list[dict]:
    """V47 adaptive/hybrid photo-first recognition with per-listing/per-box strategy.

    A lightweight full-photo OCR pass identifies physical POP! badge anchors and
    chooses the layout. Each resulting physical box is then OCR'd independently.
    Names and numbers are paired inside the same crop; title/eBay recovery is used
    only after the photo has proved the Pop number.
    """
    loaded = _load_image(image)
    if loaded is None:
        return []
    pil = _prepare_vision_crop(loaded)
    started = __import__("time").perf_counter()
    explicit_count = max(1, min(int(expected_count), _MAX_FIGURES)) if expected_count else None
    inferred_count, regions = _infer_photo_layout(pil, explicit_count, status=status)
    count = inferred_count or explicit_count or 1
    # V48: Prefer adaptive badge regions (with anchor_num from photo) even when
    # explicit_count is set. Previously explicit_count always forced legacy_tiles
    # without anchor_num, so the photo-proven Pop number was never forwarded to
    # the per-box recognizer. Now if badge detection succeeded (regions not empty)
    # we use them with their anchor numbers. Legacy geometric splitter is the
    # fallback when badge detection found nothing.
    if explicit_count and regions:
        count = len(regions)
        log.info("V48 box slicing: expected=%s badge_regions=%s photo=%sx%s", expected_count, count, pil.width, pil.height)
        tiles=[]
        for reg in regions:
            x1,y1,x2,y2,anchor,anchor_num = reg
            tiles.append((pil.crop((x1,y1,x2,y2)), anchor, anchor_num))
    elif explicit_count:
        count = max(1, min(explicit_count, _MAX_FIGURES))
        legacy_tiles = _v34_isolated_box_tiles(pil, count)
        log.info("V47 box slicing: expected=%s photo_inferred=%s actual=%s stable_tiles=%s photo=%sx%s", expected_count, inferred_count, count, len(legacy_tiles), pil.width, pil.height)
        tiles=[(t,None,None) for t in legacy_tiles]
    elif regions:
        count = len(regions)
        log.info("V47 box slicing: generic title, adaptive_regions=%s photo=%sx%s", count, pil.width, pil.height)
        tiles=[]
        for reg in regions:
            x1,y1,x2,y2,anchor,anchor_num = reg
            tiles.append((pil.crop((x1,y1,x2,y2)), anchor, anchor_num))
    else:
        count = max(1, min(count, _MAX_FIGURES))
        legacy_tiles = _v34_isolated_box_tiles(pil, count)
        log.info("V47 box slicing: fallback stable_tiles=%s photo=%sx%s", len(legacy_tiles), pil.width, pil.height)
        tiles=[(t,None,None) for t in legacy_tiles]

    results=[]; used_numbers=set()
    for index,(tile,anchor,anchor_num) in enumerate(tiles,1):
        log.info("V44 OCR box %d/%d: crop=%sx%s", index,len(tiles),tile.width,tile.height)
        row=_v34_recognize_isolated_box(tile,title=title,box_index=index-1,expected_count=count,status=status)
        # Strong geometry anchor beats accidental number tokens in the crop.
        number=_normalize_number(anchor_num or row.get("number"))
        if anchor_num and number:
            row["number"]=number
        raw_name=str(row.get("name") or "")
        series=str(row.get("series") or "")
        if not number:
            retry=_prepare_vision_crop(tile.resize((tile.width*2,tile.height*2),Image.Resampling.BICUBIC))
            row2=_v34_recognize_isolated_box(retry,title=title,box_index=index-1,expected_count=count,status=status)
            number=_normalize_number(anchor_num or row2.get("number"))
            raw_name=str(row2.get("name") or raw_name)
            series=str(row2.get("series") or series)
        if not number:
            log.warning("V47 box %d/%d unresolved: no reliable Pop number",index,len(tiles)); continue
        if number in used_numbers:
            log.warning("V44 duplicate Pop number=%s; keeping first physical box",number); continue
        used_numbers.add(number)
        name=_validate_photo_name(raw_name,"",number) if raw_name else ""
        if not name or is_suspicious_ocr_name(name):
            name=_specific_name(_title_member_map(title).get(str(number),""))
        if not name or is_suspicious_ocr_name(name):
            name=_specific_name(_title_number_name_map(title).get(str(number),""))
        if not name or is_suspicious_ocr_name(name):
            name=_title_name_fallback_for_box(title,number,index-1,count)
        if name and is_suspicious_ocr_name(name): name=""
        results.append({"name":name,"number":number,"exclusive":row.get("exclusive") or None,"series":series})
        log.info("V47 box %d/%d identity number=%s name=%r series=%r",index,len(tiles),number,name,series)

    results=_dedupe_numbered_pairs(results)
    if results: results[0]["_photo_box_count"]=len(results) if inferred_count is None else count
    elapsed=__import__("time").perf_counter()-started
    named=sum(1 for row in results if row.get("name") and row.get("number"))
    log.info("V47 OCR recognized %d/%s boxes (%d named) in %.2fs",len(results),count,named,elapsed)
    return results

def ocr_image_bytes(data: bytes) -> str:
    """Compatibility helper: return Florence OCR labels as plain text."""
    pil = _load_image(data)
    if pil is None:
        return ""
    result = _run_florence(pil, _OCR_REGION_TASK)
    return " ".join(token.text for token in _ocr_tokens(result))


def _expected_figure_count(title: str) -> int | None:
    count = extract_bundle_count(title)
    if count:
        return min(count, _MAX_FIGURES)
    # Explicit Duo/Pair wording is authoritative; do this before the legacy
    # title-member parser, which can mistake franchise words for names.
    if re.search(r"\b(?:duo|pair|twin|two[- ]pack|2[- ]pack)\b", title or "", re.I):
        return 2
    names = extract_lot_members(title)
    if len(names) >= 2:
        return min(len(names), _MAX_FIGURES)
    ranges = re.findall(r"#?\s*(\d{3,4})\s*[-–]\s*#?\s*(\d{3,4})", title or "")
    if ranges:
        a, b = map(int, ranges[0])
        if 0 < a <= b <= 9999 and (b - a + 1) <= _MAX_FIGURES:
            return b - a + 1
    return None



def expected_figure_count(title: str) -> int | None:
    """Public helper for deciding whether a bundle was fully recognized."""
    return _expected_figure_count(title)

def _dicts_to_refs(items: list[dict]) -> list[PopRef]:
    refs: list[PopRef] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _clean_name(str(item.get("name") or "")) or None
        number = _normalize_number(item.get("number"))
        # V29 intentionally preserves a photo-proven Pop number even when the
        # nameplate OCR failed. The engine can then repair the name safely from
        # the exact title pair/local catalogue/strict consensus without inventing
        # a second physical box.
        if not name and not number:
            continue
        key = ((name or "").casefold(), number or "")
        if key in seen:
            continue
        seen.add(key)
        refs.append(PopRef(name=name, number=number, exclusive=item.get("exclusive") or None, series=str(item.get("series") or "") or None))
    return refs[:_MAX_FIGURES]


def _apply_refs(listing: Listing, refs: list[PopRef]) -> None:
    listing.members = [ref.as_dict() for ref in refs]
    listing.vision_checked = True
    if len(refs) == 1:
        listing.pop_name = refs[0].name
        listing.pop_number = refs[0].number
        listing.exclusive = refs[0].exclusive
    else:
        listing.pop_name = None
        listing.pop_number = None
        listing.exclusive = refs[0].exclusive if refs else None


def _refs_from_explicit_ocr(listing: Listing, ocr_text: str | None) -> list[PopRef]:
    """Compatibility only: explicit OCR supplied by callers when no photo is usable."""
    if not ocr_text:
        return []
    title_refs = parse_pop_refs(listing.title or "")
    numbers = [n for n in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", ocr_text) if _normalize_number(n)]
    out: list[PopRef] = []
    if title_refs:
        for index, ref in enumerate(title_refs[:_MAX_FIGURES]):
            title_number = None
            if len(title_refs) == 1:
                title_number = extract_pop_id(listing.title or "") or (numbers[0] if numbers else None)
            elif index < len(numbers):
                title_number = numbers[index]
            completed = complete_pop_ref_from_ocr(
                ref,
                ocr_text,
                title=listing.title,
                title_number=title_number,
                from_title=True,
            )
            if completed.name or completed.number:
                out.append(completed)
    else:
        out.extend(parse_pop_refs(ocr_text)[:_MAX_FIGURES])
        if not out:
            nums2 = [_normalize_number(n) for n in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", ocr_text)]
            nums2 = [n for n in nums2 if n]
            names2 = []
            for chunk in re.split(r"\s{2,}|[,;|]", re.sub(r"(?<!\w)\d{3,4}\b", "|", ocr_text)):
                clean = _specific_name(chunk)
                if clean:
                    names2.append(clean)
            for name, number in zip(names2, nums2):
                out.append(PopRef(name=name, number=number, exclusive=None))
    return _dicts_to_refs([ref.as_dict() for ref in out])


def _load_disk_vision_cache() -> None:
    if _VISION_CACHE:
        return
    try:
        path = _VISION_CACHE_FILE
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return
        import json
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        for key, value in list(raw.items())[-_VISION_CACHE_MAX:]:
            if isinstance(value, list) and all(isinstance(x, dict) for x in value):
                _VISION_CACHE[key] = [dict(x) for x in value]
    except Exception:
        log.debug("Unable to load persistent vision cache", exc_info=True)


def _save_disk_vision_cache() -> None:
    try:
        path = _VISION_CACHE_FILE
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        import json
        path.write_text(json.dumps(dict(_VISION_CACHE), ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.debug("Unable to save persistent vision cache", exc_info=True)


def _vision_cache_key(image_bytes: bytes) -> str:
    return f"{_VISION_CACHE_NAMESPACE}:{hashlib.md5(image_bytes).hexdigest()}"


def _vision_cache_get(image_bytes: bytes) -> list[dict] | None:
    key = _vision_cache_key(image_bytes)
    with _VISION_CACHE_LOCK:
        _load_disk_vision_cache()
        value = _VISION_CACHE.get(key)
        # Empty/failed recognitions are deliberately never cache hits. A previous
        # bad OCR result must not permanently poison manual /check retries.
        if not value:
            return None
        _VISION_CACHE.move_to_end(key)
        return [dict(item) for item in value]


def _vision_cache_put(image_bytes: bytes, recognized: list[dict]) -> None:
    # Persist only positive recognition results. Failed OCR must be retried.
    if not recognized:
        return
    key = _vision_cache_key(image_bytes)
    with _VISION_CACHE_LOCK:
        _VISION_CACHE[key] = [dict(item) for item in recognized]
        _VISION_CACHE.move_to_end(key)
        while len(_VISION_CACHE) > _VISION_CACHE_MAX:
            _VISION_CACHE.popitem(last=False)
        _save_disk_vision_cache()


_OCR_DEBUG_DIR = Path(os.getenv("OCR_DEBUG_DIR", "data/ocr_images"))
_OCR_DEBUG_KEEP_SECONDS = int(os.getenv("OCR_DEBUG_KEEP_SECONDS", "1200") or 1200)


def _debug_photo_path(item_id: str) -> Path:
    return _OCR_DEBUG_DIR / f"{item_id}.jpg"


def _save_debug_photo(item_id: str, image_bytes: bytes) -> Path | None:
    try:
        _OCR_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        # Best-effort cleanup of stale failed-debug images.
        now = __import__("time").time()
        for old in _OCR_DEBUG_DIR.glob("*.jpg"):
            try:
                if now - old.stat().st_mtime > _OCR_DEBUG_KEEP_SECONDS:
                    old.unlink(missing_ok=True)
            except OSError:
                pass
        path = _debug_photo_path(item_id)
        path.write_bytes(image_bytes)
        log.info("OCR image saved: %s", path.as_posix())
        return path
    except Exception:
        log.debug("Unable to save OCR debug photo item=%s", item_id, exc_info=True)
        return None


def _delete_debug_photo(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
        log.info("OCR image deleted: %s", path.as_posix())
    except OSError:
        log.debug("Unable to delete OCR debug photo: %s", path, exc_info=True)


def enrich_listing(
    listing: Listing,
    *,
    downloader: Downloader | None = None,
    openai_api_key: str = "",
    ocr_text: str | None = None,
    status: StatusCallback | None = None,
) -> list[PopRef]:
    """Photo-first enrichment using only local Florence-2. API key is ignored."""
    clean_url = _coerce_image_url(listing.image_url)
    if clean_url:
        listing.image_url = clean_url
    if not clean_url or downloader is None:
        refs = _refs_from_explicit_ocr(listing, ocr_text or getattr(listing, "ocr_text", None))
        if refs:
            _apply_refs(listing, refs)
            return refs
        listing.vision_checked = bool(clean_url)
        listing.members = []
        return []

    _notify(status, "📸 Фото лота получено")
    try:
        image_bytes = downloader(clean_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Image download failed for Florence-2: %s", exc)
        image_bytes = None
    debug_photo = _save_debug_photo(listing.item_id, image_bytes) if image_bytes else None
    if not image_bytes:
        refs = _refs_from_explicit_ocr(listing, ocr_text or getattr(listing, "ocr_text", None))
        if refs:
            _apply_refs(listing, refs)
            _notify(status, "⚠️ Фото недоступно; использую явно переданный OCR")
            return refs
        # Legacy compatibility only: direct remote Vision URL path is used
        # when a caller explicitly supplies a non-empty API key. Production
        # V22.1 keeps this empty and remains local/Florence-only.
        if openai_api_key:
            try:
                try:
                    refs, checked = _openai_identify_url_checked(
                        clean_url, openai_api_key, title=listing.title,
                        expected_count=_expected_figure_count(listing.title),
                    )
                except TypeError:
                    refs, checked = _openai_identify_url_checked(clean_url, openai_api_key)
                if refs:
                    _apply_refs(listing, refs)
                    return refs
            except Exception:
                log.debug("Legacy direct Vision URL fallback failed", exc_info=True)
        listing.vision_checked = True
        listing.members = []
        _notify(status, "❌ Фото не удалось скачать")
        return []

    expected = _expected_figure_count(listing.title)
    try:
        recognized = _vision_cache_get(bytes(image_bytes))
        if recognized is not None:
            log.info("Florence vision cache hit item=%s", listing.item_id)
            _notify(status, "⚡ ИИ: это фото уже распознавалось — использую кеш")
        else:
            recognized = recognize_boxes(image_bytes, expected_count=expected, title=listing.title, status=status)
            _vision_cache_put(bytes(image_bytes), recognized)
        photo_count = None
        if recognized:
            try:
                marker = recognized[0].get("_photo_box_count") if isinstance(recognized[0], dict) else None
                if marker:
                    photo_count = int(marker)
            except Exception:
                photo_count = None
        listing.vision_expected_count = photo_count or expected or 1
        recognized = _repair_with_title(recognized, listing.title)
        refs = _dicts_to_refs(recognized)
        _apply_refs(listing, refs)
        if refs:
            log.info("Florence photo parsed item=%s as %s", listing.item_id, [(ref.name, ref.number) for ref in refs])
        else:
            log.warning("Florence-2 found no complete boxed Funko identities for item=%s", listing.item_id)
        return refs
    finally:
        # Keep failed/unresolved photos briefly for post-mortem inspection; remove
        # successfully recognized photos immediately to avoid disk growth.
        if debug_photo is not None:
            completed = bool(locals().get("refs")) and all(
                getattr(ref, "name", None) and getattr(ref, "number", None)
                for ref in locals().get("refs", [])
            )
            if completed:
                _delete_debug_photo(debug_photo)
        image_bytes = None
        gc.collect()


# Compatibility wrappers retained for existing imports/tests. They all use local Florence.
def openai_identify(data: bytes, api_key: str = "") -> list[PopRef]:
    return _dicts_to_refs(recognize_boxes(data))


def openai_identify_url(image_url: str, api_key: str = "") -> list[PopRef]:
    url = _coerce_image_url(image_url)
    if not url:
        return []
    try:
        import httpx
        response = httpx.get(url, timeout=20.0, follow_redirects=True, trust_env=False)
        response.raise_for_status()
        return openai_identify(response.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("Local Florence image download failed: %s", exc)
        return []


_VISION_PROMPT = (
    "Read every boxed Funko Pop visible in this image. Example Pop number: 1295. Output the character name and complete Pop number for each physical box. JSON only. "
    "For each physical box, return the character name "
    "printed on the lower/front name plate and the complete 3-4 digit POP number printed in the upper-right of the same physical box. "
    "Ignore packaging/franchise words such as POP, FUNKO, VINYL, FIGURE, COLLECTIBLE, TELEVISION, MOVIES, "
    "GAMES, POKEMON, SOPRANOS and GODFATHER. Ignore PLAYSTATION, XBOX and GAMESTOP even when OCR glues them to the name. Never use an eBay title as the identity source."
)


def _vision_prompt(title: str = "", *, tile: bool = False) -> str:
    return _VISION_PROMPT + (" Crop: report every visible box in this crop." if tile else "")


def _clean_vision_name(name: str) -> str:
    return _clean_name(name)


def _refs_from_openai_json(content: str) -> list[PopRef]:
    import json
    raw = (content or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    rows = data.get("figures") if isinstance(data, dict) else data
    return _dicts_to_refs(rows) if isinstance(rows, list) else []


def _merge_visual_refs(groups: list[list[PopRef]], expected_count: int | None = None) -> list[PopRef]:
    """Backward-compatible flatten/dedupe helper for the pre-V15 vision tests.

    The modern engine consumes the clean list returned by ``recognize_boxes``;
    this helper simply preserves the old public utility so older integrations do
    not break when Florence is used underneath.
    """
    out: list[PopRef] = []
    seen: set[tuple[str, str]] = set()
    for group in groups or []:
        for ref in group or []:
            if not isinstance(ref, PopRef):
                continue
            if not ref.name or not ref.number:
                continue
            key = (ref.name.casefold(), ref.number)
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
            if expected_count and len(out) >= expected_count:
                return out[:expected_count]
    return out[:_MAX_FIGURES]


def _merge_single_vision_with_text(vision_refs: list[PopRef], text_refs: list[PopRef]) -> list[PopRef]:
    if not vision_refs:
        return []
    return list(vision_refs)


def _iter_ocr_rows(data: bytes):
    image = _load_image(data)
    if image is None:
        return []
    tokens = _ocr_tokens(_run_florence(image, _OCR_REGION_TASK))
    return [(list(token.quad), token.text, token.score) for token in tokens]


def _ocr_number_tokens_spatial(data: bytes | str) -> list[str]:
    if isinstance(data, str):
        return [number for number in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", data) if _normalize_number(number)]
    numbers = []
    for bbox, text, _score in _iter_ocr_rows(data):
        center = _quad_center(bbox)
        if center is None:
            continue
        number = _normalize_number(text)
        if number:
            numbers.append((center[1], center[0], number))
    seen: set[str] = set()
    out: list[str] = []
    for _cy, _cx, number in sorted(numbers):
        if number not in seen:
            seen.add(number)
            out.append(number)
    return out


def _geometry_pop_refs(data: bytes) -> list[PopRef]:
    rows = _iter_ocr_rows(data)
    tokens: list[OCRToken] = []
    max_x, max_y = 1600.0, 1200.0
    for bbox, text, score in rows:
        center = _quad_center(bbox)
        if center is None:
            continue
        cx, cy, flat = center
        max_x = max(max_x, max(flat[0::2]))
        max_y = max(max_y, max(flat[1::2]))
        tokens.append(OCRToken(str(text), cx, cy, flat, float(score)))
    pairs = _pair_tokens(tokens, (int(max_x), int(max_y)), None)
    return [PopRef(item["name"], item["number"], item.get("exclusive")) for item in pairs]


def _openai_identify_url_checked(image_url: str, api_key: str = "", title: str = "", expected_count: int | None = None) -> tuple[list[PopRef], bool]:
    try:
        import httpx
        url = _coerce_image_url(image_url)
        if not url:
            return [], False
        response = httpx.get(url, timeout=20.0, follow_redirects=True, trust_env=False)
        response.raise_for_status()
        refs = _dicts_to_refs(recognize_boxes(response.content, expected_count=expected_count))
        return refs, bool(response.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("Local Florence URL recognition failed: %s", exc)
        return [], False


def _openai_identify_bytes_checked(data: bytes, api_key: str = "", title: str = "", tile: bool = False) -> list[PopRef]:
    return _dicts_to_refs(recognize_boxes(data))
