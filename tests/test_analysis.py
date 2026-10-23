import copy
import statistics
import unittest
from datetime import datetime

from core.analysis import (
    Account, MmrInfo, SeasonInfo, MatchStat, LobbyPlayer, LobbyMatch, LobbySummary,
    parse_account, parse_mmr, parse_stored_matches, parse_mmr_history, parse_match_v4, parse_ts,
    aggregate, lobby_summary, reference_tier, rank_averages,
)
from tests import load_fixture, PLAYER_PUUID, MATCH_FIXTURES


def ts(iso):
    return int(datetime.fromisoformat(iso).timestamp())


def match_stat(match_id="m", kills=15, deaths=15, assists=5, score=4000, head=20, body=60, leg=5, damage=2600,
               rounds=20, won=True, agent="Sova", tier=14, level=100, started_at=0):
    own = 13 if won else rounds - 13
    return MatchStat(match_id, started_at, "Ascent", "e11a6", agent, tier, level, kills, deaths, assists, score,
                     head, body, leg, damage, rounds, own, rounds - own)


def lobby_player(puuid, tier=14, kills=15, deaths=15, score=4000, damage=2600, head=20, body=60, leg=5,
                 level=100, team="red", rounds=20, agent="Sova"):
    return LobbyPlayer(puuid, team, None, agent, tier, level, puuid, "T", kills, deaths, 3, score,
                       head, body, leg, damage, rounds)


def lobby_match(match_id, players, rounds=20):
    return LobbyMatch(match_id, "Ascent", 0, rounds, players, {"red": 13, "blue": rounds - 13})


class ParseAccountTests(unittest.TestCase):
    def test_fixture(self):
        raw = load_fixture("account")
        account = parse_account(raw)
        self.assertEqual(account.puuid, PLAYER_PUUID)
        self.assertEqual(account.puuid, raw["puuid"])
        self.assertEqual((account.name, account.tag), ("TestSmurf", "0001"))
        self.assertEqual(account.region, "eu")
        self.assertEqual(account.level, raw["account_level"])
        self.assertEqual(account.card, raw["card"]["small"])

    def test_missing_fields(self):
        account = parse_account({"puuid": "p", "card": None, "account_level": None, "region": None})
        self.assertEqual(account, Account("p", "", "", "eu", None, None))
        self.assertIsNone(parse_account({"puuid": "p", "card": "card-id"}).card)
        self.assertEqual(parse_account({"puuid": "p", "region": "NA"}).region, "na")

    def test_invalid(self):
        for data in (None, [], {}, {"puuid": None}, {"puuid": ""}):
            with self.assertRaises(ValueError):
                parse_account(data)


class ParseMmrTests(unittest.TestCase):
    def test_fixture(self):
        raw = load_fixture("mmr_v3")
        mmr = parse_mmr(raw)
        self.assertEqual(mmr.tier, raw["current"]["tier"]["id"])
        self.assertEqual(mmr.rr, raw["current"]["rr"])
        self.assertEqual(mmr.elo, raw["current"]["elo"])
        self.assertEqual(mmr.last_change, raw["current"]["last_change"])
        self.assertEqual(mmr.games_needed, raw["current"]["games_needed_for_rating"])
        self.assertEqual(mmr.peak_tier, raw["peak"]["tier"]["id"])
        self.assertEqual(mmr.peak_season, raw["peak"]["season"]["short"])
        self.assertEqual(len(mmr.seasons), len(raw["seasonal"]))
        self.assertEqual(mmr.ranked_games, sum(s["games"] for s in raw["seasonal"]))
        # De la plus ancienne à la plus récente, comme l'API
        self.assertEqual([s.short for s in mmr.seasons], [s["season"]["short"] for s in raw["seasonal"]])
        last = raw["seasonal"][-1]
        self.assertEqual(mmr.seasons[-1], SeasonInfo(last["season"]["short"], last["games"], last["wins"], last["end_tier"]["id"]))

    def test_null_objects(self):
        mmr = parse_mmr({"current": None, "peak": None, "seasonal": None})
        self.assertEqual(mmr, MmrInfo(0, None, None, None, 0, None, None, None, []))
        mmr = parse_mmr({"current": {"tier": None, "games_needed_for_rating": 3},
                         "peak": {"tier": {"id": 0, "name": "Unrated"}, "season": None},
                         "seasonal": [None, {"season": None, "games": None, "wins": 2, "end_tier": None},
                                      {"season": {"short": "e9a1"}, "games": 12, "wins": 7, "end_tier": {"id": 12}}]})
        self.assertEqual(mmr.tier, 0)
        self.assertEqual(mmr.games_needed, 3)
        self.assertIsNone(mmr.peak_tier)
        self.assertEqual(mmr.seasons, [SeasonInfo("", 0, 2, 0), SeasonInfo("e9a1", 12, 7, 12)])
        self.assertEqual(mmr.ranked_games, 12)

    def test_invalid(self):
        for data in (None, [], "x"):
            with self.assertRaises(ValueError):
                parse_mmr(data)


