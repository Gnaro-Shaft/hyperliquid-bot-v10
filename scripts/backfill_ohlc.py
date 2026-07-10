"""
Backfill OHLC depuis l'API REST Hyperliquid (candleSnapshot).

Le moteur exige 50 bougies 15m (~12,5 h) avant de produire un signal : sans
backfill, chaque démarrage à froid (nouveau déploiement, nouvelle base) crée
une zone morte de 12 h. Ce script amorce ohlc_1m / ohlc_15m / ohlc_1h depuis
l'historique REST, avec le MÊME schéma de document que le WS collector
(upsert sur (coin, timestamp) → cohabite sans conflit avec le live).

À lancer une fois après un démarrage à froid, ou en cron de rattrapage après
un trou détecté par coverage_report :
  python -m scripts.backfill_ohlc            # 1m: 24h, 15m: 7j, 1h: 30j
  python -m scripts.backfill_ohlc --coins BTC ETH
"""

import argparse
import time
from datetime import datetime, timezone

import requests
from pymongo import MongoClient, UpdateOne

from config import (
    MONGO_URL, MONGO_DB, COLLECT_PAIRS,
    MONGO_COLLECTION_1M, MONGO_COLLECTION_15M, MONGO_COLLECTION_1H,
)

API_URL = "https://api.hyperliquid.xyz/info"

# (interval, collection, profondeur par défaut en heures)
PLANS = [
    ("1m", MONGO_COLLECTION_1M, 24),
    ("15m", MONGO_COLLECTION_15M, 7 * 24),
    ("1h", MONGO_COLLECTION_1H, 30 * 24),
]


def fetch_candles(coin, interval, start_ms, end_ms):
    """candleSnapshot → liste de bougies au schéma du WS collector."""
    resp = requests.post(API_URL, json={
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval,
                "startTime": start_ms, "endTime": end_ms},
    }, timeout=15)
    resp.raise_for_status()
    out = []
    for c in resp.json():
        minute = datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        out.append({
            "timestamp": c["t"],
            "timestamp_end": c["T"],
            "minute": minute,
            "coin": c["s"],
            "interval": c["i"],
            "open": float(c["o"]),
            "high": float(c["h"]),
            "low": float(c["l"]),
            "close": float(c["c"]),
            "volume": float(c["v"]),
            "n": int(c["n"]),
            "backfilled": True,          # traçabilité brut/dérivé
        })
    return out


def main():
    parser = argparse.ArgumentParser(description="Backfill OHLC Hyperliquid → Mongo")
    parser.add_argument("--coins", nargs="*",
                        default=[p.split("/")[0] for p in COLLECT_PAIRS])
    args = parser.parse_args()

    if not MONGO_URL:
        raise SystemExit("MONGO_URL manquant")
    db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)[MONGO_DB]
    now_ms = int(time.time() * 1000)

    for interval, col_name, hours in PLANS:
        start_ms = now_ms - hours * 3600 * 1000
        total = 0
        for coin in args.coins:
            try:
                candles = fetch_candles(coin, interval, start_ms, now_ms)
                if candles:
                    ops = [UpdateOne({"timestamp": c["timestamp"], "coin": coin},
                                     {"$setOnInsert": c}, upsert=True)
                           for c in candles]
                    res = db[col_name].bulk_write(ops, ordered=False)
                    total += res.upserted_count
                time.sleep(0.25)   # courtoisie rate-limit
            except Exception as e:
                print(f"  [{coin}][{interval}] ERREUR: {e}")
        print(f"{col_name:10s} ({hours}h) → {total} bougies insérées")


if __name__ == "__main__":
    main()
