import discord

from discord import ui
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from bot import NBACollectBot

from actions import collect_listing_image, publish_listing
from components import (
    ANY_VALUE,
    add_search_buttons,
    build_card_count_options,
    build_set_options,
    build_subset_options,
    build_variant_options,
    on_card_count_select,
    on_set_select,
    on_subset_select,
    on_variant_select,
)
from config import format_subset_for_set, get_subset_groups, get_subset_variants
from database import CardDatabase
from logger import LOGGER, log_marketplace_event
from parsers import (
    format_sheet_datetime,
    normalize_player_name,
    parse_optional_int,
    parse_required_float,
)
from price_assist import PriceResultsView, build_price_assist
from serializers import build_listing_embed, format_card_count, highest_active_bid


class DisabledListingView(ui.View):
    """Disabled version of listing buttons after a listing is closed."""

    def __init__(self, primary_label: str = "Claimed", secondary_label: str = "Offers Closed"):
        super().__init__(timeout=None)
        self.primary_label = primary_label
        self.secondary_label = secondary_label
        if len(self.children) >= 2:
            self.children[0].label = primary_label
            self.children[1].label = secondary_label

    @ui.button(label="Closed", style=discord.ButtonStyle.grey, disabled=True, custom_id="listing:closed")
    async def claimed_button(self, interaction: discord.Interaction, button: ui.Button):
        button.label = self.primary_label
        await interaction.response.defer()

    @ui.button(label="Bidding Closed", style=discord.ButtonStyle.grey, disabled=True, custom_id="listing:bidding_closed")
    async def bidding_closed_button(self, interaction: discord.Interaction, button: ui.Button):
        button.label = self.secondary_label
        await interaction.response.defer()


def _listing_id(listing_data: Dict[str, Any]) -> int:
    return listing_data.get("message_id")


def _price_source(bot: "NBACollectBot", db: CardDatabase):
    return getattr(bot, "sheet", None) or db


def _seller_contact_text(seller: discord.abc.User) -> str:
    return f"Seller: {getattr(seller, 'mention', getattr(seller, 'name', 'Unknown seller'))}"


def _buyer_contact_text(buyer: discord.abc.User) -> str:
    return f"Buyer: {getattr(buyer, 'mention', getattr(buyer, 'name', 'Unknown buyer'))}"


def _deal_participants_text(seller: discord.abc.User, buyer: discord.abc.User) -> str:
    return "\n".join([
        _seller_contact_text(seller),
        _buyer_contact_text(buyer),
    ])


def _trust_stats_line(db: CardDatabase, user_id: int | None) -> str:
    if user_id is None:
        return "Listings sold: 0 | Listings bought: 0"
    stats = db.get_marketplace_user_stats(user_id)
    return (
        f"Listings sold: {stats['listings_sold']} | "
        f"Listings bought: {stats['listings_bought']}"
    )


def _seller_trust_text(db: CardDatabase, seller_id: int | None) -> str:
    return f"Seller trust: {_trust_stats_line(db, seller_id)}"


def _buyer_trust_text(db: CardDatabase, buyer_id: int | None) -> str:
    return f"Buyer trust: {_trust_stats_line(db, buyer_id)}"


def _card_details_block(listing_data: Dict[str, Any]) -> str:
    details = [
        f"Card: {listing_data.get('player_names', 'Unknown card')}",
        f"Set: {listing_data.get('set_name', 'Unknown set')}",
    ]
    subset = listing_data.get("subset")
    if subset:
        details.append(f"Subset: {subset}")
    details.append(f"Card Count: /{format_card_count(listing_data.get('card_count'))}")
    return "\n".join(details)


def _claim_price(listing_data: Dict[str, Any]) -> float:
    return float(listing_data.get("claim_price") or listing_data.get("price") or 0)


async def _clear_ephemeral_prompt(message, success_text: str) -> None:
    if message is None:
        return
    try:
        await message.delete()
        return
    except discord.NotFound:
        return
    except discord.HTTPException:
        LOGGER.warning("Could not delete ephemeral prompt message; clearing controls.")

    try:
        await message.edit(content=success_text, embed=None, view=None)
    except (discord.NotFound, discord.HTTPException):
        pass


async def _send_deal_listing_dms(
    bot: "NBACollectBot",
    db: CardDatabase,
    listing_data: Dict[str, Any],
    seller: discord.abc.User,
    buyer: discord.abc.User,
    *,
    context: str,
) -> discord.Thread | None:
    """Create the deal thread, falling back to listing DMs if needed."""
    thread = None
    if hasattr(bot, "create_deal_thread"):
        thread = await bot.create_deal_thread(
            listing_data,
            seller,
            buyer,
            reason=f"Marketplace {context.replace('_', ' ')}",
        )
    if thread is not None:
        return thread

    content = _deal_participants_text(seller, buyer)
    for user, user_context in ((seller, f"{context}_seller_listing"), (buyer, f"{context}_buyer_listing")):
        sent = await bot.safe_dm_listing(user, listing_data, content=content)
        if not sent:
            log_marketplace_event(
                db,
                "dm_failed",
                user_id=getattr(user, "id", None),
                listing_id=_listing_id(listing_data),
                details={"context": user_context},
                level=30,
            )
    return thread


async def _send_deal_thread_update(
    bot: "NBACollectBot",
    db: CardDatabase,
    listing_data: Dict[str, Any],
    thread: discord.Thread,
    *,
    title: str,
    description: str,
    color: discord.Color,
    context: str,
    view: ui.View | None = None,
) -> bool:
    try:
        message = await thread.send(
            embed=discord.Embed(
                title=title,
                description=description,
                color=color,
            ),
            view=view,
        )
        if view is not None:
            listing_data["deal_thread_action_channel_id"] = getattr(message.channel, "id", None)
            listing_data["deal_thread_action_message_id"] = message.id
            db.upsert_marketplace_listing(listing_data)
        return True
    except discord.DiscordException as exc:
        LOGGER.warning("Could not send deal thread update %s: %s", getattr(thread, "id", thread), exc)
        log_marketplace_event(
            db,
            "deal_thread_update_failed",
            user_id=listing_data.get("seller_id"),
            listing_id=_listing_id(listing_data),
            details={"context": context, "thread_id": getattr(thread, "id", None), "reason": type(exc).__name__},
            level=30,
        )
        return False


