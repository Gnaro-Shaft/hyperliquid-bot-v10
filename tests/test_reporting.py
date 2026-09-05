"""Rapport journalier et auto-calibration — première couverture.

L'auto-calibration modifie le seuil de signal, donc le comportement du bot,
à partir du win rate des 20 derniers trades. Elle n'était testée par rien.
"""

import pytest

from config import SIGNAL_THRESHOLD_MIN, SIGNAL_THRESHOLD_MAX
from datalog import reporting

COINS_TEST = ["BTC", "ETH"]


class FauxCurseur(list):
    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return FauxCurseur(self[:n])


class FausseCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *a, **k):
        return FauxCurseur(self.docs)


class FausseBase:
    def __init__(self, docs):
        self.col = FausseCollection(docs)

    def __getitem__(self, nom):
        return self.col


class FauxNotifier:
    def __init__(self):
        self.messages = []

    def send(self, msg):
        self.messages.append(msg)


class FauxTrader:
    def _get_total_balance(self):
        return 1000.0


@pytest.fixture
def base(monkeypatch):
    """Injecte une base factice à la place du client Mongo partagé."""
    def _installer(docs):
        monkeypatch.setattr(reporting, "get_db", lambda: FausseBase(docs))
        return docs
    return _installer


def _positions(actives=()):
    p = {c: {"active": False, "side": None, "entry": 0.0} for c in COINS_TEST}
    for coin, side, entry in actives:
        p[coin] = {"active": True, "side": side, "entry": entry}
    return p


def _trades(pnls):
    return [{"pnl": v, "action": "close"} for v in pnls]


# ── Rapport journalier ───────────────────────────────────────────────────────

def test_rapport_sans_trade_le_dit_explicitement(base):
    base([])
    notifier = FauxNotifier()
    reporting.send_daily_report(notifier, FauxTrader(), _positions(), COINS_TEST)
    assert len(notifier.messages) == 1
    assert "Aucun trade fermé" in notifier.messages[0]


def test_rapport_calcule_le_win_rate(base):
    base(_trades([1.0, 2.0, -1.0, -1.0]))       # 2 gagnants sur 4
    notifier = FauxNotifier()
    reporting.send_daily_report(notifier, FauxTrader(), _positions(), COINS_TEST)
    msg = notifier.messages[0]
    assert "50.0%" in msg
    assert "+1.0000 USDC" in msg


def test_rapport_compte_un_pnl_nul_comme_une_perte(base):
    """Frontière : `pnl > 0` pour gagner — zéro tombe côté perdant."""
    base(_trades([0.0, 1.0]))
    notifier = FauxNotifier()
    reporting.send_daily_report(notifier, FauxTrader(), _positions(), COINS_TEST)
    assert "50.0%" in notifier.messages[0]


def test_rapport_liste_les_positions_ouvertes(base):
    base(_trades([1.0]))
    notifier = FauxNotifier()
    reporting.send_daily_report(notifier, FauxTrader(),
                                _positions([("ETH", "sell", 2450.5)]), COINS_TEST)
    assert "ETH SELL @ 2450.5" in notifier.messages[0]


def test_rapport_survit_a_une_base_injoignable(monkeypatch):
    """Un rapport raté ne doit pas remonter d'exception dans la boucle."""
    def refuse():
        raise RuntimeError("Mongo injoignable")
    monkeypatch.setattr(reporting, "get_db", refuse)
    notifier = FauxNotifier()
    reporting.send_daily_report(notifier, FauxTrader(), _positions(), COINS_TEST)
    assert notifier.messages == []


# ── Auto-calibration ─────────────────────────────────────────────────────────

def test_calibration_ne_bouge_pas_sans_assez_de_trades(base):
    base(_trades([1.0, 1.0]))
    notifier = FauxNotifier()
    assert reporting.auto_calibrate(9, notifier) == 9
    assert notifier.messages == []


def test_calibration_durcit_le_seuil_si_le_win_rate_est_bas(base):
    base(_trades([1.0] + [-1.0] * 9))            # 10 %
    notifier = FauxNotifier()
    assert reporting.auto_calibrate(9, notifier) == 10
    assert "Auto-calibration" in notifier.messages[0]


def test_calibration_assouplit_si_le_win_rate_est_haut(base):
    base(_trades([1.0] * 9 + [-1.0]))            # 90 %
    notifier = FauxNotifier()
    assert reporting.auto_calibrate(9, notifier) == 8


def test_calibration_respecte_les_bornes(base):
    base(_trades([1.0] + [-1.0] * 9))
    assert reporting.auto_calibrate(SIGNAL_THRESHOLD_MAX, FauxNotifier()) == SIGNAL_THRESHOLD_MAX
    base(_trades([1.0] * 9 + [-1.0]))
    assert reporting.auto_calibrate(SIGNAL_THRESHOLD_MIN, FauxNotifier()) == SIGNAL_THRESHOLD_MIN


def test_calibration_zone_neutre_ne_notifie_pas(base):
    base(_trades([1.0] * 5 + [-1.0] * 5))       # 50 %, entre 40 et 60
    notifier = FauxNotifier()
    assert reporting.auto_calibrate(9, notifier) == 9
    assert notifier.messages == []


def test_calibration_rend_le_seuil_courant_en_cas_d_erreur(monkeypatch):
    """Le seuil ne doit jamais être perdu à cause d'une panne de base."""
    def refuse():
        raise RuntimeError("Mongo injoignable")
    monkeypatch.setattr(reporting, "get_db", refuse)
    assert reporting.auto_calibrate(9, FauxNotifier()) == 9
