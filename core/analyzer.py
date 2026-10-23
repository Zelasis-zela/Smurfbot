import asyncio
import logging
import time
from dataclasses import dataclass, replace

from core.analysis import (
    Account, MmrInfo, MatchStat, RrChange, LobbyMatch, LobbySummary, Aggregate, RankComparison,
    parse_account, parse_mmr, parse_stored_matches, parse_mmr_history, parse_match_v4,
    aggregate, lobby_summary, reference_tier, rank_averages,
)
from core.henrik import ApiError, NotFound, RateLimited
from core.ranks import tier_name
from core.scoring import Suspicion, compute_suspicion

log = logging.getLogger("smurfbot")
_now = time.monotonic   # remplaçable dans les tests

# Orchestration d'une analyse : appels API, cache de 10 min, une seule analyse en cours par joueur.
# Rien n'est écrit en base ici : ce sont les cogs qui enregistrent.

PARSE_ERRORS = (TypeError, ValueError, KeyError, AttributeError)


class PlayerNotFound(Exception):
    def __init__(self, name=None, tag=None):
        super().__init__(f"Joueur introuvable : {name}#{tag}")
        self.name = name
        self.tag = tag


class AnalysisIncomplete(Exception):
    pass


@dataclass
class Analysis:
    account: Account
    mmr: MmrInfo | None
    matches: list[MatchStat]
    history: list[RrChange] | None
    lobbies: list[LobbyMatch]
    lobby: LobbySummary | None
    agg: Aggregate
    ref_tier: int | None
    ref_source: str
    comparison: RankComparison | None
    suspicion: Suspicion
    analyzed_at: int
    from_cache: bool = False
    partial: bool = False   # une source a manqué pour une raison passagère (budget d'API, erreur) : gardée 1 min


def lobby_limited_line(k, wanted):
    return f"Détail des parties limité pour ménager l'API ({k}/{wanted})."


