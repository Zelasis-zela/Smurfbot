import discord
from discord.ext import commands
import os
import sys
import asyncio
import logging
from logging.handlers import RotatingFileHandler

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

setup_logging()
log = logging.getLogger("smurfbot")

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

load_env()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN manquant : ajoute-le dans le fichier .env")
# =================================================

class SmurfBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # Requis pour les logs
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Chargement automatique de tous les Cogs du dossier 'cogs'
        for filename in os.listdir("./cogs"):
            if filename.endswith(".py"):
                cog_name = f"cogs.{filename[:-3]}"
                try:
                    await self.load_extension(cog_name)
                    log.info("📦 Cog chargé : %s", cog_name)
                except Exception:
                    log.exception("❌ Erreur lors du chargement de %s", cog_name)

        # Synchronisation des commandes Slash auprès de Discord
        await self.tree.sync()
        log.info("⚡ Commandes Slash synchronisées !")

bot = SmurfBot()

@bot.event
async def on_ready():
    log.info("✅ Connecté en tant que %s (ID: %s)", bot.user, bot.user.id)
    log.info("🌐 Actif sur %s serveurs.", len(bot.guilds))

async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())