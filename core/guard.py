from core import db

# Protection anti-acharnement : un membre qui cible toujours le même joueur est arrêté poliment
CHECK_WINDOW_HOURS = 24
CHECK_LIMIT = 6             # /check + /compare sur le même joueur par le même membre
REPORT_WINDOW_HOURS = 24 * 7
REPORT_LIMIT = 3            # tentatives de /report (acceptées ou refusées) sur le même joueur
ALERT_COOLDOWN_HOURS = 24   # une seule alerte aux modérateurs par membre et par joueur sur cette durée

CHECK_KINDS = ("check",)
REPORT_KINDS = ("report", "report_refused")

def harassment_reason(user_id, puuid):
    # Raison du blocage (texte pour l'alerte aux modérateurs), ou None si rien d'anormal
    checks = db.count_activity(user_id, puuid, CHECK_KINDS, CHECK_WINDOW_HOURS)
    if checks >= CHECK_LIMIT:
        return f"{checks} analyses de ce joueur en {CHECK_WINDOW_HOURS} h"
    attempts = db.count_activity(user_id, puuid, REPORT_KINDS, REPORT_WINDOW_HOURS)
    if attempts >= REPORT_LIMIT:
        return f"{attempts} tentatives de signalement de ce joueur en {REPORT_WINDOW_HOURS // 24} jours"
    return None

def alert_sent_recently(user_id, puuid, guild_id=None):
    # Modérateurs (de ce serveur si guild_id) déjà prévenus pour ce membre et ce joueur dans les 24 h
    return db.count_activity(user_id, puuid, ("alert",), ALERT_COOLDOWN_HOURS, guild_id=guild_id) > 0

def record_alert(user_id, puuid, guild_id=None):
    # À appeler une fois l'alerte réellement envoyée
    db.log_activity(guild_id, user_id, puuid, "alert")

def should_alert(user_id, puuid, guild_id=None):
    # True si les modérateurs doivent être prévenus ; l'alerte est alors enregistrée aussitôt
    if alert_sent_recently(user_id, puuid):
        return False
    record_alert(user_id, puuid, guild_id)
    return True
