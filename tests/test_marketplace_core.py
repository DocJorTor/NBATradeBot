import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from database import CardDatabase
from commands import register_bot_interface
from components import CUSTOM_CARD_COUNT_VALUE, build_card_count_options, on_card_count_select
from marketplace import (
    LISTING_KIND_SET,
    SOURCE_CHASEFIENDS,
    SOURCE_DISCORD,
    active_marketplace_inventory,
    chasefiends_record_to_domain,
    is_external_listing,
    load_chasefiends_snapshot,
)
from ocr import (
    ListingMetadataGuess,
    _apply_one_of_one_subset_guard,
    _extract_card_count,
    _extract_set_listing_fields,
    _extract_set_progress,
    extract_listing_metadata,
    extract_set_listing_metadata,
)
from parsers import normalize_player_name, parse_required_float
from price_assist import build_price_assist
from serializers import _public_payment_platforms, build_listing_embed
from sheets import PriceSheet
from views import (
    ClaimedListingView,
    EditListingDetailsModal,
    EditListingWorkflowView,
    ExternalListingActionView,
    ListingActionView,
    MarketplaceCarouselView,
)


class MarketplaceDatabaseTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = Path(handle.name)
        self.db = CardDatabase(self.path)
        self.listing = {
            "message_id": 101,
            "channel_id": 202,
            "player_names": "LeBron James",
            "set_name": "Base",
            "subset": "Base",
            "card_count": 1,
            "card_rarity": "Rare",
            "price": 25.0,
            "date_time": "July 13, 2026",
            "seller_id": 1,
            "seller_name": "seller",
            "status": "active",
            "listing_type": "sale",
        }
        self.db.upsert_marketplace_listing(self.listing)

    def tearDown(self):
        self.db.close()
        self.path.unlink(missing_ok=True)

    def test_listing_and_deal_transitions_are_compare_and_set(self):
        self.assertTrue(
            self.db.transition_marketplace_listing(
                101,
                expected_statuses={"active"},
                new_status="claimed",
                buyer_id=2,
                buyer_name="buyer",
                deal_status="claimed",
            )
        )
        self.assertFalse(
            self.db.transition_marketplace_listing(
                101,
                expected_statuses={"active"},
                new_status="claimed",
            )
        )
        self.assertTrue(
            self.db.transition_marketplace_deal_status(
                101,
                expected_deal_statuses={"claimed"},
                new_deal_status="buyer_paid",
            )
        )
        self.assertFalse(
            self.db.transition_marketplace_deal_status(
                101,
                expected_deal_statuses={"claimed"},
                new_deal_status="buyer_paid",
            )
        )
        self.assertTrue(
            self.db.transition_marketplace_listing(
                101,
                expected_statuses={"claimed"},
                new_status="active",
                clear_buyer=True,
            )
        )
        restored = self.db.get_open_marketplace_listings()[0]
        self.assertIsNone(restored["buyer_id"])
        self.assertIsNone(restored["claim_price"])
        self.assertIsNone(restored["deal_status"])

    def test_sale_provenance_prevents_duplicate_records(self):
        first = self.db.add_sale(
            "LeBron James", "Base", "Base", "July 13, 2026", 25, 1,
            seller_id=1, card_rarity="Rare", source_listing_id=101,
        )
        second = self.db.add_sale(
            "LeBron James", "Base", "Base", "July 13, 2026", 25, 1,
            seller_id=1, card_rarity="Rare", source_listing_id=101,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            len(self.db.query_player("LeBron James", cc=1, card_rarity="Rare")),
            1,
        )

    def test_sheet_sync_preserves_card_count_and_listing_provenance(self):
        self.db.sync_from_sheets([{
            "Player Name(s)": "Stephen Curry",
            "Set": "Base",
            "Subset": "Gold",
            "Card Count": "50",
            "Price": "$30.00",
            "Date + Time": "July 13, 2026",
            "Rarity": "Legendary",
            "Source Listing ID": "202",
        }])
        sale = self.db.query_player("Stephen Curry")[0]
        self.assertEqual(sale["card_count"], 50)
        self.assertEqual(sale["source_listing_id"], 202)

        self.db.sync_from_sheets([{
            "Player Name(s)": "Stephen Curry",
            "Set": "Base",
            "Subset": "Gold",
            "Card Count": "50",
            "Price": "$30.00",
            "Date + Time": "July 13, 2026",
            "Rarity": "Legendary",
            "Source Listing ID": "202",
        }])
        self.assertEqual(len(self.db.query_player("Stephen Curry")), 1)

    def test_set_listing_fields_persist_without_entering_card_price_comps(self):
        set_listing = {
            **self.listing,
            "listing_kind": LISTING_KIND_SET,
            "player_names": "NBA Finals Mega Pack",
            "set_name": "NBA Finals Mega Pack",
            "subset": "White Retro",
            "card_rarity": "Uncommon",
            "card_count": 30,
            "set_cards_owned": 27,
            "set_cards_total": 30,
            "includes_award": True,
            "missing_cards": "Cards 4, 12, and 29",
        }
        self.db.upsert_marketplace_listing(set_listing)
        restored = self.db.get_open_marketplace_listings()[0]
        self.assertEqual(restored["listing_kind"], LISTING_KIND_SET)
        self.assertEqual((restored["set_cards_owned"], restored["set_cards_total"]), (27, 30))
        self.assertTrue(restored["includes_award"])
        self.assertEqual(restored["missing_cards"], "Cards 4, 12, and 29")

        self.db.add_sale(
            player_names=set_listing["player_names"],
            set_name=set_listing["set_name"],
            subset=set_listing["subset"],
            date_time="July 14, 2026",
            price=100,
            card_count=30,
            card_rarity="Uncommon",
            listing_kind=LISTING_KIND_SET,
            set_cards_owned=27,
            set_cards_total=30,
            includes_award=True,
            missing_cards=set_listing["missing_cards"],
        )
        self.assertEqual(self.db.query_player("NBA Finals Mega Pack"), [])
        self.assertEqual(self.db.get_all_sales()[0]["listing_kind"], LISTING_KIND_SET)

        embed = build_listing_embed(set_listing)
        self.assertIn("Subset: White Retro", embed.description)
        self.assertIn("Rarity: Uncommon", embed.description)
        self.assertIn("Set Progress: 27/30 cards", embed.description)
        self.assertIn("Includes Award: Yes", embed.description)
        complete_embed = build_listing_embed({
            **set_listing,
            "set_cards_owned": 30,
            "missing_cards": "Should not be displayed",
        })
        self.assertIn("Status: Complete", complete_embed.description)
        self.assertNotIn("Missing Cards:", complete_embed.description)
        action_view = ListingActionView(SimpleNamespace(sheet=None), self.db, set_listing)
        self.assertNotIn("listing:price_assist", {
            child.custom_id for child in action_view.children if child.custom_id
        })
        browser = MarketplaceCarouselView(
            SimpleNamespace(active_listings={101: set_listing}, external_listings=[]),
            self.db,
            [set_listing],
            owner_id=1,
        )
        actions_button = next(
            child for child in browser.children if child.custom_id == "marketplace:actions"
        )
        self.assertEqual(actions_button.label, "📚 Set Actions")

    def test_existing_database_schema_is_upgraded_in_place(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        migration_path = Path(handle.name)
        connection = sqlite3.connect(migration_path)
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '7')")
        connection.execute(
            """
            CREATE TABLE seller_profiles (
                user_id INTEGER PRIMARY KEY,
                payment_methods TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        connection.close()

        migrated = CardDatabase(migration_path)
        try:
            profile_columns = {
                row["name"] for row in migrated.conn.execute("PRAGMA table_info(seller_profiles)")
            }
            sale_columns = {
                row["name"] for row in migrated.conn.execute("PRAGMA table_info(card_sales)")
            }
            listing_columns = {
                row["name"] for row in migrated.conn.execute("PRAGMA table_info(marketplace_listings)")
            }
            self.assertTrue({"ign", "payment_notes"}.issubset(profile_columns))
            self.assertTrue({"card_rarity", "source_listing_id"}.issubset(sale_columns))
            self.assertTrue({
                "listing_kind", "set_cards_owned", "set_cards_total", "includes_award", "missing_cards"
            }.issubset(sale_columns))
            self.assertTrue({
                "listing_kind", "set_cards_owned", "set_cards_total", "includes_award", "missing_cards"
            }.issubset(listing_columns))
            self.assertEqual(
                migrated.get_metadata("schema_version"),
                str(CardDatabase.SCHEMA_VERSION),
            )
        finally:
            migrated.close()
            migration_path.unlink(missing_ok=True)

    def test_persistent_views_respect_discord_component_limits(self):
        listing = {
            **self.listing,
            "status": "claimed",
            "buyer_id": 2,
            "deal_status": "buyer_paid",
        }
        bot = SimpleNamespace(active_listings={101: listing})
        claimed_view = ClaimedListingView(bot, self.db, listing)
        paid_button = next(child for child in claimed_view.children if child.custom_id == "claimed:paid")
        self.assertTrue(paid_button.disabled)

        auction = {
            **self.listing,
            "listing_type": "auction",
            "bid_increment": 5,
            "auction_end_at": "2026-07-14T12:00:00+00:00",
        }
        edit_view = EditListingWorkflowView(bot, self.db, auction, owner_id=1)
        self.assertLessEqual(len(edit_view.children), 25)
        self.assertTrue(all(getattr(child, "row", 0) <= 4 for child in edit_view.children))

        browser = MarketplaceCarouselView(bot, self.db, [listing], owner_id=1)
        self.assertTrue(all(len(getattr(child, "options", [])) <= 25 for child in browser.children))

    def test_edit_listing_payment_input_accepts_multiline_accounts(self):
        payment_methods = "Venmo: @seller\nPayPal: seller@example.com"
        parent_view = SimpleNamespace(
            player_names="LeBron James",
            price=25,
            payment_methods=payment_methods,
            is_auction=False,
        )

        modal = EditListingDetailsModal(parent_view)

        self.assertEqual(modal.payment_methods.default, payment_methods)
        self.assertEqual(modal.payment_methods.style, discord.TextStyle.paragraph)
        self.assertEqual(modal.payment_methods.max_length, 1000)

    def test_main_interface_combines_help_and_feedback(self):
        bot = SimpleNamespace()
        register_bot_interface(bot, None, self.db)
        interface = bot.bot_interface_view_factory()
        buttons = {
            child.custom_id: child.label
            for child in interface.children
            if getattr(child, "custom_id", None)
        }
        self.assertEqual(buttons["nba_bot:help"], "❓ Help & Feedback")
        self.assertEqual(buttons["nba_bot:profile"], "👤 Account")
        self.assertNotIn("nba_bot:feedback", buttons)
        self.assertEqual(buttons["nba_bot:list_player"], "🏷️ List a Player")
        self.assertEqual(buttons["nba_bot:list_set"], "📚 List a Set")
        self.assertEqual(buttons["nba_bot:auction_player"], "🔨 Auction a Player")
        status_button = next(
            child for child in interface.children if child.custom_id == "nba_bot:status"
        )
        self.assertEqual(status_button.style, discord.ButtonStyle.secondary)
        self.assertLessEqual(len(interface.children), 25)
        self.assertTrue(all(getattr(child, "row", 0) <= 4 for child in interface.children))
        self.assertEqual(
            [(child.custom_id, child.row) for child in interface.children],
            [
                ("nba_bot:price", 0),
                ("nba_bot:open_marketplace", 0),
                ("nba_bot:showcase", 0),
                ("nba_bot:notifications", 0),
                ("nba_bot:list_player", 1),
                ("nba_bot:list_set", 1),
                ("nba_bot:auction_player", 1),
                ("nba_bot:help", 2),
                ("nba_bot:profile", 2),
                ("nba_bot:status", 2),
                ("nba_bot:add_set", 3),
            ],
        )

    def test_custom_sets_and_showcase_cards_persist(self):
        self.db.upsert_custom_set("2027 Test Set", ["Base", "Gold"], added_by=42)
        self.assertEqual(self.db.get_custom_sets()[0]["subsets"], ["Base", "Gold"])

        for index in range(5):
            self.db.add_showcase_card(
                42, "Collector", f"Player {index}", "2027 Test Set", "Favorite", b"image"
            )
        self.assertEqual(len(self.db.get_showcase_cards(42)), 5)
        with self.assertRaises(ValueError):
            self.db.add_showcase_card(42, "Collector", "Sixth", "Set", "", b"image")
        first = self.db.get_showcase_cards(42)[0]
        self.assertTrue(self.db.delete_showcase_card(first["id"], 42))

    def test_notes_are_searchable_and_persist_on_listings(self):
        listing = {**self.listing, "additional_information": "NBA debut career high"}
        self.db.upsert_marketplace_listing(listing)
        self.assertEqual(
            self.db.get_open_marketplace_listings()[0]["additional_information"],
            "NBA debut career high",
        )
        self.db.add_sale(
            "LeBron James", "Base", "Gold", "2026-01-01", 25, 99,
            additional_information="ROTY award",
            buying_format="Fixed Price",
            platform="Discord",
        )
        results = self.db.query_player(additional_information="roty")
        self.assertEqual(results[0]["buying_format"], "Fixed Price")
        self.assertEqual(results[0]["platform"], "Discord")

    def test_static_chasefiends_snapshot_is_sanitized_and_combined(self):
        external = load_chasefiends_snapshot()
        self.assertEqual(len(external), 126)
        self.assertTrue(all(item["source"] == SOURCE_CHASEFIENDS for item in external))
        self.assertTrue(all(item["is_external"] for item in external))
        self.assertTrue(all(item["capabilities"] == ["go_to_site", "listing_info"] for item in external))
        self.assertTrue(all(item["source_url"].startswith("https://chasefiends.com/listings/") for item in external))
        self.assertTrue(all("seller_id" not in item and "seller_notes" not in item for item in external))

        bot = SimpleNamespace(active_listings={101: self.listing}, external_listings=external[:2])
        combined = active_marketplace_inventory(bot)
        self.assertEqual({item["source"] for item in combined}, {SOURCE_DISCORD, SOURCE_CHASEFIENDS})
        self.assertEqual(len(combined), 3)

    def test_external_listing_ui_is_attributed_and_read_only(self):
        listing = load_chasefiends_snapshot()[0]
        embed = build_listing_embed(listing)
        self.assertIn("Subset:", embed.description)
        self.assertIn("Card Count:", embed.description)
        self.assertIn("Listing Price:", embed.description)
        self.assertNotIn("Team:", embed.description)
        self.assertNotIn("Rarity:", embed.description)
        self.assertNotIn("Year:", embed.description)
        self.assertNotIn("Offers accepted", embed.description)
        self.assertEqual(embed.color, discord.Color.blue())
        self.assertIn("all activity occurs on ChaseFiends", embed.footer.text)
        self.assertEqual(embed.url, listing["source_url"])

        action_view = ExternalListingActionView(listing)
        self.assertEqual(len(action_view.children), 2)
        link_button = next(child for child in action_view.children if getattr(child, "url", None))
        info_button = next(child for child in action_view.children if child.custom_id == "external_listing:info")
        self.assertEqual(link_button.label, "🌐 Go to Site")
        self.assertEqual(info_button.label, "ℹ️ Listing Info")

        bot = SimpleNamespace(active_listings={101: self.listing}, external_listings=[listing])
        browser = MarketplaceCarouselView(bot, self.db, active_marketplace_inventory(bot), owner_id=1)
        self.assertEqual(browser.source_filter, SOURCE_DISCORD)
        self.assertTrue(all(item["source"] == SOURCE_DISCORD for item in browser.listings))
        option_values = {option.value for option in browser.price_select.options}
        self.assertFalse(any(value.startswith("source:") for value in option_values))

        source_button = next(
            child for child in browser.children
            if child.custom_id == "marketplace:source_toggle"
        )
        self.assertEqual(source_button.label, "🌐 View ChaseFiends (1)")
        self.assertEqual(source_button.style, discord.ButtonStyle.green)
        self.assertFalse(source_button.disabled)

        browser.source_filter = SOURCE_CHASEFIENDS
        browser.reset_to_first_on_refresh = True
        browser._refresh_listings()
        browser._sync_buttons()
        self.assertTrue(all(item["source"] == SOURCE_CHASEFIENDS for item in browser.listings))
        self.assertEqual(source_button.label, "🃏 View Discord (1)")
        self.assertEqual(source_button.style, discord.ButtonStyle.secondary)
        external_option_values = {option.value for option in browser.price_select.options}
        self.assertNotIn("type:auction", external_option_values)
        self.assertNotIn("ending:auction", external_option_values)
        for row in range(5):
            self.assertLessEqual(
                sum(getattr(child, "row", None) == row for child in browser.children),
                5,
            )

    def test_external_validation_rejects_untrusted_listing_urls(self):
        record = {
            "source": "chasefiends",
            "id": "a61a076a-7e33-479a-897b-f0ebd284d3e1",
            "source_url": "https://example.com/listings/a61a076a-7e33-479a-897b-f0ebd284d3e1",
            "platform": "Topps NBA Collect",
            "status": "active",
            "listing_type": "buy_now",
            "asking_price": 25,
            "player_name": "LeBron James",
            "set_name": "Base",
            "subset_name": "Gold",
            "global_count": 50,
        }
        self.assertIsNone(chasefiends_record_to_domain(record))

    def test_legacy_discord_listing_without_source_stays_native(self):
        self.assertFalse(is_external_listing(self.listing))
        embed = build_listing_embed(self.listing)
        self.assertNotIn("External listing", embed.description or "")

    def test_member_reconciliation_structurally_ignores_external_sources(self):
        from bot import NBACollectBot

        fake_bot = SimpleNamespace(
            active_listings={
                999: {
                    "source": SOURCE_CHASEFIENDS,
                    "seller_id": 999,
                    "guild_id": 123,
                    "status": "active",
                }
            },
            guilds=[],
        )
        removed = asyncio.run(NBACollectBot.reconcile_absent_seller_listings(fake_bot))
        self.assertEqual(removed, 0)


class MarketplaceFormattingTests(unittest.TestCase):
    def test_card_count_options_support_custom_values(self):
        options = build_card_count_options(selected_value="125", include_any=False)
        self.assertEqual(options[0].value, "125")
        self.assertTrue(options[0].default)
        self.assertIn(CUSTOM_CARD_COUNT_VALUE, {option.value for option in options})

    def test_card_count_preset_selection_ignores_custom_sentinel(self):
        class Response:
            def __init__(self):
                self.edited = False

            async def edit_message(self, **kwargs):
                self.edited = True

        response = Response()
        view = SimpleNamespace(
            card_count_value=None,
            card_count_select=SimpleNamespace(
                values=["250"],
                options=build_card_count_options(selected_value="ANY"),
            ),
        )

        asyncio.run(on_card_count_select(view, SimpleNamespace(response=response)))

        self.assertEqual(view.card_count_value, 250)
        self.assertTrue(response.edited)
        defaults = {option.value: option.default for option in view.card_count_select.options}
        self.assertTrue(defaults["250"])
        self.assertFalse(defaults[CUSTOM_CARD_COUNT_VALUE])

    def test_public_payment_summary_never_exposes_account_values(self):
        summary = _public_payment_platforms(
            "PayPal: private@example.com\nVenmo: @private-user\nprivate-legacy-value"
        )
        self.assertEqual(summary, "PayPal, Venmo")
        self.assertNotIn("@", summary)
        self.assertNotIn("example.com", summary)

    def test_price_assist_exact_query_includes_card_count_and_rarity(self):
        class Source:
            def __init__(self):
                self.calls = []

            def query_player(self, **kwargs):
                self.calls.append(kwargs)
                return [{"player_names": "LeBron James", "price": 25.0}]

        source = Source()
        result = build_price_assist(
            source,
            {
                "player_names": "LeBron James",
                "set_name": "Base",
                "subset": "Base",
                "card_count": 100,
                "card_rarity": "Rare",
            },
        )
        self.assertEqual(result["mode"], "exact")
        self.assertEqual(source.calls[0]["cc"], 100)
        self.assertEqual(source.calls[0]["card_rarity"], "Rare")

    def test_numeric_and_name_input_normalization(self):
        self.assertEqual(parse_required_float("$1,250.50", field_name="Price"), 1250.5)
        for value in ("nan", "inf", "-inf"):
            with self.assertRaises(ValueError):
                parse_required_float(value, field_name="Price")
        self.assertEqual(normalize_player_name("  lebron   james jr. "), "LeBron James Jr.")

    def test_ocr_recognizes_one_of_one_serials_without_an_extra_pass(self):
        for value in ("1/1", "1 / 1", "I/1", "1/l", "one of one", "1 out of 1"):
            self.assertEqual(_extract_card_count(value), 1)

        with (
            patch("ocr._read_text_from_image", return_value=("Gold 1/1", "pytesseract", True)),
            patch("ocr._extract_bottom_pill_count") as bottom_pill_ocr,
            patch("ocr._extract_bottom_pill_rarity", return_value=None),
        ):
            guess = extract_listing_metadata(b"image")

        self.assertEqual(guess.card_count, 1)
        self.assertEqual(guess.card_count_source, "ocr_one_of_one")
        self.assertEqual(guess.confidence["card_count"], 0.9)
        bottom_pill_ocr.assert_not_called()

    def test_one_of_one_serial_overrides_orange_color_guess(self):
        guess = ListingMetadataGuess(
            set_name="2025-26 Topps Chrome",
            subset_group="Signatures",
            subset_variant="Orange",
            subset_value="Orange Signatures",
            card_count=1,
        )

        _apply_one_of_one_subset_guard(guess)

        self.assertEqual(guess.subset_group, "Signatures")
        self.assertEqual(guess.subset_variant, "Superfractor")
        self.assertEqual(guess.subset_value, "Superfractor Signatures")

    def test_set_screenshot_ocr_splits_structured_fields(self):
        text = (
            "BUCKS\nGiannis Antetokounmpo\n"
            "NBA Finals Mega Pack | Series 1 | White\n"
            "Retro | Uncommon\n30 / 30\n"
            "CLAIMED TUESDAY, JUNE 23, 2026"
        )
        self.assertEqual(_extract_set_progress(text), (30, 30))
        self.assertEqual(_extract_set_progress("30 30"), (30, 30))
        self.assertEqual(
            _extract_set_listing_fields(text),
            ("NBA Finals Mega Pack", "White Retro", "Uncommon"),
        )
        self.assertEqual(
            _extract_set_listing_fields(
                "NBA Finals Mega Pack Series 1 White Retro Uncommon\n30/30"
            ),
            ("NBA Finals Mega Pack", "White Retro", "Uncommon"),
        )
        self.assertEqual(
            _extract_set_listing_fields(
                "NBA Finals Mega Pack Series 1 | White Retro | Uncommon\n30/30"
            ),
            ("NBA Finals Mega Pack", "White Retro", "Uncommon"),
        )
        self.assertEqual(
            _extract_set_listing_fields(
                "NBA Finals Mega Pack | Series 1 White Retro | Uncommon\n30/30"
            ),
            ("NBA Finals Mega Pack", "White Retro", "Uncommon"),
        )
        title_text = "NBA Finals Mega Pack | Series 1 | White\nRetro | Uncommon"
        with patch(
            "ocr._read_set_text_from_image",
            return_value=(title_text, "30 / 30", "pytesseract_targeted", True),
        ) as read_ocr:
            guess = extract_set_listing_metadata(b"image")
        read_ocr.assert_called_once()
        self.assertEqual(guess.set_name, "NBA Finals Mega Pack")
        self.assertEqual(guess.subset, "White Retro")
        self.assertEqual(guess.card_rarity, "Uncommon")
        self.assertEqual((guess.cards_owned, guess.cards_total), (30, 30))


class PriceSheetTests(unittest.TestCase):
    def test_source_listing_column_makes_sheet_retry_idempotent(self):
        class FakeSheet:
            def __init__(self):
                self.rows = [
                    ["Marketplace Sales"],
                    ["Player Name(s)", "Set", "Price", "Source Listing ID"],
                    ["LeBron James", "Base", "25", "101"],
                ]
                self.inserted = []

            def get_all_values(self):
                return self.rows

            def insert_row(self, row, index):
                self.inserted.append((row, index))

        price_sheet = PriceSheet.__new__(PriceSheet)
        price_sheet.sheet = FakeSheet()
        price_sheet._insert_sale_row({
            "player_names": "LeBron James",
            "set_name": "Base",
            "price": 25,
            "source_listing_id": 101,
        })
        self.assertEqual(price_sheet.sheet.inserted, [])

        price_sheet._insert_sale_row({
            "player_names": "Stephen Curry",
            "set_name": "Base",
            "price": 30,
            "source_listing_id": 102,
        })
        self.assertEqual(len(price_sheet.sheet.inserted), 1)
        self.assertEqual(price_sheet.sheet.inserted[0][0][-1], "102")

    def test_set_sale_fields_map_to_optional_sheet_columns(self):
        class FakeSheet:
            def __init__(self):
                self.rows = [[
                    "Player Name(s)", "Set", "Subset", "Rarity", "Listing Kind",
                    "Set Cards Owned", "Set Cards Total", "Includes Award", "Missing Cards", "Price",
                ]]
                self.inserted = []

            def get_all_values(self):
                return self.rows

            def insert_row(self, row, index):
                self.inserted.append(row)

        price_sheet = PriceSheet.__new__(PriceSheet)
        price_sheet.sheet = FakeSheet()
        price_sheet._insert_sale_row({
            "player_names": "NBA Finals Mega Pack",
            "set_name": "NBA Finals Mega Pack",
            "subset": "White Retro",
            "card_rarity": "Uncommon",
            "listing_kind": "set",
            "set_cards_owned": 27,
            "set_cards_total": 30,
            "includes_award": True,
            "missing_cards": "Cards 4, 12, and 29",
        })
        self.assertEqual(
            price_sheet.sheet.inserted[0],
            [
                "NBA Finals Mega Pack", "NBA Finals Mega Pack", "White Retro", "Uncommon",
                "set", 27, 30, "Yes", "Cards 4, 12, and 29", "",
            ],
        )

    def test_transaction_format_platform_and_notes_map_to_sheet(self):
        class FakeSheet:
            def __init__(self):
                self.rows = [[
                    "Player Name(s)", "Set", "Price", "Buying Format", "Platform",
                    "Additional Information",
                ]]
                self.inserted = []
            def get_all_values(self):
                return self.rows
            def insert_row(self, row, index):
                self.inserted.append(row)

        price_sheet = PriceSheet.__new__(PriceSheet)
        price_sheet.sheet = FakeSheet()
        price_sheet._insert_sale_row({
            "player_names": "Victor Wembanyama",
            "set_name": "Topps Now",
            "price": 50,
            "buying_format": "Auction",
            "platform": "Discord",
            "additional_information": "NBA debut",
        })
        self.assertEqual(
            price_sheet.sheet.inserted[0],
            ["Victor Wembanyama", "Topps Now", 50, "Auction", "Discord", "NBA debut"],
        )


if __name__ == "__main__":
    unittest.main()
