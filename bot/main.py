import json
import os

from pathlib import Path

from dotenv import load_dotenv

from logger import LOGGER
from sheets import PriceSheet

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "config.json"
ENV_PATH = BASE_DIR.parent / ".env"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_config() -> dict:
    return {
        "discord_token": os.getenv("DISCORD_TOKEN", ""),
        "app_id": os.getenv("DISCORD_APP_ID", ""),
        "public_key": os.getenv("DISCORD_PUBLIC_KEY", ""),
        "discord_sale_channel_id": os.getenv("DISCORD_SALE_CHANNEL_ID", ""),
        "discord_listing_surface_channel_id": os.getenv("DISCORD_LISTING_SURFACE_CHANNEL_ID", ""),
        "discord_auction_surface_channel_id": os.getenv("DISCORD_AUCTION_SURFACE_CHANNEL_ID", ""),
        "discord_bot_channel_id": os.getenv("DISCORD_BOT_CHANNEL_ID", ""),
        "discord_bot_channel_name": os.getenv("DISCORD_BOT_CHANNEL_NAME", "nba-bot"),
        "mod_channel_id": os.getenv("DISCORD_MOD_CHANNEL_ID", ""),
        "discord_guild_id": os.getenv("DISCORD_GUILD_ID", ""),
        "database_path": os.getenv("DATABASE_PATH", "cards.db"),
        "disable_sheets": _env_bool("DISABLE_SHEETS", False),
        "restore_visible_listing_messages_on_startup": _env_bool("RESTORE_VISIBLE_LISTING_MESSAGES_ON_STARTUP", False),
        "google_sheets": {
            "spreadsheet_id": os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", ""),
            "credentials_file": os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "bot/service_account.json"),
        },
        "web": {
            "host": os.getenv("WEB_HOST", "127.0.0.1"),
            "port": int(os.getenv("WEB_PORT", "8000")),
        },
    }


def load_config() -> dict:
    load_dotenv(ENV_PATH, override=True)
    if os.getenv("DISCORD_TOKEN"):
        return _env_config()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def sheets_enabled(config: dict) -> bool:
    # Allow disabling sheets via config or missing credentials
    google_cfg = config.get("google_sheets", {})
    if config.get("disable_sheets", False):
        return False
    if not google_cfg.get("spreadsheet_id") or not google_cfg.get("credentials_file"):
        return False
    return True


def create_price_sheet(config: dict) -> PriceSheet:
    # Only create PriceSheet if enabled, else return None
    if not sheets_enabled(config):
        return None
    return PriceSheet(
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
        credentials_file=config["google_sheets"]["credentials_file"],
        db_path=config.get("database_path", "cards.db"),
    )


def sync_sheet_to_db(sheet: PriceSheet) -> None:
    if sheet is None:
        LOGGER.info("Google Sheets integration is disabled. Skipping sync.")
        return
    try:
        LOGGER.info("Syncing Google Sheets to database...")
        LOGGER.info(f"{sheet.sync_sheets_to_db()} records synced.")
        LOGGER.info("Sync complete.")
    except Exception:
        LOGGER.exception("Could not sync sheets")
