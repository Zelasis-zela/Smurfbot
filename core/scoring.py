import statistics
from collections import defaultdict
from dataclasses import dataclass

from core import fmt
from core.analysis import aggregate, UNKNOWN_AGENT
from core.ranks import tier_name, hs_threshold

# Indice de suspicion : 9 critères pondérés, total plafonné à 100. Purement indicatif.
REPORT_THRESHOLD = 61
SMALL_SAMPLE_CAP = 60     # moins de 5 parties : indice plafonné
QUICK_CAP = 60            # estimation sur une seule partie (/smurf-match)

LEVELS = [  # (indice max, clé, emoji, libellé, description, couleur Discord)
    (30, "faible", "🟢", "Faible", "profil cohérent avec le rang", 0x2ECC71),
    (60, "modere", "🟡", "Modéré", "quelques signaux, à surveiller", 0xF1C40F),
    (80, "eleve", "🟠", "Élevé", "plusieurs signaux convergents", 0xE67E22),
    (100, "tres_eleve", "🔴", "Très élevé", "profil fortement atypique", 0xE74C3C),
]

CRITERIA = [  # (clé, points max) dans l'ordre des critères 1 à 9
    ("level_rank", 20), ("headshot", 18), ("lobby_perf", 20), ("kd", 16), ("winrate", 14),
    ("rr_gains", 18), ("few_games", 14), ("agent_pool", 10), ("placement", 14),
]
MAX_POINTS = dict(CRITERIA)
_ORDER = {key: i for i, (key, _) in enumerate(CRITERIA)}

RETURNING_GAP = 6        # pic au moins 6 divisions au-dessus du rang de référence : retour de pause probable

MISSING_NO_MATCHES = "Aucune partie classée récente : les critères de performance n'ont pas pu être évalués."
MISSING_LEVEL = "Niveau du compte inconnu."
MISSING_REF_TIER = ("Rang de référence inconnu : niveau du compte non comparé au rang, "
                    "seuil de HS % par défaut (Or–Platine).")
MISSING_FEW_MATCHES = ("Moins de 5 parties classées récentes : HS %, K/D sur la durée, winrate, "
                       "nombre de parties jouées et pool d'agents non évalués.")
MISSING_HEADSHOT = "Trop peu de tirs enregistrés pour juger le HS % (150 au minimum)."
MISSING_LOBBY = "Comparaison au lobby impossible : détail des parties indisponible."
MISSING_KD_WINRATE = "Moins de 10 parties classées récentes : K/D sur la durée et winrate non évalués."
MISSING_HISTORY = "Historique de RR indisponible."
MISSING_RANKED_GAMES = "Nombre total de parties classées inconnu."
MISSING_AGENT_POOL = "Moins de 8 parties classées récentes : pool d'agents non évalué."
MISSING_SEASONS = "Historique des saisons indisponible."
NOTE_CAPPED = "Moins de 5 parties classées récentes : indice plafonné à 60."
REF_SOURCE_NOTES = {   # d'où vient le rang de référence quand ce n'est pas le rang actuel
    "lobby": "d'après ses lobbys",
    "matches": "d'après sa dernière partie classée",
    "last_act": "d'après son dernier acte terminé",
}


def level_for(index):
    try:
        index = float(index)
    except (TypeError, ValueError):
        index = 0
    for row in LEVELS:
        if index <= row[0]:
            return row
    return LEVELS[-1]


@dataclass
class Signal:
    key: str
    label: str
    points: int
    max_points: int


@dataclass
class Suspicion:
    index: int
    level: str
    signals: list[Signal]   # critères déclenchés uniquement, du plus lourd au plus léger
    missing: list[str]
    notes: list[str]
    capped: bool


def clamp(x):
    return max(0.0, min(1.0, x))


def graded(v, threshold, full):
    # 0 jusqu'au seuil, puis de 0,6 (juste au-dessus) à 1 (au niveau "full" ou plus)
    if v is None or v <= threshold:
        return 0.0
    return 0.6 + 0.4 * clamp((v - threshold) / (full - threshold))


