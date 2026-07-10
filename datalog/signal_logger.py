"""
SignalLogger (V10) — journalise CHAQUE évaluation de signal, pas seulement les
trades. Une ligne = un vecteur de features complet + le sentiment carry-forward
(toujours rempli une fois le store amorcé) + le régime, horodaté UTC ms.

Différences clés vs v8 :
  - INSERT (une ligne par évaluation, signal_id unique) au lieu d'un upsert par
    bougie qui écrasait l'historique intra-candle ;
  - le sentiment vient du MarketContextStore (dernière valeur connue + âge),
    plus jamais de None parce que le poll REST date de > fenêtre ;
  - le régime est présent sur CHAQUE ligne, y compris gate bloqué / neutre ;
  - colonnes à plat (Parquet/DuckDB-friendly) + debug brut conservé.

Clé de jointure : (coin, timestamp). Lien trades : signal_id.
"""

import uuid
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_SIGNALS,
    STRATEGY_ID, STRATEGY_VERSION, DEBUG,
)


def _utc_now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def new_signal_id():
    return uuid.uuid4().hex


def build_signal_doc(coin, *, now_ms=None, candle_ts=None, gate_passed, gate_reason=None,
                     score=0, raw_score=0, label=None, threshold_used=None,
                     regime=None, features=None, ctx=None, result_extra=None, debug=None):
    """Construit la ligne d'évaluation (fonction pure, testable sans I/O).

    features : dict à plat des indicateurs (close, ema9, rsi_14, atr_pct, ...)
    ctx      : sortie de MarketContextStore.get_context (sentiment + âges)
    result_extra : champs additionnels du résultat moteur (dynamic_tp, trend_1h...)
    """
    now_ms = now_ms or _utc_now_ms()
    doc = {
        "signal_id": new_signal_id(),
        "timestamp": now_ms,                       # évaluation, UTC ms
        "datetime": datetime.fromtimestamp(now_ms / 1000, timezone.utc)
                    .strftime("%Y-%m-%d %H:%M:%S"),
        "candle_ts": int(candle_ts) if candle_ts is not None else None,  # bougie 15m source
        "coin": coin,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        # décision
        "score": score,                            # niveau -2..+2
        "signal_level": score,                     # alias rétrocompat analyses v8
        "raw_score": raw_score,                    # score brut -17..+17
        "label": label,
        "gate_passed": bool(gate_passed),
        "gate_reason": gate_reason,
        "threshold_used": threshold_used,
        # régime — TOUJOURS présent (critère d'acceptation V10)
        "regime": regime,
    }

    for key, val in (features or {}).items():
        doc[key] = val

    # Sentiment carry-forward + âges — TOUJOURS joints (critère d'acceptation V10)
    for key in ("funding_rate", "funding_slope", "funding_age_ms",
                "open_interest", "oi_change_pct", "oi_trend_30m", "oi_age_ms",
                "ob_imbalance", "ob_imbalance_avg", "spread_pct", "ob_depth_ratio",
                "bid_depth_5", "ask_depth_5", "ob_age_ms"):
        doc[key] = (ctx or {}).get(key)

    for key, val in (result_extra or {}).items():
        doc[key] = val

    if debug is not None:
        doc["debug"] = debug                       # brut conservé (hygiène : brut + dérivé)

    return doc


class SignalLogger:
    def __init__(self, mongo_db=None):
        self.db = mongo_db
        self.ready = False
        if self.db is None and MONGO_URL:
            try:
                client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")
                self.db = client[MONGO_DB]
            except Exception as e:
                print(f"[SIGNAL_LOGGER][ERREUR] MongoDB: {e}")
        if self.db is not None:
            try:
                self.db[MONGO_COLLECTION_SIGNALS].create_index(
                    [("coin", ASCENDING), ("timestamp", ASCENDING)])
                self.db[MONGO_COLLECTION_SIGNALS].create_index("signal_id", unique=True)
                self.ready = True
            except Exception as e:
                print(f"[SIGNAL_LOGGER] index: {e}")
                self.ready = True   # index best-effort, l'écriture reste possible

    def log_evaluation(self, coin, **kwargs):
        """Écrit une ligne d'évaluation. Retourne le doc (avec signal_id) ou None."""
        doc = build_signal_doc(coin, **kwargs)
        if not self.ready:
            if DEBUG:
                print(f"[SIGNAL_LOGGER][DRY] {coin} score={doc['score']} raw={doc['raw_score']}")
            return doc
        try:
            self.db[MONGO_COLLECTION_SIGNALS].insert_one(dict(doc))
        except Exception as e:
            print(f"[SIGNAL_LOGGER][ERREUR] insert {coin}: {e}")
        return doc
