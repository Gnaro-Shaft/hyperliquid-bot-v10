"""
StrategyEngine V10 — MOTEUR DE DÉCISION CONSERVÉ de v8 (système de feux).

Le scoring pondéré multi-indicateurs, les niveaux -2..+2, les gates ADX/BB/1h
et les TP/SL dynamiques sont IDENTIQUES à v8. Seule la PLOMBERIE change :

  - le contexte marché (funding/OI/orderbook) vient du MarketContextStore
    (carry-forward : dernière valeur connue + âge, jamais un vide) au lieu de
    requêtes Mongo fenêtrées qui laissaient des None ;
  - CHAQUE évaluation est journalisée par le SignalLogger (vecteur de features
    complet + sentiment + régime), y compris gate bloqué et neutre ;
  - le résultat porte un `signal_id` pour lier les trades à leur signal.
"""

import pandas as pd
from utils.mongo import get_db
from datetime import datetime, timezone

from config import (
    MONGO_URL, MONGO_COLLECTION_1M, MONGO_COLLECTION_15M, MONGO_COLLECTION_1H,
    LEVELS, SL_PCT, TP_PCT, MIN_TP_PCT, DEBUG, SIGNAL_THRESHOLD_DEFAULT,
    REGIME_ADAPTIVE, REGIME_HIGH_VOL_ATR_PCT,
)
from strategy import scoring_rules
from strategy.indicators import (
    ema, rsi, macd, bollinger_bands, vwap, atr,
    bb_width, bb_percent_b, volume_ratio, ema_slope, adx
)
from datalog.signal_logger import SignalLogger
from utils.sizing import dynamic_sl_tp
from utils.regime import regime_preset

try:
    from ml.predictor import MLPredictor
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False

# Seuils ML (tunable)
ML_BLOCK_THRESHOLD  = 0.38  # En dessous → gate bloqué (signal très peu probable)
ML_PENALTY_THRESHOLD = 0.48  # En dessous → pénalité -1 (signal douteux)


def _f(val):
    """float(val) ou None si NaN/absent — colonnes propres pour Parquet."""
    try:
        return float(val) if pd.notna(val) else None
    except (TypeError, ValueError):
        return None


