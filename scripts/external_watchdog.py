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
    COLLECTOR_SILENT_ALERT_SEC,
)
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
                        help="Ignore l'anti-spam (test de la chaîne d'alerte)")
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

    if problems:
        print(f"[WATCHDOG] ⚠️ {problems}")
        if not state.get("unhealthy") or args.force_alert:
            notifier.error("🛰️ <b>WATCHDOG EXTERNE — collecte en panne</b>\n- "
                           + "\n- ".join(problems))
        _save_state({"unhealthy": True, "problems": problems, "ts": time.time()})
        sys.exit(1)
    else:
        print("[WATCHDOG] ✅ collecte OK")
        if state.get("unhealthy"):
            notifier.send("🛰️ ✅ <b>WATCHDOG EXTERNE — collecte rétablie</b>")
        _save_state({"unhealthy": False, "ts": time.time()})
        sys.exit(0)


if __name__ == "__main__":
    main()
