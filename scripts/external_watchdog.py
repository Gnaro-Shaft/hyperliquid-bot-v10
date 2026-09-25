"""
Watchdog EXTERNE (V10) — à lancer HORS du bot (cron sur homeserv01 ou autre
machine) : couvre le cas où le process bot/collector meurt entièrement, que le
HealthMonitor embarqué ne peut par définition pas signaler.

Lit les heartbeats `collector_health` + le doc `bot_status` dans MongoDB et
envoie une alerte Telegram si un flux est muet depuis plus de N secondes.
Anti-spam : un fichier d'état local mémorise le dernier état alerté (alerte sur
transition uniquement, comme le HealthMonitor).

Cron suggéré (toutes les 5 min) :
  */5 * * * * cd /path/V10 && ./venv/bin/python -m scripts.external_watchdog
"""

import argparse
import json
import os
import sys
import time

from pymongo import MongoClient

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_HEARTBEATS, MONGO_COLLECTION_BOT_STATUS,
    COLLECTOR_SILENT_ALERT_SEC, HL_WALLET_ADDRESS, AGENT_EXPIRY_ALERT_DAYS,
    BACKUP_MARKER, BACKUP_MAX_AGE_HOURS,
)
from utils import http
from collector.heartbeat import read_heartbeats, stale_heartbeats
from utils.notifier import Notifier

STATE_FILE = os.path.join(os.path.dirname(__file__), ".watchdog_state.json")


def check(db, max_age_s, bot_status_max_age_s=600):
    """Retourne la liste des problèmes (vide = OK)."""
    now_ms = int(time.time() * 1000)
    problems = []

    stale = stale_heartbeats(read_heartbeats(db, MONGO_COLLECTION_HEARTBEATS),
                             now_ms, max_age_s)
    for s in stale:
        problems.append(f"{s['component']}[{s['coin']}] muet depuis {int(s['age_s'])}s")

    status = db[MONGO_COLLECTION_BOT_STATUS].find_one({"_id": "current"})
    if status is None:
        problems.append("bot_status absent (bot jamais démarré ?)")
    else:
        age_s = (now_ms - int(status.get("timestamp", 0))) / 1000
        if age_s > bot_status_max_age_s:
            problems.append(f"bot_status périmé ({int(age_s)}s) — process bot mort ?")

    return problems


API_INFO_URL = "https://api.hyperliquid.xyz/info"


def agents_expirants(agents, now_ms, seuil_jours=AGENT_EXPIRY_ALERT_DAYS):
    """Agents Hyperliquid dont l'approbation expire bientôt (ou a expiré).

    Un API wallet expiré ne peut plus signer d'ordre : le bot continue de
    collecter et d'évaluer, mais ses entrées sont rejetées par l'exchange —
    une panne parfaitement silencieuse pour les heartbeats.
    """
    problemes = []
    for agent in agents or []:
        valid_until = agent.get("validUntil")
        if valid_until is None:
            continue
        restant_j = (int(valid_until) - now_ms) / 86_400_000
        nom = agent.get("name") or "sans nom"
        if restant_j <= 0:
            problemes.append(f"agent Hyperliquid « {nom} » EXPIRÉ depuis {abs(restant_j):.0f} j")
        elif restant_j <= seuil_jours:
            problemes.append(f"agent Hyperliquid « {nom} » expire dans {restant_j:.0f} j")
    return problemes


def check_agents(now_ms, session=None):
    """Interroge l'endpoint PUBLIC extraAgents. Ne signe rien, n'utilise aucune
    clé privée — seule l'adresse publique du compte maître est nécessaire.

    Une panne réseau ne doit pas réveiller qui que ce soit : elle est
    journalisée et ignorée. Le watchdog surveille la collecte, pas Hyperliquid.
    """
    if not HL_WALLET_ADDRESS:
        return []
    try:
        session = session or http.creer_session()
        resp = http.demander(session, "POST", API_INFO_URL,
                             json={"type": "extraAgents", "user": HL_WALLET_ADDRESS},
                             timeout=10, tentatives=2)
        return agents_expirants(resp.json(), now_ms)
    except Exception as e:
        print(f"[WATCHDOG] contrôle des agents indisponible ({e}) — ignoré")
        return []


