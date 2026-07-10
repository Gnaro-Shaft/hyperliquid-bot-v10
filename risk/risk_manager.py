import time
import os
from datetime import datetime, timezone

from pymongo import MongoClient
from config import (
    MAX_CONSECUTIVE_LOSSES,
    PAUSE_DURATION_MINUTES,
    MAX_DAILY_DRAWDOWN_PCT,
    KILL_SWITCH_FILE,
    MONGO_URL, MONGO_DB,
    DEBUG,
)

RISK_COLLECTION = "risk_state"


class RiskManager:
    def __init__(self):
        self.consecutive_losses = 0
        self.pause_until = 0  # timestamp
        self.last_trade_time = 0
        self.daily_start_balance = None
        self.daily_date = None
        self.total_pnl_today = 0.0

        # MongoDB pour persister l'etat
        self.mongo = None
        if MONGO_URL:
            try:
                client = MongoClient(MONGO_URL)
                self.mongo = client[MONGO_DB]
                self._load_state()
            except Exception as e:
                print(f"[RISK] MongoDB erreur: {e}")

    def _load_state(self):
        """Charge l'etat depuis MongoDB au demarrage."""
        if self.mongo is None:
            return
        try:
            state = self.mongo[RISK_COLLECTION].find_one({"_id": "current"})
            if state:
                saved_date = state.get("daily_date", "")
                today = datetime.now(timezone.utc).date().isoformat()
                if saved_date == today:
                    # Meme jour → restaurer l'etat
                    self.total_pnl_today = state.get("total_pnl_today", 0.0)
                    self.daily_start_balance = state.get("daily_start_balance")
                    self.consecutive_losses = state.get("consecutive_losses", 0)
                    self.last_trade_time = state.get("last_trade_time", 0)
                    self.daily_date = datetime.now(timezone.utc).date()
                    print(f"[RISK] Etat restaure: PnL={self.total_pnl_today:+.2f}, "
                          f"losses={self.consecutive_losses}, balance_init={self.daily_start_balance}")
                else:
                    print(f"[RISK] Nouveau jour, etat precedent ignore.")
        except Exception as e:
            print(f"[RISK] Erreur chargement etat: {e}")

    def _save_state(self):
        """Persiste l'etat dans MongoDB."""
        if self.mongo is None:
            return
        try:
            self.mongo[RISK_COLLECTION].update_one(
                {"_id": "current"},
                {"$set": {
                    "daily_date": self.daily_date.isoformat() if self.daily_date else "",
                    "daily_start_balance": self.daily_start_balance,
                    "total_pnl_today": self.total_pnl_today,
                    "consecutive_losses": self.consecutive_losses,
                    "last_trade_time": self.last_trade_time,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True
            )
        except Exception as e:
            print(f"[RISK] Erreur sauvegarde etat: {e}")

    def reset_daily(self, balance):
        """Appeler au debut de chaque journee ou au demarrage."""
        today = datetime.now(timezone.utc).date()
        if self.daily_date != today:
            self.daily_date = today
            self.daily_start_balance = balance
            self.total_pnl_today = 0.0
            self.consecutive_losses = 0
            self._save_state()
            if DEBUG:
                print(f"[RISK] Reset journalier. Solde initial : {balance:.2f}")

    def register_trade_result(self, pnl):
        """Enregistre le resultat d'un trade pour le suivi."""
        self.total_pnl_today += pnl
        self.last_trade_time = time.time()

        if pnl < 0:
            self.consecutive_losses += 1
            if DEBUG:
                print(f"[RISK] Perte #{self.consecutive_losses} consecutive. PnL jour: {self.total_pnl_today:.2f}")
        else:
            self.consecutive_losses = 0
            if DEBUG:
                print(f"[RISK] Trade gagnant. Serie pertes reset. PnL jour: {self.total_pnl_today:.2f}")

        # Pause automatique apres N pertes consecutives
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.pause_until = time.time() + PAUSE_DURATION_MINUTES * 60
            if DEBUG:
                print(f"[RISK] PAUSE {PAUSE_DURATION_MINUTES}min apres {MAX_CONSECUTIVE_LOSSES} pertes consecutives")

        # Persister apres chaque trade
        self._save_state()

    def can_trade(self, current_balance=None):
        """Verifie si le bot est autorise a trader. Retourne (bool, raison)."""

        # Kill switch
        if os.path.exists(KILL_SWITCH_FILE):
            return False, "KILL SWITCH actif (fichier KILL detecte)"

        # Pause apres pertes consecutives
        now = time.time()
        if now < self.pause_until:
            remaining = int((self.pause_until - now) / 60)
            return False, f"Pause anti-overtrading ({remaining}min restantes)"

        # Drawdown max journalier
        if current_balance and self.daily_start_balance:
            drawdown = (self.daily_start_balance - current_balance) / self.daily_start_balance
            if drawdown >= MAX_DAILY_DRAWDOWN_PCT:
                return False, f"Drawdown journalier max atteint ({drawdown*100:.1f}% >= {MAX_DAILY_DRAWDOWN_PCT*100:.1f}%)"

        return True, "OK"

    def status(self):
        """Retourne un dict avec l'etat courant du risk manager."""
        return {
            "consecutive_losses": self.consecutive_losses,
            "paused": time.time() < self.pause_until,
            "pause_remaining_min": max(0, int((self.pause_until - time.time()) / 60)),
            "pnl_today": round(self.total_pnl_today, 2),
            "daily_start_balance": self.daily_start_balance,
            "kill_switch": os.path.exists(KILL_SWITCH_FILE),
        }


if __name__ == "__main__":
    rm = RiskManager()
    rm.reset_daily(1000.0)

    # Simule 3 pertes
    for i in range(3):
        rm.register_trade_result(-10)
        ok, reason = rm.can_trade(current_balance=1000 - (i + 1) * 10)
        print(f"Trade #{i+1}: can_trade={ok}, reason={reason}")

    print("Status:", rm.status())
