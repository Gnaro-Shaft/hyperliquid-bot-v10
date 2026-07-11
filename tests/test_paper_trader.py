"""Tests du PaperTrader multi-positions — non-régression du bug cross-paire.

Bug v8 hérité (constaté en prod le 11/07/2026) : un seul slot de position et
fills simulés avec la bougie de la paire COURANTE de la boucle bot → une
position INJ (TP 5.02) était « remplie » par la bougie de BTC (~111 000).
Tous les longs finissaient TP, tous les shorts SL, en quelques secondes.
"""

from unittest.mock import patch

from trader.paper_trader import PaperTrader


def _mk_trader(candles_by_pair):
    """PaperTrader sans Mongo, avec bougies injectées par paire."""
    with patch.object(PaperTrader, "_connect", lambda self: None), \
         patch.object(PaperTrader, "_load_state", lambda self: None):
        t = PaperTrader()
    t._save_state = lambda: None
    t._latest_candle = lambda pair: candles_by_pair.get(pair)
    return t


INJ = "INJ/USDC:USDC"
BTC = "BTC/USDC:USDC"


def test_cross_pair_candle_cannot_fill_position():
    """La bougie de BTC (high 111000 > TP INJ 5.02) ne doit PAS remplir INJ."""
    candles = {
        INJ: {"high": 4.91, "low": 4.89, "close": 4.90},
        BTC: {"high": 111000.0, "low": 110900.0, "close": 110950.0},
    }
    t = _mk_trader(candles)
    t.pair = INJ
    assert t.place_order_with_tp_sl("buy", 4.90, tp_pct=0.025, sl_pct=0.006) is not None

    # La boucle bot passe sur BTC : aucune position BTC, et INJ reste intacte
    t.pair = BTC
    has_pos, _ = t.has_open_position()
    assert has_pos is False
    assert INJ in t.positions, "la position INJ a été fermée par la bougie BTC !"

    # Retour sur INJ : toujours ouverte (sa bougie ne touche ni TP ni SL)
    t.pair = INJ
    has_pos, info = t.has_open_position()
    assert has_pos is True
    assert info["entry_price"] == 4.90


def test_fill_uses_own_pair_candle():
    candles = {INJ: {"high": 5.10, "low": 4.89, "close": 5.05}}   # TP 5.0225 touché
    t = _mk_trader(candles)
    t.pair = INJ
    t.place_order_with_tp_sl("buy", 4.90, tp_pct=0.025, sl_pct=0.006)
    start = t.balance
    has_pos, _ = t.has_open_position()
    assert has_pos is False                     # TP rempli par SA bougie
    assert t.balance > start                    # gain crédité
    assert t.get_last_closed_trade()["price"] == t.last_closed[INJ]["price"]


def test_two_positions_are_independent():
    candles = {
        INJ: {"high": 4.91, "low": 4.89, "close": 4.90},
        BTC: {"high": 111000.0, "low": 108000.0, "close": 110950.0},  # SL BTC touché
    }
    t = _mk_trader(candles)
    t.pair = INJ
    t.place_order_with_tp_sl("buy", 4.90, tp_pct=0.025, sl_pct=0.006)
    t.pair = BTC
    t.place_order_with_tp_sl("buy", 110000.0, tp_pct=0.025, sl_pct=0.006)

    # BTC se fait stopper par SA bougie (low 108000 < SL 109340)
    has_pos, _ = t.has_open_position()
    assert has_pos is False and BTC not in t.positions
    # INJ n'est pas affectée
    assert INJ in t.positions


def test_no_double_open_same_pair():
    candles = {INJ: {"high": 4.91, "low": 4.89, "close": 4.90}}
    t = _mk_trader(candles)
    t.pair = INJ
    assert t.place_order_with_tp_sl("buy", 4.90) is not None
    assert t.place_order_with_tp_sl("buy", 4.90) is None