class ParseStoredMatchesTests(unittest.TestCase):
    def test_fixture(self):
        raw = load_fixture("stored_matches")
        matches = parse_stored_matches(raw)
        self.assertEqual(len(matches), len(raw))
        self.assertEqual([m.match_id for m in matches], [e["meta"]["id"] for e in raw])
        for m, e in zip(matches, raw):
            stats, teams = e["stats"], e["teams"]
            self.assertEqual(m.started_at, ts(e["meta"]["started_at"]))
            self.assertEqual(m.map, e["meta"]["map"]["name"])
            self.assertEqual(m.season, e["meta"]["season"]["short"])
            self.assertEqual(m.agent, stats["character"]["name"])
            self.assertEqual(m.tier, stats["tier"])
            self.assertEqual(m.level, stats["level"])
            self.assertEqual((m.kills, m.deaths, m.assists, m.score), (stats["kills"], stats["deaths"], stats["assists"], stats["score"]))
            self.assertEqual((m.headshots, m.bodyshots, m.legshots), (stats["shots"]["head"], stats["shots"]["body"], stats["shots"]["leg"]))
            self.assertEqual(m.damage, stats["damage"]["made"])
            self.assertEqual(m.rounds, teams["red"] + teams["blue"])
            own = teams[stats["team"].lower()]
            self.assertEqual(m.team_rounds, own)
            self.assertEqual(m.enemy_rounds, m.rounds - own)
        # Plus récente d'abord
        self.assertEqual([m.started_at for m in matches], sorted((m.started_at for m in matches), reverse=True))
        first = matches[0]
        self.assertEqual(first.result, "win")      # Blue 13 - 7
        self.assertAlmostEqual(first.acs, 4469 / 20)
        self.assertAlmostEqual(first.adr, 3291 / 20)
        self.assertEqual(first.hits, 9 + 47 + 5)
        self.assertAlmostEqual(first.hs_pct, 9 / 61 * 100)
        self.assertAlmostEqual(first.kd, 14 / 16)

    def test_skips_malformed_and_unranked(self):
        raw = load_fixture("stored_matches")
        good = raw[0]
        upper = copy.deepcopy(good)
        upper["meta"]["mode"] = "COMPETITIVE"
        upper["meta"]["id"] = "upper"
        dm = copy.deepcopy(good)
        dm["meta"]["mode"] = "Deathmatch"
        no_teams = copy.deepcopy(good)
        no_teams["teams"] = None
        no_stats = copy.deepcopy(good)
        no_stats["stats"] = None
        no_id = copy.deepcopy(good)
        no_id["meta"]["id"] = None
        no_meta = copy.deepcopy(good)
        no_meta["meta"] = None
        bad_team = copy.deepcopy(good)
        bad_team["stats"]["team"] = "Neutral"
        data = [None, "x", dm, no_teams, no_stats, no_id, no_meta, bad_team, upper, good]
        self.assertEqual([m.match_id for m in parse_stored_matches(data)], ["upper", good["meta"]["id"]])
        self.assertEqual(parse_stored_matches(None), [])
        self.assertEqual(parse_stored_matches({"data": []}), [])

    def test_null_stats_fields(self):
        entry = {"meta": {"id": "m1", "mode": "competitive", "map": None, "season": None, "started_at": None},
                 "stats": {"team": "red", "character": None, "tier": None, "level": None, "shots": None,
                           "damage": None, "kills": None, "deaths": 0, "score": 300},
                 "teams": {"red": 0, "blue": 0}}
        m = parse_stored_matches([entry])[0]
        self.assertEqual((m.map, m.agent, m.tier, m.level, m.started_at), ("Inconnue", "Inconnu", 0, None, 0))
        self.assertEqual(m.rounds, 1)   # minimum 1
        self.assertEqual(m.result, "draw")
        self.assertEqual(m.hits, 0)
        self.assertIsNone(m.hs_pct)
        self.assertEqual(m.kd, 0)
        self.assertEqual(m.acs, 300)


