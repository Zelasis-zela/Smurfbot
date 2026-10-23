import copy
import datetime
import re
import time

import discord

from core import fmt
from core.ranks import tier_name, tier_icon, tier_from_name, UNRANKED
from core.scoring import LEVELS, level_for, REPORT_THRESHOLD, QUICK_CAP

# Tous les embeds et textes affichés par les cogs. Chaque constructeur respecte les limites de Discord.

TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELD_NAME_MAX = 256
FIELD_MAX = 1024
FIELDS_MAX = 25
FOOTER_MAX = 2048
TOTAL_MAX = 6000
CONTENT_MAX = 2000
DOSSIER_TEXT_MAX = 1500
LIST_MAX = 20             # lignes de /watchlist
NOTABLE_MATCHES = 8       # parties citées dans un dossier

FOOTER = "Analyse indicative, ne constitue pas une preuve."
DOSSIER_FOOTER = "Analyse indicative, ne constitue pas une preuve. La décision finale appartient à Riot Games."
REMINDER = ("*Un bon joueur, un joueur qui revient d'une pause ou qui a progressé vite "
            "peut déclencher des signaux sans être un smurf.*")
REPORT_HINT = ("Si tu as toi-même affronté ce joueur, `/report` prépare un dossier privé pour les modérateurs : "
               "un seul dossier par personne suffit, inutile de le relayer.")
UNKNOWN = "inconnu"
COLOR_INFO = 0x5865F2
COLOR_OK = 0x2ECC71
STATUS_FIELD = "Statut"
STATUS_PENDING = "⏳ En attente de validation par un modérateur"
SUPPORT_SITE = "support-valorant.riotgames.com"

AGENT_EMOJIS = {
    "Jett": "<:jett:1527378099233362000>", "Reyna": "<:reyna:1527378937104171128>",
    "Raze": "<:raze:1527378379010211853>", "Phoenix": "<:phoenix:1527378438405750986>",
    "Yoru": "<:yoru:1527379215933112580>", "Neon": "<:neon:1527378640994832514>",
    "Iso": "<:iso:1527384160811876364>", "Sova": "<:sova:1527378232167370772>",
    "Breach": "<:breach:1527378328955388065>", "Skye": "<:skye:1527378704483750089>",
    "KAY/O": "<:kayo:1527379295822155966>", "Fade": "<:fade:1527379167900074164>",
    "Gekko": "<:gekko:1527378184679587931>", "Omen": "<:omen:1527379090691195070>",
    "Brimstone": "<:brimstone:1527378882985070762>", "Viper": "<:viper:1527378992766783548>",
    "Astra": "<:astra:1527378831390933162>", "Harbor": "<:harbor:1527379131166359835>",
    "Clove": "<:clove:1527384037986013294>", "Killjoy": "<:killjoy:1527379256060153916>",
    "Cypher": "<:cypher:1527378284789366906>", "Sage": "<:sage:1527378517921239240>",
    "Chamber": "<:chamber:1527378772012306562>", "Deadlock": "<:deadlock:1527379050816081991>",
    "Vyse": "<:vyse:1527384123335770142>", "Waylay": "<:waylay:1527384081891983420>",
}
DEFAULT_AGENT_EMOJI = "🦸"   # Tejo, Veto, Miks et les futurs agents


# ================= OUTILS =================
def clip(text, limit):
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


def join_lines(lines, limit, more="… et {k} autre(s)"):
    # Assemble des lignes sans dépasser `limit` ; celles qui ne tiennent pas sont résumées par `more`
    lines = [clip(line, limit) for line in lines]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    for keep in range(len(lines) - 1, -1, -1):
        text = "\n".join(lines[:keep] + [more.format(k=len(lines) - keep)])
        if len(text) <= limit:
            return text
    return clip(more.format(k=len(lines)), limit)


def fit_lines(lines, limit):
    # Comme join_lines, mais raccourcit d'abord chaque ligne pour toutes les garder (signaux)
    text = "\n".join(lines)
    if len(text) <= limit or not lines:
        return join_lines(lines, limit)
    width = max(20, (limit - (len(lines) - 1)) // len(lines))
    return join_lines([clip(line, width) for line in lines], limit)


def plural(n, word, word_plural=None):
    # En français, 0 et 1 restent au singulier
    return word if n is not None and abs(n) < 2 else (word_plural or word + "s")


_MARKDOWN = re.compile(r"([\\*_~|`])")


def escape(text):
    # Neutralise la mise en forme Discord (\ * _ ~ | `) dans un texte venant de l'API ou d'un membre
    return _MARKDOWN.sub(r"\\\1", "" if text is None else str(text))


def riot(name, tag):
    # Riot ID affiché dans du texte Discord (les _ et * des pseudos ne doivent pas casser la mise en forme)
    return escape(f"{name or UNKNOWN}#{tag or '?'}")


def plain_riot(name, tag):
    return f"{name or UNKNOWN}#{tag or '?'}"


def ts_tag(ts, style="d"):
    return f"<t:{int(ts)}:{style}>" if ts else None


def utc(ts):
    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)


