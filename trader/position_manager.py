"""
Gestion des positions ouvertes : trailing, breakeven, TP/SL, clôture.

Extrait de main.py, où ces 176 lignes cohabitaient avec le démarrage, la boucle
et la décision d'entrée. Une position ouverte a sa propre raison de changer.

Les décisions arithmétiques vivent dans position_rules.py, testées sur les deux
sens et vérifiées par un test différentiel contre l'implémentation de v8. Ce
module ne porte que l'enchaînement et les effets : ordres passés à l'exchange,
journalisation, mise à jour de l'état.

Le dictionnaire `positions` est partagé avec TradingBot — les entrées sont
modifiées en place, jamais réaffectées en bloc.
"""

import time

from config import (
    DEBUG, PAIRS, TRAIL_PCT, TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
    BREAKEVEN_TRIGGER_PCT, BREAKEVEN_OFFSET_PCT, MAX_HOLD_SEC,
)
from risk import guards
from trader import position_rules as regles


def empty_position():
    return {
        "active": False,
        "entry": None,
        "side": None,
        "size": 0,
        "trail_distance": TRAIL_PCT,
        "trail_trigger": TRAILING_TRIGGER_PCT,
        "trail_step": TRAILING_STEP_PCT,
        "trailing": None,
        "trailing_active": False,
        "open_time": None,
        "best_price": None,
        "initial_tp_dist": 0,
        "current_tp": 0,
        "sl_price": 0,
        "sl_order_id": None,        # ID ordre SL sur l'exchange
        "tp_order_id": None,        # ID ordre TP sur l'exchange
        "breakeven_done": False,
        "pending_entry": None,
        # Lien trade ↔ signal (V10)
        "signal_id": None,
        "signal_score": None,
        "raw_score": None,
        "regime": None,
        "entry_features": None,
    }


