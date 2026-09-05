"""Parcours de décision d'entrée — chaque garde-fou doit vraiment bloquer.

_try_open_position enchaînait sept conditions avant de poser une entrée en
attente. Aucune n'était vérifiée : un garde-fou débranché par mégarde n'aurait
laissé aucune trace, sinon des positions ouvertes qui n'auraient pas dû l'être.
"""

import pytest

from config import COINS, SIGNAL_CONFIRM_COUNT, PULLBACK_PCT
from strategy import entry as mod
from strategy.entry import EntryManager
from trader.position_manager import empty_position


class FauxTrader:
    pair = "BTC/USDC:USDC"

    def __init__(self, solde=1000.0):
        self.solde = solde
        self.ordres = []

    def _get_total_balance(self):
        return self.solde

    def place_order_with_tp_sl(self, side, price, **kw):
        self.ordres.append((side, price))
        return {"size": 1.0, "tp_price": price * 1.02, "sl_price": price * 0.99,
                "tp_order_id": "tp1", "sl_order_id": "sl1"}


class FauxRisk:
    def __init__(self, autorise=True, raison=""):
        self.autorise, self.raison = autorise, raison

    def can_trade(self, current_balance=None):
        return self.autorise, self.raison

    def status(self):
        return {"pnl_today": 0.0, "daily_start_balance": 1000.0}


class FauxNotifier:
    def __init__(self):
        self.signaux, self.risques = [], []

    def signal_alert(self, *a, **k):
        self.signaux.append(a)

    def risk_alert(self, msg):
        self.risques.append(msg)


MARCHE_CALME = {"atr_pct": 0.005, "funding_rate": 0.00005,
                "candle_range_pct": 0.01, "spread_pct": 0.0002,
                "ob_depth_ratio": 1.0}


def _sig(score=2, **debug):
    return {"score": score, "raw_score": score * 5, "label": "Achat", "color": "🟢",
            "signal_id": "sig-1", "regime": "trend",
            "debug": dict(MARCHE_CALME, **debug)}


@pytest.fixture
def entry(monkeypatch):
    """EntryManager isolé — aucun accès réseau ni base."""
    monkeypatch.setattr(mod, "log_decision", lambda *a, **k: None)
    positions = {c: empty_position() for c in COINS}
    e = EntryManager(FauxTrader(), FauxRisk(), FauxNotifier(), positions,
                     {c: 0 for c in COINS}, {c: 0.0 for c in COINS},
                     {c: 0 for c in COINS}, COINS)
    return e


def _confirmer(entry, coin="BTC", score=2, prix=100.0):
    """Amène la série de confirmation au seuil, sans déclencher l'entrée."""
    for _ in range(SIGNAL_CONFIRM_COUNT - 1):
        entry.try_open(coin, _sig(score), prix)


def test_signal_faible_remet_la_serie_a_zero(entry):
    entry.signal_streaks["BTC"] = 2
    entry.try_open("BTC", _sig(1), 100.0)
    assert entry.signal_streaks["BTC"] == 0
    assert entry.positions["BTC"]["pending_entry"] is None


def test_signal_fort_non_confirme_n_ouvre_pas(entry):
    entry.try_open("BTC", _sig(2), 100.0)
    assert entry.signal_streaks["BTC"] == 1
    assert entry.positions["BTC"]["pending_entry"] is None


def test_entree_posee_quand_tout_passe(entry):
    _confirmer(entry)
    entry.try_open("BTC", _sig(2), 100.0)
    pending = entry.positions["BTC"]["pending_entry"]
    assert pending is not None
    assert pending["direction"] == "buy"
    assert pending["target_price"] < 100.0        # pullback sous le prix
    assert entry.notifier.signaux                 # l'alerte signal est partie


def test_cooldown_actif_bloque_l_entree(entry):
    import time
    entry.last_trade_times["BTC"] = time.time()
    entry.cooldowns["BTC"] = 600
    _confirmer(entry)
    entry.try_open("BTC", _sig(2), 100.0)
    assert entry.positions["BTC"]["pending_entry"] is None


def test_le_gestionnaire_de_risque_bloque_et_alerte(entry):
    entry.risk = FauxRisk(autorise=False, raison="drawdown")
    _confirmer(entry)
    entry.try_open("BTC", _sig(2), 100.0)
    assert entry.positions["BTC"]["pending_entry"] is None
    assert entry.notifier.risques


def test_circuit_breaker_bloque(entry):
    _confirmer(entry)
    entry.try_open("BTC", _sig(2, spread_pct=0.05), 100.0)
    assert entry.positions["BTC"]["pending_entry"] is None
    assert any("Circuit breaker" in m for m in entry.notifier.risques)


def test_correlation_bloque_une_paire_soeur_de_meme_sens(entry):
    entry.positions["ETH"].update({"active": True, "side": "buy", "entry": 2000})
    _confirmer(entry)
    entry.try_open("BTC", _sig(2), 100.0)
    assert entry.positions["BTC"]["pending_entry"] is None


def test_une_entree_deja_en_attente_ne_se_redouble_pas(entry):
    entry.positions["BTC"]["pending_entry"] = {"direction": "buy"}
    entry.try_open("BTC", _sig(2), 100.0)
    assert entry.signal_streaks["BTC"] == 0       # même pas compté


def test_pullback_atteint_declenche_l_ordre(entry):
    _confirmer(entry)
    entry.try_open("BTC", _sig(2), 100.0)
    cible = entry.positions["BTC"]["pending_entry"]["target_price"]
    entry.positions["BTC"]["pending_entry"]["sig"] = _sig(2)
    entry.check_pending("BTC", cible - 0.01)
    assert entry.trader.ordres, "l'ordre aurait dû partir"
    assert entry.positions["BTC"]["active"] is True


def test_pullback_expire_entre_au_marche(entry):
    import time
    _confirmer(entry)
    entry.try_open("BTC", _sig(2), 100.0)
    entry.positions["BTC"]["pending_entry"]["expiry_ts"] = time.time() - 1
    entry.check_pending("BTC", 100.0)
    assert entry.trader.ordres


def test_pullback_ni_atteint_ni_expire_attend(entry):
    _confirmer(entry)
    entry.try_open("BTC", _sig(2), 100.0)
    entry.check_pending("BTC", 200.0)             # prix parti à la hausse
    assert entry.trader.ordres == []
    assert entry.positions["BTC"]["pending_entry"] is not None


def test_inversion_apres_serie_opposee_confirmee(entry):
    entry.positions["BTC"].update({"active": True, "side": "buy"})
    for i in range(SIGNAL_CONFIRM_COUNT - 1):
        assert entry.should_reverse("BTC", _sig(-2)) is False
    assert entry.should_reverse("BTC", _sig(-2)) is True


def test_un_signal_non_oppose_casse_la_serie(entry):
    entry.positions["BTC"].update({"active": True, "side": "buy"})
    entry.should_reverse("BTC", _sig(-2))
    entry.should_reverse("BTC", _sig(2))          # plus opposé
    assert entry.reverse_streaks["BTC"] == 0
