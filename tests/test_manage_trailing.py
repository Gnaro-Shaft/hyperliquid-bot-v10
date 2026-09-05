"""Test différentiel du trailing : nouvelle implémentation contre celle de v8.

_manage_trailing a été récrite pour s'appuyer sur trader/position_rules.py.
Comme elle gère des positions, « ça compile » ne suffit pas : l'arithmétique
d'origine est reproduite ici littéralement comme oracle, et les deux versions
sont comparées sur des milliers de trajectoires de prix aléatoires.

L'oracle n'est pas du code mort : il documente le comportement de v8 et fera
échouer la suite si quelqu'un modifie le trailing sans s'en rendre compte.
"""

import random
import time

import pytest

from config import (TRAIL_PCT, TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
                    BREAKEVEN_TRIGGER_PCT, BREAKEVEN_OFFSET_PCT)
from utils.prices import round_price_sig
import main
from trader.position_manager import PositionManager, empty_position


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
                bot.positions[coin] = empty_position()
        elif side == "sell":
            new_trailing = last_price * (1 + trail_dist)
            if new_trailing < trailing - (entry * trail_step):
                pos["trailing"] = new_trailing
            elif last_price >= trailing:
                bot.trader.close_position(reason="trailing_stop", context={})
                bot.positions[coin] = empty_position()


def _bot(trader):
    """PositionManager isolé — plus besoin d'un TradingBot pour tester le trailing.

    C'est le bénéfice direct de l'extraction : avant, cette logique n'était
    atteignable qu'en instanciant le bot entier, donc en ouvrant des connexions.
    """
    return PositionManager(trader, FauxRisk(), None, {}, {"BTC": 600}, {"BTC": 0.0})


def _position(entry, side, tp_dist=0.04):
    p = empty_position()
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
            nouveau.manage_trailing("BTC", p)

        assert _etat_comparable(nouveau.positions["BTC"]) == \
               _etat_comparable(ancien.positions["BTC"]), \
               f"état divergent (scénario {scenario}, entry={entry}, {side})"
        assert nouveau.trader.appels == ancien.trader.appels, \
               f"ordres divergents (scénario {scenario}, entry={entry}, {side})"


def test_position_sans_entree_ne_declenche_rien():
    b = _bot(FauxTrader())
    b.positions["BTC"] = _position(0, "buy")
    b.manage_trailing("BTC", 100.0)
    assert b.trader.appels == []


# ── Plafond de détention ─────────────────────────────────────────────────────

def _bot_avec_position(open_time, side="buy", entry=100.0):
    """PositionManager doté d'une position ouverte, sur trader factice."""
    trader = FauxTrader()
    positions = {"BTC": empty_position()}
    positions["BTC"].update(active=True, entry=entry, side=side, size=1.0,
                            open_time=open_time)
    # `adjust_cooldown` lit cooldowns[coin] : la clé doit préexister, comme en
    # production où elle est initialisée pour toutes les paires au démarrage.
    mgr = PositionManager(trader, FauxRisk(), None, positions,
                          {"BTC": 600}, {"BTC": 0.0})
    return mgr, trader, positions


def test_position_fermee_quand_la_detention_max_est_atteinte(monkeypatch):
    import trader.position_manager as pm
    monkeypatch.setattr(pm, "MAX_HOLD_SEC", 7 * 86400)
    mgr, trader, positions = _bot_avec_position(open_time=time.time() - 8 * 86400)

    mgr.manage_trailing("BTC", 101.0)

    assert ("close", "max_hold") in trader.appels
    assert not positions["BTC"]["active"]


def test_position_jeune_nest_pas_fermee(monkeypatch):
    import trader.position_manager as pm
    monkeypatch.setattr(pm, "MAX_HOLD_SEC", 7 * 86400)
    mgr, trader, positions = _bot_avec_position(open_time=time.time() - 3600)

    mgr.manage_trailing("BTC", 101.0)

    assert ("close", "max_hold") not in trader.appels
    assert positions["BTC"]["active"]


def test_le_plafond_ne_depend_pas_du_prix(monkeypatch):
    """Il est évalué avant le garde-fou sur le prix : une position expirée doit
    se fermer même si le flux de prix est momentanément absent."""
    import trader.position_manager as pm
    monkeypatch.setattr(pm, "MAX_HOLD_SEC", 7 * 86400)
    mgr, trader, positions = _bot_avec_position(open_time=time.time() - 8 * 86400)

    mgr.manage_trailing("BTC", None)

    assert ("close", "max_hold") in trader.appels


def test_position_sans_open_time_survit_au_plafond(monkeypatch):
    import trader.position_manager as pm
    monkeypatch.setattr(pm, "MAX_HOLD_SEC", 7 * 86400)
    mgr, trader, positions = _bot_avec_position(open_time=None)

    mgr.manage_trailing("BTC", 101.0)

    assert ("close", "max_hold") not in trader.appels
    assert positions["BTC"]["active"]


# ── Reprise après redémarrage : la date d'ouverture doit survivre ────────────

class FauxTraderAvecPosition:
    """Trader factice exposant une position ouverte, comme au redémarrage."""

    def __init__(self, open_ts):
        self.pair = None
        self._open_ts = open_ts

    def has_open_position(self):
        if self.pair != "BTC/USDC:USDC":
            return False, None
        return True, {"side": "long", "entry_price": 100.0, "contracts": 1.0,
                      "mark_price": 101.0, "open_ts": self._open_ts,
                      "unrealized_pnl": 1.0}


def test_sync_conserve_la_date_d_ouverture_reelle():
    """Sans ça, chaque redémarrage remettrait à zéro le compteur de détention
    et le plafond de 7 jours ne se déclencherait jamais."""
    ouverture_ms = int((time.time() - 5 * 86400) * 1000)
    mgr = PositionManager(FauxTraderAvecPosition(ouverture_ms), FauxRisk(), None,
                          {"BTC": empty_position()}, {"BTC": 600}, {"BTC": 0.0})

    mgr.sync_on_start()

    age_jours = (time.time() - mgr.positions["BTC"]["open_time"]) / 86400
    assert age_jours == pytest.approx(5, abs=0.01)


def test_sync_retombe_sur_maintenant_sans_horodatage():
    """Un exchange qui ne fournit pas la date : on ne ferme pas sur une date
    inventée, on repart de maintenant."""
    mgr = PositionManager(FauxTraderAvecPosition(None), FauxRisk(), None,
                          {"BTC": empty_position()}, {"BTC": 600}, {"BTC": 0.0})

    mgr.sync_on_start()

    assert mgr.positions["BTC"]["open_time"] == pytest.approx(time.time(), abs=5)


def test_une_position_ancienne_reprise_est_fermee_au_prochain_cycle(monkeypatch):
    """Bout en bout : reprise d'une position de 8 jours, puis clôture."""
    import trader.position_manager as pm
    monkeypatch.setattr(pm, "MAX_HOLD_SEC", 7 * 86400)

    reprise = FauxTraderAvecPosition(int((time.time() - 8 * 86400) * 1000))
    positions = {"BTC": empty_position()}
    mgr = PositionManager(reprise, FauxRisk(), None, positions,
                          {"BTC": 600}, {"BTC": 0.0})
    mgr.sync_on_start()
    assert positions["BTC"]["active"]

    # le trader factice de clôture prend le relais pour le cycle suivant
    fermeur = FauxTrader()
    mgr.trader = fermeur
    mgr.manage_trailing("BTC", 101.0)

    assert ("close", "max_hold") in fermeur.appels
    assert not positions["BTC"]["active"]
