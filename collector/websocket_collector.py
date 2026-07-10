"""
WebSocketCollector V10 — collecte multi-coins (10) : candles 1m/15m/1h,
snapshots orderbook, trades marché agrégés par minute.

Évolutions vs v8 :
  - pousse chaque donnée fraîche dans le MarketContextStore (carry-forward) ;
  - heartbeat par flux et par coin (collection `collector_health`) →
    l'alerte « collector muet > 5 min » est par-coin, pas globale ;
  - seuil « gros trade » en NOTIONNEL USD (coin-agnostique) ;
  - is_alive par coin (un coin mort ne masque plus les autres) ;
  - plus de CSV (le stockage V10 = Mongo → Parquet).
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

import websockets
from pymongo import MongoClient, ASCENDING

from config import (
    COLLECT_PAIRS, MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_1M, MONGO_COLLECTION_15M, MONGO_COLLECTION_1H,
    MONGO_COLLECTION_ORDERBOOK, MONGO_COLLECTION_TRADES_MARKET,
    MONGO_COLLECTION_HEARTBEATS,
    DL_SNAPSHOT_INTERVAL, LARGE_TRADE_USD,
)
from collector.heartbeat import Heartbeat

COINS = [pair.split("/")[0] for pair in COLLECT_PAIRS]
PING_INTERVAL = 30
WS_URL = "wss://api.hyperliquid.xyz/ws"

# Hyperliquid ferme TOUTE la connexion WS, sans message d'erreur, si UNE
# souscription référence un coin inconnu (constaté le 10/07/2026 avec PEPE,
# qui s'appelle kPEPE sur HL). On shard donc les coins sur plusieurs
# connexions : un coin invalide/retiré du listing ne tue que son shard de 5,
# pas la collecte des 10. (50 souscriptions sur une connexion passent sinon.)
WS_SHARD_SIZE = 5
SUBSCRIBE_THROTTLE_S = 0.05   # petite pause entre les sends de souscription


class WebSocketCollector:
    def __init__(self, context_store=None):
        self.mongo = None
        self.context_store = context_store
        if MONGO_URL:
            try:
                client = MongoClient(MONGO_URL)
                self.mongo = client[MONGO_DB]
                self._mongo_connected = True
                self._ensure_indexes()
            except Exception as e:
                self._mongo_connected = False
                print(f"[COLLECTOR][ERREUR] MongoDB: {e}")
        else:
            self._mongo_connected = False

        self.heartbeat = Heartbeat(self.mongo, MONGO_COLLECTION_HEARTBEATS)

        self._live_prices = {}                    # {coin: dernier prix live}
        self._last_candle_time = defaultdict(float)  # {coin: epoch dernière bougie}
        self.last_candle_time = 0                 # global (rétrocompat is_alive)
        self._running = True

        self._last_ob_snapshot = defaultdict(float)  # {coin: epoch}
        self._trade_buffer = defaultdict(lambda: {
            "buy_volume": 0.0, "sell_volume": 0.0,
            "buy_notional": 0.0, "sell_notional": 0.0,
            "trade_count": 0, "buy_count": 0, "sell_count": 0,
            "large_trades": 0, "large_notional": 0.0, "minute_ts": 0,
        })

    def _ensure_indexes(self):
        """Index (coin, timestamp) sur TOUTES les collections time-series.

        v8 n'indexait pas les collections OHLC alors que la stratégie les
        requête à chaque évaluation — indispensable avec 10 coins.
        """
        specs = [
            (MONGO_COLLECTION_1M, True),
            (MONGO_COLLECTION_15M, True),
            (MONGO_COLLECTION_1H, True),
            (MONGO_COLLECTION_ORDERBOOK, False),
            (MONGO_COLLECTION_TRADES_MARKET, True),
        ]
        for col, unique in specs:
            try:
                self.mongo[col].create_index(
                    [("coin", ASCENDING), ("timestamp", ASCENDING)], unique=unique)
            except Exception as e:
                print(f"[COLLECTOR] Index {col}: {e}")
        try:
            # Orderbook — TTL 90 jours (fenêtre de collecte V10 = ~3 mois)
            self.mongo[MONGO_COLLECTION_ORDERBOOK].create_index(
                "created_at", expireAfterSeconds=90 * 86400)
        except Exception as e:
            print(f"[COLLECTOR] Index TTL orderbook: {e}")

    def get_live_price(self, coin):
        """Retourne le dernier prix live pour un coin (midprice OB ou close candle)."""
        return self._live_prices.get(coin, 0)

    @property
    def is_alive(self):
        """Au moins un coin a émis une bougie il y a < 5 min."""
        if self.last_candle_time == 0:
            return False
        return (time.time() - self.last_candle_time) < 300

    def coin_alive(self, coin, max_age_s=300):
        last = self._last_candle_time.get(coin, 0)
        return last > 0 and (time.time() - last) < max_age_s

    def stop(self):
        self._running = False

    async def subscribe(self, ws, coins):
        """Subscribe aux channels candles + l2Book + trades pour un shard de coins."""
        for coin in coins:
            for tf in ["1m", "15m", "1h"]:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "candle", "coin": coin, "interval": tf}
                }))
                await asyncio.sleep(SUBSCRIBE_THROTTLE_S)
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": coin, "nSigFigs": 5}
            }))
            await asyncio.sleep(SUBSCRIBE_THROTTLE_S)
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin}
            }))
            await asyncio.sleep(SUBSCRIBE_THROTTLE_S)
        print(f"[COLLECTOR] Abonne: {', '.join(coins)} (candles + orderbook + trades)")

    async def process_message(self, message):
        try:
            msg = json.loads(message)
            channel = msg.get("channel", "")
            data = msg.get("data", {})

            if channel == "candle" and isinstance(data, dict):
                self.handle_candle(data)
            elif channel == "l2Book" and isinstance(data, dict):
                self.handle_orderbook(data)
            elif channel == "trades" and isinstance(data, list):
                for trade in data:
                    self.handle_market_trade(trade)
        except Exception as e:
            print(f"[COLLECTOR][ERREUR] process_message: {e}")

    # ────────────────────── CANDLES ──────────────────────

    def handle_candle(self, candle):
        tf = candle["i"]
        coin = candle["s"]
        minute = datetime.fromtimestamp(candle["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        bougie = {
            "timestamp": candle["t"],
            "timestamp_end": candle["T"],
            "minute": minute,
            "coin": coin,
            "interval": tf,
            "open": float(candle["o"]),
            "high": float(candle["h"]),
            "low": float(candle["l"]),
            "close": float(candle["c"]),
            "volume": float(candle["v"]),
            "n": int(candle["n"]),
        }

        now = time.time()
        self.last_candle_time = now
        self._last_candle_time[coin] = now
        self._live_prices[coin] = bougie["close"]

        if self._mongo_connected:
            col = MONGO_COLLECTION_1M if tf == "1m" else (MONGO_COLLECTION_15M if tf == "15m" else MONGO_COLLECTION_1H)
            try:
                self.mongo[col].update_one(
                    {"timestamp": bougie["timestamp"], "coin": coin},
                    {"$set": bougie},
                    upsert=True
                )
                self.heartbeat.beat("ws_candles", coin)
            except Exception as e:
                print(f"[COLLECTOR][ERREUR][MongoDB] {e}")

    # ────────────────────── ORDERBOOK ──────────────────────

    def handle_orderbook(self, data):
        """Snapshot orderbook toutes les DL_SNAPSHOT_INTERVAL s + push ContextStore."""
        coin = data.get("coin", "")
        if not coin:
            return

        now = time.time()
        if now - self._last_ob_snapshot[coin] < DL_SNAPSHOT_INTERVAL:
            return

        self._last_ob_snapshot[coin] = now

        levels = data.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []

        if not bids or not asks:
            return

        best_bid = float(bids[0].get("px", 0))
        best_ask = float(asks[0].get("px", 0))
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2

        if mid > 0:
            self._live_prices[coin] = mid

        bid_depth = sum(float(b.get("sz", 0)) * float(b.get("px", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("sz", 0)) * float(a.get("px", 0)) for a in asks[:5])
        total_depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0
        spread_pct = spread / mid if mid > 0 else 0
        ts_ms = int(now * 1000)

        snapshot = {
            "timestamp": ts_ms,
            "coin": coin,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "bid_depth_5": round(bid_depth, 2),
            "ask_depth_5": round(ask_depth, 2),
            "imbalance": round(imbalance, 4),
            "created_at": datetime.now(timezone.utc),
        }

        # Carry-forward : le store reçoit la valeur fraîche immédiatement
        if self.context_store is not None:
            self.context_store.update_orderbook(
                coin, imbalance, spread_pct, bid_depth, ask_depth, ts_ms=ts_ms)

        if self._mongo_connected:
            try:
                self.mongo[MONGO_COLLECTION_ORDERBOOK].insert_one(snapshot)
                self.heartbeat.beat("ws_orderbook", coin)
            except Exception as e:
                print(f"[COLLECTOR][ERREUR][OB] {e}")

    # ────────────────────── MARKET TRADES ──────────────────────

    def handle_market_trade(self, trade):
        """Agrege les trades par minute (volumes, notionnels, gros trades USD)."""
        coin = trade.get("coin", "")
        if not coin:
            return

        size = float(trade.get("sz", 0))
        price = float(trade.get("px", 0) or 0)
        notional = size * price
        side = trade.get("side", "").upper()
        ts = int(trade.get("time", time.time() * 1000))
        minute_ts = (ts // 60000) * 60000

        buf = self._trade_buffer[coin]

        if buf["minute_ts"] != 0 and buf["minute_ts"] != minute_ts:
            self._flush_trade_buffer(coin)

        buf["minute_ts"] = minute_ts
        buf["trade_count"] += 1

        if side == "B" or side == "BUY":
            buf["buy_volume"] += size
            buf["buy_notional"] += notional
            buf["buy_count"] += 1
        else:
            buf["sell_volume"] += size
            buf["sell_notional"] += notional
            buf["sell_count"] += 1

        # Gros trade = notionnel USD (coin-agnostique, V10)
        if notional >= LARGE_TRADE_USD:
            buf["large_trades"] += 1
            buf["large_notional"] += notional

    def _flush_trade_buffer(self, coin):
        """Ecrit le buffer de trades agregés dans MongoDB."""
        buf = self._trade_buffer[coin]
        if buf["trade_count"] == 0:
            return

        doc = {
            "timestamp": buf["minute_ts"],
            "coin": coin,
            "buy_volume": round(buf["buy_volume"], 6),
            "sell_volume": round(buf["sell_volume"], 6),
            "buy_notional": round(buf["buy_notional"], 2),
            "sell_notional": round(buf["sell_notional"], 2),
            "trade_count": buf["trade_count"],
            "buy_count": buf["buy_count"],
            "sell_count": buf["sell_count"],
            "large_trades": buf["large_trades"],
            "large_notional": round(buf["large_notional"], 2),
        }

        if self._mongo_connected:
            try:
                self.mongo[MONGO_COLLECTION_TRADES_MARKET].update_one(
                    {"timestamp": doc["timestamp"], "coin": coin},
                    {"$set": doc},
                    upsert=True
                )
                self.heartbeat.beat("ws_trades", coin)
            except Exception as e:
                print(f"[COLLECTOR][ERREUR][TRADES] {e}")

        self._trade_buffer[coin] = {
            "buy_volume": 0.0, "sell_volume": 0.0,
            "buy_notional": 0.0, "sell_notional": 0.0,
            "trade_count": 0, "buy_count": 0, "sell_count": 0,
            "large_trades": 0, "large_notional": 0.0, "minute_ts": 0,
        }

    # ────────────────────── LIFECYCLE ──────────────────────

    async def heartbeat_ws(self, ws):
        while self._running:
            try:
                await ws.ping()
            except Exception:
                break
            await asyncio.sleep(PING_INTERVAL)

    async def periodic_flush(self):
        """Flush les trade buffers periodiquement (au cas ou pas de nouveau trade)."""
        while self._running:
            await asyncio.sleep(60)
            for coin in COINS:
                self._flush_trade_buffer(coin)

    async def _collect_shard(self, coins):
        """Boucle connect/subscribe/read pour UN shard de coins.

        Chaque shard a sa propre connexion WS et reconnecte indépendamment :
        un shard qui tombe n'interrompt pas la collecte des autres coins.
        """
        label = f"{coins[0]}…{coins[-1]}" if len(coins) > 1 else coins[0]
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    print(f"[COLLECTOR] WebSocket connecte (shard {label}).")
                    await self.subscribe(ws, coins)
                    heartbeat_task = asyncio.create_task(self.heartbeat_ws(ws))
                    try:
                        async for message in ws:
                            if not self._running:
                                break
                            await self.process_message(message)
                    finally:
                        heartbeat_task.cancel()
            except Exception as e:
                print(f"[COLLECTOR][ERREUR] Deconnexion WebSocket (shard {label}): {e}")
                if self._running:
                    await asyncio.sleep(5)

    async def collect(self):
        """Lance une connexion WS par shard de WS_SHARD_SIZE coins + le flush."""
        shards = [COINS[i:i + WS_SHARD_SIZE] for i in range(0, len(COINS), WS_SHARD_SIZE)]
        print(f"[COLLECTOR] {len(COINS)} coins sur {len(shards)} connexions WS "
              f"(max {WS_SHARD_SIZE * 5} souscriptions/connexion)")
        flush_task = asyncio.create_task(self.periodic_flush())
        try:
            await asyncio.gather(*[self._collect_shard(shard) for shard in shards])
        finally:
            flush_task.cancel()


if __name__ == "__main__":
    collector = WebSocketCollector()
    try:
        asyncio.run(collector.collect())
    except KeyboardInterrupt:
        collector.stop()
        print("[COLLECTOR] Arret propre.")
