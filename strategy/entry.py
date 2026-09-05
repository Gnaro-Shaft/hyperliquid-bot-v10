"""
Décision d'entrée : confirmation, garde-fous, pullback, passage d'ordre.

Extrait de main.py, où ces 227 lignes cohabitaient avec le démarrage, la boucle
et la gestion des positions ouvertes. Entrer en position a sa propre raison de
changer — et son propre état : les séries de signaux confirmés et opposés
appartiennent à ce module, pas au bot.

Restent partagés avec TradingBot les dictionnaires que la fermeture de position
touche aussi (cooldowns, dates de dernier trade) et les scores lus par le filtre
de corrélation. Ils sont modifiés en place des deux côtés.

Les décisions arithmétiques vivent dans entry_rules.py, où elles sont testées.
"""

import time

from config import (
    DEBUG, SIGNAL_CONFIRM_COUNT, PULLBACK_PCT, PULLBACK_EXPIRY_SEC,
    TP_PCT, SL_PCT, TRAIL_PCT, TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
    MAX_DAILY_DRAWDOWN_PCT,
)
from datalog.decision_log import log_decision
from risk import guards
from strategy import entry_rules as regles
from utils.sizing import size_factor


class EntryManager:
    """Décide et exécute les entrées en position pour le compte du bot."""

    def __init__(self, trader, risk, notifier, positions, cooldowns,
                 last_trade_times, last_signal_scores, coins):
        self.trader = trader
        self.risk = risk
        self.notifier = notifier
        self.positions = positions
        self.cooldowns = cooldowns
        self.last_trade_times = last_trade_times
        self.last_signal_scores = last_signal_scores
        # État propre à la décision d'entrée
        self.signal_streaks = {c: 0 for c in coins}
        self.signal_dirs = {c: 0 for c in coins}
        self.reverse_streaks = {c: 0 for c in coins}

    def should_reverse(self, coin, sig):
        """Ferme la position seulement après SIGNAL_CONFIRM_COUNT signaux opposés consécutifs."""
        pos = self.positions[coin]
        if not pos["active"]:
            self.reverse_streaks[coin] = 0
            return False

        self.reverse_streaks[coin] = regles.maj_reverse_streak(
            pos["active"], pos["side"], sig["score"], self.reverse_streaks[coin])
        if self.reverse_streaks[coin] == 0:
            return False

        if regles.inversion_confirmee(self.reverse_streaks[coin], SIGNAL_CONFIRM_COUNT):
            self.reverse_streaks[coin] = 0
            return True

        if DEBUG:
            print(f"  [REVERSE][{coin}] Signal opposé {self.reverse_streaks[coin]}/{SIGNAL_CONFIRM_COUNT}")
        return False

    def try_open(self, coin, sig, price):
        """Tente d'ouvrir une position si le signal est fort ET confirmé."""
        if self.positions[coin].get("pending_entry"):
            return

        if not regles.signal_est_fort(sig["score"]):
            self.signal_streaks[coin] = 0
            self.signal_dirs[coin] = 0
            return

        self.signal_streaks[coin], self.signal_dirs[coin] = regles.maj_streak(
            sig["score"], self.signal_dirs[coin], self.signal_streaks[coin])

        if not regles.est_confirme(self.signal_streaks[coin], SIGNAL_CONFIRM_COUNT):
            if DEBUG:
                print(f"  [CONFIRM][{coin}] Signal fort {sig['score']} "
                      f"({self.signal_streaks[coin]}/{SIGNAL_CONFIRM_COUNT})")
            return

        # Cooldown dynamique
        restant = regles.cooldown_restant(
            time.time(), self.last_trade_times[coin], self.cooldowns[coin])
        if restant > 0:
            cooldown = self.cooldowns[coin]
            remaining = int(restant)
            if DEBUG:
                print(f"  [COOLDOWN][{coin}] {remaining}s restantes (cooldown={cooldown:.0f}s)")
            return

        # Reset streaks
        self.signal_streaks[coin] = 0
        self.reverse_streaks[coin] = 0

        self.notifier.signal_alert(
            coin, sig["score"], sig["raw_score"],
            sig["label"], sig["color"], price, sig["debug"]
        )

        side = regles.side_depuis_score(sig["score"])

        # Risk manager
        balance = self.trader._get_total_balance()
        can_trade, reason = self.risk.can_trade(current_balance=balance)
        if not can_trade:
            print(f"[BOT][{coin}] Trading bloqué: {reason}")
            self.notifier.risk_alert(reason)
            log_decision(coin, sig, side, "refused", f"risk: {reason}", price)
            return

        # ── Circuit breaker marché ──
        tripped, cb_reasons = guards.market_breaker(sig)
        if tripped:
            joined = ", ".join(cb_reasons)
            print(f"[BOT][{coin}] 🚧 Circuit breaker marché — entrée bloquée : {joined}")
            self.notifier.risk_alert(f"Circuit breaker [{coin}] : {joined}")
            log_decision(coin, sig, side, "refused", f"circuit_breaker: {joined}", price)
            return

        # ── Filtre corrélation ──
        blocked, corr_boost = guards.correlation_filter(
            coin, side, self.positions, self.last_signal_scores)
        if blocked:
            print(f"[BOT][{coin}] ⚡ Bloqué — conflit corrélation avec paire sœur")
            log_decision(coin, sig, side, "refused", "correlation", price)
            return
        if corr_boost > 0 and DEBUG:
            print(f"  [CORR][{coin}] Signal corroboré par paire sœur → size_boost +{corr_boost*100:.0f}%")

        size_factor = regles.taille_avec_boost(self._size_factor(sig), corr_boost)

        # ── Garde-fou exposition globale ──
        allowed, exp_reason = guards.exposure_guard(
            coin, side, size_factor, balance, self.positions)
        if not allowed:
            print(f"[BOT][{coin}] 🛡️ Exposition — entrée bloquée : {exp_reason}")
            self.notifier.risk_alert(f"Exposition [{coin}] : {exp_reason}")
            log_decision(coin, sig, side, "refused", f"exposure: {exp_reason}", price)
            return

        # ── Pullback entry — arrondi coin-agnostique (V10) ──
        target = regles.cible_pullback(side, price, PULLBACK_PCT)

        self.positions[coin]["pending_entry"] = {
            "direction": side,
            "target_price": target,
            "expiry_ts": time.time() + PULLBACK_EXPIRY_SEC,
            "sig": sig,
            "size_factor": size_factor,
            "score": sig["score"],
        }
        print(f"[BOT][{coin}] ⏳ En attente pullback @ {target:.6g} "
              f"(actuel={price:.6g}, expiry={PULLBACK_EXPIRY_SEC}s)")
        log_decision(coin, sig, side, "accepted", "ok", price, size_factor)

    def check_pending(self, coin, live_price):
        """Déclenche l'entrée quand le pullback est atteint ou expiré."""
        pos = self.positions[coin]
        pending = pos.get("pending_entry")
        if not pending:
            return

        direction = pending["direction"]
        target = pending["target_price"]
        expiry = pending["expiry_ts"]
        sig = pending["sig"]
        size_factor = pending["size_factor"]

        pullback_hit = regles.pullback_atteint(direction, live_price, target)
        expired = time.time() > expiry

        if pullback_hit:
            print(f"[BOT][{coin}] 🎯 Pullback @ {live_price:.6g} (target={target:.6g}) — entrée!")
        elif expired:
            print(f"[BOT][{coin}] ⏰ Pullback expiré — entrée au marché @ {live_price:.6g}")
        else:
            return  # Pas encore

        pos["pending_entry"] = None
        self.execute(coin, sig, live_price, size_factor)

    def execute(self, coin, sig, price, size_factor=1.0):
        """Place réellement l'ordre d'entrée et met à jour l'état de la position."""
        side = regles.side_depuis_score(sig["score"])
        tp = sig.get("dynamic_tp") or TP_PCT
        sl = sig.get("dynamic_sl") or SL_PCT

        # Lien trade ↔ signal (V10) : id + snapshot des features à l'entrée
        entry_context = {
            "coin": coin,
            "signal_id": sig.get("signal_id"),
            "signal_score": sig.get("score"),
            "raw_score": sig.get("raw_score"),
            "regime": sig.get("regime"),
            "entry_features": sig.get("debug"),
        }

        result = self.trader.place_order_with_tp_sl(
            side, price, tp_pct=tp, sl_pct=sl, size_factor=size_factor,
            context=entry_context)
        if result:
            self.last_trade_times[coin] = time.time()
            atr_pct = sig["debug"].get("atr_pct", 0.001)
            # Planchers alignés sur config : le trade doit être bien en profit avant de protéger
            params = regles.parametres_trailing(
                atr_pct, TRAIL_PCT, TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT)
            trail_distance = params["trail_distance"]
            trail_trigger = params["trail_trigger"]
            trail_step = params["trail_step"]

            self.positions[coin].update({
                "active": True,
                "entry": price,
                "side": side,
                "size": result.get("size", 0),
                "trailing": None,
                "trailing_active": False,
                "trail_distance": trail_distance,
                "trail_trigger": trail_trigger,
                "trail_step": trail_step,
                "open_time": time.time(),
                "best_price": price,
                "initial_tp_dist": tp,
                "current_tp": result.get("tp_price", 0),
                "sl_price": result.get("sl_price", 0),
                "sl_order_id": result.get("sl_order_id"),
                "tp_order_id": result.get("tp_order_id"),
                "breakeven_done": False,
                "pending_entry": None,
                # Lien trade ↔ signal (V10)
                "signal_id": sig.get("signal_id"),
                "signal_score": sig.get("score"),
                "raw_score": sig.get("raw_score"),
                "regime": sig.get("regime"),
                "entry_features": sig.get("debug"),
            })
            rr = tp / sl if sl > 0 else 0
            print(f"[BOT][{coin}] ✅ {side.upper()} @ {price:.6g} | TP: {result['tp_price']:.6g} | "
                  f"SL: {result['sl_price']:.6g} | R:R={rr:.1f}:1 | "
                  f"trail: {trail_distance*100:.2f}% / trigger: {trail_trigger*100:.2f}%")

    def _size_factor(self, sig):
        """Facteur de taille [0.3, 1.0] : signal × volatilité (ATR) × drawdown."""
        risk_status = self.risk.status()
        factor = size_factor(
            raw_score              = sig.get("raw_score", 10),
            dynamic_sl             = sig.get("dynamic_sl"),
            sl_pct                 = SL_PCT,
            pnl_today              = risk_status.get("pnl_today", 0),
            daily_start_balance    = risk_status.get("daily_start_balance"),
            max_daily_drawdown_pct = MAX_DAILY_DRAWDOWN_PCT,
        )
        # Taille adaptée au régime de marché
        factor = round(factor * sig.get("regime_size_mult", 1.0), 2)
        if DEBUG:
            dyn_sl = sig.get("dynamic_sl") or SL_PCT
            print(f"  [SIZE] raw={sig.get('raw_score', 10)} SL={dyn_sl*100:.2f}% "
                  f"pnl_day={risk_status.get('pnl_today', 0):+.2f} "
                  f"regime×{sig.get('regime_size_mult', 1.0)} → {factor:.2f}")
        return factor
