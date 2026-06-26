from discord import ui

from datetime import datetime
from typing import Any, Dict, Optional


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
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number.") from exc


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
    }

    def format_piece(piece: str) -> str:
        lowered = piece.lower()
        return known_casing.get(lowered, lowered[:1].upper() + lowered[1:])

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
    date_value = date_value or datetime.utcnow()
    return f"{date_value.strftime('%B')} {date_value.day}, {date_value.year}"


def parse_card_modal_fields(modal: ui.Modal) -> Dict[str, Any]:
    """Parse the shared fields used by listing and transaction modals."""
    subset = parse_optional_int(modal.subset.value, default=0, field_name="Subset")
    card_count = parse_optional_int(modal.card_count.value, default=1, field_name="Card Count")
    price = parse_required_float(modal.price.value, field_name="Price")

    if card_count <= 0:
        raise ValueError("Card Count must be greater than 0.")
    if price <= 0:
        raise ValueError("Price must be greater than 0.")


    return {
        "player_names": normalize_player_name(modal.player_names.value),
        "set_name": modal.card_set.value.strip(),
        "subset": subset,
        "card_count": card_count,
        "price": price,
    }

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