def or_unknown(text):
    return UNKNOWN if text in (None, "", fmt.MISSING) else text


def agent_emoji(agent):
    return AGENT_EMOJIS.get(agent, DEFAULT_AGENT_EMOJI)


def level_row(index):
    return level_for(index)   # (max, clé, emoji, libellé, description, couleur)


def level_line(index):
    _, _, emoji, label, description, _ = level_row(index)
    return f"{emoji} **{index}/100 — {label}**\n{description[:1].upper()}{description[1:]}"


def display_rank(rank):
    # Rang enregistré en base : anciens noms anglais ("Gold 2") convertis en français
    tier = tier_from_name(rank)
    if tier is None:
        return rank or "rang inconnu"
    return tier_name(tier)


def fit(embed):
    # Dernière vérification avant l'envoi : rien ne doit dépasser les limites de Discord
    if embed.title:
        embed.title = clip(embed.title, TITLE_MAX)
    if embed.description:
        embed.description = clip(embed.description, DESCRIPTION_MAX)
    while len(embed.fields) > FIELDS_MAX:
        embed.remove_field(len(embed.fields) - 1)
    for i, field in enumerate(embed.fields):
        embed.set_field_at(i, name=clip(field.name, FIELD_NAME_MAX) or "​",
                           value=clip(field.value, FIELD_MAX) or "​", inline=field.inline)
    if embed.footer.text:
        embed.set_footer(text=clip(embed.footer.text, FOOTER_MAX), icon_url=embed.footer.icon_url)
    while len(embed) > TOTAL_MAX and embed.fields:
        i = max(range(len(embed.fields)), key=lambda k: len(embed.fields[k].value))
        field = embed.fields[i]
        if len(field.value) <= 1:
            break
        keep = max(1, len(field.value) - (len(embed) - TOTAL_MAX))
        embed.set_field_at(i, name=field.name, value=clip(field.value, keep), inline=field.inline)
    if len(embed) > TOTAL_MAX and embed.description:
        embed.description = clip(embed.description, max(1, len(embed.description) - (len(embed) - TOTAL_MAX)))
    return embed


# ================= TEXTES DE L'ANALYSE =================
def current_rank(mmr):
    # Rang actuel lisible (aussi enregistré en base)
    if mmr is None:
        return None
    if mmr.games_needed > 0:
        return "En placements"
    name = tier_name(mmr.tier)
    if name == UNRANKED:
        return UNRANKED
    return name


def current_rank_text(mmr):
    rank = current_rank(mmr)
    if rank is None:
        return UNKNOWN
    if rank == "En placements":
        left = mmr.games_needed
        return f"En placements (encore {left} {plural(left, 'partie')})"
    if rank != UNRANKED and mmr.rr is not None:
        return f"{rank} ({mmr.rr} RR)"
    return rank


def peak_text(mmr):
    if mmr is None or not mmr.peak_tier:
        return UNKNOWN
    season = f" ({mmr.peak_season})" if mmr.peak_season else ""
    return f"{tier_name(mmr.peak_tier)}{season}"


def winrate_text(agg):
    if agg.winrate is None:
        return UNKNOWN
    record = f"{agg.wins}V-{agg.losses}D" + (f"-{agg.draws}N" if agg.draws else "")
    return f"{fmt.num(agg.winrate)} % ({record})"


def stats_title(n):
    if n == 0:
        return "Stats clés"
    if n == 1:
        return "Stats clés (dernière partie)"
    return f"Stats clés ({n} dernières parties)"


def signal_lines(signals):
    return [f"• {s.label} (+{s.points})" for s in signals]


def context_lines(suspicion):
    return ([f"• {note}" for note in suspicion.notes]
            + [f"⚠️ Donnée manquante : {line}" for line in suspicion.missing])


