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
    LEVELS, DEBUG, SIGNAL_THRESHOLD_DEFAULT,
)
from strategy import gates, scoring_rules, signal_output
from strategy.indicators import (
    ema, rsi, macd, bollinger_bands, vwap, atr,
    bb_width, bb_percent_b, volume_ratio, ema_slope, adx
)
from datalog.signal_logger import SignalLogger

try:
    from ml.predictor import MLPredictor
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False

# Seuils ML (tunable)


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
        return signal_output.features_depuis_bougie(row)

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

        # === FILTRES GATE (anti-chop) et régime de marché ===
        adx_val = row["ADX"] if pd.notna(row["ADX"]) else 0.0
        bb_w = row["BB_width"] if pd.notna(row["BB_width"]) else 0.0
        atr_pct_now = (row["ATR"] / row["close"]) if pd.notna(row.get("ATR")) and row["close"] > 0 else None

        rg = gates.evaluer_regime(adx_val, bb_w, atr_pct_now)
        regime = rg["regime"]
        regime_threshold_adj = rg["threshold_adj"]
        regime_tp_mult, regime_sl_mult = rg["tp_mult"], rg["sl_mult"]
        regime_size_mult = rg["size_mult"]
        is_squeeze = rg["is_squeeze"]
        debug.update(rg["debug"])

        if rg["blocked"]:
            return self._gate_blocked(debug, row, regime=regime, mkt=mkt,
                                      gate_reason=f"regime:{regime}")

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
        level = gates.niveau_depuis_score(score, threshold)

        # === Gate 1h : bloquer les trades contre-tendance horaire ===
        bloque_1h, texte_1h = gates.gate_1h(level, trend_1h)
        debug["gate_1h"] = texte_1h
        if bloque_1h:
            return self._gate_blocked(debug, row, regime=regime, mkt=mkt,
                                      gate_reason="gate_1h", trend_1h=trend_1h)

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
            verdict, debug["gate_ml"] = gates.verdict_ml(ml_conf)
            if verdict == "blocked":
                return self._gate_blocked(debug, row, regime=regime, mkt=mkt,
                                          gate_reason="gate_ml", trend_1h=trend_1h)
            if verdict == "penalty":
                score -= 1
                # Renormaliser après pénalité — le seuil régime reste le même
                level = gates.niveau_depuis_score(score, threshold)
        else:
            debug["gate_ml"] = "N/A (modèle non entraîné)" if self.ml_predictor is None else "N/A (signal ≠ ±2)"

        # === TP/SL dynamiques basés sur ATR ===
        atr_val = row["ATR"] if pd.notna(row["ATR"]) else None
        atr_pct = (atr_val / row["close"]) if (atr_val and row["close"] > 0) else None
        dynamic_sl, dynamic_tp, debug_tpsl = signal_output.tp_sl_dynamiques(
            atr_val, row["close"], regime_sl_mult, regime_tp_mult)
        debug.update(debug_tpsl)

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

    def _gate_blocked(self, debug, row, **kw):
        """Sortie anticipée quand un gate bloque — déléguée à signal_output."""
        return signal_output.sortie_gate_bloque(
            self.coin, self.signal_logger, debug, row, **kw)

    def _neutral(self, reason="", mkt=None):
        """Signal neutre faute de données — délégué à signal_output."""
        return signal_output.sortie_neutre(self.coin, self.signal_logger, reason, mkt)

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
