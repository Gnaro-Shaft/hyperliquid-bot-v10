"""
Sorties du moteur de signaux : document produit, ou sortie anticipée.

compute_signals a trois issues — un signal complet, un gate qui bloque, ou des
données insuffisantes — et V10 exige qu'elles produisent TOUTES une ligne au
même schéma : c'est le critère « aucun trou dans le dataset ». Les trois
chemins vivent donc ensemble ici plutôt que dispersés dans le moteur.

Les fonctions ne connaissent pas StrategyEngine : elles reçoivent le coin et le
logger, ce qui les rend vérifiables sans instancier quoi que ce soit.
"""

import pandas as pd

from config import LEVELS, DEBUG, SL_PCT, TP_PCT, MIN_TP_PCT
from utils.sizing import dynamic_sl_tp


def _f(val):
    """float(val) ou None si NaN/absent — colonnes propres pour Parquet."""
    try:
        return float(val) if pd.notna(val) else None
    except (TypeError, ValueError):
        return None


def tp_sl_dynamiques(atr_val, close, sl_mult, tp_mult):
    """TP et SL proportionnels à la volatilité, ajustés par le régime.

    Retourne (sl, tp, textes de debug). Sans ATR exploitable, on retombe sur
    les valeurs statiques de la configuration — et le debug le dit, pour qu'une
    ligne d'archive permette de savoir laquelle des deux voies a servi.
    """
    sl, tp = dynamic_sl_tp(atr_val, close, SL_PCT, TP_PCT, MIN_TP_PCT)
    sl *= sl_mult
    tp *= tp_mult

    if atr_val and close > 0:
        atr_pct = atr_val / close
        debug = {
            "atr": f"{atr_val:.4f} ({atr_pct*100:.4f}%)",
            "dynamic_sl": f"{sl*100:.3f}% (raw ATR*1.5={atr_pct*1.5*100:.4f}%)",
            "dynamic_tp": f"{tp*100:.3f}% (R:R={tp/sl:.1f}:1)",
        }
    else:
        debug = {
            "atr": "N/A",
            "dynamic_tp": f"{tp*100:.3f}% (static fallback)",
            "dynamic_sl": f"{sl*100:.3f}% (static fallback)",
        }
    return sl, tp, debug


@staticmethod


def features_depuis_bougie(row):
    """Vecteur de features à plat depuis la dernière bougie 15m enrichie.

    Utilisé par TOUS les chemins de sortie (signal, gate bloqué) pour que
    chaque ligne du dataset porte le même schéma de colonnes.
    """
    close = _f(row.get("close"))
    atr_v = _f(row.get("ATR"))
    high, low = _f(row.get("high")), _f(row.get("low"))
    return {
        "close_15m": close,
        "open_15m": _f(row.get("open")),
        "high_15m": high,
        "low_15m": low,
        "volume_15m": _f(row.get("volume")),
        "candle_range_pct": ((high - low) / close) if (high and low and close) else None,
        "ema9": _f(row.get("EMA9")),
        "ema21": _f(row.get("EMA21")),
        "ema9_slope": _f(row.get("EMA9_slope")),
        "rsi_14": _f(row.get("RSI")),
        "macd": _f(row.get("MACD")),
        "macd_signal": _f(row.get("MACD_signal")),
        "macd_hist": _f(row.get("MACD_hist")),
        "bb_upper": _f(row.get("BB_upper")),
        "bb_lower": _f(row.get("BB_lower")),
        "bb_pctb": _f(row.get("BB_pctB")),
        "bb_width": _f(row.get("BB_width")),
        "vwap": _f(row.get("VWAP")),
        "atr": atr_v,
        "atr_pct": (atr_v / close) if (atr_v and close) else None,
        "vol_ratio": _f(row.get("vol_ratio")),
        "adx_14": _f(row.get("ADX")),
        "plus_di": _f(row.get("PLUS_DI")),
        "minus_di": _f(row.get("MINUS_DI")),
    }


def sortie_gate_bloque(coin, signal_logger, debug, row, regime=None, mkt=None,
                       gate_reason="gate", trend_1h=None):
    """Retourne un signal neutre quand un filtre gate bloque.

    V10 : la ligne journalisée porte le MÊME schéma que les signaux passés
    (features complètes + sentiment carry-forward + régime).
    """
    mkt = mkt or {}
    rsi_val = row["RSI"] if pd.notna(row["RSI"]) else 50.0
    atr_val = row["ATR"] if pd.notna(row["ATR"]) else None

    features = features_depuis_bougie(row)
    features["close"] = _f(row.get("close"))

    eval_doc = signal_logger.log_evaluation(
        coin,
        candle_ts=row.get("timestamp"),
        gate_passed=False,
        gate_reason=gate_reason,
        score=0,
        raw_score=0,
        label=LEVELS[0]["label"],
        threshold_used=None,
        regime=regime,
        features=features,
        ctx=mkt,
        result_extra={"trend_1h": trend_1h},
        debug={**debug, "close": float(row["close"]), "RSI": float(rsi_val),
               "ATR": float(atr_val) if atr_val else None},
    )
    return {
        "score": 0,
        "raw_score": 0,
        "label": LEVELS[0]["label"],
        "color": LEVELS[0]["color"],
        "dynamic_tp": None,
        "dynamic_sl": None,
        "regime": regime,
        "is_squeeze": debug.get("bb_width_filter", "").endswith("BLOCKED"),
        "signal_id": eval_doc["signal_id"] if eval_doc else None,
        "eval_ts": eval_doc["timestamp"] if eval_doc else None,
        "debug": {
            **debug,
            "close": float(row["close"]),
            "RSI": float(rsi_val),
            "ATR": float(atr_val) if atr_val else None,
        }
    }


def sortie_neutre(coin, signal_logger, reason="", mkt=None):
    """Signal neutre faute de données. V10 : journalisé aussi (aucun trou)."""
    if DEBUG:
        print(f"[STRATEGY] Signal neutre : {reason}")
    eval_doc = signal_logger.log_evaluation(
        coin,
        candle_ts=None,
        gate_passed=False,
        gate_reason=f"insufficient_data: {reason}",
        score=0,
        raw_score=0,
        label=LEVELS[0]["label"],
        threshold_used=None,
        regime=None,
        features=None,
        ctx=mkt or {},
        debug={"reason": reason},
    )
    return {
        "score": 0,
        "raw_score": 0,
        "label": LEVELS[0]["label"],
        "color": LEVELS[0]["color"],
        "dynamic_tp": None,
        "dynamic_sl": None,
        "regime": None,
        "is_squeeze": False,
        "signal_id": eval_doc["signal_id"] if eval_doc else None,
        "eval_ts": eval_doc["timestamp"] if eval_doc else None,
        "debug": {"reason": reason}
    }