class ParseHistoryTests(unittest.TestCase):
    def test_fixture(self):
        raw = load_fixture("mmr_history")
        history = parse_mmr_history(raw)
        self.assertEqual(len(history), len(raw["history"]))
        for change, e in zip(history, raw["history"]):
            self.assertEqual(change.match_id, e["match_id"])
            self.assertEqual(change.date, ts(e["date"]))
            self.assertEqual(change.season, e["season"]["short"])
            self.assertEqual(change.tier, e["tier"]["id"])
            self.assertEqual(change.tier_before, e["tier_before_update"]["id"])
            self.assertEqual(change.last_change, e["last_change"])
            self.assertEqual(change.refunded, e["refunded_rr"] > 0)
            self.assertEqual(change.derank_protected, e["was_derank_protected"])
            self.assertEqual(change.placement, e["is_placement_match"])
        self.assertEqual([c.date for c in history], sorted((c.date for c in history), reverse=True))
        # Les parties de l'historique sont celles des parties stockées (même anonymisation)
        stored_ids = [e["meta"]["id"] for e in load_fixture("stored_matches")]
        self.assertEqual([c.match_id for c in history], stored_ids)

    def test_flags_and_nulls(self):
        history = parse_mmr_history({"history": [
            None,
            {"match_id": "a", "tier": {"id": 14}, "tier_before_update": None, "last_change": 30,
             "refunded_rr": 12, "was_derank_protected": True, "is_placement_match": True, "date": None, "season": None},
        ]})
        self.assertEqual(len(history), 1)
        change = history[0]
        self.assertIsNone(change.tier_before)
        self.assertTrue(change.refunded and change.derank_protected and change.placement)
        self.assertEqual((change.date, change.season), (0, ""))
        self.assertEqual(parse_mmr_history({"history": []}), [])

    def test_invalid(self):
        for data in (None, {}, {"history": None}, "x"):
            with self.assertRaises(ValueError):
                parse_mmr_history(data)


class ParseMatchV4Tests(unittest.TestCase):
    def test_fixtures(self):
        for name in MATCH_FIXTURES:
            raw = load_fixture(name)
            match = parse_match_v4(raw)
            self.assertIsNotNone(match, name)
            self.assertEqual(match.match_id, raw["metadata"]["match_id"])
            self.assertEqual(match.map, raw["metadata"]["map"]["name"])
            self.assertEqual(match.started_at, ts(raw["metadata"]["started_at"]))
            self.assertEqual(match.rounds, sum(t["rounds"]["won"] for t in raw["teams"]))
            self.assertEqual(len(match.players), len(raw["players"]))
            winner = next(t["team_id"].lower() for t in raw["teams"] if t["won"])
            self.assertEqual(match.winner, winner)
            self.assertEqual(match.teams_rounds, {t["team_id"].lower(): t["rounds"]["won"] for t in raw["teams"]})
            self.assertIsNotNone(match.player(PLAYER_PUUID), name)
            for p, e in zip(match.players, raw["players"]):
                s = e["stats"]
                self.assertEqual(p.puuid, e["puuid"])
                self.assertEqual((p.name, p.tag), (e["name"], e["tag"]))
                self.assertEqual(p.team, e["team_id"].lower())
                self.assertEqual(p.party_id, e["party_id"])
                self.assertEqual(p.agent, e["agent"]["name"])
                self.assertEqual(p.tier, e["tier"]["id"])
                self.assertEqual(p.level, e["account_level"])
                self.assertEqual((p.kills, p.deaths, p.assists, p.score), (s["kills"], s["deaths"], s["assists"], s["score"]))
                self.assertEqual((p.headshots, p.bodyshots, p.legshots), (s["headshots"], s["bodyshots"], s["legshots"]))
                self.assertEqual(p.damage, s["damage"]["dealt"])
                self.assertEqual(p.rounds, match.rounds)
                self.assertAlmostEqual(p.acs, s["score"] / match.rounds)

    def test_player_found_in_stored_matches(self):
        # Le joueur analysé des fixtures v4 a la même partie dans ses parties stockées
        stored = {m.match_id: m for m in parse_stored_matches(load_fixture("stored_matches"))}
        for name in MATCH_FIXTURES:
            match = parse_match_v4(load_fixture(name))
            me = match.player(PLAYER_PUUID)
            self.assertIn(match.match_id, stored)
            self.assertEqual(stored[match.match_id].kills, me.kills)
            self.assertEqual(stored[match.match_id].score, me.score)

    def test_malformed(self):
        raw = load_fixture("match_v4_single")
        self.assertIsNone(parse_match_v4(None))
        self.assertIsNone(parse_match_v4({}))
        no_id = copy.deepcopy(raw)
        no_id["metadata"]["match_id"] = None
        self.assertIsNone(parse_match_v4(no_id))
        no_meta = copy.deepcopy(raw)
        no_meta["metadata"] = None
        self.assertIsNone(parse_match_v4(no_meta))
        no_teams = copy.deepcopy(raw)
        no_teams["teams"] = None
        self.assertIsNone(parse_match_v4(no_teams))
        one_player = copy.deepcopy(raw)
        one_player["players"] = raw["players"][:1] + [None, {"puuid": None}]
        self.assertIsNone(parse_match_v4(one_player))

    def test_null_player_fields(self):
        raw = load_fixture("match_v4_single")
        data = copy.deepcopy(raw)
        data["players"][0].update({"stats": None, "agent": None, "tier": None, "account_level": None, "party_id": None})
        data["players"].append(None)
        match = parse_match_v4(data)
        p = match.players[0]
        self.assertEqual(len(match.players), 10)
        self.assertEqual((p.kills, p.score, p.damage, p.hits, p.tier, p.agent), (0, 0, 0, 0, 0, "Inconnu"))
        self.assertIsNone(p.level)
        self.assertIsNone(p.party_id)
        self.assertIsNone(p.hs_pct)

    def test_parse_ts(self):
        self.assertEqual(parse_ts("2026-10-22T16:29:50.88Z"), 1792686590)
        self.assertEqual(parse_ts("2026-10-22T18:06:34Z"), 1792692394)
        self.assertEqual(parse_ts("2026-10-22T18:06:34"), 1792692394)   # sans fuseau : UTC
        self.assertEqual(parse_ts(1792692394.5), 1792692394)
        for value in (None, "", "hier", True, -5, {}):
            self.assertIsNone(parse_ts(value))


