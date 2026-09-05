"""Test différentiel du trailing : nouvelle implémentation contre celle de v8.

_manage_trailing a été récrite pour s'appuyer sur trader/position_rules.py.
Comme elle gère des positions, « ça compile » ne suffit pas : l'arithmétique
d'origine est reproduite ici littéralement comme oracle, et les deux versions
sont comparées sur des milliers de trajectoires de prix aléatoires.

L'oracle n'est pas du code mort : il documente le comportement de v8 et fera
échouer la suite si quelqu'un modifie le trailing sans s'en rendre compte.
"""

import random

import pytest

from config import (TRAIL_PCT, TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
                    BREAKEVEN_TRIGGER_PCT, BREAKEVEN_OFFSET_PCT)
from utils.prices import round_price_sig
import main


class FauxTrader:
    """Enregistre les ordres au lieu de les passer."""

    def __init__(self, echec_tp=False, echec_sl=False):
        self.appels = []
        self.echec_tp = echec_tp
        self.echec_sl = echec_sl

    def update_tp(self, prix, old_tp_order_id=None):
        self.appels.append(("tp", round(prix, 8)))
        return None if self.echec_tp else {"id": f"tp{len(self.appels)}"}

    def update_sl(self, prix, old_sl_order_id=None):
        self.appels.append(("sl", round(prix, 8)))
        return None if self.echec_sl else {"id": f"sl{len(self.appels)}"}

    def close_position(self, reason=None, context=None):
        self.appels.append(("close", reason))
        return {"pnl": 1.0}


class FauxRisk:
    def register_trade_result(self, pnl):
        pass


def _reference_v8(bot, coin, last_price):
    """Implémentation d'origine, recopiée telle quelle. NE PAS simplifier."""
    pos = bot.positions[coin]
    entry = pos["entry"]
    side = pos["side"]
    if not entry or not side or not last_price:
        return

    trail_dist = pos.get("trail_distance", TRAIL_PCT)
    trail_trig = pos.get("trail_trigger", TRAILING_TRIGGER_PCT)
    trail_step = pos.get("trail_step", TRAILING_STEP_PCT)
    gain_pct = (last_price - entry) / entry if side == "buy" else (entry - last_price) / entry

    best = pos.get("best_price") or entry
    initial_tp_dist = pos.get("initial_tp_dist", 0)

    if side == "buy" and last_price > best:
        pos["best_price"] = last_price
        if initial_tp_dist > 0:
            new_tp = last_price * (1 + initial_tp_dist * 0.5)
            if new_tp > pos.get("current_tp", 0):
                o = bot.trader.update_tp(new_tp, old_tp_order_id=pos.get("tp_order_id"))
                if o is not None:
                    pos["current_tp"] = new_tp
                    pos["tp_order_id"] = o.get("id")
    elif side == "sell" and last_price < best:
        pos["best_price"] = last_price
        if initial_tp_dist > 0:
            new_tp = last_price * (1 - initial_tp_dist * 0.5)
            if new_tp < pos.get("current_tp", float("inf")):
                o = bot.trader.update_tp(new_tp, old_tp_order_id=pos.get("tp_order_id"))
                if o is not None:
                    pos["current_tp"] = new_tp
                    pos["tp_order_id"] = o.get("id")

    if not pos.get("breakeven_done", False) and gain_pct >= BREAKEVEN_TRIGGER_PCT:
        breakeven_sl = round_price_sig(entry * (1 + BREAKEVEN_OFFSET_PCT)) if side == "buy" \
                       else round_price_sig(entry * (1 - BREAKEVEN_OFFSET_PCT))
        old_sl = pos.get("sl_price", 0)
        is_better = (side == "buy" and breakeven_sl > old_sl) or \
                    (side == "sell" and breakeven_sl < old_sl)
        if is_better:
            o = bot.trader.update_sl(breakeven_sl, old_sl_order_id=pos.get("sl_order_id"))
            if o is not None:
                pos["sl_price"] = breakeven_sl
                pos["sl_order_id"] = o.get("id")
                pos["breakeven_done"] = True

    if not pos["trailing_active"] and gain_pct >= trail_trig:
        pos["trailing"] = last_price * (1 - trail_dist) if side == "buy" \
                          else last_price * (1 + trail_dist)
        pos["trailing_active"] = True

    if pos["trailing_active"]:
        trailing = pos["trailing"]
        if side == "buy":
            new_trailing = last_price * (1 - trail_dist)
            if new_trailing > trailing + (entry * trail_step):
                pos["trailing"] = new_trailing
            elif last_price <= trailing:
                bot.trader.close_position(reason="trailing_stop", context={})
                bot.positions[coin] = bot._empty_position()
        elif side == "sell":
            new_trailing = last_price * (1 + trail_dist)
            if new_trailing < trailing - (entry * trail_step):
                pos["trailing"] = new_trailing
            elif last_price >= trailing:
                bot.trader.close_position(reason="trailing_stop", context={})
                bot.positions[coin] = bot._empty_position()


def _bot(trader):
    """TradingBot minimal, sans __init__ (qui ouvrirait des connexions)."""
    b = main.TradingBot.__new__(main.TradingBot)
    b.trader = trader
    b.risk = FauxRisk()
    b._cooldowns = {"BTC": 600}
    b._last_trade_times = {"BTC": 0.0}
    b.positions = {}
    return b


def _position(entry, side, tp_dist=0.04):
    p = main.TradingBot._empty_position(None)
    p.update({"active": True, "entry": entry, "side": side, "size": 1.0,
              "initial_tp_dist": tp_dist, "current_tp": 0, "sl_price": 0,
              "open_time": 0.0})
    return p


def _etat_comparable(pos):
    """Champs qui portent la décision, arrondis contre le bruit flottant."""
    cles = ("active", "side", "entry", "best_price", "current_tp", "sl_price",
            "trailing", "trailing_active", "breakeven_done")
    return {k: (round(pos[k], 8) if isinstance(pos.get(k), float) else pos.get(k))
            for k in cles}


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("echecs", [(False, False), (True, False), (False, True)])
def test_equivalence_sur_trajectoires_aleatoires(side, echecs):
    """Même trajectoire de prix, même état final et mêmes ordres passés."""
    echec_tp, echec_sl = echecs
    alea = random.Random(1234)

    for scenario in range(300):
        entry = alea.uniform(1.0, 50_000.0)
        prix = []
        courant = entry
        for _ in range(alea.randint(1, 25)):
            courant *= 1 + alea.uniform(-0.03, 0.03)
            prix.append(courant)

        ancien = _bot(FauxTrader(echec_tp, echec_sl))
        ancien.positions["BTC"] = _position(entry, side)
        nouveau = _bot(FauxTrader(echec_tp, echec_sl))
        nouveau.positions["BTC"] = _position(entry, side)

        for p in prix:
            _reference_v8(ancien, "BTC", p)
            nouveau._manage_trailing("BTC", p)

        assert _etat_comparable(nouveau.positions["BTC"]) == \
               _etat_comparable(ancien.positions["BTC"]), \
               f"état divergent (scénario {scenario}, entry={entry}, {side})"
        assert nouveau.trader.appels == ancien.trader.appels, \
               f"ordres divergents (scénario {scenario}, entry={entry}, {side})"


def test_position_sans_entree_ne_declenche_rien():
    b = _bot(FauxTrader())
    b.positions["BTC"] = _position(0, "buy")
    b._manage_trailing("BTC", 100.0)
    assert b.trader.appels == []