class MarketplaceCarouselView(ui.View):
    """Browse active marketplace listings one at a time."""

    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        listings: list[Dict[str, Any]],
        *,
        owner_id: int | None = None,
    ):
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.owner_id = owner_id
        self.listings = listings
        self.index = 0
        self.search_query = ""
        self.player_page = 0
        self.sort_mode = "newest"
        self.price_filter = ""
        self.reset_to_first_on_refresh = False
        self._missing_image_logged = set()
        self.player_select = ui.Select(
            placeholder="Filter by player",
            min_values=1,
            max_values=1,
            custom_id="marketplace:player_filter",
            row=0,
        )
        self.player_select.callback = self.player_filter_selected
        self.add_item(self.player_select)
        self.price_select = ui.Select(
            placeholder="Price filter / sort",
            min_values=1,
            max_values=1,
            custom_id="marketplace:price_filter",
            row=2,
            options=self._price_filter_options(),
        )
        self.price_select.callback = self.price_filter_selected
        self.add_item(self.price_select)
        self._sync_buttons()

    @staticmethod
    def _is_open_listing(listing_data: Dict[str, Any]) -> bool:
        return str(listing_data.get("status", "active")).lower() in {"active", "open"}

    def _refresh_listings(self) -> None:
        reset_to_first = self.reset_to_first_on_refresh
        self.reset_to_first_on_refresh = False
        current_id = None if reset_to_first else self.current_listing().get("message_id") if self.listings else None
        search_query = self.search_query.casefold().strip()
        listings = [
            listing
            for listing in (getattr(self.bot, "active_listings", {}) or {}).values()
            if self._is_open_listing(listing)
            and (
                not search_query
                or search_query in str(listing.get("player_names") or "").casefold()
            )
        ]
        if self.price_filter:
            max_price = float(self.price_filter)
            listings = [
                listing for listing in listings
                if float(listing.get("price") or 0) <= max_price
            ]
        if self.sort_mode == "price_asc":
            listings.sort(key=lambda listing: (float(listing.get("price") or 0), str(listing.get("player_names") or "").casefold()))
        elif self.sort_mode == "price_desc":
            listings.sort(key=lambda listing: (float(listing.get("price") or 0), str(listing.get("player_names") or "").casefold()), reverse=True)
        else:
            listings.sort(
                key=lambda listing: (
                    str(listing.get("date_time") or ""),
                    int(listing.get("message_id") or 0),
                ),
                reverse=True,
            )
        self.listings = listings
        if not self.listings:
            self.index = 0
            return
        if reset_to_first:
            self.index = 0
            return
        matching_index = next(
            (
                index
                for index, listing in enumerate(self.listings)
                if listing.get("message_id") == current_id
            ),
            None,
        )
        self.index = matching_index if matching_index is not None else min(self.index, len(self.listings) - 1)

    def _player_filter_options(self) -> list[discord.SelectOption]:
        seen = set()
        options = [
            discord.SelectOption(
                label="All Players",
                value="__all__",
                default=not self.search_query,
            )
        ]
        active_listings = [
            listing
            for listing in (getattr(self.bot, "active_listings", {}) or {}).values()
            if self._is_open_listing(listing)
        ]
        active_listings.sort(key=lambda listing: str(listing.get("player_names") or "").casefold())
        player_names = []
        for listing in active_listings:
            player_name = str(listing.get("player_names") or "").strip()
            if not player_name:
                continue
            player_key = player_name.casefold()
            if player_key in seen:
                continue
            seen.add(player_key)
            player_names.append(player_name)

        page_size = 24
        page_count = max(1, (len(player_names) + page_size - 1) // page_size)
        self.player_page = min(self.player_page, page_count - 1)
        start = self.player_page * page_size
        for player_name in player_names[start:start + page_size]:
            options.append(
                discord.SelectOption(
                    label=player_name[:100],
                    value=player_name[:100],
                    default=player_name == self.search_query,
                )
            )
        return options

    def _price_filter_options(self) -> list[discord.SelectOption]:
        options = [
            ("Newest Listings", "newest", not self.price_filter and self.sort_mode == "newest"),
            ("Price: Low to High", "price_asc", not self.price_filter and self.sort_mode == "price_asc"),
            ("Price: High to Low", "price_desc", not self.price_filter and self.sort_mode == "price_desc"),
            ("Under $25", "under:25", self.price_filter == "25"),
            ("Under $50", "under:50", self.price_filter == "50"),
            ("Under $100", "under:100", self.price_filter == "100"),
            ("Under $250", "under:250", self.price_filter == "250"),
        ]
        return [
            discord.SelectOption(label=label, value=value, default=selected)
            for label, value, selected in options
        ]

    def current_listing(self) -> Dict[str, Any]:
        return self.listings[self.index]

    async def build_embed(self, *, log_missing_image: bool = True) -> discord.Embed:
        self._refresh_listings()
        if not self.listings:
            description = "No active listings are available right now."
            if self.search_query:
                description = f"No active listings match `{self.search_query}`."
            return discord.Embed(
                title="Marketplace",
                description=description,
                color=discord.Color.dark_grey(),
            )

        listing_data = self.current_listing()
        await self.bot.ensure_listing_image_url(listing_data)
        embed = build_listing_embed(listing_data)
        image_url = (
            listing_data.get("image_url")
            if not str(listing_data.get("image_url") or "").startswith("attachment://")
            else listing_data.get("surface_image_url")
        ) or listing_data.get("surface_image_url")
        if image_url and not embed.image.url:
            embed.set_image(url=image_url)
        if (
            log_missing_image
            and not embed.image.url
            and listing_data.get("message_id") not in self._missing_image_logged
        ):
            self._missing_image_logged.add(listing_data.get("message_id"))
            log_marketplace_event(
                self.db,
                "marketplace_view_missing_image",
                listing_id=_listing_id(listing_data),
                details={
                    "player_names": listing_data.get("player_names"),
                    "has_image_url": bool(listing_data.get("image_url")),
                    "has_surface_image_url": bool(listing_data.get("surface_image_url")),
                },
                level=30,
            )
        footer = embed.footer.text or ""
        position = f"Listing {self.index + 1} of {len(self.listings)}"
        if self.search_query:
            position = f"{position} | Search: {self.search_query}"
        if self.price_filter:
            position = f"{position} | Under ${float(self.price_filter):.0f}"
        elif self.sort_mode == "price_asc":
            position = f"{position} | Price low-high"
        elif self.sort_mode == "price_desc":
            position = f"{position} | Price high-low"
        embed.set_footer(text=f"{position} | {footer}" if footer else position)
        return embed

    async def build_message_kwargs(self) -> Dict[str, Any]:
        embed = await self.build_embed(log_missing_image=False)
        if not self.listings:
            return {"embed": embed}
        kwargs = await self.bot.build_listing_message_kwargs(self.current_listing(), embed=embed)
        if (
            not getattr(kwargs["embed"].image, "url", None)
            and not kwargs.get("file")
            and self.current_listing().get("message_id") not in self._missing_image_logged
        ):
            listing_data = self.current_listing()
            self._missing_image_logged.add(listing_data.get("message_id"))
            log_marketplace_event(
                self.db,
                "marketplace_view_missing_image",
                listing_id=_listing_id(listing_data),
                details={
                    "player_names": listing_data.get("player_names"),
                    "has_image_url": bool(listing_data.get("image_url")),
                    "has_surface_image_url": bool(listing_data.get("surface_image_url")),
                    "has_image_bytes": bool(self.bot.listing_image_bytes(listing_data)),
                },
                level=30,
            )
        return kwargs

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        current_listing_id = None
        if self.listings:
            current_listing_id = _listing_id(self.current_listing())
        log_marketplace_event(
            self.db,
            "marketplace_view_exception",
            user_id=getattr(interaction.user, "id", None),
            listing_id=current_listing_id,
            details={
                "item": getattr(item, "custom_id", None),
                "error": type(error).__name__,
                "message": str(error),
            },
            level=40,
        )
        LOGGER.exception("Marketplace carousel interaction failed")

    async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return False
        await interaction.response.send_message(
            "Only the person who opened this marketplace viewer can use these controls.",
            ephemeral=True,
        )
        return True

    def _sync_buttons(self) -> None:
        has_listings = bool(self.listings)
        self.player_select.options = self._player_filter_options()
        self.player_select.disabled = len(self.player_select.options) <= 1
        self.price_select.options = self._price_filter_options()
        for child in self.children:
            custom_id = getattr(child, "custom_id", "")
            if custom_id in {"marketplace:prev", "marketplace:next"}:
                child.disabled = len(self.listings) <= 1
            if custom_id == "marketplace:actions":
                child.disabled = not has_listings
            if custom_id == "marketplace:player_prev":
                child.disabled = self.player_page <= 0
            if custom_id == "marketplace:player_next":
                child.disabled = len(self._all_player_names()) <= (self.player_page + 1) * 24

    def _all_player_names(self) -> list[str]:
        seen = set()
        names = []
        for listing in (getattr(self.bot, "active_listings", {}) or {}).values():
            if not self._is_open_listing(listing):
                continue
            player_name = str(listing.get("player_names") or "").strip()
            key = player_name.casefold()
            if player_name and key not in seen:
                seen.add(key)
                names.append(player_name)
        return sorted(names, key=str.casefold)

    async def _edit(self, interaction: discord.Interaction) -> None:
        try:
            kwargs = await self.build_message_kwargs()
            self._sync_buttons()
            edit_kwargs = {"embed": kwargs["embed"], "view": self, "attachments": []}
            if kwargs.get("file"):
                edit_kwargs["attachments"] = [kwargs["file"]]
            await interaction.response.edit_message(**edit_kwargs)
        except discord.DiscordException as exc:
            log_marketplace_event(
                self.db,
                "marketplace_view_edit_failed",
                user_id=getattr(interaction.user, "id", None),
                listing_id=_listing_id(self.current_listing()) if self.listings else None,
                details={"error": type(exc).__name__, "message": str(exc)},
                level=40,
            )
            LOGGER.exception("Could not edit marketplace carousel message")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The marketplace viewer could not update. Try `/marketplace` again.",
                    ephemeral=True,
                )

    @ui.button(label="Previous Listing", style=discord.ButtonStyle.secondary, row=3, custom_id="marketplace:prev")
    async def previous_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self._reject_wrong_user(interaction):
            return
        if self.listings:
            self.index = (self.index - 1) % len(self.listings)
        await self._edit(interaction)

    @ui.button(label="Next Listing", style=discord.ButtonStyle.secondary, row=3, custom_id="marketplace:next")
    async def next_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self._reject_wrong_user(interaction):
            return
        if self.listings:
            self.index = (self.index + 1) % len(self.listings)
        await self._edit(interaction)

    async def player_filter_selected(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        selected_value = self.player_select.values[0] if self.player_select.values else "__all__"
        self.search_query = "" if selected_value == "__all__" else selected_value
        self.index = 0
        self.reset_to_first_on_refresh = True
        await self._edit(interaction)

    async def price_filter_selected(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        selected_value = self.price_select.values[0] if self.price_select.values else "newest"
        if selected_value.startswith("under:"):
            self.price_filter = selected_value.split(":", 1)[1]
            self.sort_mode = "price_asc"
        else:
            self.price_filter = ""
            self.sort_mode = selected_value
        self.index = 0
        self.reset_to_first_on_refresh = True
        await self._edit(interaction)

    @ui.button(label="Previous Players Page", style=discord.ButtonStyle.secondary, row=1, custom_id="marketplace:player_prev")
    async def player_prev_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self._reject_wrong_user(interaction):
            return
        self.player_page = max(0, self.player_page - 1)
        await self._edit(interaction)

    @ui.button(label="Next Players Page", style=discord.ButtonStyle.secondary, row=1, custom_id="marketplace:player_next")
    async def player_next_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self._reject_wrong_user(interaction):
            return
        if len(self._all_player_names()) > (self.player_page + 1) * 24:
            self.player_page += 1
        await self._edit(interaction)

    @ui.button(label="Card Actions", style=discord.ButtonStyle.blurple, row=4, custom_id="marketplace:actions")
    async def actions_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self._reject_wrong_user(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._refresh_listings()
        if not self.listings:
            self._sync_buttons()
            await interaction.followup.send(
                "There are no active marketplace listings right now.",
                ephemeral=True,
            )
            return

        listing_data = self.current_listing()
        await self.bot.ensure_listing_image_url(listing_data)
        view_cls = AuctionActionView if listing_data.get("listing_type") == "auction" else ListingActionView
        log_marketplace_event(
            self.db,
            "marketplace_view_actions_opened",
            user_id=interaction.user.id,
            listing_id=_listing_id(listing_data),
            details={
                "player_names": listing_data.get("player_names"),
                "listing_type": listing_data.get("listing_type", "sale"),
                "has_image": bool(listing_data.get("image_url") or listing_data.get("surface_image_url")),
            },
        )
        kwargs = await self.bot.build_listing_message_kwargs(listing_data)
        await interaction.followup.send(
            **kwargs,
            view=view_cls(self.bot, self.db, listing_data),
            ephemeral=True,
        )

async def _notify_user_on_listing(
    bot: "NBACollectBot",
    db: CardDatabase,
    listing_data: Dict[str, Any],
    user: discord.abc.User,
    message: str,
    *,
    context: str,
) -> bool:
    """Tag a user near the listing when their DM could not be delivered."""
    listing_id = _listing_id(listing_data)
    channel = await bot.fetch_channel_safely(listing_data.get("channel_id"))
    if channel is None:
        log_marketplace_event(
            db,
            "channel_notify_failed",
            user_id=getattr(user, "id", None),
            listing_id=listing_id,
            details={"context": context, "reason": "channel_not_found"},
            level=30,
        )
        return False

    content = f"{getattr(user, 'mention', user)} {message}"
    try:
        listing_message = await channel.fetch_message(listing_id) if listing_id else None
        if listing_message is not None:
            await listing_message.reply(content, mention_author=False)
        else:
            await channel.send(content)
    except discord.NotFound:
        try:
            await channel.send(content)
        except discord.DiscordException as exc:
            LOGGER.warning("Could not notify user %s in listing channel: %s", getattr(user, "id", user), exc)
            log_marketplace_event(
                db,
                "channel_notify_failed",
                user_id=getattr(user, "id", None),
                listing_id=listing_id,
                details={"context": context, "reason": type(exc).__name__},
                level=30,
            )
            return False
    except discord.DiscordException as exc:
        LOGGER.warning("Could not notify user %s in listing channel: %s", getattr(user, "id", user), exc)
        log_marketplace_event(
            db,
            "channel_notify_failed",
            user_id=getattr(user, "id", None),
            listing_id=listing_id,
            details={"context": context, "reason": type(exc).__name__},
            level=30,
        )
        return False

    log_marketplace_event(
        db,
        "channel_notify_sent",
        user_id=getattr(user, "id", None),
        listing_id=listing_id,
        details={"context": context},
    )
    return True


def _user_has_active_bid(listing_data: Dict[str, Any], user_id: int) -> bool:
    active_statuses = {"placed", "active", "pending", "countered"}
    for bid in listing_data.get("bids", []):
        bid_user_id = bid.get("user_id") or bid.get("bidder_id")
        bid_status = str(bid.get("status", "placed")).lower()
        if bid_user_id == user_id and bid_status in active_statuses:
            return True
    return False


def _bid_is_open(bid_record: Dict[str, Any] | None) -> bool:
    if not bid_record:
        return False
    return str(bid_record.get("status", "placed")).lower() in {"placed", "active", "pending", "countered"}


def _auction_has_active_bids(listing_data: Dict[str, Any]) -> bool:
    return any(_bid_is_open(bid) for bid in listing_data.get("bids", []))


def _find_bid_by_id(listing_data: Dict[str, Any], bid_id: int | None) -> Dict[str, Any] | None:
    if bid_id is None:
        return None
    for bid in listing_data.get("bids", []):
        if bid.get("id") == bid_id:
            return bid
    return None


def _auction_end_at(listing_data: Dict[str, Any]) -> datetime | None:
    value = listing_data.get("auction_end_at")
    if not value:
        return None
    try:
        end_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    return end_at


def _auction_has_ended(listing_data: Dict[str, Any]) -> bool:
    end_at = _auction_end_at(listing_data)
    return end_at is not None and datetime.now(timezone.utc) >= end_at


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _auction_started_at(listing_data: Dict[str, Any]) -> datetime | None:
    started_at = _parse_datetime(listing_data.get("created_at"))
    if started_at is not None:
        return started_at
    message_id = listing_data.get("message_id")
    if not message_id:
        return None
    try:
        return discord.utils.snowflake_time(int(message_id))
    except (TypeError, ValueError):
        return None


def _auction_can_be_removed(listing_data: Dict[str, Any]) -> bool:
    if _auction_has_active_bids(listing_data):
        return False
    started_at = _auction_started_at(listing_data)
    if started_at is None:
        return not listing_data.get("message_id")
    return datetime.now(timezone.utc) < started_at + timedelta(minutes=10)


def _parse_card_count_edit(value: str):
    normalized = str(value or "").strip()
    if normalized.upper() in {"ANY", "ANY CARD COUNT"}:
        return "ANY"
    if normalized.lower() in {"unlimited", "unlimited edition"}:
        return 999
    if normalized.lower().startswith("cc"):
        normalized = normalized[2:].strip(" /#")
    return parse_optional_int(normalized, default=1, field_name="Card Count")


def _split_subset_for_set(set_name: str | None, subset_value: str | None) -> tuple[str | None, str | None, str | None]:
    if not set_name or not subset_value:
        return None, None, subset_value
    for group in get_subset_groups(set_name):
        variants = get_subset_variants(set_name, group)
        if not variants and group == subset_value:
            return group, None, group
        for variant in variants:
            final_subset = format_subset_for_set(set_name, group, variant)
            if final_subset == subset_value:
                return group, variant, final_subset
    return subset_value, None, subset_value


async def _apply_listing_edit(
    bot: "NBACollectBot",
    db: CardDatabase,
    interaction: discord.Interaction,
    listing_data: Dict[str, Any],
    *,
    player_names: str,
    set_name: str,
    subset: str,
    card_count,
    price: float,
    payment_methods: str,
    send_confirmation: bool = True,
) -> None:
    before = {
        "player_names": listing_data.get("player_names"),
        "set_name": listing_data.get("set_name"),
        "price": listing_data.get("price"),
        "subset": listing_data.get("subset"),
        "card_count": listing_data.get("card_count"),
        "payment_methods": listing_data.get("payment_methods"),
    }
    old_message_id = listing_data.get("message_id")
    old_surface_message_id = listing_data.get("surface_message_id")
    old_price = float(listing_data.get("price", 0))
    price_changed = old_price != float(price)
    listing_data.update(
        {
            "player_names": player_names,
            "set_name": set_name,
            "subset": subset,
            "card_count": card_count,
            "price": price,
            "payment_methods": payment_methods,
        }
    )
    if payment_methods:
        db.upsert_seller_payment_methods(listing_data["seller_id"], payment_methods)
    if listing_data.get("message_id"):
        bot.active_listings[listing_data["message_id"]] = listing_data
    if listing_data.get("listing_type") != "auction":
        listing_data["price_assist"] = build_price_assist(_price_source(bot, db), listing_data)

    bumped = False
    if price_changed:
        bumped = await bot.bump_listing_messages(
            listing_data,
            old_price=old_price,
            new_price=price,
        )
        if bumped and old_message_id and old_message_id != listing_data.get("message_id"):
            db.update_marketplace_listing_status(old_message_id, "removed")

    if not bumped:
        view_cls = AuctionActionView if listing_data.get("listing_type") == "auction" else ListingActionView
        await bot.edit_listing_messages(
            listing_data,
            embed=build_listing_embed(listing_data),
            view=view_cls(bot, db, listing_data),
        )

    db.upsert_marketplace_listing(listing_data)
    log_marketplace_event(
        db,
        "listing_edited",
        user_id=interaction.user.id,
        listing_id=_listing_id(listing_data),
        details={"before": before, "after": {
            "player_names": player_names,
            "set_name": set_name,
            "price": price,
            "subset": subset,
            "card_count": card_count,
            "payment_methods": payment_methods,
            "bumped": bumped,
            "old_message_id": old_message_id,
            "new_message_id": listing_data.get("message_id"),
            "old_surface_message_id": old_surface_message_id,
            "new_surface_message_id": listing_data.get("surface_message_id"),
        }},
    )
    if send_confirmation:
        message = "Listing updated."
        if bumped:
            message = "Listing updated and bumped with a price update note."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class EditListingDetailsModal(ui.Modal, title="Edit Listing Details"):
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        self.player_names = ui.TextInput(
            label="Player Name(s)",
            default=str(parent_view.player_names or "")[:4000],
            required=False,
        )
        self.price = ui.TextInput(
            label="Price",
            default=f"{float(parent_view.price or 0):.2f}",
            required=True,
        )
        self.payment_methods = ui.TextInput(
            label="Payment Platforms",
            default=str(parent_view.payment_methods or "")[:4000],
            required=False,
            placeholder="PayPal, Venmo, Cash App, etc.",
        )
        self.add_item(self.player_names)
        self.add_item(self.price)
        self.add_item(self.payment_methods)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            player_names = normalize_player_name(self.player_names.value) or self.parent_view.player_names
            price = parse_required_float(self.price.value, field_name="Price")
            payment_methods = str(self.payment_methods.value or "").strip()
            if price <= 0:
                raise ValueError("Price must be greater than 0.")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        self.parent_view.player_names = player_names
        self.parent_view.price = price
        self.parent_view.payment_methods = payment_methods
        await interaction.response.edit_message(
            content=self.parent_view.selected_summary(),
            view=self.parent_view,
        )


class EditListingWorkflowView(ui.View):
    """Ephemeral listing edit flow matching listing creation controls."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, listing_data: Dict[str, Any], owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self.owner_id = owner_id
        self.player_names = listing_data.get("player_names")
        self.price = float(listing_data.get("price") or 0)
        self.payment_methods = (
            listing_data.get("payment_methods")
            or db.get_seller_payment_methods(listing_data.get("seller_id"))
            or ""
        )
        self.set_optional = False
        self.subset_required = True
        self.set_value = listing_data.get("set_name")
        self.subset_group_value, self.subset_variant_value, self.subset_value = _split_subset_for_set(
            self.set_value,
            listing_data.get("subset"),
        )
        self.card_count_value = _parse_card_count_edit(str(listing_data.get("card_count", 1)))

        self.set_select = ui.Select(
            placeholder="Select Card Set",
            options=build_set_options(self),
            row=0,
        )
        async def handle_set_select(interaction: discord.Interaction):
            await on_set_select(self, interaction, True)
        self.set_select.callback = handle_set_select
        self.add_item(self.set_select)

        self.subset_select = ui.Select(
            placeholder="Select Subset",
            options=build_subset_options(self) if self.set_value else [
                discord.SelectOption(label="Select a set first", value="select_set_first")
            ],
            disabled=not self.set_value,
            row=1,
        )
        self.subset_select.callback = self.on_subset_select
        self.add_item(self.subset_select)

        self.variant_select = ui.Select(
            placeholder="Select Variant",
            options=build_variant_options(self),
            disabled=not (self.set_value and self.subset_group_value and get_subset_variants(self.set_value, self.subset_group_value)),
            row=2,
        )
        self.variant_select.callback = self.on_variant_select
        self.add_item(self.variant_select)

        self.card_count_select = ui.Select(
            placeholder="Select Card Count",
            options=build_card_count_options(selected_value=str(self.card_count_value)),
            row=3,
        )
        async def handle_card_count_select(interaction: discord.Interaction):
            await on_card_count_select(self, interaction)
        self.card_count_select.callback = handle_card_count_select
        self.add_item(self.card_count_select)

        self.details_button = ui.Button(
            label="Price / Payment",
            style=discord.ButtonStyle.blurple,
            row=4,
        )
        self.details_button.callback = self.on_details
        self.add_item(self.details_button)

        self.save_button = ui.Button(
            label="Save Listing",
            style=discord.ButtonStyle.green,
            row=4,
        )
        self.save_button.callback = self.on_save
        self.add_item(self.save_button)
        add_search_buttons(self, row=4)

    async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return False
        await interaction.response.send_message(
            "Only the seller can edit this listing.",
            ephemeral=True,
        )
        return True

    def selected_summary(self) -> str:
        return "\n".join([
            "**Edit listing details**",
            f"Player: **{self.player_names or 'Needs entry'}**",
            f"Set: **{self.set_value or 'Needs selection'}**",
            f"Subset: **{self.subset_value or self.subset_group_value or 'Needs selection'}**",
            f"Card Count: **/{format_card_count(self.card_count_value)}**",
            f"Price: **${float(self.price or 0):.2f}**",
            f"Payment: **{self.payment_methods or 'Not set'}**",
        ])

    async def on_subset_select(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        await on_subset_select(self, interaction)

    async def on_variant_select(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        await on_variant_select(self, interaction)

    async def on_details(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        await interaction.response.send_modal(EditListingDetailsModal(self))

    async def on_save(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message("This listing is no longer active.", ephemeral=True)
            return
        if not self.player_names or not self.set_value or not self.subset_value:
            await interaction.response.edit_message(
                content=f"{self.selected_summary()}\n\nPlease select player, set, and subset before saving.",
                view=self,
            )
            return
        if float(self.price or 0) <= 0:
            await interaction.response.edit_message(
                content=f"{self.selected_summary()}\n\nUse Price / Payment before saving.",
                view=self,
            )
            return

        await interaction.response.defer()
        await _apply_listing_edit(
            self.bot,
            self.db,
            interaction,
            self.listing_data,
            player_names=self.player_names,
            set_name=self.set_value,
            subset=self.subset_value,
            card_count=self.card_count_value if self.card_count_value is not None else "ANY",
            price=float(self.price),
            payment_methods=self.payment_methods,
            send_confirmation=False,
        )

        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            LOGGER.warning("Could not delete edit listing workflow message; clearing controls.")
            try:
                await interaction.edit_original_response(content="Listing updated.", view=None)
            except (discord.NotFound, discord.HTTPException):
                pass
        self.stop()


class SetSubsetSelectView(ui.View):
    """View for selecting card set and subset before listing."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, sale_channel: discord.TextChannel):
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.sale_channel = sale_channel
        self.selected_set = None
        self.selected_subset = None

    @ui.select(
        placeholder="Select Card Set",
        options=[
            discord.SelectOption(label=name.replace("_", " ").title(), value=name)
            for name in ["2025-26 Flagship", "2025-26 Topps Now", "2025-26 Topps Chrome",]
        ]
    )
    async def set_select(self, interaction: discord.Interaction, select: ui.Select):
        self.selected_set = select.values[0]
        await interaction.response.defer()

    @ui.select(
        placeholder="Select Subset",
        options=[
            discord.SelectOption(label="0", value="0"),
            discord.SelectOption(label="1", value="1"),
            discord.SelectOption(label="All Kings", value="All Kings"),
            discord.SelectOption(label="Base", value="Base"),
            discord.SelectOption(label="Other", value="Other"),
        ]
    )
    async def subset_select(self, interaction: discord.Interaction, select: ui.Select):
        self.selected_subset = select.values[0]
        await interaction.response.defer()

    @ui.button(label="Continue to List Card", style=discord.ButtonStyle.green)
    async def continue_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_set or not self.selected_subset:
            await interaction.response.send_message("Please select both set and subset.", ephemeral=True)
            return

        # Open the modal with pre-filled set and subset
        modal = CardListingModal(self.bot, self.db, self.sale_channel, prefill_set=self.selected_set, prefill_subset=self.selected_subset)
        await interaction.response.send_modal(modal)


class CardListingModal(ui.Modal, title="List Player Card for Sale"):
    """Modal for users to enter card details and upload an image when listing for sale."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, sale_channel: discord.TextChannel, prefill_set: str = None, prefill_subset: str = None):
        super().__init__()
        self.bot = bot
        self.db = db
        self.sale_channel = sale_channel
        self.prefill_set = prefill_set
        self.prefill_subset = prefill_subset

        player_names = ui.TextInput(label="Player Name(s)", placeholder="LeBron James")
        card_count = ui.TextInput(label="Card Count", placeholder="1", required=False)
        price = ui.TextInput(label="Price", placeholder="256")

        self.add_item(player_names)
        self.add_item(card_count)
        self.add_item(price)

        self.player_names = player_names
        self.card_count = card_count
        self.price = price

    async def on_submit(self, interaction: discord.Interaction):
        try:
            player_names = normalize_player_name(self.player_names.value)
            card_set = self.prefill_set.replace("_", " ").title() if self.prefill_set else ""
            price = parse_required_float(self.price.value, field_name="Price")
            subset = self.prefill_subset if self.prefill_subset else "N/A"
            card_count = parse_optional_int(self.card_count.value, default=1, field_name="Card Count")
            if not player_names or not card_set or price <= 0 or card_count <= 0:
                raise ValueError("Required fields are missing or invalid.")
        except Exception as exc:
            log_marketplace_event(
                self.db,
                "listing_create_failed",
                user_id=interaction.user.id,
                details=str(exc),
                level=40,
            )
            await interaction.response.send_message(f"Invalid input: {exc}", ephemeral=True)
            return

        if self.sale_channel is None:
            LOGGER.error("Sale channel is None. sale_channel_id: %s", getattr(self.bot, 'sale_channel_id', None))
            log_marketplace_event(
                self.db,
                "listing_create_failed",
                user_id=interaction.user.id,
                details="Sale channel is not configured.",
                level=40,
            )
            await interaction.response.send_message(
                "Sale channel is not configured or could not be found. Please contact an admin.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Please upload the card image in this channel within 2 minutes.",
            ephemeral=True,
        )

        try:
            upload_prompt = await interaction.original_response()
        except discord.DiscordException:
            upload_prompt = None

        image_url, image_file, image_bytes = await collect_listing_image(
            self.bot,
            self.db,
            interaction,
            upload_prompt,
        )

        listing_data = {
            "player_names": player_names,
            "set_name": card_set,
            "subset": subset,
            "card_count": card_count,
            "price": price,
            "date_time": format_sheet_datetime(),
            "seller": interaction.user,
            "seller_id": interaction.user.id,
            "status": "active",
            "image_url": image_url,
            "image_bytes": image_bytes,
            "image_filename": "card_image.png",
            "bids": [],
        }

        await publish_listing(
            self.bot,
            self.db,
            self.sale_channel,
            interaction,
            listing_data,
            image_file=image_file,
        )

        self.db.add_sale(
            player_names=player_names,
            set_name=card_set,
            subset=subset,
            date_time=listing_data["date_time"],
            price=price,
            card_count=card_count,
            seller_id=interaction.user.id,
            image_url=listing_data.get("image_url"),
        )

        await interaction.followup.send(
            "Card listed for sale!",
            ephemeral=True,
        )


class BidPromptView(ui.View):
    """Trust prompt that opens the bid or offer modal."""

    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        listing_data: Dict[str, Any],
        bidder: discord.User,
        *,
        auction: bool = False,
    ):
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self.bidder = bidder
        self.auction = auction
        for child in self.children:
            if getattr(child, "custom_id", None) == "bid_prompt:continue":
                child.label = "Continue to Bid" if auction else "Continue to Offer"

    @ui.button(label="Continue to Bid", style=discord.ButtonStyle.blurple, custom_id="bid_prompt:continue")
    async def continue_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.bidder.id:
            action = "bid prompt" if self.auction else "offer prompt"
            await interaction.response.send_message(
                f"Only the person who opened this {action} can use it.",
                ephemeral=True,
            )
            return
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message("This listing is no longer active.", ephemeral=True)
            return
        if self.auction and _auction_has_ended(self.listing_data):
            await interaction.response.send_message("This auction has ended.", ephemeral=True)
            return
        if not self.auction and _user_has_active_bid(self.listing_data, interaction.user.id):
            await interaction.response.send_message(
                "You already have an active offer on this listing. Wait for the seller to accept or decline it before making another.",
                ephemeral=True,
            )
            return

        if self.auction:
            modal = AuctionBidModal(self.bot, self.db, self.listing_data, interaction.user)
        else:
            modal = BidModal(
                self.bot,
                self.db,
                self.listing_data,
                interaction.user,
                prompt_message=getattr(interaction, "message", None),
            )
        await interaction.response.send_modal(modal)

    @ui.button(label="Cancel", style=discord.ButtonStyle.grey, custom_id="bid_prompt:cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.bidder.id:
            action = "bid prompt" if self.auction else "offer prompt"
            await interaction.response.send_message(
                f"Only the person who opened this {action} can use it.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            content="Bid cancelled." if self.auction else "Offer cancelled.",
            embed=None,
            view=None,
        )
        self.stop()


class ListingActionView(ui.View):
    """View with Claim and Make an Offer buttons for card listings."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, listing_data: Dict[str, Any]):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self._sync_price_assist()

    def _sync_price_assist(self) -> None:
        price_assist = build_price_assist(_price_source(self.bot, self.db), self.listing_data)
        self.listing_data["price_assist"] = price_assist
        if not price_assist.get("results"):
            return

        button = ui.Button(
            label="Price Assist",
            style=discord.ButtonStyle.secondary,
            row=2,
            custom_id="listing:price_assist",
        )
        button.callback = self.on_price_assist
        self.add_item(button)

    def listing_is_active(self) -> bool:
        return self.listing_data.get("status") == "active"

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log_marketplace_event(
            self.db,
            "listing_action_exception",
            user_id=getattr(interaction.user, "id", None),
            listing_id=_listing_id(self.listing_data),
            details=str(error),
            level=40,
        )
        LOGGER.exception("Listing action failed")

    async def reject_if_inactive(self, interaction: discord.Interaction) -> bool:
        if self.listing_is_active():
            return False

        await interaction.response.send_message(
            "This listing is no longer active.", ephemeral=True
        )
        return True

    @ui.button(label="Claim", style=discord.ButtonStyle.green, custom_id="listing:claim")
    async def claim_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_inactive(interaction):
            return

        if interaction.user.id == self.listing_data["seller_id"]:
            await interaction.response.send_message(
                "You cannot claim your own listing.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Confirm Purchase",
            description=(
                f"Purchase {self.listing_data['player_names']} "
                f"from {self.listing_data['seller'].name} "
                f"for ${self.listing_data['price']:.2f}?\n\n"
                f"{_seller_trust_text(self.db, self.listing_data.get('seller_id'))}"
            ),
            color=discord.Color.green(),
        )

        confirm_view = ConfirmPurchaseView(self.bot, self.db, self.listing_data)
        await interaction.response.send_message(embed=embed, view=confirm_view, ephemeral=True)

    @ui.button(label="Make an Offer", style=discord.ButtonStyle.blurple, custom_id="listing:bid")
    async def bid_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_inactive(interaction):
            return

        if interaction.user.id == self.listing_data["seller_id"]:
            await interaction.response.send_message(
                "You cannot make an offer on your own listing.", ephemeral=True
            )
            return
        if _user_has_active_bid(self.listing_data, interaction.user.id):
            await interaction.response.send_message(
                "You already have an active offer on this listing. Wait for the seller to accept or decline it before making another.",
                ephemeral=True,
            )
            return

        seller = self.listing_data.get("seller")
        seller_name = getattr(seller, "display_name", None) or getattr(seller, "name", "this seller")
        embed = discord.Embed(
            title="Review Seller Before Making an Offer",
            description=(
                f"Seller: {seller_name}\n"
                f"{_seller_trust_text(self.db, self.listing_data.get('seller_id'))}\n\n"
                f"{_card_details_block(self.listing_data)}\n\n"
                f"Asking price: ${float(self.listing_data['price']):.2f}"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=BidPromptView(self.bot, self.db, self.listing_data, interaction.user),
            ephemeral=True,
        )

    async def on_price_assist(self, interaction: discord.Interaction):
        price_assist = self.listing_data.get("price_assist")
        if not price_assist:
            price_assist = build_price_assist(_price_source(self.bot, self.db), self.listing_data)
            self.listing_data["price_assist"] = price_assist

        results = price_assist.get("results") or []
        if not results:
            await interaction.response.send_message(
                "Sorry, there were no similar matches found for this card",
                ephemeral=True,
            )
            return

        result_view = PriceResultsView(
            results,
            interaction.user.id,
            price_assist.get("results_heading") or "Recent price results",
            price_assist.get("range_label"),
        )
        paginated_view = result_view if len(results) > result_view.page_size else None
        send_kwargs = {
            "content": result_view.content(),
            "embeds": result_view.embeds(),
            "ephemeral": True,
        }
        if paginated_view is not None:
            send_kwargs["view"] = paginated_view
        await interaction.response.send_message(**send_kwargs)

    async def reject_if_not_seller(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.listing_data["seller_id"]:
            return False

        await interaction.response.send_message(
            "Only the seller can manage this listing.", ephemeral=True
        )
        return True

    @ui.button(label="Edit Listing", style=discord.ButtonStyle.grey, row=1, custom_id="listing:edit")
    async def edit_listing_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_inactive(interaction):
            return
        if await self.reject_if_not_seller(interaction):
            return

        view = EditListingWorkflowView(self.bot, self.db, self.listing_data, interaction.user.id)
        await interaction.response.send_message(
            view.selected_summary(),
            view=view,
            ephemeral=True,
        )

    @ui.button(label="Remove Listing", style=discord.ButtonStyle.red, row=1, custom_id="listing:remove")
    async def remove_listing_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_inactive(interaction):
            return
        if await self.reject_if_not_seller(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        self.listing_data["status"] = "removed"
        self.bot.active_listings.pop(self.listing_data.get("message_id"), None)
        await self.bot.delete_listing_messages(self.listing_data)
        self.db.update_marketplace_listing_status(
            self.listing_data["message_id"],
            "removed",
        )
        log_marketplace_event(
            self.db,
            "listing_removed",
            user_id=interaction.user.id,
            listing_id=_listing_id(self.listing_data),
            details={"player_names": self.listing_data.get("player_names")},
        )
        await interaction.followup.send("Listing removed.", ephemeral=True)


class AuctionActionView(ui.View):
    """View with bidding controls for auction listings."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, listing_data: Dict[str, Any]):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        if self.listing_data.get("listing_type") == "auction":
            for child in self.children:
                if getattr(child, "custom_id", None) == "claimed:cancel":
                    child.label = "Auction Locked"
                    child.disabled = True
        self._sync_button_state()

    def auction_is_active(self) -> bool:
        return self.listing_data.get("status") == "active" and not _auction_has_ended(self.listing_data)

    def _sync_button_state(self) -> None:
        ended = _auction_has_ended(self.listing_data)
        can_remove = _auction_can_be_removed(self.listing_data)
        for child in self.children:
            if getattr(child, "custom_id", None) == "auction:bid":
                child.disabled = ended or self.listing_data.get("status") != "active"
            if getattr(child, "custom_id", None) == "auction:end":
                child.label = "Finalize Auction"
                child.disabled = self.listing_data.get("status") != "active"
            if getattr(child, "custom_id", None) == "auction:remove":
                child.disabled = not can_remove or self.listing_data.get("status") != "active"

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log_marketplace_event(
            self.db,
            "auction_action_exception",
            user_id=getattr(interaction.user, "id", None),
            listing_id=_listing_id(self.listing_data),
            details=str(error),
            level=40,
        )
        LOGGER.exception("Auction action failed")

    async def reject_if_not_seller(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.listing_data["seller_id"]:
            return False
        await interaction.response.send_message("Only the seller can manage this auction.", ephemeral=True)
        return True

    @ui.button(label="Place Bid", style=discord.ButtonStyle.blurple, custom_id="auction:bid")
    async def bid_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message("This auction is no longer active.", ephemeral=True)
            return
        if _auction_has_ended(self.listing_data):
            await interaction.response.send_message("This auction has ended. The seller can close it now.", ephemeral=True)
            return
        if interaction.user.id == self.listing_data["seller_id"]:
            await interaction.response.send_message("You cannot bid on your own auction.", ephemeral=True)
            return

        seller = self.listing_data.get("seller")
        seller_name = getattr(seller, "display_name", None) or getattr(seller, "name", "this seller")
        embed = discord.Embed(
            title="Review Seller Before Bidding",
            description=(
                f"Seller: {seller_name}\n"
                f"{_seller_trust_text(self.db, self.listing_data.get('seller_id'))}\n\n"
                f"{_card_details_block(self.listing_data)}"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=BidPromptView(self.bot, self.db, self.listing_data, interaction.user, auction=True),
            ephemeral=True,
        )

    @ui.button(label="Finalize Auction", style=discord.ButtonStyle.green, custom_id="auction:end")
    async def end_auction_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_not_seller(interaction):
            return
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message("This auction is no longer active.", ephemeral=True)
            return
        if not _auction_has_ended(self.listing_data):
            await interaction.response.send_message("This auction has not ended yet.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        winning_bid = highest_active_bid(self.listing_data)
        if not winning_bid:
            self.listing_data["status"] = "removed"
            self.bot.active_listings.pop(self.listing_data.get("message_id"), None)
            await self.bot.edit_listing_messages(
                self.listing_data,
                embed=build_listing_embed(self.listing_data),
                view=DisabledListingView(primary_label="Auction Ended", secondary_label="No Winner"),
            )
            self.db.update_marketplace_listing_status(self.listing_data["message_id"], "removed")
            log_marketplace_event(
                self.db,
                "auction_ended_no_bids",
                user_id=interaction.user.id,
                listing_id=_listing_id(self.listing_data),
            )
            await interaction.followup.send("Auction ended with no bids.", ephemeral=True)
            return

        bidder = await self.bot.hydrate_user(
            winning_bid.get("bidder_id") or winning_bid.get("user_id"),
            winning_bid.get("username"),
        )
        accepted = await finalize_bid_claim(
            self.bot,
            self.db,
            self.listing_data,
            winning_bid,
            bidder,
            float(winning_bid.get("amount") or winning_bid.get("bid_amount")),
            interaction,
            "auction",
        )
        if not accepted:
            await interaction.followup.send("This auction could not be closed.", ephemeral=True)
            return
        await interaction.followup.send("Auction ended. The high bidder has been notified.", ephemeral=True)

    @ui.button(label="Remove Auction", style=discord.ButtonStyle.red, row=1, custom_id="auction:remove")
    async def remove_auction_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_not_seller(interaction):
            return
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message("This auction is no longer active.", ephemeral=True)
            return
        if not _auction_can_be_removed(self.listing_data):
            await interaction.response.send_message(
                "Auctions can only be removed during the first 10 minutes after posting.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        self.listing_data["status"] = "removed"
        self.bot.active_listings.pop(self.listing_data.get("message_id"), None)
        await self.bot.delete_listing_messages(self.listing_data)
        self.db.update_marketplace_listing_status(self.listing_data["message_id"], "removed")
        log_marketplace_event(
            self.db,
            "auction_removed",
            user_id=interaction.user.id,
            listing_id=_listing_id(self.listing_data),
            details={"player_names": self.listing_data.get("player_names")},
        )
        await interaction.followup.send("Auction removed.", ephemeral=True)


class EditListingModal(ui.Modal, title="Edit Listing"):
    """Modal for seller-facing listing edits."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, listing_data: Dict[str, Any]):
        super().__init__()
        self.bot = bot
        self.db = db
        self.listing_data = listing_data

        self.player_names = ui.TextInput(
            label="Player Name(s)",
            default=str(listing_data.get("player_names", ""))[:4000],
            required=False,
        )
        self.price = ui.TextInput(
            label="Price",
            default=f"{float(listing_data.get('price', 0)):.2f}",
            required=True,
        )
        self.subset = ui.TextInput(
            label="Subset",
            default=str(listing_data.get("subset", ""))[:4000],
            required=True,
        )
        self.card_count = ui.TextInput(
            label="Card Count",
            default=str(listing_data.get("card_count", 1)),
            required=True,
        )
        saved_payment_methods = (
            listing_data.get("payment_methods")
            or db.get_seller_payment_methods(listing_data.get("seller_id"))
            or ""
        )
        self.payment_methods = ui.TextInput(
            label="Payment Platforms",
            default=str(saved_payment_methods)[:4000],
            required=False,
            placeholder="PayPal, Venmo, Cash App, etc.",
        )

        self.add_item(self.player_names)
        self.add_item(self.price)
        self.add_item(self.subset)
        self.add_item(self.card_count)
        self.add_item(self.payment_methods)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.listing_data["seller_id"]:
            await interaction.response.send_message(
                "Only the seller can edit this listing.", ephemeral=True
            )
            return
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message(
                "This listing is no longer active.", ephemeral=True
            )
            return

        try:
            player_names = normalize_player_name(self.player_names.value) or self.listing_data["player_names"]
            price = parse_required_float(self.price.value, field_name="Price")
            card_count = _parse_card_count_edit(self.card_count.value)
            subset = str(self.subset.value).strip() or "N/A"
            payment_methods = str(self.payment_methods.value or "").strip()
            if price <= 0 or (isinstance(card_count, int) and card_count <= 0):
                raise ValueError("Price and Card Count must be greater than 0.")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        before = {
            "player_names": self.listing_data.get("player_names"),
            "price": self.listing_data.get("price"),
            "subset": self.listing_data.get("subset"),
            "card_count": self.listing_data.get("card_count"),
            "payment_methods": self.listing_data.get("payment_methods"),
        }
        old_message_id = self.listing_data.get("message_id")
        old_surface_message_id = self.listing_data.get("surface_message_id")
        old_price = float(self.listing_data.get("price", 0))
        price_changed = old_price != float(price)
        self.listing_data.update(
            {
                "player_names": player_names,
                "price": price,
                "subset": subset,
                "card_count": card_count,
                "payment_methods": payment_methods,
            }
        )
        if payment_methods:
            self.db.upsert_seller_payment_methods(self.listing_data["seller_id"], payment_methods)
        if self.listing_data.get("message_id"):
            self.bot.active_listings[self.listing_data["message_id"]] = self.listing_data
        if self.listing_data.get("listing_type") != "auction":
            self.listing_data["price_assist"] = build_price_assist(
                _price_source(self.bot, self.db),
                self.listing_data,
            )

        bumped = False
        if price_changed:
            bumped = await self.bot.bump_listing_messages(
                self.listing_data,
                old_price=old_price,
                new_price=price,
            )
            if bumped and old_message_id and old_message_id != self.listing_data.get("message_id"):
                self.db.update_marketplace_listing_status(old_message_id, "removed")

        if not bumped:
            await self.bot.edit_listing_messages(
                self.listing_data,
                embed=build_listing_embed(self.listing_data),
                view=ListingActionView(self.bot, self.db, self.listing_data),
            )

        self.db.upsert_marketplace_listing(self.listing_data)
        log_marketplace_event(
            self.db,
            "listing_edited",
            user_id=interaction.user.id,
            listing_id=_listing_id(self.listing_data),
            details={"before": before, "after": {
                "player_names": player_names,
                "price": price,
                "subset": subset,
                "card_count": card_count,
                "payment_methods": payment_methods,
                "bumped": bumped,
                "old_message_id": old_message_id,
                "new_message_id": self.listing_data.get("message_id"),
                "old_surface_message_id": old_surface_message_id,
                "new_surface_message_id": self.listing_data.get("surface_message_id"),
            }},
        )
        message = "Listing updated."
        if bumped:
            message = "Listing updated and bumped with a price update note."
        await interaction.followup.send(message, ephemeral=True)


class BidModal(ui.Modal, title="Make an Offer"):
    """Modal for making an offer on a fixed-price listing."""

    bid_price = ui.TextInput(label="Your Offer Price", placeholder="e.g., 140.00")

    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        listing_data: Dict[str, Any],
        bidder: discord.User,
        *,
        prompt_message=None,
    ):
        super().__init__()
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self.bidder = bidder
        self.prompt_message = prompt_message

    async def on_submit(self, interaction: discord.Interaction):
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message(
                "This listing is no longer active.", ephemeral=True
            )
            return
        if _user_has_active_bid(self.listing_data, self.bidder.id):
            await interaction.response.send_message(
                "You already have an active offer on this listing. Wait for the seller to accept or decline it before making another.",
                ephemeral=True,
            )
            return

        try:
            bid_val = parse_required_float(self.bid_price.value, field_name="Offer Price")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if bid_val <= 0:
            await interaction.response.send_message(
                "Offer Price must be greater than 0.", ephemeral=True
            )
            return

        max_bid = self.listing_data["price"]
        if bid_val > max_bid:
            await interaction.response.send_message(
                f"Your offer must be lower than the asking price of (${max_bid:.2f}).",
                ephemeral=True,
            )
            return

        bid_record = {
            "user_id": self.bidder.id,
            "bidder_id": self.bidder.id,
            "username": getattr(self.bidder, "display_name", None) or self.bidder.name,
            "amount": bid_val,
            "bid_amount": bid_val,
            "status": "placed",
            "created_at": format_sheet_datetime(),
        }
        bids = self.listing_data.setdefault("bids", [])
        bids.append(bid_record)
        message_id = self.listing_data.get("message_id")
        if message_id:
            self.bot.active_listings[message_id] = self.listing_data
            bid_record["id"] = self.db.add_marketplace_bid(message_id, bid_record)
        log_marketplace_event(
            self.db,
            "offer_placed",
            user_id=self.bidder.id,
            listing_id=message_id,
            details={"amount": bid_val, "seller_id": self.listing_data.get("seller_id")},
        )

        dm_embed = discord.Embed(
            title="New Offer on Your Listing",
            description=(
                f"{self.bidder.name} has made an offer of ${bid_val:.2f}.\n\n"
                f"{_buyer_trust_text(self.db, self.bidder.id)}\n\n"
                f"{_card_details_block(self.listing_data)}\n\n"
                f"Your asking price: ${self.listing_data['price']:.2f}"
            ),
            color=discord.Color.gold(),
        )
        dm_view = AcceptBidView(self.bot, self.db, self.listing_data, self.bidder, bid_val, bid_record)
        dm_sent = False
        try:
            seller = await self.bot.hydrate_user(
                self.listing_data.get("seller_id"),
                getattr(self.listing_data.get("seller"), "name", None),
                fetch=True,
            )
            self.listing_data["seller"] = seller
            dm_message = await seller.send(embed=dm_embed, view=dm_view)
            dm_sent = True
            bid_record["dm_channel_id"] = dm_message.channel.id
            bid_record["dm_message_id"] = dm_message.id
            if bid_record.get("id"):
                self.db.update_marketplace_bid(
                    bid_record["id"],
                    dm_channel_id=dm_message.channel.id,
                    dm_message_id=dm_message.id,
                )
        except discord.Forbidden:
            LOGGER.warning("Could not DM user %s", getattr(self.listing_data["seller"], "name", self.listing_data["seller"]))
        except discord.DiscordException as exc:
            LOGGER.warning("Could not send offer DM: %s", exc)
        except AttributeError:
            LOGGER.warning("Seller object cannot receive DMs for listing %s", message_id)

        message = "✅ Offer sent! The seller will see it."
        if not dm_sent:
            log_marketplace_event(
                self.db,
                "dm_failed",
                user_id=self.listing_data.get("seller_id"),
                listing_id=message_id,
                details={"context": "new_offer", "bidder_id": self.bidder.id},
                level=30,
            )
            message = "✅ Offer saved, but I could not DM the seller. You may want to message them directly."

        await interaction.response.send_message(message, ephemeral=True)
        await _clear_ephemeral_prompt(self.prompt_message, message)


class AuctionBidModal(ui.Modal, title="Place Auction Bid"):
    """Modal for bidding on an auction listing."""

    bid_price = ui.TextInput(label="Your Bid Price", placeholder="e.g., 140.00")

    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        listing_data: Dict[str, Any],
        bidder: discord.User,
    ):
        super().__init__()
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self.bidder = bidder

    async def on_submit(self, interaction: discord.Interaction):
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message("This auction is no longer active.", ephemeral=True)
            return
        if _auction_has_ended(self.listing_data):
            await interaction.response.send_message("This auction has ended.", ephemeral=True)
            return

        try:
            bid_val = parse_required_float(self.bid_price.value, field_name="Bid Price")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        highest_bid = highest_active_bid(self.listing_data)
        highest_amount = float(highest_bid.get("amount") or highest_bid.get("bid_amount")) if highest_bid else None
        minimum_bid = float(self.listing_data.get("starting_price") or self.listing_data["price"])
        if highest_amount is not None:
            minimum_bid = highest_amount + float(self.listing_data.get("bid_increment") or 1)

        if bid_val < minimum_bid:
            await interaction.response.send_message(
                f"Your bid must be at least ${minimum_bid:.2f}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if highest_bid:
            highest_bid["status"] = "outbid"
            if highest_bid.get("id"):
                self.db.update_marketplace_bid(highest_bid["id"], status="outbid")

        bid_record = {
            "user_id": self.bidder.id,
            "bidder_id": self.bidder.id,
            "username": getattr(self.bidder, "display_name", None) or self.bidder.name,
            "amount": bid_val,
            "bid_amount": bid_val,
            "status": "placed",
            "created_at": format_sheet_datetime(),
        }
        bids = self.listing_data.setdefault("bids", [])
        bids.append(bid_record)
        self.listing_data["price"] = bid_val
        message_id = self.listing_data.get("message_id")
        if message_id:
            self.bot.active_listings[message_id] = self.listing_data
            bid_record["id"] = self.db.add_marketplace_bid(message_id, bid_record)
            self.db.update_marketplace_listing_status(message_id, "active", price=bid_val)

        await self.bot.edit_listing_messages(
            self.listing_data,
            embed=build_listing_embed(self.listing_data),
            view=AuctionActionView(self.bot, self.db, self.listing_data),
        )

        if highest_bid and (highest_bid.get("bidder_id") or highest_bid.get("user_id")) != self.bidder.id:
            previous_bidder = await self.bot.hydrate_user(
                highest_bid.get("bidder_id") or highest_bid.get("user_id"),
                highest_bid.get("username"),
            )
            await self.bot.safe_dm_user(
                previous_bidder,
                embed=discord.Embed(
                    title="You Were Outbid",
                    description=(
                        f"Your bid was topped.\n\n"
                        f"{_card_details_block(self.listing_data)}\n\n"
                        f"The current high bid is ${bid_val:.2f}."
                    ),
                    color=discord.Color.orange(),
                ),
            )

        await self.bot.safe_dm_user(
            self.listing_data["seller"],
            embed=discord.Embed(
                title="New Auction Bid",
                description=(
                    f"{self.bidder.name} bid ${bid_val:.2f} on your auction.\n\n"
                    f"{_buyer_trust_text(self.db, self.bidder.id)}\n\n"
                    f"{_card_details_block(self.listing_data)}"
                ),
                color=discord.Color.gold(),
            ),
        )
        log_marketplace_event(
            self.db,
            "auction_bid_placed",
            user_id=self.bidder.id,
            listing_id=message_id,
            details={"amount": bid_val, "seller_id": self.listing_data.get("seller_id")},
        )

        await interaction.followup.send("Bid placed. The auction has been updated.", ephemeral=True)


async def finalize_bid_claim(
    bot: "NBACollectBot",
    db: CardDatabase,
    listing_data: Dict[str, Any],
    bid_record: Dict[str, Any],
    bidder: discord.User,
    final_price: float,
    interaction: discord.Interaction,
    source: str,
) -> bool:
    if listing_data.get("status") != "active":
        return False
    if not _bid_is_open(bid_record):
        return False
    if source == "counter" and str(bid_record.get("counter_status", "")).lower() != "pending":
        return False

    seller = listing_data["seller"]
    listing_id = _listing_id(listing_data)
    claim_status = (
        "accepted_counter"
        if source == "counter"
        else "accepted_auction_bid"
        if source == "auction"
        else "accepted_offer"
    )
    buyer_name = getattr(bidder, "display_name", None) or bidder.name

    listing_data["status"] = "claimed"
    listing_data["buyer_id"] = bidder.id
    listing_data["buyer"] = bidder
    listing_data["claim_price"] = final_price
    listing_data["claims"] = [{
        "user_id": bidder.id,
        "claimer_id": bidder.id,
        "username": buyer_name,
        "amount": final_price,
        "status": claim_status,
    }]

    bid_record["status"] = "accepted"
    if source == "counter":
        bid_record["counter_status"] = "accepted"
        bid_record["counter_updated_at"] = format_sheet_datetime()

    winning_bid_id = bid_record.get("id")
    for bid in listing_data.get("bids", []):
        if bid is bid_record or (winning_bid_id and bid.get("id") == winning_bid_id):
            bid["status"] = "accepted"
            if source == "counter":
                bid["counter_status"] = "accepted"
            continue
        if str(bid.get("status", "placed")).lower() in {"placed", "active", "pending", "countered"}:
            bid["status"] = "superseded"
            if str(bid.get("counter_status", "")).lower() == "pending":
                bid["counter_status"] = "superseded"
                bid["counter_updated_at"] = format_sheet_datetime()

    if winning_bid_id:
        update_fields = {"status": "accepted"}
        if source == "counter":
            update_fields["counter_status"] = "accepted"
            update_fields["counter_updated_at"] = format_sheet_datetime()
        db.update_marketplace_bid(winning_bid_id, **update_fields)
        db.supersede_other_marketplace_bids(listing_id, winning_bid_id)
    db.upsert_marketplace_listing(listing_data)

    embed = build_listing_embed(listing_data, claimed=True)
    view = ClaimedListingView(bot, db, listing_data)
    await bot.edit_listing_messages(listing_data, embed=embed, view=view)

    deal_thread = await _send_deal_listing_dms(
        bot,
        db,
        listing_data,
        seller,
        bidder,
        context=f"{source}_accepted",
    )

    if source == "auction":
        buyer_title = "Your Auction Bid Won!"
        seller_title = "Auction Ended"
    elif source == "counter":
        buyer_title = "Counter Offer Accepted!"
        seller_title = "Counter Offer Accepted"
    else:
        buyer_title = "Your Offer Was Accepted!"
        seller_title = "Offer Accepted"
    buyer_description = (
        f"You claimed this card for ${final_price:.2f}.\n\n"
        f"{_card_details_block(listing_data)}\n\n"
        f"{_seller_trust_text(db, listing_data.get('seller_id'))}\n\n"
        f"{_seller_contact_text(seller)}"
    )
    seller_description = (
        f"{bidder.name} claimed your card for ${final_price:.2f}.\n\n"
        f"{_buyer_trust_text(db, bidder.id)}\n\n"
        f"{_card_details_block(listing_data)}\n\n"
        f"{_buyer_contact_text(bidder)}"
    )

    if deal_thread is not None:
        await _send_deal_thread_update(
            bot,
            db,
            listing_data,
            deal_thread,
            title=seller_title,
            description=(
                f"Accepted at ${final_price:.2f}.\n\n"
                f"{_deal_participants_text(seller, bidder)}\n\n"
                f"{_seller_trust_text(db, listing_data.get('seller_id'))}\n"
                f"{_buyer_trust_text(db, bidder.id)}\n\n"
                "Coordinate payment and transfer here. The seller can record the transaction after payment is complete. Only the buyer can cancel a claim." 
            ),
            color=discord.Color.green(),
            context=f"{source}_accepted",
            view=ClaimedListingView(bot, db, listing_data),
        )
    else:
        bidder_dm_sent = await bot.safe_dm_user(
            bidder,
            embed=discord.Embed(
                title=buyer_title,
                description=buyer_description,
                color=discord.Color.green(),
            ),
        )
        await bot.safe_dm_user(
            seller,
            embed=discord.Embed(
                title=seller_title,
                description=seller_description,
                color=discord.Color.green(),
            ),
        )
        if not bidder_dm_sent:
            await _notify_user_on_listing(
                bot,
                db,
                listing_data,
                bidder,
                (
                    f"I could not DM you, but your "
                    f"{'counter offer was accepted' if source == 'counter' else 'auction bid won' if source == 'auction' else 'offer was accepted'} "
                    f"at ${final_price:.2f} for {listing_data.get('player_names', 'this card')} "
                    f"({listing_data.get('set_name', 'Unknown set')}). "
                    f"Please coordinate with the seller here."
                ),
                context=f"{source}_accepted",
            )
            log_marketplace_event(
                db,
                "dm_failed",
                user_id=bidder.id,
                listing_id=listing_id,
                details={"context": f"{source}_accepted"},
                level=30,
            )

    event_type = (
        "counter_offer_accepted"
        if source == "counter"
        else "auction_bid_accepted"
        if source == "auction"
        else "offer_accepted"
    )
    log_marketplace_event(
        db,
        event_type,
        user_id=interaction.user.id,
        listing_id=listing_id,
        details={
            "bidder_id": bidder.id,
            "amount": final_price,
            "original_bid": bid_record.get("amount") or bid_record.get("bid_amount"),
            "counter_amount": bid_record.get("counter_amount"),
        },
    )
    return True


class ConfirmPurchaseView(ui.View):
    """View for confirming a purchase."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, listing_data: Dict[str, Any]):
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log_marketplace_event(
            self.db,
            "claim_flow_exception",
            user_id=getattr(interaction.user, "id", None),
            listing_id=_listing_id(self.listing_data),
            details=str(error),
            level=40,
        )
        LOGGER.exception("Claim flow failed")

    @ui.button(label="Confirm Purchase", style=discord.ButtonStyle.green, custom_id="claim:confirm")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message(
                "This listing is no longer active.", ephemeral=True
            )
            return

        await interaction.response.defer()

        buyer = interaction.user
        seller = self.listing_data["seller"]
        self.listing_data["status"] = "claimed"
        self.listing_data["buyer_id"] = buyer.id
        self.listing_data["buyer"] = buyer
        self.listing_data["claim_price"] = self.listing_data["price"]
        self.listing_data["claims"] = [{
            "user_id": buyer.id,
            "claimer_id": buyer.id,
            "username": getattr(buyer, "display_name", None) or buyer.name,
            "amount": self.listing_data["price"],
            "status": "claimed",
        }]
        self.db.upsert_marketplace_listing(self.listing_data)

        # Do not record sale yet
        # Edit the message view to ClaimedListingView
        embed = build_listing_embed(self.listing_data, claimed=True)
        view = ClaimedListingView(self.bot, self.db, self.listing_data)
        await self.bot.edit_listing_messages(self.listing_data, embed=embed, view=view)

        deal_thread = await _send_deal_listing_dms(
            self.bot,
            self.db,
            self.listing_data,
            seller,
            buyer,
            context="claim",
        )

        if deal_thread is not None:
            await _send_deal_thread_update(
                self.bot,
                self.db,
                self.listing_data,
                deal_thread,
                title="Card Claimed",
                description=(
                    f"Claimed at ${self.listing_data['price']:.2f}.\n\n"
                    f"{_deal_participants_text(seller, buyer)}\n\n"
                    f"{_seller_trust_text(self.db, self.listing_data.get('seller_id'))}\n"
                    f"{_buyer_trust_text(self.db, buyer.id)}\n\n"
                    "Coordinate payment and transfer here. The seller can record the transaction after payment is complete. Only the buyer can cancel a claim."
                ),
                color=discord.Color.green(),
                context="claim",
                view=ClaimedListingView(self.bot, self.db, self.listing_data),
            )
            message = "✅ Claim confirmed. Private deal thread created."
        else:
            dm_sent = await self.bot.safe_dm_user(
                seller,
                embed=discord.Embed(
                    title="Your Card Has Been Claimed!",
                    description=(
                        f"{buyer.name} has claimed your "
                        f"{self.listing_data['player_names']} "
                        f"for ${self.listing_data['price']:.2f}."
                        " Please wait for payment before recording the transaction.\n\n"
                        f"{_buyer_trust_text(self.db, buyer.id)}\n\n"
                        f"{_buyer_contact_text(buyer)}"
                    ),
                    color=discord.Color.gold(),
                ),
            )

            message = "✅ Claim confirmed. The seller has been notified."
            if not dm_sent:
                message = "✅ Claim confirmed, but I could not DM the seller."

            buyer_dm_sent = await self.bot.safe_dm_user(
                buyer,
                embed=discord.Embed(
                    title="Claim Sent to Seller",
                    description=(
                        f"You claimed {self.listing_data['player_names']} "
                        f"for ${self.listing_data['price']:.2f}.\n\n"
                        f"{_seller_trust_text(self.db, self.listing_data.get('seller_id'))}\n\n"
                        f"{_seller_contact_text(seller)}"
                    ),
                    color=discord.Color.green(),
                ),
            )
            if not buyer_dm_sent:
                await _notify_user_on_listing(
                    self.bot,
                    self.db,
                    self.listing_data,
                    buyer,
                    (
                        f"I could not DM you, but your claim for "
                        f"{self.listing_data['player_names']} at "
                        f"${self.listing_data['price']:.2f} was sent to the seller. "
                        f"Please coordinate with the seller here."
                    ),
                    context="claim_buyer",
                )
                log_marketplace_event(
                    self.db,
                    "dm_failed",
                    user_id=buyer.id,
                    listing_id=_listing_id(self.listing_data),
                    details={"context": "claim_buyer"},
                    level=30,
                )

        await interaction.edit_original_response(content=message, embed=None, view=None)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.red, custom_id="claim:cancel")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="Purchase cancelled.", view=None)
        self.stop()


class DisabledClaimedListingActionView(ui.View):
    """Disabled private-thread claim actions after the action is closed."""

    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Record Transaction", style=discord.ButtonStyle.grey, disabled=True, custom_id="claimed:record:closed")
    async def record_transaction_closed(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()

    @ui.button(label="Cancel Claim", style=discord.ButtonStyle.grey, disabled=True, custom_id="claimed:cancel:closed")
    async def cancel_claim_closed(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()


class ClaimedListingView(ui.View):
    """View for claimed listings with Record Transaction and Cancel Claim buttons."""

    def __init__(self, bot: "NBACollectBot", db: CardDatabase, listing_data: Dict[str, Any]):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data

    def interaction_message_is_listing_message(self, interaction: discord.Interaction) -> bool:
        message = getattr(interaction, "message", None)
        if message is None:
            return False

        channel = getattr(message, "channel", None) or getattr(interaction, "channel", None)
        channel_id = getattr(channel, "id", None)
        message_id = getattr(message, "id", None)
        targets = [
            (self.listing_data.get("channel_id"), self.listing_data.get("message_id")),
            (self.listing_data.get("surface_channel_id"), self.listing_data.get("surface_message_id")),
        ]
        if not channel_id or not message_id:
            return False
        for target_channel_id, target_message_id in targets:
            if not target_channel_id or not target_message_id:
                continue
            if int(target_channel_id) == int(channel_id) and int(target_message_id) == int(message_id):
                return True
        return False

    async def close_thread_action_message(self, interaction: discord.Interaction, content: str) -> None:
        if self.interaction_message_is_listing_message(interaction):
            return

        for child in self.children:
            child.disabled = True

        try:
            if interaction.message:
                await interaction.message.edit(content=content, view=DisabledClaimedListingActionView())
            self.stop()
        except (discord.NotFound, discord.HTTPException):
            LOGGER.warning("Could not close claimed listing action message")

    def claim_actions_are_current(self) -> bool:
        return (
            str(self.listing_data.get("status", "")).lower() in {"claimed", "pending"}
            and bool(self.listing_data.get("buyer_id"))
        )

    async def reject_if_claim_actions_closed(self, interaction: discord.Interaction) -> bool:
        if self.claim_actions_are_current():
            return False

        await self.close_thread_action_message(
            interaction,
            "These claim actions are no longer active.",
        )
        await interaction.response.send_message(
            "These claim actions are no longer active.",
            ephemeral=True,
        )
        return True

    @ui.button(label="Record Transaction", style=discord.ButtonStyle.green, custom_id="claimed:record")
    async def record_transaction(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_claim_actions_closed(interaction):
            return
        if interaction.user.id != self.listing_data["seller_id"]:
            await interaction.response.send_message("Only the seller can record the transaction.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Record the sale
        record_date = format_sheet_datetime()
        sale_price = _claim_price(self.listing_data)
        if self.bot.sheet is not None:
            self.bot.sheet.add_sale(
                player_names=self.listing_data["player_names"],
                set_name=self.listing_data["set_name"],
                subset=self.listing_data["subset"],
                date_time=record_date,
                price=sale_price,
                card_count=self.listing_data["card_count"],
                seller_id=self.listing_data["seller_id"],
                card_rarity=self.listing_data.get("card_rarity") or "Legendary",
            )
        else:
            self.db.add_sale(
                player_names=self.listing_data["player_names"],
                set_name=self.listing_data["set_name"],
                subset=self.listing_data["subset"],
                date_time=record_date,
                price=sale_price,
                card_count=self.listing_data["card_count"],
                seller_id=self.listing_data["seller_id"],
            )

        self.listing_data["status"] = "sold"
        self.bot.active_listings.pop(self.listing_data.get("message_id"), None)
        self.db.update_marketplace_listing_status(
            self.listing_data["message_id"],
            "sold",
        )
        await self.bot.mark_listing_message_sold(self.listing_data)

        if hasattr(self.bot, "close_deal_thread_action_messages"):
            await self.bot.close_deal_thread_action_messages(
                self.listing_data,
                content="Transaction recorded. These claim actions are closed.",
            )
        await self.close_thread_action_message(
            interaction,
            "Transaction recorded. These claim actions are closed.",
        )
        await interaction.followup.send("Transaction recorded.", ephemeral=True)
        if hasattr(self.bot, "delete_deal_thread"):
            await self.bot.delete_deal_thread(
                self.listing_data,
                reason="Marketplace transaction recorded",
            )

    @ui.button(label="Cancel Claim", style=discord.ButtonStyle.red, custom_id="claimed:cancel")
    async def cancel_claim(self, interaction: discord.Interaction, button: ui.Button):
        if await self.reject_if_claim_actions_closed(interaction):
            return
        if interaction.user.id != self.listing_data.get("buyer_id"):
            await interaction.response.send_message("Only the buyer can cancel the claim.", ephemeral=True)
            return
        if self.listing_data.get("listing_type") == "auction":
            await interaction.response.send_message(
                "Auction wins cannot be cancelled. Please coordinate payment with the seller.",
                ephemeral=True,
            )
            return

        # Notify mods
        mod_channel_id = self.bot.config.get("mod_channel_id")
        if mod_channel_id:
            mod_channel = self.bot.get_channel(mod_channel_id)
            if mod_channel:
                await mod_channel.send(f"Claim cancelled by {interaction.user.name} for listing: {self.listing_data['player_names']} - {self.listing_data['set_name']}")

        self.listing_data["status"] = "active"
        self.listing_data.pop("buyer_id", None)
        self.listing_data.pop("buyer", None)
        self.listing_data.pop("claim_price", None)
        self.listing_data.pop("claims", None)
        await self.bot.ensure_listing_image_url(self.listing_data)
        self.db.upsert_marketplace_listing(self.listing_data)

        # Update the message view back to the correct active-listing controls.
        embed = build_listing_embed(self.listing_data)
        view_cls = AuctionActionView if self.listing_data.get("listing_type") == "auction" else ListingActionView
        view = view_cls(self.bot, self.db, self.listing_data)
        await self.bot.edit_listing_messages(self.listing_data, embed=embed, view=view)

        if hasattr(self.bot, "close_deal_thread_action_messages"):
            await self.bot.close_deal_thread_action_messages(
                self.listing_data,
                content="Claim cancelled. These claim actions are closed.",
            )
        await self.close_thread_action_message(
            interaction,
            "Claim cancelled. These claim actions are closed.",
        )
        await interaction.response.send_message("Claim cancelled. Mods have been notified.", ephemeral=True)
        if hasattr(self.bot, "delete_deal_thread"):
            await self.bot.delete_deal_thread(
                self.listing_data,
                reason="Marketplace claim cancelled",
            )


class CounterOfferView(ui.View):
    """View for a buyer to accept or decline a seller counter offer."""

    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        listing_data: Dict[str, Any],
        bidder: discord.User,
        bid_record: Dict[str, Any],
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self.bidder = bidder
        self.bid_record = bid_record

    async def close_counter_message(self, interaction: discord.Interaction, content: str) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if interaction.response.is_done():
                await interaction.message.edit(content=content, view=self)
            else:
                await interaction.response.edit_message(content=content, view=self)
        except (discord.NotFound, discord.HTTPException):
            if interaction.message:
                try:
                    await interaction.message.edit(content=content, view=self)
                except discord.DiscordException:
                    LOGGER.warning("Could not close counter offer message")

    def refresh_bid_record(self) -> None:
        found = _find_bid_by_id(self.listing_data, self.bid_record.get("id"))
        if found is not None:
            self.bid_record = found

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log_marketplace_event(
            self.db,
            "counter_offer_flow_exception",
            user_id=getattr(interaction.user, "id", None),
            listing_id=_listing_id(self.listing_data),
            details=str(error),
            level=40,
        )
        LOGGER.exception("Counter offer decision failed")

    @ui.button(label="Accept Counter", style=discord.ButtonStyle.green, custom_id="counter:accept")
    async def accept_counter(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.bidder.id:
            await interaction.response.send_message("Only the buyer can accept this counter offer.", ephemeral=True)
            return

        self.refresh_bid_record()
        if self.listing_data.get("status") != "active":
            await self.close_counter_message(interaction, "This listing is no longer active.")
            self.stop()
            return
        if str(self.bid_record.get("status", "")).lower() != "countered" or str(self.bid_record.get("counter_status", "")).lower() != "pending":
            await self.close_counter_message(interaction, "This counter offer is no longer available.")
            self.stop()
            return

        counter_amount = self.bid_record.get("counter_amount")
        if counter_amount is None:
            await self.close_counter_message(interaction, "This counter offer is missing its price.")
            self.stop()
            return

        accepted = await finalize_bid_claim(
            self.bot,
            self.db,
            self.listing_data,
            self.bid_record,
            self.bidder,
            float(counter_amount),
            interaction,
            "counter",
        )
        if not accepted:
            await self.close_counter_message(interaction, "This counter offer is no longer available.")
            self.stop()
            return

        await self.close_counter_message(interaction, "Counter accepted. The listing is now claimed.")
        self.stop()

    @ui.button(label="Decline Counter", style=discord.ButtonStyle.red, custom_id="counter:decline")
    async def decline_counter(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.bidder.id:
            await interaction.response.send_message("Only the buyer can decline this counter offer.", ephemeral=True)
            return

        self.refresh_bid_record()
        if self.listing_data.get("status") != "active":
            await self.close_counter_message(interaction, "This listing is no longer active.")
            self.stop()
            return
        if str(self.bid_record.get("status", "")).lower() != "countered" or str(self.bid_record.get("counter_status", "")).lower() != "pending":
            await self.close_counter_message(interaction, "This counter offer is no longer available.")
            self.stop()
            return

        self.bid_record["status"] = "declined"
        self.bid_record["counter_status"] = "declined"
        self.bid_record["counter_updated_at"] = format_sheet_datetime()
        if self.bid_record.get("id"):
            self.db.update_marketplace_bid(
                self.bid_record["id"],
                status="declined",
                counter_status="declined",
                counter_updated_at=self.bid_record["counter_updated_at"],
            )

        await self.bot.safe_dm_user(
            self.listing_data["seller"],
            embed=discord.Embed(
                title="Counter Offer Declined",
                description=(
                    f"{self.bidder.name} declined your ${float(self.bid_record['counter_amount']):.2f} "
                    f"counter.\n\n{_card_details_block(self.listing_data)}"
                ),
                color=discord.Color.red(),
            ),
        )
        log_marketplace_event(
            self.db,
            "counter_offer_declined",
            user_id=interaction.user.id,
            listing_id=_listing_id(self.listing_data),
            details={
                "bidder_id": self.bidder.id,
                "original_bid": self.bid_record.get("amount") or self.bid_record.get("bid_amount"),
                "counter_amount": self.bid_record.get("counter_amount"),
            },
        )

        await self.close_counter_message(interaction, "Counter declined.")
        self.stop()


class CounterOfferModal(ui.Modal, title="Counter Offer"):
    counter_price = ui.TextInput(label="Counter Price", placeholder="e.g., 15.00")

    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        listing_data: Dict[str, Any],
        bidder: discord.User,
        bid_record: Dict[str, Any],
        seller_message: discord.Message = None,
    ):
        super().__init__()
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self.bidder = bidder
        self.bid_record = bid_record
        self.seller_message = seller_message

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.listing_data["seller_id"]:
            await interaction.response.send_message("Only the seller can counter this offer.", ephemeral=True)
            return
        if self.listing_data.get("status") != "active":
            await interaction.response.send_message("This listing is no longer active.", ephemeral=True)
            return
        if not _bid_is_open(self.bid_record) or str(self.bid_record.get("status", "placed")).lower() == "countered":
            await interaction.response.send_message("This offer is no longer available for a counter offer.", ephemeral=True)
            return

        try:
            counter_amount = parse_required_float(self.counter_price.value, field_name="Counter Price")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        original_bid = float(self.bid_record.get("amount") or self.bid_record.get("bid_amount"))
        asking_price = float(self.listing_data["price"])
        if counter_amount <= original_bid or counter_amount >= asking_price:
            await interaction.response.send_message(
                f"Counter price must be higher than the ${original_bid:.2f} offer and lower than the ${asking_price:.2f} asking price.",
                ephemeral=True,
            )
            return

        now = format_sheet_datetime()
        self.bid_record["status"] = "countered"
        self.bid_record["counter_amount"] = counter_amount
        self.bid_record["counter_status"] = "pending"
        self.bid_record["counter_created_at"] = now
        self.bid_record["counter_updated_at"] = now

        counter_view = CounterOfferView(self.bot, self.db, self.listing_data, self.bidder, self.bid_record)
        dm_message = None
        try:
            bidder = await self.bot.hydrate_user(
                self.bidder.id,
                getattr(self.bidder, "name", None),
                fetch=True,
            )
            self.bidder = bidder
            dm_message = await bidder.send(
                embed=discord.Embed(
                    title="Counter Offer Received",
                    description=(
                        f"The seller countered your ${original_bid:.2f} offer at ${counter_amount:.2f}.\n\n"
                        f"{_card_details_block(self.listing_data)}"
                    ),
                    color=discord.Color.gold(),
                ),
                view=counter_view,
            )
        except discord.Forbidden:
            LOGGER.warning("Could not DM counter offer to user %s", getattr(self.bidder, "name", self.bidder))
        except discord.DiscordException as exc:
            LOGGER.warning("Could not send counter offer DM: %s", exc)
        except AttributeError:
            LOGGER.warning("Bidder object cannot receive counter offer DMs for listing %s", _listing_id(self.listing_data))

        if dm_message:
            self.bid_record["counter_dm_channel_id"] = dm_message.channel.id
            self.bid_record["counter_dm_message_id"] = dm_message.id

        if self.bid_record.get("id"):
            self.db.update_marketplace_bid(
                self.bid_record["id"],
                status="countered",
                counter_amount=counter_amount,
                counter_status="pending",
                counter_dm_channel_id=self.bid_record.get("counter_dm_channel_id"),
                counter_dm_message_id=self.bid_record.get("counter_dm_message_id"),
                counter_created_at=now,
                counter_updated_at=now,
            )

        log_marketplace_event(
            self.db,
            "counter_offer_sent",
            user_id=interaction.user.id,
            listing_id=_listing_id(self.listing_data),
            details={
                "bidder_id": self.bidder.id,
                "original_bid": original_bid,
                "counter_amount": counter_amount,
            },
        )

        message = f"Counter offer sent at ${counter_amount:.2f}."
        if not dm_message:
            log_marketplace_event(
                self.db,
                "dm_failed",
                user_id=self.bidder.id,
                listing_id=_listing_id(self.listing_data),
                details={"context": "counter_offer"},
                level=30,
            )
            message = "Counter saved, but I could not DM the buyer."

        await interaction.response.send_message(message, ephemeral=True)
        if self.seller_message:
            try:
                await self.seller_message.edit(content=message, view=None)
            except discord.DiscordException:
                LOGGER.warning("Could not close seller offer DM after counter offer")


class AcceptBidView(ui.View):
    """View for seller to accept, counter, or decline an offer."""

    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        listing_data: Dict[str, Any],
        bidder: discord.User,
        bid_price: float,
        bid_record: Dict[str, Any] = None,
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.listing_data = listing_data
        self.bidder = bidder
        self.bid_price = bid_price
        self.bid_record = bid_record

    async def close_bid_message(self, interaction: discord.Interaction, content: str) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if interaction.response.is_done():
                await interaction.message.edit(content=content, view=self)
            else:
                await interaction.response.edit_message(content=content, view=self)
        except (discord.NotFound, discord.HTTPException):
            if interaction.message:
                try:
                    await interaction.message.edit(content=content, view=self)
                except discord.DiscordException:
                    LOGGER.warning("Could not close seller offer message")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log_marketplace_event(
            self.db,
            "offer_flow_exception",
            user_id=getattr(interaction.user, "id", None),
            listing_id=_listing_id(self.listing_data),
            details=str(error),
            level=40,
        )
        LOGGER.exception("Offer decision failed")

    @ui.button(label="Accept Offer", style=discord.ButtonStyle.green, custom_id="bid:accept")
    async def accept_bid(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.listing_data["seller_id"]:
            await interaction.response.send_message("Only the seller can accept offers.", ephemeral=True)
            return
        if self.listing_data.get("status") != "active":
            await self.close_bid_message(interaction, "This listing is no longer active.")
            self.stop()
            return
        if not _bid_is_open(self.bid_record) or str(self.bid_record.get("status", "placed")).lower() == "countered":
            await self.close_bid_message(interaction, "This offer is no longer available.")
            self.stop()
            return

        await interaction.response.defer()
        accepted = await finalize_bid_claim(
            self.bot,
            self.db,
            self.listing_data,
            self.bid_record,
            self.bidder,
            self.bid_price,
            interaction,
            "offer",
        )
        if not accepted:
            await self.close_bid_message(interaction, "This offer is no longer available.")
            self.stop()
            return

        await self.close_bid_message(interaction, "Offer accepted. The listing is now claimed.")
        self.stop()

    @ui.button(label="Counter Offer", style=discord.ButtonStyle.blurple, custom_id="bid:counter")
    async def counter_offer(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.listing_data["seller_id"]:
            await interaction.response.send_message("Only the seller can counter offers.", ephemeral=True)
            return
        if self.listing_data.get("status") != "active":
            await self.close_bid_message(interaction, "This listing is no longer active.")
            self.stop()
            return
        if not _bid_is_open(self.bid_record) or str(self.bid_record.get("status", "placed")).lower() == "countered":
            await self.close_bid_message(interaction, "This offer is no longer available for a counter offer.")
            self.stop()
            return

        await interaction.response.send_modal(
            CounterOfferModal(
                self.bot,
                self.db,
                self.listing_data,
                self.bidder,
                self.bid_record,
                interaction.message,
            )
        )

    @ui.button(label="Decline Offer", style=discord.ButtonStyle.red, custom_id="bid:decline")
    async def decline_bid(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.listing_data["seller_id"]:
            await interaction.response.send_message("Only the seller can decline offers.", ephemeral=True)
            return
        if self.listing_data.get("status") != "active":
            await self.close_bid_message(interaction, "This listing is no longer active.")
            self.stop()
            return
        if not _bid_is_open(self.bid_record):
            await self.close_bid_message(interaction, "This offer is no longer available.")
            self.stop()
            return

        await interaction.response.defer()
        # DM the user that their offer was declined.
        if self.bid_record is not None:
            self.bid_record["status"] = "declined"
            if str(self.bid_record.get("counter_status", "")).lower() == "pending":
                self.bid_record["counter_status"] = "declined"
                self.bid_record["counter_updated_at"] = format_sheet_datetime()
            if self.bid_record.get("id"):
                update_fields = {"status": "declined"}
                if self.bid_record.get("counter_status") == "declined":
                    update_fields["counter_status"] = "declined"
                    update_fields["counter_updated_at"] = self.bid_record.get("counter_updated_at")
                self.db.update_marketplace_bid(self.bid_record["id"], **update_fields)
        bidder_dm_sent = await self.bot.safe_dm_user(
            self.bidder,
            embed=discord.Embed(
                title="Your Offer Was Declined",
                description=(
                    f"Your offer of ${self.bid_price:.2f} was not accepted.\n\n"
                    f"{_card_details_block(self.listing_data)}"
                ),
                color=discord.Color.red(),
            )
        )
        if not bidder_dm_sent:
            log_marketplace_event(
                self.db,
                "dm_failed",
                user_id=self.bidder.id,
                listing_id=_listing_id(self.listing_data),
                details={"context": "offer_declined"},
                level=30,
            )
        log_marketplace_event(
            self.db,
            "offer_declined",
            user_id=interaction.user.id,
            listing_id=_listing_id(self.listing_data),
            details={"bidder_id": self.bidder.id, "amount": self.bid_price},
        )

        await self.close_bid_message(interaction, "Offer declined.")
        self.stop()
