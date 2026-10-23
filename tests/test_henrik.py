import asyncio
import json
import logging
import unittest
from unittest import mock

import aiohttp

from core import henrik
from core.henrik import HenrikClient, RateLimited, NotFound, ApiError, HenrikError
from tests import load_fixture

FAKE_KEY = "HDEV-test-cle-secrete-0000"
HEADERS = {"x-ratelimit-limit": "30", "x-ratelimit-remaining": "28", "x-ratelimit-reset": "60"}
RATE_LIMIT_BODY = {"errors": [{"code": 0, "message": "Rate limit exceeded, please try again later.", "status": 429, "details": None}]}
NOT_FOUND_BODY = {"errors": [{"code": 22, "message": "Account not found", "status": 404, "details": None}]}


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, raw=None, enter_error=None):
        self.status = status
        self.headers = headers if headers is not None else dict(HEADERS)
        self._raw = raw if raw is not None else json.dumps(body).encode("utf-8")
        self._enter_error = enter_error

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self._raw


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []
        self.closed = False

    def get(self, url):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        self.closed = True


def ok(data, headers=None):
    return FakeResponse(200, {"status": 200, "data": data}, headers)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class HenrikClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch.object(henrik, "_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = HenrikClient(FAKE_KEY)

    def use(self, *responses):
        self.client._session = FakeSession(*responses)
        return self.client._session

    async def test_account_and_urls(self):
        data = load_fixture("account")
        session = self.use(ok(data))
        self.assertEqual(await self.client.account("Nom Ç#", "T/1"), data)
        self.assertEqual(session.urls[0], "https://api.henrikdev.xyz/valorant/v1/account/Nom%20%C3%87%23/T%2F1")

    async def test_endpoints(self):
        session = self.use(ok(load_fixture("mmr_v3")), ok(load_fixture("stored_matches")), ok(load_fixture("mmr_history")),
                           ok(load_fixture("match_v4_single")), ok([]))
        self.assertEqual((await self.client.mmr("eu", "abc"))["current"]["tier"]["id"], 15)
        self.assertEqual(len(await self.client.stored_matches("eu", "abc")), 20)
        self.assertIn("history", await self.client.mmr_history("eu", "abc"))
        self.assertIn("players", await self.client.match("eu", "m-1"))
        self.assertEqual(await self.client.stored_matches("eu", "abc", size=1), [])
        base = HenrikClient.BASE
        self.assertEqual(session.urls, [
            base + "/valorant/v3/by-puuid/mmr/eu/pc/abc",
            base + "/valorant/v1/by-puuid/stored-matches/eu/abc?mode=competitive&size=20",
            base + "/valorant/v2/by-puuid/mmr-history/eu/pc/abc",
            base + "/valorant/v4/match/eu/m-1",
            base + "/valorant/v1/by-puuid/stored-matches/eu/abc?mode=competitive&size=1",
        ])

    async def test_budget_from_headers(self):
        self.assertEqual(self.client.budget(), 30)   # rien de connu : limite par défaut
        self.use(ok({}, {"x-ratelimit-limit": "30", "x-ratelimit-remaining": "25", "x-ratelimit-reset": "40"}))
        await self.client.mmr("eu", "p")
        self.assertEqual(self.client.budget(), 25)
        self.clock.now += 39
        self.assertEqual(self.client.budget(), 25)
        self.clock.now += 2   # reset relatif : fenêtre terminée 40 s après la réponse
        self.assertEqual(self.client.budget(), 30)

    async def test_budget_new_limit_and_out_of_order(self):
        self.use(ok({}, {"X-RateLimit-Limit": "40", "X-RateLimit-Remaining": "20", "X-RateLimit-Reset": "50"}),
                 ok({}, {"x-ratelimit-limit": "40", "x-ratelimit-remaining": "22", "x-ratelimit-reset": "50"}))
        await self.client.mmr("eu", "p")
        await self.client.mmr("eu", "p")
        self.assertEqual(self.client.budget(), 20)   # réponses concurrentes reçues dans le désordre
        self.clock.now += 51
        self.assertEqual(self.client.budget(), 40)

    async def test_rate_limited_blocks_without_calling(self):
        session = self.use(FakeResponse(429, RATE_LIMIT_BODY, {"x-ratelimit-limit": "30", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "45"}),
                           ok({}))
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(RateLimited) as ctx:
                await self.client.account("Nom", "TAG")
        self.assertEqual(ctx.exception.retry_after, 45)
        self.assertEqual(self.client.budget(), 0)
        self.clock.now += 10
        with self.assertRaises(RateLimited) as ctx:
            await self.client.mmr("eu", "p")
        self.assertEqual(ctx.exception.retry_after, 35)
        self.assertEqual(len(session.urls), 1)        # aucun second appel pendant le blocage
        self.clock.now += 36
        await self.client.mmr("eu", "p")
        self.assertEqual(len(session.urls), 2)

    async def test_rate_limited_fallbacks(self):
        self.use(FakeResponse(429, RATE_LIMIT_BODY, {"retry-after": "12"}))
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(RateLimited) as ctx:
                await self.client.mmr("eu", "p")
        self.assertEqual(ctx.exception.retry_after, 12)

        client = HenrikClient(FAKE_KEY)
        client._session = FakeSession(FakeResponse(429, RATE_LIMIT_BODY, {}))
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(RateLimited) as ctx:
                await client.mmr("eu", "p")
        self.assertEqual(ctx.exception.retry_after, 60)

    async def test_rate_limited_with_reset_zero(self):
        # R1-F4 : « x-ratelimit-reset: 0 » veut dire « fenêtre qui se termine », pas « en-tête absent » (60 s)
        self.use(FakeResponse(429, RATE_LIMIT_BODY, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}))
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(RateLimited) as ctx:
                await self.client.mmr("eu", "p")
        self.assertEqual(ctx.exception.retry_after, 1)
        self.assertEqual(self.client.blocked_for(), 1)
        # Les deux en-têtes présents : on respecte le plus long
        self.clock.now += 2
        self.use(FakeResponse(429, RATE_LIMIT_BODY, {"x-ratelimit-reset": "5", "retry-after": "20"}))
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(RateLimited) as ctx:
                await self.client.mmr("eu", "p")
        self.assertEqual(ctx.exception.retry_after, 20)

    async def test_closed_client_never_reopens_a_session(self):
        # R1-F2 : après close(), une analyse encore en cours ne rouvre pas de session (jamais fermée sinon)
        session = self.use(ok({}))
        await self.client.close()
        self.assertTrue(session.closed)
        with self.assertRaises(ApiError) as ctx:
            await self.client.mmr("eu", "p")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIsNone(self.client._session)
        self.assertEqual(session.urls, [])

    async def test_not_found(self):
        self.use(FakeResponse(404, NOT_FOUND_BODY))
        with self.assertRaises(NotFound) as ctx:
            await self.client.account("Personne", "000")
        self.assertEqual(ctx.exception.code, 22)
        self.assertEqual(ctx.exception.message, "Account not found")
        self.assertIsInstance(ctx.exception, HenrikError)
        self.use(FakeResponse(404, raw=b"not json"))
        with self.assertRaises(NotFound) as ctx:
            await self.client.account("Personne", "000")
        self.assertIsNone(ctx.exception.code)

    async def test_forbidden_logs_critical_without_key(self):
        for status in (401, 403):
            self.use(FakeResponse(status, {"errors": [{"code": 1, "message": "Forbidden", "status": status}]}))
            with self.assertLogs("smurfbot", "CRITICAL") as logs:
                with self.assertRaises(ApiError) as ctx:
                    await self.client.account("Nom", "TAG")
            self.assertEqual(ctx.exception.status, status)
            self.assertEqual(ctx.exception.code, 1)
            self.assertTrue(any("Clé HenrikDev refusée" in line for line in logs.output))
            self.assertFalse(any(FAKE_KEY in line for line in logs.output))
            self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertNotIn(FAKE_KEY, repr(self.client))

    async def test_other_errors(self):
        self.use(FakeResponse(500, {"errors": [{"code": 5, "message": "Internal", "status": 500}]}))
        with self.assertRaises(ApiError) as ctx:
            await self.client.mmr("eu", "p")
        self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.message), (500, 5, "Internal"))

        self.use(FakeResponse(200, raw=b"<html>oops</html>"))
        with self.assertRaises(ApiError) as ctx:
            await self.client.mmr("eu", "p")
        self.assertEqual(ctx.exception.status, 200)

        self.use(ok({"not": "a list"}))
        with self.assertRaises(ApiError):
            await self.client.stored_matches("eu", "p")
        self.use(ok(None))
        with self.assertRaises(ApiError):
            await self.client.mmr("eu", "p")

    async def test_network_errors(self):
        errors = [aiohttp.ClientConnectionError("boom"), asyncio.TimeoutError()]
        for error in errors:
            self.use(error)
            with self.assertLogs("smurfbot", "WARNING"):
                with self.assertRaises(ApiError) as ctx:
                    await self.client.mmr("eu", "p")
            self.assertEqual(ctx.exception.status, 0)
        self.use(FakeResponse(enter_error=asyncio.TimeoutError()))
        with self.assertLogs("smurfbot", "WARNING"):
            with self.assertRaises(ApiError) as ctx:
                await self.client.mmr("eu", "p")
        self.assertEqual(ctx.exception.status, 0)

    async def test_session_lazy_and_close(self):
        client = HenrikClient(FAKE_KEY, max_concurrency=2, timeout=5)
        self.assertIsNone(client._session)
        session = client._get_session()   # aucune requête envoyée
        try:
            self.assertEqual(session.headers["Authorization"], FAKE_KEY)
            self.assertEqual(session.headers["User-Agent"], "SmurfWatch/1.0 (Discord bot)")
            self.assertEqual(session.timeout.total, 5)
            self.assertIs(client._get_session(), session)
        finally:
            await client.close()
        self.assertTrue(session.closed)
        self.assertIsNone(client._session)
        fake = self.use()
        await self.client.close()
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
