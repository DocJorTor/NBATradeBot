import discord

import io
import asyncio
import time

from datetime import datetime, timedelta, timezone
from discord import ui

from actions import collect_listing_image, publish_listing
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bot import NBACollectBot

from components import (
    PlayerNamePromptModal,
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
from config import format_subset_for_set, has_subset_variants
from database import CardDatabase
from logger import LOGGER, log_marketplace_event
from ocr import extract_listing_metadata
from parsers import format_sheet_datetime, normalize_player_name
from price_assist import (
    PriceResultsView as SharedPriceResultsView,
    build_price_assist,
    query_price_source,
)
from serializers import build_listing_embed, format_card_count
from sheets import PriceSheet
from views import ClaimedListingView, ListingActionView, MarketplaceCarouselView
ANY_VALUE = "ANY"
BUTTON_PAD = "\u00a0"


def _extract_listing_metadata_for_review(image_bytes: bytes | None, known_players: list[str]):
    metadata_guess = extract_listing_metadata(
        image_bytes,
        known_players=known_players,
    )
    return metadata_guess, image_bytes, False


def _format_price(value) -> str:
    if value in (None, ""):
        return "N/A"
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _filter_or_none(value):
    return None if value in (None, "", ANY_VALUE) else value


def _get_active_listings_for_user(bot: "NBACollectBot", user_id: int) -> list[dict]:
    listings = getattr(bot, "active_listings", {}) or {}
    return [
        listing
        for listing in listings.values()
        if listing.get("seller_id") == user_id
        and str(listing.get("status", "active")).lower() in {"active", "open", "claimed", "pending"}
    ]


def _get_active_sale_listings_for_user(bot: "NBACollectBot", user_id: int) -> list[dict]:
    listings = [
        listing
        for listing in (getattr(bot, "active_listings", {}) or {}).values()
        if listing.get("seller_id") == user_id
        and str(listing.get("status", "active")).lower() in {"active", "open"}
        and str(listing.get("listing_type", "sale")).lower() != "auction"
    ]
    listings.sort(
        key=lambda listing: (
            str(listing.get("date_time") or ""),
            int(listing.get("message_id") or 0),
        ),
        reverse=True,
    )
    return listings


def _get_claimed_sale_listings_for_user(bot: "NBACollectBot", user_id: int) -> list[dict]:
    listings = [
        listing
        for listing in (getattr(bot, "active_listings", {}) or {}).values()
        if listing.get("seller_id") == user_id
        and str(listing.get("status", "active")).lower() in {"claimed", "pending"}
        and str(listing.get("listing_type", "sale")).lower() != "auction"
    ]
    listings.sort(
        key=lambda listing: (
            str(listing.get("updated_at") or listing.get("date_time") or ""),
            int(listing.get("message_id") or 0),
        ),
        reverse=True,
    )
    return listings


def _get_known_player_names(db: CardDatabase, limit: int = 5000) -> list[str]:
    try:
        rows = db.conn.execute(
            """
            SELECT player_names, COUNT(*) AS row_count
            FROM card_sales
            WHERE TRIM(player_names) != ''
            GROUP BY player_names
            ORDER BY row_count DESC, player_names
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        LOGGER.exception("Could not load known player names for OCR")
        return []
    return [row["player_names"] for row in rows]


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _get_active_marketplace_listings(bot: "NBACollectBot") -> list[dict]:
    listings = [
        listing
        for listing in (getattr(bot, "active_listings", {}) or {}).values()
        if str(listing.get("status", "active")).lower() in {"active", "open"}
    ]
    listings.sort(
        key=lambda listing: (
            str(listing.get("date_time") or ""),
            int(listing.get("message_id") or 0),
        ),
        reverse=True,
    )
    return listings


def _get_active_notify_rules_for_user(bot: "NBACollectBot", user_id: int) -> list[dict]:
    rules = getattr(bot, "notify_rules", []) or []
    return [rule for rule in rules if rule.get("user_id") == user_id]


def _extract_listing_bids(listing: dict) -> list[dict]:
    bids = listing.get("bids") or listing.get("active_bids") or []
    if isinstance(bids, list):
        normalized = []
        for bid in bids:
            if isinstance(bid, dict):
                normalized.append(bid)
        return normalized

    bid_user_id = listing.get("bid_user_id") or listing.get("bidder_id")
    if bid_user_id:
        return [
            {
                "user_id": bid_user_id,
                "bidder_id": bid_user_id,
                "username": listing.get("bid_user") or listing.get("bidder"),
                "amount": listing.get("bid_amount") or listing.get("bid_price"),
                "status": listing.get("bid_status", "placed"),
            }
        ]
    return []


def _extract_listing_claims(listing: dict) -> list[dict]:
    claims = listing.get("claims") or listing.get("active_claims") or []
    if isinstance(claims, list):
        return claims

    claim_user_id = listing.get("claim_user_id") or listing.get("claimed_by_id") or listing.get("claimer_id")
    claim_user_id = claim_user_id or listing.get("buyer_id")
    if claim_user_id:
        return [
            {
                "user_id": claim_user_id,
                "username": listing.get("claim_user") or listing.get("claimed_by") or listing.get("claimer") or getattr(listing.get("buyer"), "name", None),
                "amount": listing.get("claim_price") or listing.get("price"),
            }
        ]
    return []


def _get_active_bids_for_user(bot: "NBACollectBot", user_id: int) -> list[dict]:
    user_bids = []
    listings = getattr(bot, "active_listings", {}) or {}
    for message_id, listing in listings.items():
        if str(listing.get("status", "active")).lower() not in {"active", "open", "claimed", "pending"}:
            continue
        for bid in _extract_listing_bids(listing):
            if str(bid.get("status", "placed")).lower() not in {"placed", "active", "pending", "countered"}:
                continue
            bid_user_id = bid.get("user_id") or bid.get("bidder_id")
            if bid_user_id == user_id:
                user_bids.append({**bid, "message_id": message_id, "listing": listing})
    return user_bids


def _get_active_claims_for_user(bot: "NBACollectBot", user_id: int) -> list[dict]:
    user_claims = []
    listings = getattr(bot, "active_listings", {}) or {}
    for message_id, listing in listings.items():
        if str(listing.get("status", "active")).lower() not in {"active", "open", "claimed", "pending"}:
            continue
        for claim in _extract_listing_claims(listing):
            claim_user_id = claim.get("user_id") or claim.get("claimer_id") or claim.get("claimed_by_id")
            if claim_user_id == user_id:
                user_claims.append({**claim, "message_id": message_id, "listing": listing})
    return user_claims


def _listing_line(listing: dict) -> str:
    listing_type = "Auction" if listing.get("listing_type") == "auction" else "Listing"
    player = listing.get("player_names") or listing.get("player_name") or "Unknown Player"
    set_name = listing.get("set_name") or "Unknown Set"
    subset = listing.get("subset") or "N/A"
    card_count = format_card_count(listing.get("card_count"))
    price = _format_price(listing.get("price"))
    status = str(listing.get("status", "active")).title()
    bids_count = len([
        bid for bid in _extract_listing_bids(listing)
        if str(bid.get("status", "placed")).lower() in {"placed", "active", "pending", "countered"}
    ])
    claims_count = len(_extract_listing_claims(listing))

    activity = []
    if claims_count:
        activity.append(f"{claims_count} claim(s)")
    if bids_count:
        term = "bid(s)" if listing.get("listing_type") == "auction" else "offer(s)"
        activity.append(f"{bids_count} {term}")

    activity_text = f" | {'; '.join(activity)}" if activity else ""
    return (
        f"• **{listing_type}**: **{player}** — {price}\n"
        f"  {set_name} | {subset} | /{card_count}\n"
        f"  Status: `{status}`{activity_text}"
    )


def _bid_or_claim_line(item: dict, label: str) -> str:
    listing = item.get("listing", {})
    player = listing.get("player_names") or listing.get("player_name") or "Unknown Player"
    set_name = listing.get("set_name") or "Unknown Set"
    amount = item.get("amount") or item.get("bid_amount") or item.get("price") or listing.get("price")
    status = str(item.get("status", "")).lower()
    counter_amount = item.get("counter_amount")
    display_label = label
    if label == "Bid" and listing.get("listing_type") != "auction":
        display_label = "Offer"
    if display_label == "Offer" and status == "countered" and counter_amount not in (None, ""):
        return f"• **Offer**: {player} — {_format_price(amount)}; counter pending {_format_price(counter_amount)} ({set_name})"
    return f"• **{display_label}**: {player} — {_format_price(amount)} ({set_name})"


def _notify_rule_line(rule: dict) -> str:
    player = rule.get("player_name") or rule.get("player_names") or "Any Player"
    set_name = rule.get("set_name") or "Any Set"
    subset = rule.get("subset") or "Any Subset"
    card_count = format_card_count(rule.get("card_count"))
    return f"• **{player}** — {set_name} | {subset} | /{card_count}"


def _notify_rule_option_text(rule: dict) -> tuple[str, str]:
    player = rule.get("player_name") or rule.get("player_names") or "Any Player"
    set_name = rule.get("set_name") or "Any Set"
    subset = rule.get("subset") or "Any Subset"
    card_count = format_card_count(rule.get("card_count"))
    label = f"{player} | /{card_count}"
    description = f"{set_name} | {subset}"
    return label[:100], description[:100]


def _notify_rules_match(left: dict, right: dict) -> bool:
    return (
        int(left.get("user_id")) == int(right.get("user_id"))
        and str(left.get("player_name") or "") == str(right.get("player_name") or "")
        and (left.get("set_name") or None) == (right.get("set_name") or None)
        and (left.get("subset") or None) == (right.get("subset") or None)
        and (
            None if left.get("card_count") in ("", ANY_VALUE) else str(left.get("card_count"))
        ) == (
            None if right.get("card_count") in ("", ANY_VALUE) else str(right.get("card_count"))
        )
    )


async def _save_notify_rule_from_filters(
    bot: "NBACollectBot",
    db: CardDatabase,
    interaction: discord.Interaction,
    rule: dict,
    *,
    event_type: str,
    edit_response: bool = False,
) -> None:
    if not any(rule.get(key) not in (None, "", ANY_VALUE) for key in ("player_name", "set_name", "subset", "card_count")):
        message = "Choose at least one filter before saving a notification."
        if edit_response:
            await interaction.response.edit_message(content=message, view=None)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return

    for active_rule in getattr(bot, "notify_rules", []) or []:
        if _notify_rules_match(active_rule, rule):
            message = f"You already have this notification:\n{_notify_rule_line(active_rule)}"
            if edit_response:
                await interaction.response.edit_message(content=message, view=None)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return

    rule = dict(rule)
    rule["id"] = db.add_notify_rule(rule)
    bot.notify_rules.append(rule)
    log_marketplace_event(
        db,
        event_type,
        user_id=interaction.user.id,
        details={
            "player": rule.get("player_name") or None,
            "set_name": rule.get("set_name"),
            "subset": rule.get("subset"),
            "card_count": rule.get("card_count"),
        },
        level=20,
    )
    message = f"Notification saved:\n{_notify_rule_line(rule)}"
    if edit_response:
        await interaction.response.edit_message(content=message, view=None)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _join_or_empty(
    lines: list[str],
    empty_message: str,
    limit: int = 10,
    char_limit: int = 1024,
) -> str:
    if not lines:
        return empty_message
    visible = []
    remaining = 0
    for index, line in enumerate(lines):
        if len(visible) >= limit:
            remaining = len(lines) - index
            break

        candidate = "\n".join([*visible, line])
        summary = f"\n_Showing first {len(visible) + 1} of {len(lines)}._"
        if len(candidate) + (len(summary) if index < len(lines) - 1 else 0) > char_limit:
            remaining = len(lines) - index
            break
        visible.append(line)

    if remaining:
        summary = f"_Showing first {len(visible)} of {len(lines)}._"
        while visible and len("\n".join([*visible, summary])) > char_limit:
            visible.pop()
        visible.append(summary)
    return "\n".join(visible)


async def _delete_ephemeral_message(message) -> bool:
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except discord.NotFound:
        return True
    except discord.HTTPException:
        LOGGER.warning("Could not delete ephemeral workflow message.")
        return False


async def _edit_original_workflow_status(
    interaction: discord.Interaction,
    content: str,
) -> None:
    try:
        await interaction.edit_original_response(content=content, view=None, embed=None)
    except (discord.NotFound, discord.HTTPException):
        LOGGER.warning("Could not update listing workflow status message.")


async def _finish_listing_workflow_message(interaction: discord.Interaction, message) -> None:
    if interaction.response.is_done():
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

    target = getattr(interaction, "message", None) or message
    if await _delete_ephemeral_message(target):
        return

    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(content="Listing posted.", view=None, embed=None)
    except (discord.NotFound, discord.HTTPException):
        LOGGER.warning("Could not clear listing workflow message.")


async def _edit_listing_workflow_error(
    interaction: discord.Interaction,
    view: ui.View,
    message: str,
) -> None:
    summary = getattr(view, "selected_summary", None)
    content = summary() if callable(summary) else "Review listing details"
    content = f"{content}\n\n{message}"
    try:
        await interaction.response.edit_message(content=content, view=view)
    except (discord.NotFound, discord.HTTPException):
        await interaction.response.send_message(message, ephemeral=True)


class StatusListingSelectView(ui.View):
    def __init__(
        self,
        bot: "NBACollectBot",
        db: CardDatabase,
        owner_id: int,
        listings: list[dict],
        *,
        placeholder: str = "Choose one of your active listings",
        allowed_statuses: set[str] | None = None,
        action_view_cls=ListingActionView,
        event_type: str = "status_listing_actions_opened",
        stale_message: str = "That listing is no longer active.",
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.db = db
        self.owner_id = owner_id
        self.allowed_statuses = allowed_statuses or {"active", "open"}
        self.action_view_cls = action_view_cls
        self.event_type = event_type
        self.stale_message = stale_message
        self.listings_by_id = {
            str(listing.get("message_id")): listing
            for listing in listings[:25]
            if listing.get("message_id")
        }
        options = []
        for listing_id, listing in self.listings_by_id.items():
            player = str(listing.get("player_names") or "Unknown Player")
            set_name = str(listing.get("set_name") or "Unknown Set")
            subset = str(listing.get("subset") or "N/A")
            options.append(
                discord.SelectOption(
                    label=f"{player} - {_format_price(listing.get('price'))}"[:100],
                    description=f"{set_name} | {subset}"[:100],
                    value=listing_id,
                )
            )

        self.select = ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return False
        await interaction.response.send_message(
            "Only the status owner can use these listing actions.",
            ephemeral=True,
        )
        return True

    async def on_select(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return

        listing = self.listings_by_id.get(self.select.values[0])
        if not listing or str(listing.get("status", "active")).lower() not in self.allowed_statuses:
            await interaction.response.send_message(
                self.stale_message,
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        log_marketplace_event(
            self.db,
            self.event_type,
            user_id=interaction.user.id,
            listing_id=listing.get("message_id"),
            details={"player_names": listing.get("player_names")},
        )
        kwargs = await self.bot.build_listing_message_kwargs(listing)
        await interaction.followup.send(
            **kwargs,
            view=self.action_view_cls(self.bot, self.db, listing),
            ephemeral=True,
        )


class StatusActionsView(ui.View):
    def __init__(self, bot: "NBACollectBot", db: CardDatabase, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.owner_id = owner_id

    async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return False
        await interaction.response.send_message(
            "Only the status owner can use these listing actions.",
            ephemeral=True,
        )
        return True

    async def _send_listing_actions(
        self,
        interaction: discord.Interaction,
        listing: dict,
        *,
        action_view_cls=ListingActionView,
        event_type: str = "status_listing_actions_opened",
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log_marketplace_event(
            self.db,
            event_type,
            user_id=interaction.user.id,
            listing_id=listing.get("message_id"),
            details={"player_names": listing.get("player_names")},
        )
        kwargs = await self.bot.build_listing_message_kwargs(listing)
        await interaction.followup.send(
            **kwargs,
            view=action_view_cls(self.bot, self.db, listing),
            ephemeral=True,
        )

    @ui.button(label="📝 Listing Actions", style=discord.ButtonStyle.blurple, custom_id="status:listing_actions")
    async def listing_actions_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self._reject_wrong_user(interaction):
            return

        listings = _get_active_sale_listings_for_user(self.bot, self.owner_id)
        if not listings:
            await interaction.response.send_message(
                "You do not have any active fixed-price listings right now.",
                ephemeral=True,
            )
            return

        if len(listings) == 1:
            await self._send_listing_actions(interaction, listings[0])
            return

        extra = ""
        if len(listings) > 25:
            extra = "\nShowing the first 25 active listings."
        await interaction.response.send_message(
            f"Choose a listing to manage.{extra}",
            view=StatusListingSelectView(self.bot, self.db, self.owner_id, listings),
            ephemeral=True,
        )

    @ui.button(label="✅ Claimed Listing Actions", style=discord.ButtonStyle.green, custom_id="status:claimed_listing_actions")
    async def claimed_listing_actions_button(self, interaction: discord.Interaction, button: ui.Button):
        if await self._reject_wrong_user(interaction):
            return

        listings = _get_claimed_sale_listings_for_user(self.bot, self.owner_id)
        if not listings:
            await interaction.response.send_message(
                "You do not have any claimed fixed-price listings right now.",
                ephemeral=True,
            )
            return

        if len(listings) == 1:
            await self._send_listing_actions(
                interaction,
                listings[0],
                action_view_cls=ClaimedListingView,
                event_type="status_claimed_listing_actions_opened",
            )
            return

        extra = ""
        if len(listings) > 25:
            extra = "\nShowing the first 25 claimed listings."
        await interaction.response.send_message(
            f"Choose a claimed listing to manage.{extra}",
            view=StatusListingSelectView(
                self.bot,
                self.db,
                self.owner_id,
                listings,
                placeholder="Choose one of your claimed listings",
                allowed_statuses={"claimed", "pending"},
                action_view_cls=ClaimedListingView,
                event_type="status_claimed_listing_actions_opened",
                stale_message="That listing is no longer claimed.",
            ),
            ephemeral=True,
        )

def register_bot_interface(bot: "NBACollectBot", sheet: PriceSheet, db: CardDatabase) -> None:
    async def help_command(interaction: discord.Interaction):

        embed = discord.Embed(
            title="🏀 NBA Trade Bot Help",
            description=(
                "Marketplace and pricing tools for NBA digital card trading.\n\n"
                "`🔍 Price Search`\n"
                "Search recent card sales. You can leave the player blank, then filter by set, subset, and card count.\n\n"
                "`🔔 Notifications`\n"
                "View your active alerts, then use `🔔 Add Notification` or `🔕 Remove Notification`.\n\n"
                "`🏷️ List Player`\n"
                "Create a fixed-price card listing with image upload.\n\n"
                "`🔨 Auction Player`\n"
                "Create a timed auction with image upload.\n\n"
                "`🛒 Marketplace`\n"
                "Browse active marketplace listings in one carousel. Claim listings, make offers, bid on auctions, and record transactions.\n\n"
                "`📊 View Status`\n"
                "View your active listings, offers, auction bids, claims, and notifications. This response is private.\n\n"
                "`💬 Leave Feedback`\n"
                "Report missing sets, subsets, players, bugs, or general feedback."
            ),
            color=discord.Color.orange(),
        )

        embed.set_footer(text="Built to help!")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def feedback(interaction: discord.Interaction):
        categories = {
            "missing_set": {
                "label": "Missing Set",
                "modal_title": "Report Missing Set",
                "subject_label": "Set name",
                "subject_placeholder": "2026 Playoffs Contest",
            },
            "missing_subset": {
                "label": "Missing Subset",
                "modal_title": "Report Missing Subset",
                "subject_label": "Set and subset",
                "subject_placeholder": "2026 Topps Midnight - Black Light Base",
            },
            "missing_player": {
                "label": "Missing Player",
                "modal_title": "Report Missing Player",
                "subject_label": "Player name",
                "subject_placeholder": "Player name as it should appear",
            },
            "bug": {
                "label": "Bug Report",
                "modal_title": "Report Bug",
                "subject_label": "What broke?",
                "subject_placeholder": "Price lookup returned no results",
            },
            "general": {
                "label": "General Feedback",
                "modal_title": "Leave Feedback",
                "subject_label": "Short summary",
                "subject_placeholder": "What would make the bot better?",
            },
        }

        class FeedbackModal(ui.Modal):
            def __init__(self, category_key: str):
                category = categories[category_key]
                super().__init__(title=category["modal_title"])
                self.category_key = category_key
                self.subject = ui.TextInput(
                    label=category["subject_label"],
                    placeholder=category["subject_placeholder"],
                    required=True,
                    max_length=120,
                )
                self.details = ui.TextInput(
                    label="Details",
                    placeholder="Add any context that would help us fix or evaluate this.",
                    style=discord.TextStyle.paragraph,
                    required=True,
                    max_length=1500,
                )
                self.add_item(self.subject)
                self.add_item(self.details)

            async def on_submit(self, modal_interaction: discord.Interaction):
                category = categories[self.category_key]
                subject = str(self.subject.value or "").strip()
                details = str(self.details.value or "").strip()
                log_marketplace_event(
                    db,
                    "user_feedback_submitted",
                    user_id=modal_interaction.user.id,
                    details={
                        "category": self.category_key,
                        "subject": subject,
                        "details": details,
                        "channel_id": modal_interaction.channel_id,
                    },
                )

                embed = discord.Embed(
                    title=f"Feedback: {category['label']}",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="From", value=f"{modal_interaction.user} (`{modal_interaction.user.id}`)", inline=False)
                embed.add_field(name=category["subject_label"], value=subject[:1024], inline=False)
                embed.add_field(name="Details", value=details[:1024], inline=False)
                if modal_interaction.channel:
                    embed.add_field(name="Channel", value=modal_interaction.channel.mention, inline=True)

                mod_channel_id = bot.config.get("mod_channel_id")
                mod_channel = await bot.fetch_channel_safely(mod_channel_id) if mod_channel_id else None
                if mod_channel:
                    try:
                        await mod_channel.send(embed=embed)
                    except discord.DiscordException:
                        LOGGER.exception("Could not send feedback to mod channel")

                await modal_interaction.response.send_message(
                    "Thanks, your feedback was submitted.",
                    ephemeral=True,
                )

        class FeedbackView(ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.select = ui.Select(
                    placeholder="Choose feedback type",
                    min_values=1,
                    max_values=1,
                    options=[
                        discord.SelectOption(label=category["label"], value=key)
                        for key, category in categories.items()
                    ],
                )
                self.select.callback = self.on_select
                self.add_item(self.select)

            async def on_select(self, select_interaction: discord.Interaction):
                await select_interaction.response.send_modal(
                    FeedbackModal(self.select.values[0])
                )

        await interaction.response.send_message(
            "What would you like to report?",
            view=FeedbackView(),
            ephemeral=True,
        )

    async def remove_notify(interaction: discord.Interaction):
        user_id = interaction.user.id
        notify_rules = [
            rule
            for rule in _get_active_notify_rules_for_user(bot, user_id)
            if rule.get("id") is not None
        ]

        if not notify_rules:
            await interaction.response.send_message(
                "You do not have any active notifications to remove.",
                ephemeral=True,
            )
            return

        class RemoveNotifyView(ui.View):
            def __init__(self, rules: list[dict]):
                super().__init__(timeout=120)
                self.rules_by_id = {str(rule["id"]): rule for rule in rules[:25]}
                self.pending_rule_id = None
                options = []
                for rule_id, rule in self.rules_by_id.items():
                    label, description = _notify_rule_option_text(rule)
                    options.append(
                        discord.SelectOption(
                            label=label,
                            description=description,
                            value=rule_id,
                        )
                    )

                self.select = ui.Select(
                    placeholder="Choose a notification to remove",
                    min_values=1,
                    max_values=1,
                    options=options,
                )
                self.select.callback = self.on_select
                self.add_item(self.select)

                self.confirm_button = ui.Button(
                    label="🗑️ Confirm Remove",
                    style=discord.ButtonStyle.danger,
                    row=1,
                    disabled=True,
                )
                self.confirm_button.callback = self.on_confirm
                self.add_item(self.confirm_button)

                self.cancel_button = ui.Button(
                    label="✖️ Cancel",
                    style=discord.ButtonStyle.secondary,
                    row=1,
                )
                self.cancel_button.callback = self.on_cancel
                self.add_item(self.cancel_button)

            async def _reject_wrong_user(self, select_interaction: discord.Interaction) -> bool:
                if select_interaction.user.id == user_id:
                    return False
                await select_interaction.response.send_message(
                    "Only the notification owner can use this menu.",
                    ephemeral=True,
                )
                return True

            async def on_select(self, select_interaction: discord.Interaction):
                if await self._reject_wrong_user(select_interaction):
                    return

                rule_id = self.select.values[0]
                rule = self.rules_by_id.get(rule_id)
                if not rule:
                    await select_interaction.response.send_message(
                        "That notification could not be found.",
                        ephemeral=True,
                    )
                    return

                self.pending_rule_id = rule_id
                label, _ = _notify_rule_option_text(rule)
                self.select.placeholder = label
                for option in self.select.options:
                    option.default = option.value == rule_id
                self.confirm_button.disabled = False
                await select_interaction.response.edit_message(
                    content=f"Confirm removal of this notification:\n{_notify_rule_line(rule)}",
                    view=self,
                )

            async def on_confirm(self, confirm_interaction: discord.Interaction):
                if await self._reject_wrong_user(confirm_interaction):
                    return

                rule_id = self.pending_rule_id
                rule = self.rules_by_id.get(rule_id)
                if not rule_id or not rule:
                    await confirm_interaction.response.send_message(
                        "Choose a notification first.",
                        ephemeral=True,
                    )
                    return

                removed = db.deactivate_notify_rule(int(rule_id), user_id)
                if not removed:
                    await confirm_interaction.response.edit_message(
                        content="That notification was already removed.",
                        view=None,
                    )
                    self.stop()
                    return

                bot.notify_rules = [
                    active_rule
                    for active_rule in (getattr(bot, "notify_rules", []) or [])
                    if str(active_rule.get("id")) != rule_id
                ]
                log_marketplace_event(
                    db,
                    "notification_removed",
                    user_id=user_id,
                    details={
                        "rule_id": int(rule_id),
                        "player": rule.get("player_name") or None,
                        "set_name": rule.get("set_name"),
                        "subset": rule.get("subset"),
                        "card_count": rule.get("card_count"),
                    },
                    level=20,
                )

                await confirm_interaction.response.edit_message(
                    content=f"Removed notification:\n{_notify_rule_line(rule)}",
                    view=None,
                )
                self.stop()

            async def on_cancel(self, cancel_interaction: discord.Interaction):
                if await self._reject_wrong_user(cancel_interaction):
                    return
                await cancel_interaction.response.edit_message(
                    content="Notification removal cancelled.",
                    view=None,
                )
                self.stop()

        extra = ""
        if len(notify_rules) > 25:
            extra = "\nShowing the first 25 active notifications."

        await interaction.response.send_message(
            f"Choose a notification to remove.{extra}",
            view=RemoveNotifyView(notify_rules),
            ephemeral=True,
        )

    async def status(interaction: discord.Interaction):
        user_id = interaction.user.id

        listings = _get_active_listings_for_user(bot, user_id)
        sale_listings = _get_active_sale_listings_for_user(bot, user_id)
        claimed_sale_listings = _get_claimed_sale_listings_for_user(bot, user_id)
        bids = _get_active_bids_for_user(bot, user_id)
        claims = _get_active_claims_for_user(bot, user_id)
        notify_rules = _get_active_notify_rules_for_user(bot, user_id)
        trust_stats = db.get_marketplace_user_stats(user_id)

        embed = discord.Embed(
            title="📊 Your Marketplace Status",
            description="Active marketplace items tied to your Discord account.",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="🤝 Marketplace Trust",
            value=(
                f"Listings sold: **{trust_stats['listings_sold']}**\n"
                f"Listings bought: **{trust_stats['listings_bought']}**"
            ),
            inline=False,
        )

        embed.add_field(
            name=f"📝 Active Listings / Auctions ({len(listings)})",
            value=_join_or_empty(
                [_listing_line(listing) for listing in listings],
                "No active listings or auctions.",
            ),
            inline=False,
        )

        bid_claim_lines = [
            *[_bid_or_claim_line(claim, "Claim") for claim in claims],
            *[_bid_or_claim_line(bid, "Bid") for bid in bids],
        ]
        embed.add_field(
            name=f"💵 Active Offers / Bids / Claims ({len(bids) + len(claims)})",
            value=_join_or_empty(
                bid_claim_lines,
                "No active offers, bids, or claims.",
            ),
            inline=False,
        )

        embed.add_field(
            name=f"🔔 Active Notifications ({len(notify_rules)})",
            value=_join_or_empty(
                [_notify_rule_line(rule) for rule in notify_rules],
                "No active notifications.",
            ),
            inline=False,
        )

        embed.set_footer(text="Only you can see this status message.")

        if sale_listings or claimed_sale_listings:
            await interaction.response.send_message(
                embed=embed,
                view=StatusActionsView(bot, db, user_id),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def price(interaction: discord.Interaction, player: Optional[str] = None):
        class PriceSelectView(ui.View):
            def __init__(self, initial_player: str | None = None):
                super().__init__(timeout=180)
                self.set_optional = True
                self.subset_required = False
                self.include_any_options = False
                self.player_name = normalize_player_name(initial_player)
                self.set_value = None
                self.subset_group_value = None
                self.subset_variant_value = None
                self.subset_value = None
                self.card_count_value = None
                self.sync_filter_items()

            def _variant_required(self) -> bool:
                return bool(
                    self.set_value
                    and self.subset_group_value
                    and has_subset_variants(self.set_value, self.subset_group_value)
                )

            def sync_filter_items(self) -> None:
                self.clear_items()

                self.set_select = ui.Select(
                    placeholder="Select Set",
                    options=build_set_options(self),
                    row=0,
                )
                async def handle_set_select(interaction: discord.Interaction):
                    await self.on_set_select(interaction)

                self.set_select.callback = handle_set_select
                self.add_item(self.set_select)

                self.subset_select = ui.Select(
                    placeholder="Select Subset",
                    options=build_subset_options(self),
                    disabled=not self.set_value,
                    row=1,
                )
                self.subset_select.callback = self.on_subset_select
                self.add_item(self.subset_select)

                if self._variant_required():
                    self.variant_select = ui.Select(
                        placeholder="Select Variant",
                        options=build_variant_options(self),
                        row=2,
                    )
                    self.variant_select.callback = self.on_variant_select
                    self.add_item(self.variant_select)
                card_count_row = 3 if self._variant_required() else 2
                self.card_count_select = ui.Select(
                    placeholder="Select Card Count",
                    options=build_card_count_options(
                        selected_value=self.card_count_value,
                        include_any=self.include_any_options,
                    ),
                    row=card_count_row,
                )
                async def handle_card_count_select(interaction: discord.Interaction):
                    await on_card_count_select(self, interaction)

                self.card_count_select.callback = handle_card_count_select
                self.add_item(self.card_count_select)

                self.submit_button = ui.Button(
                    label="💰 Show Prices",
                    style=discord.ButtonStyle.green,
                    row=4,
                )
                self.submit_button.callback = self.on_submit
                self.add_item(self.submit_button)
                self.player_button = ui.Button(
                    label="🔍 Select Player",
                    style=discord.ButtonStyle.blurple,
                    row=4,
                )
                self.player_button.callback = self.on_player_name
                self.add_item(self.player_button)
                add_search_buttons(self, row=4)
                self.clear_button = ui.Button(
                    label="Clear Filters",
                    style=discord.ButtonStyle.red,
                    row=4,
                )
                self.clear_button.callback = self.on_clear_filters
                self.add_item(self.clear_button)

            def selected_summary(self) -> str:
                player_display = self.player_name or "Any Player"
                set_display = self.set_value or "Any Set"
                if self.subset_value:
                    subset_display = self.subset_value
                elif self.subset_group_value and self._variant_required():
                    subset_display = f"{self.subset_group_value} (choose variant)"
                else:
                    subset_display = self.subset_group_value or "Any Subset"
                if self.card_count_value is None:
                    card_count_display = "Any Card Count"
                elif str(self.card_count_value) in {"999", "9999"}:
                    card_count_display = "Unlimited"
                else:
                    card_count_display = str(self.card_count_value)
                return (
                    "Select search filters, then choose **Show Prices**.\n\n"
                    f"Player: `{player_display}`\n"
                    f"Set: `{set_display}`\n"
                    f"Subset: `{subset_display}`\n"
                    f"Card Count: `{card_count_display}`"
                )

            async def on_player_name(self, interaction: discord.Interaction):
                await interaction.response.send_modal(
                    PlayerNamePromptModal(
                        "Search Player",
                        self.on_player_name_submit,
                        normalizer=normalize_player_name,
                        initial_value=self.player_name,
                    )
                )

            async def on_player_name_submit(self, interaction: discord.Interaction, player_name: str | None):
                self.player_name = player_name
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_clear_filters(self, interaction: discord.Interaction):
                self.player_name = None
                self.set_value = None
                self.subset_group_value = None
                self.subset_variant_value = None
                self.subset_value = None
                self.card_count_value = None
                self.set_option_order = None
                self.subset_option_order = None
                self.variant_option_order = None
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_set_select(self, interaction: discord.Interaction):
                selected = self.set_select.values[0]
                self.set_value = None if selected == ANY_VALUE else selected
                self.subset_group_value = None
                self.subset_variant_value = None
                self.subset_value = None
                self.subset_option_order = None
                self.variant_option_order = None
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_subset_select(self, interaction: discord.Interaction):
                selected = self.subset_select.values[0]
                if selected == ANY_VALUE:
                    self.subset_group_value = None
                    self.subset_variant_value = None
                    self.subset_value = None
                else:
                    self.subset_group_value = selected
                    self.subset_variant_value = None
                    if self._variant_required():
                        self.subset_value = None
                    else:
                        self.subset_value = selected
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_variant_select(self, interaction: discord.Interaction):
                selected = self.variant_select.values[0]
                if selected in {"select_subset_first", "no_variants"}:
                    await interaction.response.edit_message(content=self.selected_summary(), view=self)
                    return
                self.subset_variant_value = selected
                self.subset_value = format_subset_for_set(
                    self.set_value,
                    self.subset_group_value,
                    self.subset_variant_value,
                )
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_submit(self, interaction: discord.Interaction):
                set_name = _filter_or_none(self.set_value)
                subset = _filter_or_none(self.subset_value)
                cc = _filter_or_none(self.card_count_value)
                player_name = _filter_or_none(self.player_name)
                player_display = player_name or "Any Player"
                results = query_price_source(
                    sheet or db,
                    player_name=player_name,
                    set_name=set_name,
                    cc=cc,
                    subset=subset,
                )
                log_marketplace_event(
                    db,
                    "price_search",
                    user_id=interaction.user.id,
                    details={
                        "player": player_name,
                        "set_name": set_name,
                        "subset": subset,
                        "card_count": cc,
                        "result_count": len(results),
                    },
                    level=20,
                )
                if not results:
                    log_marketplace_event(
                        db,
                        "price_search_empty",
                        user_id=interaction.user.id,
                        details={
                            "player": player_name,
                            "set_name": set_name,
                            "subset": subset,
                            "card_count": cc,
                        },
                        level=20,
                    )
                    await interaction.response.send_message("No pricing data found for those filters.", ephemeral=True)
                    return

                async def save_price_search_as_notification(save_interaction: discord.Interaction):
                    await _save_notify_rule_from_filters(
                        bot,
                        db,
                        save_interaction,
                        {
                            "user_id": save_interaction.user.id,
                            "player_name": player_name,
                            "set_name": set_name,
                            "subset": subset,
                            "card_count": cc,
                        },
                        event_type="price_search_notification_created",
                    )

                result_view = SharedPriceResultsView(
                    results,
                    interaction.user.id,
                    f"Recent sales for **{player_display}**",
                    save_callback=save_price_search_as_notification,
                )
                await interaction.response.send_message(
                    content=result_view.content(),
                    embeds=result_view.embeds(),
                    view=result_view,
                    ephemeral=True,
                )

        view = PriceSelectView(player)
        await interaction.response.send_message(
            view.selected_summary(),
            view=view,
            ephemeral=True,
        )


    async def list_a_player(interaction: discord.Interaction):
        upload_sessions = getattr(bot, "listing_upload_sessions", None)
        if upload_sessions is None:
            upload_sessions = set()
            bot.listing_upload_sessions = upload_sessions
        upload_session_key = (interaction.user.id, interaction.channel_id)
        if upload_session_key in upload_sessions:
            await interaction.response.send_message(
                "You already have a listing image upload in progress in this channel.",
                ephemeral=True,
            )
            return
        upload_sessions.add(upload_session_key)

        sale_channel = bot.get_channel(bot.sale_channel_id)
        if sale_channel is None:
            try:
                sale_channel = await bot.fetch_channel(bot.sale_channel_id)
            except Exception as e:
                LOGGER.error(f"Could not fetch sale channel: {e}")
                log_marketplace_event(
                    db,
                    "listing_create_failed",
                    user_id=interaction.user.id,
                    details=f"Could not fetch sale channel: {e}",
                    level=40,
                )
                sale_channel = None

        class PaymentMethodsModal(ui.Modal, title="Payment Platforms"):
            def __init__(self, parent_view, listing_data: dict):
                super().__init__()
                self.parent_view = parent_view
                self.listing_data = listing_data
                self.payment_methods = ui.TextInput(
                    label="Payment Platforms",
                    placeholder="PayPal, Venmo, Cash App, etc.",
                    required=True,
                )
                self.add_item(self.payment_methods)

            async def on_submit(self, interaction: discord.Interaction):
                payment_methods = str(self.payment_methods.value or "").strip()
                if not payment_methods:
                    await interaction.response.send_message(
                        "Payment platforms are required for your first listing.",
                        ephemeral=True,
                    )
                    return
                self.db = self.parent_view.db
                self.db.upsert_seller_payment_methods(interaction.user.id, payment_methods)
                self.listing_data["payment_methods"] = payment_methods
                await self.parent_view.create_listing(interaction, self.listing_data)

        async def update_listing_review(interaction: discord.Interaction, view: ui.View) -> None:
            content = view.selected_summary()
            try:
                await interaction.response.edit_message(content=content, view=view)
                return
            except (discord.NotFound, discord.HTTPException):
                LOGGER.warning("Could not edit listing review message from modal; sending replacement.")
            if not interaction.response.is_done():
                await interaction.response.send_message(content, view=view, ephemeral=True)
            else:
                await interaction.followup.send(content, view=view, ephemeral=True)

        def build_listing_price_assist(view: ui.View) -> dict | None:
            if not (
                getattr(view, "player_name", None)
                and getattr(view, "set_value", None)
                and getattr(view, "subset_value", None)
                and getattr(view, "card_count_value", None)
            ):
                return None
            return build_price_assist(
                getattr(view.bot, "sheet", None) or view.db,
                {
                    "player_names": view.player_name,
                    "set_name": view.set_value,
                    "subset": view.subset_value,
                    "card_count": view.card_count_value,
                },
            )

        class ListingPriceAssistView(ui.View):
            def __init__(self, price_assist: dict, owner_id: int):
                super().__init__(timeout=180)
                self.price_assist = price_assist
                self.owner_id = owner_id

                button = ui.Button(
                    label="💡 Price Assist",
                    style=discord.ButtonStyle.secondary,
                )
                button.callback = self.on_view_prices
                self.add_item(button)

            async def on_view_prices(self, interaction: discord.Interaction):
                if interaction.user.id != self.owner_id:
                    await interaction.response.send_message(
                        "Only the listing creator can open this price assist.",
                        ephemeral=True,
                    )
                    return

                results = self.price_assist.get("results") or []
                if not results:
                    await interaction.response.send_message(
                        "Sorry, there were no similar matches found for this card",
                        ephemeral=True,
                    )
                    return

                result_view = SharedPriceResultsView(
                    results,
                    interaction.user.id,
                    self.price_assist.get("results_heading") or "Recent price results",
                    self.price_assist.get("range_label"),
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

        async def send_listing_price_assist(interaction: discord.Interaction, view: ui.View) -> None:
            price_assist = build_listing_price_assist(view)
            if not price_assist:
                return

            assist_view = (
                ListingPriceAssistView(price_assist, interaction.user.id)
                if price_assist.get("results")
                else None
            )
            content = (
                "**Price Assist**"
                if price_assist.get("results")
                else f"**Price Assist**\n{price_assist['field_value']}"
            )
            send_kwargs = {
                "content": content,
                "ephemeral": True,
            }
            if assist_view is not None:
                send_kwargs["view"] = assist_view
            await interaction.followup.send(**send_kwargs)

        class PlayerNameModal(ui.Modal, title="Player Name"):
            def __init__(self, parent_view):
                super().__init__()
                self.parent_view = parent_view
                self.player_name = ui.TextInput(
                    label="Player Name(s)",
                    placeholder="Luka Doncic",
                    default=str(parent_view.player_name or "")[:4000],
                    required=True,
                )
                self.add_item(self.player_name)

            async def on_submit(self, interaction: discord.Interaction):
                player_name = normalize_player_name(self.player_name.value)
                if not player_name:
                    await interaction.response.send_message("Player name is required.", ephemeral=True)
                    return

                self.parent_view.player_name = player_name
                await update_listing_review(interaction, self.parent_view)

        class ListingPriceModal(ui.Modal, title="Listing Price"):
            def __init__(self, parent_view):
                super().__init__()
                self.parent_view = parent_view
                self.price = ui.TextInput(
                    label="Listing Price",
                    placeholder="25.00",
                    default="" if parent_view.price is None else f"{parent_view.price:.2f}",
                    required=True,
                )
                self.add_item(self.price)

            async def on_submit(self, interaction: discord.Interaction):
                try:
                    price = float(str(self.price.value).replace("$", "").replace(",", "").strip())
                except (TypeError, ValueError):
                    await interaction.response.send_message("Price must be a number, like 25.00.", ephemeral=True)
                    return
                if price <= 0:
                    await interaction.response.send_message("Price must be greater than 0.", ephemeral=True)
                    return

                self.parent_view.price = price
                await update_listing_review(interaction, self.parent_view)
                await send_listing_price_assist(interaction, self.parent_view)

        class ListPlayerView(ui.View):
            def __init__(self, bot, db, sale_channel, image_url, image_bytes, metadata_guess):
                super().__init__(timeout=300)

                self.bot = bot
                self.db = db
                self.sale_channel = sale_channel
                self.image_url = image_url
                self.image_bytes = image_bytes
                self.image_filename = "card_image.png"
                self.metadata_guess = metadata_guess
                self.player_name = metadata_guess.player_name
                self.price = None
                self.image_was_cropped = metadata_guess.image_was_cropped

                self.set_optional = False
                self.subset_required = True
                self.set_value = metadata_guess.set_name
                self.subset_group_value = metadata_guess.subset_group
                self.subset_variant_value = metadata_guess.subset_variant
                self.subset_value = metadata_guess.subset_value
                self.card_count_value = metadata_guess.card_count
                self.card_rarity = metadata_guess.card_rarity
                self.set_option_order = metadata_guess.set_option_order
                self.subset_option_order = metadata_guess.subset_option_order
                self.variant_option_order = metadata_guess.variant_option_order
                self.review_message = None

                self.set_select = ui.Select(
                    placeholder="Select Set",
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
                    disabled=not (
                        self.set_value
                        and self.subset_group_value
                        and self.variant_option_order
                    ),
                    row=2,
                )
                self.variant_select.callback = self.on_variant_select
                self.add_item(self.variant_select)

                self.card_count_select = ui.Select(
                    placeholder="Select Card Count",
                    options=build_card_count_options(
                        selected_value=(
                            str(self.card_count_value)
                            if self.card_count_value is not None
                            else ANY_VALUE
                        )
                    ),
                    row=3,
                )
                async def handle_card_count_select(interaction: discord.Interaction):
                    await on_card_count_select(self, interaction)

                self.card_count_select.callback = handle_card_count_select
                self.add_item(self.card_count_select)

                add_search_buttons(self, row=4)

                self.price_button = ui.Button(
                    label="💵 Set Price",
                    style=discord.ButtonStyle.blurple,
                    row=4,
                )
                self.price_button.callback = self.on_set_price
                self.add_item(self.price_button)

                self.player_button = ui.Button(
                    label="✏️ Edit Player",
                    style=discord.ButtonStyle.blurple,
                    row=4,
                )
                self.player_button.callback = self.on_edit_player
                self.add_item(self.player_button)

                self.submit_button = ui.Button(
                    label="🏷️ Create Listing",
                    style=discord.ButtonStyle.green,
                    row=4,
                )
                self.submit_button.callback = self.on_submit
                self.add_item(self.submit_button)


            async def on_subset_select(self, interaction: discord.Interaction):
                await on_subset_select(self, interaction)

            async def on_variant_select(self, interaction: discord.Interaction):
                await on_variant_select(self, interaction)

            def selected_summary(self) -> str:
                details = [
                    "**Review listing details**",
                    f"Player: **{self.player_name or 'Needs entry'}**",
                    f"Set: **{self.set_value or 'Needs selection'}**",
                    f"Subset: **{self.subset_value or self.subset_group_value or 'Needs selection'}**",
                    f"Card Count: **/{format_card_count(self.card_count_value)}**"
                    if self.card_count_value
                    else "Card Count: **Needs selection**",
                ]
                if self.card_rarity:
                    details.append(f"Rarity: **{self.card_rarity}**")
                if self.price is not None:
                    details.append(f"Price: **{_format_price(self.price)}**")
                return "\n".join(details)

            async def on_set_price(self, interaction: discord.Interaction):
                await interaction.response.send_modal(ListingPriceModal(self))

            async def on_edit_player(self, interaction: discord.Interaction):
                await interaction.response.send_modal(PlayerNameModal(self))

            async def create_listing(self, interaction: discord.Interaction, listing_data: dict):
                if not interaction.response.is_done():
                    if interaction.type == discord.InteractionType.modal_submit:
                        await interaction.response.defer(ephemeral=True, thinking=True)
                    else:
                        await interaction.response.defer()

                image_file = None
                if self.image_bytes:
                    image_file = discord.File(
                        io.BytesIO(self.image_bytes),
                        filename=self.image_filename,
                    )
                    listing_data["image_url"] = f"attachment://{self.image_filename}"
                    listing_data["image_bytes"] = self.image_bytes
                    listing_data["image_filename"] = self.image_filename
                else:
                    listing_data["image_url"] = self.image_url

                await publish_listing(
                    self.bot,
                    self.db,
                    self.sale_channel,
                    interaction,
                    listing_data,
                    image_file=image_file,
                )
                await _finish_listing_workflow_message(interaction, self.review_message)
                self.stop()


            async def on_submit(self, interaction: discord.Interaction):
                if not self.player_name or self.price is None:
                    await _edit_listing_workflow_error(
                        interaction,
                        self,
                        "Use Edit Player and Set Price before submitting.",
                    )
                    return
                if not self.set_value or not self.subset_value or not self.card_count_value:
                    await _edit_listing_workflow_error(
                        interaction,
                        self,
                        "Please select set, subset, and card count.",
                    )
                    return
                if self.sale_channel is None:
                    log_marketplace_event(
                        self.db,
                        "listing_create_failed",
                        user_id=interaction.user.id,
                        details="Sale channel is not configured.",
                        level=40,
                    )
                    await _edit_listing_workflow_error(
                        interaction,
                        self,
                        "Sale channel is not configured or could not be found. Please contact an admin.",
                    )
                    return

                listing_data = {
                    "player_names": self.player_name,
                    "set_name": self.set_value,
                    "subset": self.subset_value,
                    "card_count": self.card_count_value,
                    "card_rarity": self.card_rarity,
                    "price": self.price,
                    "date_time": format_sheet_datetime(),
                    "seller": interaction.user,
                    "seller_id": interaction.user.id,
                    "status": "active",
                    "image_url": None,
                    "bids": [],
                }
                payment_methods = self.db.get_seller_payment_methods(interaction.user.id)
                if not payment_methods:
                    await interaction.response.send_modal(PaymentMethodsModal(self, listing_data))
                    return

                listing_data["payment_methods"] = payment_methods
                await self.create_listing(interaction, listing_data)
                return

        await interaction.response.send_message(
            "Please upload the card image in this channel within 2 minutes.",
            ephemeral=True,
        )
        try:
            upload_prompt = await interaction.original_response()
        except discord.DiscordException:
            upload_prompt = None

        try:
            image_url, _image_file, image_bytes = await collect_listing_image(
                bot,
                db,
                interaction,
                None,
            )
        finally:
            upload_sessions.discard(upload_session_key)

        if not image_bytes and not image_url:
            await _edit_original_workflow_status(
                interaction,
                "No image was received.",
            )
            await interaction.followup.send(
                "No image was received, so the listing was not started. Use List Player again when you are ready to upload.",
                ephemeral=True,
            )
            return
        await _edit_original_workflow_status(
            interaction,
            "Image received. Reading card details...",
        )
        ocr_started_at = time.perf_counter()
        metadata_guess, processed_image_bytes, image_was_cropped = await asyncio.to_thread(
            _extract_listing_metadata_for_review,
            image_bytes,
            _get_known_player_names(db),
        )
        ocr_elapsed_ms = int((time.perf_counter() - ocr_started_at) * 1000)
        if processed_image_bytes:
            image_bytes = processed_image_bytes
        metadata_guess.image_was_cropped = image_was_cropped
        log_marketplace_event(
            db,
            "listing_ocr_processed",
            user_id=interaction.user.id,
            details={
                "engine": metadata_guess.engine,
                "ocr_available": metadata_guess.ocr_available,
                "set_name": metadata_guess.set_name,
                "player_name": metadata_guess.player_name,
                "subset": metadata_guess.subset_value or metadata_guess.subset_group,
                "card_count": metadata_guess.card_count,
                "card_count_source": metadata_guess.card_count_source,
                "card_rarity": metadata_guess.card_rarity,
                "card_rarity_source": metadata_guess.card_rarity_source,
                "image_was_cropped": image_was_cropped,
                "elapsed_ms": ocr_elapsed_ms,
                "confidence": metadata_guess.confidence,
            },
        )
        view = ListPlayerView(bot, db, sale_channel, image_url, image_bytes, metadata_guess)
        try:
            view.review_message = await interaction.edit_original_response(
                content=view.selected_summary(),
                view=view,
            )
        except discord.DiscordException:
            try:
                view.review_message = await interaction.followup.send(
                    view.selected_summary(),
                    view=view,
                    ephemeral=True,
                    wait=True,
                )
            except TypeError:
                await interaction.followup.send(
                    view.selected_summary(),
                    view=view,
                    ephemeral=True,
                )

    async def auction_a_player(
        interaction: discord.Interaction,
        player_name: str = None,
        starting_price: float = None,
        duration_hours: int = 24,
        bid_increment: float = 1.0,
    ):
        player_name = normalize_player_name(player_name)
        if starting_price is not None and starting_price <= 0:
            await interaction.response.send_message("Starting price must be greater than 0.", ephemeral=True)
            return
        duration_hours = duration_hours or 24
        if duration_hours < 1 or duration_hours > 168:
            await interaction.response.send_message("Duration must be between 1 and 168 hours.", ephemeral=True)
            return
        bid_increment = bid_increment or 1.0
        if bid_increment <= 0:
            await interaction.response.send_message("Bid increment must be greater than 0.", ephemeral=True)
            return

        auction_channel = await bot.fetch_channel_safely(bot.auction_surface_channel_id)
        if auction_channel is None:
            log_marketplace_event(
                db,
                "auction_create_failed",
                user_id=interaction.user.id,
                details="Auction channel is not configured or could not be found.",
                level=40,
            )
            await interaction.response.send_message(
                "Auction channel is not configured or could not be found. Please contact an admin.",
                ephemeral=True,
            )
            return

        upload_sessions = getattr(bot, "listing_upload_sessions", None)
        if upload_sessions is None:
            upload_sessions = set()
            bot.listing_upload_sessions = upload_sessions
        upload_session_key = (interaction.user.id, interaction.channel_id)
        if upload_session_key in upload_sessions:
            await interaction.response.send_message(
                "You already have a listing image upload in progress in this channel.",
                ephemeral=True,
            )
            return
        upload_sessions.add(upload_session_key)

        class AuctionPaymentMethodsModal(ui.Modal, title="Payment Platforms"):
            def __init__(self, parent_view, listing_data: dict):
                super().__init__()
                self.parent_view = parent_view
                self.listing_data = listing_data
                self.payment_methods = ui.TextInput(
                    label="Payment Platforms",
                    placeholder="PayPal, Venmo, Cash App, etc.",
                    required=True,
                )
                self.add_item(self.payment_methods)

            async def on_submit(self, interaction: discord.Interaction):
                payment_methods = str(self.payment_methods.value or "").strip()
                if not payment_methods:
                    await interaction.response.send_message(
                        "Payment platforms are required for your first auction.",
                        ephemeral=True,
                    )
                    return
                self.parent_view.db.upsert_seller_payment_methods(interaction.user.id, payment_methods)
                self.listing_data["payment_methods"] = payment_methods
                await self.parent_view.create_auction(interaction, self.listing_data)

        class AuctionPlayerNameModal(ui.Modal, title="Auction Player"):
            def __init__(self, parent_view):
                super().__init__()
                self.parent_view = parent_view
                self.player_name = ui.TextInput(
                    label="Player Name(s)",
                    placeholder="Luka Doncic",
                    default=str(parent_view.player_name or "")[:4000],
                    required=True,
                )
                self.add_item(self.player_name)

            async def on_submit(self, interaction: discord.Interaction):
                player_name = normalize_player_name(self.player_name.value)
                if not player_name:
                    await interaction.response.send_message("Player name is required.", ephemeral=True)
                    return

                self.parent_view.player_name = player_name
                try:
                    await interaction.response.edit_message(
                        content=self.parent_view.selected_summary(),
                        view=self.parent_view,
                    )
                except (discord.NotFound, discord.HTTPException):
                    await interaction.response.send_message(
                        self.parent_view.selected_summary(),
                        view=self.parent_view,
                        ephemeral=True,
                    )

        class AuctionTermsModal(ui.Modal, title="Auction Terms"):
            def __init__(self, parent_view):
                super().__init__()
                self.parent_view = parent_view
                self.starting_price = ui.TextInput(
                    label="Starting price",
                    placeholder="25.00",
                    default="" if parent_view.starting_price is None else f"{parent_view.starting_price:.2f}",
                    required=True,
                    max_length=20,
                )
                self.duration_hours = ui.TextInput(
                    label="Duration hours",
                    placeholder="24",
                    default=str(parent_view.duration_hours or 24),
                    required=True,
                    max_length=3,
                )
                self.bid_increment = ui.TextInput(
                    label="Bid increment",
                    placeholder="1.00",
                    default=f"{float(parent_view.bid_increment or 1.0):.2f}",
                    required=True,
                    max_length=20,
                )
                self.add_item(self.starting_price)
                self.add_item(self.duration_hours)
                self.add_item(self.bid_increment)

            async def on_submit(self, interaction: discord.Interaction):
                try:
                    starting_price = float(str(self.starting_price.value).replace("$", "").replace(",", "").strip())
                    duration_hours = int(str(self.duration_hours.value).strip())
                    bid_increment = float(str(self.bid_increment.value).replace("$", "").replace(",", "").strip())
                except (TypeError, ValueError):
                    await interaction.response.send_message(
                        "Starting price, duration, and bid increment must be valid numbers.",
                        ephemeral=True,
                    )
                    return
                if starting_price <= 0:
                    await interaction.response.send_message("Starting price must be greater than 0.", ephemeral=True)
                    return
                if duration_hours < 1 or duration_hours > 168:
                    await interaction.response.send_message("Duration must be between 1 and 168 hours.", ephemeral=True)
                    return
                if bid_increment <= 0:
                    await interaction.response.send_message("Bid increment must be greater than 0.", ephemeral=True)
                    return

                self.parent_view.starting_price = starting_price
                self.parent_view.duration_hours = duration_hours
                self.parent_view.bid_increment = bid_increment
                try:
                    await interaction.response.edit_message(
                        content=self.parent_view.selected_summary(),
                        view=self.parent_view,
                    )
                except (discord.NotFound, discord.HTTPException):
                    await interaction.response.send_message(
                        self.parent_view.selected_summary(),
                        view=self.parent_view,
                        ephemeral=True,
                    )

        class AuctionPlayerView(ui.View):
            def __init__(
                self,
                bot,
                db,
                auction_channel,
                player_name,
                starting_price,
                duration_hours,
                bid_increment,
                image_url,
                image_bytes,
                metadata_guess,
            ):
                super().__init__(timeout=300)
                self.bot = bot
                self.db = db
                self.auction_channel = auction_channel
                self.image_url = image_url
                self.image_bytes = image_bytes
                self.image_filename = "card_image.png"
                self.metadata_guess = metadata_guess
                self.player_name = player_name or metadata_guess.player_name
                self.starting_price = starting_price
                self.duration_hours = duration_hours
                self.bid_increment = bid_increment
                self.image_was_cropped = metadata_guess.image_was_cropped
                self.set_optional = False
                self.subset_required = True
                self.set_value = metadata_guess.set_name
                self.subset_group_value = metadata_guess.subset_group
                self.subset_variant_value = metadata_guess.subset_variant
                self.subset_value = metadata_guess.subset_value
                self.card_count_value = metadata_guess.card_count
                self.card_rarity = metadata_guess.card_rarity
                self.set_option_order = metadata_guess.set_option_order
                self.subset_option_order = metadata_guess.subset_option_order
                self.variant_option_order = metadata_guess.variant_option_order
                self.review_message = None

                self.set_select = ui.Select(
                    placeholder="Select Set",
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
                    disabled=not (
                        self.set_value
                        and self.subset_group_value
                        and self.variant_option_order
                    ),
                    row=2,
                )
                self.variant_select.callback = self.on_variant_select
                self.add_item(self.variant_select)

                self.card_count_select = ui.Select(
                    placeholder="Optional: Select Card Count",
                    options=build_card_count_options(
                        selected_value=(
                            str(self.card_count_value)
                            if self.card_count_value is not None
                            else ANY_VALUE
                        )
                    ),
                    row=3,
                )
                async def handle_card_count_select(interaction: discord.Interaction):
                    await on_card_count_select(self, interaction)

                self.card_count_select.callback = handle_card_count_select
                self.add_item(self.card_count_select)

                add_search_buttons(self, row=4)

                self.player_button = ui.Button(
                    label="✏️ Edit Player",
                    style=discord.ButtonStyle.blurple,
                    row=4,
                )
                self.player_button.callback = self.on_edit_player
                self.add_item(self.player_button)

                self.terms_button = ui.Button(
                    label="💵 Set Terms",
                    style=discord.ButtonStyle.blurple,
                    row=4,
                )
                self.terms_button.callback = self.on_set_terms
                self.add_item(self.terms_button)

                self.submit_button = ui.Button(
                    label="🔨 Submit Auction",
                    style=discord.ButtonStyle.green,
                    row=4,
                )
                self.submit_button.callback = self.on_submit
                self.add_item(self.submit_button)

            async def on_subset_select(self, interaction: discord.Interaction):
                await on_subset_select(self, interaction)

            async def on_variant_select(self, interaction: discord.Interaction):
                await on_variant_select(self, interaction)

            def selected_summary(self) -> str:
                details = [
                    "**Review auction details**",
                    f"Player: **{self.player_name or 'Needs entry'}**",
                    f"Set: **{self.set_value or 'Needs selection'}**",
                    f"Subset: **{self.subset_value or self.subset_group_value or 'Needs selection'}**",
                    f"Card Count: **/{format_card_count(self.card_count_value)}**"
                    if self.card_count_value
                    else "Card Count: **Needs selection**",
                    f"Starting Bid: **{_format_price(self.starting_price) if self.starting_price is not None else 'Needs entry'}**",
                    f"Bid Increment: **{_format_price(self.bid_increment)}**",
                    f"Duration: **{self.duration_hours} hour(s)**",
                ]
                if self.card_rarity:
                    details.append(f"Rarity: **{self.card_rarity}**")
                return "\n".join(details)

            async def on_edit_player(self, interaction: discord.Interaction):
                await interaction.response.send_modal(AuctionPlayerNameModal(self))

            async def on_set_terms(self, interaction: discord.Interaction):
                await interaction.response.send_modal(AuctionTermsModal(self))

            async def create_auction(self, interaction: discord.Interaction, listing_data: dict):
                if not interaction.response.is_done():
                    if interaction.type == discord.InteractionType.modal_submit:
                        await interaction.response.defer(ephemeral=True, thinking=True)
                    else:
                        await interaction.response.defer()

                image_file = None
                if self.image_bytes:
                    image_file = discord.File(
                        io.BytesIO(self.image_bytes),
                        filename=self.image_filename,
                    )
                    listing_data["image_url"] = f"attachment://{self.image_filename}"
                    listing_data["image_bytes"] = self.image_bytes
                    listing_data["image_filename"] = self.image_filename
                else:
                    listing_data["image_url"] = self.image_url

                await publish_listing(
                    self.bot,
                    self.db,
                    self.auction_channel,
                    interaction,
                    listing_data,
                    image_file=image_file,
                )
                await _finish_listing_workflow_message(interaction, self.review_message)
                self.stop()

            async def create_listing(self, interaction: discord.Interaction, listing_data: dict):
                await self.create_auction(interaction, listing_data)

            async def on_submit(self, interaction: discord.Interaction):
                if not self.player_name:
                    await _edit_listing_workflow_error(
                        interaction,
                        self,
                        "Use Edit Player before submitting.",
                    )
                    return
                if self.starting_price is None:
                    await _edit_listing_workflow_error(
                        interaction,
                        self,
                        "Use Set Terms before submitting.",
                    )
                    return
                if not self.set_value or not self.subset_value or not self.card_count_value:
                    await _edit_listing_workflow_error(
                        interaction,
                        self,
                        "Please select set, subset, and card count.",
                    )
                    return

                end_at = datetime.now(timezone.utc) + timedelta(hours=self.duration_hours)
                listing_data = {
                    "listing_type": "auction",
                    "player_names": self.player_name,
                    "set_name": self.set_value,
                    "subset": self.subset_value,
                    "card_count": self.card_count_value,
                    "card_rarity": self.card_rarity,
                    "price": self.starting_price,
                    "starting_price": self.starting_price,
                    "bid_increment": self.bid_increment,
                    "auction_end_at": end_at.isoformat(),
                    "date_time": format_sheet_datetime(),
                    "seller": interaction.user,
                    "seller_id": interaction.user.id,
                    "status": "active",
                    "image_url": None,
                    "bids": [],
                }
                payment_methods = self.db.get_seller_payment_methods(interaction.user.id)
                if not payment_methods:
                    await interaction.response.send_modal(AuctionPaymentMethodsModal(self, listing_data))
                    return

                listing_data["payment_methods"] = payment_methods
                await self.create_auction(interaction, listing_data)

        await interaction.response.send_message(
            "Please upload the card image in this channel within 2 minutes.",
            ephemeral=True,
        )
        try:
            upload_prompt = await interaction.original_response()
        except discord.DiscordException:
            upload_prompt = None

        try:
            image_url, _image_file, image_bytes = await collect_listing_image(
                bot,
                db,
                interaction,
                None,
            )
        finally:
            upload_sessions.discard(upload_session_key)

        if not image_bytes and not image_url:
            await _edit_original_workflow_status(
                interaction,
                "No image was received.",
            )
            await interaction.followup.send(
                "No image was received, so the auction was not started. Use Auction Player again when you are ready to upload.",
                ephemeral=True,
            )
            return

        await _edit_original_workflow_status(
            interaction,
            "Image received. Reading card details...",
        )
        ocr_started_at = time.perf_counter()
        metadata_guess, processed_image_bytes, image_was_cropped = await asyncio.to_thread(
            _extract_listing_metadata_for_review,
            image_bytes,
            _get_known_player_names(db),
        )
        ocr_elapsed_ms = int((time.perf_counter() - ocr_started_at) * 1000)
        if processed_image_bytes:
            image_bytes = processed_image_bytes
        metadata_guess.image_was_cropped = image_was_cropped
        log_marketplace_event(
            db,
            "auction_ocr_processed",
            user_id=interaction.user.id,
            details={
                "engine": metadata_guess.engine,
                "ocr_available": metadata_guess.ocr_available,
                "set_name": metadata_guess.set_name,
                "player_name": metadata_guess.player_name,
                "entered_player_name": player_name,
                "subset": metadata_guess.subset_value or metadata_guess.subset_group,
                "card_count": metadata_guess.card_count,
                "card_count_source": metadata_guess.card_count_source,
                "card_rarity": metadata_guess.card_rarity,
                "card_rarity_source": metadata_guess.card_rarity_source,
                "image_was_cropped": image_was_cropped,
                "elapsed_ms": ocr_elapsed_ms,
                "confidence": metadata_guess.confidence,
            },
        )

        view = AuctionPlayerView(
            bot,
            db,
            auction_channel,
            player_name,
            starting_price,
            duration_hours,
            bid_increment,
            image_url,
            image_bytes,
            metadata_guess,
        )
        try:
            view.review_message = await interaction.edit_original_response(
                content=view.selected_summary(),
                view=view,
            )
        except discord.DiscordException:
            try:
                view.review_message = await interaction.followup.send(
                    view.selected_summary(),
                    view=view,
                    ephemeral=True,
                    wait=True,
                )
            except TypeError:
                await interaction.followup.send(
                    view.selected_summary(),
                    view=view,
                    ephemeral=True,
                )


    async def notify(interaction: discord.Interaction, player_name: str = None):
        class NotifyView(ui.View):
            def __init__(self, initial_player: str | None = None):
                super().__init__(timeout=300)

                self.set_optional = True
                self.subset_required = False
                self.include_any_options = False
                self.player_name = normalize_player_name(initial_player)
                self.set_value = None
                self.subset_group_value = None
                self.subset_variant_value = None
                self.subset_value = None
                self.card_count_value = None

                # Ensure notify_rules exists before this view tries to append to it.
                # IMPORTANT: You should still initialize bot.notify_rules = [] before bot.run(...).
                if not hasattr(bot, "notify_rules"):
                    bot.notify_rules = []
                self.sync_filter_items()

            def _variant_required(self) -> bool:
                return bool(
                    self.set_value
                    and self.subset_group_value
                    and has_subset_variants(self.set_value, self.subset_group_value)
                )

            def sync_filter_items(self) -> None:
                self.clear_items()

                self.set_select = ui.Select(
                    placeholder="Select Set",
                    options=build_set_options(self),
                    row=0,
                )
                async def handle_set_select(interaction: discord.Interaction):
                    await self.on_set_select(interaction)

                self.set_select.callback = handle_set_select
                self.add_item(self.set_select)

                self.subset_select = ui.Select(
                    placeholder="Select Subset",
                    options=build_subset_options(self),
                    disabled=not self.set_value,
                    row=1,
                )
                self.subset_select.callback = self.on_subset_select
                self.add_item(self.subset_select)

                if self._variant_required():
                    self.variant_select = ui.Select(
                        placeholder="Optional: Select Variant",
                        options=build_variant_options(self),
                        row=2,
                    )
                    self.variant_select.callback = self.on_variant_select
                    self.add_item(self.variant_select)
                card_count_row = 3 if self._variant_required() else 2
                self.card_count_select = ui.Select(
                    placeholder="Select Card Count",
                    options=build_card_count_options(
                        selected_value=self.card_count_value,
                        include_any=self.include_any_options,
                    ),
                    row=card_count_row,
                )
                async def handle_card_count_select(interaction: discord.Interaction):
                    await on_card_count_select(self, interaction)

                self.card_count_select.callback = handle_card_count_select
                self.add_item(self.card_count_select)

                self.submit_button = ui.Button(
                    label="🔔 Create Notification",
                    style=discord.ButtonStyle.green,
                    row=4,
                )
                self.submit_button.callback = self.on_submit
                self.add_item(self.submit_button)
                self.player_button = ui.Button(
                    label="🔍 Select Player",
                    style=discord.ButtonStyle.blurple,
                    row=4,
                )
                self.player_button.callback = self.on_player_name
                self.add_item(self.player_button)
                add_search_buttons(self, row=4)
                self.clear_button = ui.Button(
                    label="Clear Filters",
                    style=discord.ButtonStyle.red,
                    row=4,
                )
                self.clear_button.callback = self.on_clear_filters
                self.add_item(self.clear_button)

            def selected_summary(self) -> str:
                player_display = self.player_name or "Any Player"
                set_display = self.set_value or "Any Set"
                if self.subset_value:
                    subset_display = self.subset_value
                elif self.subset_group_value and self._variant_required():
                    subset_display = f"{self.subset_group_value} (choose variant)"
                else:
                    subset_display = self.subset_group_value or "Any Subset"

                if self.card_count_value is None:
                    card_count_display = "Any Card Count"
                elif str(self.card_count_value) in {"999", "9999"}:
                    card_count_display = "Unlimited"
                else:
                    card_count_display = str(self.card_count_value)

                return (
                    "Choose notification filters, then select **🔔 Create Notification**.\n\n"
                    f"Player: `{player_display}`\n"
                    f"Set: `{set_display}`\n"
                    f"Subset: `{subset_display}`\n"
                    f"Card Count: `{card_count_display}`"
                )

            async def on_player_name(self, interaction: discord.Interaction):
                await interaction.response.send_modal(
                    PlayerNamePromptModal(
                        "Search Player",
                        self.on_player_name_submit,
                        normalizer=normalize_player_name,
                        initial_value=self.player_name,
                    )
                )

            async def on_player_name_submit(self, interaction: discord.Interaction, player_name: str | None):
                self.player_name = player_name
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_clear_filters(self, interaction: discord.Interaction):
                self.player_name = None
                self.set_value = None
                self.subset_group_value = None
                self.subset_variant_value = None
                self.subset_value = None
                self.card_count_value = None
                self.set_option_order = None
                self.subset_option_order = None
                self.variant_option_order = None
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_set_select(self, interaction: discord.Interaction):
                selected = self.set_select.values[0]
                self.set_value = None if selected == ANY_VALUE else selected
                self.subset_group_value = None
                self.subset_variant_value = None
                self.subset_value = None
                self.subset_option_order = None
                self.variant_option_order = None
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_subset_select(self, interaction: discord.Interaction):
                selected = self.subset_select.values[0]
                if selected == ANY_VALUE:
                    self.subset_group_value = None
                    self.subset_variant_value = None
                    self.subset_value = None
                else:
                    self.subset_group_value = selected
                    self.subset_variant_value = None
                    if self._variant_required():
                        self.subset_value = None
                    else:
                        self.subset_value = selected
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_variant_select(self, interaction: discord.Interaction):
                selected = self.variant_select.values[0]
                if selected in {"select_subset_first", "no_variants"}:
                    await interaction.response.edit_message(content=self.selected_summary(), view=self)
                    return
                self.subset_variant_value = selected
                self.subset_value = format_subset_for_set(
                    self.set_value,
                    self.subset_group_value,
                    self.subset_variant_value,
                )
                self.sync_filter_items()
                await interaction.response.edit_message(content=self.selected_summary(), view=self)

            async def on_submit(self, interaction: discord.Interaction):
                if not self.player_name and not self.set_value and not self.subset_value and self.card_count_value is None:
                    await interaction.response.send_message(
                        "Choose at least one notification filter: player, set, subset, or card count.",
                        ephemeral=True,
                    )
                    return

                rule = {
                    "user_id": interaction.user.id,
                    "player_name": _filter_or_none(self.player_name),
                    # Keep this as the exact selected set key/name so matching works later.
                    "set_name": _filter_or_none(self.set_value),
                    "subset": _filter_or_none(self.subset_value),
                    "card_count": _filter_or_none(self.card_count_value),
                }

                await _save_notify_rule_from_filters(
                    bot,
                    db,
                    interaction,
                    rule,
                    event_type="notification_created",
                    edit_response=True,
                )
                self.stop()

        view = NotifyView(player_name)
        await interaction.response.send_message(
            view.selected_summary(),
            view=view,
            ephemeral=True,
        )

    class NotificationHubView(ui.View):
        def __init__(self, owner_id: int):
            super().__init__(timeout=180)
            self.owner_id = owner_id

        async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id == self.owner_id:
                return False
            await interaction.response.send_message(
                "Only the notification owner can use this panel.",
                ephemeral=True,
            )
            return True

        @ui.button(label="🔔 Add Notification", style=discord.ButtonStyle.green, custom_id="nba_bot:notification_add", row=0)
        async def add_notification_button(self, interaction: discord.Interaction, button: ui.Button):
            if await self._reject_wrong_user(interaction):
                return
            await notify(interaction)

        @ui.button(label="🔕 Remove Notification", style=discord.ButtonStyle.red, custom_id="nba_bot:notification_remove", row=0)
        async def remove_notification_button(self, interaction: discord.Interaction, button: ui.Button):
            if await self._reject_wrong_user(interaction):
                return
            await remove_notify(interaction)

    def _notification_hub_content(user_id: int) -> str:
        notify_rules = _get_active_notify_rules_for_user(bot, user_id)
        lines = [
            "## 🔔 Notifications",
            "Your active notification alerts:",
            "",
        ]
        if notify_rules:
            lines.extend(_notify_rule_line(rule) for rule in notify_rules[:25])
            if len(notify_rules) > 25:
                lines.append(f"\nShowing 25 of {len(notify_rules)} active notifications.")
        else:
            lines.append("No active notifications yet.")
        return "\n".join(lines)

    class BotInterfaceView(ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @ui.button(label="🏷️ List Player", style=discord.ButtonStyle.green, custom_id="nba_bot:list_player", row=1)
        async def list_player_button(self, interaction: discord.Interaction, button: ui.Button):
            await list_a_player(interaction)

        @ui.button(label="🔨 Auction Player", style=discord.ButtonStyle.green, custom_id="nba_bot:auction_player", row=1)
        async def auction_player_button(self, interaction: discord.Interaction, button: ui.Button):
            await auction_a_player(interaction)

        @ui.button(label="🔍 Price Search", style=discord.ButtonStyle.blurple, custom_id="nba_bot:price", row=0)
        async def price_button(self, interaction: discord.Interaction, button: ui.Button):
            await price(interaction)

        @ui.button(label="📊 View Status", style=discord.ButtonStyle.blurple, custom_id="nba_bot:status", row=3)
        async def status_button(self, interaction: discord.Interaction, button: ui.Button):
            await status(interaction)

        @ui.button(label=f"🔔 Notifications{BUTTON_PAD * 2}", style=discord.ButtonStyle.blurple, custom_id="nba_bot:notifications", row=3)
        async def notifications_button(self, interaction: discord.Interaction, button: ui.Button):
            await interaction.response.send_message(
                _notification_hub_content(interaction.user.id),
                view=NotificationHubView(interaction.user.id),
                ephemeral=True,
            )

        @ui.button(label="❓ Get Help", style=discord.ButtonStyle.secondary, custom_id="nba_bot:help", row=4)
        async def help_button(self, interaction: discord.Interaction, button: ui.Button):
            await help_command(interaction)

        @ui.button(label=f"💬 Leave Feedback{BUTTON_PAD}", style=discord.ButtonStyle.secondary, custom_id="nba_bot:feedback", row=4)
        async def feedback_button(self, interaction: discord.Interaction, button: ui.Button):
            await feedback(interaction)

        @ui.button(label=f"🛒 Marketplace{BUTTON_PAD * 2}", style=discord.ButtonStyle.blurple, custom_id="nba_bot:open_marketplace", row=0)
        async def open_marketplace_button(self, interaction: discord.Interaction, button: ui.Button):
            await interaction.response.defer(ephemeral=True, thinking=True)
            listings = _get_active_marketplace_listings(bot)
            log_marketplace_event(
                db,
                "marketplace_view_opened",
                user_id=interaction.user.id,
                details={
                    "active_listing_count": len(listings),
                    "source": "nba_bot_interface",
                },
            )
            view = MarketplaceCarouselView(
                bot,
                db,
                listings,
                owner_id=interaction.user.id,
            )
            kwargs = await view.build_message_kwargs()
            await interaction.followup.send(
                **kwargs,
                view=view,
                ephemeral=True,
            )

    bot.bot_interface_view_factory = BotInterfaceView
