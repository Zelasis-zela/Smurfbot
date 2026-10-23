import logging
import re
from urllib.parse import quote

import discord
from discord import app_commands

from core import db, embeds, guard
from core.errors import error_message

log = logging.getLogger("smurfbot")

# Outils communs aux cogs : Riot ID, autocomplétion, droits, salon des dossiers, alerte anti-acharnement.

REPORTS_CHANNEL_NAME = "reports-smurf"
SUPPORT_URL = "https://support-valorant.riotgames.com/hc/fr/requests/new"
NAME_MAX, TAG_MAX = 16, 5

RiotId = app_commands.Range[str, 3, 30]
RIOT_ID_HELP = "Riot ID du joueur, format Nom#TAG"
RIOT_ID_OR_LINKED_HELP = "Riot ID du joueur, format Nom#TAG (vide : ton compte lié avec /link)"
INVALID_RIOT_ID = "❌ Riot ID invalide : utilise le format `Nom#TAG` (ex. `Pseudo#EUW`)."
MISSING_RIOT_ID = ("❌ Indique un Riot ID (format `Nom#TAG`), ou lie ton compte une fois avec `/link` "
                   "pour ne plus avoir à le saisir.")
MOD_ONLY = "❌ Cette commande est réservée aux modérateurs."
NO_REPORTS_CHANNEL = ("Le salon #reports-smurf n'existe pas encore : un administrateur peut le créer "
                      "avec `/report-setup`.")
PUBLIC_REPORTS_CHANNEL = ("Le salon des dossiers est visible de tous ; il doit rester réservé aux modérateurs. "
                          "Un administrateur doit le rendre privé (ou utiliser `/report-setup`).")

BOT_CHANNEL_PERMISSIONS = [("view_channel", "Voir le salon"), ("send_messages", "Envoyer des messages"),
                           ("embed_links", "Intégrer des liens")]
MOD_PERMISSIONS = ("administrator", "manage_guild", "manage_messages")
ADMIN_PERMISSIONS = ("administrator", "manage_guild")


# ================= RIOT ID =================
def parse_riot_id(text):
    # "Nom#TAG" -> ("Nom", "TAG") ; le dernier # sépare le tag
    name, sep, tag = (text or "").strip().rpartition("#")
    name, tag = name.strip(), tag.strip()
    if not sep or not 1 <= len(name) <= NAME_MAX or not 1 <= len(tag) <= TAG_MAX:
        return None
    return name, tag


def resolve_riot_id(user_id, text):
    # Renvoie ((nom, tag), None) ou (None, message d'erreur) ; rien de saisi -> compte lié avec /link
    if text is None or not str(text).strip():
        linked = db.get_linked_account(user_id)
        return (tuple(linked), None) if linked else (None, MISSING_RIOT_ID)
    parsed = parse_riot_id(text)
    return (parsed, None) if parsed else (None, INVALID_RIOT_ID)


def is_own_account(user_id, account):
    linked = db.get_linked_account(user_id)
    if not linked:
        return False
    return (linked[0].casefold(), linked[1].casefold()) == (account.name.casefold(), account.tag.casefold())


def tracker_url(name, tag):
    return f"https://tracker.gg/valorant/profile/riot/{quote(name, safe='')}%23{quote(tag, safe='')}/overview"


# ================= INFORMATIONS PERSONNELLES =================
REDACTED = "[retiré]"
PERSONAL_PATTERNS = [   # dans cet ordre : un e-mail ne doit pas être pris pour un pseudo « @… »
    re.compile(r"(?i)\b(?:https?://|www\.)\S+"),                               # liens
    re.compile(r"(?i)\b(?:discord\.gg|discord(?:app)?\.com/invite)/\S+"),       # invitations Discord
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),                                # e-mails
    re.compile(r"<@[!&]?\d+>"),                                                 # mentions Discord
    re.compile(r"(?<![\w<])@[\w.]{2,}"),                                        # pseudos de réseaux sociaux
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),                                 # adresses IP
    re.compile(r"\+\d[\d\s.-]{7,}\d"),                                          # téléphones internationaux
    re.compile(r"\b0[1-9](?:[\s.-]?\d{2}){4}\b"),                               # téléphones français
    re.compile(r"\b\d{10,}\b"),                                                 # longs numéros (téléphone, ID)
]


def redact_personal(text):
    # Retire des observations ce qui identifie quelqu'un hors du jeu ; renvoie (texte, quelque chose a été retiré)
    text = text or ""
    cleaned = text
    for pattern in PERSONAL_PATTERNS:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned, cleaned != text


# ================= AUTOCOMPLÉTION =================
async def riot_id_autocomplete(interaction, current):
    # Compte lié du membre d'abord (⭐), puis les joueurs déjà analysés (ceux de ce serveur en premier)
    try:
        text = (current or "").strip().casefold()
        choices, seen = [], set()
        linked = db.get_linked_account(interaction.user.id)
        if linked:
            value = f"{linked[0]}#{linked[1]}"
            if text in value.casefold():
                choices.append(app_commands.Choice(name=embeds.clip(f"⭐ {value} (ton compte)", 100), value=value[:100]))
                seen.add(value.casefold())
        for name, tag, rank, _ in db.search_players(interaction.guild_id, text, limit=25):
            value = f"{name}#{tag}"
            if value.casefold() in seen:
                continue
            seen.add(value.casefold())
            label = f"{value} · {embeds.display_rank(rank)}" if rank else value
            choices.append(app_commands.Choice(name=embeds.clip(label, 100), value=value[:100]))
        return choices[:25]
    except Exception:
        log.exception("Autocomplétion du Riot ID impossible")
        return []