# ================= /check =================
def analysis_embed(analysis, now=None):
    account, mmr, agg, suspicion = analysis.account, analysis.mmr, analysis.agg, analysis.suspicion
    row = level_row(suspicion.index)
    lines = [REMINDER]
    if suspicion.index >= REPORT_THRESHOLD:
        lines.append(REPORT_HINT)
    if analysis.from_cache:
        now = time.time() if now is None else now
        minutes = max(0, int((now - analysis.analyzed_at) // 60))
        ago = f"{minutes} min" if minutes else "moins d'une minute"
        lines.append(f"*Résultat en cache (analyse d'il y a {ago}).*")
    embed = discord.Embed(title=f"🔍 Analyse de {plain_riot(account.name, account.tag)}",
                          description="\n".join(lines), color=row[5], timestamp=utc(analysis.analyzed_at))
    if mmr is not None:
        embed.set_thumbnail(url=tier_icon(mmr.tier))

    level = fmt.num(account.level) if account.level is not None else UNKNOWN
    games = fmt.num(mmr.ranked_games) if mmr is not None and mmr.ranked_games is not None else UNKNOWN
    embed.add_field(name="Profil", inline=True, value=(
        f"**Rang actuel :** {current_rank_text(mmr)}\n"
        f"**Pic :** {peak_text(mmr)}\n"
        f"**Niveau du compte :** {level}\n"
        f"**Parties classées :** {games}"))
    if agg.n == 0:
        stats = "Aucune partie classée récente."
    else:
        stats = (f"**K/D :** {or_unknown(fmt.num(agg.kd, 2))}\n"
                 f"**HS % :** {or_unknown(fmt.pct(agg.hs_pct))}\n"
                 f"**ACS :** {or_unknown(fmt.num(agg.acs))}\n"
                 f"**ADR :** {or_unknown(fmt.num(agg.adr))}\n"
                 f"**Winrate :** {winrate_text(agg)}")
    embed.add_field(name=stats_title(agg.n), value=stats, inline=True)
    embed.add_field(name="Indice de suspicion", value=level_line(suspicion.index), inline=False)
    signals = fit_lines(signal_lines(suspicion.signals), FIELD_MAX) or "Aucun signal détecté."
    embed.add_field(name="Signaux détectés", value=signals, inline=False)
    context = context_lines(suspicion)
    if context:
        embed.add_field(name="Contexte", value=fit_lines(context, FIELD_MAX), inline=False)
    embed.set_footer(text=FOOTER)
    return fit(embed)


def matches_embed(analysis):
    # Bouton « Dernières parties » de /check (réponse visible du seul membre)
    lines = []
    for m in analysis.matches:
        icon = {"win": "✅", "loss": "❌"}.get(m.result, "➖")
        date = f"{ts_tag(m.started_at)} · " if m.started_at else ""
        lines.append(f"{icon} {date}**{escape(m.map)}** · {agent_emoji(m.agent)} {escape(m.agent)} · "
                     f"`{m.kills}/{m.deaths}/{m.assists}` · ACS {fmt.num(m.acs)} · HS {fmt.pct(m.hs_pct, 0)}")
    embed = discord.Embed(title=f"📜 Dernières parties classées — {plain_riot(analysis.account.name, analysis.account.tag)}",
                          description=join_lines(lines, DESCRIPTION_MAX) or "Aucune partie classée récente.",
                          color=COLOR_INFO)
    n = len(analysis.matches)
    embed.set_footer(text=f"{n} {plural(n, 'partie classée', 'parties classées')} · {FOOTER}")
    return fit(embed)


# ================= /compare =================
COMPARE_ROWS = [("K/D", "kd"), ("HS %", "hs_pct"), ("ACS", "acs"), ("ADR", "adr"), ("Niveau (méd.)", "level")]


def _cell(key, value):
    if value is None:
        return fmt.MISSING
    if key == "kd":
        return fmt.num(value, 2)
    if key == "hs_pct":
        return fmt.pct(value)
    return fmt.num(value)


def _gap(key, player, ref):
    if key == "level":
        return ""
    if player is None or ref is None:
        return fmt.MISSING
    if key == "hs_pct":
        return f"{fmt.signed(player - ref, 1)} pts"
    if not ref:
        return fmt.MISSING
    return fmt.ratio(player / ref)


def compare_table(comparison):
    rows = [("", "Joueur", "Moyenne", "Écart")]
    for label, key in COMPARE_ROWS:
        player, ref = comparison.player.get(key), comparison.ref.get(key)
        rows.append((label, _cell(key, player), _cell(key, ref), _gap(key, player, ref)))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() for r in rows)


def compare_embed(analysis):
    c, account = analysis.comparison, analysis.account
    games = "sa dernière partie" if c.matches == 1 else f"ses {c.matches} dernières parties"
    # Une ligne par joueur et par partie : un coéquipier croisé 5 fois compte 5 fois
    sample = f"{c.sample} {plural(c.sample, 'performance')} de {plural(c.sample, 'joueur')}"
    if c.scope == "lobby":
        intro = (f"Comparé à l'ensemble des joueurs croisés dans {games} ({sample}) : "
                 f"trop peu de joueurs de son rang pour une comparaison plus fine.")
        title = f"📊 {plain_riot(account.name, account.tag)} face à la moyenne de ses lobbys"
    else:
        intro = f"Comparé aux joueurs **{escape(c.label)}** croisés dans {games} ({sample})."
        title = f"📊 {plain_riot(account.name, account.tag)} face à la moyenne de son rang"
    intro += " Ses propres stats sont calculées sur ces mêmes parties ; pour le niveau, c'est la médiane."
    embed = discord.Embed(title=title, description=f"{intro}\n```text\n{compare_table(c)}\n```", color=COLOR_INFO)
    if analysis.ref_tier:
        embed.set_thumbnail(url=tier_icon(analysis.ref_tier))
    keys = ["kd", "hs_pct", "acs", "adr"]
    comparable = [k for k in keys if c.player.get(k) is not None and c.ref.get(k) is not None]
    above = sum(1 for k in comparable if c.player[k] > c.ref[k])
    total = "4" if len(comparable) == len(keys) else f"{len(comparable)} comparables"
    if not comparable:
        reading = "Aucune stat comparable sur ces parties."
    elif above == 0:
        reading = f"Au-dessus de la moyenne sur aucune stat (sur {total})."
    else:
        reading = f"Au-dessus de la moyenne sur {above} {plural(above, 'stat')} sur {total}."
    reading += ("\nÊtre au-dessus de la moyenne de son rang ne suffit pas à conclure : "
                "un bon joueur la dépasse naturellement.")
    embed.add_field(name="Lecture", value=reading, inline=False)
    embed.set_footer(text=FOOTER)
    return fit(embed)


# ================= /help =================
HELP_DESCRIPTION = ("Analyse de profils Valorant pour estimer la probabilité qu'un compte soit un smurf "
                    "(joueur expérimenté sur un compte à bas niveau ou à bas rang). Le Riot ID s'écrit `Nom#TAG`.")
HELP_SECTIONS = [
    ("🔍 Analyse", [
        ("check", "`/check` — analyse complète d'un joueur et indice de suspicion (option `prive` : réponse visible de toi seul)"),
        ("compare", "`/compare` — ses stats face à la moyenne des joueurs de son rang croisés dans ses parties"),
        ("smurf-match", "`/smurf-match` — estimation rapide des 10 joueurs de sa dernière partie classée"),
        ("historique", "`/historique` — évolution de son indice sur ce serveur"),
    ]),
    ("🗂️ Signalement", [
        ("report", f"`/report` — prépare un dossier pour les modérateurs (indice de {REPORT_THRESHOLD}/100 ou plus)"),
        ("watchlist", "`/watchlist` — comptes signalés sur ce serveur (modérateurs)"),
        ("report-setup", "`/report-setup` — configure le salon privé #reports-smurf, qui reçoit les dossiers et "
                         "les alertes anti-acharnement (administrateurs)"),
    ]),
    ("🔗 Compte", [
        ("link", "`/link` — lie ton compte Riot : les commandes l'utilisent quand tu ne saisis rien"),
        ("unlink", "`/unlink` — supprime ce lien"),
    ]),
    ("👁️ Suivi de rang", [
        ("watch", "`/watch` — annonce dans le salon les changements de rang d'un joueur"),
        ("unwatch", "`/unwatch` — arrête un suivi"),
        ("suivis", "`/suivis` — joueurs suivis sur ce serveur"),
    ]),
    ("ℹ️ Divers", [
        ("botstats", "`/botstats` — nombre de serveurs qui utilisent SmurfWatch"),
    ]),
]
HELP_RULES = [
    "L'indice est indicatif et ne constitue jamais une preuve : la décision finale appartient à Riot Games.",
    "Pas d'acharnement, de « name and shame » ni de signalements en groupe : les dossiers restent dans le salon des modérateurs.",
    "Un même compte ne peut être signalé qu'une fois tous les 7 jours par membre.",
    "Consulter sans cesse le même joueur est limité, et les modérateurs sont prévenus dans #reports-smurf.",
    "Seules des données de jeu sont utilisées : aucune information personnelle hors jeu.",
    "Aucune aide à la triche, aux exploits, à la vente de comptes ni au contournement des sanctions.",
]


def help_commands():
    return [name for _, entries in HELP_SECTIONS for name, _ in entries]


def scale_lines():
    lines, low = [], 0
    for high, _, emoji, label, description, _ in LEVELS:
        lines.append(f"{emoji} **{low}–{high} · {label}** : {description}")
        low = high + 1
    return lines


def help_embed(available=None):
    # available : noms des commandes réellement enregistrées (None = toutes)
    embed = discord.Embed(title="🛡️ SmurfWatch — commandes", description=HELP_DESCRIPTION, color=COLOR_INFO)
    for title, entries in HELP_SECTIONS:
        lines = [text for name, text in entries if available is None or name in available]
        if lines:
            embed.add_field(name=title, value=join_lines(lines, FIELD_MAX), inline=False)
    embed.add_field(name="📏 Échelle de l'indice", value="\n".join(scale_lines()), inline=False)
    embed.add_field(name="📌 Règles", value=join_lines([f"• {r}" for r in HELP_RULES], FIELD_MAX), inline=False)
    embed.set_footer(text=FOOTER)
    return fit(embed)


# ================= /smurf-match =================
TEAMS = [("red", "🔴 Équipe Rouge"), ("blue", "🔵 Équipe Bleue")]
QUICK_SIGNALS = {"level_rank": "niveau bas", "headshot": "HS %", "lobby_perf": "ACS/ADR"}   # critères de l'estimation
QUICK_LEGEND = (f"Indice sur {QUICK_CAP}, d'après trois critères seulement : **niveau bas** pour le rang, **HS %** "
                "au-dessus du seuil de son rang, **ACS/ADR** à plus de 1,5× la moyenne de la partie.")


def smurf_match_line(player, index, is_target, signals=()):
    _, _, emoji, *_ = level_row(index)
    me = " ⬅️" if is_target else ""
    level = fmt.num(player.level) if player.level is not None else "?"
    why = ", ".join(QUICK_SIGNALS.get(s.key, s.key) for s in signals)
    return (f"{emoji} **{index}**/{QUICK_CAP} · {agent_emoji(player.agent)} **{riot(player.name, player.tag)}**{me} — "
            f"{tier_name(player.tier)} (niv {level}) · K/D `{fmt.num(player.kd, 2)}` · "
            f"ACS `{fmt.num(player.acs)}` · HS `{fmt.pct(player.hs_pct)}`" + (f" · {why}" if why else ""))


def smurf_match_embed(account, match, scored):
    # scored : [(LobbyPlayer, indice, signaux)] pour les joueurs de la partie
    worst = max((index for _, index, _ in scored), default=0)
    when = f" · {ts_tag(match.started_at, 'f')}" if match.started_at else ""
    embed = discord.Embed(title=f"🔍 Dernière partie classée de {plain_riot(account.name, account.tag)}",
                          description=f"🗺️ **{escape(match.map)}**{when}\n{QUICK_LEGEND}\n{REMINDER}",
                          color=level_row(worst)[5])
    known = {key for key, _ in TEAMS}
    groups = [(key, label) for key, label in TEAMS] + [(None, "Autres joueurs")]
    for key, label in groups:
        members = [s for s in scored if (s[0].team == key if key else s[0].team not in known)]
        if not members:
            continue
        members.sort(key=lambda s: (-s[1], s[0].name.casefold()))
        if key:
            won = match.teams_rounds.get(key)
            title = f"{label} — {won if won is not None else '?'} {plural(won or 0, 'manche')}"
            if match.winner == key:
                title += " 🏆"
        else:
            title = label
        lines = [smurf_match_line(p, index, p.puuid == account.puuid, signals) for p, index, signals in members]
        embed.add_field(name=title, value=fit_lines(lines, FIELD_MAX), inline=False)   # aucun joueur retiré
    embed.set_footer(text=f"Estimation sur une seule partie (plafond {QUICK_CAP}) — /check pour une analyse complète. {FOOTER}")
    return fit(embed)


# ================= /historique =================
def is_old_model(model_version):
    return model_version is None or model_version < 2


def history_line(row):
    _, _, score, rank, kd, ts, match_count, model_version = row
    old = is_old_model(model_version)
    emoji = "⚪" if old or score is None else level_row(score)[2]
    # L'ancien bot enregistrait K/D = 0 quand il n'avait aucune stat : ce n'est pas un vrai K/D
    kd_text = UNKNOWN if kd is None or (old and not kd) else fmt.num(kd, 2)
    parts = [f"{emoji} **{score if score is not None else '?'}/100**", display_rank(rank), f"K/D `{kd_text}`"]
    if match_count:
        parts.append(f"{match_count} {plural(match_count, 'partie')}")
    when = f"{ts_tag(ts, 'd')} {ts_tag(ts, 't')} — " if ts else ""
    return when + " · ".join(parts) + (" (ancien calcul)" if old else "")


def history_embed(rows, total):
    # rows : (name, tag, score, rank, kd, ts, match_count, model_version), du plus récent au plus ancien
    name, tag = rows[0][0], rows[0][1]
    lines = join_lines([history_line(r) for r in rows], DESCRIPTION_MAX - len(REMINDER) - 1)
    embed = discord.Embed(title=f"📈 Historique : {plain_riot(name, tag)}",
                          description=f"{REMINDER}\n{lines}", color=COLOR_INFO)
    current = [r for r in rows if not is_old_model(r[7]) and r[2] is not None]
    if len(current) >= 2:
        diff = current[0][2] - current[-1][2]
        if diff > 0:
            trend = f"📈 **+{diff} points**"
        elif diff < 0:
            trend = f"📉 **{fmt.signed(diff)} points**"
        else:
            trend = "➡️ **Indice stable**"
        embed.add_field(name="Évolution", inline=False,
                        value=f"{trend} entre la plus ancienne et la plus récente des analyses affichées (calcul actuel).")
    if any(is_old_model(r[7]) for r in rows):
        embed.add_field(name="Ancien calcul", inline=False,
                        value="Les analyses marquées « ancien calcul » utilisaient une autre échelle : "
                              "elles ne sont pas comparables aux nouvelles.")
    embed.set_footer(text=f"{total} {plural(total, 'analyse')} {plural(total, 'enregistrée')} — "
                          f"{plural(len(rows), 'la plus récente est affichée', f'les {len(rows)} plus récentes sont affichées')} · {FOOTER}")
    return fit(embed)


# ================= /report =================
def notable_matches(analysis, limit=NOTABLE_MATCHES):
    # Parties citées dans le dossier : celles où il domine le plus son lobby, puis les meilleurs ACS
    by_id = {m.match_id: m for m in analysis.matches}
    lobbies = {m.match_id: m for m in analysis.lobbies}
    per_match = analysis.lobby.per_match if analysis.lobby else []
    ranked = sorted(per_match, key=lambda r: r[1] if r[1] is not None else -1.0, reverse=True)
    rows, seen = [], set()
    for match_id, acs_ratio, _ in ranked:
        if match_id in seen:
            continue
        seen.add(match_id)
        stat, lobby = by_id.get(match_id), lobbies.get(match_id)
        me = lobby.player(analysis.account.puuid) if lobby else None
        rows.append({
            "match_id": match_id,
            "map": stat.map if stat else (lobby.map if lobby else None),
            "started_at": stat.started_at if stat else (lobby.started_at if lobby else 0),
            "acs": stat.acs if stat else (me.acs if me else None),
            "ratio": acs_ratio,
        })
    for m in sorted((m for m in analysis.matches if m.match_id not in seen), key=lambda m: m.acs, reverse=True):
        seen.add(m.match_id)
        rows.append({"match_id": m.match_id, "map": m.map, "started_at": m.started_at, "acs": m.acs, "ratio": None})
    return rows[:limit]


def match_line(row):
    parts = [f"`{row['match_id']}`"]
    if row["map"]:
        parts.append(escape(row["map"]))
    if row["started_at"]:
        parts.append(ts_tag(row["started_at"]))
    if row["acs"] is not None:
        ratio = f" ({fmt.ratio(row['ratio'])} lobby)" if row["ratio"] is not None else ""
        parts.append(f"ACS {fmt.num(row['acs'])}{ratio}")
    return " · ".join(parts)


def key_stats_text(analysis):
    agg, account = analysis.agg, analysis.account
    level = fmt.num(account.level) if account.level is not None else UNKNOWN
    head = f"**Rang :** {current_rank_text(analysis.mmr)} · **Niveau :** {level}"
    if agg.n == 0:
        return head + "\nAucune partie classée récente."
    return (f"{head}\n"
            f"**K/D :** {or_unknown(fmt.num(agg.kd, 2))} · **HS % :** {or_unknown(fmt.pct(agg.hs_pct))} · "
            f"**ACS :** {or_unknown(fmt.num(agg.acs))} · **ADR :** {or_unknown(fmt.num(agg.adr))}\n"
            f"**Winrate :** {winrate_text(agg)} sur {agg.n} {plural(agg.n, 'partie')}")


def dossier_embed(report_id, analysis, reporter, observations, created_at, previous_reports=0):
    # previous_reports : autres dossiers déjà ouverts sur ce compte dans ce serveur (signalements en groupe ?)
    account, suspicion = analysis.account, analysis.suspicion
    created_at = created_at or int(time.time())
    embed = discord.Embed(title=f"🗂️ Dossier n°{report_id} — {plain_riot(account.name, account.tag)}",
                          description=REMINDER, color=level_row(suspicion.index)[5], timestamp=utc(created_at))
    embed.add_field(name="Riot ID", value=riot(account.name, account.tag), inline=True)
    embed.add_field(name="Date", value=ts_tag(created_at, "f"), inline=True)
    embed.add_field(name="Signalé par", value=reporter.mention, inline=True)
    if previous_reports:
        embed.add_field(name="Autres dossiers sur ce compte", inline=False, value=(
            f"{previous_reports} {plural(previous_reports, 'dossier déjà ouvert', 'dossiers déjà ouverts')} "
            "sur ce serveur (voir `/watchlist`) : vérifier qu'il ne s'agit pas de signalements en groupe."))
    embed.add_field(name="Indice de suspicion", value=level_line(suspicion.index), inline=False)
    embed.add_field(name="Stats anormales", inline=False,
                    value=fit_lines(signal_lines(suspicion.signals), FIELD_MAX) or "Aucun signal détecté.")
    embed.add_field(name="Stats clés", value=key_stats_text(analysis), inline=False)
    lines = [match_line(row) for row in notable_matches(analysis)]
    embed.add_field(name="Parties concernées", value=join_lines(lines, FIELD_MAX) or "Aucune partie disponible.", inline=False)
    context = context_lines(suspicion)
    if context:
        embed.add_field(name="Contexte", value=fit_lines(context, FIELD_MAX), inline=False)
    text = (observations or "").strip()
    embed.add_field(name="Observations du membre", value=clip(text, FIELD_MAX) if text else "Aucune.", inline=False)
    embed.add_field(name=STATUS_FIELD, value=STATUS_PENDING, inline=False)
    embed.set_footer(text=DOSSIER_FOOTER)
    return fit(embed)


def review_status_text(status, reviewer_mention, ts):
    when = f" le {ts_tag(ts, 'f')}" if ts else ""
    if status == "validated":
        return f"✅ Validé par {reviewer_mention}{when}"
    return f"❌ Rejeté par {reviewer_mention}{when}"


def with_status(embed, text):
    # Copie de l'embed du dossier avec le champ « Statut » mis à jour
    embed = discord.Embed.from_dict(copy.deepcopy(embed.to_dict()))   # copy() partagerait les champs
    for i, field in enumerate(embed.fields):
        if field.name == STATUS_FIELD:
            embed.set_field_at(i, name=STATUS_FIELD, value=text, inline=False)
            break
    else:
        embed.add_field(name=STATUS_FIELD, value=text, inline=False)
    return fit(embed)


def _date_text(ts):
    return utc(ts).strftime("%d/%m/%Y %H:%M UTC") if ts else UNKNOWN


def dossier_text(report_id, analysis, observations, created_at, limit=DOSSIER_TEXT_MAX):
    # Version texte brut du dossier, à joindre à un ticket du support Riot (≤ 1 500 caractères)
    account, agg, suspicion = analysis.account, analysis.agg, analysis.suspicion
    label = level_row(suspicion.index)[3]
    level = fmt.num(account.level) if account.level is not None else UNKNOWN
    lines = [
        f"Dossier SmurfWatch n°{report_id}",
        f"Riot ID : {plain_riot(account.name, account.tag)}",
        f"Date : {_date_text(created_at)}",
        f"Indice de suspicion : {suspicion.index}/100 ({label}) — indicatif, ne constitue pas une preuve",
        f"Rang : {current_rank_text(analysis.mmr)} · Niveau du compte : {level}",
    ]
    if agg.n:
        lines.append(f"Stats ({agg.n} {plural(agg.n, 'partie classée', 'parties classées')}) : "
                     f"K/D {fmt.num(agg.kd, 2)} · HS {fmt.pct(agg.hs_pct)} · ACS {fmt.num(agg.acs)} · "
                     f"ADR {fmt.num(agg.adr)} · Winrate {winrate_text(agg)}")
    lines.append("Stats anormales :")
    lines += [f"- {s.label}" for s in suspicion.signals] or ["- aucune"]
    ids = [row["match_id"] for row in notable_matches(analysis)]
    if ids:
        lines.append("Parties concernées (ID) :")
        lines += [f"- {match_id}" for match_id in ids]
    head = "\n".join(lines).replace("```", "'''")
    text = (observations or "").strip().replace("```", "'''")
    obs = "Observations du membre : " + (text or "aucune")
    room = limit - len(head) - 1
    if room < len(obs):
        obs = clip(obs, room) if room > 40 else ""
    full = f"{head}\n{obs}" if obs else head
    return clip(full, limit)


def official_report_embed(report_id, redacted=False):
    # redacted : des informations personnelles ont été retirées des observations du membre
    embed = discord.Embed(
        title=f"✅ Dossier n°{report_id} transmis aux modérateurs",
        color=COLOR_OK,
        description=(
            "Les modérateurs vont l'examiner avant toute action sur le serveur. SmurfWatch ne peut pas signaler "
            "un joueur à Riot à ta place : pour un signalement officiel, deux possibilités.\n\n"
            "**En jeu** : écran de fin de partie ou historique → sélectionne le joueur → **Signaler** → "
            "catégorie appropriée (ex. « Manipulation du classement / smurf » si disponible).\n\n"
            f"**Support Riot** : ouvre un ticket sur {SUPPORT_SITE} et joins le dossier ci-dessus "
            "(bloc de texte à copier)."
        ),
    )
    if redacted:
        embed.add_field(name="ℹ️ Observations", inline=False, value=(
            "Des informations personnelles (liens, pseudos de réseaux, contacts, numéros) ont été retirées de tes "
            "observations : un dossier ne contient que des faits de jeu."))
    embed.add_field(name="À garder en tête", inline=False, value=(
        "L'indice de suspicion est indicatif et ne constitue pas une preuve : la décision finale appartient "
        "à Riot Games. Merci de ne pas diffuser ce dossier publiquement ni d'interpeller le joueur."))
    embed.set_footer(text=DOSSIER_FOOTER)
    return fit(embed)


def dossier_message(text):
    # Message (contenu) qui accompagne official_report_embed : le dossier en texte brut à copier
    content = f"📋 **Dossier à joindre à ton ticket Riot :**\n```text\n{text}\n```"
    return content if len(content) <= CONTENT_MAX else clip(content, CONTENT_MAX)


def duplicate_report_text(created_at, days=7):
    again = int(created_at) + days * 86400
    return (f"Tu as déjà signalé ce compte le {ts_tag(created_at, 'd')}. Un même compte ne peut être signalé "
            f"qu'une fois tous les {days} jours par membre : tu pourras recommencer {ts_tag(again, 'R')}.")


PARTIAL_REFUSAL = ("⚠️ L'analyse est incomplète (API Valorant très sollicitée) : ce refus n'est pas compté comme "
                   "une tentative, réessaie dans quelques minutes.")


def report_refused_embed(analysis):
    account, suspicion = analysis.account, analysis.suspicion
    description = (f"L'indice de suspicion de **{riot(account.name, account.tag)}** est de "
                   f"{level_row(suspicion.index)[2]} **{suspicion.index}/100 ({level_row(suspicion.index)[3]})**.\n\n"
                   f"Un dossier n'est accepté qu'à partir de {REPORT_THRESHOLD}/100 : en dessous, les signaux ne sont "
                   "pas assez convergents. Un bon joueur, un joueur qui revient d'une pause ou qui a progressé vite "
                   "peut déclencher quelques signaux sans être un smurf.")
    if getattr(analysis, "partial", False):
        description += f"\n\n{PARTIAL_REFUSAL}"
    embed = discord.Embed(title="Signalement non retenu", color=level_row(suspicion.index)[5], description=description)
    embed.add_field(name="Signaux détectés", inline=False,
                    value=fit_lines(signal_lines(suspicion.signals), FIELD_MAX) or "Aucun signal détecté.")
    context = context_lines(suspicion)
    if context:
        embed.add_field(name="Contexte", value=fit_lines(context, FIELD_MAX), inline=False)
    embed.set_footer(text=FOOTER)
    return fit(embed)


# ================= /watchlist =================
def reported_icon(row):
    if row.get("pending"):
        return "⏳"
    if row.get("validated"):
        return "✅"
    return "❌"


def reported_line(row):
    reports, reporters = row.get("reports") or 0, row.get("reporters") or 0
    max_score = row.get("max_score")
    parts = [f"{reports} {plural(reports, 'dossier')} par {reporters} {plural(reporters, 'membre')}",
             f"indice max {max_score if max_score is not None else fmt.MISSING}"]
    if row.get("last_at"):
        parts.append(f"dernier {ts_tag(row['last_at'], 'R')}")
    parts.append(f"n°{row.get('last_id')}")
    return f"{reported_icon(row)} **{riot(row.get('name'), row.get('tag'))}** — " + " · ".join(parts)


def watchlist_embed(rows, status_label=None):
    lines = [reported_line(r) for r in rows[:LIST_MAX]]
    if len(rows) > LIST_MAX:
        lines.append(f"… et {len(rows) - LIST_MAX} autres")
    head = f"Filtre : **{status_label}**\n" if status_label else ""
    embed = discord.Embed(title="🗂️ Comptes signalés sur ce serveur", color=COLOR_INFO,
                          description=head + join_lines(lines, DESCRIPTION_MAX - len(head)))
    embed.add_field(name="Légende", inline=False,
                    value="⏳ au moins un dossier en attente · ✅ validé · ❌ rejeté")
    embed.set_footer(text="Liste réservée aux modérateurs")
    return fit(embed)


# ================= Anti-acharnement =================
def harassment_refusal(command, notified):
    if command == "report":
        text = ("Tu as déjà beaucoup consulté ou signalé ce joueur récemment. Pour éviter tout acharnement, "
                "je ne crée pas de nouveau dossier pour l'instant")
    elif command == "historique":
        text = ("Tu as déjà beaucoup consulté ce joueur récemment. Pour éviter tout acharnement, "
                "je n'affiche pas son historique pour l'instant")
    else:
        text = ("Tu as déjà beaucoup consulté ce joueur récemment. Pour éviter tout acharnement, "
                "je ne relance pas l'analyse pour l'instant")
    return text + (" ; les modérateurs ont été prévenus." if notified else ". Réessaie dans quelques heures.")


def harassment_alert_embed(member, account, reason, command, linked=False):
    # linked : le joueur visé est le compte lié par ce membre avec /link (liaison non vérifiée)
    who = riot(account.name, account.tag) + (" (compte lié par ce membre avec `/link`)" if linked else "")
    embed = discord.Embed(
        title="🚨 Protection anti-acharnement",
        color=0xE67E22,
        description=(f"{member.mention} a demandé `/{command}` sur **{who}** : "
                     f"déjà {reason}. La demande a été refusée poliment ; "
                     "aucune analyse ni aucun dossier n'a été créé."),
    )
    embed.add_field(name="À faire", inline=False,
                    value="Vérifier s'il s'agit d'un acharnement et, si besoin, en parler avec le membre.")
    embed.set_footer(text="Une seule alerte par membre et par joueur toutes les 24 h.")
    return fit(embed)


# ================= Suivi de rang (/watch) =================
def watch_change_text(watch, snapshot):
    # Texte à annoncer, ou None si rien à dire (pas rejoué, placements, données manquantes)
    if not snapshot or not snapshot.get("tier") or snapshot.get("games_needed"):
        return None
    old_elo, new_elo = watch.get("last_elo"), snapshot.get("elo")
    if old_elo is None or new_elo is None or old_elo == new_elo:
        return None
    who = f"**{riot(watch.get('name'), watch.get('tag'))}**"
    old_tier, new_tier = tier_from_name(watch.get("last_rank")), snapshot["tier"]
    if old_tier and new_tier and old_tier != new_tier:
        if new_tier > old_tier:
            return f"📈 {who} est monté de **{tier_name(old_tier)}** à **{tier_name(new_tier)}**."
        return f"📉 {who} est descendu de **{tier_name(old_tier)}** à **{tier_name(new_tier)}**."
    return f"🎮 {who} a rejoué en classé : **{fmt.signed(new_elo - old_elo)} RR** ({tier_name(new_tier)})."


def watch_notice_embed(text):
    embed = discord.Embed(description=clip(text, DESCRIPTION_MAX), color=COLOR_INFO)
    embed.set_footer(text="Suivi de rang /watch — /unwatch pour arrêter")
    return fit(embed)


def suivis_embed(watches, max_watches, interval_min):
    lines = [f"• **{riot(w['name'], w['tag'])}** — {display_rank(w['last_rank']) if w['last_rank'] else 'rang inconnu'}"
             f" · <#{w['channel_id']}> · ajouté par <@{w['user_id']}>" for w in watches]
    embed = discord.Embed(title="👁️ Joueurs suivis", description=join_lines(lines, DESCRIPTION_MAX), color=COLOR_INFO)
    embed.set_footer(text=f"{len(watches)}/{max_watches} — vérification toutes les {interval_min} minutes")
    return fit(embed)