def _points(key, strength):
    return int(round(MAX_POINTS[key] * strength))


def _sorted(signals):
    return sorted(signals, key=lambda s: (-s.points, _ORDER.get(s.key, 99)))


# ================= CRITÈRES (force 0-1 + libellé) =================
def _level_rank(level, ref, rapid):
    # 1. Niveau de compte bas avec un rang élevé ou une montée rapide
    if level is None or level >= 50 or not ref or not (ref >= 12 or rapid):
        return 0.0, None
    rank_factor = 1.0 if ref >= 18 else (0.85 if ref >= 15 else 0.6)
    if rapid:
        rank_factor = max(rank_factor, 0.85)
    strength = (0.5 + 0.5 * clamp((45 - level) / 20)) * rank_factor
    label = f"Niveau {level} pour un rang {tier_name(ref)}" + (", avec une montée rapide" if rapid else "")
    return strength, label


def _headshot(hs, ref, from_peak=False):
    # 2. HS % anormalement haut pour le rang (celui de son pic s'il revient probablement d'une pause)
    threshold, group = hs_threshold(ref)
    strength = graded(hs, threshold, threshold + 10)
    where = f"{group}, rang de son pic" if from_peak else group
    return strength, f"HS % de {fmt.pct(hs)} (seuil {fmt.num(threshold)} % en {where})"


def _lobby_perf(acs_ratio, adr_ratio, where):
    # 3. ACS / ADR très au-dessus de la moyenne du lobby
    candidates = [(r, kind) for r, kind in ((acs_ratio, "ACS"), (adr_ratio, "ADR")) if r is not None]
    if not candidates:
        return 0.0, None
    ratio, kind = candidates[0]
    for r, k in candidates[1:]:
        if r > ratio:
            ratio, kind = r, k
    return graded(ratio, 1.5, 2.0), f"{kind} {fmt.ratio(ratio)} la moyenne {where}"


def _kd(agg):
    # 4. K/D élevé et régulier
    consistent = agg.kd_consistency is not None and agg.kd_consistency >= 0.7
    strength = graded(agg.kd, 1.8, 2.6) if consistent else 0.0
    share = fmt.num((agg.kd_consistency or 0) * 100)
    return strength, f"K/D de {fmt.num(agg.kd, 2)} sur {agg.n} parties, régulier ({share} % des parties à 1,2 ou plus)"


def _winrate(agg):
    # 5. Winrate élevé
    strength = graded(agg.winrate, 70, 90)
    return strength, f"Winrate de {fmt.num(agg.winrate)} % ({agg.wins}V-{agg.losses}D) sur {agg.n} parties"


def _rr_gains(history):
    # 6. Gains de RR élevés / sauts de rang rapides
    entries = [e for e in history if not e.placement and not e.refunded and not e.derank_protected]
    gains = [e.last_change for e in entries if e.last_change > 0]
    losses = [-e.last_change for e in entries if e.last_change < 0]
    mean_gain = statistics.fmean(gains) if gains else None
    mean_loss = statistics.fmean(losses) if losses else None
    parts = []  # (force, libellé) par ordre de priorité en cas d'égalité

    jump = None
    for e in entries:
        if e.tier_before is not None and e.tier_before > 0 and e.tier - e.tier_before >= 2:
            if jump is None or e.tier - e.tier_before > jump.tier - jump.tier_before:
                jump = e
    if jump is not None:
        parts.append((1.0, f"Saut de rang : {tier_name(jump.tier_before)} → {tier_name(jump.tier)} en une partie"))

    ranked = [e for e in reversed(entries) if e.tier > 0]   # de la plus ancienne à la plus récente
    if len(ranked) >= 5:
        climb = ranked[-1].tier - ranked[0].tier
        parts.append((graded(climb, 4, 8),
                      f"Montée de {climb} divisions en {len(ranked)} parties "
                      f"({tier_name(ranked[0].tier)} → {tier_name(ranked[-1].tier)})"))

    if len(gains) >= 4:
        loss_text = f" ({fmt.MINUS}{fmt.num(mean_loss)} par défaite)" if mean_loss is not None else ""
        parts.append((graded(mean_gain, 25, 32),
                      f"Gains de RR élevés : +{fmt.num(mean_gain)} en moyenne par victoire{loss_text}"))
        if len(losses) >= 3 and mean_gain >= 22 and mean_loss <= 14:
            parts.append((0.6, f"RR très favorable : +{fmt.num(mean_gain)} par victoire "
                               f"contre {fmt.MINUS}{fmt.num(mean_loss)} par défaite"))

    best = (0.0, None)
    for strength, label in parts:
        if strength > best[0]:
            best = (strength, label)
    return best


