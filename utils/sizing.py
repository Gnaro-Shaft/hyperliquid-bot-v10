"""
Fonctions pures de dimensionnement et de TP/SL — extraites pour être testables
indépendamment du bot live (pas d'accès réseau / exchange / Mongo).

Le comportement reproduit EXACTEMENT celui des call-sites historiques :
  - main.py : _compute_size_factor()
  - strategy/strategy_engine.py : bloc TP/SL dynamique
"""


def size_factor(raw_score, dynamic_sl, sl_pct, pnl_today,
                daily_start_balance, max_daily_drawdown_pct):
    """Facteur de taille de position ∈ [0.3, 1.0].

    = signal_factor × vol_factor × risk_factor, borné [0.3, 1.0], arrondi 2 déc.
      - signal_factor : plus le signal est fort (raw_score), plus la taille monte
      - vol_factor    : risque dollar ~constant (taille ↓ si SL large = volatilité ↑)
      - risk_factor   : taille ↓ si le PnL du jour est déjà négatif
    """
    raw = abs(raw_score)
    dynamic_sl = dynamic_sl or sl_pct

    signal_factor = max(0.4, min(1.0, 0.6 + (raw - 10) * 0.08))
    vol_factor = max(0.3, min(1.0, sl_pct / dynamic_sl))

    start_bal = daily_start_balance or 1
    daily_limit = start_bal * max_daily_drawdown_pct
    if pnl_today < 0 and daily_limit > 0:
        risk_factor = max(0.4, 1.0 - (abs(pnl_today) / daily_limit) * 0.6)
    else:
        risk_factor = 1.0

    return round(max(0.3, min(1.0, signal_factor * vol_factor * risk_factor)), 2)


def dynamic_sl_tp(atr_val, close, sl_pct, tp_pct, min_tp_pct):
    """Retourne (dynamic_sl, dynamic_tp) en fraction du prix.

    Avec ATR : SL = clamp(ATR/close × 1.5, sl_pct/2, sl_pct×2), TP = max(SL×2, min_tp_pct).
    Sans ATR : fallback statique SL = sl_pct, TP = max(tp_pct, sl_pct×1.5).
    """
    dynamic_sl = sl_pct
    dynamic_tp = tp_pct
    if atr_val and close > 0:
        atr_pct = atr_val / close
        raw_sl = atr_pct * 1.5
        dynamic_sl = max(raw_sl, sl_pct * 0.5)
        dynamic_sl = min(dynamic_sl, sl_pct * 2)
        dynamic_tp = dynamic_sl * 2.0
        dynamic_tp = max(dynamic_tp, min_tp_pct)
    else:
        dynamic_tp = max(tp_pct, sl_pct * 1.5)
    return dynamic_sl, dynamic_tp
