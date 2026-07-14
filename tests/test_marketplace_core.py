import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from database import CardDatabase
from commands import register_bot_interface
from ocr import (
    ListingMetadataGuess,
    _apply_one_of_one_subset_guard,
    _extract_card_count,
    extract_listing_metadata,
)
from parsers import normalize_player_name, parse_required_float
from price_assist import build_price_assist
from serializers import _public_payment_platforms
from sheets import PriceSheet
from views import ClaimedListingView, EditListingWorkflowView, MarketplaceCarouselView


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
            self.assertTrue({"ign", "payment_notes"}.issubset(profile_columns))
            self.assertTrue({"card_rarity", "source_listing_id"}.issubset(sale_columns))
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
        self.assertLessEqual(len(interface.children), 25)
        self.assertTrue(all(getattr(child, "row", 0) <= 4 for child in interface.children))


class MarketplaceFormattingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