class Analyzer:
    def __init__(self, client, *, ttl=600, partial_ttl=60, lobby_matches=5, lobby_reserve=6):
        self.client = client
        self.ttl = ttl
        self.partial_ttl = partial_ttl
        self.lobby_matches = lobby_matches
        self.lobby_reserve = lobby_reserve
        self._accounts = {}   # (nom, tag) casefold -> (monotonic, Account)
        self._cache = {}      # (nom, tag) casefold -> (monotonic, Analysis)
        self._inflight = {}   # (nom, tag) casefold -> Task partagée par les appels simultanés

    @staticmethod
    def _key(name, tag):
        return (str(name).strip().casefold(), str(tag).strip().casefold())

    def _fresh(self, entry):
        return entry is not None and _now() - entry[0] < self.ttl

    def _store(self, cache, key, value, ttl=None):
        now = _now()
        for old in [k for k, (ts, _) in cache.items() if now - ts >= self.ttl]:
            del cache[old]
        # Durée de vie plus courte : l'entrée est antidatée, _fresh() la juge périmée plus tôt
        stamp = now - (self.ttl - ttl) if ttl is not None and ttl < self.ttl else now
        cache[key] = (stamp, value)

    # ---------- Compte ----------
    async def find_account(self, name, tag):
        key = self._key(name, tag)
        entry = self._accounts.get(key)
        if self._fresh(entry):
            return entry[1]
        try:
            data = await self.client.account(name, tag)
        except NotFound as e:
            raise PlayerNotFound(name, tag) from e
        try:
            account = parse_account(data)
        except PARSE_ERRORS as e:
            raise ApiError(200, None, "Réponse de compte invalide") from e
        if not account.name or not account.tag:
            account = replace(account, name=account.name or str(name).strip(), tag=account.tag or str(tag).strip())
        self._store(self._accounts, key, account)
        return account

    # ---------- Analyse complète ----------
    async def analyze(self, name, tag):
        key = self._key(name, tag)
        entry = self._cache.get(key)
        if self._fresh(entry):
            return replace(entry[1], from_cache=True)
        task = self._inflight.get(key)
        if task is not None:
            # Même joueur déjà en cours d'analyse : on attend le même résultat (aucun appel en plus)
            return replace(await asyncio.shield(task), from_cache=True)
        task = asyncio.ensure_future(self._run(key, name, tag))
        self._inflight[key] = task
        task.add_done_callback(lambda t: self._task_done(key, t))
        return await asyncio.shield(task)

    def _task_done(self, key, task):
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # marque l'exception comme lue même si l'appelant a été annulé

    async def _run(self, key, name, tag):
        account = await self.find_account(name, tag)
        region, puuid = account.region, account.puuid
        results = await asyncio.gather(
            self.client.mmr(region, puuid),
            self.client.stored_matches(region, puuid, size=20),
            self.client.mmr_history(region, puuid),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, RateLimited) or (isinstance(result, BaseException) and not isinstance(result, Exception)):
                raise result
        mmr_data, stored_data, history_data = results

        if isinstance(stored_data, NotFound):
            matches = []
        elif isinstance(stored_data, Exception):
            log.warning("Parties classées indisponibles pour %s#%s : %s", account.name, account.tag, stored_data)
            raise AnalysisIncomplete("parties classées indisponibles") from stored_data
        else:
            try:
                matches = parse_stored_matches(stored_data)
            except PARSE_ERRORS as e:
                raise AnalysisIncomplete("parties classées illisibles") from e
        mmr = self._optional(parse_mmr, mmr_data, "MMR", account)
        history = self._optional(parse_mmr_history, history_data, "historique de RR", account)

        lobbies, k, wanted, failed = await self._fetch_lobbies(account, matches)
        lobby = lobby_summary(lobbies, puuid)
        ref_tier, ref_source = reference_tier(mmr, lobby, matches)
        comparison = rank_averages(lobbies, puuid, ref_tier, account.level)
        suspicion = compute_suspicion(account, mmr, matches, history, lobby, ref_tier, ref_source)
        if k < wanted:
            suspicion.missing.append(lobby_limited_line(k, wanted))
        # Analyse incomplète pour une raison passagère : la relancer dans une minute peut donner un résultat complet
        transient = [r for r in (mmr_data, history_data) if isinstance(r, Exception) and not isinstance(r, NotFound)]
        partial = bool(transient) or k < wanted or failed > 0

        analysis = Analysis(
            account=account, mmr=mmr, matches=matches, history=history, lobbies=lobbies, lobby=lobby,
            agg=aggregate(matches), ref_tier=ref_tier, ref_source=ref_source, comparison=comparison,
            suspicion=suspicion, analyzed_at=int(time.time()), partial=partial,
        )
        self._store(self._cache, key, analysis, ttl=self.partial_ttl if partial else None)
        return analysis

    def _optional(self, parse, value, label, account):
        # Source secondaire : en cas d'erreur, None (le critère concerné signale la donnée manquante)
        if isinstance(value, Exception):
            if not isinstance(value, NotFound):
                log.warning("%s indisponible pour %s#%s : %s", label, account.name, account.tag, value)
            return None
        try:
            return parse(value)
        except PARSE_ERRORS as e:
            log.warning("%s illisible pour %s#%s : %s", label, account.name, account.tag, e)
            return None

    async def _fetch_lobbies(self, account, matches):
        # Détail (v4) des parties les plus récentes, dans la limite du budget d'appels restant
        ids = [m.match_id for m in matches[:self.lobby_matches]]
        wanted = len(ids)
        k = min(wanted, max(0, self.client.budget() - self.lobby_reserve))
        results = await asyncio.gather(*(self.client.match(account.region, match_id) for match_id in ids[:k]),
                                       return_exceptions=True)
        lobbies, failed = [], 0   # failed : échecs passagers (429, erreur réseau…), pas les parties introuvables
        for match_id, result in zip(ids[:k], results):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                if not isinstance(result, NotFound):
                    failed += 1
                log.info("Partie %s ignorée pour %s#%s : %s", match_id, account.name, account.tag, result)
                continue
            try:
                lobby = parse_match_v4(result)
            except PARSE_ERRORS:
                lobby = None
            if lobby is not None:
                lobbies.append(lobby)
        return lobbies, k, wanted, failed

    # ---------- Cache ----------
    def cached_by_puuid(self, puuid):
        best = None
        for entry in self._cache.values():
            if entry[1].account.puuid == puuid and self._fresh(entry) and (best is None or entry[0] > best[0]):
                best = entry
        return replace(best[1], from_cache=True) if best else None

    def forget(self, puuid):
        for cache in (self._cache, self._accounts):
            for key in [k for k, (_, value) in cache.items()
                        if (value.account.puuid if isinstance(value, Analysis) else value.puuid) == puuid]:
                del cache[key]

    # ---------- Dernière partie (/smurf-match) ----------
    async def last_match(self, name, tag):
        account = await self.find_account(name, tag)
        match_id = None
        entry = self._cache.get(self._key(name, tag))
        if self._fresh(entry) and entry[1].matches:
            # Analyse récente en cache : on réutilise la dernière partie (et son détail si déjà chargé)
            cached = entry[1]
            match_id = cached.matches[0].match_id
            lobby = next((m for m in cached.lobbies if m.match_id == match_id), None)
            if lobby is not None:
                return account, lobby
        if match_id is None:
            try:
                data = await self.client.stored_matches(account.region, account.puuid, size=1)
            except NotFound:
                return None
            matches = parse_stored_matches(data)
            if not matches:
                return None
            match_id = matches[0].match_id
        try:
            data = await self.client.match(account.region, match_id)
        except NotFound as e:
            raise AnalysisIncomplete("détail de la partie introuvable") from e
        match = parse_match_v4(data)
        if match is None:
            raise AnalysisIncomplete("détail de la partie illisible")
        return account, match

    # ---------- Rang du moment (/watch) ----------
    async def snapshot(self, region, puuid):
        try:
            data = await self.client.mmr(region, puuid)
        except NotFound:
            return None
        try:
            mmr = parse_mmr(data)
        except PARSE_ERRORS as e:
            log.warning("MMR illisible pour %s : %s", puuid, e)
            return None
        return {"tier": mmr.tier, "rank": tier_name(mmr.tier), "elo": mmr.elo, "games_needed": mmr.games_needed}