class PositionManager:
    """Pilote les positions ouvertes pour le compte du bot."""

    def __init__(self, trader, risk, notifier, positions, cooldowns, last_trade_times):
        self.trader = trader
        self.risk = risk
        self.notifier = notifier
        self.positions = positions
        self.cooldowns = cooldowns
        self.last_trade_times = last_trade_times

    def context(self, coin, closing_signal_id=None):
        """Contexte de journalisation d'une position (lien trade ↔ signal V10)."""
        pos = self.positions.get(coin, {})
        ctx = {
            "coin": coin,
            "signal_id": pos.get("signal_id"),
            "signal_score": pos.get("signal_score"),
            "raw_score": pos.get("raw_score"),
            "regime": pos.get("regime"),
            "entry_features": pos.get("entry_features"),
        }
        if closing_signal_id:
            ctx["closing_signal_id"] = closing_signal_id
        return ctx

    def manage_trailing(self, coin, last_price):
        """Trailing profit + breakeven stop + trailing stop pour une paire.

        MOTEUR CONSERVÉ (seule sortie performante de v8). Les décisions
        arithmétiques vivent dans trader/position_rules.py, où elles sont
        testées sur les deux sens ; il ne reste ici que l'enchaînement et les
        effets sur l'exchange.
        """
        pos = self.positions[coin]
        entry, side = pos["entry"], pos["side"]

        # Garde-fou : si entry est manquant ou nul, on ne peut rien calculer
        if not entry or not side:
            return

        # Plafond de détention — évalué avant le prix, dont il ne dépend pas.
        # Aligne le bot sur l'horizon de 7 jours du test de barrière : sans
        # lui, une position non résolue gèlerait un des deux emplacements
        # indéfiniment.
        if regles.detention_expiree(pos.get("open_time"), time.time(), MAX_HOLD_SEC):
            age_h = (time.time() - pos["open_time"]) / 3600
            print(f"[BOT][{coin}] ⏳ Détention max atteinte ({age_h:.1f} h) — clôture")
            self._close_on_max_hold(coin)
            return

        if not last_price:
            return

        trail_dist = pos.get("trail_distance", TRAIL_PCT)
        trail_trig = pos.get("trail_trigger", TRAILING_TRIGGER_PCT)
        trail_step = pos.get("trail_step", TRAILING_STEP_PCT)
        gain = regles.gain_pct(entry, last_price, side)

        # --- Trailing Profit ---
        if regles.est_nouveau_sommet(side, last_price, pos.get("best_price"), entry):
            pos["best_price"] = last_price
            nouveau_tp = regles.tp_ratchet(side, last_price,
                                           pos.get("initial_tp_dist", 0),
                                           pos.get("current_tp"))
            if nouveau_tp is not None:
                ordre = self.trader.update_tp(
                    nouveau_tp, old_tp_order_id=pos.get("tp_order_id"))
                if ordre is not None:
                    pos["current_tp"] = nouveau_tp
                    pos["tp_order_id"] = ordre.get("id")
                    print(f"[BOT][{coin}] 🎯 TP → {nouveau_tp:.6g}")
                else:
                    print(f"[BOT][{coin}] ⚠️ TP update ECHEC @ {nouveau_tp:.6g}")

        # --- Breakeven Stop ---
        if not pos.get("breakeven_done", False) and gain >= BREAKEVEN_TRIGGER_PCT:
            breakeven_sl = regles.niveau_breakeven(side, entry, BREAKEVEN_OFFSET_PCT)
            if regles.breakeven_ameliore(side, breakeven_sl, pos.get("sl_price", 0)):
                print(f"[BOT][{coin}] 🛡️ Breakeven @ {breakeven_sl:.6g} (gain: {gain*100:.2f}%)")
                ordre = self.trader.update_sl(
                    breakeven_sl, old_sl_order_id=pos.get("sl_order_id"))
                if ordre is not None:
                    # Confirmer seulement si l'exchange a bien placé le nouvel ordre
                    pos["sl_price"] = breakeven_sl
                    pos["sl_order_id"] = ordre.get("id")
                    pos["breakeven_done"] = True
                else:
                    print(f"[BOT][{coin}] ⚠️ Breakeven SL ECHEC — sera retenté au prochain cycle")

        # --- Trailing Stop ---
        if not pos["trailing_active"] and gain >= trail_trig:
            pos["trailing"] = regles.niveau_trailing(side, last_price, trail_dist)
            pos["trailing_active"] = True
            print(f"[BOT][{coin}] 📈 Trailing activé @ {pos['trailing']:.6g} "
                  f"(gain: {gain*100:.2f}%)")

        if pos["trailing_active"]:
            candidat = regles.niveau_trailing(side, last_price, trail_dist)
            # `elif` volontaire : un cycle qui fait monter le stop ne peut pas
            # aussi le déclencher — comportement d'origine.
            if regles.trailing_doit_monter(side, candidat, pos["trailing"], entry, trail_step):
                pos["trailing"] = candidat
                fleche = "📈" if side == "buy" else "📉"
                print(f"[BOT][{coin}] {fleche} Trailing → {candidat:.6g}")
            elif regles.trailing_touche(side, last_price, pos["trailing"]):
                comparaison = "<=" if side == "buy" else ">="
                print(f"[BOT][{coin}] 🔔 Trailing touché "
                      f"({last_price:.6g} {comparaison} {pos['trailing']:.6g})")
                self._close_on_trailing(coin)

    def _close_on_max_hold(self, coin):
        """Ferme une position qui a dépassé la durée de détention maximale."""
        result = self.trader.close_position(
            reason="max_hold", context=self.context(coin))
        if result:
            self.risk.register_trade_result(result["pnl"])
            guards.adjust_cooldown(self.cooldowns, coin, result["pnl"])
        self.last_trade_times[coin] = time.time()
        self.positions[coin] = empty_position()

    def _close_on_trailing(self, coin):
        """Ferme la position sur déclenchement du stop suiveur."""
        result = self.trader.close_position(
            reason="trailing_stop", context=self.context(coin))
        if result:
            self.risk.register_trade_result(result["pnl"])
            guards.adjust_cooldown(self.cooldowns, coin, result["pnl"])
        self.last_trade_times[coin] = time.time()
        self.positions[coin] = empty_position()

    def check_tp_sl_hit(self, coin, live_price):
        """Détecte si le prix live a croisé le TP ou SL — confirme avec l'exchange."""
        pos = self.positions[coin]
        side = pos.get("side")
        current_tp = pos.get("current_tp", 0)
        sl_price = pos.get("sl_price", 0)

        tp_hit, sl_hit = regles.tp_sl_franchi(side, live_price, current_tp, sl_price)

        if tp_hit or sl_hit:
            tag = "TP" if tp_hit else "SL"
            if DEBUG:
                print(f"[BOT][{coin}] ⚡ Prix live {live_price:.6g} a croisé le {tag}")
            # Laisser l'exchange confirmer la fermeture (TP/SL exchange)
            has_pos, _ = self.trader.has_open_position()
            if not has_pos and self.positions[coin]["active"]:
                self.handle_exchange_closure(coin, live_price)

    def handle_exchange_closure(self, coin, fallback_price):
        """Traite une fermeture détectée sur l'exchange (TP/SL atteint)."""
        pos = self.positions[coin]
        entry = pos.get("entry", 0)
        side = pos.get("side", "buy")
        size = pos.get("size", 0)
        open_time = pos.get("open_time", time.time() - 3600)

        since_ms = int(open_time * 1000)
        last_fill = self.trader.get_last_closed_trade(since_ms=since_ms)
        exit_price = last_fill["price"] if (last_fill and last_fill["price"] > 0) else fallback_price

        pnl = regles.pnl_realise(side, entry, exit_price, size)

        print(f"[BOT][{coin}] ⚡ Fermé par l'exchange | {entry:.6g} → {exit_price:.6g} | PnL: {pnl:+.4f}")

        self.trader.cancel_open_orders()
        self.notifier.trade_closed(self.trader.pair, side, entry, exit_price, pnl, "tp_sl_exchange")
        self.risk.register_trade_result(pnl)
        guards.adjust_cooldown(self.cooldowns, coin, pnl)
        self.last_trade_times[coin] = time.time()
        self.trader.logger.log_trade({
            "pair": self.trader.pair,
            "side": side,
            "action": "close",
            "entry_price": entry,
            "exit_price": exit_price,
            "size": size,
            "pnl": pnl,
            "reason": "tp_sl_exchange",
        }, context=self.context(coin))
        self.positions[coin] = empty_position()

    def sync_on_start(self):
        """Détecte les positions ouvertes sur toutes les paires au redémarrage."""
        for pair in PAIRS:
            coin = pair.split("/")[0]
            self.trader.pair = pair
            has_pos, pos_info = self.trader.has_open_position()
            if has_pos and pos_info:
                # Conserver la date d'ouverture réelle : la remplacer par
                # `now` remettrait à zéro le compteur de MAX_HOLD_SEC à chaque
                # redémarrage, et le plafond de détention ne se déclencherait
                # jamais sur un service qui redémarre plus souvent que lui.
                open_ts = pos_info.get("open_ts")
                self.positions[coin] = {
                    **empty_position(),
                    "active": True,
                    "entry": pos_info["entry_price"],
                    "side": "buy" if pos_info["side"] == "long" else "sell",
                    "size": abs(pos_info.get("contracts", 0)),
                    "open_time": open_ts / 1000 if open_ts else time.time(),
                }
                print(f"[BOT] [{coin}] Position existante: {pos_info['side']} "
                      f"@ {pos_info['entry_price']:.6g} | PnL: {pos_info['unrealized_pnl']:+.2f}")

    # ──────────────────────────────────────────────────────────
    # Boucle principale
    # ──────────────────────────────────────────────────────────
