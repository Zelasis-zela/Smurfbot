import asyncio
import unittest
from collections import Counter
from unittest import mock

from core import analyzer as analyzer_module
from core.analyzer import Analyzer, Analysis, PlayerNotFound, AnalysisIncomplete
from core.analysis import Account, LobbyMatch
from core.henrik import RateLimited, NotFound, ApiError
from core.scoring import MISSING_HISTORY, MISSING_SEASONS, MISSING_RANKED_GAMES, MISSING_LOBBY, MISSING_NO_MATCHES
from tests import load_fixture, PLAYER_PUUID, MATCH_FIXTURES

STORED = load_fixture("stored_matches")
MATCHES = {load_fixture(name)["metadata"]["match_id"]: load_fixture(name) for name in MATCH_FIXTURES}
NEWEST_IDS = [e["meta"]["id"] for e in STORED[:5]]   # 3 d'entre elles ont un détail v4 en fixture


class FakeClient:
    # Client HenrikDev factice : réponses enregistrées, aucun appel réseau
    def __init__(self, budget=30, errors=None, stored=None):
        self.calls = []
        self._budget = budget
        self.errors = dict(errors or {})
        self.stored = STORED if stored is None else stored
        self.gate = None   # asyncio.Event pour retenir les appels

    def budget(self):
        return self._budget

    def count(self, method):
        return sum(1 for c in self.calls if c[0] == method)

    async def _call(self, method, *args):
        self.calls.append((method,) + args)
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)
        error = self.errors.get(method)
        if callable(error) and not isinstance(error, BaseException):
            error = error(*args)
        if error is not None:
            raise error

    async def account(self, name, tag):
        await self._call("account", name, tag)
        return load_fixture("account")

    async def mmr(self, region, puuid):
        await self._call("mmr", region, puuid)
        return load_fixture("mmr_v3")

    async def stored_matches(self, region, puuid, size=20):
        await self._call("stored_matches", region, puuid, size)
        return self.stored[:size]

    async def mmr_history(self, region, puuid):
        await self._call("mmr_history", region, puuid)
        return load_fixture("mmr_history")

    async def match(self, region, match_id):
        await self._call("match", region, match_id)
        if match_id not in MATCHES:
            raise NotFound(None, "Match not found")
        return MATCHES[match_id]


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class AnalyzerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch.object(analyzer_module, "_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_full_analysis(self):
        client = FakeClient()
        result = await Analyzer(client).analyze("TestSmurf", "0001")
        self.assertIsInstance(result, Analysis)
        self.assertFalse(result.from_cache)
        self.assertEqual(Counter(c[0] for c in client.calls),
                         Counter({"account": 1, "mmr": 1, "stored_matches": 1, "mmr_history": 1, "match": 5}))
        self.assertIn(("stored_matches", "eu", PLAYER_PUUID, 20), client.calls)
        self.assertEqual([c[2] for c in client.calls if c[0] == "match"], NEWEST_IDS)
        self.assertEqual((result.account.name, result.account.tag, result.account.puuid), ("TestSmurf", "0001", PLAYER_PUUID))
        self.assertEqual(len(result.matches), 20)
        self.assertEqual(len(result.history), 20)
        self.assertEqual(result.mmr.tier, 15)
        self.assertEqual(len(result.lobbies), 3)       # 2 détails introuvables : ignorés
        self.assertEqual(result.lobby.n, 3)
        self.assertEqual((result.ref_tier, result.ref_source), (15, "current"))
        self.assertIsNotNone(result.comparison)
        self.assertEqual(result.agg.n, 20)
        self.assertTrue(0 <= result.suspicion.index <= 100)
        self.assertFalse(any("Détail des parties limité" in line for line in result.suspicion.missing))
        self.assertIsInstance(result.analyzed_at, int)
        self.assertFalse(result.partial)   # 2 détails introuvables (404) : rien de passager

    async def test_cache(self):
        client = FakeClient()
        analyzer = Analyzer(client)
        first = await analyzer.analyze("TestSmurf", "0001")
        calls = len(client.calls)
        second = await analyzer.analyze(" testsmurf ", "0001")
        self.assertEqual(len(client.calls), calls)
        self.assertTrue(second.from_cache)
        self.assertFalse(first.from_cache)
        self.assertEqual(second.suspicion, first.suspicion)
        cached = analyzer.cached_by_puuid(PLAYER_PUUID)
        self.assertTrue(cached.from_cache)
        self.assertIsNone(analyzer.cached_by_puuid("autre"))
        # Expiration après 10 min
        self.clock.now += 601
        self.assertIsNone(analyzer.cached_by_puuid(PLAYER_PUUID))
        third = await analyzer.analyze("TestSmurf", "0001")
        self.assertFalse(third.from_cache)
        self.assertEqual(client.count("account"), 2)
        self.assertEqual(client.count("mmr"), 2)

    async def test_forget(self):
        client = FakeClient()
        analyzer = Analyzer(client)
        await analyzer.analyze("TestSmurf", "0001")
        analyzer.forget(PLAYER_PUUID)
        self.assertIsNone(analyzer.cached_by_puuid(PLAYER_PUUID))
        result = await analyzer.analyze("TestSmurf", "0001")
        self.assertFalse(result.from_cache)
        self.assertEqual(client.count("account"), 2)

    async def test_in_flight_dedup(self):
        client = FakeClient()
        analyzer = Analyzer(client)
        a, b = await asyncio.gather(analyzer.analyze("TestSmurf", "0001"), analyzer.analyze("TESTSMURF", "0001"))
        self.assertEqual(Counter(c[0] for c in client.calls),
                         Counter({"account": 1, "mmr": 1, "stored_matches": 1, "mmr_history": 1, "match": 5}))
        self.assertEqual(sorted([a.from_cache, b.from_cache]), [False, True])
        self.assertEqual(a.suspicion, b.suspicion)
        self.assertEqual(analyzer._inflight, {})

    async def test_cancelled_caller_does_not_cancel_shared_analysis(self):
        client = FakeClient()
        client.gate = asyncio.Event()
        analyzer = Analyzer(client)
        first = asyncio.ensure_future(analyzer.analyze("TestSmurf", "0001"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(analyzer.analyze("TestSmurf", "0001"))
        await asyncio.sleep(0)
        first.cancel()
        client.gate.set()
        result = await second
        self.assertTrue(result.from_cache)
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(client.count("account"), 1)
        self.assertIsNotNone(analyzer.cached_by_puuid(PLAYER_PUUID))

    async def test_lobby_budget_limit(self):
        client = FakeClient(budget=8)
        result = await Analyzer(client).analyze("TestSmurf", "0001")
        self.assertEqual(client.count("match"), 2)
        self.assertIn("Détail des parties limité pour ménager l'API (2/5).", result.suspicion.missing)
        self.assertEqual(result.suspicion.missing[-1], "Détail des parties limité pour ménager l'API (2/5).")
        self.assertIn(MISSING_LOBBY, result.suspicion.missing)   # 2 parties < 3 : critère 3 non évalué

        client = FakeClient(budget=3)
        result = await Analyzer(client).analyze("TestSmurf", "0001")
        self.assertEqual(client.count("match"), 0)
        self.assertEqual(result.lobbies, [])
        self.assertIsNone(result.lobby)
        self.assertIsNone(result.comparison)
        self.assertIn("Détail des parties limité pour ménager l'API (0/5).", result.suspicion.missing)

    async def test_lobby_failures_skipped(self):
        client = FakeClient(errors={"match": RateLimited(30)})
        result = await Analyzer(client).analyze("TestSmurf", "0001")
        self.assertEqual(result.lobbies, [])
        self.assertIn(MISSING_LOBBY, result.suspicion.missing)
        self.assertTrue(result.partial)

    async def test_partial_analysis_kept_one_minute_only(self):
        # R1-F1 : une analyse limitée par le budget d'API ne reste pas 10 min en cache
        client = FakeClient(budget=3)
        analyzer = Analyzer(client)
        first = await analyzer.analyze("TestSmurf", "0001")
        self.assertTrue(first.partial)
        self.assertTrue((await analyzer.analyze("TestSmurf", "0001")).from_cache)
        self.clock.now += 59
        self.assertIsNotNone(analyzer.cached_by_puuid(PLAYER_PUUID))
        self.clock.now += 2
        self.assertIsNone(analyzer.cached_by_puuid(PLAYER_PUUID))
        client._budget = 30
        second = await analyzer.analyze("TestSmurf", "0001")
        self.assertFalse(second.from_cache)
        self.assertFalse(second.partial)
        self.assertEqual(len(second.lobbies), 3)
        self.clock.now += 300
        self.assertTrue((await analyzer.analyze("TestSmurf", "0001")).from_cache)   # complète : 10 min

    async def test_partial_when_a_secondary_source_fails(self):
        with self.assertLogs("smurfbot", "WARNING"):
            result = await Analyzer(FakeClient(errors={"mmr_history": ApiError(502)})).analyze("TestSmurf", "0001")
        self.assertTrue(result.partial)
        # Introuvable (404) n'est pas passager : l'analyse est complète avec ce qui existe
        result = await Analyzer(FakeClient(errors={"mmr_history": NotFound(None, "x")})).analyze("TestSmurf", "0001")
        self.assertFalse(result.partial)

    async def test_stored_matches_errors(self):
        client = FakeClient(errors={"stored_matches": ApiError(500, None, "Internal")})
        analyzer = Analyzer(client)
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(AnalysisIncomplete):
                await analyzer.analyze("TestSmurf", "0001")

        client = FakeClient(errors={"stored_matches": NotFound(None, "x")})
        result = await Analyzer(client).analyze("TestSmurf", "0001")
        self.assertEqual(result.matches, [])
        self.assertEqual(client.count("match"), 0)
        self.assertEqual(result.suspicion.missing[0], MISSING_NO_MATCHES)
        self.assertNotIn("Détail des parties limité pour ménager l'API (0/0).", result.suspicion.missing)

    async def test_secondary_sources_errors(self):
        client = FakeClient(errors={"mmr": ApiError(500), "mmr_history": ApiError(0, None, "Erreur réseau")})
        with self.assertLogs("smurfbot", "WARNING"):
            result = await Analyzer(client).analyze("TestSmurf", "0001")
        self.assertIsNone(result.mmr)
        self.assertIsNone(result.history)
        for line in (MISSING_HISTORY, MISSING_SEASONS, MISSING_RANKED_GAMES):
            self.assertIn(line, result.suspicion.missing)
        self.assertEqual(result.ref_source, "lobby")   # plus de rang actuel : estimé d'après les lobbys

        client = FakeClient(errors={"mmr": NotFound(24, "No MMR data")})
        result = await Analyzer(client).analyze("TestSmurf", "0001")
        self.assertIsNone(result.mmr)

    async def test_rate_limited_propagates(self):
        for method in ("account", "mmr", "stored_matches", "mmr_history"):
            client = FakeClient(errors={method: RateLimited(42)})
            analyzer = Analyzer(client)
            with self.assertRaises(RateLimited) as ctx:
                await analyzer.analyze("TestSmurf", "0001")
            self.assertEqual(ctx.exception.retry_after, 42)
            self.assertEqual(analyzer._inflight, {})
            self.assertIsNone(analyzer.cached_by_puuid(PLAYER_PUUID))

    async def test_error_does_not_poison_cache(self):
        client = FakeClient(errors={"stored_matches": ApiError(502)})
        analyzer = Analyzer(client)
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(AnalysisIncomplete):
                await analyzer.analyze("TestSmurf", "0001")
        self.assertEqual(analyzer._inflight, {})
        self.assertEqual(analyzer._cache, {})
        client.errors.clear()
        result = await analyzer.analyze("TestSmurf", "0001")
        self.assertFalse(result.from_cache)
        self.assertEqual(client.count("stored_matches"), 2)
        self.assertEqual(client.count("account"), 1)   # le compte, lui, reste en cache

    async def test_concurrent_failure_reaches_both_callers(self):
        client = FakeClient(errors={"stored_matches": ApiError(502)})
        analyzer = Analyzer(client)
        with self.assertLogs("smurfbot", "WARNING"):
            results = await asyncio.gather(analyzer.analyze("TestSmurf", "0001"), analyzer.analyze("TestSmurf", "0001"),
                                           return_exceptions=True)
        self.assertTrue(all(isinstance(r, AnalysisIncomplete) for r in results))
        self.assertEqual(client.count("stored_matches"), 1)
        self.assertEqual(analyzer._inflight, {})

    async def test_find_account(self):
        client = FakeClient()
        analyzer = Analyzer(client)
        account = await analyzer.find_account("TestSmurf", "0001")
        self.assertIsInstance(account, Account)
        self.assertEqual(account.level, 331)
        await analyzer.find_account("testsmurf", "0001")
        self.assertEqual(client.count("account"), 1)

        client = FakeClient(errors={"account": NotFound(22, "Account not found")})
        with self.assertRaises(PlayerNotFound):
            await Analyzer(client).find_account("Personne", "000")
        with self.assertRaises(PlayerNotFound):
            await Analyzer(client).analyze("Personne", "000")

        client = FakeClient()
        client.account = mock.AsyncMock(return_value={"name": "x"})   # pas de puuid
        with self.assertRaises(ApiError):
            await Analyzer(client).find_account("x", "y")

    async def test_last_match(self):
        client = FakeClient()
        analyzer = Analyzer(client)
        account, match = await analyzer.last_match("TestSmurf", "0001")
        self.assertEqual(account.puuid, PLAYER_PUUID)
        self.assertIsInstance(match, LobbyMatch)
        self.assertEqual(match.match_id, NEWEST_IDS[0])
        self.assertIsNotNone(match.player(PLAYER_PUUID))
        self.assertIn(("stored_matches", "eu", PLAYER_PUUID, 1), client.calls)
        self.assertEqual(client.count("match"), 1)

    async def test_last_match_uses_cached_analysis(self):
        client = FakeClient()
        analyzer = Analyzer(client)
        await analyzer.analyze("TestSmurf", "0001")
        calls = len(client.calls)
        account, match = await analyzer.last_match("TestSmurf", "0001")
        self.assertEqual(len(client.calls), calls)
        self.assertEqual(match.match_id, NEWEST_IDS[0])

    async def test_last_match_none_and_errors(self):
        client = FakeClient(stored=[])
        self.assertIsNone(await Analyzer(client).last_match("TestSmurf", "0001"))
        client = FakeClient(errors={"stored_matches": NotFound(None, "x")})
        self.assertIsNone(await Analyzer(client).last_match("TestSmurf", "0001"))
        client = FakeClient(stored=STORED[3:])   # partie sans détail v4 disponible
        with self.assertRaises(AnalysisIncomplete):
            await Analyzer(client).last_match("TestSmurf", "0001")
        client = FakeClient(errors={"match": RateLimited(9)})
        with self.assertRaises(RateLimited):
            await Analyzer(client).last_match("TestSmurf", "0001")

    async def test_snapshot(self):
        client = FakeClient()
        snapshot = await Analyzer(client).snapshot("eu", PLAYER_PUUID)
        self.assertEqual(snapshot, {"tier": 15, "rank": "Platine 1", "elo": 1231, "games_needed": 0})
        self.assertIn(("mmr", "eu", PLAYER_PUUID), client.calls)
        client = FakeClient(errors={"mmr": NotFound(None, "x")})
        self.assertIsNone(await Analyzer(client).snapshot("eu", PLAYER_PUUID))
        client = FakeClient(errors={"mmr": RateLimited(5)})
        with self.assertRaises(RateLimited):
            await Analyzer(client).snapshot("eu", PLAYER_PUUID)


if __name__ == "__main__":
    unittest.main()
