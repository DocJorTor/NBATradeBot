"""Google Sheets integration with database sync."""
from typing import Any, Dict, List, Optional

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from database import CardDatabase

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

    def query_player(self, player_name: Optional[str] = None, set_name: Optional[str] = None, cc: Optional[int] = None, subset: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query player sales from the database."""
        return self.db.query_player(player_name, set_name, cc, subset)

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
    ):
        """Add a sale to both the database and Google Sheets."""
        # Add to database
        self.db.add_sale(
            player_names=player_names,
            set_name=set_name,
            subset=subset,
            date_time=date_time,
            price=price,
            card_count=card_count,
            seller_id=seller_id,
            image_url=image_url,
        )

        # Add to Google Sheets as backup
        try:
            normalized_card_count = int(card_count)
        except (TypeError, ValueError):
            normalized_card_count = 999 if str(card_count).upper() == "ANY" else 1
        card_count_value = "Unlimited" if normalized_card_count >= 999 else str(normalized_card_count)

        row = [
            player_names,
            set_name,
            card_rarity or "Legendary",
            subset,
            card_count_value,
            price,
            date_time,
            "Discord Buy it Now",
        ]

        try:
            self.sheet.insert_row(row, index=2)
        except Exception as e:
            print(f"Warning: Could not append to Google Sheets: {e}")
