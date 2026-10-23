import math
import logging

import discord
from discord import app_commands

from core.henrik import RateLimited, NotFound, ApiError
from core.analyzer import PlayerNotFound, AnalysisIncomplete

log = logging.getLogger("smurfbot")

GENERIC_MESSAGE = "❌ Une erreur est survenue, réessaie dans un instant."
MOD_ONLY_MESSAGE = "❌ Cette commande est réservée aux modérateurs."
NOT_FOUND_MESSAGE = "❌ Joueur introuvable : vérifie le Riot ID (format `Nom#TAG`)."
API_MESSAGE = "❌ L'API Valorant n'a pas répondu correctement. Réessaie dans un instant."

def _seconds(value):
    try:
        return max(1, math.ceil(float(value)))
    except (TypeError, ValueError):
        return None

def _command_name(interaction):
    command = getattr(interaction, "command", None)
    return getattr(command, "qualified_name", None) or getattr(command, "name", None) or "?"

def error_message(interaction, error):
    # Message français affiché au membre pour une erreur de commande (déjà déballée)
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"⏳ Doucement ! Réessaie dans **{_seconds(error.retry_after) or 1} s**."
    if isinstance(error, app_commands.NoPrivateMessage):
        return "Cette commande s'utilise sur un serveur."
    if isinstance(error, app_commands.BotMissingPermissions):
        # Sous-classe de CheckFailure : c'est le bot, pas le membre, qui manque de droits
        return "❌ Il me manque des permissions dans ce salon pour exécuter cette commande."
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        return MOD_ONLY_MESSAGE
    if isinstance(error, RateLimited):
        seconds = _seconds(getattr(error, "retry_after", None))
        wait = f"dans **{seconds} s**" if seconds else "dans une minute"
        return f"⏳ L'API Valorant est très sollicitée. Réessaie {wait}."
    if isinstance(error, (PlayerNotFound, NotFound)):
        return NOT_FOUND_MESSAGE
    if isinstance(error, (AnalysisIncomplete, ApiError)):
        log.warning("Erreur de l'API pendant /%s : %s", _command_name(interaction), error)
        return API_MESSAGE
    log.error("Erreur dans la commande /%s", _command_name(interaction), exc_info=error)
    return GENERIC_MESSAGE

async def on_app_command_error(interaction, error):
    # Installé comme bot.tree.on_error : ne doit jamais lever d'exception
    try:
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original
        message = error_message(interaction, error)
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as e:
        log.warning("Impossible d'afficher l'erreur de /%s : %s", _command_name(interaction), e)
    except Exception:
        log.exception("Erreur dans le gestionnaire d'erreurs des commandes")