def sauvegarde_perimee(marqueur_epoch, now_ms, seuil_h=BACKUP_MAX_AGE_HOURS):
    """La sauvegarde de l'archive est-elle trop ancienne ?

    Elle tourne une fois par nuit ; au-delà de deux passages manqués, quelque
    chose ne va pas. Une sauvegarde qui cesse est parfaitement silencieuse :
    la collecte continue, les heartbeats battent, et l'unique copie hors du VPS
    vieillit sans que personne ne le sache.
    """
    age_h = (now_ms / 1000 - marqueur_epoch) / 3600
    if age_h < -5 / 60:
        # Une horloge fausse rendrait le contrôle muet pour toujours.
        return "marqueur de sauvegarde daté dans le futur — horloge fausse ?"
    if age_h > seuil_h:
        return f"sauvegarde de l'archive vieille de {age_h:.0f} h (seuil {seuil_h} h)"
    return None


def check_sauvegarde(now_ms, chemin=None):
    """Lit le marqueur horodaté écrit par v10-archive-pull sur cette machine.

    Un marqueur qu'on ne peut pas lire est une ALERTE. Jusqu'au 25/09/2026 il
    était « journalisé et ignoré » : homeserv02, qui portait la sauvegarde, est
    mort le 16/09 et le watchdog a écrit « ignoré » 2 444 fois en neuf jours —
    l'archive n'avait plus aucune copie et personne ne l'a su. Un contrôle qui
    ne peut pas conclure ne conclut pas que tout va bien.

    Le message ne porte que le type d'erreur, jamais un détail variable :
    l'anti-spam compare les contenus, un texte changeant réalerterait à chaque
    passage.
    """
    chemin = BACKUP_MARKER if chemin is None else chemin
    if not chemin:
        print("[WATCHDOG] contrôle de la sauvegarde désactivé (BACKUP_MARKER vide)")
        return []
    try:
        with open(chemin) as f:
            brut = f.read().strip()
        # int() accepte les chiffres de toute écriture Unicode : « １７９… »
        # passerait pour une date. `date +%s` n'écrit que de l'ASCII.
        if not (brut.isascii() and brut.isdigit()):
            raise ValueError("pas un entier ASCII")
        marqueur = int(brut)
    except Exception as e:
        return [f"sauvegarde de l'archive incontrôlable : marqueur illisible "
                f"({type(e).__name__})"]
    probleme = sauvegarde_perimee(marqueur, now_ms)
    return [probleme] if probleme else []


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"unhealthy": False}


def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[WATCHDOG] state: {e}")


def main():
    parser = argparse.ArgumentParser(description="Watchdog externe V10")
    parser.add_argument("--max-age", type=int, default=COLLECTOR_SILENT_ALERT_SEC,
                        help="Âge max (s) d'un heartbeat avant alerte")
    parser.add_argument("--force-alert", action="store_true",
                        help="Envoie un Telegram même si tout va bien "
                             "(test bout-en-bout de la chaîne d'alerte)")
    args = parser.parse_args()

    if not MONGO_URL:
        print("MONGO_URL manquant"); sys.exit(2)

    notifier = Notifier()
    state = _load_state()
    try:
        db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]
        db.command("ping")
        problems = check(db, args.max_age)
    except Exception as e:
        problems = [f"MongoDB injoignable: {e}"]
        db = None

    maintenant_ms = int(time.time() * 1000)
    problems += check_agents(maintenant_ms)
    problems += check_sauvegarde(maintenant_ms)

    if problems:
        print(f"[WATCHDOG] ⚠️ {problems}")
        # Anti-spam sur le CONTENU, pas sur un simple booléen : un problème
        # durable (agent qui expire dans 14 jours) maintiendrait sinon l'état
        # « unhealthy » et étoufferait l'alerte d'une panne survenant après.
        nouveaux = [p for p in problems if p not in set(state.get("problems", []))]
        if nouveaux or args.force_alert:
            notifier.error("🛰️ <b>WATCHDOG EXTERNE — collecte en panne</b>\n- "
                           + "\n- ".join(problems))
        _save_state({"unhealthy": True, "problems": problems, "ts": time.time()})
        sys.exit(1)
    else:
        print("[WATCHDOG] ✅ collecte OK")
        if state.get("unhealthy"):
            notifier.send("🛰️ ✅ <b>WATCHDOG EXTERNE — collecte rétablie</b>")
        elif args.force_alert:
            # Test bout-en-bout : machine locale → Mongo → Telegram
            notifier.send("🛰️ 🧪 <b>WATCHDOG EXTERNE — test de la chaîne d'alerte</b>\n"
                          "Tout est vert : heartbeats frais, bot_status à jour. "
                          "Ce message confirme que le chemin externe fonctionne.")
        _save_state({"unhealthy": False, "ts": time.time()})
        sys.exit(0)


if __name__ == "__main__":
    main()
