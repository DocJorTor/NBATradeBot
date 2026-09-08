import discord
from typing import Any, Dict
from datetime import datetime, timezone

from marketplace import CHASEFIENDS_DISPLAY_NAME, is_external_listing, is_set_listing


def format_card_count(card_count) -> str:
    if card_count in (None, "", "Any", "ANY"):
        return "Any"
    if str(card_count) in {"999", "9999"}:
        return "Unlimited"
    return str(card_count)


def _seller_display_name(listing_data: Dict[str, Any]) -> str:
    seller = listing_data.get("seller")
    return (
        getattr(seller, "display_name", None)
        or getattr(seller, "name", None)
        or str(listing_data.get("seller_id", "Unknown seller"))
    )


def _format_auction_end(value: str) -> str:
    if not value:
        return "Not set"
    try:
        end_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= end_at:
        return "Ended"
    return f"<t:{int(end_at.timestamp())}:R>"


def _listing_created_at(listing_data: Dict[str, Any]) -> datetime | None:
    value = listing_data.get("created_at")
    if value:
        try:
            created_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            return created_at
        except (TypeError, ValueError):
            pass
    message_id = listing_data.get("message_id")
    if message_id:
        try:
            return discord.utils.snowflake_time(int(message_id))
        except (TypeError, ValueError):
            pass
    return None


def _listing_time_text(listing_data: Dict[str, Any]) -> str:
    created_at = _listing_created_at(listing_data)
    if created_at is None:
        return ""
    timestamp = int(created_at.timestamp())
    return f"Listed: <t:{timestamp}:F>"


