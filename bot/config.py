VARIANT_PREFIX_MARKER = "{variant} "

SET_SUBSETS = {
    '2025-26 Flagship': ['Auto', 'Gold',
        {f'{VARIANT_PREFIX_MARKER}Signatures': ['Gold', 'Orange', 'Red', 'Black']},
        'Blue', 'Blue Sandglitter', 'Orange', 'Purple', 'Red', 'Black',
        'Entry Pack Signatures AWARD', 'Foilfractor', 'Foilfractor Signatures'],
    '2025-26 Topps Chrome': ['Gold Wave',
        {f'{VARIANT_PREFIX_MARKER}Signatures': ['Gold', 'Blue', 'Red', 'Green', 'Orange', 'Purple', 'Superfractor', 'Black']},
        'Superfractor', 'Signature Style', 'Frozenfractor', 'Ultraviolet All Stars', 'The Finals', 'Rock Stars', 'Future Stars',
        'Paradox', 'Glass Canvas', 'Patented', 'Helix', "Let's Go!", 'Topps Chrome Hobby Box'],
    '2026 Topps Chrome Sapphire': [
        {'Certified Signature Issue': ['Black', 'Gold', 'Orange', 'Padparadscha', 'Red']},
        {'Sky Write Signatures': ['Black', 'Gold', 'Orange', 'Padparadscha', 'Red']},
        {f'{VARIANT_PREFIX_MARKER}Base': ['Gold', 'Orange', 'Purple', 'Red', 'Black']},
        'Green', 'Padparadscha',
        {f'{VARIANT_PREFIX_MARKER}Signatures': ['Red', 'Gold', 'Orange', 'Padparadscha', 'Black']},
        {f'{VARIANT_PREFIX_MARKER}Next Stop Signatures': ['Red', 'Gold', 'Orange', 'Padparadscha', 'Black']},
        {'Signature Style': ['Gold', 'Orange', 'Padparadscha', 'Red', 'Black']},
        {f'{VARIANT_PREFIX_MARKER}Sapphire Selections': ['Red', 'Gold', 'Orange', 'Padparadscha', 'Black']}, 'Infinite Sapphire'],
    '2026 Topps Finest': ['Arrivals', 'Aura', 'Finishers',
        {f'{VARIANT_PREFIX_MARKER}Common': ['Gold', 'Orange', 'Red']},
        'Headliners', {'Masters Signatures': ['Gold', 'Orange', 'Red', 'Geometric', 'Superfractor']},
        {'Finest Rookie Signatures': ['Gold', 'Orange', 'Red', 'Superfractor']},
        {'Finest Signatures': ['Gold', 'Orange', 'Red', 'Superfractor']},
        {'Baseline Signatures': ['Gold', 'Orange', 'Red', 'Superfractor']},
        'Purple Geometric Common',
        'Superfractor', 'The Man', 'Pulse', 'Orange Geometric Rookie Signatures', 'Topps Finest Hobby Box'],
    '2026 Topps Chrome Cosmic':[{f'{VARIANT_PREFIX_MARKER}Base': ['Blue Moon', 'Green Space Dust', 'Black Eclipse', 'Red Flare', 'Superfractor']}, 'First Light', 'Pink First Light', 'Geocentric', 'Pink Geocentric', 'Re-Entry', 'Pink Re-Entry', {f'{VARIANT_PREFIX_MARKER}Interstellar Base': ['Gold', 'Orange']},
        {f'{VARIANT_PREFIX_MARKER}Signature': ['Orange Galactic', 'Gold Interstellar', 'Black Eclipse', 'Red Flare', 'Superfractor']}, {f'{VARIANT_PREFIX_MARKER}Singularity Signatures': ['Orange Galactic', 'Gold Interstellar', 'Black Eclipse', 'Red Flare', 'Superfractor']},
        {f'{VARIANT_PREFIX_MARKER}Alien Autographs': ['Orange Galactic', 'Gold Interstellar', 'Black Eclipse', 'Red Flare', 'Superfractor']},
        {f'{VARIANT_PREFIX_MARKER}Electro-Static Signatures': ['Orange Galactic', 'Gold Interstellar', 'Black Eclipse', 'Red Flare', 'Superfractor']}, {f'{VARIANT_PREFIX_MARKER}First Flight Signatures': ['Orange Galactic', 'Gold Interstellar', 'Black Eclipse', 'Red Flare', 'Superfractor']},
        'Superfractor Extraterrestrial Talent', 'Superfractor Propulsion', 'Superfractor Space Walk', 'Topps Chrome Cosmic Hobby Box'],
    '2025-26 Topps 1980-81': ['Auto', 'Gold',
        {f'{VARIANT_PREFIX_MARKER}Signatures': ['Gold', 'Red', 'Black']},
        'Blue', 'Orange', 'Red', 'Black', 'Entry Pack Signatures AWARD',
        'Foilfractor', 'Foilfractor Signatures',
        {f'{VARIANT_PREFIX_MARKER}Signatures AWARD': ['Orange', 'Gold']}, 'Topps Flagship Hobby Box'],
    '2025-26 Topps Now': ['Gold', 'Green', 'Orange', 'Black', 'Red', 'Foilfractor'],
    '2026 Bowman': [{f'{VARIANT_PREFIX_MARKER}Chrome Signatures': ['Purple', 'Blue', 'Green', 'Yellow', 'Gold', 'Orange', 'Black', 'Red', 'Superfractor']}, {f'{VARIANT_PREFIX_MARKER}Bowman Spotlights': ['Red', '', 'Superfractor']}, {f'{VARIANT_PREFIX_MARKER}Crystallized': ['Gold', 'Orange', '', 'Red', 'Superfractor']}, 'Etched in Glass Base', 'Superfractor Chrome Base', 'Superfractor Greatness Loading',
        'Superfractor Young Kings', 'Superfractor Rockstar Rookies', 'Superfractor Hobby Stars', 'Superfractor ROY Favorites', 'Anime'],
    '2026 All-Star': ['All Kings', 'All Kings Foilfractor', 'All Star Signatures Foilfractor', 'Helix Superfractor',
        {f'{VARIANT_PREFIX_MARKER}Booklet': ['Blue', 'Red']},
        {'Hidden Gems': ['Emerald', 'Pink', 'Purple', 'Yellow']},
        'Red Signatures AWARD', 'Retro Signatures Foilfractor', 'Retro All Star Signatures Foilfractor'],
    '2026 Topps Midnight': [
        {f'{VARIANT_PREFIX_MARKER}Base': ['Black Light', 'Moonbeam']},
        {f'{VARIANT_PREFIX_MARKER}Midnight Sun Signatures': ['Black Light', 'Moonbeam', 'Moonrise']},
        'Midnight Sun Rookie Signatures AWARD',
        {f'{VARIANT_PREFIX_MARKER}Relic Signatures': ['Black Light', 'Moonbeam', 'Moonrise', 'Twilight', 'Witching Hour']},
        {f'{VARIANT_PREFIX_MARKER}Stroke of Midnight Signatures': ['Black Light', 'Moonbeam', 'Moonrise', 'Witching Hour']},
        'Black Light Night Shade', 'Black Light Night Vision', 'Night Shade', 'Night Vision', 'Twilight Relic Signatures AWARD'],
    '2025 Inception Black': ['Base', 'Mercurial Booklet', 'Mercurial Relic Signatures', 'Superfractor Logoman', 'Volcanic Base', 'Volcanic Booklet', 
        'Volcanic Base AWARD', 'Volcanic Relic Signatures', 'Volcanic Relic AWARD', 'Volcanic Signatures', 'Volcanic Signatures AWARD'],
    '2026 Full Court': [{f'{VARIANT_PREFIX_MARKER}Signature': ['Gold', 'Foilfractor']}, {f'{VARIANT_PREFIX_MARKER}Signature Relic': ['Silver', 'Gold', 'Foilfractor']}, 'Foilfractor Base', 'Foilfractor Relic'],
    '2025-26 Power Players': ['Gold', 'Green', 'Orange', 'Purple', 'Red', 'Black', 'Blue',
        {f'{VARIANT_PREFIX_MARKER}AWARD': ['Blue', 'Green', 'Orange', 'Purple']}],
    '2026 Chromatographs': [{f'{VARIANT_PREFIX_MARKER}Signatures': ['Black', 'Red', 'Superfractor']}],
    '2025 Topps 1952': ['Blue', 'Gold', 'Black', 'Foilfractor'],
    '2026 Strata': [{f'{VARIANT_PREFIX_MARKER}Relic Signatures': ['Gold', 'Silver']}, 'Die Cut Signatures'],
    '2026 In The Name': [
        {f'{VARIANT_PREFIX_MARKER}Relic Signatures': ['Red', 'Platinum']},
        'Platinum Signatures', 'Platinum Relic', 'Platinum Booklet'],
    '2026 Reverence': [{f'{VARIANT_PREFIX_MARKER}Relic Signatures': ['Red', 'Platinum']}],
    '2026 Platinum': ['Fire Booklet', 'Fire Relic Signatures', 'Fire AWARD', 'Fire Dual Relic', 'Fire Signature'],
    '2026 Duos': ['Red', 'Purple', 'Black', {f'{VARIANT_PREFIX_MARKER}Signatures AWARD': ['Purple', 'Orange', 'Black']}],
    '2026 Springtime': [{f'{VARIANT_PREFIX_MARKER}Signature Relic': ['Blue', 'Gold']}],
    '2025-26 New Applicants': [{f'{VARIANT_PREFIX_MARKER}Signatures': ['Red', 'Gold']}, 'Red Signatures AWARD'],
    '2025-26 Stars of NBA': [{f'{VARIANT_PREFIX_MARKER}AWARD': ['Blue', 'Gold', 'Purple', 'Orange', 'Green']}, 'Green', 'Black', 'Foilfractor', 'Orange', 'Red', 'Gold'],
    '2026 MVP Vault': [{f'{VARIANT_PREFIX_MARKER}AWARD': ['Orange', 'Gold', 'Purple', 'Black']}, 'Purple', 'Black',],
    '2026 Comic Court': ['Comic'],
    '2026 Tenacity': ['Fire Signature', 'Gold Signature Relic', 'Fire Signature Relic'],
    '2025 12 Days of Topps': ['Contest Card Signatures'],
    '2025 Thanksgiving': ['Signatures'],
    '2025 Tip Off Collection': ['Signatures', 'Gold Relic Signatures AWARD', 'Gold Relic Signatures'],
    '2025-26 Base Tiers': ['Pink', 'Orange AWARD', 'Red'],
    '2025-26 Color Splash': ['Rainbow', 'Rainbow Signatures', 'Red AWARD', 'Red Signatures AWARD'],
    '2025-26 Franchise Fabrics': ['Relic', 'Relic AWARD', 'Franchise Fabric AWARD'],
    '2025-26 Genesis': ['Contest Card Signatures', {f'{VARIANT_PREFIX_MARKER}Shimmer AWARD': ['Red', 'Black']}, 'Red Shimmer'],
    '2025-26 Levitation x Rise to Stardom': ['Levitation Orange AWARD', 'Orange AWARD', 'Levitation Red AWARD', 'Red AWARD',
        'Orange', 'Green AWARD', 'Levitation Green AWARD', 'Black Signatures', 'Levitation Yellow AWARD'],
    '2025-26 Marks of Excellence': ['Red Signatures', {f'{VARIANT_PREFIX_MARKER}Signatures AWARD': ['Red', 'Gold']}, 'Red'],
    '2025-26 Translucent': ['Gold Speckle', 'Gold Speckle AWARD', 'Gold Signatures AWARD'],
    '2025-26 VIP': ['Relic', 'Base', 'Signatures'],
    '2026 Ball of Duty x Loading': ['Loading... Purple AWARD', {'Ball of Duty': ['Gold AWARD', 'Green AWARD', 'Purple AWARD', 'Red AWARD']}],
    '2026 Holiday': ['Glitter Relic', 'Red Glitter Signatures', 'Signatures', 'Green Signatures', 'Contest Signatures', 'Red Glitter Relic',
        'Glitter Relic AWARD', 'Purple Glitter Relic'],
    '2026 Holiday Ornaments': ['Ornaments', 'Nice List Glitter AWARD'],
    '2026 Mystic': [{f'{VARIANT_PREFIX_MARKER}Relic Signatures': ['White', 'Black', 'Rainbow']},
        {f'{VARIANT_PREFIX_MARKER}Relic Signatures AWARD': ['White', 'Black']}, 'Rainbow Signatures'],
    '2026 No Limit': ['Red', 'Black AWARD', 'Red AWARD'],
    '2026 Playoffs Base': ['Gold', 'Red', 'Rainbow', 'Gold Base'],
    '2026 Portraits': ['Gold Signatures'],
    '2026 Prominent Performers': ['Gold Signatures', 'Signatures'],
    '2026 Renowned': ['Platinum', 'Blue', 'Platinum AWARD'],
    '2026 Retro': [{f'{VARIANT_PREFIX_MARKER}Relic Signatures': ['Pink', 'Blue']}, 'Atomic', 'Atomic AWARD', 'White Relic Signatures AWARD',
        'Blue Signatures AWARD', 'Blue Signatures'],
    '2026 Rookie Rundown': ['Red', 'Black', 'Black Signatures', 'Orange', 'Green AWARD', 'Purple AWARD', 'Superfractor'],
    '2026 Signature Series': ['Gold Signatures'],
    '2026 St. Patricks Day': ['Gold Signatures', 'Gold Signatures AWARD'],
    '2026 Youthquake': ['Superfractor', 'Red', 'Orange', 'Gold', 'Green', 'Purple'],
    '2026 Tall Tales x Instinct': [{'Instinct': ['Gold', 'Green AWARD', 'Gold AWARD']}, 'Tall Tales Gold'],
    '2026 Weekly Tip Off': ['Black', 'Bronze', 'Red', 'Blue', 'Green'],
    '2026 Finals': [{f'{VARIANT_PREFIX_MARKER}Relic Signatures Prime': ['Platinum', 'Blue']}, {f'{VARIANT_PREFIX_MARKER}3 Patch Signature': ['Platinum', 'Gold']}, {f'{VARIANT_PREFIX_MARKER}Fanatical': ['Superfractor', 'Red']},
        'Foilfractor Retro Topps Basketball Signature', 'Foilfractor Topps Now', 'Red Finals MVP Booklet', 'Gold Finals MVP Booklet', 'Gold Retro Topps 1975-76 Basketball Signature'],
    '2026 Topps Three': ['Gold Base', 'Red Base', 'Rim Reapers Gold'],
    '2026 Topps Three Rookie Signatures': ['Gold Rookie Signature', 'Red Rookie Signature'],
    '2026 Generation Rising': ['Red'],
    '2026 Planetary Pursuit': ['Jupiter', 'Pluto', 'Neptune', 'Saturn', 'Uranus', 'Solar System AWARD'],
    '2026-27 Topps Now': ['Gold', 'Green', 'Orange', 'Black', 'Red', 'Foilfractor'],
}


