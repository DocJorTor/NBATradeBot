import asyncio
import io
from datetime import datetime, timezone

import discord

from parsers import listing_matches_notify_rule
from logger import LOGGER, log_marketplace_event
from marketplace import is_set_listing
from price_assist import build_price_assist
from serializers import build_listing_embed


def _attachment_is_image(attachment: discord.Attachment) -> bool:
    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = str(getattr(attachment, "filename", "") or "").lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


async def delete_message_if_possible(message: discord.Message, interaction: discord.Interaction = None) -> bool:
    try:
        await message.delete()
        return True
    except discord.Forbidden:
        if interaction:
            await interaction.followup.send(
                "I found the upload, but I do not have permission to delete that message.",
                ephemeral=True,
            )
        return False
    except discord.NotFound:
        return True
    except discord.DiscordException as exc:
        LOGGER.warning("Could not delete message %s: %s", getattr(message, "id", None), exc)
        return False


async def collect_listing_image(
    bot,
    db,
    interaction: discord.Interaction,
    upload_prompt: discord.Message = None,
    upload_channel: discord.abc.Messageable = None,
) -> tuple[str | None, discord.File | None, bytes | None]:
    image_url = None
    image_file = None
    image_bytes = None

    target_channel = upload_channel or interaction.channel
    target_channel_id = getattr(target_channel, "id", None)

    def check(msg: discord.Message):
        return (
            msg.author.id == interaction.user.id
            and msg.channel.id == target_channel_id
            and len(msg.attachments) > 0
        )

    try:
        msg = await bot.wait_for("message", timeout=120, check=check)
        attachment = msg.attachments[0]

        if not _attachment_is_image(attachment):
            log_marketplace_event(
                db,
                "listing_image_invalid",
                user_id=interaction.user.id,
                details={
                    "content_type": attachment.content_type,
                    "filename": getattr(attachment, "filename", None),
                },
                level=30,
            )
            await interaction.followup.send(
                "That file was not detected as an image. An image is required, so this workflow will stop.",
                ephemeral=True,
            )
        else:
            image_bytes = await attachment.read()
            image_file = discord.File(
                io.BytesIO(image_bytes),
                filename="card_image.png",
            )
            image_url = "attachment://card_image.png"

        if getattr(msg.channel, "type", None) == discord.ChannelType.private:
            deleted_upload = False
        else:
            deleted_upload = await delete_message_if_possible(msg, interaction)
        log_marketplace_event(
            db,
            "listing_upload_message_cleanup",
            user_id=interaction.user.id,
            details={"message_id": msg.id, "deleted": deleted_upload},
            level=20 if deleted_upload else 30,
        )
    except asyncio.TimeoutError:
        log_marketplace_event(
            db,
            "listing_image_timeout",
            user_id=interaction.user.id,
            level=30,
        )
        await interaction.followup.send(
            "No image was uploaded. An image is required, so this workflow will stop.",
            ephemeral=True,
        )
    finally:
        if upload_prompt:
            await delete_message_if_possible(upload_prompt)

    return image_url, image_file, image_bytes


