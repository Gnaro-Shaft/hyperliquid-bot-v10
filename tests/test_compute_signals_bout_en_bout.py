"""compute_signals() de bout en bout, sur données synthétiques.

Écrit après avoir cassé la production le 05/09/2026 : l'extraction des règles
de scoring avait supprimé cinq variables locales (bb_pctb, vol_r, funding,
imbalance, oi_chg) encore utilisées plus bas dans la construction du document
signal. Résultat — NameError à chaque évaluation, collecte tombée de 40 à 12
évaluations par minute pendant six minutes, ~170 évaluations perdues.

Rien ne l'avait vu : le golden master vérifie les RÈGLES, pas le câblage du
moteur autour d'elles. Ce test comble exactement ce trou — il exerce la méthode
entière et vérifie que le document produit est complet.
"""

import numpy as np
import pandas as pd
import pytest

from strategy.strategy_engine import StrategyEngine

# Champs que le document doit porter. Les cinq premiers sont ceux dont la
# disparition n'avait pas été détectée.
FEATURES_ATTENDUES = ["BB_pctB", "vol_ratio", "funding_rate", "ob_imbalance",
                      "oi_change_pct", "EMA9", "EMA21", "RSI", "MACD", "ATR",
                      "close", "close_15m"]


def _bougies(n, depart=100.0, tendance=0.006, graine=7):
    """Série OHLCV synthétique en tendance FRANCHE.

    La force compte : une tendance molle donne un ADX < 25, le gate anti-chop
    bloque, et compute_signals sort par le chemin « neutre » sans jamais
    exercer la construction du document — le test ne testerait alors rien.
    """
    alea = np.random.default_rng(graine)
    closes, prix = [], depart
    for _ in range(n):
        prix *= 1 + tendance + alea.normal(0, 0.0004)
        closes.append(prix)
    closes = np.array(closes)
    return pd.DataFrame({
        "timestamp": np.arange(n) * 900_000,
        "open": closes * 0.999,
        "high": closes * 1.004,
        "low": closes * 0.996,
        "close": closes,
        "volume": alea.uniform(1000, 5000, n),
    })


CONTEXTE_MARCHE = {
    "funding_rate": 0.00025, "funding_slope": 0.00001,
    "open_interest": 1e6, "oi_change_pct": 0.004, "oi_trend_30m": 0.004,
    "ob_imbalance": 0.25, "ob_imbalance_avg": 0.25,
    "spread_pct": 0.0002, "ob_depth_ratio": 1.0,
    "bid_depth_5": 10.0, "ask_depth_5": 8.0,
    "funding_age_ms": 1000, "oi_age_ms": 1000, "ob_age_ms": 1000,
}


class FauxSignalLogger:
    """Capture les évaluations au lieu de les écrire en base."""

    def __init__(self):
        self.evaluations = []

    def log_evaluation(self, *a, **kw):
        self.evaluations.append(kw or a)


@pytest.fixture
def moteur(monkeypatch):
    """StrategyEngine sans Mongo ni collectors, alimenté en données synthétiques."""
    m = StrategyEngine.__new__(StrategyEngine)
    m.coin = "BTC"
    m.mongo = None
    m.context_store = None
    m.candle_store = None
    m.signal_logger = FauxSignalLogger()
    m.ml_predictor = None

    # signature réelle : get_last_n_candles(self, n=100, tf="1m")
    tailles = {"1m": 60, "15m": 260, "1h": 80}
    monkeypatch.setattr(StrategyEngine, "get_last_n_candles",
                        lambda self, n=100, tf="1m": _bougies(tailles.get(tf, n)))
    monkeypatch.setattr(StrategyEngine, "get_market_context",
                        lambda self: dict(CONTEXTE_MARCHE))
    return m


def test_le_document_signal_est_complet(moteur):
    """Le trou du 05/09 : des features disparues sans que rien n'échoue."""
    sig = moteur.compute_signals()
    assert sig is not None
    debug = sig["debug"]
    manquantes = [c for c in FEATURES_ATTENDUES if c not in debug]
    assert not manquantes, f"features absentes du document : {manquantes}"


def test_les_treize_regles_sont_toutes_presentes(moteur):
    from strategy.scoring_rules import REGLES
    debug = moteur.compute_signals()["debug"]
    manquantes = [c for c, _ in REGLES if c not in debug]
    assert not manquantes, f"règles absentes du debug : {manquantes}"


def test_le_signal_porte_les_champs_de_decision(moteur):
    sig = moteur.compute_signals()
    # `gate_passed` n'appartient pas au signal retourné mais à l'évaluation
    # journalisée — vérifié séparément par test_chaque_evaluation_est_journalisee.
    for champ in ("score", "raw_score", "label", "regime", "dynamic_tp",
                  "dynamic_sl", "trend_1h", "trend_1m", "signal_id"):
        assert champ in sig, f"champ manquant : {champ}"
    assert sig["score"] in (-2, -1, 0, 1, 2)


def test_chaque_evaluation_est_journalisee(moteur):
    """Le critère V10 : une ligne par évaluation, gate bloqué compris."""
    moteur.compute_signals()
    moteur.compute_signals()
    assert len(moteur.signal_logger.evaluations) == 2


def test_deux_appels_donnent_le_meme_resultat(moteur):
    """Le moteur ne doit pas dépendre d'un état résiduel entre appels."""
    a = moteur.compute_signals()
    b = moteur.compute_signals()
    assert a["raw_score"] == b["raw_score"]
    assert a["score"] == b["score"]


def test_le_chemin_gate_bloque_est_lui_aussi_complet(moteur, monkeypatch):
    """L'autre moitié du moteur, celle qui a survécu à l'incident du 05/09.

    Quand le gate bloque, compute_signals sort par _gate_blocked — une méthode
    distincte, avec ses propres variables. C'est précisément pour ça qu'elle
    continuait de fonctionner pendant que le chemin nominal levait NameError :
    la panne était invisible dans le compte global d'évaluations.
    """
    # Marché atone : pas de tendance, bandes resserrées → gate anti-chop
    monkeypatch.setattr(StrategyEngine, "get_last_n_candles",
                        lambda self, n=100, tf="1m": _bougies(
                            {"1m": 60, "15m": 260, "1h": 80}.get(tf, n),
                            tendance=0.0, graine=3))
    sig = moteur.compute_signals()
    assert sig is not None
    assert sig["score"] == 0, "un marché sans direction ne doit pas donner de signal"
    assert len(moteur.signal_logger.evaluations) == 1, "l'évaluation doit être journalisée"
    for champ in ("raw_score", "label", "regime", "debug"):
        assert champ in sig, f"champ manquant sur le chemin bloqué : {champ}"