def get_set_names() -> list[str]:
    """Return configured card set names in display order."""
    return list(SET_SUBSETS.keys())


def register_custom_set(set_name: str, subsets: list[str]) -> None:
    """Add or update a runtime set definition loaded from persistent storage."""
    clean_name = str(set_name or "").strip()
    clean_subsets = list(dict.fromkeys(str(value).strip() for value in subsets if str(value).strip()))
    if not clean_name or not clean_subsets:
        raise ValueError("Set name and at least one subset are required.")
    SET_SUBSETS[clean_name] = clean_subsets


def _iter_subset_items(set_name: str):
    return SET_SUBSETS.get(set_name, [])


def _display_subset_name(subset_name: str) -> str:
    subset_name = str(subset_name)
    if subset_name.startswith(VARIANT_PREFIX_MARKER):
        return subset_name.removeprefix(VARIANT_PREFIX_MARKER)
    return subset_name


def _uses_prefix_variant(subset_name: str) -> bool:
    return str(subset_name).startswith(VARIANT_PREFIX_MARKER)


def get_subset_groups(set_name: str) -> list[str]:
    """Return first-level subset choices for old and new config shapes."""
    groups = []
    for item in _iter_subset_items(set_name):
        if isinstance(item, str):
            groups.append(item)
        elif isinstance(item, dict):
            groups.extend(_display_subset_name(subset) for subset in item.keys())
    return groups


