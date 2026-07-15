"""Tests du CandleStore — cache mémoire anti-throttle M0."""

from collector.candle_store import CandleStore, LIMITS


def _c(ts, close=100.0):
    return {"timestamp": ts, "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close, "volume": 10.0}


def test_update_and_ascending_order():
    s = CandleStore(["BTC"])
    for ts in (3000, 1000, 2000):          # arrivée désordonnée
        s.update("BTC", "15m", _c(ts))
    rows = s.get_last_n("BTC", "15m", 10)
    assert [r["timestamp"] for r in rows] == [1000, 2000, 3000]


def test_in_progress_candle_updates_in_place():
    """La bougie en cours arrive plusieurs fois avec le même timestamp —
    elle doit se mettre à jour, pas se dupliquer."""
    s = CandleStore(["BTC"])
    s.update("BTC", "1m", _c(1000, close=100.0))
    s.update("BTC", "1m", _c(1000, close=101.5))
    rows = s.get_last_n("BTC", "1m", 10)
    assert len(rows) == 1
    assert rows[0]["close"] == 101.5


def test_trim_to_limit():
    s = CandleStore(["BTC"])
    for ts in range(300):
        s.update("BTC", "15m", _c(ts))
    assert s.count("BTC", "15m") == LIMITS["15m"]
    rows = s.get_last_n("BTC", "15m", 500)
    assert rows[0]["timestamp"] == 300 - LIMITS["15m"]   # les plus vieilles purgées
    assert rows[-1]["timestamp"] == 299


def test_coins_and_timeframes_isolated():
    s = CandleStore(["BTC", "ETH"])
    s.update("BTC", "15m", _c(1000))
    s.update("BTC", "1m", _c(2000))
    assert s.count("ETH", "15m") == 0
    assert s.count("BTC", "1m") == 1
    assert s.get_last_n("BTC", "15m", 5)[0]["timestamp"] == 1000


def test_unknown_coin_ignored():
    s = CandleStore(["BTC"])
    s.update("XXX", "15m", _c(1000))       # ne doit pas lever
    assert s.get_last_n("XXX", "15m", 5) == []


def test_seed_many():
    s = CandleStore(["BTC"])
    s.seed_many("BTC", "1h", [_c(ts) for ts in (5000, 4000, 6000)])
    rows = s.get_last_n("BTC", "1h", 2)
    assert [r["timestamp"] for r in rows] == [5000, 6000]
