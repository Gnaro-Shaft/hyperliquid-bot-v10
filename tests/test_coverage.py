"""Tests du script de vérification de couverture (fonctions pures)."""

from scripts.coverage_report import column_fill_rates, max_gap_ms, missing_minutes


def test_fill_rates_basic():
    docs = [
        {"funding_rate": 0.1, "regime": "STRONG"},
        {"funding_rate": None, "regime": "RANGE"},
        {"funding_rate": 0.2, "regime": None},
        {"funding_rate": 0.3},                      # clé absente = non rempli
    ]
    rates = column_fill_rates(docs, ["funding_rate", "regime", "spread_pct"])
    assert rates["funding_rate"] == 75.0
    assert rates["regime"] == 50.0
    assert rates["spread_pct"] == 0.0


def test_fill_rates_empty():
    assert column_fill_rates([], ["a"]) == {"a": 0.0}


def test_max_gap():
    assert max_gap_ms([0, 15_000, 30_000, 300_000]) == 270_000
    assert max_gap_ms([5]) is None
    # non trié en entrée → doit trier
    assert max_gap_ms([30_000, 0, 15_000]) == 15_000


def test_missing_minutes():
    # fenêtre de 5 minutes, bougies présentes aux minutes 0, 1, 3 → 2 manquantes
    start = 0
    end = 5 * 60_000
    candles = [0, 60_000, 180_000]
    assert missing_minutes(candles, start, end) == 2


def test_missing_minutes_full_coverage():
    start = 0
    end = 3 * 60_000
    candles = [0, 60_000, 120_000]
    assert missing_minutes(candles, start, end) == 0