def _few_games(agg, ranked_games, lobby):
    # 7. Peu de parties jouées mais performance de haut niveau
    high = agg.kd is not None and agg.kd >= 1.4 and (
        (agg.acs is not None and agg.acs >= 250)
        or (lobby is not None and lobby.acs_ratio is not None and lobby.acs_ratio >= 1.3))
    if ranked_games >= 100 or not high:
        return 0.0, None
    strength = 0.6 + 0.4 * clamp((100 - ranked_games) / 70)
    return strength, (f"Seulement {fmt.num(ranked_games)} parties classées au total, "
                      f"avec un K/D de {fmt.num(agg.kd, 2)} et un ACS de {fmt.num(agg.acs)}")


def _agent_pool(matches, ranked_games, level):
    # 8. Pool d'agents maîtrisé dès les premières parties
    early = (ranked_games is not None and ranked_games <= 150) or (level is not None and level < 60)
    if not early:
        return 0.0, None
    by_agent = defaultdict(list)
    for m in matches:
        if m.agent and m.agent != UNKNOWN_AGENT:
            by_agent[m.agent].append(m)
    mastered = []
    for agent, games in by_agent.items():
        if len(games) < 2:
            continue
        kd = sum(m.kills for m in games) / max(1, sum(m.deaths for m in games))
        acs = sum(m.score for m in games) / max(1, sum(m.rounds for m in games))
        if kd >= 1.3 and acs >= 230:
            mastered.append((len(games), agent))
    old = matches[len(matches) // 2:]   # liste de la plus récente à la plus ancienne : 2e moitié = les plus anciennes
    kd_old = sum(m.kills for m in old) / max(1, sum(m.deaths for m in old))
    if len(mastered) < 3 or kd_old < 1.3:
        return 0.0, None
    strength = {3: 0.6, 4: 0.8}.get(len(mastered), 1.0)
    names = ", ".join(agent for _, agent in sorted(mastered, key=lambda a: (-a[0], a[1])))
    return strength, f"{len(mastered)} agents maîtrisés dès les premières parties ({names})"


def _placement(seasons):
    # 9. Premier acte classé terminé très haut
    ranked = [s for s in seasons if s.games > 0]
    first = next((s for s in ranked if s.end_tier > 0), None)
    if first is None or len(ranked) > 6 or first.games > 40:
        return 0.0, None
    strength = graded(first.end_tier, 11, 18)
    season = f" ({first.short})" if first.short else ""
    return strength, f"Premier acte classé terminé {tier_name(first.end_tier)} en {first.games} parties{season}"


# ================= INDICE =================
def compute_suspicion(account, mmr, matches, history, lobby, ref_tier, ref_source):
    matches = [m for m in (matches or []) if m is not None]
    n = len(matches)
    agg = aggregate(matches)
    level = account.level if account is not None else None
    ranked_games = mmr.ranked_games if mmr is not None else None
    signals, missing, notes = [], {}, []

    def add(key, strength, label):
        points = _points(key, strength)
        if points > 0 and label:
            signals.append(Signal(key, label, points, MAX_POINTS[key]))
            return True
        return False

    # 6 et 9 d'abord : ils disent si la montée est rapide (critère 1)
    rr_triggered = placement_triggered = False
    if history is None:
        missing[6] = MISSING_HISTORY
    else:
        rr_triggered = add("rr_gains", *_rr_gains(history))
    if mmr is None or not mmr.seasons:
        missing[9] = MISSING_SEASONS
    else:
        placement_triggered = add("placement", *_placement(mmr.seasons))
    rapid = rr_triggered or placement_triggered

    if not ref_tier:
        missing[0] = MISSING_REF_TIER
    if level is None:
        missing[1] = MISSING_LEVEL
    else:
        add("level_rank", *_level_rank(level, ref_tier, rapid))

    # Retour de pause probable : le HS % se juge au rang de son pic, pas à son rang du moment
    returning = bool(mmr is not None and mmr.peak_tier and ref_tier and mmr.peak_tier - ref_tier >= RETURNING_GAP)
    if 1 <= n < 5:
        missing[2] = MISSING_FEW_MATCHES   # couvre aussi les critères 4, 5, 7 et 8
    elif n >= 5 and agg.hits >= 150:
        add("headshot", *_headshot(agg.hs_pct, mmr.peak_tier if returning else ref_tier, from_peak=returning))
    elif n >= 5:
        missing[2] = MISSING_HEADSHOT

    if lobby is not None and lobby.n >= 3:
        add("lobby_perf", *_lobby_perf(lobby.acs_ratio, lobby.adr_ratio, f"du lobby ({lobby.n} parties)"))
    else:
        missing[3] = MISSING_LOBBY

    if n >= 10:
        add("kd", *_kd(agg))
        add("winrate", *_winrate(agg))
    elif n >= 5:
        missing[4] = MISSING_KD_WINRATE

    if ranked_games is None:
        if n >= 1:
            missing[7] = MISSING_RANKED_GAMES
    elif n >= 5:
        add("few_games", *_few_games(agg, ranked_games, lobby))

    if n >= 8:
        add("agent_pool", *_agent_pool(matches, ranked_games, level))
    elif n >= 5:
        missing[8] = MISSING_AGENT_POOL

    index = min(100, sum(s.points for s in signals))

    if mmr is not None and mmr.games_needed > 0:
        left = mmr.games_needed
        notes.append(f"En placements : encore {left} {'partie' if left < 2 else 'parties'} avant d'obtenir un rang.")
    if ref_tier and ref_source in REF_SOURCE_NOTES:
        notes.append(f"Rang de référence estimé {REF_SOURCE_NOTES[ref_source]} : {tier_name(ref_tier)}.")
    if returning:
        season = f" ({mmr.peak_season})" if mmr.peak_season else ""
        notes.append(f"Pic à {tier_name(mmr.peak_tier)}{season} : le joueur revient peut-être d'une pause ou a perdu des rangs.")
    if level is not None and level >= 200:
        notes.append(f"Compte ancien (niveau {level}) : des stats élevées peuvent simplement refléter un bon joueur.")
    capped = False
    if n < 5 and index > SMALL_SAMPLE_CAP:
        index, capped = SMALL_SAMPLE_CAP, True
        notes.append(NOTE_CAPPED)

    missing_lines = ([MISSING_NO_MATCHES] if n == 0 else []) + [missing[k] for k in sorted(missing)]
    return Suspicion(index=index, level=level_for(index)[1], signals=_sorted(signals),
                     missing=missing_lines, notes=notes, capped=capped)


def quick_match_index(player, match):
    # Estimation sur une seule partie v4 (critères 1, 2 et 3 seulement), plafonnée à 60
    others = [p for p in match.players if p.puuid != player.puuid]
    signals = []

    def add(key, strength, label):
        points = _points(key, strength)
        if points > 0 and label:
            signals.append(Signal(key, label, points, MAX_POINTS[key]))

    if player.level is not None:
        add("level_rank", *_level_rank(player.level, player.tier, False))
    if player.hits >= 30:
        add("headshot", *_headshot(player.hs_pct, player.tier))
    if others:
        acs_mean = statistics.fmean(p.acs for p in others)
        adr_mean = statistics.fmean(p.adr for p in others)
        add("lobby_perf", *_lobby_perf(player.acs / acs_mean if acs_mean > 0 else None,
                                       player.adr / adr_mean if adr_mean > 0 else None, "de la partie"))
    index = min(QUICK_CAP, sum(s.points for s in signals))
    return index, _sorted(signals)
