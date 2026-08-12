"""Provider-neutral marketplace listing domain and static external snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID


SOURCE_DISCORD = "discord"
SOURCE_CHASEFIENDS = "chasefiends"
LISTING_KIND_PLAYER = "player"
LISTING_KIND_SET = "set"
CHASEFIENDS_DISPLAY_NAME = "ChaseFiends"
CHASEFIENDS_LISTING_HOST = "chasefiends.com"
CHASEFIENDS_IMAGE_HOSTS = {
    "dcrppqygcupfshshjizs.supabase.co",
}
DEFAULT_CHASEFIENDS_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent / "data" / "chasefiends_nba_listings.jsonl"
)


@dataclass(frozen=True)
class MarketplaceListing:
    """Canonical listing fields consumed by the Discord marketplace browser."""

    source: str
    source_listing_id: str
    player_names: str
    set_name: str
    subset: str
    card_count: int | str
    price: float
    status: str = "active"
    listing_type: str = "sale"
    card_rarity: str | None = None
    currency: str = "USD"
    seller_name: str | None = None
    image_url: str | None = None
    source_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    title: str | None = None
    team: str | None = None
    year: int | None = None
    variant: str | None = None
    offers_allowed: bool = False
    minimum_offer: float | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    native_data: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def marketplace_id(self) -> str:
        return f"{self.source}:{self.source_listing_id}"

    @property
    def is_external(self) -> bool:
        return self.source != SOURCE_DISCORD

    def to_view_data(self) -> dict[str, Any]:
        data = dict(self.native_data)
        data.update({
            "marketplace_id": self.marketplace_id,
            "source": self.source,
            "source_listing_id": self.source_listing_id,
            "source_label": (
                CHASEFIENDS_DISPLAY_NAME
                if self.source == SOURCE_CHASEFIENDS
                else "Discord"
            ),
            "source_url": self.source_url,
            "is_external": self.is_external,
            "capabilities": list(self.capabilities),
            "player_names": self.player_names,
            "set_name": self.set_name,
            "subset": self.subset,
            "card_count": self.card_count,
            "card_rarity": self.card_rarity,
            "price": self.price,
            "currency": self.currency,
            "seller_name": self.seller_name,
            "image_url": self.image_url,
            "status": self.status,
            "listing_type": self.listing_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "external_title": self.title,
            "team": self.team,
            "year": self.year,
            "variant": self.variant,
            "offers_allowed": self.offers_allowed,
            "minimum_offer": self.minimum_offer,
        })
        return data


def _clean_text(value: Any, *, fallback: str = "") -> str:
    return " ".join(str(value or fallback).split()).strip()


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_chasefiends_listing_url(listing_id: str, value: Any) -> str | None:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != CHASEFIENDS_LISTING_HOST:
        return None
    if parsed.path.rstrip("/") != f"/listings/{listing_id}":
        return None
    return url


def _safe_external_image_url(value: Any) -> str | None:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in CHASEFIENDS_IMAGE_HOSTS:
        return None
    return url


def native_listing_to_domain(listing: dict[str, Any]) -> MarketplaceListing | None:
    """Adapt one existing Discord listing without changing its native fields."""
    message_id = listing.get("message_id")
    if not message_id:
        return None
    listing_type = "auction" if listing.get("listing_type") == "auction" else "sale"
    capabilities = (
        ("bid", "manage", "report")
        if listing_type == "auction"
        else ("claim", "offer", "manage", "report")
    )
    return MarketplaceListing(
        source=SOURCE_DISCORD,
        source_listing_id=str(message_id),
        player_names=_clean_text(listing.get("player_names"), fallback="Unknown player"),
        set_name=_clean_text(listing.get("set_name"), fallback="Unknown set"),
        subset=_clean_text(listing.get("subset"), fallback="Unknown subset"),
        card_count=listing.get("card_count", 1),
        card_rarity=_clean_text(listing.get("card_rarity")) or None,
        price=float(listing.get("price") or 0),
        currency="USD",
        seller_name=_clean_text(listing.get("seller_name")) or None,
        image_url=listing.get("image_url"),
        created_at=listing.get("created_at"),
        updated_at=listing.get("updated_at"),
        status=str(listing.get("status") or "active"),
        listing_type=listing_type,
        capabilities=capabilities,
        native_data=listing,
    )


def chasefiends_record_to_domain(record: dict[str, Any]) -> MarketplaceListing | None:
    """Validate and adapt one sanitized ChaseFiends snapshot record."""
    if record.get("source") != SOURCE_CHASEFIENDS:
        return None
    if record.get("platform") != "Topps NBA Collect":
        return None
    if str(record.get("status") or "").lower() != "active":
        return None
    if record.get("listing_type") != "buy_now":
        return None

    listing_id = str(record.get("id") or "").strip()
    try:
        UUID(listing_id)
    except (ValueError, AttributeError):
        return None
    source_url = _safe_chasefiends_listing_url(listing_id, record.get("source_url"))
    price = _positive_float(record.get("asking_price"))
    if source_url is None or price is None:
        return None

    card_count = record.get("global_count")
    try:
        card_count = int(card_count)
    except (TypeError, ValueError):
        card_count = "ANY"
    if isinstance(card_count, int) and card_count <= 0:
        card_count = "ANY"

    minimum_offer = _positive_float(record.get("minimum_offer"))
    return MarketplaceListing(
        source=SOURCE_CHASEFIENDS,
        source_listing_id=listing_id,
        player_names=_clean_text(
            record.get("player_name"),
            fallback=record.get("title") or "Unknown player",
        ),
        set_name=_clean_text(record.get("set_name"), fallback="Unknown set"),
        subset=_clean_text(record.get("subset_name"), fallback="Unknown subset"),
        card_count=card_count,
        card_rarity=_clean_text(record.get("rarity")).title() or None,
        price=price,
        currency=_clean_text(record.get("currency"), fallback="USD").upper(),
        seller_name=_clean_text(record.get("seller_ign")) or "ChaseFiends seller",
        image_url=_safe_external_image_url(record.get("image_url")),
        source_url=source_url,
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
        title=_clean_text(record.get("title")) or None,
        team=_clean_text(record.get("team")) or None,
        year=int(record["year"]) if str(record.get("year") or "").isdigit() else None,
        variant=_clean_text(record.get("variant")) or None,
        offers_allowed=bool(record.get("offers_allowed")),
        minimum_offer=minimum_offer,
        status="active",
        listing_type="external_sale",
        capabilities=("go_to_site", "listing_info"),
    )


def load_chasefiends_snapshot(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Load a local JSON-lines snapshot; this function performs no network I/O."""
    snapshot_path = Path(path) if path else DEFAULT_CHASEFIENDS_SNAPSHOT_PATH
    if not snapshot_path.is_absolute():
        snapshot_path = Path(__file__).resolve().parent.parent / snapshot_path
    if not snapshot_path.exists():
        return []

    listings = []
    with snapshot_path.open("r", encoding="utf-8") as snapshot_file:
        for line_number, line in enumerate(snapshot_file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid ChaseFiends snapshot JSON on line {line_number}."
                ) from exc
            if record.get("record_type") == "snapshot_metadata":
                continue
            listing = chasefiends_record_to_domain(record)
            if listing is not None:
                listings.append(listing.to_view_data())
    return listings


