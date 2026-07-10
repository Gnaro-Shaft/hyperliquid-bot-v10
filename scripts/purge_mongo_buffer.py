"""
Purge du buffer Mongo (V10) — à exécuter sur homeserv01 APRÈS l'export Parquet.

Contexte : le cluster Atlas est un tier partagé (M0 512MB) ; à ~60-80 MB/jour,
il serait plein en une semaine. L'architecture V10 traite Mongo comme un BUFFER
ROULANT (le bot n'a besoin que de quelques jours : 150 bougies 15m ≈ 37h) et le
Parquet sur homeserv01 comme l'ARCHIVE de référence.

Garde-fou anti-trou (critère V10 « aucune perte non détectée ») : un jour n'est
purgé d'une collection QUE si sa partition Parquet du jour existe ET contient
au moins autant de lignes que Mongo pour ce jour. Sinon : jour conservé,
warning, exit code 1 (le cron le remonte dans purge.log, le watchdog Telegram
couvre déjà la panne d'export via bot_status/heartbeats).

Usage :
  python -m scripts.purge_mongo_buffer                # garde 4 jours
  python -m scripts.purge_mongo_buffer --keep-days 7
  python -m scripts.purge_mongo_buffer --dry-run
"""

import argparse
import glob
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# Collections volumineuses purgées (buffer roulant).
# ohlc_15m / ohlc_1h / funding / OI restent entiers : quelques MB sur 3 mois,
# et le moteur lit 150×15m + 30×1h en continu.
PURGE_COLLECTIONS = [
    "signal_evaluations",
    "orderbook_snapshots",
    "market_trades",
    "whale_positions",
    "liquidation_clusters",
    "ohlc_1m",              # le moteur n'utilise que les 20 dernières bougies 1m
]


def day_bounds_ms(day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def parquet_day_count(parquet_dir, collection, day):
    """Nb de lignes archivées en Parquet pour (collection, jour), toutes
    partitions coin confondues. None si aucun fichier."""
    pattern = os.path.join(parquet_dir, collection, "coin=*", f"date={day.isoformat()}.parquet")
    files = glob.glob(pattern)
    if not files:
        return None
    import polars as pl
    return sum(pl.scan_parquet(f).select(pl.len()).collect().item() for f in files)


def purge_day(db, collection, day, parquet_dir, dry_run=False):
    """Purge un (collection, jour) si l'archive Parquet le couvre.

    Retourne (deleted: int, problem: str|None).
    """
    start_ms, end_ms = day_bounds_ms(day)
    query = {"timestamp": {"$gte": start_ms, "$lt": end_ms}}
    mongo_n = db[collection].count_documents(query)
    if mongo_n == 0:
        return 0, None

    pq_n = parquet_day_count(parquet_dir, collection, day)
    if pq_n is None:
        return 0, f"{collection}/{day}: pas de partition Parquet ({mongo_n} docs conservés)"
    if pq_n < mongo_n:
        return 0, (f"{collection}/{day}: Parquet incomplet ({pq_n} < {mongo_n}) "
                   f"— relancer export_parquet sur ce jour")

    if dry_run:
        return mongo_n, None
    res = db[collection].delete_many(query)
    return res.deleted_count, None


def main():
    parser = argparse.ArgumentParser(description="Purge buffer Mongo (archive = Parquet)")
    parser.add_argument("--keep-days", type=int, default=4,
                        help="Jours conservés dans Mongo (défaut 4 ; le moteur a besoin de ~2)")
    parser.add_argument("--max-days-back", type=int, default=30,
                        help="Profondeur max balayée en arrière")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from pymongo import MongoClient
    from config import MONGO_URL, MONGO_DB, PARQUET_DIR

    if not MONGO_URL:
        print("MONGO_URL manquant"); sys.exit(2)
    db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)[MONGO_DB]

    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=args.keep_days)
    days = [cutoff - timedelta(days=i) for i in range(args.max_days_back)]

    total, problems = 0, []
    for collection in PURGE_COLLECTIONS:
        col_deleted = 0
        for day in days:
            deleted, problem = purge_day(db, collection, day, PARQUET_DIR,
                                         dry_run=args.dry_run)
            col_deleted += deleted
            if problem:
                problems.append(problem)
        if col_deleted:
            tag = "[DRY-RUN] " if args.dry_run else ""
            print(f"{tag}{collection:22s} → {col_deleted} docs purgés (< {cutoff})")
        total += col_deleted

    stats = db.command("dbStats")
    print(f"\nTotal: {total} docs {'purgeables' if args.dry_run else 'purgés'} | "
          f"base: {stats['dataSize']/1e6:.1f} MB")
    if problems:
        print("\n⚠️ Jours NON purgés (archive incomplète) :")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)


if __name__ == "__main__":
    main()
