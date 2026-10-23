import unittest

from core.analysis import (
    Account, MmrInfo, SeasonInfo, MatchStat, RrChange, LobbySummary, LobbyPlayer, LobbyMatch,
    parse_match_v4, reference_tier,
)
from core import scoring
from core.scoring import (
    compute_suspicion, quick_match_index, level_for, graded, clamp, REPORT_THRESHOLD, LEVELS,
    MISSING_NO_MATCHES, MISSING_LEVEL, MISSING_HEADSHOT, MISSING_LOBBY, MISSING_KD_WINRATE,
    MISSING_HISTORY, MISSING_RANKED_GAMES, MISSING_SEASONS, NOTE_CAPPED,
    MISSING_FEW_MATCHES, MISSING_AGENT_POOL, MISSING_REF_TIER,
)
from tests import load_fixture, MATCH_FIXTURES

MINUS = "−"


# ---------- Constructeurs de données synthétiques ----------
def account(level=300):
    return Account("p", "Nom", "TAG", "eu", level, None)


def season(short="e1a1", games=200, end_tier=14, wins=None):
    return SeasonInfo(short, games, games // 2 if wins is None else wins, end_tier)


VETERAN_SEASONS = [season(f"e{i}a1", 200, 15) for i in range(1, 11)]   # 2 000 parties sur 10 actes


def mmr(tier=14, games_needed=0, seasons=None, peak=None, peak_season=None):
    seasons = VETERAN_SEASONS if seasons is None else seasons
    ranked = sum(s.games for s in seasons) if seasons else None
    return MmrInfo(tier, 50, 1200, 10, games_needed, peak, peak_season, ranked, seasons)


def M(kills=15, deaths=15, score=4000, rounds=20, head=20, body=60, leg=5, damage=2600, won=True, agent="Sova", tier=14):
    own = 13 if won else rounds - 13
    return MatchStat("m", 0, "Ascent", "e11a6", agent, tier, 100, kills, deaths, 3, score,
                     head, body, leg, damage, rounds, own, rounds - own)


def rr(change, tier=14, before=None, placement=False, refunded=False, protected=False):
    return RrChange("m", 0, "e11a6", tier, tier if before is None else before, change, refunded, protected, placement)


def lobby(n=3, acs=1.0, adr=1.0, median=14):
    return LobbySummary(n, acs, adr, median, [])


DEFAULT = object()


def compute(acc=DEFAULT, mmr_info=DEFAULT, matches=(), history=DEFAULT, lobby_summary=None, ref=14, source="current"):
    # Base neutre : compte ancien, 2 000 parties sur 10 actes, historique vide, pas de lobby
    return compute_suspicion(
        account() if acc is DEFAULT else acc,
        mmr() if mmr_info is DEFAULT else mmr_info,
        list(matches),
        [] if history is DEFAULT else history,
        lobby_summary, ref, source,
    )


def sig(suspicion, key):
    return next((s for s in suspicion.signals if s.key == key), None)


def points(suspicion, key):
    s = sig(suspicion, key)
    return s.points if s else 0


class HelpersTests(unittest.TestCase):
    def test_graded(self):
        self.assertEqual(graded(None, 1, 2), 0)
        self.assertEqual(graded(1.0, 1, 2), 0)
        self.assertAlmostEqual(graded(1.5, 1, 2), 0.8)
        self.assertEqual(graded(2.0, 1, 2), 1.0)
        self.assertEqual(graded(9.0, 1, 2), 1.0)
        self.assertEqual(clamp(-1), 0)
        self.assertEqual(clamp(3), 1)

    def test_levels(self):
        self.assertEqual(REPORT_THRESHOLD, 61)
        cases = {0: "faible", 30: "faible", 31: "modere", 60: "modere", 61: "eleve", 80: "eleve", 81: "tres_eleve", 100: "tres_eleve"}
        for index, key in cases.items():
            self.assertEqual(level_for(index)[1], key, index)
        self.assertEqual(level_for(45), LEVELS[1])
        self.assertEqual(level_for(45)[2:4], ("🟡", "Modéré"))


class LevelRankTests(unittest.TestCase):
    def test_threshold(self):
        self.assertIsNone(sig(compute(account(50), ref=14), "level_rank"))
        s = sig(compute(account(49), ref=14), "level_rank")
        self.assertEqual(s.points, 6)                      # (0,5 + 0) x 0,6
        self.assertEqual(s.label, "Niveau 49 pour un rang Or 3")
        self.assertEqual(s.max_points, 20)
        self.assertEqual(points(compute(account(25), ref=18), "level_rank"), 20)   # plein

    def test_rank_factor(self):
        self.assertEqual(points(compute(account(25), ref=14), "level_rank"), 12)
        self.assertEqual(points(compute(account(25), ref=15), "level_rank"), 17)
        self.assertEqual(points(compute(account(25), ref=18), "level_rank"), 20)

    def test_low_rank_needs_rapid_climb(self):
        self.assertIsNone(sig(compute(account(30), ref=11), "level_rank"))
        jump = [rr(25, tier=11, before=9)]
        s = sig(compute(account(30), history=jump, ref=11), "level_rank")
        self.assertEqual(s.points, 15)                     # 0,875 x 0,85
        self.assertTrue(s.label.endswith(", avec une montée rapide"))

    def test_missing(self):
        result = compute(account(None), ref=18)
        self.assertIsNone(sig(result, "level_rank"))
        self.assertIn(MISSING_LEVEL, result.missing)
        self.assertIsNone(sig(compute(account(10), ref=None, source="unknown"), "level_rank"))


class HeadshotTests(unittest.TestCase):
    def matches(self, heads):
        return [M(head=h, body=100 - h, leg=0) for h in heads]

    def test_threshold(self):
        self.assertIsNone(sig(compute(matches=self.matches([33, 33, 33, 33, 32])), "headshot"))   # 32,8 %
        s = sig(compute(matches=self.matches([33, 33, 33, 33, 34])), "headshot")                  # 33,2 %
        self.assertEqual(s.points, 11)
        self.assertEqual(s.label, "HS % de 33,2 % (seuil 33 % en Or–Platine)")
        self.assertEqual(points(compute(matches=self.matches([43] * 5)), "headshot"), 18)
        self.assertEqual(points(compute(matches=self.matches([60] * 5)), "headshot"), 18)

    def test_threshold_depends_on_rank(self):
        heads = [31] * 5
        self.assertIsNone(sig(compute(matches=self.matches(heads), ref=14), "headshot"))
        s = sig(compute(matches=self.matches(heads), ref=9), "headshot")
        self.assertEqual(s.label, "HS % de 31,0 % (seuil 30 % en Fer–Argent)")

    def test_not_enough_data(self):
        few_shots = compute(matches=[M(head=10, body=19, leg=0)] * 5)   # 145 tirs
        self.assertIsNone(sig(few_shots, "headshot"))
        self.assertIn(MISSING_HEADSHOT, few_shots.missing)
        four = compute(matches=self.matches([60] * 4))
        self.assertIsNone(sig(four, "headshot"))
        # Moins de 5 parties : la vraie raison est le nombre de parties, pas le nombre de tirs
        self.assertIn(MISSING_FEW_MATCHES, four.missing)
        self.assertNotIn(MISSING_HEADSHOT, four.missing)

    def test_returning_player_judged_at_peak(self):
        # R2-05 : retour de pause probable (pic ≥ 6 divisions au-dessus) : seuil de HS % du rang de son pic
        info = mmr(tier=10, peak=18, peak_season="e9a1")
        s = sig(compute(mmr_info=info, matches=self.matches([47] * 5), ref=10), "headshot")
        self.assertEqual(s.label, "HS % de 47,0 % (seuil 36 % en Diamant–Ascendant, rang de son pic)")
        self.assertIsNone(sig(compute(mmr_info=info, matches=self.matches([34] * 5), ref=10), "headshot"))
        # Pic proche du rang actuel : seuil du rang actuel, comme avant
        near = mmr(tier=10, peak=15)
        self.assertEqual(sig(compute(mmr_info=near, matches=self.matches([34] * 5), ref=10), "headshot").label,
                         "HS % de 34,0 % (seuil 30 % en Fer–Argent)")


class LobbyPerfTests(unittest.TestCase):
    def test_threshold(self):
        self.assertIsNone(sig(compute(lobby_summary=lobby(acs=1.49)), "lobby_perf"))
        s = sig(compute(lobby_summary=lobby(acs=1.51)), "lobby_perf")
        self.assertEqual(s.points, 12)
        self.assertEqual(s.label, "ACS 1,5× la moyenne du lobby (3 parties)")
        self.assertEqual(points(compute(lobby_summary=lobby(acs=2.0)), "lobby_perf"), 20)
        self.assertEqual(points(compute(lobby_summary=lobby(acs=2.7)), "lobby_perf"), 20)

    def test_adr_dominant(self):
        s = sig(compute(lobby_summary=lobby(n=5, acs=1.6, adr=1.8)), "lobby_perf")
        self.assertEqual(s.label, "ADR 1,8× la moyenne du lobby (5 parties)")
        s = sig(compute(lobby_summary=lobby(n=5, acs=None, adr=1.8)), "lobby_perf")
        self.assertTrue(s.label.startswith("ADR"))

    def test_missing(self):
        for summary in (None, lobby(n=2, acs=3.0)):
            result = compute(lobby_summary=summary)
            self.assertIsNone(sig(result, "lobby_perf"))
            self.assertIn(MISSING_LOBBY, result.missing)
        result = compute(lobby_summary=lobby(acs=None, adr=None))
        self.assertIsNone(sig(result, "lobby_perf"))
        self.assertNotIn(MISSING_LOBBY, result.missing)


class KdTests(unittest.TestCase):
    def matches(self, kills):
        return [M(kills=k, deaths=10, won=False) for k in kills]

    def test_threshold(self):
        self.assertIsNone(sig(compute(matches=self.matches([18] * 9 + [17])), "kd"))      # 1,79
        s = sig(compute(matches=self.matches([19] * 5 + [18] * 5)), "kd")                 # 1,85
        self.assertEqual(s.points, 10)
        self.assertEqual(s.label, "K/D de 1,85 sur 10 parties, régulier (100 % des parties à 1,2 ou plus)")
        self.assertEqual(points(compute(matches=self.matches([26] * 10)), "kd"), 16)

    def test_consistency(self):
        self.assertIsNone(sig(compute(matches=self.matches([40] * 6 + [5] * 4)), "kd"))   # K/D 2,6 mais 60 %
        s = sig(compute(matches=self.matches([34] * 7 + [7] * 3)), "kd")                  # 70 % pile
        self.assertEqual(s.points, 16)
        self.assertIn("(70 % des parties", s.label)

    def test_missing(self):
        result = compute(matches=self.matches([30] * 9))
        self.assertIsNone(sig(result, "kd"))
        self.assertIsNone(sig(result, "winrate"))
        self.assertEqual(result.missing.count(MISSING_KD_WINRATE), 1)


class WinrateTests(unittest.TestCase):
    def matches(self, wins, n):
        return [M(won=i < wins) for i in range(n)]

    def test_threshold(self):
        self.assertIsNone(sig(compute(matches=self.matches(7, 10)), "winrate"))   # 70 % pile
        self.assertEqual(points(compute(matches=self.matches(10, 14)), "winrate"), 9)   # 71,4 %
        s = sig(compute(matches=self.matches(8, 10)), "winrate")
        self.assertEqual(s.points, 11)
        self.assertEqual(s.label, "Winrate de 80 % (8V-2D) sur 10 parties")
        self.assertEqual(points(compute(matches=self.matches(9, 10)), "winrate"), 14)


class RrGainsTests(unittest.TestCase):
    def test_mean_gain(self):
        self.assertIsNone(sig(compute(history=[rr(25)] * 4), "rr_gains"))
        s = sig(compute(history=[rr(26)] * 4), "rr_gains")
        self.assertEqual(s.points, 12)
        self.assertEqual(s.label, "Gains de RR élevés : +26 en moyenne par victoire")
        s = sig(compute(history=[rr(32)] * 4 + [rr(-20)] * 2), "rr_gains")
        self.assertEqual(s.points, 18)
        self.assertEqual(s.label, f"Gains de RR élevés : +32 en moyenne par victoire ({MINUS}20 par défaite)")
        self.assertIsNone(sig(compute(history=[rr(40)] * 3), "rr_gains"))   # moins de 4 victoires

    def test_asymmetry(self):
        s = sig(compute(history=[rr(22)] * 4 + [rr(-14)] * 3), "rr_gains")
        self.assertEqual(s.points, 11)
        self.assertEqual(s.label, f"RR très favorable : +22 par victoire contre {MINUS}14 par défaite")
        self.assertIsNone(sig(compute(history=[rr(22)] * 4 + [rr(-15)] * 3), "rr_gains"))
        self.assertIsNone(sig(compute(history=[rr(22)] * 3 + [rr(21)] + [rr(-10)] * 3), "rr_gains"))
        self.assertIsNone(sig(compute(history=[rr(24)] * 4 + [rr(-10)] * 2), "rr_gains"))

    def test_jump(self):
        s = sig(compute(history=[rr(20, tier=14, before=12)]), "rr_gains")
        self.assertEqual(s.points, 18)
        self.assertEqual(s.label, "Saut de rang : Or 1 → Or 3 en une partie")
        self.assertIsNone(sig(compute(history=[rr(20, tier=14, before=13)]), "rr_gains"))
        self.assertIsNone(sig(compute(history=[rr(20, tier=14, before=0)]), "rr_gains"))

    def test_climb(self):
        def climb(newest, oldest):
            return [rr(10, tier=t, before=t - 1) for t in range(newest, oldest - 1, -1)]
        self.assertIsNone(sig(compute(history=climb(14, 10)), "rr_gains"))     # +4
        self.assertEqual(points(compute(history=climb(15, 10)), "rr_gains"), 13)   # +5
        s = sig(compute(history=climb(18, 10)), "rr_gains")
        self.assertEqual(s.points, 18)
        self.assertEqual(s.label, "Montée de 8 divisions en 9 parties (Argent 2 → Diamant 1)")
        self.assertIsNone(sig(compute(history=climb(18, 15)), "rr_gains"))     # 4 parties classées seulement

    def test_excluded_entries(self):
        for flags in ({"placement": True}, {"refunded": True}, {"protected": True}):
            history = [rr(60, **flags)] * 4 + [rr(20, tier=14, before=10, **flags)]
            self.assertIsNone(sig(compute(history=history), "rr_gains"), flags)

    def test_dominant_label(self):
        s = sig(compute(history=[rr(32, tier=14, before=12)] + [rr(32)] * 4), "rr_gains")
        self.assertTrue(s.label.startswith("Saut de rang"))

    def test_missing(self):
        result = compute(history=None)
        self.assertIn(MISSING_HISTORY, result.missing)
        self.assertNotIn(MISSING_HISTORY, compute(history=[]).missing)


class FewGamesTests(unittest.TestCase):
    def matches(self, n=5, kills=15, score=5200):
        return [M(kills=kills, deaths=10, score=score) for _ in range(n)]

    def mmr_games(self, games):
        return mmr(seasons=[season("e11a6", games, end_tier=0)])

    def test_threshold(self):
        self.assertIsNone(sig(compute(mmr_info=self.mmr_games(100), matches=self.matches()), "few_games"))
        self.assertEqual(points(compute(mmr_info=self.mmr_games(99), matches=self.matches()), "few_games"), 8)
        s = sig(compute(mmr_info=self.mmr_games(30), matches=self.matches()), "few_games")
        self.assertEqual(s.points, 14)
        self.assertEqual(s.label, "Seulement 30 parties classées au total, avec un K/D de 1,50 et un ACS de 260")

    def test_performance_condition(self):
        low_kd = [M(kills=14, deaths=10, score=5200)] * 4 + [M(kills=13, deaths=10, score=5200)]   # 1,38
        self.assertIsNone(sig(compute(mmr_info=self.mmr_games(30), matches=low_kd), "few_games"))
        low_acs = self.matches(score=4800)   # ACS 240
        self.assertIsNone(sig(compute(mmr_info=self.mmr_games(30), matches=low_acs, lobby_summary=lobby(1, acs=1.29)), "few_games"))
        self.assertIsNotNone(sig(compute(mmr_info=self.mmr_games(30), matches=low_acs, lobby_summary=lobby(1, acs=1.3)), "few_games"))

    def test_needs_five_matches(self):
        self.assertIsNone(sig(compute(mmr_info=self.mmr_games(30), matches=self.matches(4)), "few_games"))

    def test_missing(self):
        self.assertIn(MISSING_RANKED_GAMES, compute(mmr_info=None, matches=self.matches(2)).missing)
        self.assertIn(MISSING_RANKED_GAMES, compute(mmr_info=mmr(seasons=[]), matches=self.matches(2)).missing)
        self.assertNotIn(MISSING_RANKED_GAMES, compute(mmr_info=None).missing)   # n == 0 : ligne générale


class AgentPoolTests(unittest.TestCase):
    def matches(self, agents, kills=20, score=5000):
        return [M(kills=kills, deaths=10, score=score, agent=a, won=False) for a in agents]

    def mmr_games(self, games):
        return mmr(seasons=[season("e11a6", games, end_tier=0)])

    def test_levels(self):
        early = self.mmr_games(100)
        s = sig(compute(mmr_info=early, matches=self.matches(["Jett", "Jett", "Raze", "Raze", "Sova", "Sova", "Omen", "Viper"])), "agent_pool")
        self.assertEqual(s.points, 6)
        self.assertEqual(s.label, "3 agents maîtrisés dès les premières parties (Jett, Raze, Sova)")
        four = self.matches(["Jett", "Jett", "Raze", "Raze", "Sova", "Sova", "Omen", "Omen"])
        self.assertEqual(points(compute(mmr_info=early, matches=four), "agent_pool"), 8)
        five = self.matches(["Jett", "Jett", "Raze", "Raze", "Sova", "Sova", "Omen", "Omen", "Viper", "Viper"])
        self.assertEqual(points(compute(mmr_info=early, matches=five), "agent_pool"), 10)

    def test_early_condition(self):
        agents = ["Jett", "Jett", "Raze", "Raze", "Sova", "Sova", "Omen", "Viper"]
        self.assertIsNone(sig(compute(matches=self.matches(agents)), "agent_pool"))   # 2 000 parties, niveau 300
        self.assertIsNotNone(sig(compute(account(59), matches=self.matches(agents)), "agent_pool"))

    def test_learning_curve(self):
        # Les parties les plus anciennes (fin de liste) sont faibles : il y a eu une courbe d'apprentissage
        agents = ["Jett", "Raze", "Sova", "Omen"] * 2
        matches = [M(kills=40 if i < 4 else 10, deaths=10, score=5000, agent=a) for i, a in enumerate(agents)]
        self.assertIsNone(sig(compute(mmr_info=self.mmr_games(100), matches=matches), "agent_pool"))

    def test_needs_three_mastered(self):
        agents = ["Jett", "Jett", "Raze", "Raze", "Sova", "Sova", "Omen", "Omen"]
        matches = [M(kills=20, deaths=10, score=5000 if a in ("Jett", "Raze") else 4000, agent=a) for a in agents]
        self.assertIsNone(sig(compute(mmr_info=self.mmr_games(100), matches=matches), "agent_pool"))

    def test_needs_eight_matches(self):
        agents = ["Jett", "Jett", "Raze", "Raze", "Sova", "Sova", "Omen"]
        result = compute(mmr_info=self.mmr_games(100), matches=self.matches(agents))
        self.assertIsNone(sig(result, "agent_pool"))
        self.assertIn(MISSING_AGENT_POOL, result.missing)   # R2-07 : critère non évalué, et c'est dit
        agents.append("Viper")
        self.assertNotIn(MISSING_AGENT_POOL, compute(mmr_info=self.mmr_games(100), matches=self.matches(agents)).missing)


class PlacementTests(unittest.TestCase):
    def test_threshold(self):
        s = sig(compute(mmr_info=mmr(seasons=[season("e5a1", 12, 12), season("e5a2", 30, 14)])), "placement")
        self.assertEqual(s.points, 9)
        self.assertEqual(s.label, "Premier acte classé terminé Or 1 en 12 parties (e5a1)")
        self.assertIsNone(sig(compute(mmr_info=mmr(seasons=[season("e5a1", 12, 11)])), "placement"))
        self.assertEqual(points(compute(mmr_info=mmr(seasons=[season("e5a1", 12, 18)])), "placement"), 14)

    def test_conditions(self):
        self.assertIsNone(sig(compute(mmr_info=mmr(seasons=[season("e5a1", 41, 18)])), "placement"))
        self.assertIsNotNone(sig(compute(mmr_info=mmr(seasons=[season("e5a1", 40, 18)])), "placement"))
        six = [season("e5a1", 20, 18)] + [season(f"e6a{i}", 50, 18) for i in range(5)]
        self.assertIsNotNone(sig(compute(mmr_info=mmr(seasons=six)), "placement"))
        seven = six + [season("e7a1", 50, 18)]
        self.assertIsNone(sig(compute(mmr_info=mmr(seasons=seven)), "placement"))

    def test_skips_unplayed_and_unranked_acts(self):
        seasons = [season("e1a1", 0, 0), season("e2a1", 5, 0), season("e3a1", 12, 12)]
        s = sig(compute(mmr_info=mmr(seasons=seasons)), "placement")
        self.assertEqual(s.label, "Premier acte classé terminé Or 1 en 12 parties (e3a1)")

    def test_rapid_for_level_rank(self):
        result = compute(account(30), mmr_info=mmr(seasons=[season("e5a1", 12, 18)]), ref=11)
        self.assertTrue(sig(result, "level_rank").label.endswith(", avec une montée rapide"))

    def test_missing(self):
        self.assertIn(MISSING_SEASONS, compute(mmr_info=None).missing)
        self.assertIn(MISSING_SEASONS, compute(mmr_info=mmr(seasons=[])).missing)


class NotesAndCapTests(unittest.TestCase):
    def test_notes_order(self):
        info = mmr(tier=14, games_needed=3, peak=22, peak_season="e9a1")
        result = compute(account(250), mmr_info=info, ref=16, source="lobby")
        self.assertEqual(result.notes, [
            "En placements : encore 3 parties avant d'obtenir un rang.",
            "Rang de référence estimé d'après ses lobbys : Platine 2.",
            "Pic à Ascendant 2 (e9a1) : le joueur revient peut-être d'une pause ou a perdu des rangs.",
            "Compte ancien (niveau 250) : des stats élevées peuvent simplement refléter un bon joueur.",
        ])

    def test_reference_rank_is_always_explained(self):
        # R2-07 : rang de référence estimé ou inconnu, toujours dit dans le contexte
        self.assertIn("En placements : encore 1 partie avant d'obtenir un rang.",
                      compute(mmr_info=mmr(games_needed=1), ref=14).notes)
        self.assertIn("Rang de référence estimé d'après sa dernière partie classée : Or 3.",
                      compute(ref=14, source="matches").notes)
        self.assertIn("Rang de référence estimé d'après son dernier acte terminé : Or 3.",
                      compute(ref=14, source="last_act").notes)
        self.assertEqual(compute(account(100), ref=14, source="current").notes, [])
        unknown = compute(account(30), ref=None, source="unknown")
        self.assertIn(MISSING_REF_TIER, unknown.missing)
        self.assertNotIn(MISSING_REF_TIER, compute(ref=14).missing)

    def test_small_samples_say_what_is_not_evaluated(self):
        # R2-07 : 1 à 4 parties → une ligne pour les critères 2, 4, 5, 7, 8 ; 5 à 9 parties → 4/5 (et 8 sous 8 parties)
        two = compute(matches=[M()] * 2)
        self.assertIn(MISSING_FEW_MATCHES, two.missing)
        self.assertNotIn(MISSING_KD_WINRATE, two.missing)
        six = compute(matches=[M()] * 6)
        self.assertNotIn(MISSING_FEW_MATCHES, six.missing)
        self.assertIn(MISSING_KD_WINRATE, six.missing)
        self.assertIn(MISSING_AGENT_POOL, six.missing)
        nine = compute(matches=[M()] * 9)
        self.assertIn(MISSING_KD_WINRATE, nine.missing)
        self.assertNotIn(MISSING_AGENT_POOL, nine.missing)

    def test_note_thresholds(self):
        self.assertEqual(compute(account(199), mmr_info=mmr(peak=19), ref=14).notes, [])
        self.assertEqual(len(compute(account(199), mmr_info=mmr(peak=20), ref=14).notes), 1)
        self.assertEqual(len(compute(account(200), mmr_info=mmr(peak=19), ref=14).notes), 1)

    def test_no_matches(self):
        result = compute(mmr_info=None, matches=[])
        self.assertEqual(result.missing[0], MISSING_NO_MATCHES)
        for line in (MISSING_HEADSHOT, MISSING_KD_WINRATE, MISSING_RANKED_GAMES):
            self.assertNotIn(line, result.missing)
        self.assertIn(MISSING_LOBBY, result.missing)
        self.assertIn(MISSING_SEASONS, result.missing)
        self.assertEqual(result.index, 0)
        self.assertEqual(result.level, "faible")

    def strong_inputs(self, n):
        matches = [M(kills=20, deaths=10, score=5000)] * n
        info = mmr(tier=18, seasons=[season("e5a1", 12, 18), season("e5a2", 20, 18)])
        return dict(acc=account(20), mmr_info=info, matches=matches, history=[rr(20, tier=18, before=16)],
                    lobby_summary=lobby(3, acs=2.0), ref=18)

    def test_cap_under_five_matches(self):
        result = compute(**self.strong_inputs(4))
        self.assertEqual(result.index, 60)
        self.assertTrue(result.capped)
        self.assertEqual(result.notes[-1], NOTE_CAPPED)
        self.assertEqual(sum(s.points for s in result.signals), 72)
        self.assertEqual(result.level, "modere")
        zero = compute(**dict(self.strong_inputs(0), matches=[]))
        self.assertTrue(zero.capped)
        self.assertEqual(zero.index, 60)

    def test_no_cap_from_five_matches(self):
        result = compute(**self.strong_inputs(5))
        self.assertFalse(result.capped)
        self.assertGreater(result.index, 60)
        self.assertNotIn(NOTE_CAPPED, result.notes)

    def test_no_cap_when_low(self):
        result = compute(account(49), matches=[M()] * 2)
        self.assertFalse(result.capped)
        self.assertEqual(result.index, 6)

    def test_signals_sorted(self):
        result = compute(**self.strong_inputs(5))
        pts = [s.points for s in result.signals]
        self.assertEqual(pts, sorted(pts, reverse=True))
        # À égalité (20 points), l'ordre des critères : niveau (1) avant lobby (3)
        self.assertEqual([s.key for s in result.signals[:2]], ["level_rank", "lobby_perf"])
        self.assertEqual(result.index, min(100, sum(pts)))


class CalibrationTests(unittest.TestCase):
    def test_obvious_smurf(self):
        matches = [M(kills=20, deaths=10, score=5200, damage=3200, head=22, body=70, leg=8, won=i % 4 != 3, agent="Jett")
                   for i in range(16)]
        info = mmr(tier=14, peak=14, peak_season="e11a6", seasons=[season("e10a1", 12, 12), season("e11a6", 33, 14)])
        history = [rr(28)] * 8 + [rr(-15)] * 4
        summary = lobby(5, acs=1.6, adr=1.4, median=14)
        ref, source = reference_tier(info, summary, matches)
        self.assertEqual((ref, source), (14, "current"))
        result = compute_suspicion(account(30), info, matches, history, summary, ref, source)
        self.assertGreaterEqual(result.index, 61, result)
        self.assertIn(result.level, ("eleve", "tres_eleve"))
        for key in ("level_rank", "lobby_perf", "kd", "winrate", "rr_gains", "few_games", "placement"):
            self.assertIsNotNone(sig(result, key), key)
        self.assertTrue(sig(result, "level_rank").label.endswith(", avec une montée rapide"))

    def test_veteran_good_player(self):
        matches = [M(kills=15, deaths=10, score=4600, damage=3000, head=20, body=70, leg=10, won=i < 12) for i in range(20)]
        info = mmr(tier=16, peak=17, peak_season="e10a1")
        history = [rr(21, tier=16)] * 12 + [rr(-19, tier=16)] * 8
        summary = lobby(5, acs=1.2, adr=1.1, median=16)
        result = compute_suspicion(account(300), info, matches, history, summary, 16, "current")
        self.assertLessEqual(result.index, 30, result)
        self.assertEqual(result.level, "faible")
        self.assertIn("Compte ancien (niveau 300) : des stats élevées peuvent simplement refléter un bon joueur.", result.notes)

    def test_returning_player(self):
        matches = [M(kills=20, deaths=10, score=5000, damage=3100, head=22, body=70, leg=8, won=i < 15, tier=10) for i in range(20)]
        seasons = [season("e7a1", 100, 16), season("e7a2", 100, 18), season("e8a1", 100, 17),
                   season("e9a1", 100, 18), season("e10a1", 100, 12), season("e11a6", 100, 10)]
        info = mmr(tier=10, peak=18, peak_season="e9a1", seasons=seasons)
        self.assertEqual(info.ranked_games, 600)
        history = [rr(27, tier=10)] * 12 + [rr(-16, tier=10)] * 5
        summary = lobby(5, acs=1.6, adr=1.5, median=11)
        result = compute_suspicion(account(150), info, matches, history, summary, 10, "current")
        self.assertLessEqual(result.index, 60, result)
        self.assertLess(result.index, REPORT_THRESHOLD)
        self.assertIn("Pic à Diamant 1 (e9a1) : le joueur revient peut-être d'une pause ou a perdu des rangs.", result.notes)

    def test_returning_player_with_good_aim_stays_below_report_threshold(self):
        # R2-05 : même profil de retour de pause avec 34 % de HS (au-dessus du seuil Fer–Argent) : pas reportable
        matches = [M(kills=20, deaths=10, score=5000, damage=3100, head=34, body=58, leg=8, won=i < 15, tier=10)
                   for i in range(20)]
        seasons = [season("e7a1", 100, 16), season("e7a2", 100, 18), season("e8a1", 100, 17),
                   season("e9a1", 100, 18), season("e10a1", 100, 12), season("e11a6", 100, 10)]
        info = mmr(tier=10, peak=18, peak_season="e9a1", seasons=seasons)
        history = [rr(27, tier=10)] * 12 + [rr(-16, tier=10)] * 5
        summary = lobby(5, acs=1.6, adr=1.5, median=11)
        result = compute_suspicion(account(150), info, matches, history, summary, 10, "current")
        self.assertLessEqual(result.index, 60, result)
        self.assertIsNone(sig(result, "headshot"))


class QuickMatchIndexTests(unittest.TestCase):
    def player(self, puuid, level=100, tier=14, score=4000, damage=2600, head=20, body=60, leg=5):
        return LobbyPlayer(puuid, "red", None, "Jett", tier, level, puuid, "T", 15, 15, 3, score, head, body, leg, damage, 20)

    def test_fixture_matches(self):
        for name in MATCH_FIXTURES:
            match = parse_match_v4(load_fixture(name))
            for p in match.players:
                index, signals = quick_match_index(p, match)
                self.assertGreaterEqual(index, 0)
                self.assertLessEqual(index, 60)
                self.assertEqual(index, min(60, sum(s.points for s in signals)))
                for s in signals:
                    self.assertIn(s.key, ("level_rank", "headshot", "lobby_perf"))
                    self.assertGreater(s.points, 0)
                    if s.key == "lobby_perf":
                        self.assertIn("la moyenne de la partie", s.label)

    def test_synthetic(self):
        me = self.player("me", level=20, tier=18, score=8000, damage=5200, head=25, body=25, leg=0)
        others = [self.player(f"o{i}") for i in range(9)]
        match = LobbyMatch("x", "Ascent", 0, 20, [me] + others, {"red": 13, "blue": 7})
        index, signals = quick_match_index(me, match)
        self.assertEqual([s.key for s in signals], ["level_rank", "lobby_perf", "headshot"])
        self.assertEqual(index, 58)
        self.assertEqual(signals[1].label, "ACS 2,0× la moyenne de la partie")
        self.assertEqual(quick_match_index(others[0], match), (0, []))

    def test_missing_level_and_few_shots(self):
        me = self.player("me", level=None, tier=18, head=20, body=5, leg=0)   # 25 tirs
        others = [self.player(f"o{i}") for i in range(9)]
        match = LobbyMatch("x", "Ascent", 0, 20, [me] + others, {"red": 13, "blue": 7})
        self.assertEqual(quick_match_index(me, match), (0, []))


if __name__ == "__main__":
    unittest.main()
