"""
Rapport journalier Telegram et auto-calibration du seuil de signal.

Extrait de `main.py`. Ces deux traitements ne partagent avec le bot que le
notifier, le trader, les positions et le seuil courant — passés en paramètres
plutôt que lus sur `self`, ce qui les rend vérifiables sans instancier le bot.

Ce sont aussi les deux méthodes qui créaient un MongoClient par appel (1×/jour
et 1×/semaine) sans jamais le fermer : la fuite qui a mené à l'OOM du
22/08/2026. Elles passent désormais par le client partagé.
"""

from datetime import datetime, timezone

from config import (
    COINS, MONGO_COLLECTION_TRADES, AUTOCAL_LOOKBACK_TRADES,
    SIGNAL_THRESHOLD_MIN, SIGNAL_THRESHOLD_MAX,
)
from utils.mongo import get_db
from utils.reporting import daily_report_window


def send_daily_report(notifier, trader, positions, coins=COINS):
    """Envoie un résumé journalier Telegram (trades, PnL, win rate)."""
    try:
        db = get_db()
        day_start_ms, today_start_ms, day_label = daily_report_window(
            datetime.now(timezone.utc)
        )
        trades = list(db[MONGO_COLLECTION_TRADES].find(
            {"action": "close",
             "timestamp": {"$gte": day_start_ms, "$lt": today_start_ms}}
        ))
        balance = trader._get_total_balance()
        if not trades:
            notifier.send(
                f"📊 <b>Bilan de la veille ({day_label})</b>\n"
                f"Aucun trade fermé\n"
                f"Solde: <code>{balance:.2f} USDC</code>"
            )
            return
        wins   = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        win_rate  = len(wins) / len(trades) * 100

        open_parts = []
        for c in coins:
            p = positions[c]
            if p["active"]:
                open_parts.append(f"{c} {p['side'].upper()} @ {p['entry']:.6g}")
        open_str = " | ".join(open_parts) if open_parts else "Aucune"

        emoji = "📈" if total_pnl >= 0 else "📉"
        notifier.send(
            f"{emoji} <b>Bilan de la veille ({day_label})</b>\n"
            f"Trades: {len(trades)} | ✅ {len(wins)} gagnants / ❌ {len(losses)} perdants\n"
            f"Win rate: <b>{win_rate:.1f}%</b>\n"
            f"PnL total: <b>{total_pnl:+.4f} USDC</b>\n"
            f"Solde: <code>{balance:.2f} USDC</code>\n"
            f"Positions ouvertes: {open_str}"
        )
    except Exception as e:
        print(f"[BOT] Daily report error: {e}")


def auto_calibrate(current_threshold, notifier):
    """Ajuste le seuil de signal selon les performances récentes.

    Retourne le nouveau seuil (inchangé si les données manquent ou en cas
    d'erreur) — l'appelant décide quoi en faire, la fonction ne mute rien.
    """
    try:
        db = get_db()
        trades = list(db[MONGO_COLLECTION_TRADES].find(
            {"action": "close"}
        ).sort("timestamp", -1).limit(AUTOCAL_LOOKBACK_TRADES))

        if len(trades) < 5:
            print("[BOT] Auto-cal: pas assez de trades pour calibrer")
            return current_threshold

        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        win_rate = wins / len(trades)
        nouveau = current_threshold

        if win_rate < 0.40:
            nouveau = min(SIGNAL_THRESHOLD_MAX, current_threshold + 1)
        elif win_rate > 0.60:
            nouveau = max(SIGNAL_THRESHOLD_MIN, current_threshold - 1)

        if nouveau != current_threshold:
            notifier.send(
                f"🔧 <b>Auto-calibration</b>\n"
                f"Win rate ({len(trades)} trades): <b>{win_rate*100:.1f}%</b>\n"
                f"Seuil: {current_threshold} → <b>{nouveau}</b>"
            )
        print(f"[BOT] Auto-cal: seuil={nouveau} | "
              f"win_rate={win_rate*100:.1f}% ({wins}/{len(trades)})")
        return nouveau
    except Exception as e:
        print(f"[BOT] Auto-cal error: {e}")
        return current_threshold