def _public_payment_platforms(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    platforms = []
    seen = set()

    def add_platform(platform: str) -> None:
        platform = platform.strip()
        key = platform.casefold()
        if platform and key not in seen:
            seen.add(key)
            platforms.append(platform)

    known_platforms = {
        "paypal": "PayPal",
        "venmo": "Venmo",
        "cash app": "Cash App",
        "cashapp": "Cash App",
        "zelle": "Zelle",
        "revolut": "Revolut",
        "wise": "Wise",
        "apple pay": "Apple Pay",
        "google pay": "Google Pay",
    }
    # Never publish an unstructured value verbatim: legacy values may contain
    # an email address or username even when they lack the expected colon.
    for value_part in text.replace("\n", ",").split(","):
        if ":" in value_part:
            add_platform(value_part.split(":", 1)[0])
        else:
            normalized = value_part.strip().casefold()
            for key, display in known_platforms.items():
                if key in normalized:
                    add_platform(display)

    return ", ".join(platforms)


def _listing_image_url(listing_data: Dict[str, Any]) -> str | None:
    image_url = listing_data.get("image_url")
    surface_image_url = listing_data.get("surface_image_url")
    if image_url and not str(image_url).startswith("attachment://"):
        return image_url
    return surface_image_url or image_url


def highest_active_bid(listing_data: Dict[str, Any]) -> Dict[str, Any] | None:
    bids = [
        bid for bid in listing_data.get("bids", [])
        if str(bid.get("status", "placed")).lower() in {"placed", "active", "pending", "accepted"}
    ]
    if not bids:
        return None
    return max(bids, key=lambda bid: float(bid.get("amount") or bid.get("bid_amount") or 0))


def _external_price_text(listing_data: Dict[str, Any]) -> str:
    price = float(listing_data.get("price") or 0)
    currency = str(listing_data.get("currency") or "USD").upper()
    return f"${price:,.2f}" if currency == "USD" else f"{price:,.2f} {currency}"


def build_external_listing_embed(listing_data: Dict[str, Any]) -> discord.Embed:
    """Build a clearly attributed external listing in the native sale style."""
    raw_card_count = listing_data.get("card_count")
    card_count_text = (
        "Not provided"
        if raw_card_count in (None, "", "Any", "ANY")
        else f"/{format_card_count(raw_card_count)}"
    )
    details = [
        f"Subset: {listing_data.get('subset') or 'Not provided'}",
        f"Card Count: {card_count_text}",
        f"Listing Price: {_external_price_text(listing_data)}",
    ]
    listing_time = _listing_time_text(listing_data)
    if listing_time:
        details.append(listing_time)

    source_url = listing_data.get("source_url")
    embed = discord.Embed(
        title=(
            f"🌐 {listing_data.get('player_names') or 'External Listing'} - "
            f"{listing_data.get('set_name') or 'Unknown set'}"
        )[:256],
        url=source_url,
        description="\n".join(details),
        color=discord.Color.blue(),
    )
    image_url = _listing_image_url(listing_data)
    if image_url:
        embed.set_image(url=image_url)
    seller_name = str(listing_data.get("seller_name") or "ChaseFiends seller")
    embed.set_footer(
        text=(
            f"Listed on {CHASEFIENDS_DISPLAY_NAME} by {seller_name} | "
            "External listing — all activity occurs on ChaseFiends"
        )
    )
    return embed


def build_set_listing_embed(
    listing_data: Dict[str, Any],
    *,
    sold: bool = False,
    claimed: bool = False,
) -> discord.Embed:
    """Build a fixed-price listing for a complete or partial collection set."""
    status = str(listing_data.get("status", "")).lower()
    removed = status in {"removed", "voided", "cancelled"}
    title_prefix = "REMOVED - " if removed else "SOLD - " if sold else "CLAIMED - " if claimed else ""
    cards_owned = int(listing_data.get("set_cards_owned") or 0)
    cards_total = int(listing_data.get("set_cards_total") or 0)
    complete = cards_total > 0 and cards_owned >= cards_total
    details = [
        f"Subset: {listing_data.get('subset') or 'Not provided'}",
        f"Rarity: {listing_data.get('card_rarity') or 'Not provided'}",
        f"Set Progress: {cards_owned}/{cards_total} cards",
        f"Status: {'Complete' if complete else 'Incomplete'}",
        f"Includes Award: {'Yes' if listing_data.get('includes_award') else 'No'}",
    ]
    missing_cards = str(listing_data.get("missing_cards") or "").strip()
    if not complete and missing_cards:
        details.append(f"Missing Cards: {missing_cards}")
    if listing_data.get("additional_information"):
        details.append(f"Notes: {listing_data['additional_information']}")
    details.append(f"Listing Price: ${float(listing_data.get('price') or 0):.2f}")
    listing_time = _listing_time_text(listing_data)
    if listing_time:
        details.append(listing_time)

    set_name = str(listing_data.get("set_name") or listing_data.get("player_names") or "Unknown Set")
    embed = discord.Embed(
        title=f"{title_prefix}{set_name} - Set"[:256],
        description="\n".join(details),
        color=(
            discord.Color.dark_grey()
            if removed
            else discord.Color.red()
            if sold
            else discord.Color.yellow()
            if claimed
            else discord.Color.blue()
        ),
    )
    image_url = _listing_image_url(listing_data)
    if image_url:
        embed.set_image(url=image_url)

    footer = f"Listed by {_seller_display_name(listing_data)}"
    payment_platforms = _public_payment_platforms(listing_data.get("payment_methods"))
    payment_notes = str(listing_data.get("payment_notes") or "").strip()
    if payment_platforms or payment_notes:
        footer = f"{footer} | Payment Platforms: {payment_platforms or 'Not provided'}"
    if payment_notes:
        footer = f"{footer} | Payment Notes: {payment_notes}"
    if removed:
        footer = f"{footer} | Removed/archived"
    embed.set_footer(text=footer)
    return embed


def build_listing_embed(
    listing_data: Dict[str, Any],
    *,
    sold: bool = False,
    claimed: bool = False,
) -> discord.Embed:
    """Build the public sale listing embed."""
    if is_external_listing(listing_data):
        return build_external_listing_embed(listing_data)
    if is_set_listing(listing_data):
        return build_set_listing_embed(listing_data, sold=sold, claimed=claimed)

    status = str(listing_data.get("status", "")).lower()
    removed = status in {"removed", "voided", "cancelled"}
    is_auction = str(listing_data.get("listing_type", "sale")).lower() == "auction"
    title_prefix = "REMOVED - " if removed else "SOLD - " if sold else "CLAIMED - " if claimed else ""
    if is_auction:
        highest_bid = highest_active_bid(listing_data)
        highest_amount = (
            float(highest_bid.get("amount") or highest_bid.get("bid_amount"))
            if highest_bid else None
        )
        highest_text = f"${highest_amount:.2f}" if highest_amount is not None else "No bids yet"
        starting_price = float(listing_data.get("starting_price") or listing_data["price"])
        desc = (
            f"Subset: {listing_data['subset']} \n"
            f"Card Count: /{format_card_count(listing_data['card_count'])}\n"
            f"Starting Bid: ${starting_price:.2f}\n"
            f"Current High Bid: {highest_text}\n"
            f"Ends: {_format_auction_end(listing_data.get('auction_end_at'))}"
        )
    else:
        desc = (
            f"Subset: {listing_data['subset']} \n"
            f"Card Count: /{format_card_count(listing_data['card_count'])}\n"
            f"Listing Price: ${listing_data['price']:.2f}"
        )
    if listing_data.get("card_rarity"):
        desc = f"{desc}\nRarity: {listing_data['card_rarity']}"
    if listing_data.get("additional_information"):
        desc = f"{desc}\nNotes: {listing_data['additional_information']}"
    listing_time = _listing_time_text(listing_data)
    if listing_time:
        desc = f"{desc}\n{listing_time}"
    embed = discord.Embed(
        title=f"{title_prefix}{listing_data['player_names']} - {listing_data['set_name']}"[:256],
        description=desc,
        color=(
            discord.Color.dark_grey()
            if removed
            else discord.Color.red()
            if sold
            else discord.Color.yellow()
            if claimed
            else discord.Color.gold()
            if is_auction
            else discord.Color.blue()
        ),
    )
    image_url = _listing_image_url(listing_data)
    if image_url:
        embed.set_image(url=image_url)

    footer = f"{'Auction' if is_auction else 'Listed'} by {_seller_display_name(listing_data)}"
    payment_platforms = _public_payment_platforms(listing_data.get("payment_methods"))
    payment_notes = str(listing_data.get("payment_notes") or "").strip()
    if payment_platforms or payment_notes:
        footer = f"{footer} | Payment Platforms: {payment_platforms or 'Not provided'}"
    if payment_notes:
        footer = f"{footer} | Payment Notes: {payment_notes}"
    if removed:
        footer = f"{footer} | Removed/archived"
    embed.set_footer(text=footer)
    return embed
