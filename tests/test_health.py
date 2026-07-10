"""Tests de l'alerte « collector muet > 5 min » — critère d'acceptation :
aucun trou de données non détecté (alerte fonctionnelle et testée)."""

from collector.heartbeat import stale_heartbeats
from monitor.health import evaluate_health

NOW_MS = 1_700_000_000_000
THRESHOLDS = {"max_1m_age_s": 300, "max_15m_age_s": 2400, "max_consec_errors": 5}


def _hb(component, coin, age_s):
    return {"_id": f"{component}:{coin}", "component": component, "coin": coin,
            "last_write_ms": NOW_MS - int(age_s * 1000)}


def test_fresh_heartbeats_are_not_stale():
    hbs = [_hb("ws_candles", "BTC", 30), _hb("rest_funding_oi", "ETH", 200)]
    assert stale_heartbeats(hbs, NOW_MS, 300) == []


def test_silent_collector_detected_after_5_min():
    hbs = [_hb("ws_candles", "BTC", 30), _hb("ws_orderbook", "PEPE", 301)]
    stale = stale_heartbeats(hbs, NOW_MS, 300)
    assert len(stale) == 1
    assert stale[0]["component"] == "ws_orderbook"
    assert stale[0]["coin"] == "PEPE"
    assert stale[0]["age_s"] > 300


def test_one_dead_coin_does_not_hide_behind_others():
    """v8 n'avait qu'un is_alive global : un coin mort passait inaperçu tant
    qu'un autre émettait. V10 doit détecter le flux PAR coin."""
    hbs = [_hb("ws_candles", c, 10) for c in ["BTC", "ETH", "SOL"]]
    hbs.append(_hb("ws_candles", "TIA", 900))   # TIA muet depuis 15 min
    stale = stale_heartbeats(hbs, NOW_MS, 300)
    assert [s["coin"] for s in stale] == ["TIA"]


def test_evaluate_health_includes_stale_streams():
    metrics = {"ws_alive": True, "mongo_ok": True, "last_1m_age_s": 10,
               "last_15m_age_s": 100, "balance": 100.0, "consec_errors": 0,
               "stale_streams": [{"component": "rest_funding_oi", "coin": "DOGE",
                                  "age_s": 400.0, "key": "rest_funding_oi:DOGE"}]}
    problems = evaluate_health(metrics, THRESHOLDS)
    assert len(problems) == 1
    assert "rest_funding_oi" in problems[0]
    assert "DOGE" in problems[0]


def test_evaluate_health_all_green():
    metrics = {"ws_alive": True, "mongo_ok": True, "last_1m_age_s": 10,
               "last_15m_age_s": 100, "balance": 100.0, "consec_errors": 0,
               "stale_streams": []}
    assert evaluate_health(metrics, THRESHOLDS) == []


def test_evaluate_health_classic_problems_still_work():
    metrics = {"ws_alive": False, "mongo_ok": False, "last_1m_age_s": 400,
               "last_15m_age_s": 100, "balance": None, "consec_errors": 9,
               "stale_streams": []}
    problems = evaluate_health(metrics, THRESHOLDS)
    assert len(problems) == 5
