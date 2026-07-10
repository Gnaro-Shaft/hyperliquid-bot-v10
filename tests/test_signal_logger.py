"""Tests du SignalLogger — chaque ligne doit porter le schéma complet."""

from datalog.signal_logger import build_signal_doc
from config import STRATEGY_ID, STRATEGY_VERSION

SENTIMENT_COLS = [
    "funding_rate", "funding_slope", "funding_age_ms",
    "open_interest", "oi_change_pct", "oi_trend_30m", "oi_age_ms",
    "ob_imbalance", "ob_imbalance_avg", "spread_pct", "ob_depth_ratio",
    "bid_depth_5", "ask_depth_5", "ob_age_ms",
]


def _ctx_full():
    return {
        "funding_rate": 0.0001, "funding_slope": 1e-5, "funding_age_ms": 120000,
        "open_interest": 5000.0, "oi_change_pct": 0.001, "oi_trend_30m": 0.004,
        "oi_age_ms": 60000,
        "ob_imbalance": 0.15, "ob_imbalance_avg": 0.12, "spread_pct": 0.0001,
        "ob_depth_ratio": 1.1, "bid_depth_5": 100.0, "ask_depth_5": 90.0,
        "ob_age_ms": 5000,
    }


def test_doc_has_identity_and_key():
    doc = build_signal_doc("BTC", gate_passed=True, score=2, raw_score=10,
                           candle_ts=1_700_000_000_000, ctx=_ctx_full())
    assert doc["coin"] == "BTC"
    assert doc["strategy_id"] == STRATEGY_ID
    assert doc["strategy_version"] == STRATEGY_VERSION
    assert isinstance(doc["timestamp"], int)          # UTC ms
    assert doc["candle_ts"] == 1_700_000_000_000
    assert len(doc["signal_id"]) == 32                # uuid4 hex
    assert doc["signal_level"] == doc["score"] == 2


def test_sentiment_always_joined_even_when_gate_blocked():
    """Critère d'acceptation : sentiment rempli sur CHAQUE ligne, y compris
    les évaluations bloquées par un gate."""
    doc = build_signal_doc("SOL", gate_passed=False, gate_reason="regime:RANGE",
                           regime="RANGE", ctx=_ctx_full())
    for col in SENTIMENT_COLS:
        assert doc[col] is not None, f"{col} manquant sur ligne gate-blocked"
    assert doc["regime"] == "RANGE"
    assert doc["gate_passed"] is False


def test_sentiment_columns_present_even_without_ctx():
    """Même sans store (cold start), les colonnes existent (valeur None) —
    schéma stable pour Parquet."""
    doc = build_signal_doc("PEPE", gate_passed=False,
                           gate_reason="insufficient_data", ctx=None)
    for col in SENTIMENT_COLS:
        assert col in doc


def test_features_and_extras_are_flattened():
    features = {"close_15m": 100.5, "rsi_14": 55.2, "atr_pct": 0.004}
    extra = {"dynamic_tp": 0.03, "trend_1h": "bull"}
    doc = build_signal_doc("ETH", gate_passed=True, score=1, raw_score=5,
                           features=features, result_extra=extra,
                           debug={"raw": "kept"})
    assert doc["close_15m"] == 100.5
    assert doc["rsi_14"] == 55.2
    assert doc["dynamic_tp"] == 0.03
    assert doc["trend_1h"] == "bull"
    assert doc["debug"] == {"raw": "kept"}            # brut conservé


def test_signal_ids_are_unique():
    ids = {build_signal_doc("BTC", gate_passed=True)["signal_id"] for _ in range(100)}
    assert len(ids) == 100
