<img width="1008" height="257" alt="Screenshot 2026-07-06 012345" src="https://github.com/user-attachments/assets/043748a9-6c44-4400-a7fb-b2cc6a5b74bd" />

<img width="986" height="661" alt="Screenshot 2026-07-06 012502" src="https://github.com/user-attachments/assets/afb45ac1-4d25-4bc1-b664-76f611bd3cc4" />

# NBACollectEval

Discord marketplace bot for NBA digital card trading communities. The bot runs a button-driven interface in `#nba-bot`, supports fixed-price listings and auctions, provides price lookup from historical sales, tracks user notification rules, and persists marketplace state in SQLite so active listings survive restarts.

Slash commands are deprecated. User actions are expected to start from the persistent `NBA Bot` interface message in the configured bot channel.

## Core Features

- **Button-first Discord UI**
  - Persistent `#nba-bot` control panel.
  - Marketplace carousel for browsing active listings.
  - Private/ephemeral filter panels for price search, notifications, status, and listing management.

- **Price search**
  - Filters by player name, set, subset/variant, and card count.
  - Set/subset filtering uses Discord dropdowns; subset options update dynamically after a set is selected.
  - Player search uses a modal because Discord text inputs are modal-only.
  - Search results are sent as separate ephemeral result messages so the filter panel stays open.
  - Results support paging, closing, and saving the search as a notification.

- **Marketplace listings**
  - Fixed-price listing flow with image upload.
  - Auction listing flow with starting price, duration, and bid increment.
  - OCR-assisted metadata extraction from uploaded card images.
  - Set/subset/variant/card-count review before publish.
  - Listing embeds include seller info, card metadata, price, and image.

- **Claims, offers, auctions, and transactions**
  - Buyers can claim fixed-price listings or make offers.
  - Sellers can accept, counter, or decline offers.
  - Auctions support bids, bid increments, auction end time, and seller finalization.
  - Completed marketplace deals can be recorded as transactions.
  - Claimed listings have follow-up controls and stale-claim reminder handling.

- **Notifications**
  - Users can create and remove alert rules.
  - Rules can match player, set, subset/variant, and card count.
  - Matching users are DM'd when a new listing is published.

- **Persistence and recovery**
  - SQLite stores sales, marketplace listings, bids, notification rules, events, seller payment methods, and bot metadata.
  - Startup restores active listings and persistent views from the database.
  - Listing images can be persisted as blobs to repair Discord CDN/attachment edge cases.
  - Optional surface channels can mirror listings and auctions outside the primary sale channel.

## Architecture

```text
bot/
  bot.py            Discord client, startup, persistent interface message, restore loops, listing repair helpers.
  commands.py       Button-interface workflows: help, feedback, status, price search, listing, auction, notifications.
  views.py          Persistent listing/auction/claim/bid/counteroffer views and modals.
  components.py     Shared set/subset/variant/card-count dropdown builders and fallback search modals.
  actions.py        Listing image collection, listing publication, notification fanout.
  database.py       SQLite schema, migrations, marketplace persistence, notify rules, event logging.
  price_assist.py   Price query helpers and paginated price result views.
  ocr.py            Image preprocessing and OCR metadata extraction.
  serializers.py    Discord embed builders and display formatting.
  sheets.py         Google Sheets integration and sheet-to-SQLite sync.
  main.py           Configuration loading and sheet bootstrap helpers.
```

## Runtime Flow

1. `bot.py` loads config from `.env` or `config.json`.
2. `main.py` optionally creates a Google Sheets client and syncs sheet data into SQLite.
3. `NBACollectBot` starts with Discord intents and registers the persistent button interface from `commands.py`.
4. On ready:
   - active marketplace state is restored from SQLite,
   - the `NBA Bot` interface message is created or updated in `#nba-bot`,
   - stale listing/interface cleanup runs,
   - claim reminder loop starts.
5. Users interact through buttons, dropdowns, modals, and ephemeral panels.

## Configuration

The bot prefers `.env` when `DISCORD_TOKEN` is present. Otherwise it falls back to `config.json`.

Supported environment variables:

```env
DISCORD_TOKEN=
DISCORD_APP_ID=
DISCORD_PUBLIC_KEY=
DISCORD_GUILD_ID=

DISCORD_BOT_CHANNEL_ID=
DISCORD_BOT_CHANNEL_NAME=nba-bot
DISCORD_SALE_CHANNEL_ID=
DISCORD_LISTING_SURFACE_CHANNEL_ID=
DISCORD_AUCTION_SURFACE_CHANNEL_ID=
DISCORD_MOD_CHANNEL_ID=

DATABASE_PATH=cards.db
DISABLE_SHEETS=false
RESTORE_VISIBLE_LISTING_MESSAGES_ON_STARTUP=false

GOOGLE_SHEETS_SPREADSHEET_ID=
GOOGLE_SHEETS_CREDENTIALS_FILE=bot/service_account.json

WEB_HOST=127.0.0.1
WEB_PORT=8000
```

## Data Storage

SQLite is the operational store. `CardDatabase` owns schema creation and lightweight migrations.

Important tables:

- `card_sales` / sales data used by price lookup.
- `marketplace_listings` for active, claimed, sold, removed, and auction listings.
- `marketplace_bids` for offers, bids, counters, and bid state.
- `notify_rules` for user alert filters.
- `marketplace_events` for audit/debug telemetry.
- metadata tables for bot message IDs and persistent interface state.

Google Sheets can be used as an upstream source for pricing and transaction history. When enabled, the bot syncs sheet rows into the local database on startup.

## Discord UI Notes

- Discord text inputs only work inside modals, so freeform player entry uses a modal.
- Set, subset, variant, and card-count filters use dropdowns.
- Subset choices are generated from the selected set.
- Variant choices are shown when the selected subset has variants.
- Price result messages are separate from the filter panel, allowing users to close results and keep searching.

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Install Tesseract OCR separately if image metadata extraction is needed. The Python dependency is `pytesseract`, but the Tesseract executable must also be available on the host.

Run the bot:

```powershell
.\.venv\Scripts\python.exe bot\bot.py
```

## Verification

Useful smoke checks:

```powershell
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'bot'); import bot, commands, views, components; print('imports ok')"
python -m compileall bot
```

For database inspection:

```powershell
sqlite3 cards.db ".tables"
sqlite3 cards.db ".schema marketplace_listings"
sqlite3 cards.db ".schema notify_rules"
```

## Dependency Notes

Current runtime dependencies are listed in `requirements.txt`:

- `discord.py`
- `aiohttp`
- `gspread`
- `oauth2client`
- `python-dotenv`
- `Pillow`
- `pytesseract`

The codebase currently imports `discord.py` APIs directly. A future migration to `disnake` would mainly affect Discord UI classes, interaction response APIs, and modal/component construction.
