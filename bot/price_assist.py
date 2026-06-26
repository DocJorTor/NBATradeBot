from __future__ import annotations

from typing import Any

import discord
from discord import ui


ANY_VALUE = "ANY"


def _format_card_count(card_count) -> str:
    if card_count in (None, "", "Any", ANY_VALUE):
        return "Any"
    if str(card_count) in {"999", "9999"}:
        return "Unlimited"
    return str(card_count)


def _format_price(value) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _normalize_text(value) -> str:
    return str(value or "").strip().casefold()


def _is_topps_now_set(value) -> bool:
    return "topps now" in _normalize_text(value)


def _normalize_card_count_for_query(value) -> int | None:
    text = str(value or "").strip()
    if not text or text.upper() == ANY_VALUE:
        return None
    if text.lower() == "unlimited":
        return 999
    try:
        card_count = int(text)
    except (TypeError, ValueError):
        return None
    return 999 if card_count == 9999 else card_count


def price_range_label(results: list[dict[str, Any]]) -> str:
    prices = []
    for result in results:
        try:
            prices.append(float(result["price"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not prices:
        return "N/A"

    low = min(prices)
    high = max(prices)
    if low == high:
        return _format_price(low)
    return f"{_format_price(low)}-{_format_price(high)}"


def query_price_source(
    source,
    *,
    player_name: str | None = None,
    set_name: str | None = None,
    cc=None,
    subset: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if source is None or not hasattr(source, "query_player"):
        return []

    try:
        return list(
            source.query_player(
                player_name=player_name,
                set_name=set_name,
                cc=cc,
                subset=subset,
                limit=limit,
            )
        )
    except TypeError:
        return list(
            source.query_player(
                player_name=player_name,
                set_name=set_name,
                cc=cc,
                subset=subset,
            )
        )[:limit]


def build_price_assist(source, listing_data: dict[str, Any]) -> dict[str, Any]:
    player_name = str(listing_data.get("player_names") or "").strip()
    set_name = str(listing_data.get("set_name") or "").strip()
    subset = str(listing_data.get("subset") or "").strip()
    card_count = _normalize_card_count_for_query(listing_data.get("card_count"))
    include_topps_now_similars = _is_topps_now_set(set_name)

    exact_candidates = query_price_source(
        source,
        player_name=player_name or None,
        set_name=set_name or None,
        subset=subset or None,
        limit=50,
    )
    normalized_player = _normalize_text(player_name)
    exact_results = [
        result for result in exact_candidates
        if _normalize_text(result.get("player_names")) == normalized_player
    ]

    if exact_results:
        price_range = price_range_label(exact_results)
        return {
            "mode": "exact",
            "results": exact_results,
            "range_label": price_range,
            "field_value": f"This exact card has sold for **{price_range}**.",
            "results_heading": f"Recent exact sales for **{player_name or 'this card'}**",
        }

    similar_results = []
    if normalized_player and card_count is not None:
        similar_candidates = query_price_source(
            source,
            player_name=player_name,
            cc=card_count,
            limit=50,
        )
        similar_results = [
            result for result in similar_candidates
            if _normalize_text(result.get("player_names")) == normalized_player
            and (
                include_topps_now_similars
                or not _is_topps_now_set(result.get("set_name"))
            )
        ][:10]

    if similar_results:
        price_range = price_range_label(similar_results)
        return {
            "mode": "similar",
            "results": similar_results,
            "range_label": price_range,
            "field_value": f"Similar {player_name} cards with the same CC have gone for **{price_range}**.",
            "results_heading": (
                f"Recent sales for **{player_name}** cards numbered "
                f"**/{_format_card_count(card_count)}**"
            ),
        }

    return {
        "mode": "none",
        "results": [],
        "range_label": None,
        "field_value": "Sorry, there were no similar matches found for this card",
        "results_heading": None,
    }


class PriceResultsView(ui.View):
    page_size = 5

    def __init__(
        self,
        results: list[dict[str, Any]],
        owner_id: int,
        heading: str,
        range_label: str | None = None,
    ):
        super().__init__(timeout=180)
        self.results = results
        self.owner_id = owner_id
        self.heading = heading
        self.range_label = range_label
        self.page = 0
        self.page_count = max(1, (len(results) + self.page_size - 1) // self.page_size)

        self.prev_button = ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=True,
        )
        self.prev_button.callback = self.on_previous
        self.add_item(self.prev_button)

        self.next_button = ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            row=0,
            disabled=self.page_count <= 1,
        )
        self.next_button.callback = self.on_next
        self.add_item(self.next_button)

    async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return False
        await interaction.response.send_message(
            "Only the user who opened this price search can page these results.",
            ephemeral=True,
        )
        return True

    def _current_slice(self) -> list[dict[str, Any]]:
        start = self.page * self.page_size
        return self.results[start:start + self.page_size]

    def _refresh_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.page_count - 1

    def content(self) -> str:
        start = self.page * self.page_size + 1
        end = min(len(self.results), start + self.page_size - 1)
        header = (
            f"{self.heading} "
            f"(showing {start}-{end} of {len(self.results)}, page {self.page + 1}/{self.page_count})"
        )
        if self.range_label:
            return f"{header}\nPrice Range: **{self.range_label}**"
        return header

    def embeds(self) -> list[discord.Embed]:
        embeds = []
        for latest in self._current_slice():
            embed = discord.Embed(
                title=latest["player_names"],
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="Set",
                value=latest.get("set_name", "Unknown Set"),
                inline=True,
            )
            embed.add_field(
                name="Subset",
                value=latest.get("subset", "N/A"),
                inline=True,
            )
            embed.add_field(
                name="Card Count",
                value=f"/{_format_card_count(latest.get('card_count', ANY_VALUE))}",
                inline=True,
            )
            embed.add_field(
                name="Price",
                value=_format_price(latest.get("price")),
                inline=True,
            )
            embed.add_field(
                name="Date",
                value=latest.get("date_time", "Unknown Date"),
                inline=True,
            )
            embeds.append(embed)
        return embeds

    async def on_previous(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        self.page = max(0, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            content=self.content(),
            embeds=self.embeds(),
            view=self,
        )

    async def on_next(self, interaction: discord.Interaction):
        if await self._reject_wrong_user(interaction):
            return
        self.page = min(self.page_count - 1, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            content=self.content(),
            embeds=self.embeds(),
            view=self,
        )
