import discord
import asyncio
import io
import json

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from discord import app_commands, ui

from commands import register_bot_interface
from database import CardDatabase
from logger import LOGGER, log_marketplace_event
from main import load_config, create_price_sheet, sync_sheet_to_db
from serializers import build_listing_embed
from sheets import PriceSheet
from views import (
    AcceptBidView,
    AuctionActionView,
    ClaimedListingView,
    CounterOfferView,
    DisabledClaimedListingActionView,
    DisabledListingView,
    ListingActionView,
)

UNAVAILABLE_MESSAGE = object()
SELLER_RECONCILIATION_INTERVAL_SECONDS = 60 * 60
CLAIM_REMINDER_INTERVAL_SECONDS = 60 * 60
NBA_BOT_INTERFACE_CONTENT = "**NBA Bot**\nUse the buttons below to browse, price, list, auction, and manage your marketplace activity."


class StoredDiscordUser:
    """Small user stand-in for restored marketplace records."""

    def __init__(self, user_id: int, name: str = None):
        self.id = int(user_id)
        self.name = name or str(user_id)
        self.display_name = self.name
        self.mention = f"<@{self.id}>"


def _mention_user(user: discord.abc.User) -> str:
    return getattr(user, "mention", None) or f"<@{getattr(user, 'id', user)}>"


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


def _listing_newest_sort_key(listing: dict) -> tuple[datetime, int]:
    timestamp = (
        _parse_iso_datetime(listing.get("created_at"))
        or _parse_iso_datetime(listing.get("updated_at"))
    )
    message_id = listing.get("message_id")
    if timestamp is None and message_id:
        try:
            timestamp = discord.utils.snowflake_time(int(message_id))
        except (TypeError, ValueError):
            timestamp = None
    return timestamp or datetime.min.replace(tzinfo=timezone.utc), int(message_id or 0)


