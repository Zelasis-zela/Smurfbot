import discord
from discord.ext import commands
import os
import sys
import json
import asyncio
import hashlib
import logging
from logging.handlers import RotatingFileHandler

from core import db
from core.errors import on_app_command_error
from core.henrik import HenrikClient
from core.analyzer import Analyzer

log = logging.getLogger("smurfbot")

COGS_DIR = "cogs"
SYNC_HASH_FILE = ".commands-synced"   # empreinte des commandes déjà envoyées à Discord (gitignoré)

# ================= LOGS =================
def setup_logging():
    # Console + fichier logs/bot.log (5 fichiers de 1 Mo conservés), avec la date sur chaque ligne
    os.makedirs("logs", exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler("logs/bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    console_handler = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

# ================= CONFIGURATION =================
def load_env(path=".env"):
    # Charge les variables du fichier .env (sans écraser celles déjà définies)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

# ================= SYNCHRONISATION DES COMMANDES =================
def commands_hash(payload):
    # Empreinte stable de la liste des commandes (ordre des clés normalisé)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

def read_synced_hash(path=None):
    try:
        with open(path or SYNC_HASH_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None

def write_synced_hash(digest, path=None):
    with open(path or SYNC_HASH_FILE, "w", encoding="utf-8") as f:
        f.write(digest + "\n")

def needs_sync(digest, path=None, force=False):
    # Discord limite les synchronisations : on ne renvoie les commandes que si elles ont changé
    return force or read_synced_hash(path) != digest

def sync_forced():
    return os.getenv("SYNC_COMMANDS", "").strip() == "1"

# ================= BOT =================
class SmurfBot(commands.Bot):
    def __init__(self, api_key):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.api_key = api_key
        self.henrik = None
        self.analyzer = None
        self.failed_cogs = []

    async def setup_hook(self):
        db.init_db()
        self.henrik = HenrikClient(self.api_key)
        self.analyzer = Analyzer(self.henrik)
        self.tree.on_error = on_app_command_error

        # Chargement automatique de tous les Cogs du dossier 'cogs'
        for filename in sorted(os.listdir(COGS_DIR)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            cog_name = f"{COGS_DIR}.{filename[:-3]}"
            try:
                await self.load_extension(cog_name)
                log.info("📦 Cog chargé : %s", cog_name)
            except Exception:
                self.failed_cogs.append(cog_name)
                log.exception("❌ Erreur lors du chargement de %s", cog_name)

        await self.sync_commands()

    async def sync_commands(self):
        # Un cog en erreur : synchroniser ferait disparaître ses commandes sur tous les serveurs
        if self.failed_cogs:
            log.error("⚠️ Commandes slash non synchronisées : cog(s) en erreur (%s)", ", ".join(self.failed_cogs))
            return
        try:
            digest = commands_hash([c.to_dict(self.tree) for c in self.tree.get_commands()])
            if not needs_sync(digest, force=sync_forced()):
                log.info("⚡ Commandes slash inchangées : pas de synchronisation")
                return
            synced = await self.tree.sync()
            write_synced_hash(digest)
            log.info("⚡ %s commandes slash synchronisées", len(synced))
        except Exception:
            log.exception("❌ Échec de la synchronisation des commandes slash")

    async def on_ready(self):
        log.info("✅ Connecté en tant que %s (ID: %s)", self.user, self.user.id)
        log.info("🌐 Actif sur %s serveurs.", len(self.guilds))

    async def close(self):
        # D'abord les cogs (boucles de fond arrêtées), ensuite seulement le client HenrikDev
        try:
            await super().close()
        finally:
            if self.henrik is not None:
                try:
                    await self.henrik.close()
                except Exception:
                    log.exception("Erreur à la fermeture du client HenrikDev")

async def run(token, api_key):
    bot = SmurfBot(api_key)
    async with bot:
        await bot.start(token)

def main():
    # Les vérifications sont ici (et non à l'import) : les tests importent ce module sans .env
    setup_logging()
    load_env()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("❌ DISCORD_TOKEN manquant : ajoute-le dans le fichier .env")
    api_key = os.getenv("API_KEY")
    if not api_key:
        raise SystemExit("❌ API_KEY manquante : ajoute-la dans le fichier .env")
    try:
        asyncio.run(run(token, api_key))
    except KeyboardInterrupt:
        log.info("Arrêt du bot demandé.")

if __name__ == "__main__":
    main()
