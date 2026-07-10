"""
Matérialisation Mongo → Parquet (V10).

Exporte les collections time-series en fichiers Parquet partitionnés par
coin et par jour UTC :
    data/parquet/<collection>/coin=<COIN>/date=<YYYY-MM-DD>.parquet

Idempotent : chaque partition (jour, coin) est réécrite entièrement — relancer
l'export d'un jour donné écrase proprement la partition. Par défaut on exporte
hier + aujourd'hui (couvre les retards) ; --days N pour remonter plus loin.

Requêtable ensuite en DuckDB :
    SELECT * FROM read_parquet('data/parquet/signal_evaluations/**/*.parquet')
    WHERE coin = 'BTC' ORDER BY timestamp;

Usage :
  python -m scripts.export_parquet                # hier + aujourd'hui
  python -m scripts.export_parquet --days 7
  python -m scripts.export_parquet --collections signal_evaluations trades
"""

import argparse
import os
from datetime import datetime, timedelta, timezone

import polars as pl
from pymongo import MongoClient

from config import (
    MONGO_URL, MONGO_DB, PARQUET_DIR, COLLECT_PAIRS,
    MONGO_COLLECTION_SIGNALS, MONGO_COLLECTION_TRADES,
    MONGO_COLLECTION_1M, MONGO_COLLECTION_15M, MONGO_COLLECTION_1H,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI,
    MONGO_COLLECTION_ORDERBOOK, MONGO_COLLECTION_TRADES_MARKET,
    MONGO_COLLECTION_WHALE_POSITIONS, MONGO_COLLECTION_LIQ_CLUSTERS,
    MONGO_COLLECTION_WHALE_FLOWS, MONGO_COLLECTION_DECISIONS,
    MONGO_COLLECTION_AGENT_OUTPUTS, MONGO_COLLECTION_PAPER_TRADES,
)

DEFAULT_COLLECTIONS = [
    MONGO_COLLECTION_SIGNALS, MONGO_COLLECTION_TRADES,
    MONGO_COLLECTION_1M, MONGO_COLLECTION_15M, MONGO_COLLECTION_1H,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI,
    MONGO_COLLECTION_ORDERBOOK, MONGO_COLLECTION_TRADES_MARKET,
    MONGO_COLLECTION_WHALE_POSITIONS, MONGO_COLLECTION_LIQ_CLUSTERS,
    MONGO_COLLECTION_WHALE_FLOWS, MONGO_COLLECTION_DECISIONS,
    MONGO_COLLECTION_AGENT_OUTPUTS, MONGO_COLLECTION_PAPER_TRADES,
]

# Champs dict/list → sérialisés en str pour rester colonnaires en Parquet
_STRINGIFY = ("debug", "entry_features", "clusters", "payload", "meta", "positions")


def _day_bounds_ms(day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _clean(doc):
    doc.pop("_id", None)
    for k in _STRINGIFY:
        if k in doc and not isinstance(doc[k], (str, int, float, bool, type(None))):
            doc[k] = repr(doc[k])
    # created_at datetime → iso (colonnes homogènes)
    ca = doc.get("created_at")
    if hasattr(ca, "isoformat"):
        doc["created_at"] = ca.isoformat()
    return doc


def export_partition(db, collection, coin, day, out_dir):
    """Exporte une partition (collection, coin, jour). Retourne le nb de lignes."""
    start_ms, end_ms = _day_bounds_ms(day)
    query = {"timestamp": {"$gte": start_ms, "$lt": end_ms}}
    if coin is not None:
        query["coin"] = coin
    docs = [_clean(d) for d in db[collection].find(query)]
    if not docs:
        return 0

    part_dir = os.path.join(out_dir, collection,
                            f"coin={coin if coin is not None else 'ALL'}")
    os.makedirs(part_dir, exist_ok=True)
    path = os.path.join(part_dir, f"date={day.isoformat()}.parquet")

    df = pl.from_dicts(docs, infer_schema_length=None)
    if "timestamp" in df.columns:
        df = df.sort("timestamp")
    df.write_parquet(path, compression="zstd")
    return len(docs)


def main():
    parser = argparse.ArgumentParser(description="Export Mongo → Parquet (V10)")
    parser.add_argument("--days", type=int, default=2,
                        help="Nb de jours à exporter en remontant depuis aujourd'hui")
    parser.add_argument("--collections", nargs="*", default=DEFAULT_COLLECTIONS)
    parser.add_argument("--out", default=PARQUET_DIR)
    args = parser.parse_args()

    if not MONGO_URL:
        raise SystemExit("MONGO_URL manquant")
    db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]
    coins = [p.split("/")[0] for p in COLLECT_PAIRS]

    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(args.days)]

    total = 0
    for collection in args.collections:
        col_total = 0
        # Les collections par-compte (flows) ou multi-coin (whale_positions,
        # agent_outputs globaux) sont aussi partitionnées par coin quand le
        # champ existe ; le reste part dans coin=ALL.
        distinct_coins = set()
        try:
            distinct_coins = set(db[collection].distinct("coin")) - {None}
        except Exception:
            pass
        targets = sorted(c for c in distinct_coins if c in coins or c == "ALL") or [None]
        for day in days:
            for coin in targets:
                n = export_partition(db, collection, coin, day, args.out)
                col_total += n
        total += col_total
        print(f"  {collection:24s} → {col_total} lignes")
    print(f"\nExport terminé : {total} lignes → {args.out}")


if __name__ == "__main__":
    main()
