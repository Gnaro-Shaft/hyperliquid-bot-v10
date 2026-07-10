"""Tests du clustering des liquidations (fonction pure)."""

from collector.whale_collector import build_liq_clusters


def _pos(coin, liq, value, szi=1.0):
    return {"coin": coin, "liquidation_px": liq, "position_value": value, "szi": szi}


def test_clusters_aggregate_nearby_liquidations():
    marks = {"BTC": 100_000.0}
    # bucket = 0.5% du mark = 500 → 99_400 et 99_600 tombent dans des buckets
    # adjacents ; 99_500 et 99_490 dans le même
    positions = [
        _pos("BTC", 99_500, 1_000_000, szi=2.0),
        _pos("BTC", 99_490, 500_000, szi=-1.0),
        _pos("BTC", 90_000, 200_000),
    ]
    clusters = build_liq_clusters(positions, marks)
    assert "BTC" in clusters
    top = clusters["BTC"][0]                      # trié par notionnel décroissant
    assert top["notional"] == 1_500_000
    assert top["n"] == 2
    assert top["n_long"] == 1 and top["n_short"] == 1


def test_far_liquidations_ignored_with_narrow_range():
    marks = {"BTC": 100_000.0}
    positions = [_pos("BTC", 50_000, 1_000_000)]   # -50% → hors ±25%
    assert build_liq_clusters(positions, marks, range_pct=0.25) == {}


def test_default_range_captures_low_leverage_whales():
    """Défaut ±100% : les baleines peu leveragées (liq à -50%) sont loggées —
    à ±25% on perdait 23 positions sur 23 (mesuré le 10/07/2026)."""
    marks = {"BTC": 100_000.0}
    positions = [_pos("BTC", 50_000, 1_000_000)]
    clusters = build_liq_clusters(positions, marks)
    assert clusters["BTC"][0]["notional"] == 1_000_000


def test_missing_liq_px_or_mark_is_skipped():
    marks = {"BTC": 100_000.0}
    positions = [
        _pos("BTC", None, 1_000_000),
        _pos("ETH", 3_000, 1_000_000),             # pas de mark ETH
    ]
    assert build_liq_clusters(positions, marks) == {}


def test_micro_price_coin():
    """Les prix micro (PEPE ~1e-5) doivent clusteriser sans effondrement à 0."""
    marks = {"PEPE": 1.2e-5}
    positions = [
        _pos("PEPE", 1.15e-5, 10_000),
        _pos("PEPE", 1.151e-5, 20_000),
    ]
    clusters = build_liq_clusters(positions, marks)
    assert clusters["PEPE"][0]["n"] == 2
    assert clusters["PEPE"][0]["px"] > 0
