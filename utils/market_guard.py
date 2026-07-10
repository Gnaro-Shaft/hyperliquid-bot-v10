"""
Circuit breaker marché (v8.9) — fonction pure et testable.

Bloque les ENTRÉES pendant des conditions de marché extrêmes : volatilité
anormale, funding extrême, bougie énorme, spread trop large, liquidité orderbook
effondrée. Chaque métrique absente (None) est simplement ignorée.

spread_pct et ob_depth_ratio sont alimentés par la stratégie depuis les snapshots
orderbook (branchés en Axe B, 28/06/2026). ob_depth_ratio = depth courant / moyenne
récente → mesure coin-agnostique de la perte de liquidité.
"""


def market_circuit_breaker(metrics: dict, thresholds: dict):
    """Retourne (déclenché: bool, raisons: list[str]).

    metrics : {atr_pct, funding_rate, candle_range_pct, spread_pct}
    thresholds : {max_atr_pct, max_abs_funding, max_candle_range_pct, max_spread_pct}
    """
    reasons = []

    atr = metrics.get("atr_pct")
    if atr is not None and atr > thresholds["max_atr_pct"]:
        reasons.append(f"volatilité anormale (ATR {atr*100:.2f}%)")

    funding = metrics.get("funding_rate")
    if funding is not None and abs(funding) > thresholds["max_abs_funding"]:
        reasons.append(f"funding extrême ({funding*100:.3f}%)")

    crange = metrics.get("candle_range_pct")
    if crange is not None and crange > thresholds["max_candle_range_pct"]:
        reasons.append(f"bougie énorme ({crange*100:.2f}%)")

    spread = metrics.get("spread_pct")
    if spread is not None and spread > thresholds["max_spread_pct"]:
        reasons.append(f"spread trop large ({spread*100:.3f}%)")

    depth_ratio = metrics.get("ob_depth_ratio")
    min_ratio = thresholds.get("min_ob_depth_ratio")
    if depth_ratio is not None and min_ratio is not None and depth_ratio < min_ratio:
        reasons.append(f"liquidité orderbook effondrée (depth {depth_ratio:.2f}× la moyenne)")

    return (len(reasons) > 0, reasons)
