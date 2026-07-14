"""Database module for card sales and pricing data."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class CardDatabase:
    """SQLite database for managing card sales and pricing data."""

    SCHEMA_VERSION = 13

    def __init__(self, db_path: str | Path = "cards.db"):
        self.db_path = Path(db_path)
        self.conn = None
        self._initialize()

    def _initialize(self):
        """Initialize the database connection and create tables if needed."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        """Create the database schema."""
        cursor = self.conn.cursor()

        # Main sales table with exact column names from spec
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS card_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_names TEXT NOT NULL,
                set_name TEXT NOT NULL,
                subset TEXT NOT NULL,
                date_time TEXT NOT NULL,
                price REAL NOT NULL,
                card_count INTEGER DEFAULT 1,
                image_url TEXT,
                seller_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Create indexes for common queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_player_names 
            ON card_sales(player_names)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_date_time 
            ON card_sales(date_time)
        """)
        self._ensure_column("card_sales", "card_rarity", "TEXT")
        self._ensure_column("card_sales", "source_listing_id", "INTEGER")
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_card_sales_source_listing
            ON card_sales(source_listing_id)
            WHERE source_listing_id IS NOT NULL
        """)

        # Metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                user_id INTEGER,
                listing_id INTEGER,
                details TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_marketplace_events_type
            ON marketplace_events(event_type)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_marketplace_events_listing
            ON marketplace_events(listing_id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_listings (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER,
                surface_message_id INTEGER,
                surface_channel_id INTEGER,
                player_names TEXT NOT NULL,
                set_name TEXT NOT NULL,
                subset TEXT NOT NULL,
                card_count TEXT NOT NULL,
                price REAL NOT NULL,
                date_time TEXT NOT NULL,
                seller_id INTEGER NOT NULL,
                seller_name TEXT,
                buyer_id INTEGER,
                buyer_name TEXT,
                status TEXT NOT NULL,
                payment_methods TEXT,
                image_url TEXT,
                surface_image_url TEXT,
                image_blob BLOB,
                image_filename TEXT,
                image_content_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._ensure_column("marketplace_listings", "payment_methods", "TEXT")
        self._ensure_column("marketplace_listings", "listing_type", "TEXT DEFAULT 'sale'")
        self._ensure_column("marketplace_listings", "auction_end_at", "TEXT")
        self._ensure_column("marketplace_listings", "bid_increment", "REAL")
        self._ensure_column("marketplace_listings", "starting_price", "REAL")
        self._ensure_column("marketplace_listings", "claim_price", "REAL")
        self._ensure_column("marketplace_listings", "card_rarity", "TEXT")
        self._ensure_column("marketplace_listings", "image_blob", "BLOB")
        self._ensure_column("marketplace_listings", "image_filename", "TEXT")
        self._ensure_column("marketplace_listings", "image_content_type", "TEXT")
        self._ensure_column("marketplace_listings", "deal_thread_id", "INTEGER")
        self._ensure_column("marketplace_listings", "deal_thread_parent_channel_id", "INTEGER")
        self._ensure_column("marketplace_listings", "deal_thread_action_channel_id", "INTEGER")
        self._ensure_column("marketplace_listings", "deal_thread_action_message_id", "INTEGER")
        self._ensure_column("marketplace_listings", "claimed_at", "TEXT")
        self._ensure_column("marketplace_listings", "resolution_reason", "TEXT")
        self._ensure_column("marketplace_listings", "deal_status", "TEXT")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_marketplace_listings_status
            ON marketplace_listings(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_marketplace_listings_seller
            ON marketplace_listings(seller_id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_bids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                bidder_id INTEGER NOT NULL,
                bidder_name TEXT,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                dm_channel_id INTEGER,
                dm_message_id INTEGER,
                counter_amount REAL,
                counter_status TEXT,
                counter_dm_channel_id INTEGER,
                counter_dm_message_id INTEGER,
                counter_created_at TEXT,
                counter_updated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (listing_id) REFERENCES marketplace_listings(message_id)
            )
        """)
        self._ensure_column("marketplace_bids", "counter_amount", "REAL")
        self._ensure_column("marketplace_bids", "counter_status", "TEXT")
        self._ensure_column("marketplace_bids", "counter_dm_channel_id", "INTEGER")
        self._ensure_column("marketplace_bids", "counter_dm_message_id", "INTEGER")
        self._ensure_column("marketplace_bids", "counter_created_at", "TEXT")
        self._ensure_column("marketplace_bids", "counter_updated_at", "TEXT")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_marketplace_bids_listing
            ON marketplace_bids(listing_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_marketplace_bids_bidder
            ON marketplace_bids(bidder_id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notify_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                set_name TEXT,
                subset TEXT,
                card_count TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notify_rules_user
            ON notify_rules(user_id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seller_profiles (
                user_id INTEGER PRIMARY KEY,
                ign TEXT,
                payment_methods TEXT,
                payment_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._ensure_column("seller_profiles", "ign", "TEXT")
        self._ensure_column("seller_profiles", "payment_notes", "TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_sheet_sales (
                listing_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Check schema version
        cursor.execute("SELECT value FROM metadata WHERE key = 'schema_version'")
        result = cursor.fetchone()
        if not result:
            cursor.execute(
                "INSERT INTO metadata (key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
        elif result["value"] != str(self.SCHEMA_VERSION):
            cursor.execute(
                "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                (str(self.SCHEMA_VERSION),),
            )

        self.conn.commit()

    def _ensure_column(self, table_name: str, column_name: str, definition: str) -> None:
        columns = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(column["name"] == column_name for column in columns):
            return
        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def get_metadata(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return default
        return row["value"]

    def set_metadata(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    @staticmethod
    def _coerce_card_count(value: Any) -> Any:
        if value in (None, ""):
            return value
        text = str(value)
        try:
            return int(text)
        except ValueError:
            return text

    def upsert_marketplace_listing(self, listing_data: Dict[str, Any]) -> None:
        """Create or update a persisted active marketplace listing."""
        message_id = listing_data.get("message_id")
        channel_id = listing_data.get("channel_id")
        if not message_id or not channel_id:
            return

        now = datetime.now(timezone.utc).isoformat()
        created_at = listing_data.get("created_at") or now
        listing_data.setdefault("created_at", created_at)
        seller = listing_data.get("seller")
        buyer = listing_data.get("buyer")
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO marketplace_listings (
                message_id, channel_id, guild_id,
                surface_message_id, surface_channel_id,
                player_names, set_name, subset, card_count, card_rarity,
                price, date_time, seller_id, seller_name,
                buyer_id, buyer_name, status, payment_methods, image_url,
                surface_image_url, image_blob, image_filename, image_content_type,
                listing_type, auction_end_at, bid_increment, starting_price, claim_price,
                claimed_at, resolution_reason, deal_status,
                deal_thread_id, deal_thread_parent_channel_id,
                deal_thread_action_channel_id, deal_thread_action_message_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                guild_id = excluded.guild_id,
                surface_message_id = excluded.surface_message_id,
                surface_channel_id = excluded.surface_channel_id,
                player_names = excluded.player_names,
                set_name = excluded.set_name,
                subset = excluded.subset,
                card_count = excluded.card_count,
                card_rarity = excluded.card_rarity,
                price = excluded.price,
                date_time = excluded.date_time,
                seller_id = excluded.seller_id,
                seller_name = excluded.seller_name,
                buyer_id = excluded.buyer_id,
                buyer_name = excluded.buyer_name,
                status = excluded.status,
                payment_methods = excluded.payment_methods,
                image_url = excluded.image_url,
                surface_image_url = excluded.surface_image_url,
                image_blob = COALESCE(excluded.image_blob, marketplace_listings.image_blob),
                image_filename = COALESCE(excluded.image_filename, marketplace_listings.image_filename),
                image_content_type = COALESCE(excluded.image_content_type, marketplace_listings.image_content_type),
                listing_type = excluded.listing_type,
                auction_end_at = excluded.auction_end_at,
                bid_increment = excluded.bid_increment,
                starting_price = excluded.starting_price,
                claim_price = excluded.claim_price,
                claimed_at = excluded.claimed_at,
                resolution_reason = excluded.resolution_reason,
                deal_status = excluded.deal_status,
                deal_thread_id = COALESCE(excluded.deal_thread_id, marketplace_listings.deal_thread_id),
                deal_thread_parent_channel_id = COALESCE(excluded.deal_thread_parent_channel_id, marketplace_listings.deal_thread_parent_channel_id),
                deal_thread_action_channel_id = COALESCE(excluded.deal_thread_action_channel_id, marketplace_listings.deal_thread_action_channel_id),
                deal_thread_action_message_id = COALESCE(excluded.deal_thread_action_message_id, marketplace_listings.deal_thread_action_message_id),
                updated_at = excluded.updated_at
            """,
            (
                int(message_id),
                int(channel_id),
                listing_data.get("guild_id"),
                listing_data.get("surface_message_id"),
                listing_data.get("surface_channel_id"),
                listing_data.get("player_names"),
                listing_data.get("set_name"),
                str(listing_data.get("subset", "N/A")),
                str(listing_data.get("card_count", 1)),
                listing_data.get("card_rarity"),
                float(listing_data.get("price", 0)),
                listing_data.get("date_time"),
                int(listing_data.get("seller_id")),
                getattr(seller, "display_name", None) or getattr(seller, "name", None) or listing_data.get("seller_name"),
                listing_data.get("buyer_id"),
                getattr(buyer, "display_name", None) or getattr(buyer, "name", None) or listing_data.get("buyer_name"),
                listing_data.get("status", "active"),
                listing_data.get("payment_methods"),
                listing_data.get("image_url"),
                listing_data.get("surface_image_url"),
                listing_data.get("image_bytes") if listing_data.get("image_bytes") is not None else listing_data.get("image_blob"),
                listing_data.get("image_filename"),
                listing_data.get("image_content_type"),
                listing_data.get("listing_type", "sale"),
                listing_data.get("auction_end_at"),
                listing_data.get("bid_increment"),
                listing_data.get("starting_price", listing_data.get("price")),
                listing_data.get("claim_price"),
                listing_data.get("claimed_at"),
                listing_data.get("resolution_reason"),
                listing_data.get("deal_status"),
                listing_data.get("deal_thread_id"),
                listing_data.get("deal_thread_parent_channel_id"),
                listing_data.get("deal_thread_action_channel_id"),
                listing_data.get("deal_thread_action_message_id"),
                created_at,
                now,
            ),
        )
        self.conn.commit()

    def transition_marketplace_listing(
        self,
        message_id: int,
        *,
        expected_statuses: set[str] | tuple[str, ...] | list[str],
        new_status: str,
        buyer_id: Optional[int] = None,
        buyer_name: Optional[str] = None,
        price: Optional[float] = None,
        claim_price: Optional[float] = None,
        claimed_at: Optional[str] = None,
        resolution_reason: Optional[str] = None,
        deal_status: Optional[str] = None,
        clear_buyer: bool = False,
    ) -> bool:
        """Atomically move a listing only if its persisted status is still expected."""
        statuses = [str(status).lower() for status in expected_statuses]
        if not statuses:
            return False
        now = datetime.now(timezone.utc).isoformat()
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [new_status, now]
        if clear_buyer:
            updates.extend([
                "buyer_id = NULL",
                "buyer_name = NULL",
                "claim_price = NULL",
                "claimed_at = NULL",
                "deal_status = NULL",
            ])
        else:
            if buyer_id is not None:
                updates.append("buyer_id = ?")
                params.append(int(buyer_id))
            if buyer_name is not None:
                updates.append("buyer_name = ?")
                params.append(str(buyer_name))
            if claim_price is not None:
                updates.append("claim_price = ?")
                params.append(float(claim_price))
            if claimed_at is not None:
                updates.append("claimed_at = ?")
                params.append(claimed_at)
        if price is not None:
            updates.append("price = ?")
            params.append(float(price))
        if resolution_reason is not None:
            updates.append("resolution_reason = ?")
            params.append(str(resolution_reason))
        if deal_status is not None:
            updates.append("deal_status = ?")
            params.append(str(deal_status))
        placeholders = ", ".join("?" for _ in statuses)
        params.extend([int(message_id), *statuses])
        cursor = self.conn.execute(
            f"""
            UPDATE marketplace_listings
            SET {', '.join(updates)}
            WHERE message_id = ?
              AND LOWER(status) IN ({placeholders})
            """,
            params,
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def transition_marketplace_deal_status(
        self,
        message_id: int,
        *,
        expected_deal_statuses: set[str] | tuple[str, ...] | list[str],
        new_deal_status: str,
    ) -> bool:
        """Atomically advance a still-open claimed deal."""
        statuses = [str(status).lower() for status in expected_deal_statuses]
        if not statuses:
            return False
        placeholders = ", ".join("?" for _ in statuses)
        cursor = self.conn.execute(
            f"""
            UPDATE marketplace_listings
            SET deal_status = ?, updated_at = ?
            WHERE message_id = ?
              AND LOWER(status) IN ('claimed', 'pending')
              AND LOWER(COALESCE(deal_status, 'claimed')) IN ({placeholders})
            """,
            (
                str(new_deal_status),
                datetime.now(timezone.utc).isoformat(),
                int(message_id),
                *statuses,
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def get_marketplace_listing_status(self, message_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM marketplace_listings WHERE message_id = ?",
            (int(message_id),),
        ).fetchone()
        return str(row["status"]) if row else None

    def get_marketplace_profile(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM seller_profiles WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def upsert_marketplace_profile(
        self,
        user_id: int,
        *,
        ign: str,
        payment_methods: str = "",
        payment_notes: str = "",
    ) -> None:
        ign = str(ign or "").strip()
        payment_methods = str(payment_methods or "").strip()
        payment_notes = str(payment_notes or "").strip()
        if not ign:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO seller_profiles (
                user_id, ign, payment_methods, payment_notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                ign = excluded.ign,
                payment_methods = excluded.payment_methods,
                payment_notes = excluded.payment_notes,
                updated_at = excluded.updated_at
            """,
            (int(user_id), ign, payment_methods, payment_notes, now, now),
        )
        self.conn.commit()

    def get_seller_payment_methods(self, user_id: int) -> Optional[str]:
        profile = self.get_marketplace_profile(user_id)
        return profile.get("payment_methods") if profile else None

    def upsert_seller_payment_methods(self, user_id: int, payment_methods: str) -> None:
        payment_methods = str(payment_methods or "").strip()
        if not payment_methods:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO seller_profiles (
                user_id, payment_methods, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                payment_methods = excluded.payment_methods,
                updated_at = excluded.updated_at
            """,
            (int(user_id), payment_methods, now, now),
        )
        self.conn.commit()

    def get_marketplace_user_stats(self, user_id: int) -> Dict[str, int]:
        """Return completed marketplace trust stats for a user."""
        user_id = int(user_id)
        listings_sold = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM marketplace_listings
            WHERE seller_id = ?
              AND LOWER(status) = 'sold'
            """,
            (user_id,),
        ).fetchone()[0]
        listings_bought = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM marketplace_listings
            WHERE buyer_id = ?
              AND LOWER(status) = 'sold'
            """,
            (user_id,),
        ).fetchone()[0]
        return {
            "listings_sold": int(listings_sold or 0),
            "listings_bought": int(listings_bought or 0),
        }

    def update_marketplace_listing_status(
        self,
        message_id: int,
        status: str,
        *,
        buyer_id: Optional[int] = None,
        buyer_name: Optional[str] = None,
        price: Optional[float] = None,
    ) -> None:
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if buyer_id is not None:
            updates.append("buyer_id = ?")
            params.append(buyer_id)
        if buyer_name is not None:
            updates.append("buyer_name = ?")
            params.append(buyer_name)
        if price is not None:
            updates.append("price = ?")
            params.append(price)
        params.append(message_id)
        cursor.execute(
            f"UPDATE marketplace_listings SET {', '.join(updates)} WHERE message_id = ?",
            params,
        )
        self.conn.commit()

    def clear_marketplace_listing_deal_thread(self, message_id: int) -> None:
        """Remove persisted private deal thread metadata for a listing."""
        if not message_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE marketplace_listings
            SET deal_thread_id = NULL,
                deal_thread_parent_channel_id = NULL,
                deal_thread_action_channel_id = NULL,
                deal_thread_action_message_id = NULL,
                updated_at = ?
            WHERE message_id = ?
            """,
            (now, int(message_id)),
        )
        self.conn.commit()

    def add_marketplace_bid(self, listing_id: int, bid: Dict[str, Any]) -> int:
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT INTO marketplace_bids (
                listing_id, bidder_id, bidder_name, amount,
                status, dm_channel_id, dm_message_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                bid.get("bidder_id") or bid.get("user_id"),
                bid.get("username") or bid.get("bidder_name"),
                bid.get("amount") or bid.get("bid_amount"),
                bid.get("status", "placed"),
                bid.get("dm_channel_id"),
                bid.get("dm_message_id"),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_marketplace_bid(self, bid_id: int, **fields: Any) -> None:
        allowed = {
            "status",
            "dm_channel_id",
            "dm_message_id",
            "counter_amount",
            "counter_status",
            "counter_dm_channel_id",
            "counter_dm_message_id",
            "counter_created_at",
            "counter_updated_at",
        }
        updates = []
        params = []
        for key, value in fields.items():
            if key in allowed:
                updates.append(f"{key} = ?")
                params.append(value)
        if not updates:
            return
        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(bid_id)
        self.conn.execute(
            f"UPDATE marketplace_bids SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        self.conn.commit()

    def supersede_other_marketplace_bids(self, listing_id: int, winning_bid_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE marketplace_bids
            SET
                status = 'superseded',
                counter_status = CASE
                    WHEN counter_status = 'pending' THEN 'superseded'
                    ELSE counter_status
                END,
                counter_updated_at = CASE
                    WHEN counter_status = 'pending' THEN ?
                    ELSE counter_updated_at
                END,
                updated_at = ?
            WHERE listing_id = ?
              AND id != ?
              AND LOWER(status) IN ('placed', 'active', 'pending', 'countered')
            """,
            (now, now, int(listing_id), int(winning_bid_id)),
        )
        self.conn.commit()

    def add_notify_rule(self, rule: Dict[str, Any]) -> int:
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            """
            INSERT INTO notify_rules (
                user_id, player_name, set_name, subset,
                card_count, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                rule.get("user_id"),
                rule.get("player_name") or "",
                rule.get("set_name"),
                rule.get("subset"),
                None if rule.get("card_count") is None else str(rule.get("card_count")),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_active_notify_rules(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notify_rules WHERE active = 1 ORDER BY created_at"
        ).fetchall()
        rules = []
        for row in rows:
            rule = dict(row)
            rule["card_count"] = self._coerce_card_count(rule.get("card_count"))
            rules.append(rule)
        return rules

    def deactivate_notify_rule(self, rule_id: int, user_id: int) -> bool:
        """Soft-delete one active notification rule owned by a user."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE notify_rules
            SET active = 0, updated_at = ?
            WHERE id = ? AND user_id = ? AND active = 1
            """,
            (now, int(rule_id), int(user_id)),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_open_marketplace_listings(self) -> List[Dict[str, Any]]:
        listing_rows = self.conn.execute(
            """
            SELECT * FROM marketplace_listings
            WHERE LOWER(status) IN ('active', 'open', 'claimed', 'pending')
            ORDER BY created_at
            """
        ).fetchall()
        listings = []
        for row in listing_rows:
            listing = dict(row)
            profile = self.get_marketplace_profile(listing.get("seller_id")) or {}
            if profile.get("payment_methods"):
                listing["payment_methods"] = profile["payment_methods"]
            listing["payment_notes"] = profile.get("payment_notes")
            listing["image_bytes"] = listing.get("image_blob")
            listing["card_count"] = self._coerce_card_count(listing.get("card_count"))
            bid_rows = self.conn.execute(
                """
                SELECT * FROM marketplace_bids
                WHERE listing_id = ?
                ORDER BY created_at
                """,
                (listing["message_id"],),
            ).fetchall()
            listing["bids"] = [
                {
                    "id": bid["id"],
                    "user_id": bid["bidder_id"],
                    "bidder_id": bid["bidder_id"],
                    "username": bid["bidder_name"],
                    "amount": bid["amount"],
                    "bid_amount": bid["amount"],
                    "status": bid["status"],
                    "dm_channel_id": bid["dm_channel_id"],
                    "dm_message_id": bid["dm_message_id"],
                    "counter_amount": bid["counter_amount"],
                    "counter_status": bid["counter_status"],
                    "counter_dm_channel_id": bid["counter_dm_channel_id"],
                    "counter_dm_message_id": bid["counter_dm_message_id"],
                    "counter_created_at": bid["counter_created_at"],
                    "counter_updated_at": bid["counter_updated_at"],
                    "created_at": bid["created_at"],
                    "updated_at": bid["updated_at"],
                }
                for bid in bid_rows
            ]
            if listing.get("buyer_id"):
                listing["claims"] = [{
                    "user_id": listing["buyer_id"],
                    "claimer_id": listing["buyer_id"],
                    "username": listing.get("buyer_name"),
                    "amount": listing.get("claim_price") or listing.get("price"),
                    "status": "claimed",
                }]
            listings.append(listing)
        return listings

    def add_marketplace_event(
        self,
        event_type: str,
        *,
        user_id: Optional[int] = None,
        listing_id: Optional[int] = None,
        details: Optional[Dict[str, Any] | str] = None,
    ) -> int:
        """Record a marketplace event for auditing and troubleshooting."""
        cursor = self.conn.cursor()
        timestamp = datetime.now(timezone.utc).isoformat()
        if isinstance(details, (dict, list)):
            details_text = json.dumps(details, default=str)
        elif details is None:
            details_text = None
        else:
            details_text = str(details)

        cursor.execute(
            """
            INSERT INTO marketplace_events (
                timestamp, event_type, user_id, listing_id, details
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (timestamp, event_type, user_id, listing_id, details_text),
        )
        self.conn.commit()
        return cursor.lastrowid

    def add_sale(
        self,
        player_names: str,
        set_name: str,
        subset: str,
        date_time: str,
        price: float,
        card_count: int,
        seller_id: Optional[int] = None,
        image_url: str = None,
        card_rarity: str = None,
        source_listing_id: int = None,
    ) -> int:
        """Add a new card sale record."""
        cursor = self.conn.cursor()
        now = datetime.now(timezone.utc).isoformat()

        cursor.execute(
            """
            INSERT OR IGNORE INTO card_sales (
                player_names, set_name, subset,
                date_time, price, card_count, seller_id,
                image_url, card_rarity, source_listing_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                player_names,
                set_name,
                str(subset),
                date_time,
                price,
                card_count,
                seller_id,
                image_url,
                card_rarity,
                source_listing_id,
                now,
                now,
            ),
        )
        self.conn.commit()
        if cursor.rowcount:
            return cursor.lastrowid
        if source_listing_id is not None:
            row = self.conn.execute(
                "SELECT id FROM card_sales WHERE source_listing_id = ?",
                (int(source_listing_id),),
            ).fetchone()
            return int(row["id"]) if row else 0
        return 0

    def query_player(
        self,
        player_name: Optional[str] = None,
        set_name: Optional[str] = None,
        cc: Optional[int] = None,
        subset: Optional[str] = None,
        limit: int = 50,
        card_rarity: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query sales, optionally filtered by player, set, subset, and card count."""
        cursor = self.conn.cursor()

        query = """
            SELECT *
            FROM card_sales
            WHERE 1 = 1
        """
        params: list[Any] = []

        if player_name:
            query += " AND LOWER(player_names) LIKE ?"
            params.append(f"%{player_name.lower()}%")

        if set_name is not None:
            query += " AND set_name = ?"
            params.append(set_name)

        if subset is not None:
            query += " AND subset = ?"
            params.append(subset)

        if str(cc) == "9999":
            cc = 999

        if cc is not None:
            query += " AND card_count = ?"
            params.append(cc)

        if card_rarity:
            query += " AND LOWER(COALESCE(card_rarity, '')) = ?"
            params.append(str(card_rarity).strip().lower())

        query += """
            ORDER BY
                COALESCE(
                    date(date_time),
                    CASE
                        WHEN instr(date_time, ' ') > 0 AND instr(date_time, ',') > instr(date_time, ' ') THEN
                            date(
                                trim(substr(date_time, instr(date_time, ',') + 1)) || '-' ||
                                CASE lower(substr(date_time, 1, instr(date_time, ' ') - 1))
                                    WHEN 'jan' THEN '01'
                                    WHEN 'january' THEN '01'
                                    WHEN 'feb' THEN '02'
                                    WHEN 'february' THEN '02'
                                    WHEN 'mar' THEN '03'
                                    WHEN 'march' THEN '03'
                                    WHEN 'apr' THEN '04'
                                    WHEN 'april' THEN '04'
                                    WHEN 'may' THEN '05'
                                    WHEN 'jun' THEN '06'
                                    WHEN 'june' THEN '06'
                                    WHEN 'jul' THEN '07'
                                    WHEN 'july' THEN '07'
                                    WHEN 'aug' THEN '08'
                                    WHEN 'august' THEN '08'
                                    WHEN 'sep' THEN '09'
                                    WHEN 'sept' THEN '09'
                                    WHEN 'september' THEN '09'
                                    WHEN 'oct' THEN '10'
                                    WHEN 'october' THEN '10'
                                    WHEN 'nov' THEN '11'
                                    WHEN 'november' THEN '11'
                                    WHEN 'dec' THEN '12'
                                    WHEN 'december' THEN '12'
                                END || '-' ||
                                printf(
                                    '%02d',
                                    CAST(substr(date_time, instr(date_time, ' ') + 1, instr(date_time, ',') - instr(date_time, ' ') - 1) AS INTEGER)
                                )
                            )
                    END
                ) DESC,
                id DESC
            LIMIT ?
        """
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def get_all_sales(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Get all sales records (with limit to avoid memory issues)."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM card_sales ORDER BY date_time DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def sync_from_sheets(self, sheets_data: List[Dict[str, Any]]):
        """Sync data from Google Sheets to the database."""
        cursor = self.conn.cursor()

        for row in sheets_data:
            # Parse the row to match our schema
            player_names = row.get("Player Name(s)", "").strip()
            set_name = row.get("Set", "")
            subset = row.get("Subset", "")
            le_val = row.get(
                "Limited Edition or Unlimited",
                row.get("Card Count", "Unlimited"),
            )
            try:
                if le_val is None:
                    card_count = 999
                else:
                    le_text = str(le_val).strip()

                    if not le_text or le_text.lower() == "unlimited":
                        card_count = 999
                    else:
                        card_count = int(le_text)
            except (ValueError, TypeError):
                card_count = 999
            date_time = row.get("Date + Time", "").strip()
            price_val = str(row.get("Price", 0)).replace("$", "").replace(",", "").strip()
            try:
                price = float(price_val) if price_val else 0
            except (ValueError, TypeError):
                # Skip rows where price is not a number (e.g., contains 'trade')
                continue

            source_listing_value = row.get("Source Listing ID") or row.get("Listing ID")
            try:
                source_listing_id = int(str(source_listing_value).strip()) if source_listing_value else None
            except (TypeError, ValueError):
                source_listing_id = None

            existing = None
            if source_listing_id is not None:
                existing = cursor.execute(
                    "SELECT id, source_listing_id FROM card_sales WHERE source_listing_id = ?",
                    (source_listing_id,),
                ).fetchone()
            if existing is None:
                existing = cursor.execute(
                    """
                    SELECT id, source_listing_id FROM card_sales
                    WHERE player_names = ? AND set_name = ? AND date_time = ? AND price = ?
                    """,
                    (player_names, set_name, date_time, price),
                ).fetchone()
            if existing is None:
                self.add_sale(
                    player_names=player_names,
                    set_name=set_name,
                    subset=subset,
                    date_time=date_time,
                    price=price,
                    card_count=card_count,
                    card_rarity=row.get("Rarity") or None,
                    source_listing_id=source_listing_id,
                )
            elif source_listing_id is not None and existing["source_listing_id"] is None:
                cursor.execute(
                    "UPDATE card_sales SET source_listing_id = ? WHERE id = ?",
                    (source_listing_id, existing["id"]),
                )
                self.conn.commit()

    def enqueue_pending_sheet_sale(self, listing_id: int, payload: Dict[str, Any], error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO pending_sheet_sales (
                listing_id, payload, attempts, last_error, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                payload = excluded.payload,
                attempts = pending_sheet_sales.attempts + 1,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (int(listing_id), json.dumps(payload, default=str), str(error), now, now),
        )
        self.conn.commit()

    def get_pending_sheet_sales(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM pending_sheet_sales ORDER BY created_at"
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            results.append(item)
        return results

    def complete_pending_sheet_sale(self, listing_id: int) -> None:
        self.conn.execute(
            "DELETE FROM pending_sheet_sales WHERE listing_id = ?",
            (int(listing_id),),
        )
        self.conn.commit()

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
