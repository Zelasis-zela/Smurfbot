import unicodedata

# Tier ids HenrikDev : 0 non classé, 3-5 Fer 1-3, ... 24-26 Immortel 1-3, 27 Radiant
GROUP_NAMES = ["Fer", "Bronze", "Argent", "Or", "Platine", "Diamant", "Ascendant", "Immortel", "Radiant"]
UNRANKED = "Non classé"
ICON_URL = "https://media.valorant-api.com/competitivetiers/03621f52-342b-cf4e-4f86-9350a49c6d04/{tier}/smallicon.png"

# (tier max, seuil HS %, groupe)
HS_THRESHOLDS = [(11, 30.0, "Fer–Argent"), (17, 33.0, "Or–Platine"), (23, 36.0, "Diamant–Ascendant"), (27, 40.0, "Immortel–Radiant")]
DEFAULT_HS_THRESHOLD = (33.0, "Or–Platine")

# Noms anglais (API, anciennes lignes de la base) et français
_GROUP_ALIASES = {
    "iron": 0, "fer": 0,
    "bronze": 1,
    "silver": 2, "argent": 2,
    "gold": 3, "or": 3,
    "platinum": 4, "platine": 4,
    "diamond": 5, "diamant": 5,
    "ascendant": 6,
    "immortal": 7, "immortel": 7,
    "radiant": 8,
}
_UNRANKED_ALIASES = {"unrated", "unranked", "non classe", "non-classe", "nonclasse"}


def _as_tier(tier):
    # Tier valide (3-27) en int, sinon None
    if isinstance(tier, bool):
        return None
    if isinstance(tier, float) and tier.is_integer():
        tier = int(tier)
    if isinstance(tier, int) and 3 <= tier <= 27:
        return tier
    return None


def tier_group(tier):
    tier = _as_tier(tier)
    if tier is None:
        return None
    return 8 if tier == 27 else (tier - 3) // 3


def group_name(group):
    if isinstance(group, int) and not isinstance(group, bool) and 0 <= group < len(GROUP_NAMES):
        return GROUP_NAMES[group]
    return UNRANKED


def tier_name(tier):
    tier = _as_tier(tier)
    if tier is None:
        return UNRANKED
    if tier == 27:
        return "Radiant"
    return f"{GROUP_NAMES[(tier - 3) // 3]} {(tier - 3) % 3 + 1}"


def tier_icon(tier):
    tier = _as_tier(tier)
    return ICON_URL.format(tier=tier if tier is not None else 0)


def _plain(text):
    # minuscules sans accents : "Non classé" -> "non classe"
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def tier_from_name(name):
    # "Gold 2" -> 13, "Or 2" -> 13, "Radiant" -> 27, "Unrated" / "Non classé" -> 0, inconnu -> None
    if not isinstance(name, str):
        return None
    plain = " ".join(_plain(name).split())
    if not plain:
        return None
    if plain in _UNRANKED_ALIASES:
        return 0
    parts = plain.split()
    group = _GROUP_ALIASES.get(parts[0])
    if group is None:
        return None
    if group == 8:
        return 27
    division = 1
    if len(parts) > 1:
        if not parts[1].isdigit() or not 1 <= int(parts[1]) <= 3:
            return None
        division = int(parts[1])
    return 3 + group * 3 + division - 1


def hs_threshold(tier):
    tier = _as_tier(tier)
    if tier is None:
        return DEFAULT_HS_THRESHOLD
    for max_tier, threshold, label in HS_THRESHOLDS:
        if max_tier >= tier:
            return threshold, label
    return DEFAULT_HS_THRESHOLD
