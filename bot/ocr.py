import io
import re
import colorsys
from collections.abc import Iterator
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from config import (
    format_subset_for_set,
    get_all_subset_names,
    get_set_names,
    get_subset_groups,
    get_subset_variants,
)

TESSERACT_TIMEOUT_SECONDS = 1.2
MAX_OCR_DIMENSION = 900
VALID_CARD_COUNTS = [1, 3, 5, 10, 15, 25, 35, 50, 75, 99, 100, 250, 500]
CARD_COUNT_SUBSET_FALLBACKS = [
    ("superfractor", 1),
    ("pink", 1),
    ("red", 5),
    ("black", 10),
    ("orange", 25),
    ("gold", 50),
]
ONE_OF_ONE_SUBSET_MARKERS = ("superfractor", "foilfractor", "platinum")


@dataclass
class ListingMetadataGuess:
    text: str = ""
    engine: str = "none"
    ocr_available: bool = False
    player_name: str | None = None
    set_name: str | None = None
    subset_group: str | None = None
    subset_variant: str | None = None
    subset_value: str | None = None
    card_count: int | None = None
    set_option_order: list[str] = field(default_factory=list)
    subset_option_order: list[str] = field(default_factory=list)
    variant_option_order: list[str] = field(default_factory=list)
    confidence: dict[str, float] = field(default_factory=dict)
    card_count_source: str | None = None
    card_rarity: str | None = None
    card_rarity_source: str | None = None


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _score_candidate(text: str, candidate: str) -> float:
    normalized_text = _normalize(text)
    normalized_candidate = _normalize(candidate)
    candidate_aliases = _candidate_aliases(candidate)
    if not normalized_text or not normalized_candidate:
        return 0.0
    if normalized_candidate in normalized_text:
        return 1.0
    for alias in candidate_aliases:
        if alias and alias in normalized_text:
            return 0.97
    words = normalized_candidate.split()
    if words and all(word in normalized_text for word in words):
        return 0.88
    for alias in candidate_aliases:
        alias_words = alias.split()
        if alias_words and all(word in normalized_text for word in alias_words):
            return 0.9
    return SequenceMatcher(None, normalized_text, normalized_candidate).ratio()


def _candidate_aliases(candidate: str) -> list[str]:
    normalized = _normalize(candidate)
    aliases = set()
    aliases.add(normalized)
    aliases.add(re.sub(r"^(?:20)?\d{2}(?:\s+\d{2})?\s+", "", normalized).strip())
    aliases.add(normalized.replace("2025 26", "25 26"))
    aliases.add(normalized.replace("2025 26", "26"))
    aliases.add(normalized.replace("2026", "26"))
    aliases.add(normalized.replace("topps now", "topps now 26"))
    aliases.add(normalized.replace("2025 26 topps now", "topps now 26"))
    aliases.add(normalized.replace("2026 topps now", "topps now 26"))
    return [alias for alias in aliases if alias and alias != normalized]


def _rank_candidates(text: str, candidates: list[str], *, limit: int = 25) -> list[tuple[str, float]]:
    ranked = [
        (candidate, _score_candidate(text, candidate))
        for candidate in candidates
        if candidate
    ]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def _rank_set_candidates(text: str, candidates: list[str], *, limit: int = 25) -> list[tuple[str, float]]:
    """Rank set names, preferring more specific matches when scores are close."""
    normalized_text = _normalize(text)
    ranked = [
        (candidate, _score_candidate(text, candidate))
        for candidate in candidates
        if candidate
    ]

    def sort_key(item: tuple[str, float]) -> tuple[float, int, int]:
        candidate, score = item
        tokens = _normalize(candidate).split()
        matched_tokens = sum(1 for token in tokens if token in normalized_text)
        specificity_bonus = 0.04 if score >= 0.95 and matched_tokens >= 3 else 0.0
        return (score + specificity_bonus, matched_tokens, len(tokens))

    ranked.sort(key=sort_key, reverse=True)
    return ranked[:limit]


