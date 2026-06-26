import discord
from typing import Any, Dict
from datetime import datetime, timezone


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


def build_listing_embed(
    listing_data: Dict[str, Any],
    *,
    sold: bool = False,
    claimed: bool = False,
) -> discord.Embed:
    """Build the public sale listing embed."""
    removed = str(listing_data.get("status", "")).lower() == "removed"
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
    embed = discord.Embed(
        title=f"{title_prefix}{listing_data['player_names']} - {listing_data['set_name']}",
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
    payment_methods = str(listing_data.get("payment_methods") or "").strip()
    if payment_methods:
        footer = f"{footer} | Payment: {payment_methods}"
    if removed:
        footer = f"{footer} | Removed/archived"
    embed.set_footer(text=footer)
    return embed
