"""
Healthcheck V10 — daemon thread qui surveille la santé du bot ET des collectors,
et alerte sur Telegram en cas de problème (sur transition, anti-spam).

Nouveauté V10 (garde-fou anti-trous de collecte) :
  - lit les heartbeats `collector_health` (un par composant × coin) et alerte
    « collector muet > 5 min » PAR FLUX — un coin mort ou un collecteur REST
    silencieux ne passe plus inaperçu derrière les autres.

Un watchdog EXTERNE complémentaire (scripts/external_watchdog.py) couvre le cas
où ce process meurt entièrement — à lancer en cron hors du bot (homeserv01).
"""

import os
import time
import traceback
from datetime import datetime, timezone

from pymongo import MongoClient

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_1M, MONGO_COLLECTION_15M,
    MONGO_COLLECTION_BOT_STATUS, MONGO_COLLECTION_HEARTBEATS, KILL_SWITCH_FILE,
    HEALTH_CHECK_INTERVAL_SEC, HEALTH_MAX_1M_AGE_SEC,
    HEALTH_MAX_15M_AGE_SEC, HEALTH_MAX_CONSEC_ERRORS,
    COLLECTOR_SILENT_ALERT_SEC,
)
from collector.heartbeat import read_heartbeats, stale_heartbeats
from utils.observability import build_bot_status


def evaluate_health(metrics: dict, thresholds: dict) -> list:
    """Retourne la liste des problèmes détectés (vide = tout va bien). PURE.

    metrics : {ws_alive, mongo_ok, last_1m_age_s, last_15m_age_s, balance,
               consec_errors, stale_streams}
    thresholds : {max_1m_age_s, max_15m_age_s, max_consec_errors}
    """
    problems = []

    if not metrics.get("mongo_ok", True):
        problems.append("MongoDB injoignable")

    if not metrics.get("ws_alive", True):
        problems.append("WebSocket inactif (collector muet)")

    age1 = metrics.get("last_1m_age_s")
    if age1 is not None and age1 > thresholds["max_1m_age_s"]:
        problems.append(f"Bougie 1m périmée ({int(age1)}s)")

    age15 = metrics.get("last_15m_age_s")
    if age15 is not None and age15 > thresholds["max_15m_age_s"]:
        problems.append(f"Bougie 15m périmée ({int(age15)}s)")

    if metrics.get("balance") is None:
        problems.append("Solde inaccessible (fetch_balance KO)")

    ce = metrics.get("consec_errors", 0)
    if ce > thresholds["max_consec_errors"]:
        problems.append(f"Trop d'erreurs consécutives ({ce})")

    # V10 : flux de collecte muets (par composant × coin)
    for s in metrics.get("stale_streams") or []:
        problems.append(
            f"Collecteur muet: {s['component']}[{s['coin']}] ({int(s['age_s'])}s)")

    return problems


class HealthMonitor:
    """Surveillance autonome de la santé du bot (daemon thread)."""

    def __init__(self, bot, notifier=None):
        self.bot = bot
        self.notifier = notifier
        self._client = None
        self._unhealthy = False          # état précédent (alerte sur transition)
        self.thresholds = {
            "max_1m_age_s": HEALTH_MAX_1M_AGE_SEC,
            "max_15m_age_s": HEALTH_MAX_15M_AGE_SEC,
            "max_consec_errors": HEALTH_MAX_CONSEC_ERRORS,
        }

    def _db(self):
        if self._client is None:
            self._client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        return self._client[MONGO_DB]

    def _last_age_s(self, db, collection) -> float:
        """Âge (s) de la bougie la plus récente, ou None si introuvable."""
        doc = db[collection].find_one(sort=[("timestamp", -1)])
        if not doc or "timestamp" not in doc:
            return None
        return (time.time() * 1000 - float(doc["timestamp"])) / 1000.0

    def collect_metrics(self) -> dict:
        m = {"ws_alive": True, "mongo_ok": True,
             "last_1m_age_s": None, "last_15m_age_s": None,
             "balance": 0.0, "consec_errors": 0, "stale_streams": []}

        # WebSocket / collector
        try:
            m["ws_alive"] = bool(getattr(self.bot.collector, "is_alive", True))
        except Exception:
            m["ws_alive"] = False

        # MongoDB + fraîcheur des bougies + heartbeats des collectors (V10)
        try:
            db = self._db()
            db.command("ping")
            m["last_1m_age_s"] = self._last_age_s(db, MONGO_COLLECTION_1M)
            m["last_15m_age_s"] = self._last_age_s(db, MONGO_COLLECTION_15M)
            heartbeats = read_heartbeats(db, MONGO_COLLECTION_HEARTBEATS)
            m["stale_streams"] = stale_heartbeats(
                heartbeats, int(time.time() * 1000), COLLECTOR_SILENT_ALERT_SEC)
        except Exception:
            m["mongo_ok"] = False

        # Solde
        try:
            bal = self.bot.trader._get_total_balance()
            m["balance"] = bal if bal is not None else None
        except Exception:
            m["balance"] = None

        # Erreurs consécutives
        m["consec_errors"] = int(getattr(self.bot, "_err_count", 0) or 0)
        return m

    def _write_bot_status(self, metrics):
        """Écrit le doc heartbeat `bot_status` (_id='current') pour le dashboard."""
        try:
            risk_status = self.bot.risk.status()
            kill = os.path.exists(KILL_SWITCH_FILE)
            doc = build_bot_status(
                metrics, risk_status, self.bot.positions, kill,
                int(time.time() * 1000),
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._db()[MONGO_COLLECTION_BOT_STATUS].replace_one(
                {"_id": "current"}, doc, upsert=True
            )
        except Exception as e:
            print(f"[Health] Erreur écriture bot_status: {e}")

    def _notify(self, msg, error=False):
        if self.notifier is None:
            print(f"[Health] (no notifier) {msg}")
            return
        try:
            self.notifier.error(msg) if error else self.notifier.send(msg)
        except Exception as e:
            print(f"[Health] Erreur notification: {e}")

    def run_loop(self):
        print(f"[Health] Démarré — vérification toutes les {HEALTH_CHECK_INTERVAL_SEC}s "
              f"(alerte collecteur muet > {COLLECTOR_SILENT_ALERT_SEC}s)")
        time.sleep(45)  # laisser le bot se stabiliser
        while True:
            try:
                metrics = self.collect_metrics()
                self._write_bot_status(metrics)          # heartbeat pour le dashboard
                problems = evaluate_health(metrics, self.thresholds)
                if problems and not self._unhealthy:
                    self._unhealthy = True
                    self._notify(
                        "🚑 <b>HEALTHCHECK — problème détecté</b>\n- " + "\n- ".join(problems),
                        error=True,
                    )
                    print(f"[Health] ⚠️ Problèmes: {problems}")
                elif not problems and self._unhealthy:
                    self._unhealthy = False
                    self._notify("✅ <b>HEALTHCHECK — tout est rentré dans l'ordre</b>")
                    print("[Health] ✅ Rétabli")
            except Exception as e:
                print(f"[Health] Erreur cycle: {e}")
                print(traceback.format_exc())
            time.sleep(HEALTH_CHECK_INTERVAL_SEC)
