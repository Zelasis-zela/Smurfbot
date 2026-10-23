import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.ranks import tier_group, group_name, tier_name

# Parsing des réponses HenrikDev et calculs purs (ni réseau, ni Discord).
# L'API renvoie parfois des objets null ou des champs absents : chaque lecture a une valeur de repli.

UNKNOWN_MAP = "Inconnue"
UNKNOWN_AGENT = "Inconnu"
DEFAULT_REGION = "eu"
LOBBY_MIN_OTHERS = 5     # une partie compte pour le lobby si on y trouve au moins 5 autres joueurs
COMPARE_MIN_SAMPLE = 8   # lignes minimum pour comparer au rang exact (ou proche)
NEAR_TIERS = 3           # "rang proche" : 3 divisions d'écart au plus


# ================= LECTURE SÛRE =================
def _int(value, default=0):
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _opt_int(value):
    return _int(value, None)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _name(value):
    # {"id": ..., "name": "Lotus"} ou directement "Lotus"
    if isinstance(value, dict):
        value = value.get("name")
    return value if isinstance(value, str) and value else None


def _tier_id(value):
    # {"id": 15, "name": "Platinum 1"} ou directement 15
    if isinstance(value, dict):
        value = value.get("id")
    return _opt_int(value)


def _short(value):
    short = _dict(value).get("short")
    return short if isinstance(short, str) else ""


def parse_ts(value):
    # ISO 8601 ("2026-10-22T18:06:34.201Z") ou timestamp unix -> int, sinon None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def _ratio(value, reference):
    if value is None or not reference:
        return None
    return value / reference


def _round_half_up(x):
    return int(math.floor(x + 0.5))


class _LineStats:
    # Propriétés communes à une ligne de stats (MatchStat, LobbyPlayer)
    @property
    def kd(self):
        return self.kills / max(1, self.deaths)

    @property
    def acs(self):
        return self.score / max(1, self.rounds)

    @property
    def adr(self):
        return self.damage / max(1, self.rounds)

    @property
    def hits(self):
        return self.headshots + self.bodyshots + self.legshots

    @property
    def hs_pct(self):
        hits = self.hits
        return self.headshots / hits * 100 if hits > 0 else None


# ================= COMPTE =================
@dataclass
class Account:
    puuid: str
    name: str
    tag: str
    region: str
    level: int | None
    card: str | None   # URL de card.small


def parse_account(data):
    if not isinstance(data, dict):
        raise ValueError("compte : réponse invalide")
    puuid = data.get("puuid")
    if not isinstance(puuid, str) or not puuid:
        raise ValueError("compte : puuid manquant")
    region = data.get("region")
    region = region.strip().lower() if isinstance(region, str) and region.strip() else DEFAULT_REGION
    card = _dict(data.get("card")).get("small")
    return Account(
        puuid=puuid,
        name=data.get("name") if isinstance(data.get("name"), str) else "",
        tag=data.get("tag") if isinstance(data.get("tag"), str) else "",
        region=region,
        level=_opt_int(data.get("account_level")),
        card=card if isinstance(card, str) and card else None,
    )


# ================= MMR (v3) =================
@dataclass
class SeasonInfo:
    short: str
    games: int
    wins: int
    end_tier: int


@dataclass
class MmrInfo:
    tier: int
    rr: int | None
    elo: int | None
    last_change: int | None
    games_needed: int
    peak_tier: int | None
    peak_season: str | None
    ranked_games: int | None          # somme des parties de toutes les saisons ; None si inconnu
    seasons: list[SeasonInfo]         # de la plus ancienne à la plus récente, comme l'API


def parse_mmr(data):
    if not isinstance(data, dict):
        raise ValueError("mmr : réponse invalide")
    current = _dict(data.get("current"))
    peak = _dict(data.get("peak"))
    peak_tier = _tier_id(peak.get("tier"))
    seasons = []
    for entry in _list(data.get("seasonal")):
        if not isinstance(entry, dict):
            continue
        seasons.append(SeasonInfo(
            short=_short(entry.get("season")),
            games=max(0, _int(entry.get("games"))),
            wins=max(0, _int(entry.get("wins"))),
            end_tier=_tier_id(entry.get("end_tier")) or 0,
        ))
    return MmrInfo(
        tier=_tier_id(current.get("tier")) or 0,
        rr=_opt_int(current.get("rr")),
        elo=_opt_int(current.get("elo")),
        last_change=_opt_int(current.get("last_change")),
        games_needed=max(0, _int(current.get("games_needed_for_rating"))),
        peak_tier=peak_tier if peak_tier and peak_tier > 0 else None,
        peak_season=_short(peak.get("season")) or None,
        ranked_games=sum(s.games for s in seasons) if seasons else None,
        seasons=seasons,
    )


