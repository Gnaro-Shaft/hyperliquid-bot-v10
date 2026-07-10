"""Tests du MarketContextStore — le carry-forward est le cœur de V10."""

from collector.context_store import MarketContextStore

COINS = ["BTC", "ETH"]


def test_empty_store_returns_none_everywhere():
    store = MarketContextStore(COINS)
    ctx = store.get_context("BTC")
    assert ctx["funding_rate"] is None
    assert ctx["ob_imbalance"] is None
    assert ctx["spread_pct"] is None


def test_carry_forward_never_reverts_to_none():
    """La valeur reste disponible même longtemps après le dernier poll —
    c'est LA correction du sentiment à 38% de v8."""
    store = MarketContextStore(COINS)
    t0 = 1_700_000_000_000
    store.update_funding("BTC", 0.0001, ts_ms=t0)

    one_hour_later = t0 + 3600 * 1000
    ctx = store.get_context("BTC", now_ms=one_hour_later)
    assert ctx["funding_rate"] == 0.0001          # toujours rempli
    assert ctx["funding_age_ms"] == 3600 * 1000   # mais l'âge est traçable


def test_funding_slope_matches_v8_window():
    """slope = last - first sur la fenêtre de 6 polls (formule v8)."""
    store = MarketContextStore(COINS)
    rates = [0.0001, 0.00012, 0.00013, 0.00015, 0.00014, 0.00018]
    for i, r in enumerate(rates):
        store.update_funding("BTC", r, ts_ms=1000 + i)
    ctx = store.get_context("BTC", now_ms=2000)
    assert abs(ctx["funding_slope"] - (0.00018 - 0.0001)) < 1e-12

    # Fenêtre glissante : un 7e poll évince le premier
    store.update_funding("BTC", 0.0002, ts_ms=1007)
    ctx = store.get_context("BTC", now_ms=2000)
    assert abs(ctx["funding_slope"] - (0.0002 - 0.00012)) < 1e-12


def test_oi_trend_30m():
    store = MarketContextStore(COINS)
    store.update_oi("BTC", 1000.0, 0.0, ts_ms=1)
    store.update_oi("BTC", 1100.0, 0.1, ts_ms=2)
    ctx = store.get_context("BTC", now_ms=10)
    assert abs(ctx["oi_trend_30m"] - 0.1) < 1e-12
    assert ctx["open_interest"] == 1100.0
    assert ctx["oi_change_pct"] == 0.1


def test_orderbook_avg_and_depth_ratio():
    store = MarketContextStore(COINS)
    # 4 snapshots : imbalances 0.1/0.2/0.3/0.4, depth 100/100/100/200
    for i, (imb, depth) in enumerate([(0.1, 100), (0.2, 100), (0.3, 100), (0.4, 200)]):
        store.update_orderbook("BTC", imb, 0.0001, depth / 2, depth / 2, ts_ms=i)
    ctx = store.get_context("BTC", now_ms=100)
    assert ctx["ob_imbalance"] == 0.4                       # dernier snapshot
    assert abs(ctx["ob_imbalance_avg"] - 0.25) < 1e-9       # moyenne fenêtre
    assert ctx["spread_pct"] == 0.0001
    # depth ratio = dernier depth / moyenne = 200 / 125 = 1.6
    assert abs(ctx["ob_depth_ratio"] - 1.6) < 1e-9


def test_coins_are_isolated():
    store = MarketContextStore(COINS)
    store.update_funding("BTC", 0.0005, ts_ms=1)
    assert store.get_context("ETH")["funding_rate"] is None
    assert store.get_context("BTC")["funding_rate"] == 0.0005


def test_unknown_coin_is_ignored():
    store = MarketContextStore(COINS)
    store.update_funding("XXX", 0.1)   # ne doit pas lever
    assert "XXX" not in store._funding