async def watch_autocomplete(interaction, current):
    try:
        text = (current or "").strip().casefold()
        choices = []
        for watch in db.get_watches(interaction.guild_id):
            value = f"{watch['name']}#{watch['tag']}"
            if text in value.casefold():
                choices.append(app_commands.Choice(name=value[:100], value=value[:100]))
        return choices[:25]
    except Exception:
        log.exception("Autocomplétion des suivis impossible")
        return []


# ================= DROITS =================
def _has_any(interaction, names):
    for perms in (getattr(interaction, "permissions", None), getattr(interaction.user, "guild_permissions", None)):
        if isinstance(perms, discord.Permissions) and any(getattr(perms, name) for name in names):
            return True
    return False


def is_moderator(interaction):
    return _has_any(interaction, MOD_PERMISSIONS)


def can_manage_guild(interaction):
    return _has_any(interaction, ADMIN_PERMISSIONS)


# ================= SALON DES DOSSIERS =================
def find_reports_channel(guild):
    # Salon enregistré avec /report-setup s'il existe encore, sinon un salon nommé reports-smurf
    if guild is None:
        return None
    channel_id = db.get_reports_channel_id(guild.id)
    if channel_id:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
    return discord.utils.find(lambda c: c.name.casefold() == REPORTS_CHANNEL_NAME, guild.text_channels)


def everyone_can_view(channel):
    return channel.permissions_for(channel.guild.default_role).view_channel


def bot_missing_permissions(channel):
    perms = channel.permissions_for(channel.guild.me)
    return [label for attr, label in BOT_CHANNEL_PERMISSIONS if not getattr(perms, attr)]


def missing_permissions_text(channel, missing):
    return (f"❌ Je ne peux pas publier dans {channel.mention} : il me manque "
            f"{', '.join(f'**{m}**' for m in missing)}. Un administrateur peut corriger les permissions du salon.")


def reports_channel_problem(channel):
    # Message expliquant pourquoi ce salon ne peut pas recevoir de dossier, ou None s'il convient
    if channel is None:
        return NO_REPORTS_CHANNEL
    if everyone_can_view(channel):
        return PUBLIC_REPORTS_CHANNEL
    missing = bot_missing_permissions(channel)
    if missing:
        return missing_permissions_text(channel, missing)
    return None


# ================= RÉPONSES =================
async def private_reply(interaction, content=None, *, embed=None, public_defer=False):
    # Réponse visible du seul membre, même après un defer() public (le message « réfléchit… » est retiré)
    kwargs = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if not interaction.response.is_done():
        await interaction.response.send_message(**kwargs)
        return
    if public_defer:
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
    await interaction.followup.send(**kwargs)


async def reply_error(interaction, error, *, public_defer=False):
    # Même message que le gestionnaire global (core.errors), mais toujours en privé
    message = error_message(interaction, error)
    try:
        await private_reply(interaction, message, public_defer=public_defer)
    except discord.HTTPException as e:
        log.warning("Impossible d'afficher une erreur au membre : %s", e)


# ================= ANALYSES =================
def store_analysis(analysis, guild_id):
    # Enregistre une analyse neuve (jamais une copie du cache) et rattache le joueur au serveur
    try:
        if not analysis.from_cache:
            account, agg, suspicion = analysis.account, analysis.agg, analysis.suspicion
            db.save_analysis(
                account.puuid, account.name, account.tag, suspicion.index, embeds.current_rank(analysis.mmr),
                account.level, _round(agg.kd, 2), _round(agg.acs, 1), _round(agg.hs_pct, 1), _round(agg.adr, 1),
                _round(agg.winrate, 1), agg.n, suspicion.signals,
            )
        if guild_id is not None:
            db.record_guild_player(guild_id, analysis.account.puuid)
    except Exception:
        log.exception("Impossible d'enregistrer l'analyse de %s#%s", analysis.account.name, analysis.account.tag)


def _round(value, digits):
    return None if value is None else round(value, digits)


def record_check(interaction, account):
    # Consultation affichée : comptée pour la protection anti-acharnement (une erreur de base n'interrompt rien)
    try:
        db.log_activity(interaction.guild_id, interaction.user.id, account.puuid, "check")
    except Exception:
        log.exception("Impossible de journaliser la consultation de %s#%s", account.name, account.tag)


# ================= ANTI-ACHARNEMENT =================
def alert_channel(guild):
    # Salon où prévenir les modérateurs : seulement s'il est privé et utilisable
    channel = find_reports_channel(guild)
    if channel is None or reports_channel_problem(channel):
        return None
    return channel


async def send_harassment_alert(channel, member, account, reason, command, linked=False):
    try:
        await channel.send(embed=embeds.harassment_alert_embed(member, account, reason, command, linked=linked))
        return True
    except discord.HTTPException as e:
        log.warning("Alerte anti-acharnement non envoyée dans le salon %s : %s", channel.id, e)
        return False


async def check_guard(interaction, account, command):
    # None si la demande peut continuer, sinon le texte de refus (les modérateurs sont prévenus une fois par 24 h).
    # Aucune exemption pour le compte lié : /link ne prouve pas que le compte appartient au membre.
    user_id, guild_id = interaction.user.id, interaction.guild_id
    reason = guard.harassment_reason(user_id, account.puuid)
    if reason is None:
        return None
    log.info("Anti-acharnement : /%s refusée au membre %s sur %s (%s)", command, user_id, account.puuid, reason)
    channel = alert_channel(interaction.guild)
    notified = False
    if channel is not None:
        if guard.alert_sent_recently(user_id, account.puuid, guild_id):
            notified = True
        elif await send_harassment_alert(channel, interaction.user, account, reason, command,
                                         linked=is_own_account(user_id, account)):
            guard.record_alert(user_id, account.puuid, guild_id)   # seulement une fois l'alerte réellement partie
            notified = True
    return embeds.harassment_refusal(command, notified)
