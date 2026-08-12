"""Google Sheets integration with database sync."""
from typing import Any, Dict, List, Optional

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from database import CardDatabase
from logger import LOGGER


class PriceSheet:
    """Integration between Google Sheets and local SQLite database."""

    REQUIRED_HEADERS = {"Player Name(s)", "Set", "Price"}

    def __init__(
        self,
        spreadsheet_id: str,
        credentials_file: str,
        db_path: str = "cards.db",
    ):
        self.spreadsheet_id = spreadsheet_id
        self.credentials_file = credentials_file
        self.db = CardDatabase(db_path)
        self.client = self._authorize()
        self.sheet = self.client.open_by_key(spreadsheet_id).sheet1

    def _authorize(self) -> gspread.Client:
        """Authorize with Google Sheets API."""
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(self.credentials_file, scopes)
        return gspread.authorize(creds)

    def sync_sheets_to_db(self):
        """Fetch data from Google Sheets and sync to local database."""
        rows = self._get_sheet_records()
        if not rows:
            print("No data found in Google Sheets.")
            return 0
        self.db.sync_from_sheets(rows)
        return len(rows)

    def _get_sheet_records(self) -> List[Dict[str, Any]]:
        """Read sheet rows while tolerating blank/duplicate header cells."""
        values = self.sheet.get_all_values()
        if not values:
            return []

        header_index = self._find_header_row(values)
        if header_index is None:
            return []

        headers = values[header_index]
        header_positions = {}
        for index, header in enumerate(headers):
            header = str(header).strip()
            if header and header not in header_positions:
                header_positions[header] = index

        records = []
        for row in values[header_index + 1:]:
            if not any(str(cell).strip() for cell in row):
                continue

            record = {}
            for header, index in header_positions.items():
                record[header] = row[index].strip() if index < len(row) else ""
            records.append(record)

        return records

    def _find_header_row(self, values: List[List[str]]) -> Optional[int]:
        for index, row in enumerate(values[:10]):
            headers = {str(cell).strip() for cell in row if str(cell).strip()}
            if self.REQUIRED_HEADERS.issubset(headers):
                return index
        return None

    def get_all_sales(self) -> List[Dict[str, Any]]:
        """Get all sales from the database."""
        return self.db.get_all_sales()

    def query_player(self, player_name: Optional[str] = None, set_name: Optional[str] = None, cc: Optional[int] = None, subset: Optional[str] = None, card_rarity: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Query player sales from the database."""
        return self.db.query_player(player_name, set_name, cc, subset, limit=limit, card_rarity=card_rarity)

    def _insert_sale_row(self, payload: Dict[str, Any]) -> None:
        values = self.sheet.get_all_values()
        header_index = self._find_header_row(values)
        if header_index is None:
            raise ValueError("Could not find the Google Sheet header row.")
        headers = [str(header).strip() for header in values[header_index]]
        source_listing_id = payload.get("source_listing_id")
        source_header = next(
            (name for name in ("Source Listing ID", "Listing ID") if name in headers),
            None,
        )
        if source_listing_id is not None and source_header:
            source_index = headers.index(source_header)
            for row in values[header_index + 1:]:
                if source_index < len(row) and str(row[source_index]).strip() == str(source_listing_id):
                    return
        aliases = {
            "Player Name(s)": payload.get("player_names", ""),
            "Set": payload.get("set_name", ""),
            "Rarity": payload.get("card_rarity") or "",
            "Subset": payload.get("subset", ""),
            "Limited Edition or Unlimited": payload.get("card_count_value", ""),
            "Card Count": payload.get("card_count_value", ""),
            "Price": payload.get("price", ""),
            "Date + Time": payload.get("date_time", ""),
            "Source": "Discord Marketplace",
            "Sale Type": "Discord Marketplace",
            "Source Listing ID": str(source_listing_id or ""),
            "Listing ID": str(source_listing_id or ""),
            "Listing Kind": payload.get("listing_kind", "player"),
            "Set Cards Owned": payload.get("set_cards_owned", ""),
            "Set Cards Total": payload.get("set_cards_total", ""),
            "Includes Award": "Yes" if payload.get("includes_award") else "No",
            "Missing Cards": payload.get("missing_cards", ""),
        }
        row = [aliases.get(header, "") for header in headers]
        self.sheet.insert_row(row, index=header_index + 2)

    def add_sale(
        self,
        player_names: str,
        set_name: str,
        subset: str,
        date_time: str,
        price: float,
        card_count: int = 1,
        seller_id: int = None,
        image_url: str = None,
        card_rarity: str = "Legendary",
        source_listing_id: int = None,
        listing_kind: str = "player",
        set_cards_owned: int = None,
        set_cards_total: int = None,
        includes_award: bool = False,
        missing_cards: str = None,
    ):
        """Add a sale to both the database and Google Sheets."""
        # Add to database
        sale_id = self.db.add_sale(
            player_names=player_names,
            set_name=set_name,
            subset=subset,
            date_time=date_time,
            price=price,
            card_count=card_count,
            seller_id=seller_id,
            image_url=image_url,
            card_rarity=card_rarity,
            source_listing_id=source_listing_id,
            listing_kind=listing_kind,
            set_cards_owned=set_cards_owned,
            set_cards_total=set_cards_total,
            includes_award=includes_award,
            missing_cards=missing_cards,
        )

        # Add to Google Sheets as backup
        try:
            normalized_card_count = int(card_count)
        except (TypeError, ValueError):
            normalized_card_count = 999 if str(card_count).upper() == "ANY" else 1
        card_count_value = "Unlimited" if normalized_card_count >= 999 else str(normalized_card_count)

        payload = {
            "player_names": player_names,
            "set_name": set_name,
            "card_rarity": card_rarity,
            "subset": subset,
            "card_count_value": card_count_value,
            "price": price,
            "date_time": date_time,
            "seller_id": seller_id,
            "image_url": image_url,
            "source_listing_id": source_listing_id,
            "listing_kind": listing_kind,
            "set_cards_owned": set_cards_owned,
            "set_cards_total": set_cards_total,
            "includes_award": includes_award,
            "missing_cards": missing_cards,
        }

        try:
            self._insert_sale_row(payload)
            if source_listing_id is not None:
                self.db.complete_pending_sheet_sale(source_listing_id)
            return {"sale_id": sale_id, "sheet_synced": True}
        except Exception as e:
            LOGGER.warning("Could not append marketplace sale to Google Sheets: %s", e)
            if source_listing_id is not None:
                self.db.enqueue_pending_sheet_sale(source_listing_id, payload, str(e))
            return {"sale_id": sale_id, "sheet_synced": False, "error": str(e)}

    def retry_pending_sheet_sales(self) -> int:
        completed = 0
        for pending in self.db.get_pending_sheet_sales():
            try:
                self._insert_sale_row(pending.get("payload") or {})
            except Exception as exc:
                self.db.enqueue_pending_sheet_sale(
                    pending["listing_id"], pending.get("payload") or {}, str(exc)
                )
                continue
            self.db.complete_pending_sheet_sale(pending["listing_id"])
            completed += 1
        return completed
