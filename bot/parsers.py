from datetime import datetime, timezone
import math
import re
from typing import Optional


ANY_VALUE = "ANY"
def parse_optional_int(value: str, *, default: int, field_name: str) -> int:
    """Parse an optional integer text field with a friendly error message."""
    value = value.strip()
    if not value:
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc


def parse_required_float(value: str, *, field_name: str) -> float:
    """Parse a required decimal text field with a friendly error message."""
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} is required.")

    try:
        parsed = float(value.replace("$", "").replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be a finite number.")
    if parsed > 1_000_000:
        raise ValueError(f"{field_name} must be $1,000,000 or less.")
    return parsed


def normalize_player_name(value: str) -> str:
    """Normalize player-name input for consistent display."""
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
    }
    normalized = str(value or "").strip()
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    normalized = " ".join(normalized.split())
    if not normalized:
        return ""

    known_casing = {
        "lebron": "LeBron",
        "dejounte": "Dejounte",
        "lamelo": "LaMelo",
        "lonzo": "Lonzo",
        "pj": "PJ",
        "cj": "CJ",
        "rj": "RJ",
        "tj": "TJ",
        "aj": "AJ",
        "ii": "II",
        "iii": "III",
        "iv": "IV",
        "jr": "Jr",
        "jr.": "Jr.",
        "sr": "Sr",
        "sr.": "Sr.",
    }

    def format_piece(piece: str) -> str:
        lowered = piece.lower()
        if lowered in known_casing:
            return known_casing[lowered]
        if re.fullmatch(r"(?:[A-Za-z]\.){2,}", piece):
            return piece.upper()
        if any(character.isupper() for character in piece[1:]):
            return piece
        if lowered.startswith("mc") and len(lowered) > 2:
            return f"Mc{lowered[2].upper()}{lowered[3:]}"
        return lowered[:1].upper() + lowered[1:]

    def format_token(token: str) -> str:
        hyphen_parts = []
        for hyphen_part in token.split("-"):
            apostrophe_parts = [
                format_piece(part)
                for part in hyphen_part.split("'")
            ]
            hyphen_parts.append("'".join(apostrophe_parts))
        return "-".join(hyphen_parts)

    return " ".join(format_token(token) for token in normalized.split(" "))


def format_sheet_datetime(date_value: Optional[datetime] = None) -> str:
    """Format a transaction date string consistent with sheet sync format."""
    date_value = date_value or datetime.now(timezone.utc)
    return f"{date_value.strftime('%B')} {date_value.day}, {date_value.year}"


def normalize(value):
    if value in (None, "", ANY_VALUE):
        return None
    return str(value).strip().lower()


def normalize_card_count_value(value):
    if value in (None, "", ANY_VALUE):
        return None
    text = str(value).strip().lower()
    if text in {"unlimited", "999", "9999"}:
        return 999
    return int(value)

def listing_matches_notify_rule(listing_data: dict, rule: dict) -> bool:
    player_filter = normalize(rule.get("player_name"))
    listing_players = normalize(listing_data.get("player_names")) or ""
    if player_filter and player_filter not in listing_players:
        return False

    if rule.get("set_name") and normalize(rule["set_name"]) != normalize(listing_data.get("set_name")):
        return False

    if rule.get("subset") and normalize(rule["subset"]) != normalize(listing_data.get("subset")):
        return False

    if (
        rule.get("card_count")
        and normalize_card_count_value(rule["card_count"])
        != normalize_card_count_value(listing_data.get("card_count"))
    ):
        return False

    return True
