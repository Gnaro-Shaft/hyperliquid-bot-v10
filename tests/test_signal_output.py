"""Sorties du moteur : TP/SL dynamiques et vecteur de features.

Les trois issues de compute_signals doivent produire une ligne au même schéma —
c'est le critère V10 « aucun trou dans le dataset ». Les sorties anticipées
sont couvertes par test_compute_signals_bout_en_bout ; ici, les deux briques
qu'elles partagent.
"""

import pytest

from config import SL_PCT, TP_PCT
from strategy import signal_output as so


# ── TP/SL dynamiques ─────────────────────────────────────────────────────────

def test_tp_sl_suivent_la_volatilite():
    sl_calme, tp_calme, _ = so.tp_sl_dynamiques(0.5, 100.0, 1.0, 1.0)
    sl_agite, tp_agite, _ = so.tp_sl_dynamiques(3.0, 100.0, 1.0, 1.0)
    assert sl_agite > sl_calme, "un marché plus volatil exige un stop plus large"


def test_les_multiplicateurs_de_regime_s_appliquent():
    sl_ref, tp_ref, _ = so.tp_sl_dynamiques(1.0, 100.0, 1.0, 1.0)
    sl_x2, tp_x2, _ = so.tp_sl_dynamiques(1.0, 100.0, 2.0, 1.5)
    assert sl_x2 == pytest.approx(sl_ref * 2)
    assert tp_x2 == pytest.approx(tp_ref * 1.5)


def test_repli_statique_sans_atr():
    """Sans ATR exploitable, on retombe sur la config — et le debug le dit,
    pour qu'une ligne d'archive permette de savoir quelle voie a servi."""
    for atr in (None, 0):
        sl, tp, dbg = so.tp_sl_dynamiques(atr, 100.0, 1.0, 1.0)
        assert dbg["atr"] == "N/A"
        assert "static fallback" in dbg["dynamic_sl"]
        assert "static fallback" in dbg["dynamic_tp"]


def test_repli_statique_si_prix_nul():
    sl, tp, dbg = so.tp_sl_dynamiques(1.0, 0.0, 1.0, 1.0)
    assert dbg["atr"] == "N/A"


def test_le_debug_expose_le_ratio_gain_risque():
    sl, tp, dbg = so.tp_sl_dynamiques(1.0, 100.0, 1.0, 1.0)
    assert "R:R=" in dbg["dynamic_tp"]
    assert f"{tp/sl:.1f}" in dbg["dynamic_tp"]


# ── Vecteur de features ──────────────────────────────────────────────────────

def test_features_meme_schema_quelles_que_soient_les_donnees():
    """Le schéma ne doit pas dépendre du contenu : sinon les colonnes du
    dataset varieraient d'une ligne à l'autre."""
    pleine = so.features_depuis_bougie({
        "close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1000.0,
        "EMA9": 100.0, "EMA21": 99.0, "EMA9_slope": 0.1, "RSI": 55.0,
        "MACD": 1.0, "MACD_signal": 0.5, "MACD_hist": 0.5,
        "BB_upper": 102.0, "BB_lower": 98.0, "BB_pctB": 0.5, "BB_width": 0.04,
        "VWAP": 99.5, "ATR": 1.0, "vol_ratio": 1.2,
        "ADX": 30.0, "PLUS_DI": 25.0, "MINUS_DI": 15.0,
    })
    vide = so.features_depuis_bougie({})
    assert set(pleine) == set(vide)
    assert len(pleine) == 24


def test_valeurs_absentes_donnent_none_pas_nan():
    """None se sérialise proprement en Parquet ; NaN pollue les colonnes."""
    vide = so.features_depuis_bougie({})
    assert all(v is None for v in vide.values())


def test_atr_pct_et_candle_range_sont_derives():
    f = so.features_depuis_bougie({"close": 100.0, "ATR": 2.0, "high": 103.0, "low": 98.0})
    assert f["atr_pct"] == pytest.approx(0.02)
    assert f["candle_range_pct"] == pytest.approx(0.05)


def test_pas_de_division_par_zero_sur_prix_nul():
    f = so.features_depuis_bougie({"close": 0.0, "ATR": 2.0, "high": 1.0, "low": 0.5})
    assert f["atr_pct"] is None
    assert f["candle_range_pct"] is None
