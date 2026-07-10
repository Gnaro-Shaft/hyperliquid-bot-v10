"""
Rapport de couverture du dataset V10 — critère d'acceptation :
colonnes clés (funding, OI, imbalance, régime, spread) remplies à ~100%.

Vérifie, par coin et sur une fenêtre glissante :
  1. le % de remplissage de chaque colonne clé de `signal_evaluations` ;
  2. le plus grand trou entre deux évaluations consécutives ;
  3. les minutes manquantes dans `ohlc_1m` (trous de collecte bougies).

Usage :
  python -m scripts.coverage_report --hours 24
  python -m scripts.coverage_report --hours 24 --json    # sortie machine
Exit code 1 si une colonne clé < --min-fill (défaut 95%) ou trou d'évaluation
> --max-eval-gap-min (défaut 5 min) → utilisable en cron/CI.
"""

import argparse
import json
import sys
import time

# Colonnes soumises au seuil d'acceptation (sentiment carry-forward + régime)
KEY_COLUMNS = [
    "funding_rate", "open_interest", "oi_change_pct",
    "ob_imbalance", "spread_pct", "regime",
]
# Colonnes suivies à titre informatif (features + dérivés)
INFO_COLUMNS = [
    "funding_slope", "oi_trend_30m", "ob_imbalance_avg", "ob_depth_ratio",
    "close_15m", "rsi_14", "adx_14", "atr_pct", "bb_width", "vwap",
    "trend_1h", "candle_ts",
]


def column_fill_rates(docs, columns):
    """Fonction PURE : % de docs où chaque colonne est non-null. {col: pct}."""
    n = len(docs)
    if n == 0:
        return {c: 0.0 for c in columns}
    return {
        c: round(100.0 * sum(1 for d in docs if d.get(c) is not None) / n, 2)
        for c in columns
    }


def max_gap_ms(timestamps):
    """Fonction PURE : plus grand écart entre timestamps consécutifs triés."""
    if len(timestamps) < 2:
        return None
    ts = sorted(timestamps)
    return max(b - a for a, b in zip(ts, ts[1:]))


def missing_minutes(candle_timestamps, window_start_ms, window_end_ms):
    """Fonction PURE : nb de minutes sans bougie 1m dans la fenêtre."""
    expected = set(range(int(window_start_ms // 60000), int(window_end_ms // 60000)))
    got = set(int(t // 60000) for t in candle_timestamps)
    return len(expected - got)


def run_report(db, coins, hours, min_fill, max_eval_gap_min, signals_col, ohlc_col):
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - hours * 3600 * 1000
    report = {"window_hours": hours, "generated_at": now_ms, "coins": {}, "ok": True}

    for coin in coins:
        docs = list(db[signals_col].find(
            {"coin": coin, "timestamp": {"$gte": start_ms}},
            projection={c: 1 for c in KEY_COLUMNS + INFO_COLUMNS + ["timestamp"]},
        ))
        entry = {
            "n_evaluations": len(docs),
            "key_columns": column_fill_rates(docs, KEY_COLUMNS),
            "info_columns": column_fill_rates(docs, INFO_COLUMNS),
        }

        gap = max_gap_ms([d["timestamp"] for d in docs])
        entry["max_eval_gap_min"] = round(gap / 60000, 2) if gap is not None else None

        candles = [d["timestamp"] for d in db[ohlc_col].find(
            {"coin": coin, "timestamp": {"$gte": start_ms}},
            projection={"timestamp": 1})]
        entry["candles_1m"] = len(candles)
        entry["missing_1m_minutes"] = missing_minutes(candles, start_ms, now_ms) \
            if candles else None

        # Verdict par coin
        problems = []
        if len(docs) == 0:
            problems.append("aucune évaluation")
        else:
            for c, pct in entry["key_columns"].items():
                if pct < min_fill:
                    problems.append(f"{c}={pct}% < {min_fill}%")
            if entry["max_eval_gap_min"] is not None and \
               entry["max_eval_gap_min"] > max_eval_gap_min:
                problems.append(f"trou d'évaluation {entry['max_eval_gap_min']}min")
        entry["problems"] = problems
        if problems:
            report["ok"] = False
        report["coins"][coin] = entry

    return report


def print_report(report):
    print(f"\n=== Couverture dataset V10 — fenêtre {report['window_hours']}h ===\n")
    for coin, e in report["coins"].items():
        status = "✅" if not e["problems"] else "❌"
        print(f"{status} {coin:6s} | {e['n_evaluations']:6d} évals | "
              f"trou max {e['max_eval_gap_min']} min | "
              f"bougies 1m manquantes: {e['missing_1m_minutes']}")
        for col, pct in e["key_columns"].items():
            flag = "  " if pct >= 95 else "⚠️"
            print(f"    {flag} {col:16s} {pct:6.2f}%")
        if e["problems"]:
            print(f"    → problèmes : {', '.join(e['problems'])}")
    print(f"\nVerdict global : {'OK' if report['ok'] else 'COUVERTURE INSUFFISANTE'}\n")


def main():
    parser = argparse.ArgumentParser(description="Rapport de couverture dataset V10")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--min-fill", type=float, default=95.0,
                        help="Seuil %% de remplissage des colonnes clés")
    parser.add_argument("--max-eval-gap-min", type=float, default=5.0)
    parser.add_argument("--json", action="store_true", help="Sortie JSON")
    args = parser.parse_args()

    from pymongo import MongoClient
    from config import (MONGO_URL, MONGO_DB, MONGO_COLLECTION_SIGNALS,
                        MONGO_COLLECTION_1M, COLLECT_PAIRS)

    if not MONGO_URL:
        print("MONGO_URL manquant"); sys.exit(2)
    db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]
    coins = [p.split("/")[0] for p in COLLECT_PAIRS]

    report = run_report(db, coins, args.hours, args.min_fill,
                        args.max_eval_gap_min,
                        MONGO_COLLECTION_SIGNALS, MONGO_COLLECTION_1M)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
