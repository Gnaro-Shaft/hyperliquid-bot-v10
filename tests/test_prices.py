"""Tests de l'arrondi de prix coin-agnostique."""

from utils.prices import round_price_sig


def test_btc_scale():
    assert round_price_sig(102_345.67) == 102_350.0


def test_sol_scale():
    assert round_price_sig(151.23456) == 151.23


def test_micro_price_not_crushed_to_zero():
    """round(x, 2) de v8 aurait donné 0.0 pour PEPE — bug corrigé."""
    assert round_price_sig(1.234567e-5) == 1.2346e-5
    assert round(1.234567e-5, 2) == 0.0            # le bug v8 documenté


def test_edge_cases():
    assert round_price_sig(0) == 0
    assert round_price_sig(None) is None