async def publish_listing(
    bot,
    db,
    sale_channel: discord.TextChannel,
    interaction: discord.Interaction,
    listing_data: dict,
    *,
    image_file: discord.File = None,
    notify_users: bool = True,
) -> discord.Message:
    from views import AuctionActionView, ListingActionView

    seller_id = listing_data.get("seller_id")
    profile = (db.get_marketplace_profile(seller_id) or {}) if seller_id else {}
    if profile.get("payment_methods"):
        listing_data["payment_methods"] = profile["payment_methods"]
    listing_data["payment_notes"] = profile.get("payment_notes")
    listing_data.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    view_cls = AuctionActionView if listing_data.get("listing_type") == "auction" else ListingActionView
    view = view_cls(bot, db, listing_data)
    if listing_data.get("listing_type") != "auction" and not is_set_listing(listing_data):
        listing_data["price_assist"] = build_price_assist(getattr(bot, "sheet", None) or db, listing_data)
        log_marketplace_event(
            db,
            "price_assist_lookup",
            user_id=interaction.user.id,
            details={
                "player_names": listing_data.get("player_names"),
                "set_name": listing_data.get("set_name"),
                "subset": listing_data.get("subset"),
                "card_count": listing_data.get("card_count"),
                "mode": listing_data["price_assist"].get("mode"),
                "result_count": len(listing_data["price_assist"].get("results") or []),
            },
            level=20,
        )
    embed = build_listing_embed(listing_data)
    if image_file:
        message = await sale_channel.send(embed=embed, view=view, file=image_file)
    else:
        message = await sale_channel.send(embed=embed, view=view)

    listing_data["message_id"] = message.id
    listing_data["channel_id"] = message.channel.id
    if interaction.guild:
        listing_data["guild_id"] = interaction.guild.id

    attachment_url = message.attachments[0].url if message.attachments else None
    if image_file and not attachment_url:
        try:
            refreshed_message = await sale_channel.fetch_message(message.id)
            if refreshed_message.attachments:
                attachment_url = refreshed_message.attachments[0].url
        except discord.DiscordException:
            LOGGER.warning("Could not refetch listing message %s for attachment URL", message.id)

    if attachment_url:
        listing_data["image_url"] = attachment_url
        listing_data.setdefault("image_filename", "card_image.png")
    elif str(listing_data.get("image_url") or "").startswith("attachment://"):
        listing_data["image_url"] = None
        if not listing_data.get("image_bytes") and not listing_data.get("image_blob"):
            log_marketplace_event(
                db,
                "listing_image_url_missing_after_publish",
                user_id=interaction.user.id,
                listing_id=message.id,
                level=30,
            )

    if hasattr(bot, "capture_listing_message_image"):
        await bot.capture_listing_message_image(listing_data, message)

    bot.active_listings[message.id] = listing_data
    await bot.surface_listing_message(
        listing_data,
        view=view_cls(bot, db, listing_data),
    )
    db.upsert_marketplace_listing(listing_data)
    log_marketplace_event(
        db,
        "listing_created",
        user_id=interaction.user.id,
        listing_id=message.id,
        details={
            "player_names": listing_data.get("player_names"),
            "price": listing_data.get("price"),
            "listing_type": listing_data.get("listing_type", "sale"),
            "listing_kind": listing_data.get("listing_kind", "player"),
        },
    )
    if notify_users:
        await notify_matching_users(bot, listing_data)
    return message

async def notify_matching_users(bot, listing_data: dict):
    seller_id = listing_data.get("seller_id")

    matching_rules = [
        rule for rule in bot.notify_rules
        if rule["user_id"] != seller_id
        and listing_matches_notify_rule(listing_data, rule)
    ]

    matching_user_ids = sorted({int(rule["user_id"]) for rule in matching_rules})
    listing_link = None
    if listing_data.get("guild_id") and listing_data.get("channel_id") and listing_data.get("message_id"):
        listing_link = (
            f"https://discord.com/channels/{listing_data['guild_id']}/"
            f"{listing_data['channel_id']}/{listing_data['message_id']}"
        )

    for user_id in matching_user_ids:
        user = await bot.hydrate_user(user_id, fetch=True)

        sent = await bot.safe_dm_user(
            user,
            content=(
                f"🔔 New listing match!\n\n"
                f"**Player:** {listing_data['player_names']}\n"
                f"**Set:** {listing_data['set_name']}\n"
                f"**Subset:** {listing_data['subset']}\n"
                f"**Card Count:** /{listing_data['card_count']}\n"
                f"**Price:** ${listing_data['price']:.2f}\n"
                f"{f'**Open listing:** {listing_link}' if listing_link else 'Open the NBA Bot Marketplace to view this listing.'}\n\n"
            ),
        )
        if not sent:
            LOGGER.warning("Could not DM user %s", user_id)
        else:
            log_marketplace_event(
                bot.db,
                "listing_notification_sent",
                user_id=user_id,
                listing_id=listing_data.get("message_id"),
                details={"matching_rule_count": sum(1 for rule in matching_rules if int(rule["user_id"]) == user_id)},
            )
