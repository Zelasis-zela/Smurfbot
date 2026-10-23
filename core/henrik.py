import asyncio
import json
import logging
import math
import time
from urllib.parse import quote

import aiohttp

log = logging.getLogger("smurfbot")
_now = time.monotonic   # remplaçable dans les tests

# Client asynchrone de l'API HenrikDev (clé Basic : 30 requêtes par minute, partagée par tout le bot)


class HenrikError(Exception):
    pass


class RateLimited(HenrikError):
    def __init__(self, retry_after=None):
        super().__init__("Limite de requêtes de l'API HenrikDev atteinte")
        self.retry_after = retry_after


class NotFound(HenrikError):
    def __init__(self, code=None, message="Introuvable"):
        super().__init__(message)
        self.code = code
        self.message = message


class ApiError(HenrikError):
    def __init__(self, status, code=None, message=""):
        super().__init__(f"HTTP {status} : {message}" if message else f"HTTP {status}")
        self.status = status
        self.code = code
        self.message = message


def _q(part):
    return quote(str(part), safe="")


def _header_int(headers, name):
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def _first_error(payload):
    # {"errors": [{"code": 22, "message": "Account not found", ...}]} -> (22, "Account not found")
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code, message = errors[0].get("code"), errors[0].get("message")
        return (code if isinstance(code, int) and not isinstance(code, bool) else None,
                message if isinstance(message, str) else "")
    return None, ""


def _expect(data, kind):
    if not isinstance(data, kind):
        raise ApiError(200, None, "Réponse inattendue de l'API")
    return data


class HenrikClient:
    BASE = "https://api.henrikdev.xyz"
    USER_AGENT = "SmurfWatch/1.0 (Discord bot)"
    DEFAULT_LIMIT = 30
    DEFAULT_BLOCK = 60    # secondes de pause après un 429 sans indication de l'API
    DEFAULT_WINDOW = 60   # fenêtre supposée si x-ratelimit-reset manque

    def __init__(self, api_key, *, max_concurrency=3, timeout=20):
        self._api_key = api_key
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._session = None
        self._limit = self.DEFAULT_LIMIT
        self._remaining = None
        self._window_end = 0.0     # _now() de fin de la fenêtre en cours
        self._blocked_until = 0.0  # _now() de fin du blocage après un 429
        self._closed = False

    def __repr__(self):
        return f"<HenrikClient budget={self.budget()}>"  # jamais la clé

    async def close(self):
        # Définitif : une analyse encore en cours ne doit pas rouvrir une session que personne ne fermerait
        self._closed = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _check_open(self):
        if self._closed:
            raise ApiError(0, None, "Client HenrikDev fermé")

    def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self._api_key, "User-Agent": self.USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self._session

    # ---------- Limite de requêtes ----------
    def blocked_for(self):
        # Secondes restantes de blocage après un 429 (0 si aucun)
        left = self._blocked_until - _now()
        return math.ceil(left) if left > 0 else 0

    def budget(self):
        # Appels encore disponibles dans la fenêtre en cours (estimation d'après les en-têtes)
        now = _now()
        if now < self._blocked_until:
            return 0
        if self._remaining is not None and now < self._window_end:
            return max(0, self._remaining)
        return self._limit

    def _update_limits(self, headers):
        limit = _header_int(headers, "x-ratelimit-limit")
        remaining = _header_int(headers, "x-ratelimit-remaining")
        reset = _header_int(headers, "x-ratelimit-reset")   # secondes avant la fin de la fenêtre
        if limit is not None and limit > 0:
            self._limit = limit
        if remaining is None:
            return
        now = _now()
        end = now + max(0, reset if reset is not None else self.DEFAULT_WINDOW)
        if self._remaining is not None and now < self._window_end and end <= self._window_end + 1:
            # même fenêtre : des réponses concurrentes peuvent arriver dans le désordre
            self._remaining = min(self._remaining, remaining)
            self._window_end = max(self._window_end, end)
        else:
            self._remaining = remaining
            self._window_end = end

    # ---------- Requêtes ----------
    async def _get(self, path):
        self._check_open()
        wait = self.blocked_for()
        if wait:
            raise RateLimited(wait)
        async with self._semaphore:
            self._check_open()         # le bot a pu s'arrêter pendant l'attente du sémaphore
            wait = self.blocked_for()  # un 429 a pu arriver pendant l'attente du sémaphore
            if wait:
                raise RateLimited(wait)
            try:
                session = self._get_session()
                async with session.get(self.BASE + path) as resp:
                    status = resp.status
                    headers = {str(k).lower(): v for k, v in resp.headers.items()}
                    raw = await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("API HenrikDev injoignable (%s) : %s", path.split("?")[0], type(e).__name__)
                raise ApiError(0, None, f"Erreur réseau ({type(e).__name__})") from e
        self._update_limits(headers)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None

        if status == 200:
            if not isinstance(payload, dict):
                raise ApiError(200, None, "Réponse JSON invalide")
            return payload.get("data")
        code, message = _first_error(payload)
        if status == 404:
            raise NotFound(code, message or "Introuvable")
        if status == 429:
            # x-ratelimit-reset peut valoir 0 (fenêtre qui se termine) : ce n'est pas « absent »
            known = [v for v in (_header_int(headers, "x-ratelimit-reset"), _header_int(headers, "retry-after"))
                     if v is not None]
            wait = max(1, max(known) if known else self.DEFAULT_BLOCK)
            self._blocked_until = _now() + wait
            self._remaining = 0
            self._window_end = max(self._window_end, self._blocked_until)
            log.warning("Limite de requêtes HenrikDev atteinte : appels suspendus pendant %s s", wait)
            raise RateLimited(wait)
        if status in (401, 403):
            log.critical("Clé HenrikDev refusée (HTTP %s) : vérifie API_KEY dans le fichier .env.", status)
            raise ApiError(status, code, message or "Clé API refusée")
        raise ApiError(status, code, message or f"Réponse HTTP {status}")

    async def account(self, name, tag):
        return _expect(await self._get(f"/valorant/v1/account/{_q(name)}/{_q(tag)}"), dict)

    async def mmr(self, region, puuid):
        return _expect(await self._get(f"/valorant/v3/by-puuid/mmr/{_q(region)}/pc/{_q(puuid)}"), dict)

    async def stored_matches(self, region, puuid, size=20):
        path = f"/valorant/v1/by-puuid/stored-matches/{_q(region)}/{_q(puuid)}?mode=competitive&size={int(size)}"
        return _expect(await self._get(path), list)

    async def mmr_history(self, region, puuid):
        return _expect(await self._get(f"/valorant/v2/by-puuid/mmr-history/{_q(region)}/pc/{_q(puuid)}"), dict)

    async def match(self, region, match_id):
        return _expect(await self._get(f"/valorant/v4/match/{_q(region)}/{_q(match_id)}"), dict)
