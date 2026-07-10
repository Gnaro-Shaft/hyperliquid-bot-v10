"""
REST Collector V10 — Funding Rates + Open Interest (10 coins).
Poll l'API REST Hyperliquid toutes les DL_REST_INTERVAL secondes.

Évolutions vs v8 :
  - pousse chaque valeur fraîche dans le MarketContextStore (carry-forward),
    la stratégie ne dépend plus de la fenêtre du poll ;
  - heartbeat par coin (collection `collector_health`).
"""
import time
import requests
from pymongo import MongoClient, ASCENDING

from config import (
    COLLECT_PAIRS, MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI, MONGO_COLLECTION_HEARTBEATS,
    DL_REST_INTERVAL,
)
from collector.heartbeat import Heartbeat

COINS = [pair.split("/")[0] for pair in COLLECT_PAIRS]
API_URL = "https://api.hyperliquid.xyz/info"


class RestCollector:
    def __init__(self, context_store=None):
        self.mongo = None
        self.context_store = context_store
        if MONGO_URL:
            try:
                client = MongoClient(MONGO_URL)
                self.mongo = client[MONGO_DB]
                self._ensure_indexes()
                print("[REST_COLLECTOR] MongoDB connecte.")
            except Exception as e:
                print(f"[REST_COLLECTOR][ERREUR] MongoDB: {e}")
        self.heartbeat = Heartbeat(self.mongo, MONGO_COLLECTION_HEARTBEATS)
        self._running = True
        self._prev_oi = {}  # {coin: last_oi}

    def _ensure_indexes(self):
        try:
            self.mongo[MONGO_COLLECTION_FUNDING].create_index(
                [("coin", ASCENDING), ("timestamp", ASCENDING)], unique=True
            )
            self.mongo[MONGO_COLLECTION_OI].create_index(
                [("coin", ASCENDING), ("timestamp", ASCENDING)], unique=True
            )
        except Exception as e:
            print(f"[REST_COLLECTOR] Index: {e}")

    def stop(self):
        self._running = False

    def collect_loop(self):
        """Boucle principale — appeler dans un thread."""
        print(f"[REST_COLLECTOR] Demarrage (interval={DL_REST_INTERVAL}s, coins={COINS})")
        while self._running:
            try:
                self._fetch_and_store()
            except Exception as e:
                print(f"[REST_COLLECTOR][ERREUR] {e}")
            time.sleep(DL_REST_INTERVAL)

    def _fetch_and_store(self):
        """Appelle metaAndAssetCtxs et extrait funding + OI."""
        resp = requests.post(API_URL, json={"type": "metaAndAssetCtxs"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # data = [meta, [assetCtx1, assetCtx2, ...]]
        if not isinstance(data, list) or len(data) < 2:
            return

        meta = data[0]
        asset_ctxs = data[1]
        universe = meta.get("universe", [])

        now_ms = int(time.time() * 1000)

        for i, ctx in enumerate(asset_ctxs):
            if i >= len(universe):
                break
            coin = universe[i].get("name", "")
            if coin not in COINS:
                continue

            funding_rate = float(ctx.get("funding", 0))
            mark_price = float(ctx.get("markPx", 0))
            open_interest = float(ctx.get("openInterest", 0))
            premium = float(ctx.get("premium", 0) or 0)

            funding_doc = {
                "timestamp": now_ms,
                "coin": coin,
                "funding_rate": funding_rate,
                "premium": premium,
                "mark_price": mark_price,
            }

            prev_oi = self._prev_oi.get(coin, open_interest)
            oi_change = (open_interest - prev_oi) / prev_oi if prev_oi > 0 else 0
            self._prev_oi[coin] = open_interest

            oi_doc = {
                "timestamp": now_ms,
                "coin": coin,
                "open_interest": open_interest,
                "oi_change_pct": round(oi_change, 6),
                "mark_price": mark_price,
            }

            # Carry-forward : le store reçoit les valeurs fraîches immédiatement
            if self.context_store is not None:
                self.context_store.update_funding(
                    coin, funding_rate, ts_ms=now_ms,
                    premium=premium, mark_price=mark_price)
                self.context_store.update_oi(
                    coin, open_interest, oi_change, ts_ms=now_ms,
                    mark_price=mark_price)

            if self.mongo is not None:
                try:
                    self.mongo[MONGO_COLLECTION_FUNDING].update_one(
                        {"timestamp": now_ms, "coin": coin},
                        {"$set": funding_doc},
                        upsert=True
                    )
                    self.mongo[MONGO_COLLECTION_OI].update_one(
                        {"timestamp": now_ms, "coin": coin},
                        {"$set": oi_doc},
                        upsert=True
                    )
                    self.heartbeat.beat("rest_funding_oi", coin,
                                        max_age_s=DL_REST_INTERVAL * 2 + 60)
                except Exception as e:
                    print(f"[REST_COLLECTOR][ERREUR][MongoDB] {coin}: {e}")


if __name__ == "__main__":
    collector = RestCollector()
    try:
        collector.collect_loop()
    except KeyboardInterrupt:
        collector.stop()
        print("[REST_COLLECTOR] Arret.")