# ================= PARTIES STOCKÉES (v1 stored-matches) =================
@dataclass
class MatchStat(_LineStats):
    match_id: str
    started_at: int
    map: str
    season: str
    agent: str
    tier: int
    level: int | None
    kills: int
    deaths: int
    assists: int
    score: int
    headshots: int
    bodyshots: int
    legshots: int
    damage: int
    rounds: int
    team_rounds: int
    enemy_rounds: int

    @property
    def result(self):
        if self.team_rounds > self.enemy_rounds:
            return "win"
        if self.team_rounds < self.enemy_rounds:
            return "loss"
        return "draw"


def _damage(value, key):
    # {"made": 3291, "received": 3024} / {"dealt": ..., "received": ...} ou un entier
    if isinstance(value, dict):
        return max(0, _int(value.get(key)))
    return max(0, _int(value))


def _parse_stored_match(entry):
    if not isinstance(entry, dict):
        return None
    meta, stats, teams = _dict(entry.get("meta")), _dict(entry.get("stats")), _dict(entry.get("teams"))
    mode = meta.get("mode")
    if not isinstance(mode, str) or mode.casefold() != "competitive":
        return None
    match_id = meta.get("id")
    if not isinstance(match_id, str) or not match_id or not stats:
        return None
    red, blue = _opt_int(teams.get("red")), _opt_int(teams.get("blue"))
    team = stats.get("team")
    team = team.casefold() if isinstance(team, str) else ""
    if red is None or blue is None or team not in ("red", "blue"):
        return None  # sans le score des équipes, ni ACS ni résultat ne sont calculables
    shots = _dict(stats.get("shots"))
    own, enemy = (red, blue) if team == "red" else (blue, red)
    return MatchStat(
        match_id=match_id,
        started_at=parse_ts(meta.get("started_at")) or 0,
        map=_name(meta.get("map")) or UNKNOWN_MAP,
        season=_short(meta.get("season")),
        agent=_name(stats.get("character")) or UNKNOWN_AGENT,
        tier=_tier_id(stats.get("tier")) or 0,
        level=_opt_int(stats.get("level")),
        kills=max(0, _int(stats.get("kills"))),
        deaths=max(0, _int(stats.get("deaths"))),
        assists=max(0, _int(stats.get("assists"))),
        score=max(0, _int(stats.get("score"))),
        headshots=max(0, _int(shots.get("head"))),
        bodyshots=max(0, _int(shots.get("body"))),
        legshots=max(0, _int(shots.get("leg"))),
        damage=_damage(stats.get("damage"), "made"),
        rounds=max(1, red + blue),
        team_rounds=own,
        enemy_rounds=enemy,
    )


def parse_stored_matches(data):
    # De la plus récente à la plus ancienne ; les entrées illisibles ou non classées sont ignorées
    matches = []
    for entry in _list(data):
        match = _parse_stored_match(entry)
        if match is not None:
            matches.append(match)
    return matches


# ================= HISTORIQUE DE RR (v2 mmr-history) =================
@dataclass
class RrChange:
    match_id: str
    date: int
    season: str
    tier: int
    tier_before: int | None
    last_change: int
    refunded: bool
    derank_protected: bool
    placement: bool


def parse_mmr_history(data):
    # De la plus récente à la plus ancienne, comme l'API
    if isinstance(data, dict):
        history = data.get("history")
    else:
        history = data
    if not isinstance(history, list):
        raise ValueError("historique de RR : réponse invalide")
    changes = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        match_id = entry.get("match_id")
        changes.append(RrChange(
            match_id=match_id if isinstance(match_id, str) else "",
            date=parse_ts(entry.get("date")) or 0,
            season=_short(entry.get("season")),
            tier=_tier_id(entry.get("tier")) or 0,
            tier_before=_tier_id(entry.get("tier_before_update")),
            last_change=_int(entry.get("last_change")),
            refunded=_int(entry.get("refunded_rr")) > 0 or entry.get("refunded_rr") is True,
            derank_protected=bool(entry.get("was_derank_protected")),
            placement=bool(entry.get("is_placement_match")),
        ))
    return changes


# ================= DÉTAIL D'UNE PARTIE (v4 match) =================
@dataclass
class LobbyPlayer(_LineStats):
    puuid: str
    team: str               # "red" / "blue" (minuscules)
    party_id: str | None
    agent: str
    tier: int
    level: int | None
    name: str
    tag: str
    kills: int
    deaths: int
    assists: int
    score: int
    headshots: int
    bodyshots: int
    legshots: int
    damage: int
    rounds: int