def _prefer_specific_set(text: str, rankings: list[tuple[str, float]]) -> list[tuple[str, float]]:
    if not rankings:
        return rankings

    normalized_text = _normalize(text)
    strong_sapphire_signal = any(
        signal in normalized_text
        for signal in {
            "sapphire",
            "padparadscha",
            "certified signature issue",
            "sapphire selections",
        }
    )
    base_color_signal = (
        "base" in normalized_text
        and any(color in normalized_text for color in {"gold", "orange", "purple", "red", "black"})
    )
    if not strong_sapphire_signal and not base_color_signal:
        return rankings

    top_name, top_score = rankings[0]
    if top_name != "2025-26 Topps Chrome":
        return rankings

    sapphire_index = next(
        (
            index for index, (candidate, _score) in enumerate(rankings)
            if candidate == "2026 Topps Chrome Sapphire"
        ),
        None,
    )
    if sapphire_index is None:
        return rankings

    _sapphire_name, sapphire_score = rankings[sapphire_index]
    minimum_score = 0.35 if strong_sapphire_signal else 0.40
    if sapphire_score < minimum_score:
        return rankings

    promoted = rankings.pop(sapphire_index)
    return [promoted, *rankings]


def _score_player_candidate(text: str, player_name: str) -> float:
    text_tokens = _normalize(text).split()
    player_tokens = _normalize(player_name).split()
    if not text_tokens or not player_tokens:
        return 0.0
    player_text = " ".join(player_tokens)
    ocr_text = " ".join(text_tokens)
    if player_text in ocr_text:
        return 1.0

    best_score = 0.0
    window_size = len(player_tokens)
    for start in range(0, max(1, len(text_tokens) - window_size + 1)):
        window = text_tokens[start:start + window_size]
        if len(window) != window_size:
            continue
        token_scores = [
            SequenceMatcher(None, expected, actual).ratio()
            for expected, actual in zip(player_tokens, window)
        ]
        score = sum(token_scores) / len(token_scores)
        if player_tokens[-1] == window[-1]:
            score += 0.12
        best_score = max(best_score, min(score, 1.0))
    return best_score


def _crop_regions_for_ocr(image):
    width, height = image.size
    regions = [
        image,
        image.crop((0, int(height * 0.64), width, height)),
    ]
    return regions


def _prepare_ocr_image(image):
    try:
        from PIL import ImageOps, ImageEnhance
    except ImportError:
        return image
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    return ImageEnhance.Contrast(gray).enhance(1.8)


def _contains_one_of_one(text: str) -> bool:
    """Recognize a 1/1 serial, including common OCR substitutions for one."""
    raw_text = str(text or "")
    one_glyph = r"[1Il|]"
    if re.search(
        rf"(?<![A-Za-z0-9]){one_glyph}\s*[/\\]\s*{one_glyph}(?![A-Za-z0-9])",
        raw_text,
    ):
        return True
    return bool(
        re.search(
            r"\b(?:1|one)\s+(?:of|out\s+of)\s+(?:1|one)\b",
            raw_text,
            re.IGNORECASE,
        )
    )


def _extract_card_count(text: str) -> int | None:
    normalized = str(text or "")
    if _contains_one_of_one(normalized):
        return 1
    for match in re.finditer(r"(?:/|out\s+of\s+)(\d{1,4})\b", normalized, re.IGNORECASE):
        value = int(match.group(1))
        if value > 1:
            return _normalize_ocr_card_count(value)
    if re.search(r"\bunlimited\b", normalized, re.IGNORECASE):
        return 999
    return None


def _normalize_ocr_card_count(value: int) -> int | None:
    """Snap noisy OCR count reads to values the listing UI supports."""
    if value <= 0:
        return None
    if value >= 999:
        return 999
    if value in VALID_CARD_COUNTS:
        return value
    if value < 10:
        return next((count for count in VALID_CARD_COUNTS if count >= value), 10)
    return min(VALID_CARD_COUNTS, key=lambda count: (abs(count - value), count < value))