def marketplace_inventory(bot: Any) -> list[dict[str, Any]]:
    """Return active native and static external listings in one domain shape."""
    inventory = []
    for listing in (getattr(bot, "active_listings", {}) or {}).values():
        domain_listing = native_listing_to_domain(listing)
        if domain_listing is not None:
            inventory.append(domain_listing.to_view_data())
    inventory.extend(
        dict(listing)
        for listing in (getattr(bot, "external_listings", []) or [])
    )
    return inventory


def listing_key(listing: dict[str, Any]) -> str:
    return str(
        listing.get("marketplace_id")
        or f"{listing.get('source', SOURCE_DISCORD)}:{listing.get('message_id', '')}"
    )


def is_external_listing(listing: dict[str, Any]) -> bool:
    return (
        bool(listing.get("is_external"))
        or listing.get("source", SOURCE_DISCORD) != SOURCE_DISCORD
    )


def is_set_listing(listing: dict[str, Any]) -> bool:
    return listing.get("listing_kind", LISTING_KIND_PLAYER) == LISTING_KIND_SET


def active_marketplace_inventory(bot: Any) -> list[dict[str, Any]]:
    return [
        listing
        for listing in marketplace_inventory(bot)
        if str(listing.get("status") or "active").lower() in {"active", "open"}
    ]