@dataclass
class LobbyMatch:
    match_id: str
    map: str
    started_at: int
    rounds: int
    players: list[LobbyPlayer]
    teams_rounds: dict = field(default_factory=dict)   # {"red": manches gagnées, "blue": ...}

    @property
    def winner(self):
        red, blue = self.teams_rounds.get("red"), self.teams_rounds.get("blue")
        if red is None or blue is None or red == blue:
            return None
        return "red" if red > blue else "blue"

    def player(self, puuid):
        return next((p for p in self.players if p.puuid == puuid), None)


def parse_match_v4(data):
    if not isinstance(data, dict):
        return None
    meta = _dict(data.get("metadata"))
    match_id = meta.get("match_id")
    if not isinstance(match_id, str) or not match_id:
        return None
    teams_rounds, lost = {}, {}
    for team in _list(data.get("teams")):
        team_id = _dict(team).get("team_id")
        rounds = _dict(_dict(team).get("rounds"))
        won = _opt_int(rounds.get("won"))
        if isinstance(team_id, str) and team_id and won is not None:
            teams_rounds[team_id.casefold()] = max(0, won)
            lost[team_id.casefold()] = _opt_int(rounds.get("lost"))
    if not teams_rounds:
        return None  # nombre de manches inconnu : ACS / ADR incalculables
    total = sum(teams_rounds.values())
    if len(teams_rounds) == 1:
        only = next(iter(teams_rounds))
        total += max(0, lost.get(only) or 0)
    total = max(1, total)

    players = []
    for entry in _list(data.get("players")):
        if not isinstance(entry, dict):
            continue
        puuid = entry.get("puuid")
        if not isinstance(puuid, str) or not puuid:
            continue
        stats = _dict(entry.get("stats"))
        team = entry.get("team_id")
        party = entry.get("party_id")
        players.append(LobbyPlayer(
            puuid=puuid,
            team=team.casefold() if isinstance(team, str) else "",
            party_id=party if isinstance(party, str) and party else None,
            agent=_name(entry.get("agent")) or UNKNOWN_AGENT,
            tier=_tier_id(entry.get("tier")) or 0,
            level=_opt_int(entry.get("account_level")),
            name=entry.get("name") if isinstance(entry.get("name"), str) else "",
            tag=entry.get("tag") if isinstance(entry.get("tag"), str) else "",
            kills=max(0, _int(stats.get("kills"))),
            deaths=max(0, _int(stats.get("deaths"))),
            assists=max(0, _int(stats.get("assists"))),
            score=max(0, _int(stats.get("score"))),
            headshots=max(0, _int(stats.get("headshots"))),
            bodyshots=max(0, _int(stats.get("bodyshots"))),
            legshots=max(0, _int(stats.get("legshots"))),
            damage=_damage(stats.get("damage"), "dealt"),
            rounds=total,
        ))
    if len(players) < 2:
        return None
    return LobbyMatch(
        match_id=match_id,
        map=_name(meta.get("map")) or UNKNOWN_MAP,
        started_at=parse_ts(meta.get("started_at")) or 0,
        rounds=total,
        players=players,
        teams_rounds=teams_rounds,
    )


# ================= AGRÉGATS =================
@dataclass
class Aggregate:
    n: int
    kills: int
    deaths: int
    assists: int
    kd: float | None
    acs: float | None
    adr: float | None
    hs_pct: float | None
    hits: int
    wins: int
    losses: int
    draws: int
    winrate: float | None          # 0-100
    kd_consistency: float | None   # part (0-1) des parties avec un K/D >= 1,2


def aggregate(matches):
    matches = [m for m in (matches or []) if m is not None]
    n = len(matches)
    kills = sum(m.kills for m in matches)
    deaths = sum(m.deaths for m in matches)
    assists = sum(m.assists for m in matches)
    hits = sum(m.hits for m in matches)
    wins = sum(1 for m in matches if m.result == "win")
    losses = sum(1 for m in matches if m.result == "loss")
    draws = n - wins - losses
    if n == 0:
        return Aggregate(0, 0, 0, 0, None, None, None, None, 0, 0, 0, 0, None, None)
    rounds = sum(m.rounds for m in matches)
    headshots = sum(m.headshots for m in matches)
    return Aggregate(
        n=n, kills=kills, deaths=deaths, assists=assists,
        kd=kills / max(1, deaths),
        acs=sum(m.score for m in matches) / max(1, rounds),
        adr=sum(m.damage for m in matches) / max(1, rounds),
        hs_pct=headshots / hits * 100 if hits > 0 else None,
        hits=hits, wins=wins, losses=losses, draws=draws,
        winrate=wins / n * 100,
        kd_consistency=sum(1 for m in matches if m.kd >= 1.2) / n,
    )