def _extract_bottom_pill_count(image_bytes: bytes | None) -> int | None:
    if not image_bytes:
        return None
    try:
        from PIL import Image, ImageOps, ImageEnhance, ImageFilter
        import pytesseract
    except ImportError:
        return None

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = image.size
    except Exception:
        return None

    # Right-side count pill positions vary by device crop and image aspect ratio.
    boxes = [
        (0.62, 0.69, 0.98, 0.82),
        (0.54, 0.66, 0.98, 0.84),
        (0.58, 0.72, 0.98, 0.88),
        (0.48, 0.68, 0.98, 0.92),
    ]
    config = "--psm 7 -c tessedit_char_whitelist=0123456789/"
    for left, top, right, bottom in boxes:
        region = image.crop((
            int(width * left),
            int(height * top),
            int(width * right),
            int(height * bottom),
        ))
        gray = ImageOps.grayscale(region)
        gray = ImageOps.autocontrast(gray)
        gray = gray.resize((max(1, gray.width * 3), max(1, gray.height * 3)))
        high_contrast = ImageEnhance.Contrast(gray).enhance(2.8).filter(ImageFilter.SHARPEN)
        variants = [
            high_contrast,
            high_contrast.point(lambda value: 255 if value > 145 else 0),
        ]
        for variant in variants:
            try:
                text = pytesseract.image_to_string(
                    variant,
                    config=config,
                    timeout=TESSERACT_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            parsed_count = _extract_card_count(text)
            if parsed_count is not None:
                return parsed_count
            numbers = [int(value) for value in re.findall(r"\d{1,4}", text or "")]
            if not numbers:
                continue
            value = numbers[-1]
            if value <= 1:
                continue
            return _normalize_ocr_card_count(value)
    return None


def _normalize_card_rarity(text: str) -> str | None:
    normalized = _normalize(text)
    if not normalized:
        return None
    rarity_aliases = {
        "common": "Common",
        "commom": "Common",
        "uncommon": "Uncommon",
        "un common": "Uncommon",
        "rare": "Rare",
        "super rare": "Super Rare",
        "superrare": "Super Rare",
        "s rare": "Super Rare",
        "epic": "Epic",
        "legendary": "Legendary",
        "legednary": "Legendary",
        "legandary": "Legendary",
        "iegendary": "Legendary",
        "legend ary": "Legendary",
        "iconic": "Iconic",
        "super iconic": "Super Iconic",
        "supericonic": "Super Iconic",
        "ultimate": "Ultimate",
    }
    for alias, rarity in sorted(rarity_aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in normalized:
            return rarity
    ranked = [
        (rarity, SequenceMatcher(None, normalized, alias).ratio())
        for alias, rarity in rarity_aliases.items()
    ]
    ranked.sort(key=lambda item: item[1], reverse=True)
    if ranked and ranked[0][1] >= 0.72:
        return ranked[0][0]
    return None


def _read_rarity_region_text(region) -> Iterator[str]:
    try:
        from PIL import ImageOps, ImageEnhance, ImageFilter
        import pytesseract
    except ImportError:
        return

    gray = ImageOps.grayscale(region)
    gray = ImageOps.autocontrast(gray)
    enlarged = gray.resize((max(1, gray.width * 4), max(1, gray.height * 4)))
    high_contrast = ImageEnhance.Contrast(enlarged).enhance(3.0)
    thresholded = high_contrast.filter(ImageFilter.SHARPEN).point(lambda value: 255 if value > 145 else 0)
    variants = [
        high_contrast,
        ImageOps.invert(thresholded),
    ]

    configs = [
        "--psm 7",
        "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ",
    ]
    for image in variants:
        for config in configs:
            try:
                text = pytesseract.image_to_string(
                    image,
                    config=config,
                    timeout=TESSERACT_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            if text:
                yield text


def _extract_bottom_pill_rarity(image_bytes: bytes | None) -> str | None:
    if not image_bytes:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = image.size
    except Exception:
        return None

    # Left/bottom pill positions vary by device crop. Try the tight expected pill,
    # then progressively wider bottom-left areas while avoiding the right count pill.
    boxes = [
        (0.04, 0.70, 0.38, 0.80),
        (0.02, 0.68, 0.50, 0.82),
        (0.00, 0.64, 0.64, 0.86),
    ]
    for left, top, right, bottom in boxes:
        region = image.crop((
            int(width * left),
            int(height * top),
            int(width * right),
            int(height * bottom),
        ))
        for text in _read_rarity_region_text(region):
            rarity = _normalize_card_rarity(text)
            if rarity:
                return rarity
    return None

def _extract_player_name(text: str, known_players: list[str]) -> tuple[str | None, float]:
    ranked = [
        (player_name, _score_player_candidate(text, player_name))
        for player_name in known_players
        if player_name
    ]
    ranked.sort(key=lambda item: item[1], reverse=True)
    ranked = ranked[:5]
    if not ranked:
        return None, 0.0
    player_name, score = ranked[0]
    if score < 0.72:
        return None, score
    return player_name, score


def _read_text_from_image(image_bytes: bytes) -> tuple[str, str, bool]:
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return "", "pytesseract_unavailable", False

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image.thumbnail((MAX_OCR_DIMENSION, MAX_OCR_DIMENSION))
        texts = []
        for region in _crop_regions_for_ocr(image):
            prepared = _prepare_ocr_image(region)
            text = pytesseract.image_to_string(
                prepared,
                config="--psm 6",
                timeout=TESSERACT_TIMEOUT_SECONDS,
            )
            if text:
                texts.append(text)
    except Exception:
        return "", "pytesseract_failed", False
    return "\n".join(texts), "pytesseract", True


def _dominant_card_color(image_bytes: bytes | None) -> tuple[str | None, float]:
    if not image_bytes:
        return None, 0.0
    try:
        from PIL import Image
    except ImportError:
        return None, 0.0

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = image.size
        card_region = image.crop((int(width * 0.06), int(height * 0.12), int(width * 0.94), int(height * 0.70)))
        sample_regions = [
            card_region.crop((0, int(card_region.height * 0.78), card_region.width, card_region.height)),
            card_region.crop((0, 0, int(card_region.width * 0.12), card_region.height)),
            card_region.crop((int(card_region.width * 0.88), 0, card_region.width, card_region.height)),
        ]
        counts = {"Red": 0, "Orange": 0, "Gold": 0, "Green": 0, "Blue": 0, "Purple": 0, "Black": 0}
        total = 0
        dark_count = 0
        for region in sample_regions:
            region = region.resize((80, 80))
            for red, green, blue in region.getdata():
                max_channel = max(red, green, blue)
                min_channel = min(red, green, blue)
                saturation = max_channel - min_channel
                brightness = max_channel
                if brightness < 45:
                    dark_count += 1
                    total += 1
                    counts["Black"] += 1
                    continue
                if saturation < 35:
                    continue
                total += 1
                hue, sat, val = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
                hue = hue * 360
                if hue < 12 or hue >= 345:
                    counts["Red"] += 1
                elif 12 <= hue < 38:
                    counts["Orange"] += 1
                elif 38 <= hue < 62:
                    counts["Gold"] += 1
                elif 75 <= hue < 165:
                    counts["Green"] += 1
                elif 185 <= hue < 255:
                    counts["Blue"] += 1
                elif 255 <= hue < 315:
                    counts["Purple"] += 1
        if not total:
            return None, 0.0
        color, count = max(counts.items(), key=lambda item: item[1])
        confidence = count / total
        if color == "Black":
            sampled_pixels = sum(1 for region in sample_regions for _ in region.resize((80, 80)).getdata())
            dark_total = dark_count / max(1, sampled_pixels)
            if dark_total < 0.28:
                return None, dark_total
            confidence = max(confidence, dark_total)
        if confidence < 0.10:
            return None, confidence
        return color, confidence
    except Exception:
        return None, 0.0


def _color_subset_candidates(set_name: str, color: str, preferred_group: str | None = None) -> list[tuple[str, str | None, str]]:
    candidates: list[tuple[int, str, str | None, str]] = []
    for group in get_subset_groups(set_name):
        variants = get_subset_variants(set_name, group)
        if not variants and group == color:
            candidates.append((0, group, None, group))
            continue
        for variant in variants:
            final_subset = format_subset_for_set(set_name, group, variant)
            if final_subset == color:
                candidates.append((0, group, variant, final_subset))
            elif preferred_group and group == preferred_group and variant == color:
                candidates.append((1, group, variant, final_subset))
            elif group == "Base" and variant == color:
                candidates.append((2, group, variant, final_subset))
            elif variant == color:
                candidates.append((3, group, variant, final_subset))
    candidates.sort(key=lambda item: item[0])
    return [(group, variant, final_subset) for _rank, group, variant, final_subset in candidates]


def _apply_color_subset_guess(guess: ListingMetadataGuess, image_bytes: bytes | None) -> None:
    if not guess.set_name:
        return
    color, confidence = _dominant_card_color(image_bytes)
    if not color:
        return
    candidates = _color_subset_candidates(guess.set_name, color, guess.subset_group)
    if not candidates:
        return

    current_score = max(
        guess.confidence.get("subset_value", 0.0),
        guess.confidence.get("subset_group", 0.0),
        guess.confidence.get("subset_variant", 0.0),
    )
    can_apply = (
        not guess.subset_value
        or current_score < 0.82
        or (
            guess.subset_group
            and not guess.subset_variant
            and any(group == guess.subset_group for group, _variant, _final_subset in candidates)
        )
    )
    if not can_apply:
        return

    group, variant, final_subset = candidates[0]
    guess.subset_group = group
    guess.subset_variant = variant
    guess.subset_value = final_subset
    guess.confidence["subset_color"] = confidence
    if group not in guess.subset_option_order:
        guess.subset_option_order.insert(0, group)
    if variant and variant not in guess.variant_option_order:
        guess.variant_option_order.insert(0, variant)


def _apply_one_of_one_subset_guard(guess: ListingMetadataGuess) -> None:
    """Replace a color-only Orange/Gold guess with a configured 1/1 parallel."""
    if guess.card_count != 1 or not guess.set_name:
        return

    current_text = _normalize(" ".join([
        str(guess.subset_value or ""),
        str(guess.subset_variant or ""),
    ]))
    if current_text and not any(color in current_text.split() for color in ("orange", "gold")):
        return

    candidates: list[tuple[int, int, str, str | None, str]] = []
    preferred_group = guess.subset_group
    for group in get_subset_groups(guess.set_name):
        variants = get_subset_variants(guess.set_name, group)
        configured = [(None, group)] if not variants else [
            (variant, format_subset_for_set(guess.set_name, group, variant))
            for variant in variants
        ]
        for variant, final_subset in configured:
            normalized_subset = _normalize(final_subset)
            marker_rank = next(
                (
                    index for index, marker in enumerate(ONE_OF_ONE_SUBSET_MARKERS)
                    if marker in normalized_subset
                ),
                None,
            )
            if marker_rank is None:
                continue
            context_rank = 0 if preferred_group and group == preferred_group else 1
            candidates.append((context_rank, marker_rank, group, variant, final_subset))

    if not candidates:
        return

    candidates.sort(key=lambda item: (item[0], item[1]))
    _context_rank, _marker_rank, group, variant, final_subset = candidates[0]
    guess.subset_group = group
    guess.subset_variant = variant
    guess.subset_value = final_subset
    guess.confidence["subset_one_of_one"] = 0.86
    if group not in guess.subset_option_order:
        guess.subset_option_order.insert(0, group)
    if variant and variant not in guess.variant_option_order:
        guess.variant_option_order.insert(0, variant)


def _apply_subset_guess(guess: ListingMetadataGuess, text: str) -> None:
    if not guess.set_name:
        return

    final_subset_rankings = _rank_candidates(text, get_all_subset_names(guess.set_name))
    if final_subset_rankings and final_subset_rankings[0][1] >= 0.62:
        final_subset, final_score = final_subset_rankings[0]
        for group in get_subset_groups(guess.set_name):
            variants = get_subset_variants(guess.set_name, group)
            if not variants and group == final_subset:
                guess.subset_group = group
                guess.subset_value = group
                guess.confidence["subset_value"] = final_score
                break
            for variant in variants:
                if format_subset_for_set(guess.set_name, group, variant) == final_subset:
                    guess.subset_group = group
                    guess.subset_variant = variant
                    guess.subset_value = final_subset
                    guess.confidence["subset_value"] = final_score
                    break
            if guess.subset_value:
                break

    groups = get_subset_groups(guess.set_name)
    group_rankings = _rank_candidates(text, groups)
    guess.subset_option_order = [candidate for candidate, _ in group_rankings]
    if guess.subset_group and guess.subset_group in guess.subset_option_order:
        guess.subset_option_order.remove(guess.subset_group)
        guess.subset_option_order.insert(0, guess.subset_group)
    if guess.subset_value:
        variants = get_subset_variants(guess.set_name, guess.subset_group)
        if variants:
            variant_rankings = _rank_candidates(text, variants)
            guess.variant_option_order = [variant for variant, _ in variant_rankings]
            if guess.subset_variant and guess.subset_variant in guess.variant_option_order:
                guess.variant_option_order.remove(guess.subset_variant)
                guess.variant_option_order.insert(0, guess.subset_variant)
        return
    if not group_rankings or group_rankings[0][1] < 0.55:
        return

    group, group_score = group_rankings[0]
    guess.subset_group = group
    guess.confidence["subset_group"] = group_score

    variants = get_subset_variants(guess.set_name, group)
    if not variants:
        guess.subset_value = group
        return

    final_rankings = [
        (
            variant,
            _score_candidate(text, format_subset_for_set(guess.set_name, group, variant)),
        )
        for variant in variants
    ]
    final_rankings.sort(key=lambda item: item[1], reverse=True)
    guess.variant_option_order = [variant for variant, _ in final_rankings]
    if final_rankings and final_rankings[0][1] >= 0.55:
        variant, variant_score = final_rankings[0]
        guess.subset_variant = variant
        guess.subset_value = format_subset_for_set(guess.set_name, group, variant)
        guess.confidence["subset_variant"] = variant_score


def _apply_card_count_subset_fallback(guess: ListingMetadataGuess, text: str) -> None:
    subset_text = _normalize(" ".join([
        str(guess.subset_value or ""),
        str(guess.subset_variant or ""),
        str(guess.subset_group or ""),
    ]))
    search_text = subset_text or _normalize(text)
    if not search_text:
        return

    tokens = set(search_text.split())
    for marker, count in CARD_COUNT_SUBSET_FALLBACKS:
        if marker in tokens or marker in search_text:
            if guess.card_count is None:
                guess.card_count = count
                guess.card_count_source = "subset_fallback"
                guess.confidence["card_count"] = 0.55
            elif guess.card_count > count:
                guess.card_count = count
                guess.card_count_source = "subset_guard"
                guess.confidence["card_count"] = max(
                    guess.confidence.get("card_count", 0),
                    0.65,
                )
            return


def extract_listing_metadata(
    image_bytes: bytes | None,
    *,
    known_players: list[str] | None = None,
) -> ListingMetadataGuess:
    guess = ListingMetadataGuess()
    if not image_bytes:
        return guess

    text, engine, available = _read_text_from_image(image_bytes)
    guess.text = text
    guess.engine = engine
    guess.ocr_available = available
    guess.card_count = _extract_card_count(text)
    if guess.card_count is not None:
        guess.card_count_source = "ocr_one_of_one" if guess.card_count == 1 and _contains_one_of_one(text) else "ocr_text"
    else:
        guess.card_count = _extract_bottom_pill_count(image_bytes)
        if guess.card_count is not None:
            guess.card_count_source = "bottom_pill_one_of_one" if guess.card_count == 1 else "bottom_pill"
    if guess.card_count is not None:
        if guess.card_count_source == "ocr_one_of_one":
            guess.confidence["card_count"] = 0.9
        elif guess.card_count_source == "bottom_pill_one_of_one":
            guess.confidence["card_count"] = 0.82
        else:
            guess.confidence["card_count"] = 0.7 if guess.card_count_source == "bottom_pill" else 0.75
    guess.card_rarity = _normalize_card_rarity(text)
    if guess.card_rarity:
        guess.card_rarity_source = "ocr_text"
        guess.confidence["card_rarity"] = 0.65
    else:
        guess.card_rarity = _extract_bottom_pill_rarity(image_bytes)
        if guess.card_rarity:
            guess.card_rarity_source = "bottom_pill"
            guess.confidence["card_rarity"] = 0.7

    set_rankings = _prefer_specific_set(text, _rank_set_candidates(text, get_set_names()))
    guess.set_option_order = [candidate for candidate, _ in set_rankings]
    if set_rankings and set_rankings[0][1] >= 0.55:
        guess.set_name, score = set_rankings[0]
        guess.confidence["set_name"] = score
        _apply_subset_guess(guess, text)
        _apply_color_subset_guess(guess, image_bytes)
        _apply_one_of_one_subset_guard(guess)

    _apply_card_count_subset_fallback(guess, text)

    guess.player_name, player_score = _extract_player_name(text, known_players or [])
    if guess.player_name:
        guess.confidence["player_name"] = player_score
    elif player_score:
        guess.confidence["player_name_rejected"] = player_score

    return guess