def get_subset_variants(set_name: str, subset_name: str) -> list[str]:
    """Return variant choices for a subset group, or an empty list."""
    for item in _iter_subset_items(set_name):
        if not isinstance(item, dict):
            continue
        for configured_subset, variants in item.items():
            if _display_subset_name(configured_subset) != subset_name:
                continue
            if isinstance(variants, str):
                return [variants]
            return [str(variant) for variant in variants]
    return []


def has_subset_variants(set_name: str, subset_name: str) -> bool:
    return bool(get_subset_variants(set_name, subset_name))


def format_subset(subset_name: str, variant: str | None = None) -> str:
    """Format a selected subset group and optional variant for storage/search."""
    subset_name = str(subset_name or "").strip()
    variant = str(variant or "").strip()
    if not variant:
        return subset_name
    return f"{subset_name} {variant}"


def format_subset_for_set(set_name: str, subset_name: str, variant: str | None = None) -> str:
    """Format a subset selection using the configured variant placement."""
    subset_name = str(subset_name or "").strip()
    variant = str(variant or "").strip()
    if not variant:
        return subset_name

    for item in _iter_subset_items(set_name):
        if not isinstance(item, dict):
            continue
        for configured_subset in item.keys():
            if _display_subset_name(configured_subset) != subset_name:
                continue
            if _uses_prefix_variant(configured_subset):
                return f"{variant} {subset_name}"

    return format_subset(subset_name, variant)


def get_all_subset_names(set_name: str) -> list[str]:
    """Return fully expanded subset names, matching the old string-only shape."""
    names = []
    for subset_name in get_subset_groups(set_name):
        variants = get_subset_variants(set_name, subset_name)
        if variants:
            names.extend(format_subset_for_set(set_name, subset_name, variant) for variant in variants)
        else:
            names.append(subset_name)
    return names