class StrategyEngine:
    def __init__(self, coin="BTC", context_store=None, signal_logger=None,
                 candle_store=None):
        self.mongo = get_db()
        self.coin = coin
        self.context_store = context_store
        self.candle_store = candle_store
        self.signal_logger = signal_logger or SignalLogger()

        # Filtre ML optionnel (chargé si le module/modèle existe)
        self.ml_predictor = None
        if _ML_AVAILABLE:
            try:
                pred = MLPredictor(coin=coin)
                if pred.is_available():
                    self.ml_predictor = pred
                    print(f"[STRATEGY] 🤖 Modèle ML activé pour {coin}")
            except Exception as e:
                print(f"[STRATEGY] ML non chargé ({coin}): {e}")

    def get_market_context(self):
        """Contexte marché via le MarketContextStore (carry-forward V10).

        Mêmes clés que v8 (funding_rate, funding_slope, oi_change_pct,
        oi_trend_30m, ob_imbalance, ob_imbalance_avg, spread_pct,
        ob_depth_ratio) + valeurs brutes et âges (*_age_ms).
        """
        if self.context_store is not None:
            return self.context_store.get_context(self.coin)
        # Fallback sans store (tests/scripts) : tout à None, comme v8 sans données
        return {
            "funding_rate": None, "funding_slope": None, "funding_age_ms": None,
            "open_interest": None, "oi_change_pct": None, "oi_trend_30m": None,
            "oi_age_ms": None,
            "ob_imbalance": None, "ob_imbalance_avg": None,
            "spread_pct": None, "ob_depth_ratio": None,
            "bid_depth_5": None, "ask_depth_5": None, "ob_age_ms": None,
        }

    def get_last_n_candles(self, n=100, tf="1m"):
        """Bougies via le CandleStore mémoire (V10) — Mongo en secours.

        v8 relisait Mongo à chaque évaluation (~3,5 GB/j de lecture Atlas →
        throttling M0 constaté le 14/07). Le cache est alimenté en temps réel
        par le WS ; en cas de trou (démarrage à froid), on requête Mongo UNE
        fois et on re-seed le cache — les cycles suivants restent en mémoire.
        """
        from collector.candle_store import LIMITS
        if self.candle_store is not None:
            want = min(n, LIMITS.get(tf, n))
            rows = self.candle_store.get_last_n(self.coin, tf, n)
            if len(rows) >= want:
                df = pd.DataFrame(rows)
                for c in ["open", "high", "low", "close", "volume"]:
                    df[c] = df[c].astype(float)
                return df

        if tf == "1m":
            col = MONGO_COLLECTION_1M
        elif tf == "15m":
            col = MONGO_COLLECTION_15M
        else:
            col = MONGO_COLLECTION_1H
        cursor = self.mongo[col].find({"coin": self.coin}).sort("timestamp", -1).limit(n)
        data = list(cursor)
        if self.candle_store is not None and data:
            self.candle_store.seed_many(self.coin, tf, data)   # auto-cicatrisation
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(reversed(data))
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df

    @staticmethod
    def _features_from_row(row):
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

    def compute_signals(self, score_threshold=None):
        """Scoring pondere multi-timeframe v8.2 — 15m comme timeframe principal.

        MOTEUR CONSERVÉ — cf. docstring v8 pour le détail des poids :
          Gates : ADX >= 25, BB width > 0.004, gate 1h contre-tendance.
          13 composantes de score, total ±17, normalisé en 5 niveaux [-2..+2].
          Seuil de trade : level ±2 (raw >= seuil auto-calibré, défaut 9).
        """
        # 15m = timeframe principal (tous les indicateurs)
        # 1m  = momentum court-terme uniquement (signal #9)
        df_15m = self.get_last_n_candles(150, "15m")
        df_1m  = self.get_last_n_candles(20, "1m")
        df_1h  = self.get_last_n_candles(30, "1h")
        mkt = self.get_market_context()

        if df_15m.empty or len(df_15m) < 50:
            return self._neutral(f"Pas assez de donnees 15m ({len(df_15m)}/50)", mkt)

        # === Indicateurs 15m (PRIMARY) ===
        df_15m["EMA9"] = ema(df_15m["close"], 9)
        df_15m["EMA21"] = ema(df_15m["close"], 21)
        df_15m["RSI"] = rsi(df_15m["close"], 14)
        df_15m["MACD"], df_15m["MACD_signal"], df_15m["MACD_hist"] = macd(df_15m["close"])
        df_15m["BB_upper"], df_15m["BB_mid"], df_15m["BB_lower"] = bollinger_bands(df_15m["close"])
        df_15m["VWAP"] = vwap(df_15m)
        df_15m["ATR"] = atr(df_15m)
        df_15m["BB_pctB"] = bb_percent_b(df_15m["close"], df_15m["BB_upper"], df_15m["BB_lower"])
        df_15m["BB_width"] = bb_width(df_15m["BB_upper"], df_15m["BB_lower"], df_15m["BB_mid"])
        df_15m["vol_ratio"] = volume_ratio(df_15m["volume"])
        df_15m["EMA9_slope"] = ema_slope(df_15m["EMA9"], 3)
        df_15m["ADX"], df_15m["PLUS_DI"], df_15m["MINUS_DI"] = adx(df_15m)

        row = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]

        score = 0
        debug = {}

        # === FILTRES GATE (anti-chop) ===
        adx_val = row["ADX"] if pd.notna(row["ADX"]) else 0.0
        bb_w = row["BB_width"] if pd.notna(row["BB_width"]) else 0.0
        is_squeeze = bb_w < 0.004
        is_trending = adx_val >= 25

        # Régime de marché — déterminé avant les gates (presets adaptatifs)
        atr_pct_now = (row["ATR"] / row["close"]) if pd.notna(row.get("ATR")) and row["close"] > 0 else None

        if REGIME_ADAPTIVE:
            preset = regime_preset(adx_val, bb_w, atr_pct_now,
                                   high_vol_atr=REGIME_HIGH_VOL_ATR_PCT)
            regime = preset["regime"]
            regime_threshold_adj = preset["threshold_adj"]
            regime_tp_mult = preset["tp_mult"]
            regime_sl_mult = preset["sl_mult"]
            regime_size_mult = preset["size_mult"]
            blocked = preset["blocked"]
        else:
            # Comportement legacy v8.4 (presets neutres)
            if adx_val >= 30:
                regime, regime_threshold_adj = "STRONG", 0
            elif adx_val >= 25:
                regime, regime_threshold_adj = "WEAK", 1
            else:
                regime, regime_threshold_adj = "RANGE", 0
            regime_tp_mult = regime_sl_mult = regime_size_mult = 1.0
            blocked = (adx_val < 25) or is_squeeze

        debug["adx"] = f"{adx_val:.1f} ({regime})"
        debug["bb_width_filter"] = f"{bb_w:.4f} ({'OK' if not is_squeeze else 'SQUEEZE — BLOCKED'})"

        if blocked:
            debug["gate"] = f"BLOCKED — {regime} (ADX={adx_val:.1f}, BBw={bb_w:.4f})"
            return self._gate_blocked(debug, row, regime=regime, mkt=mkt,
                                      gate_reason=f"regime:{regime}")

        debug["gate"] = f"PASSED ({regime})"

        # === Pré-calcul tendances multi-TF ===
        # 1m — momentum court-terme (entrée précise)
        confirms_bull_1m = confirms_bear_1m = False
        close_1m = None
        if not df_1m.empty and len(df_1m) >= 5:
            df_1m["EMA9"] = ema(df_1m["close"], 9)
            df_1m["EMA21"] = ema(df_1m["close"], 21)
            row_1m = df_1m.iloc[-1]
            close_1m = float(row_1m["close"])
            if pd.notna(row_1m["EMA9"]) and pd.notna(row_1m["EMA21"]):
                confirms_bull_1m = row_1m["EMA9"] > row_1m["EMA21"]
                confirms_bear_1m = row_1m["EMA9"] < row_1m["EMA21"]

        # 1h — trend filter (gate post-normalisation)
        trend_1h = "neutral"
        if not df_1h.empty and len(df_1h) >= 10:
            df_1h["EMA9"] = ema(df_1h["close"], 9)
            df_1h["EMA21"] = ema(df_1h["close"], 21)
            row_1h = df_1h.iloc[-1]
            if pd.notna(row_1h["EMA9"]) and pd.notna(row_1h["EMA21"]):
                trend_1h = "bull" if row_1h["EMA9"] > row_1h["EMA21"] else "bear"
        debug["trend_1h"] = trend_1h

        # === Scoring : 13 règles pondérées (voir strategy/scoring_rules.py) ===
        ema_age = scoring_rules.age_tendance(df_15m["EMA9"] > df_15m["EMA21"])
        ctx = scoring_rules.contexte(
            row=row, prev=prev, mkt=mkt, adx_val=adx_val,
            confirms_bull_1m=confirms_bull_1m, confirms_bear_1m=confirms_bear_1m,
            ema_age=ema_age)
        points, debug_regles = scoring_rules.scorer(ctx)
        score += points
        debug.update(debug_regles)
        # Réutilisés plus bas (document signal, gate ML) : le contexte en est la
        # source unique, pour qu'ils ne disparaissent plus quand les règles bougent.
        rsi_val, bb_pctb, vol_r = ctx["rsi_val"], ctx["bb_pctb"], ctx["vol_r"]
        funding, imbalance, oi_chg = ctx["funding"], ctx["imbalance"], ctx["oi_chg"]


        # === Normalisation [-2, +2] ===
        base_threshold = score_threshold if score_threshold is not None else SIGNAL_THRESHOLD_DEFAULT
        threshold = base_threshold + regime_threshold_adj
        debug["regime"] = f"{regime} (ADX={adx_val:.1f}, seuil±2={threshold})"
        if score >= threshold:
            level = 2
        elif score >= 4:
            level = 1
        elif score <= -threshold:
            level = -2
        elif score <= -4:
            level = -1
        else:
            level = 0

        # === Gate 1h : bloquer les trades contre-tendance horaire ===
        if level == 2 and trend_1h == "bear":
            debug["gate_1h"] = "BLOCKED — 1h BEARISH vs signal BULLISH"
            return self._gate_blocked(debug, row, regime=regime, mkt=mkt,
                                      gate_reason="gate_1h", trend_1h=trend_1h)
        elif level == -2 and trend_1h == "bull":
            debug["gate_1h"] = "BLOCKED — 1h BULLISH vs signal BEARISH"
            return self._gate_blocked(debug, row, regime=regime, mkt=mkt,
                                      gate_reason="gate_1h", trend_1h=trend_1h)
        else:
            debug["gate_1h"] = f"OK (trend_1h={trend_1h})"

        # === Gate ML (optionnel — uniquement si modèle entraîné disponible) ===
        if self.ml_predictor is not None and level in (2, -2):
            atr_pct_for_ml = (row["ATR"] / row["close"]) if pd.notna(row.get("ATR")) and row["close"] > 0 else 0.005
            ml_features = {
                "rsi_14":        float(rsi_val),
                "adx_14":        float(adx_val),
                "bb_width":      float(bb_w),
                "raw_score":     float(score),
                "signal_level":  float(level),
                "atr_pct":       float(atr_pct_for_ml),
                "funding_rate":  float(funding) if funding is not None else 0.0,
                "ob_imbalance":  float(imbalance) if imbalance is not None else 0.0,
                "oi_change_pct": float(oi_chg) if oi_chg is not None else 0.0,
                "bb_pctB":       float(bb_pctb),
            }
            ml_conf = self.ml_predictor.predict(ml_features)
            if ml_conf < ML_BLOCK_THRESHOLD:
                debug["gate_ml"] = f"BLOCKED — confidence={ml_conf:.3f} < {ML_BLOCK_THRESHOLD}"
                return self._gate_blocked(debug, row, regime=regime, mkt=mkt,
                                          gate_reason="gate_ml", trend_1h=trend_1h)
            elif ml_conf < ML_PENALTY_THRESHOLD:
                score -= 1
                debug["gate_ml"] = f"PENALTY — confidence={ml_conf:.3f} < {ML_PENALTY_THRESHOLD} (-1)"
                # Re-normaliser après pénalité (conserver l'ajustement régime)
                threshold = (score_threshold if score_threshold is not None else SIGNAL_THRESHOLD_DEFAULT) + regime_threshold_adj
                if score >= threshold:
                    level = 2
                elif score >= 4:
                    level = 1
                elif score <= -threshold:
                    level = -2
                elif score <= -4:
                    level = -1
                else:
                    level = 0
            else:
                debug["gate_ml"] = f"OK — confidence={ml_conf:.3f}"
        else:
            debug["gate_ml"] = "N/A (modèle non entraîné)" if self.ml_predictor is None else "N/A (signal ≠ ±2)"

        # === TP/SL dynamiques bases sur ATR ===
        atr_val = row["ATR"] if pd.notna(row["ATR"]) else None
        dynamic_sl, dynamic_tp = dynamic_sl_tp(atr_val, row["close"], SL_PCT, TP_PCT, MIN_TP_PCT)
        dynamic_sl *= regime_sl_mult
        dynamic_tp *= regime_tp_mult

        if atr_val and row["close"] > 0:
            atr_pct = atr_val / row["close"]
            raw_sl = atr_pct * 1.5
            debug["atr"] = f"{atr_val:.4f} ({atr_pct*100:.4f}%)"
            debug["dynamic_sl"] = f"{dynamic_sl*100:.3f}% (raw ATR*1.5={raw_sl*100:.4f}%)"
            debug["dynamic_tp"] = f"{dynamic_tp*100:.3f}% (R:R={dynamic_tp/dynamic_sl:.1f}:1)"
        else:
            debug["atr"] = "N/A"
            debug["dynamic_tp"] = f"{dynamic_tp*100:.3f}% (static fallback)"
            debug["dynamic_sl"] = f"{dynamic_sl*100:.3f}% (static fallback)"

        # === Info supplementaire pour debug ===
        ema9_slp = row["EMA9_slope"] if pd.notna(row["EMA9_slope"]) else 0.0

        # close affiché = 1m si disponible (prix temps-réel), sinon dernière bougie 15m
        display_close = close_1m if close_1m is not None else float(row["close"])

        result = {
            "score": level,
            "raw_score": score,
            "label": LEVELS[level]["label"],
            "color": LEVELS[level]["color"],
            "dynamic_tp": dynamic_tp,
            "dynamic_sl": dynamic_sl,
            "trend_1h": trend_1h,
            "trend_1m": "bull" if confirms_bull_1m else ("bear" if confirms_bear_1m else "neutral"),
            "regime": regime,
            "regime_size_mult": regime_size_mult,
            "ml_confidence": float(debug.get("gate_ml", "N/A").split("=")[-1].split(" ")[0])
                             if "confidence=" in debug.get("gate_ml", "") else None,
            "is_squeeze": is_squeeze,
            "debug": {
                **debug,
                "close": display_close,
                "close_15m": float(row["close"]),
                "EMA9": float(row["EMA9"]) if pd.notna(row["EMA9"]) else None,
                "EMA21": float(row["EMA21"]) if pd.notna(row["EMA21"]) else None,
                "RSI": float(rsi_val),
                "MACD": float(row["MACD"]) if pd.notna(row["MACD"]) else None,
                "MACD_signal": float(row["MACD_signal"]) if pd.notna(row["MACD_signal"]) else None,
                "BB_upper": float(row["BB_upper"]) if pd.notna(row["BB_upper"]) else None,
                "BB_lower": float(row["BB_lower"]) if pd.notna(row["BB_lower"]) else None,
                "BB_pctB": float(bb_pctb),
                "BB_width": float(bb_w),
                "VWAP": float(row["VWAP"]) if pd.notna(row["VWAP"]) else None,
                "ATR": float(atr_val) if atr_val else None,
                "atr_pct": float(atr_pct) if atr_val and row["close"] > 0 else 0.001,
                "candle_range_pct": (
                    float((row["high"] - row["low"]) / row["close"])
                    if pd.notna(row.get("high")) and pd.notna(row.get("low")) and row["close"] else None
                ),
                "vol_ratio": float(vol_r),
                "EMA9_slope": float(ema9_slp),
                "funding_rate": float(funding) if funding is not None else None,
                "oi_change_pct": float(oi_chg) if oi_chg is not None else None,
                "ob_imbalance": float(imbalance) if imbalance is not None else None,
                "spread_pct": mkt.get("spread_pct"),         # circuit breaker
                "ob_depth_ratio": mkt.get("ob_depth_ratio"), # liquidité
            }
        }

        # === Journalisation V10 : ligne complète à CHAQUE évaluation ===
        features = self._features_from_row(row)
        features["close"] = display_close
        features["ema_age_candles"] = int(ema_age)
        eval_doc = self.signal_logger.log_evaluation(
            self.coin,
            candle_ts=row.get("timestamp"),
            gate_passed=True,
            gate_reason=None,
            score=level,
            raw_score=score,
            label=result["label"],
            threshold_used=threshold,
            regime=regime,
            features=features,
            ctx=mkt,
            result_extra={
                "dynamic_tp": dynamic_tp,
                "dynamic_sl": dynamic_sl,
                "trend_1h": trend_1h,
                "trend_1m": result["trend_1m"],
                "regime_size_mult": regime_size_mult,
                "ml_confidence": result["ml_confidence"],
                "is_squeeze": bool(is_squeeze),
            },
            debug=result["debug"],
        )
        result["signal_id"] = eval_doc["signal_id"] if eval_doc else None
        result["eval_ts"] = eval_doc["timestamp"] if eval_doc else None

        return result

    def _gate_blocked(self, debug, row, regime=None, mkt=None, gate_reason="gate",
                      trend_1h=None):
        """Retourne un signal neutre quand un filtre gate bloque.

        V10 : la ligne journalisée porte le MÊME schéma que les signaux passés
        (features complètes + sentiment carry-forward + régime).
        """
        mkt = mkt or {}
        rsi_val = row["RSI"] if pd.notna(row["RSI"]) else 50.0
        atr_val = row["ATR"] if pd.notna(row["ATR"]) else None

        features = self._features_from_row(row)
        features["close"] = _f(row.get("close"))

        eval_doc = self.signal_logger.log_evaluation(
            self.coin,
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

    def _neutral(self, reason="", mkt=None):
        """Signal neutre faute de données. V10 : journalisé aussi (aucun trou)."""
        if DEBUG:
            print(f"[STRATEGY] Signal neutre : {reason}")
        eval_doc = self.signal_logger.log_evaluation(
            self.coin,
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


if __name__ == "__main__":
    engine = StrategyEngine(coin="BTC")
    result = engine.compute_signals()
    print(f"\nScore: {result['score']} (raw: {result['raw_score']}) | {result['label']} {result['color']}")
    if result.get("dynamic_tp"):
        rr = result["dynamic_tp"] / result["dynamic_sl"] if result["dynamic_sl"] else 0
        print(f"TP: {result['dynamic_tp']*100:.3f}% | SL: {result['dynamic_sl']*100:.3f}% | R:R = {rr:.1f}:1")
    print(f"Régime : {result.get('regime', 'N/A')} | signal_id : {result.get('signal_id')}")
    for k, v in result["debug"].items():
        print(f"  {k}: {v}")
