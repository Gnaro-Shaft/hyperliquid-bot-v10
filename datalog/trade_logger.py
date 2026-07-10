"""
TradeLogger (V10) — journal des trades avec LIEN vers le signal d'entrée.

Chaque trade porte :
  - signal_id           : id de l'évaluation qui a déclenché l'entrée
  - entry_features      : snapshot des features au moment de l'entrée (dict)
  - reason              : raison d'ouverture/fermeture (signal, trailing_stop,
                          signal_reverse, tp_sl_exchange, ...)
  - strategy_id/version : traçabilité dataset
  - coin, timestamp UTC ms

Le `context` (signal_id + snapshot) est fourni par le bot au moment de l'ordre
et fusionné ici — les traders (réel/paper) n'ont pas à connaître la stratégie.
"""

from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_TRADES,
    STRATEGY_ID, STRATEGY_VERSION, DEBUG,
)


class TradeLogger:
    def __init__(self, collection=MONGO_COLLECTION_TRADES, mongo_db=None):
        self.db = mongo_db
        self.col_name = collection
        self.ready = False
        if self.db is None and MONGO_URL:
            try:
                client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")
                self.db = client[MONGO_DB]
            except Exception as e:
                print(f"[TRADE_LOGGER][ERREUR] MongoDB: {e}")
        if self.db is not None:
            try:
                self.db[self.col_name].create_index(
                    [("coin", ASCENDING), ("timestamp", ASCENDING)])
                self.db[self.col_name].create_index("signal_id")
            except Exception as e:
                print(f"[TRADE_LOGGER] index: {e}")
            self.ready = True

    def log_trade(self, trade_info, context=None):
        """Log un trade (ouverture ou fermeture), enrichi du contexte signal.

        trade_info : pair, side, action (open/close/partial_close), entry_price,
                     exit_price, size, pnl, reason, ...
        context    : {signal_id, signal_score, raw_score, entry_features, ...}
        """
        trade = dict(trade_info)
        if context:
            for k, v in context.items():
                trade.setdefault(k, v)
        trade["timestamp"] = trade.get("timestamp") or int(
            datetime.now(timezone.utc).timestamp() * 1000)
        trade["datetime"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        trade.setdefault("coin", (trade.get("pair") or "").split("/")[0] or None)
        trade.setdefault("strategy_id", STRATEGY_ID)
        trade.setdefault("strategy_version", STRATEGY_VERSION)

        if not self.ready:
            if DEBUG:
                print(f"[TRADE_LOGGER][DRY] {trade.get('coin')} {trade.get('action')} "
                      f"pnl={trade.get('pnl')}")
            return trade
        try:
            self.db[self.col_name].insert_one(dict(trade))
        except Exception as e:
            print(f"[TRADE_LOGGER][ERREUR] insert: {e}")
        return trade