class ClaimReminderView(ClaimedListingView):
    """Seller reminder controls for a claimed listing."""

    @ui.button(label="🔔 Notify Buyer", style=discord.ButtonStyle.blurple, custom_id="claim_reminder:notify_buyer")
    async def notify_buyer(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.listing_data["seller_id"]:
            await interaction.response.send_message("Only the seller can notify the buyer.", ephemeral=True)
            return

        buyer_id = self.listing_data.get("buyer_id")
        if not buyer_id:
            await interaction.response.send_message("This claim does not have a buyer attached.", ephemeral=True)
            return

        buyer = await self.bot.hydrate_user(
            buyer_id,
            self.listing_data.get("buyer_name"),
            fetch=True,
        )
        sent = await self.bot.safe_dm_user(
            buyer,
            embed=discord.Embed(
                title="Claim Follow-Up",
                description=(
                    f"The seller is checking in on your claim for "
                    f"{self.listing_data.get('player_names', 'this card')}.\n\n"
                    f"Seller: <@{self.listing_data.get('seller_id')}>\n"
                    f"Price: ${float(self.listing_data.get('price') or 0):.2f}\n\n"
                    "Please coordinate payment or delivery details with the seller."
                ),
                color=discord.Color.gold(),
            ),
        )
        if sent:
            log_marketplace_event(
                self.db,
                "claim_buyer_notified",
                user_id=interaction.user.id,
                listing_id=self.listing_data.get("message_id"),
                details={"buyer_id": buyer_id},
            )
            await interaction.response.send_message("Buyer notified.", ephemeral=True)
        else:
            await interaction.response.send_message("I could not DM the buyer.", ephemeral=True)


class NBACollectBot(discord.Client):
    def __init__(
        self,
        *,
        sheet: PriceSheet,
        db: CardDatabase,
        sale_channel_id: int = None,
        auction_channel_id: int = None,
        listing_surface_channel_id: int = None,
        auction_surface_channel_id: int = None,
        config: dict = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.sheet = sheet
        self.db = db
        self.sale_channel_id = sale_channel_id
        self.auction_channel_id = auction_channel_id or auction_surface_channel_id or sale_channel_id
        self.listing_surface_channel_id = listing_surface_channel_id
        self.auction_surface_channel_id = auction_surface_channel_id
        self.config = config or {}
        self.active_listings: Dict[int, Dict[str, Any]] = {}
        self.notify_rules = []
        self._hydrated_users: Dict[int, discord.abc.User] = {}
        self._marketplace_state_restored = False
        self._claim_reminder_task = None
        self._seller_reconciliation_task = None
        self._bot_interface_message_id = None
        self.bot_interface_view_factory = None
        self._slash_cleanup_complete = False
        self._marketplace_locks: Dict[int, asyncio.Lock] = {}
        # This tree exists only to clear commands registered by older releases.
        self.command_tree = app_commands.CommandTree(self)

    def marketplace_lock(self, listing_id: int | str | None) -> asyncio.Lock:
        """Return the process-local lock for one marketplace listing."""
        key = int(listing_id or 0)
        lock = self._marketplace_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._marketplace_locks[key] = lock
        return lock

    async def remove_legacy_slash_commands(self) -> None:
        """Remove global and guild slash commands left behind by older releases."""
        if self._slash_cleanup_complete:
            return
        try:
            self.command_tree.clear_commands(guild=None)
            await self.command_tree.sync()
            for guild in self.guilds:
                self.command_tree.clear_commands(guild=guild)
                await self.command_tree.sync(guild=guild)
        except discord.DiscordException:
            LOGGER.exception("Could not remove all legacy slash commands; cleanup will retry on reconnect.")
            return
        self._slash_cleanup_complete = True
        LOGGER.info("Legacy slash commands removed globally and from %s guild(s).", len(self.guilds))

    async def remove_seller_listings(
        self,
        seller_id: int,
        guild_id: int,
        *,
        event_type: str,
    ) -> int:
        """Remove a seller's non-final listings belonging to one guild."""
        removable_statuses = {"active", "open", "claimed", "pending"}
        listings = [
            listing
            for listing in list(self.active_listings.values())
            if int(listing.get("seller_id") or 0) == int(seller_id)
            and (
                int(listing.get("guild_id") or 0) == int(guild_id)
                or (not listing.get("guild_id") and len(self.guilds) == 1)
            )
            and str(listing.get("status", "active")).lower() in removable_statuses
        ]
        removed = 0
        for listing in listings:
            listing_id = listing.get("message_id")
            if not listing_id:
                continue
            async with self.marketplace_lock(int(listing_id)):
                transitioned = self.db.transition_marketplace_listing(
                    int(listing_id),
                    expected_statuses=removable_statuses,
                    new_status="removed",
                    resolution_reason="Seller left the server",
                )
                if not transitioned:
                    continue
                listing["status"] = "removed"
                self.active_listings.pop(int(listing_id), None)
                removed += 1
            await self.delete_listing_messages(listing)
            if listing.get("deal_thread_id"):
                await self.close_deal_thread_action_messages(
                    listing,
                    content="This deal was closed because the seller is no longer in the server.",
                )
                await self.delete_deal_thread(
                    listing,
                    reason="Marketplace seller left server",
                )
            log_marketplace_event(
                self.db,
                event_type,
                user_id=seller_id,
                listing_id=listing_id,
                details={
                    "guild_id": guild_id,
                    "player_names": listing.get("player_names"),
                },
            )
        if removed:
            LOGGER.info("Removed %s listing(s) for absent member %s.", removed, seller_id)
        return removed

    async def remove_departed_member_listings(self, member: discord.Member) -> int:
        """Remove listings when Discord reports that their seller left."""
        return await self.remove_seller_listings(
            member.id,
            member.guild.id,
            event_type="listing_removed_member_left",
        )

    async def reconcile_absent_seller_listings(self) -> int:
        """Remove listings whose sellers are no longer members of their guild."""
        seller_guild_pairs = set()
        for listing in self.active_listings.values():
            seller_id = listing.get("seller_id")
            guild_id = listing.get("guild_id")
            if not guild_id and len(self.guilds) == 1:
                guild_id = self.guilds[0].id
            if seller_id and guild_id:
                seller_guild_pairs.add((int(seller_id), int(guild_id)))

        removed = 0
        for seller_id, guild_id in seller_guild_pairs:
            guild = self.get_guild(guild_id)
            if guild is None:
                LOGGER.warning("Cannot verify listing seller %s: guild %s is unavailable.", seller_id, guild_id)
                continue
            try:
                await guild.fetch_member(seller_id)
            except discord.NotFound:
                removed += await self.remove_seller_listings(
                    seller_id,
                    guild_id,
                    event_type="listing_removed_absent_member",
                )
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Could not verify whether listing seller %s belongs to guild %s; listings preserved.",
                    seller_id,
                    guild_id,
                )
        LOGGER.info("Startup seller reconciliation removed %s listing(s).", removed)
        return removed

    def start_claim_reminder_loop(self) -> None:
        if self._claim_reminder_task and not self._claim_reminder_task.done():
            return
        self._claim_reminder_task = asyncio.create_task(self.claim_reminder_loop())

    def start_seller_reconciliation_loop(self) -> None:
        if self._seller_reconciliation_task and not self._seller_reconciliation_task.done():
            return
        self._seller_reconciliation_task = asyncio.create_task(self.seller_reconciliation_loop())

    async def seller_reconciliation_loop(self) -> None:
        """Periodically remove listings for sellers who are no longer members."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(SELLER_RECONCILIATION_INTERVAL_SECONDS)
            try:
                await self.reconcile_absent_seller_listings()
            except Exception:
                LOGGER.exception("Seller membership reconciliation failed")

    async def claim_reminder_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.send_claim_reminders_once()
                await self.send_auction_end_reminders_once()
            except Exception:
                LOGGER.exception("Claim reminder loop failed")
            await asyncio.sleep(CLAIM_REMINDER_INTERVAL_SECONDS)

    def _active_marketplace_listings(self) -> list[dict]:
        listings = [
            listing
            for listing in (self.active_listings or {}).values()
            if str(listing.get("status", "active")).lower() in {"active", "open"}
        ]
        listings.sort(key=_listing_newest_sort_key, reverse=True)
        return listings

    def build_bot_interface_view(self) -> ui.View | None:
        if self.bot_interface_view_factory is None:
            return None
        return self.bot_interface_view_factory()

    async def _find_interface_message(self, channel, marker_title: str):
        async for message in channel.history(limit=25):
            if self.user is not None and getattr(message.author, "id", None) != self.user.id:
                continue
            content = message.content.strip()
            if content == marker_title or content.startswith(f"**{marker_title}**"):
                return message
            if marker_title == "NBA Bot" and any(
                getattr(component, "custom_id", None) == "nba_bot:list_player"
                for component in getattr(message, "components", [])
                for component in getattr(component, "children", [])
            ):
                return message
            for embed in message.embeds:
                if embed.title == marker_title:
                    return message
        return None

    async def resolve_nba_bot_channel(self):
        channel_id = self.config.get("discord_bot_channel_id")
        if channel_id:
            return await self.fetch_channel_safely(channel_id)

        channel_name = str(self.config.get("discord_bot_channel_name") or "nba-bot").lstrip("#")
        for guild in self.guilds:
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel is not None:
                return channel
        return None

    async def ensure_nba_bot_interfaces(self) -> None:
        if not (self.config.get("discord_bot_channel_id") or self.config.get("discord_bot_channel_name")):
            return
        channel = await self.resolve_nba_bot_channel()
        if channel is None:
            LOGGER.warning("Configured NBA bot channel could not be found.")
            return

        bot_message_id = self.db.get_metadata("nba_bot_interface_message_id")
        bot_message = await self.fetch_listing_message(channel.id, bot_message_id) if bot_message_id else None
        if bot_message in (None, UNAVAILABLE_MESSAGE):
            bot_message = await self._find_interface_message(channel, "NBA Bot")
        if bot_message is None:
            bot_message = await channel.send(
                content=NBA_BOT_INTERFACE_CONTENT,
                view=self.build_bot_interface_view(),
            )
        else:
            await bot_message.edit(
                content=NBA_BOT_INTERFACE_CONTENT,
                embed=None,
                view=self.build_bot_interface_view(),
            )
        self._bot_interface_message_id = bot_message.id
        self.db.set_metadata("nba_bot_interface_message_id", str(bot_message.id))
        await self.cleanup_legacy_marketplace_interface(channel)

    async def cleanup_legacy_marketplace_interface(self, channel) -> None:
        legacy_message_id = self.db.get_metadata("nba_bot_marketplace_message_id")
        legacy_message = await self.fetch_listing_message(channel.id, legacy_message_id) if legacy_message_id else None
        if legacy_message in (None, UNAVAILABLE_MESSAGE):
            legacy_message = await self._find_interface_message(channel, "Marketplace")
        if legacy_message in (None, UNAVAILABLE_MESSAGE):
            return
        try:
            await legacy_message.delete()
            LOGGER.info("Deleted legacy marketplace interface message %s.", legacy_message.id)
        except discord.NotFound:
            pass
        except discord.Forbidden:
            LOGGER.warning("Could not delete legacy marketplace interface message %s: missing permissions.", legacy_message.id)
        except discord.DiscordException:
            LOGGER.exception("Could not delete legacy marketplace interface message %s.", legacy_message.id)

    def _claim_reminder_sent(self, listing_id: int, stage: str) -> bool:
        stage_order = {
            "seller_6h": 1,
            "seller_24h": 2,
            "mod_48h": 3,
        }
        requested_order = stage_order.get(stage, 0)
        rows = self.db.conn.execute(
            """
            SELECT details
            FROM marketplace_events
            WHERE event_type = 'claim_reminder_sent'
              AND listing_id = ?
            """,
            (int(listing_id),),
        ).fetchall()
        for row in rows:
            try:
                details = json.loads(row["details"] or "{}")
            except (TypeError, ValueError):
                continue
            sent_stage = details.get("stage")
            if sent_stage == stage:
                return True
            if stage_order.get(sent_stage, 0) >= requested_order:
                return True
        return False

    async def send_claim_reminders_once(self) -> None:
        now = datetime.now(timezone.utc)
        rows = self.db.conn.execute(
            """
            SELECT *
            FROM marketplace_listings
            WHERE LOWER(status) = 'claimed'
              AND buyer_id IS NOT NULL
            ORDER BY updated_at
            """
        ).fetchall()

        for row in rows:
            listing = dict(row)
            listing_id = listing.get("message_id")
            claimed_at = self._parse_datetime(listing.get("updated_at")) or self._parse_datetime(listing.get("created_at"))
            if not listing_id or claimed_at is None:
                continue

            age = now - claimed_at
            stages = [
                ("seller_6h", timedelta(hours=6), "Claim Reminder"),
                ("seller_24h", timedelta(hours=24), "Claim Still Pending"),
                ("mod_48h", timedelta(hours=48), "Stale Claim Needs Review"),
            ]
            due_stages = [
                (stage, threshold, title)
                for stage, threshold, title in stages
                if age >= threshold and not self._claim_reminder_sent(listing_id, stage)
            ]
            if not due_stages:
                continue

            stage, _threshold, title = due_stages[-1]
            sent = False
            if stage.startswith("seller"):
                seller = await self.hydrate_user(
                    listing.get("seller_id"),
                    listing.get("seller_name"),
                    fetch=True,
                )
                buyer = await self.hydrate_user(
                    listing.get("buyer_id"),
                    listing.get("buyer_name"),
                    fetch=False,
                )
                listing["seller"] = seller
                listing["buyer"] = buyer
                embed = discord.Embed(
                    title=title,
                    description=(
                        f"Your claimed listing is still waiting to be recorded.\n\n"
                        f"Card: {listing.get('player_names', 'Unknown card')}\n"
                        f"Buyer: {getattr(buyer, 'mention', listing.get('buyer_name') or 'Unknown buyer')}\n"
                        f"Price: ${float(listing.get('price') or 0):.2f}\n\n"
                        "After the buyer marks payment sent and the card is delivered, use Confirm Transfer & Complete. "
                        "If payment was not received, coordinate with the buyer or use Cancel / Void Deal."
                    ),
                    color=discord.Color.gold(),
                )
                sent = await self.safe_dm_user(
                    seller,
                    embed=embed,
                    view=ClaimReminderView(self, self.db, listing),
                )
            else:
                mod_channel_id = self.config.get("mod_channel_id")
                channel = await self.fetch_channel_safely(mod_channel_id) if mod_channel_id else None
                if channel:
                    listing["seller"] = await self.hydrate_user(
                        listing.get("seller_id"), listing.get("seller_name"), fetch=False
                    )
                    listing["buyer"] = await self.hydrate_user(
                        listing.get("buyer_id"), listing.get("buyer_name"), fetch=False
                    )
                    await channel.send(
                        "Stale claimed listing needs review:\n"
                        f"Card: **{listing.get('player_names', 'Unknown card')}**\n"
                        f"Seller: <@{listing.get('seller_id')}>\n"
                        f"Buyer: <@{listing.get('buyer_id')}>\n"
                        f"Price: ${float(listing.get('claim_price') or listing.get('price') or 0):.2f}\n"
                        f"Claimed since: `{listing.get('claimed_at') or listing.get('updated_at')}`",
                        view=ClaimedListingView(self, self.db, listing),
                    )
                    sent = True

            if sent:
                log_marketplace_event(
                    self.db,
                    "claim_reminder_sent",
                    user_id=listing.get("seller_id"),
                    listing_id=listing_id,
                    details={
                        "stage": stage,
                        "age_hours": round(age.total_seconds() / 3600, 2),
                        "buyer_id": listing.get("buyer_id"),
                    },
                )

    async def send_auction_end_reminders_once(self) -> None:
        """Notify sellers once when an auction is ready to finalize."""
        now = datetime.now(timezone.utc)
        for listing in list(self.active_listings.values()):
            if listing.get("listing_type") != "auction" or str(listing.get("status", "")).lower() != "active":
                continue
            end_at = self._parse_datetime(listing.get("auction_end_at"))
            listing_id = listing.get("message_id")
            if not listing_id or end_at is None or end_at > now:
                continue
            already_sent = self.db.conn.execute(
                """
                SELECT 1 FROM marketplace_events
                WHERE event_type = 'auction_end_reminder_sent' AND listing_id = ?
                LIMIT 1
                """,
                (int(listing_id),),
            ).fetchone()
            if already_sent:
                continue
            seller = await self.hydrate_user(
                listing.get("seller_id"),
                listing.get("seller_name"),
                fetch=True,
            )
            sent = await self.safe_dm_user(
                seller,
                embed=discord.Embed(
                    title="Auction Ready to Finalize",
                    description=(
                        f"Your auction for **{listing.get('player_names', 'this card')}** has ended. "
                        "Use Finalize Auction to select the high bidder or close it with no winner."
                    ),
                    color=discord.Color.gold(),
                ),
                view=AuctionActionView(self, self.db, listing),
            )
            if sent:
                log_marketplace_event(
                    self.db,
                    "auction_end_reminder_sent",
                    user_id=listing.get("seller_id"),
                    listing_id=listing_id,
                )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        listing = self.active_listings.get(payload.message_id)
        if listing is not None:
            async with self.marketplace_lock(payload.message_id):
                transitioned = self.db.transition_marketplace_listing(
                    payload.message_id,
                    expected_statuses={"active", "open", "claimed", "pending"},
                    new_status="removed",
                    resolution_reason="Primary listing message deleted",
                )
                if not transitioned:
                    self.active_listings.pop(payload.message_id, None)
                    return
                listing["status"] = "removed"
                self.active_listings.pop(payload.message_id, None)
            await self.delete_listing_messages(listing)
            if listing.get("deal_thread_id"):
                await self.close_deal_thread_action_messages(
                    listing,
                    content="This deal was closed because its listing message was deleted.",
                )
                await self.delete_deal_thread(
                    listing,
                    reason="Marketplace listing message deleted",
                )
            log_marketplace_event(
                self.db,
                "listing_removed_message_deleted",
                user_id=listing.get("seller_id"),
                listing_id=payload.message_id,
            )
            return

        for candidate in self.active_listings.values():
            if int(candidate.get("surface_message_id") or 0) != int(payload.message_id):
                continue
            candidate["surface_message_id"] = None
            candidate["surface_channel_id"] = None
            status = str(candidate.get("status", "active")).lower()
            if status in {"claimed", "pending"}:
                view_cls = ClaimedListingView
            elif candidate.get("listing_type") == "auction":
                view_cls = AuctionActionView
            else:
                view_cls = ListingActionView
            if self.listing_surface_should_exist(candidate):
                await self.surface_listing_message(
                    candidate,
                    view=view_cls(self, self.db, candidate),
                )
            self.db.upsert_marketplace_listing(candidate)
            log_marketplace_event(
                self.db,
                "listing_surface_message_repaired" if candidate.get("surface_message_id") else "listing_surface_message_deleted",
                user_id=candidate.get("seller_id"),
                listing_id=candidate.get("message_id"),
                details={"deleted_message_id": payload.message_id},
            )
            break

    async def hydrate_user(self, user_id: int, fallback_name: str = None, *, fetch: bool = True):
        """Return a Discord user when needed, falling back to persisted user data.

        Startup restore can wire persistent views with StoredDiscordUser objects
        without calling Discord's user endpoint for every historical participant.
        """
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return StoredDiscordUser(0, fallback_name or str(user_id))

        cached_user = self._hydrated_users.get(user_id) or self.get_user(user_id)
        if cached_user is not None:
            self._hydrated_users[user_id] = cached_user
            return cached_user

        if not fetch:
            return StoredDiscordUser(user_id, fallback_name)

        try:
            user = await self.fetch_user(user_id)
            self._hydrated_users[user_id] = user
            return user
        except discord.DiscordException:
            return StoredDiscordUser(user_id, fallback_name)

    async def restore_marketplace_state(self) -> None:
        """Restore persisted listings and notify rules after a restart."""
        if self._marketplace_state_restored:
            return

        self.notify_rules = self.db.get_active_notify_rules()
        ended_auctions_restored = await self.log_ended_auctions_on_startup()
        restored_listings = 0
        repaired_listings = 0
        deferred_visible_repairs = 0
        removed_listings = 0
        restore_visible_messages = bool(
            self.config.get("restore_visible_listing_messages_on_startup", False)
        )
        for listing in self.db.get_open_marketplace_listings():
            seller = await self.hydrate_user(
                listing["seller_id"],
                listing.get("seller_name"),
                fetch=False,
            )
            listing["seller"] = seller
            if listing.get("buyer_id"):
                listing["buyer"] = await self.hydrate_user(
                    listing["buyer_id"],
                    listing.get("buyer_name"),
                    fetch=False,
                )

            message_id = int(listing["message_id"])
            status = str(listing.get("status", "active")).lower()
            primary_message = await self.fetch_listing_message(
                listing.get("channel_id"),
                message_id,
            )
            if primary_message is None:
                self.db.update_marketplace_listing_status(message_id, "removed")
                log_marketplace_event(
                    self.db,
                    "listing_removed",
                    user_id=listing.get("seller_id"),
                    listing_id=message_id,
                    details={"reason": "startup_missing_primary_message"},
                )
                removed_listings += 1
                continue

            if status in {"claimed", "pending"}:
                view_cls = ClaimedListingView
            elif listing.get("listing_type") == "auction":
                view_cls = AuctionActionView
            else:
                view_cls = ListingActionView

            surface_message_id = listing.get("surface_message_id")
            surface_message = None
            if surface_message_id:
                surface_message = await self.fetch_listing_message(
                    listing.get("surface_channel_id"),
                    surface_message_id,
                )

            stored_image_url = listing.get("image_url")
            stored_surface_image_url = listing.get("surface_image_url")
            image_repaired = False
            if status == "active" and restore_visible_messages:
                await self.ensure_listing_image_asset(listing)
            if (
                restore_visible_messages
                and
                status == "active"
                and (
                    not stored_image_url
                    or str(stored_image_url).startswith("attachment://")
                    or str(stored_surface_image_url or "").startswith("attachment://")
                )
            ):
                if await self.ensure_listing_image_url(listing):
                    self.db.upsert_marketplace_listing(listing)
                    repaired_listings += 1
                    image_repaired = True

            primary_missing_image = (
                status == "active"
                and (
                    not self.message_has_visible_image(primary_message)
                    or (
                        self.listing_image_bytes(listing) is not None
                        and not self.message_uses_attachment_image(primary_message)
                    )
                )
            )
            surface_missing_image = (
                restore_visible_messages
                and
                status == "active"
                and self.listing_surface_should_exist(listing)
                and surface_message not in (None, UNAVAILABLE_MESSAGE)
                and (
                    not self.message_has_visible_image(surface_message)
                    or (
                        self.listing_image_bytes(listing) is not None
                        and not self.message_uses_attachment_image(surface_message)
                    )
                )
            )
            if restore_visible_messages and (image_repaired or primary_missing_image or surface_missing_image):
                resolved_image_url = await self.ensure_listing_image_url(listing)
                if resolved_image_url:
                    self.db.upsert_marketplace_listing(listing)
                    await self.edit_listing_messages(
                        listing,
                        embed=build_listing_embed(listing),
                        view=view_cls(self, self.db, listing),
                    )
                    if not image_repaired:
                        repaired_listings += 1
                    log_marketplace_event(
                        self.db,
                        "listing_image_repaired",
                        user_id=listing.get("seller_id"),
                        listing_id=message_id,
                        details={
                            "primary_missing_image": primary_missing_image,
                            "surface_missing_image": surface_missing_image,
                            "data_repaired": image_repaired,
                        },
                    )
            elif (
                not restore_visible_messages
                and status == "active"
                and primary_message not in (None, UNAVAILABLE_MESSAGE)
                and not self.message_has_visible_image(primary_message)
            ):
                deferred_visible_repairs += 1

            if (
                restore_visible_messages
                and
                status == "active"
                and self.listing_surface_should_exist(listing)
                and surface_message is None
            ):
                await self.ensure_listing_image_url(listing)
                listing["surface_message_id"] = None
                listing["surface_channel_id"] = None
                await self.surface_listing_message(
                    listing,
                    view=view_cls(self, self.db, listing),
                )
                if listing.get("surface_message_id"):
                    self.db.upsert_marketplace_listing(listing)
                    repaired_listings += 1
                else:
                    log_marketplace_event(
                        self.db,
                        "listing_repair_failed",
                        user_id=listing.get("seller_id"),
                        listing_id=message_id,
                        details={"reason": "startup_missing_surface_message"},
                        level=30,
                    )
            elif (
                status == "active"
                and self.listing_surface_should_exist(listing)
                and surface_message not in (None, UNAVAILABLE_MESSAGE)
                and not self.message_has_visible_image(surface_message)
                and restore_visible_messages
            ):
                resolved_image_url = await self.ensure_listing_image_url(listing)
                if resolved_image_url:
                    await self.edit_listing_messages(
                        listing,
                        embed=build_listing_embed(listing),
                        view=view_cls(self, self.db, listing),
                    )
                    self.db.upsert_marketplace_listing(listing)
                    log_marketplace_event(
                        self.db,
                        "listing_surface_image_repaired",
                        user_id=listing.get("seller_id"),
                        listing_id=message_id,
                        details={"surface_message_id": surface_message_id},
                    )

            self.active_listings[message_id] = listing
            self.add_view(view_cls(self, self.db, listing), message_id=message_id)
            if listing.get("surface_message_id"):
                self.add_view(
                    view_cls(self, self.db, listing),
                    message_id=int(listing["surface_message_id"]),
                )
            if status in {"claimed", "pending"}:
                await self.restore_deal_thread_action_view(listing)
            elif listing.get("deal_thread_id"):
                await self.close_deal_thread_action_messages(listing)
            if (
                status in {"active", "claimed", "pending"}
            ):
                await self.edit_listing_messages(
                    listing,
                    embed=build_listing_embed(
                        listing,
                        claimed=status in {"claimed", "pending"},
                    ),
                    view=view_cls(self, self.db, listing),
                )
            for bid in listing.get("bids", []):
                bidder = await self.hydrate_user(
                    bid.get("bidder_id") or bid.get("user_id"),
                    bid.get("username"),
                    fetch=False,
                )
                if str(bid.get("status", "placed")).lower() == "placed" and bid.get("dm_message_id"):
                    self.add_view(
                        AcceptBidView(
                            self,
                            self.db,
                            listing,
                            bidder,
                            float(bid.get("amount") or bid.get("bid_amount")),
                            bid,
                        ),
                        message_id=int(bid["dm_message_id"]),
                    )
                if (
                    str(bid.get("status", "")).lower() == "countered"
                    and str(bid.get("counter_status", "")).lower() == "pending"
                    and bid.get("counter_dm_message_id")
                ):
                    self.add_view(
                        CounterOfferView(
                            self,
                            self.db,
                            listing,
                            bidder,
                            bid,
                        ),
                        message_id=int(bid["counter_dm_message_id"]),
                    )
            restored_listings += 1

        self._marketplace_state_restored = True
        LOGGER.info(
            "Restored %s marketplace listing(s), repaired %s, deferred %s visible repair(s), removed %s stale listing(s), preserved %s ended auction(s), and restored %s notify rule(s).",
            restored_listings,
            repaired_listings,
            deferred_visible_repairs,
            removed_listings,
            ended_auctions_restored,
            len(self.notify_rules),
        )

    def _parse_datetime(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    async def log_ended_auctions_on_startup(self) -> int:
        """Record ended auctions that remain available for seller finalization."""
        now = datetime.now(timezone.utc)
        try:
            rows = self.db.conn.execute(
                """
                SELECT *
                FROM marketplace_listings
                WHERE listing_type = ?
                  AND LOWER(status) IN ('active', 'open', 'pending', 'claimed')
                ORDER BY created_at
                """,
                ("auction",),
            ).fetchall()
        except Exception:
            LOGGER.exception("Could not query ended auctions during startup")
            return 0

        ended_count = 0
        for row in rows:
            listing = dict(row)
            end_at = self._parse_datetime(listing.get("auction_end_at"))
            if end_at is None or end_at > now:
                continue

            ended_count += 1

            log_marketplace_event(
                self.db,
                "ended_auction_restored_for_finalization",
                user_id=listing.get("seller_id"),
                listing_id=listing.get("message_id"),
                details={
                    "player_names": listing.get("player_names"),
                    "auction_end_at": listing.get("auction_end_at"),
                    "context": "startup",
                },
            )

        return ended_count

    async def safe_dm_user(self, user: discord.abc.User, *, content: str = None, embed: discord.Embed = None, view: ui.View = None) -> bool:
        """Send a DM and return whether it succeeded."""
        if not hasattr(user, "send"):
            user = await self.hydrate_user(
                getattr(user, "id", None),
                getattr(user, "name", None),
                fetch=True,
            )
        if not hasattr(user, "send"):
            LOGGER.warning("Could not resolve DM-capable user %s", getattr(user, "id", user))
            return False

        try:
            await user.send(content=content, embed=embed, view=view)
            return True
        except discord.Forbidden:
            LOGGER.warning("Could not DM user %s", getattr(user, "name", user))
            log_marketplace_event(
                self.db,
                "dm_failed",
                user_id=getattr(user, "id", None),
                details={"username": getattr(user, "name", str(user))},
                level=30,
            )
            return False
        except discord.DiscordException as exc:
            LOGGER.warning("Could not DM user %s: %s", getattr(user, "name", user), exc)
            log_marketplace_event(
                self.db,
                "dm_failed",
                user_id=getattr(user, "id", None),
                details={"username": getattr(user, "name", str(user)), "reason": type(exc).__name__},
                level=30,
            )
            return False

    async def fetch_channel_safely(self, channel_id: int | str):
        if not channel_id:
            return None

        channel_id = int(channel_id)
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel

        try:
            return await self.fetch_channel(channel_id)
        except discord.DiscordException:
            LOGGER.warning("Could not fetch channel %s", channel_id)
            return None

    async def fetch_listing_message(self, channel_id: int | str, message_id: int | str):
        """Fetch a listing message.

        Returns None only when Discord confirms the message is gone. A sentinel is
        returned for transient/unreachable states so startup cleanup does not
        archive valid listings during an API or permission hiccup.
        """
        if not channel_id or not message_id:
            return None
        channel = await self.fetch_channel_safely(channel_id)
        if channel is None:
            return UNAVAILABLE_MESSAGE
        try:
            return await channel.fetch_message(int(message_id))
        except discord.NotFound:
            return None
        except discord.DiscordException:
            LOGGER.warning("Could not fetch listing message %s", message_id)
            return UNAVAILABLE_MESSAGE

    def message_has_claimed_listing_actions(self, message: discord.Message) -> bool:
        for row in getattr(message, "components", []) or []:
            for child in getattr(row, "children", []) or []:
                if getattr(child, "custom_id", None) in {"claimed:record", "claimed:cancel"}:
                    return True
        return False

    async def close_deal_thread_action_messages(
        self,
        listing_data: Dict[str, Any],
        *,
        content: str = "These claim actions are no longer active.",
    ) -> int:
        """Disable claimed-listing action buttons in a listing's private deal thread."""
        thread_id = listing_data.get("deal_thread_action_channel_id") or listing_data.get("deal_thread_id")
        if not thread_id:
            return 0

        closed = 0
        seen_message_ids = set()
        action_message_id = listing_data.get("deal_thread_action_message_id")
        if action_message_id:
            message = await self.fetch_listing_message(thread_id, action_message_id)
            if message not in (None, UNAVAILABLE_MESSAGE) and self.message_has_claimed_listing_actions(message):
                try:
                    await message.edit(content=content, view=DisabledClaimedListingActionView())
                    closed += 1
                    seen_message_ids.add(int(action_message_id))
                except discord.DiscordException:
                    LOGGER.warning("Could not close deal thread action message %s", action_message_id)

        thread = await self.fetch_channel_safely(listing_data.get("deal_thread_id") or thread_id)
        if thread is None or not hasattr(thread, "history"):
            return closed

        try:
            async for message in thread.history(limit=50):
                if int(message.id) in seen_message_ids:
                    continue
                if self.user is not None and getattr(message.author, "id", None) != self.user.id:
                    continue
                if not self.message_has_claimed_listing_actions(message):
                    continue
                await message.edit(content=content, view=DisabledClaimedListingActionView())
                closed += 1
        except discord.DiscordException:
            LOGGER.warning("Could not scan deal thread %s for stale claim actions", getattr(thread, "id", thread))

        if closed:
            log_marketplace_event(
                self.db,
                "deal_thread_actions_closed",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"thread_id": getattr(thread, "id", thread_id), "closed_count": closed},
            )
        return closed

    async def delete_deal_thread(
        self,
        listing_data: Dict[str, Any],
        *,
        reason: str = "Marketplace deal closed",
    ) -> bool:
        """Delete a listing's private deal thread and clear its stored metadata."""
        thread_id = listing_data.get("deal_thread_id")
        if not thread_id:
            return False

        thread = await self.fetch_channel_safely(thread_id)
        deleted = False
        if thread is None:
            deleted = True
        elif not isinstance(thread, discord.Thread):
            LOGGER.warning("Stored deal thread id %s is not a thread", thread_id)
            log_marketplace_event(
                self.db,
                "deal_thread_delete_failed",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"thread_id": thread_id, "reason": "not_thread"},
                level=30,
            )
            return False
        else:
            try:
                await thread.delete(reason=reason)
                deleted = True
            except discord.NotFound:
                deleted = True
            except discord.DiscordException as exc:
                LOGGER.warning("Could not delete deal thread %s: %s", thread_id, exc)
                log_marketplace_event(
                    self.db,
                    "deal_thread_delete_failed",
                    user_id=listing_data.get("seller_id"),
                    listing_id=listing_data.get("message_id"),
                    details={"thread_id": thread_id, "reason": type(exc).__name__},
                    level=30,
                )
                return False

        if deleted:
            for key in (
                "deal_thread_id",
                "deal_thread_parent_channel_id",
                "deal_thread_action_channel_id",
                "deal_thread_action_message_id",
            ):
                listing_data.pop(key, None)
            if hasattr(self.db, "clear_marketplace_listing_deal_thread"):
                self.db.clear_marketplace_listing_deal_thread(listing_data.get("message_id"))
            log_marketplace_event(
                self.db,
                "deal_thread_deleted",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"thread_id": thread_id, "reason": reason},
            )
        return deleted

    async def restore_deal_thread_action_view(self, listing_data: Dict[str, Any]) -> bool:
        """Register the private deal thread's claimed-listing action view."""
        thread_id = listing_data.get("deal_thread_action_channel_id") or listing_data.get("deal_thread_id")
        action_message_id = listing_data.get("deal_thread_action_message_id")
        if not thread_id:
            return False

        if action_message_id:
            message = await self.fetch_listing_message(thread_id, action_message_id)
            if message not in (None, UNAVAILABLE_MESSAGE) and self.message_has_claimed_listing_actions(message):
                view = ClaimedListingView(self, self.db, listing_data)
                self.add_view(view, message_id=int(action_message_id))
                return True

        thread = await self.fetch_channel_safely(listing_data.get("deal_thread_id") or thread_id)
        if thread is None or not hasattr(thread, "history"):
            return False

        try:
            async for message in thread.history(limit=50):
                if self.user is not None and getattr(message.author, "id", None) != self.user.id:
                    continue
                if not self.message_has_claimed_listing_actions(message):
                    continue

                listing_data["deal_thread_action_channel_id"] = getattr(message.channel, "id", None)
                listing_data["deal_thread_action_message_id"] = message.id
                self.db.upsert_marketplace_listing(listing_data)
                view = ClaimedListingView(self, self.db, listing_data)
                await message.edit(view=view)
                self.add_view(ClaimedListingView(self, self.db, listing_data), message_id=message.id)
                return True
        except discord.DiscordException:
            LOGGER.warning("Could not restore deal thread action view for listing %s", listing_data.get("message_id"))
        return False

    def hydrate_listing_message_links(self, listing_data: Dict[str, Any]) -> None:
        """Fill missing primary/surface message IDs from persisted marketplace state."""
        message_ids = [
            listing_data.get("message_id"),
            listing_data.get("surface_message_id"),
        ]
        message_ids = [int(message_id) for message_id in message_ids if message_id]
        if not message_ids:
            return

        placeholders = ",".join("?" for _ in message_ids)
        try:
            row = self.db.conn.execute(
                f"""
                SELECT message_id, channel_id, surface_message_id, surface_channel_id
                FROM marketplace_listings
                WHERE message_id IN ({placeholders})
                   OR surface_message_id IN ({placeholders})
                LIMIT 1
                """,
                (*message_ids, *message_ids),
            ).fetchone()
        except Exception:
            LOGGER.exception("Could not hydrate listing message links")
            return
        if not row:
            return

        for key in ("message_id", "channel_id", "surface_message_id", "surface_channel_id"):
            if row[key] and not listing_data.get(key):
                listing_data[key] = row[key]

    def message_has_visible_image(self, message: discord.Message) -> bool:
        if message in (None, UNAVAILABLE_MESSAGE):
            return False
        image_attachment_filenames = {
            attachment.filename
            for attachment in getattr(message, "attachments", [])
            if self.message_attachment_is_image(attachment)
        }
        for embed in getattr(message, "embeds", []):
            image_url = getattr(getattr(embed, "image", None), "url", None)
            thumbnail_url = getattr(getattr(embed, "thumbnail", None), "url", None)
            if image_url and not str(image_url).startswith("attachment://"):
                return True
            if thumbnail_url and not str(thumbnail_url).startswith("attachment://"):
                return True
            if image_url and str(image_url).startswith("attachment://"):
                filename = str(image_url).removeprefix("attachment://")
                if image_attachment_filenames and filename in image_attachment_filenames:
                    return True
            if thumbnail_url and str(thumbnail_url).startswith("attachment://"):
                filename = str(thumbnail_url).removeprefix("attachment://")
                if image_attachment_filenames and filename in image_attachment_filenames:
                    return True
        return False

    def message_uses_attachment_image(self, message: discord.Message) -> bool:
        if message in (None, UNAVAILABLE_MESSAGE):
            return False
        image_attachment_filenames = {
            attachment.filename
            for attachment in getattr(message, "attachments", [])
            if self.message_attachment_is_image(attachment)
        }
        if not image_attachment_filenames:
            return False
        for embed in getattr(message, "embeds", []):
            image_url = getattr(getattr(embed, "image", None), "url", None)
            if image_url and str(image_url).startswith("attachment://"):
                filename = str(image_url).removeprefix("attachment://")
                if filename in image_attachment_filenames:
                    return True
        return False

    def message_attachment_is_image(self, attachment: discord.Attachment) -> bool:
        content_type = str(getattr(attachment, "content_type", "") or "").lower()
        if content_type.startswith("image/"):
            return True
        filename = str(getattr(attachment, "filename", "") or "").lower()
        return filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))

    def listing_image_bytes(self, listing_data: Dict[str, Any]) -> bytes | None:
        image_bytes = listing_data.get("image_bytes")
        if image_bytes is None:
            image_bytes = listing_data.get("image_blob")
        if image_bytes is None:
            return None
        if isinstance(image_bytes, memoryview):
            image_bytes = image_bytes.tobytes()
        if isinstance(image_bytes, bytearray):
            image_bytes = bytes(image_bytes)
        if not isinstance(image_bytes, bytes) or not image_bytes:
            return None
        listing_data["image_bytes"] = image_bytes
        listing_data["image_blob"] = image_bytes
        return image_bytes

    def listing_image_filename(self, listing_data: Dict[str, Any]) -> str:
        filename = str(listing_data.get("image_filename") or "card_image.png").strip()
        filename = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return filename or "card_image.png"

    def build_message_image_embed(
        self,
        listing_data: Dict[str, Any],
        *,
        embed: discord.Embed = None,
        attachment_filename: str = None,
    ) -> discord.Embed:
        target_embed = embed.copy() if embed is not None else build_listing_embed(listing_data)
        target_embed.set_image(url=f"attachment://{attachment_filename or self.listing_image_filename(listing_data)}")
        return target_embed

    async def build_listing_message_kwargs(
        self,
        listing_data: Dict[str, Any],
        *,
        embed: discord.Embed = None,
    ) -> Dict[str, Any]:
        """Build send/edit kwargs that keep listing images attached when needed."""
        image_url = await self.ensure_listing_image_url(listing_data)
        target_embed = embed.copy() if embed is not None else build_listing_embed(listing_data)
        if image_url and not str(image_url).startswith("attachment://"):
            target_embed.set_image(url=image_url)
            return {"embed": target_embed}

        image_bytes = self.listing_image_bytes(listing_data)
        if image_bytes is None:
            return {"embed": target_embed}

        filename = self.listing_image_filename(listing_data)
        target_embed.set_image(url=f"attachment://{filename}")
        return {
            "embed": target_embed,
            "file": discord.File(io.BytesIO(image_bytes), filename=filename),
        }

    async def safe_dm_listing(
        self,
        user: discord.abc.User,
        listing_data: Dict[str, Any],
        *,
        content: str = None,
    ) -> bool:
        """DM a listing embed, including its image attachment when required."""
        if not hasattr(user, "send"):
            user = await self.hydrate_user(
                getattr(user, "id", None),
                getattr(user, "name", None),
                fetch=True,
            )
        if not hasattr(user, "send"):
            LOGGER.warning("Could not resolve DM-capable user %s", getattr(user, "id", user))
            return False

        try:
            kwargs = await self.build_listing_message_kwargs(listing_data)
            await user.send(content=content, **kwargs)
            return True
        except discord.Forbidden:
            LOGGER.warning("Could not DM listing to user %s", getattr(user, "name", user))
            log_marketplace_event(
                self.db,
                "dm_failed",
                user_id=getattr(user, "id", None),
                details={"username": getattr(user, "name", str(user)), "context": "listing_context"},
                level=30,
            )
            return False
        except discord.DiscordException as exc:
            LOGGER.warning("Could not send listing DM: %s", exc)
            return False

    async def create_deal_thread(
        self,
        listing_data: Dict[str, Any],
        seller: discord.abc.User,
        buyer: discord.abc.User,
        *,
        reason: str = "Marketplace deal started",
    ) -> discord.Thread | None:
        """Create a private seller/buyer thread in the listing channel."""
        existing_thread_id = listing_data.get("deal_thread_id")
        if existing_thread_id:
            existing_thread = await self.fetch_channel_safely(existing_thread_id)
            if isinstance(existing_thread, discord.Thread):
                return existing_thread

        parent_channel = await self.fetch_channel_safely(listing_data.get("channel_id"))
        if parent_channel is None or not hasattr(parent_channel, "create_thread"):
            log_marketplace_event(
                self.db,
                "deal_thread_failed",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"reason": "listing_channel_not_found"},
                level=30,
            )
            return None

        player = str(listing_data.get("player_names") or "Listing").strip()
        thread_name = f"Deal - {player}"[:100]
        try:
            thread = await parent_channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                invitable=True,
                auto_archive_duration=10080,
                reason=reason,
            )
        except discord.Forbidden:
            LOGGER.warning("Missing permissions to create private deal thread for listing %s", listing_data.get("message_id"))
            log_marketplace_event(
                self.db,
                "deal_thread_failed",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"reason": "forbidden"},
                level=30,
            )
            return None
        except discord.DiscordException as exc:
            LOGGER.warning("Could not create private deal thread for listing %s: %s", listing_data.get("message_id"), exc)
            log_marketplace_event(
                self.db,
                "deal_thread_failed",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"reason": type(exc).__name__, "message": str(exc)},
                level=30,
            )
            return None

        failed_members = []
        for user in (seller, buyer):
            try:
                await thread.add_user(user)
            except discord.DiscordException as exc:
                failed_members.append(user)
                LOGGER.warning("Could not add user %s to deal thread %s: %s", getattr(user, "id", user), thread.id, exc)
                log_marketplace_event(
                    self.db,
                    "deal_thread_member_add_failed",
                    user_id=getattr(user, "id", None),
                    listing_id=listing_data.get("message_id"),
                    details={"thread_id": thread.id, "reason": type(exc).__name__},
                    level=30,
                )

        if failed_members:
            for user in failed_members:
                await self.safe_dm_listing(
                    user,
                    listing_data,
                    content=(
                        "A private deal thread was created, but I could not add you to it. "
                        f"Contact the other participant directly: {_mention_user(buyer if user is seller else seller)}"
                    ),
                )

        try:
            await thread.edit(invitable=False, reason="Lock marketplace deal thread invites")
        except discord.DiscordException:
            LOGGER.warning("Could not lock invites for deal thread %s", thread.id)

        listing_data["deal_thread_id"] = thread.id
        listing_data["deal_thread_parent_channel_id"] = getattr(parent_channel, "id", None)
        self.db.upsert_marketplace_listing(listing_data)

        listing_message = await self.fetch_listing_message(
            listing_data.get("channel_id"),
            listing_data.get("message_id"),
        )
        listing_link = getattr(listing_message, "jump_url", None)
        content = (
            f"Private deal thread for {_mention_user(seller)} and {_mention_user(buyer)}."
        )
        if listing_link:
            content = f"{content}\nOriginal listing: {listing_link}"
        try:
            kwargs = await self.build_listing_message_kwargs(listing_data)
            await thread.send(content=content, **kwargs)
        except discord.DiscordException as exc:
            LOGGER.warning("Could not send listing into deal thread %s: %s", thread.id, exc)
            log_marketplace_event(
                self.db,
                "deal_thread_listing_send_failed",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"thread_id": thread.id, "reason": type(exc).__name__},
                level=30,
            )

        log_marketplace_event(
            self.db,
            "deal_thread_created",
            user_id=listing_data.get("seller_id"),
            listing_id=listing_data.get("message_id"),
            details={
                "thread_id": thread.id,
                "buyer_id": getattr(buyer, "id", None),
                "seller_id": getattr(seller, "id", None),
            },
        )
        return thread

    async def capture_listing_message_image(
        self,
        listing_data: Dict[str, Any],
        message: discord.Message,
        *,
        surface: bool = False,
    ) -> bytes | None:
        """Persist the image attached to a listing message as the canonical asset."""
        attachment = next(
            (
                candidate
                for candidate in getattr(message, "attachments", [])
                if self.message_attachment_is_image(candidate)
            ),
            None,
        )
        if attachment is None:
            return self.listing_image_bytes(listing_data)

        if surface:
            listing_data["surface_image_url"] = attachment.url
        else:
            listing_data["image_url"] = attachment.url
        listing_data["image_filename"] = attachment.filename or self.listing_image_filename(listing_data)
        if attachment.content_type:
            listing_data["image_content_type"] = attachment.content_type

        image_bytes = self.listing_image_bytes(listing_data)
        if image_bytes is None:
            try:
                image_bytes = await attachment.read()
            except discord.DiscordException:
                LOGGER.warning("Could not persist listing image attachment %s", attachment.url)
                return None
            listing_data["image_bytes"] = image_bytes
            listing_data["image_blob"] = image_bytes

        self.db.upsert_marketplace_listing(listing_data)
        return image_bytes

    async def ensure_listing_image_asset(self, listing_data: Dict[str, Any]) -> bytes | None:
        """Ensure listing_data carries the persisted image bytes whenever possible."""
        image_bytes = self.listing_image_bytes(listing_data)
        if image_bytes is not None:
            return image_bytes

        targets = [
            ("surface", listing_data.get("surface_channel_id"), listing_data.get("surface_message_id")),
            ("primary", listing_data.get("channel_id"), listing_data.get("message_id")),
        ]
        for source, channel_id, message_id in targets:
            message = await self.fetch_listing_message(channel_id, message_id)
            if message in (None, UNAVAILABLE_MESSAGE):
                continue
            image_bytes = await self.capture_listing_message_image(
                listing_data,
                message,
                surface=source == "surface",
            )
            if image_bytes is not None:
                log_marketplace_event(
                    self.db,
                    "listing_image_asset_persisted",
                    user_id=listing_data.get("seller_id"),
                    listing_id=listing_data.get("message_id"),
                    details={"source": source},
                )
                return image_bytes
        return None

    async def ensure_listing_image_url(self, listing_data: Dict[str, Any]) -> str | None:
        """Resolve attachment placeholders into reusable Discord CDN URLs."""
        image_bytes = await self.ensure_listing_image_asset(listing_data)
        image_url = listing_data.get("image_url")
        surface_image_url = listing_data.get("surface_image_url")
        if image_url and not str(image_url).startswith("attachment://"):
            return image_url
        if surface_image_url and not str(surface_image_url).startswith("attachment://"):
            listing_data["image_url"] = surface_image_url
            return surface_image_url
        if image_bytes is not None:
            return f"attachment://{self.listing_image_filename(listing_data)}"

        targets = [
            ("surface", listing_data.get("surface_channel_id"), listing_data.get("surface_message_id")),
            ("primary", listing_data.get("channel_id"), listing_data.get("message_id")),
        ]
        for source, channel_id, message_id in targets:
            message = await self.fetch_listing_message(channel_id, message_id)
            if message in (None, UNAVAILABLE_MESSAGE):
                continue

            resolved_url = None
            if message.attachments:
                resolved_url = message.attachments[0].url
            elif message.embeds:
                embed = message.embeds[0]
                if embed.image and embed.image.url and not str(embed.image.url).startswith("attachment://"):
                    resolved_url = embed.image.url
                elif embed.thumbnail and embed.thumbnail.url and not str(embed.thumbnail.url).startswith("attachment://"):
                    resolved_url = embed.thumbnail.url

            if not resolved_url:
                continue

            listing_data["image_url"] = resolved_url
            if source == "surface":
                listing_data["surface_image_url"] = resolved_url
            self.db.upsert_marketplace_listing(listing_data)
            log_marketplace_event(
                self.db,
                "listing_image_resolved",
                user_id=listing_data.get("seller_id"),
                listing_id=listing_data.get("message_id"),
                details={"source": source},
            )
            return resolved_url

        log_marketplace_event(
            self.db,
            "listing_image_resolve_failed",
            user_id=listing_data.get("seller_id"),
            listing_id=listing_data.get("message_id"),
            details={
                "has_image_url": bool(listing_data.get("image_url")),
                "has_surface_image_url": bool(listing_data.get("surface_image_url")),
            },
            level=30,
        )
        return None

    def listing_surface_should_exist(self, listing_data: Dict[str, Any]) -> bool:
        """Return whether this listing should have a public surface-channel mirror."""
        is_auction = listing_data.get("listing_type") == "auction"
        surface_channel_id = (
            self.auction_surface_channel_id
            if is_auction
            else self.listing_surface_channel_id
        )
        if not surface_channel_id:
            return False
        primary_channel_id = listing_data.get("channel_id")
        return not primary_channel_id or int(primary_channel_id) != int(surface_channel_id)

    async def surface_listing_message(self, listing_data: Dict[str, Any], view: ui.View = None) -> None:
        """Mirror a listing into the optional listing or auction surface channel."""
        is_auction = listing_data.get("listing_type") == "auction"
        surface_channel_id = (
            self.auction_surface_channel_id
            if is_auction
            else self.listing_surface_channel_id
        )
        if not surface_channel_id:
            return

        primary_channel_id = listing_data.get("channel_id")
        if primary_channel_id and int(primary_channel_id) == int(surface_channel_id):
            return

        channel = await self.fetch_channel_safely(surface_channel_id)
        if channel is None:
            return

        try:
            image_bytes = await self.ensure_listing_image_asset(listing_data)
            if image_bytes:
                filename = self.listing_image_filename(listing_data)
                embed = self.build_message_image_embed(
                    listing_data,
                    attachment_filename=filename,
                )
                surface_message = await channel.send(
                    embed=embed,
                    view=view,
                    file=discord.File(io.BytesIO(image_bytes), filename=filename),
                )
                listing_data["surface_channel_id"] = surface_message.channel.id
                listing_data["surface_message_id"] = surface_message.id
                await self.capture_listing_message_image(listing_data, surface_message, surface=True)
                return

            await self.ensure_listing_image_url(listing_data)
            surface_message = await channel.send(
                embed=build_listing_embed(listing_data),
                view=view,
            )
        except discord.DiscordException:
            LOGGER.exception("Could not surface listing %s", listing_data.get("message_id"))
            return

        listing_data["surface_channel_id"] = surface_message.channel.id
        listing_data["surface_message_id"] = surface_message.id
        await self.capture_listing_message_image(listing_data, surface_message, surface=True)

    async def delete_listing_messages(self, listing_data: Dict[str, Any]) -> None:
        """Delete the primary listing message and optional surface-channel mirror."""
        self.hydrate_listing_message_links(listing_data)
        targets = [
            (listing_data.get("channel_id"), listing_data.get("message_id")),
            (listing_data.get("surface_channel_id"), listing_data.get("surface_message_id")),
        ]
        seen = set()
        for channel_id, message_id in targets:
            if not channel_id or not message_id:
                continue
            target_key = (int(channel_id), int(message_id))
            if target_key in seen:
                continue
            seen.add(target_key)
            channel = await self.fetch_channel_safely(channel_id)
            if channel is None:
                log_marketplace_event(
                    self.db,
                    "listing_message_delete_failed",
                    listing_id=listing_data.get("message_id"),
                    details={"channel_id": channel_id, "message_id": message_id, "reason": "channel_not_found"},
                    level=30,
                )
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
                log_marketplace_event(
                    self.db,
                    "listing_message_deleted",
                    listing_id=listing_data.get("message_id"),
                    details={"channel_id": channel_id, "message_id": message_id},
                )
            except discord.NotFound:
                pass
            except discord.DiscordException:
                LOGGER.warning("Could not delete listing message %s", message_id)
                log_marketplace_event(
                    self.db,
                    "listing_message_delete_failed",
                    listing_id=listing_data.get("message_id"),
                    details={"channel_id": channel_id, "message_id": message_id, "reason": "discord_exception"},
                    level=30,
                )

    async def edit_listing_messages(
        self,
        listing_data: Dict[str, Any],
        *,
        embed: discord.Embed,
        view: ui.View = None,
    ) -> None:
        """Edit the primary listing message and optional surface-channel mirror."""
        image_bytes = await self.ensure_listing_image_asset(listing_data)
        targets = [
            (listing_data.get("channel_id"), listing_data.get("message_id"), False),
            (listing_data.get("surface_channel_id"), listing_data.get("surface_message_id"), True),
        ]
        for channel_id, message_id, surface in targets:
            if not channel_id or not message_id:
                continue
            channel = await self.fetch_channel_safely(channel_id)
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(message_id)
                target_embed = embed
                image_attachment = next(
                    (
                        candidate
                        for candidate in getattr(message, "attachments", [])
                        if self.message_attachment_is_image(candidate)
                    ),
                    None,
                )
                if image_bytes is not None:
                    filename = (
                        image_attachment.filename
                        if image_attachment is not None
                        else self.listing_image_filename(listing_data)
                    )
                    target_embed = self.build_message_image_embed(
                        listing_data,
                        embed=embed,
                        attachment_filename=filename,
                    )
                    if image_attachment is None:
                        await message.edit(
                            embed=target_embed,
                            view=view,
                            attachments=[
                                discord.File(io.BytesIO(image_bytes), filename=filename),
                            ],
                        )
                        try:
                            message = await channel.fetch_message(message_id)
                        except discord.DiscordException:
                            pass
                    else:
                        await message.edit(embed=target_embed, view=view)
                    await self.capture_listing_message_image(listing_data, message, surface=surface)
                    continue

                surface_image_url = listing_data.get("surface_image_url")
                if (
                    surface
                    and str(listing_data.get("status", "active")).lower() == "active"
                    and surface_image_url
                    and not str(surface_image_url).startswith("attachment://")
                ):
                    target_embed = build_listing_embed({**listing_data, "image_url": surface_image_url})
                await message.edit(embed=target_embed, view=view)
            except discord.DiscordException:
                LOGGER.warning("Could not edit listing message %s", message_id)

    async def bump_listing_messages(
        self,
        listing_data: Dict[str, Any],
        *,
        old_price: float,
        new_price: float,
    ) -> bool:
        """Repost an active listing after a price change so it appears fresh."""
        old_primary_id = listing_data.get("message_id")
        old_primary_channel_id = listing_data.get("channel_id")
        old_surface_id = listing_data.get("surface_message_id")
        old_surface_channel_id = listing_data.get("surface_channel_id")
        reposted = False

        canonical_image_bytes = await self.ensure_listing_image_asset(listing_data)
        await self.ensure_listing_image_url(listing_data)

        def reusable_image_url(*urls: str | None) -> str | None:
            for url in urls:
                if url and not str(url).startswith("attachment://"):
                    return url
            return None

        async def image_payload_from_message(
            message: discord.Message,
            *,
            fallback_url: str = None,
            allow_fallback: bool = False,
        ) -> tuple[str | None, discord.File | None]:
            if message.attachments:
                attachment = message.attachments[0]
                filename = attachment.filename or "card_image.png"
                try:
                    image_bytes = await attachment.read()
                    return f"attachment://{filename}", discord.File(
                        io.BytesIO(image_bytes),
                        filename=filename,
                    )
                except discord.DiscordException:
                    LOGGER.warning("Could not copy bumped listing attachment %s", attachment.url)

            image_url = reusable_image_url(fallback_url) if allow_fallback else None
            if not image_url:
                return None, None
            return image_url, None

        async def repost(channel_id, message_id, *, surface: bool = False) -> None:
            nonlocal reposted
            if not channel_id or not message_id:
                return

            channel = await self.fetch_channel_safely(channel_id)
            if channel is None:
                return

            content = f"Price updated: ${old_price:.2f} -> ${new_price:.2f}"
            view = ListingActionView(self, self.db, listing_data)
            try:
                old_message = await channel.fetch_message(message_id)
                embed_data = dict(listing_data)
                fallback_url = reusable_image_url(
                    listing_data.get("image_url"),
                    listing_data.get("surface_image_url"),
                )
                current_image_bytes = canonical_image_bytes or self.listing_image_bytes(listing_data)
                if current_image_bytes is not None:
                    filename = self.listing_image_filename(listing_data)
                    image_url = f"attachment://{filename}"
                    image_file = discord.File(io.BytesIO(current_image_bytes), filename=filename)
                else:
                    image_url, image_file = await image_payload_from_message(
                        old_message,
                        fallback_url=fallback_url,
                        allow_fallback=surface,
                    )
                if image_url:
                    embed_data["image_url"] = image_url
                else:
                    embed_data["image_url"] = None
                    embed_data["surface_image_url"] = None

                send_kwargs = {
                    "content": content,
                    "embed": build_listing_embed(embed_data),
                    "view": view,
                }
                if image_file:
                    send_kwargs["file"] = image_file
                new_message = await channel.send(**send_kwargs)

                persisted_image_url = None
                if new_message.attachments:
                    persisted_image_url = new_message.attachments[0].url
                elif image_file:
                    try:
                        refreshed_message = await channel.fetch_message(new_message.id)
                        if refreshed_message.attachments:
                            persisted_image_url = refreshed_message.attachments[0].url
                    except discord.DiscordException:
                        LOGGER.warning("Could not refetch bumped listing message %s for attachment URL", new_message.id)
                persisted_image_url = persisted_image_url or reusable_image_url(image_url)

                if persisted_image_url:
                    image_url_key = "surface_image_url" if surface else "image_url"
                    listing_data[image_url_key] = persisted_image_url
                    if not surface:
                        listing_data["image_url"] = persisted_image_url
                    elif (
                        not listing_data.get("image_url")
                        or str(listing_data.get("image_url")).startswith("attachment://")
                    ):
                        listing_data["image_url"] = persisted_image_url
                    if image_file:
                        await new_message.edit(
                            embed=build_listing_embed({**listing_data, "image_url": persisted_image_url}),
                            view=view,
                        )
                elif surface:
                    listing_data["surface_image_url"] = None
                else:
                    listing_data["image_url"] = None
                reposted = True
                if surface:
                    listing_data["surface_channel_id"] = new_message.channel.id
                    listing_data["surface_message_id"] = new_message.id
                else:
                    listing_data["channel_id"] = new_message.channel.id
                    listing_data["message_id"] = new_message.id
                await self.capture_listing_message_image(listing_data, new_message, surface=surface)
                try:
                    await old_message.delete()
                except discord.NotFound:
                    pass
                except discord.DiscordException:
                    LOGGER.warning("Could not delete bumped listing message %s", message_id)
            except discord.DiscordException:
                LOGGER.warning("Could not bump listing message %s", message_id)

        await repost(old_primary_channel_id, old_primary_id)
        await repost(old_surface_channel_id, old_surface_id, surface=True)

        if reposted and old_primary_id:
            self.active_listings.pop(int(old_primary_id), None)
            if listing_data.get("message_id"):
                self.active_listings[int(listing_data["message_id"])] = listing_data
        return reposted

    async def mark_listing_message_sold(self, listing_data: Dict[str, Any]) -> None:
        """Edit the original sale-channel message after a purchase is confirmed."""
        await self.edit_listing_messages(
            listing_data,
            embed=build_listing_embed(listing_data, sold=True),
            view=DisabledListingView(),
        )



def main():
    config = load_config()

    db = CardDatabase(config.get("database_path", "cards.db"))
    sheet = create_price_sheet(config)
    sync_sheet_to_db(sheet)

    intents = discord.Intents.default()
    intents.message_content = True

    bot = NBACollectBot(
        sheet=sheet,
        db=db,
        sale_channel_id=config.get("discord_sale_channel_id"),
        auction_channel_id=(
            config.get("discord_auction_channel_id")
            or config.get("discord_auction_surface_channel_id")
            or config.get("discord_sale_channel_id")
        ),
        listing_surface_channel_id=config.get("discord_listing_surface_channel_id"),
        auction_surface_channel_id=config.get("discord_auction_surface_channel_id"),
        config=config,
        intents=intents,
    )

    register_bot_interface(bot, sheet, db)

    @bot.event
    async def on_ready():
        await bot.restore_marketplace_state()
        await bot.reconcile_absent_seller_listings()
        await bot.remove_legacy_slash_commands()
        await bot.ensure_nba_bot_interfaces()
        bot.start_claim_reminder_loop()
        bot.start_seller_reconciliation_loop()
        LOGGER.info("Discord bot is ready.")

    bot.run(config["discord_token"])


if __name__ == "__main__":
    main()