class AggregateTests(unittest.TestCase):
    def test_empty(self):
        agg = aggregate([])
        self.assertEqual(agg.n, 0)
        self.assertEqual((agg.kills, agg.hits, agg.wins, agg.losses, agg.draws), (0, 0, 0, 0, 0))
        for value in (agg.kd, agg.acs, agg.adr, agg.hs_pct, agg.winrate, agg.kd_consistency):
            self.assertIsNone(value)

    def test_math(self):
        matches = [
            match_stat(kills=30, deaths=10, score=6000, damage=4000, rounds=20, head=40, body=50, leg=10, won=True),
            match_stat(kills=10, deaths=20, score=3000, damage=2000, rounds=24, head=10, body=80, leg=10, won=False),
            match_stat(kills=12, deaths=10, score=4400, damage=2500, rounds=22, head=0, body=0, leg=0, won=True),
            match_stat(kills=5, deaths=0, score=1000, damage=500, rounds=24, head=5, body=5, leg=0, won=True),
        ]
        matches[3].team_rounds = matches[3].enemy_rounds = 12   # égalité
        agg = aggregate(matches)
        self.assertEqual(agg.n, 4)
        self.assertEqual((agg.kills, agg.deaths), (57, 40))
        self.assertAlmostEqual(agg.kd, 57 / 40)
        self.assertAlmostEqual(agg.acs, 14400 / 90)
        self.assertAlmostEqual(agg.adr, 9000 / 90)
        self.assertEqual(agg.hits, 210)
        self.assertAlmostEqual(agg.hs_pct, 55 / 210 * 100)
        self.assertEqual((agg.wins, agg.losses, agg.draws), (2, 1, 1))
        self.assertAlmostEqual(agg.winrate, 50.0)
        # K/D par partie : 3,0 / 0,5 / 1,2 / 5 (0 mort) -> 3 sur 4 à 1,2 ou plus
        self.assertAlmostEqual(agg.kd_consistency, 0.75)

    def test_fixture(self):
        raw = load_fixture("stored_matches")
        agg = aggregate(parse_stored_matches(raw))
        kills = sum(e["stats"]["kills"] for e in raw)
        deaths = sum(e["stats"]["deaths"] for e in raw)
        rounds = sum(e["teams"]["red"] + e["teams"]["blue"] for e in raw)
        head = sum(e["stats"]["shots"]["head"] for e in raw)
        hits = sum(sum(e["stats"]["shots"].values()) for e in raw)
        wins = sum(1 for e in raw if e["teams"][e["stats"]["team"].lower()] > e["teams"]["red" if e["stats"]["team"] == "Blue" else "blue"])
        self.assertEqual(agg.n, 20)
        self.assertAlmostEqual(agg.kd, kills / deaths)
        self.assertAlmostEqual(agg.acs, sum(e["stats"]["score"] for e in raw) / rounds)
        self.assertAlmostEqual(agg.adr, sum(e["stats"]["damage"]["made"] for e in raw) / rounds)
        self.assertAlmostEqual(agg.hs_pct, head / hits * 100)
        self.assertEqual(agg.wins, wins)
        self.assertAlmostEqual(agg.winrate, wins / 20 * 100)