# ================= LOBBY =================
@dataclass
class LobbySummary:
    n: int                              # parties où le joueur a été trouvé avec au moins 5 autres joueurs
    acs_ratio: float | None             # moyenne sur les parties de (valeur du joueur / moyenne des autres)
    adr_ratio: float | None
    median_other_tier: int | None       # médiane des rangs (> 0) des autres joueurs, arrondie
    per_match: list[tuple[str, float | None, float | None]]   # (match_id, acs_ratio, adr_ratio)


def lobby_summary(lobbies, puuid):
    found = False
    per_match, other_tiers = [], []
    for match in lobbies or []:
        if match is None:
            continue
        me = match.player(puuid)
        if me is None:
            continue
        found = True
        others = [p for p in match.players if p.puuid != puuid]
        if len(others) < LOBBY_MIN_OTHERS:
            continue
        acs_ratio = _ratio(me.acs, _mean(p.acs for p in others))
        adr_ratio = _ratio(me.adr, _mean(p.adr for p in others))
        per_match.append((match.match_id, acs_ratio, adr_ratio))
        other_tiers.extend(p.tier for p in others if p.tier > 0)
    if not found:
        return None
    return LobbySummary(
        n=len(per_match),
        acs_ratio=_mean(r[1] for r in per_match),
        adr_ratio=_mean(r[2] for r in per_match),
        median_other_tier=_round_half_up(statistics.median(other_tiers)) if other_tiers else None,
        per_match=per_match,
    )


def reference_tier(mmr, lobby, matches):
    # Rang auquel comparer le joueur, et d'où il vient
    if mmr is not None and mmr.tier > 0 and mmr.games_needed == 0:
        return mmr.tier, "current"
    if lobby is not None and lobby.median_other_tier:
        return lobby.median_other_tier, "lobby"
    for match in matches or []:
        if match.tier > 0:
            return match.tier, "matches"
    if mmr is not None:
        for season in reversed(mmr.seasons):
            if season.end_tier > 0:
                return season.end_tier, "last_act"
    return None, "unknown"


# ================= COMPARAISON AU RANG =================
@dataclass
class RankComparison:
    scope: str            # "rank" | "near" | "lobby"
    label: str            # "Or" | "Or 1 à Platine 1" | "joueurs de ses lobbys"
    matches: int          # parties de lobby utilisées
    sample: int           # lignes d'autres joueurs utilisées
    ref: dict             # {"kd", "hs_pct", "acs", "adr", "level"} moyennes de l'échantillon (level = médiane)
    player: dict          # mêmes clés, le joueur sur ces mêmes parties (level = niveau du compte)


def _lines_stats(lines):
    kills = sum(p.kills for p in lines)
    deaths = sum(p.deaths for p in lines)
    hits = sum(p.hits for p in lines)
    headshots = sum(p.headshots for p in lines)
    return {
        "kd": kills / max(1, deaths) if lines else None,
        "hs_pct": headshots / hits * 100 if hits > 0 else None,
        "acs": _mean(p.acs for p in lines),
        "adr": _mean(p.adr for p in lines),
    }


def rank_averages(lobbies, puuid, ref_tier, account_level):
    used, mine, lines = 0, [], []
    for match in lobbies or []:
        if match is None:
            continue
        me = match.player(puuid)
        if me is None:
            continue
        used += 1
        mine.append(me)
        lines.extend(p for p in match.players if p.puuid != puuid)
    if not used or not lines:
        return None

    scope, label, sample = "lobby", "joueurs de ses lobbys", lines
    group = tier_group(ref_tier)
    if group is not None:
        same = [p for p in lines if tier_group(p.tier) == group]
        near = [p for p in lines if tier_group(p.tier) is not None and abs(p.tier - ref_tier) <= NEAR_TIERS]
        if len(same) >= COMPARE_MIN_SAMPLE:
            scope, label, sample = "rank", group_name(group), same
        elif len(near) >= COMPARE_MIN_SAMPLE:
            low, high = min(p.tier for p in near), max(p.tier for p in near)
            label = tier_name(low) if low == high else f"{tier_name(low)} à {tier_name(high)}"
            scope, sample = "near", near

    levels = [p.level for p in sample if p.level is not None]
    ref = _lines_stats(sample)
    ref["level"] = _round_half_up(statistics.median(levels)) if levels else None
    player = _lines_stats(mine)
    player["level"] = account_level
    return RankComparison(scope=scope, label=label, matches=used, sample=len(sample), ref=ref, player=player)
