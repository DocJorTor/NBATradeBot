import discord
from difflib import SequenceMatcher
import re

from config import (
    format_subset_for_set,
    get_set_names,
    get_subset_groups,
    get_subset_variants,
    has_subset_variants,
)

ANY_VALUE = "ANY"
UNLIMITED_VALUE = 999
NO_SETS_VALUE = "__no_sets__"
NO_SUBSETS_VALUE = "__no_subsets__"


def _normalize_search(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _score_search(query: str, candidate: str) -> float:
    normalized_query = _normalize_search(query)
    normalized_candidate = _normalize_search(candidate)
    if not normalized_query or not normalized_candidate:
        return 0.0
    if normalized_query == normalized_candidate:
        return 1.0
    if normalized_query in normalized_candidate:
        return 0.95
    query_words = normalized_query.split()
    if query_words and all(word in normalized_candidate for word in query_words):
        return 0.85
    return SequenceMatcher(None, normalized_query, normalized_candidate).ratio()


def _rank_search_options(query: str, candidates: list[str], *, limit: int = 25) -> list[str]:
    ranked = [
        (candidate, _score_search(query, candidate))
        for candidate in candidates
        if candidate
    ]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [candidate for candidate, score in ranked[:limit] if score > 0]


def _selected_content(view, fallback: str) -> str:
    summary = getattr(view, "selected_summary", None)
    if callable(summary):
        return summary()
    return fallback


async def _refresh_search_view(interaction: discord.Interaction, view, message: str) -> None:
    try:
        await interaction.response.edit_message(content=message, view=view)
    except (discord.NotFound, discord.HTTPException):
        await interaction.response.send_message(message, ephemeral=True)


class SetSearchModal(discord.ui.Modal, title="Search Set"):
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        self.query = discord.ui.TextInput(
            label="Set search",
            placeholder="chrome sapphire, flagship, midnight...",
            required=True,
            max_length=100,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.query.value or "").strip()
        matches = _rank_search_options(query, get_set_names())
        if not matches:
            await interaction.response.send_message(
                f"No sets matched `{query}`.",
                ephemeral=True,
            )
            return

        selected_set = matches[0]
        self.parent_view.set_option_order = matches
        self.parent_view.set_value = selected_set
        self.parent_view.subset_group_value = None
        self.parent_view.subset_variant_value = None
        self.parent_view.subset_value = None
        self.parent_view.subset_option_order = None
        self.parent_view.variant_option_order = None

        self.parent_view.set_select.options = build_set_options(self.parent_view)
        if hasattr(self.parent_view, "subset_select"):
            self.parent_view.subset_select.disabled = False
            self.parent_view.subset_select.options = build_subset_options(self.parent_view)
            if getattr(self.parent_view, "subset_required", False):
                self.parent_view.subset_select.placeholder = "Select Subset"
            else:
                self.parent_view.subset_select.placeholder = "Select Subset (optional)"
        if hasattr(self.parent_view, "variant_select"):
            self.parent_view.variant_select.disabled = True
            self.parent_view.variant_select.options = build_variant_options(self.parent_view)
            self.parent_view.variant_select.placeholder = "Select Variant"

        message = _selected_content(
            self.parent_view,
            f"Selected closest set match: `{selected_set}`.",
        )
        if callable(getattr(self.parent_view, "selected_summary", None)):
            message = f"{message}\n\nSelected closest set match for `{query}`: `{selected_set}`."
        await _refresh_search_view(interaction, self.parent_view, message)


class SubsetSearchModal(discord.ui.Modal, title="Search Subset"):
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
        self.query = discord.ui.TextInput(
            label="Subset search",
            placeholder="gold, signatures, relic...",
            required=True,
            max_length=100,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        if not self.parent_view.set_value or self.parent_view.set_value == ANY_VALUE:
            await interaction.response.send_message(
                "Select a set before searching subsets.",
                ephemeral=True,
            )
            return

        query = str(self.query.value or "").strip()
        matches = _rank_search_options(query, get_subset_groups(self.parent_view.set_value))
        if not matches:
            await interaction.response.send_message(
                f"No subsets matched `{query}` for `{self.parent_view.set_value}`.",
                ephemeral=True,
            )
            return

        self.parent_view.subset_option_order = matches
        self.parent_view.subset_select.options = build_subset_options(self.parent_view)
        self.parent_view.subset_select.disabled = False
        message = _selected_content(
            self.parent_view,
            f"Showing subset matches for `{query}`. Choose one from the subset dropdown.",
        )
        if callable(getattr(self.parent_view, "selected_summary", None)):
            message = f"{message}\n\nShowing subset matches for `{query}`."
        await _refresh_search_view(interaction, self.parent_view, message)


def add_search_buttons(view, *, row: int = 4) -> None:
    set_button = discord.ui.Button(
        label="🔎 Search Set",
        style=discord.ButtonStyle.secondary,
        row=row,
    )
    subset_button = discord.ui.Button(
        label="🔎 Search Subset",
        style=discord.ButtonStyle.secondary,
        row=row,
    )

    async def on_search_set(interaction: discord.Interaction):
        await interaction.response.send_modal(SetSearchModal(view))

    async def on_search_subset(interaction: discord.Interaction):
        if not view.set_value or view.set_value == ANY_VALUE:
            await interaction.response.send_message(
                "Select a set before searching subsets.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(SubsetSearchModal(view))

    set_button.callback = on_search_set
    subset_button.callback = on_search_subset
    view.add_item(set_button)
    view.add_item(subset_button)


def make_option(label: str, value: str, selected_value: str | None = None) -> discord.SelectOption:
    """Build a Discord select option and mark it selected when appropriate."""
    value = str(value)
    selected_value = None if selected_value is None else str(selected_value)
    return discord.SelectOption(
        label=str(label)[:100],
        value=value[:100],
        default=value == selected_value,
    )


def _placeholder_option(label: str, value: str) -> discord.SelectOption:
    return discord.SelectOption(label=label[:100], value=value[:100], default=True)


def build_set_options(self):
    options = []
    if getattr(self, "set_optional", False):
        options.append(
            discord.SelectOption(
                label="Any",
                value=ANY_VALUE,
                default=self.set_value in (None, "", ANY_VALUE),
            )
        )
    set_names = get_set_names()
    preferred_order = getattr(self, "set_option_order", None)
    if preferred_order:
        preferred = [set_name for set_name in preferred_order if set_name in set_names]
        set_names = [*preferred, *[set_name for set_name in set_names if set_name not in preferred]]
    if self.set_value and self.set_value not in set_names and self.set_value != ANY_VALUE:
        options.append(
            discord.SelectOption(
                label=f"Current: {self.set_value}"[:100],
                value=str(self.set_value)[:100],
                default=True,
            )
        )
    options.extend([
        discord.SelectOption(
            label=set_name[:100],
            value=set_name[:100],
            default=(set_name == self.set_value),
        )
        for set_name in set_names
    ])
    if not options:
        return [_placeholder_option("No card sets configured", NO_SETS_VALUE)]
    return options[:25]


def build_subset_options(self):
    if not self.set_value or self.set_value == ANY_VALUE:
        return [discord.SelectOption(label="Any", value=ANY_VALUE, default=True)]
    selected_group = getattr(self, "subset_group_value", None) or self.subset_value
    any_is_selected = selected_group in (None, "", ANY_VALUE)
    options = []
    if not getattr(self, "subset_required", False):
        options.append(
            discord.SelectOption(
                label="Any",
                value=ANY_VALUE,
                default=any_is_selected,
            )
        )
    subsets = get_subset_groups(self.set_value)
    preferred_order = getattr(self, "subset_option_order", None)
    if preferred_order:
        preferred = [subset for subset in preferred_order if subset in subsets]
        subsets = [*preferred, *[subset for subset in subsets if subset not in preferred]]
    if selected_group and selected_group not in subsets and selected_group != ANY_VALUE:
        options.append(
            discord.SelectOption(
                label=f"Current: {selected_group}"[:100],
                value=str(selected_group)[:100],
                default=True,
            )
        )
    options.extend(
        discord.SelectOption(
            label=subset[:100],
            value=subset[:100],
            default=not any_is_selected and subset == selected_group,
        )
        for subset in subsets
    )
    if not options:
        return [_placeholder_option("No subsets configured for this set", NO_SUBSETS_VALUE)]
    return options[:25]


def build_variant_options(self):
    if not self.set_value or not getattr(self, "subset_group_value", None):
        return [discord.SelectOption(label="Select a subset first", value="select_subset_first")]

    variants = get_subset_variants(self.set_value, self.subset_group_value)
    if not variants:
        return [discord.SelectOption(label="No variants", value="no_variants", default=True)]

    preferred_order = getattr(self, "variant_option_order", None)
    if preferred_order:
        preferred = [variant for variant in preferred_order if variant in variants]
        variants = [*preferred, *[variant for variant in variants if variant not in preferred]]

    return [
        discord.SelectOption(
            label=variant[:100],
            value=variant[:100],
            default=variant == getattr(self, "subset_variant_value", None),
        )
        for variant in variants
    ][:25]


def build_card_count_options(selected_value: str | None = ANY_VALUE) -> list[discord.SelectOption]:
    card_counts = [1, 3, 5, 10, 15, 25, 35, 50, 75, 99, 100]
    cc_counts = [250, 500, 1000]

    return [
        make_option("Any Card Count", ANY_VALUE, selected_value),
        *[
            make_option(str(n), str(n), selected_value)
            for n in card_counts
        ],
        *[
            make_option(f"CC /{n}", str(n), selected_value)
            for n in cc_counts
        ],
        make_option("Unlimited", str(UNLIMITED_VALUE), selected_value),
    ]


async def on_card_count_select(self, interaction: discord.Interaction):
    val = self.card_count_select.values[0]
    if val == str(UNLIMITED_VALUE):
        self.card_count_value = UNLIMITED_VALUE
    elif val == ANY_VALUE:
        self.card_count_value = None
    else:
        self.card_count_value = int(val)
    for option in self.card_count_select.options:
        if option.value == ANY_VALUE:
            option.default = self.card_count_value is None
        elif option.value == str(UNLIMITED_VALUE):
            option.default = self.card_count_value == UNLIMITED_VALUE
        else:
            option.default = self.card_count_value == int(option.value)
    summary = getattr(self, "selected_summary", None)
    if callable(summary):
        await interaction.response.edit_message(content=summary(), view=self)
    else:
        await interaction.response.edit_message(view=self)


async def on_set_select(self, interaction: discord.Interaction, required: bool):
    selected = self.set_select.values[0]
    if selected == NO_SETS_VALUE:
        await interaction.response.edit_message(view=self)
        return
    self.set_value = None if selected == ANY_VALUE else selected
    self.subset_required = required
    self.subset_group_value = None
    self.subset_variant_value = None
    self.subset_value = None
    self.subset_option_order = None
    self.variant_option_order = None
    self.set_select.options = build_set_options(self)
    self.subset_select.disabled = not self.set_value
    self.subset_select.options = build_subset_options(self)
    if hasattr(self, "variant_select"):
        self.variant_select.disabled = True
        self.variant_select.options = build_variant_options(self)
        self.variant_select.placeholder = "Select Variant"
    if not required:
        self.subset_select.placeholder = "Select Subset (optional)"
    else:
        self.subset_select.placeholder = "Select Subset"
    summary = getattr(self, "selected_summary", None)
    if callable(summary):
        await interaction.response.edit_message(content=summary(), view=self)
    else:
        await interaction.response.edit_message(view=self)


async def on_subset_select(self, interaction: discord.Interaction):
    selected = self.subset_select.values[0]
    if selected == NO_SUBSETS_VALUE:
        await interaction.response.edit_message(view=self)
        return
    if selected == ANY_VALUE:
        self.subset_group_value = None
        self.subset_variant_value = None
        self.subset_value = None
    else:
        self.subset_group_value = selected
        self.subset_variant_value = None
        if has_subset_variants(self.set_value, selected):
            self.subset_value = None
        else:
            self.subset_value = selected

    self.subset_select.options = build_subset_options(self)
    if hasattr(self, "variant_select"):
        self.variant_select.disabled = not (
            self.set_value
            and self.subset_group_value
            and has_subset_variants(self.set_value, self.subset_group_value)
        )
        self.variant_select.options = build_variant_options(self)

    summary = getattr(self, "selected_summary", None)
    if callable(summary):
        await interaction.response.edit_message(content=summary(), view=self)
    else:
        await interaction.response.edit_message(view=self)


async def on_variant_select(self, interaction: discord.Interaction):
    self.subset_variant_value = self.variant_select.values[0]
    self.subset_value = format_subset_for_set(
        self.set_value,
        self.subset_group_value,
        self.subset_variant_value,
    )
    self.variant_select.options = build_variant_options(self)
    summary = getattr(self, "selected_summary", None)
    if callable(summary):
        await interaction.response.edit_message(content=summary(), view=self)
    else:
        await interaction.response.edit_message(view=self)