class LobbySummaryTests(unittest.TestCase):
    def test_ratios_and_median(self):
        me = "me"
        m1 = lobby_match("m1", [lobby_player(me, score=8000, damage=4000)] +
                         [lobby_player(f"o{i}", tier=12 + i % 3, score=4000, damage=2000) for i in range(9)])
        m2 = lobby_match("m2", [lobby_player(me, score=4000, damage=4000)] +
                         [lobby_player(f"p{i}", tier=0 if i < 2 else 15, score=4000, damage=2000) for i in range(9)])
        summary = lobby_summary([m1, m2], me)
        self.assertEqual(summary.n, 2)
        self.assertEqual(summary.per_match, [("m1", 2.0, 2.0), ("m2", 1.0, 2.0)])
        self.assertAlmostEqual(summary.acs_ratio, 1.5)
        self.assertAlmostEqual(summary.adr_ratio, 2.0)
        # Rangs des autres (> 0, les deux 0 sont ignorés) : 12/13/14 x3 + 15 x7 -> médiane 14
        self.assertEqual(summary.median_other_tier, 14)
        half = lobby_match("h", [lobby_player(me)] + [lobby_player(f"q{i}", tier=12 if i < 3 else 13) for i in range(6)])
        self.assertEqual(lobby_summary([half], me).median_other_tier, 13)   # médiane 12,5 arrondie au-dessus

    def test_not_enough_others(self):
        me = "me"
        small = lobby_match("s", [lobby_player(me)] + [lobby_player(f"o{i}") for i in range(4)])
        summary = lobby_summary([small], me)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.n, 0)
        self.assertIsNone(summary.acs_ratio)
        self.assertEqual(summary.per_match, [])

    def test_absent_player(self):
        match = lobby_match("x", [lobby_player(f"o{i}") for i in range(10)])
        self.assertIsNone(lobby_summary([match], "me"))
        self.assertIsNone(lobby_summary([], "me"))
        self.assertIsNone(lobby_summary([None], "me"))

    def test_fixture(self):
        lobbies = [parse_match_v4(load_fixture(name)) for name in MATCH_FIXTURES]
        summary = lobby_summary(lobbies, PLAYER_PUUID)
        self.assertEqual(summary.n, 5)
        self.assertEqual([r[0] for r in summary.per_match], [m.match_id for m in lobbies])
        m = lobbies[0]
        me = m.player(PLAYER_PUUID)
        others = [p for p in m.players if p.puuid != PLAYER_PUUID]
        self.assertAlmostEqual(summary.per_match[0][1], me.acs / statistics.fmean(p.acs for p in others))
        self.assertAlmostEqual(summary.per_match[0][2], me.adr / statistics.fmean(p.adr for p in others))
        self.assertIn(summary.median_other_tier, range(12, 18))


class ReferenceTierTests(unittest.TestCase):
    def mmr(self, tier=14, games_needed=0, seasons=None):
        seasons = seasons if seasons is not None else [SeasonInfo("e9a1", 50, 25, 11), SeasonInfo("e9a2", 30, 15, 0)]
        return MmrInfo(tier, 10, 1000, 5, games_needed, None, None, sum(s.games for s in seasons) or None, seasons)

    def test_current(self):
        self.assertEqual(reference_tier(self.mmr(14), None, []), (14, "current"))

    def test_lobby_when_in_placements(self):
        lobby = LobbySummary(3, 1.0, 1.0, 16, [])
        self.assertEqual(reference_tier(self.mmr(14, games_needed=2), lobby, []), (16, "lobby"))
        self.assertEqual(reference_tier(self.mmr(0), lobby, []), (16, "lobby"))

    def test_matches(self):
        matches = [match_stat(tier=0), match_stat(tier=9), match_stat(tier=12)]
        lobby = LobbySummary(0, None, None, None, [])
        self.assertEqual(reference_tier(self.mmr(0), lobby, matches), (9, "matches"))
        self.assertEqual(reference_tier(None, None, matches), (9, "matches"))

    def test_last_act(self):
        self.assertEqual(reference_tier(self.mmr(0), None, [match_stat(tier=0)]), (11, "last_act"))

    def test_unknown(self):
        self.assertEqual(reference_tier(None, None, []), (None, "unknown"))
        self.assertEqual(reference_tier(self.mmr(0, seasons=[]), None, []), (None, "unknown"))


class RankAveragesTests(unittest.TestCase):
    def build(self, tiers, me_stats=None):
        # Une partie par groupe de 9 autres joueurs
        me_stats = me_stats or {}
        lobbies = []
        for i in range(0, len(tiers), 9):
            others = [lobby_player(f"o{i + j}", tier=t, kills=10 + j, deaths=10, score=3000 + 100 * j,
                                   damage=2000 + 50 * j, head=10 + j, body=40, leg=0, level=50 + i + j)
                      for j, t in enumerate(tiers[i:i + 9])]
            lobbies.append(lobby_match(f"m{i}", [lobby_player("me", **me_stats)] + others))
        return lobbies

    def test_rank_scope(self):
        tiers = [12, 13, 14] * 3 + [20] * 9   # 9 lignes Or, 9 lignes Diamant
        lobbies = self.build(tiers, {"kills": 30, "deaths": 10, "score": 6000, "damage": 3000, "head": 30, "body": 60, "leg": 10})
        comp = rank_averages(lobbies, "me", 13, 42)
        self.assertEqual((comp.scope, comp.label, comp.matches, comp.sample), ("rank", "Or", 2, 9))
        sample = [p for m in lobbies for p in m.players if p.puuid != "me" and 12 <= p.tier <= 14]
        self.assertAlmostEqual(comp.ref["kd"], sum(p.kills for p in sample) / sum(p.deaths for p in sample))
        self.assertAlmostEqual(comp.ref["hs_pct"], sum(p.headshots for p in sample) / sum(p.hits for p in sample) * 100)
        self.assertAlmostEqual(comp.ref["acs"], statistics.fmean(p.acs for p in sample))
        self.assertAlmostEqual(comp.ref["adr"], statistics.fmean(p.adr for p in sample))
        self.assertEqual(comp.ref["level"], int(statistics.median(p.level for p in sample) + 0.5))
        self.assertAlmostEqual(comp.player["kd"], 3.0)
        self.assertAlmostEqual(comp.player["acs"], 300.0)
        self.assertAlmostEqual(comp.player["hs_pct"], 30.0)
        self.assertEqual(comp.player["level"], 42)

    def test_near_scope(self):
        # Ref Or 3 (14) : 5 lignes Or seulement, mais 9 lignes entre 11 et 17
        tiers = [12, 13, 14, 14, 13, 15, 16, 11, 17] + [24] * 9
        comp = rank_averages(self.build(tiers), "me", 14, 30)
        self.assertEqual(comp.scope, "near")
        self.assertEqual(comp.sample, 9)
        self.assertEqual(comp.label, "Argent 3 à Platine 3")

    def test_lobby_scope(self):
        tiers = [3, 4, 5, 6, 24, 25, 26, 27, 0]
        comp = rank_averages(self.build(tiers), "me", 14, 30)
        self.assertEqual((comp.scope, comp.label, comp.sample, comp.matches), ("lobby", "joueurs de ses lobbys", 9, 1))
        comp = rank_averages(self.build([14] * 9), "me", None, 30)
        self.assertEqual(comp.scope, "lobby")

    def test_absent(self):
        lobbies = self.build([14] * 9)
        self.assertIsNone(rank_averages(lobbies, "inconnu", 14, 30))
        self.assertIsNone(rank_averages([], "me", 14, 30))

    def test_fixture(self):
        lobbies = [parse_match_v4(load_fixture(name)) for name in MATCH_FIXTURES]
        comp = rank_averages(lobbies, PLAYER_PUUID, 15, 331)
        self.assertEqual(comp.matches, 5)
        self.assertIn(comp.scope, ("rank", "near", "lobby"))
        self.assertGreaterEqual(comp.sample, 8)
        self.assertEqual(comp.player["level"], 331)
        for key in ("kd", "hs_pct", "acs", "adr", "level"):
            self.assertIsNotNone(comp.ref[key], key)


if __name__ == "__main__":
    unittest.main()
