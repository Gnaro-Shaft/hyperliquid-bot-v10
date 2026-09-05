import requests

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DEBUG
from utils import http

# Timeout réduit à 5 s (au lieu de 10) parce que send() est appelé depuis la
# boucle de trading : avec 3 tentatives, le pire cas passe de 10 s à ~16,5 s.
# Sans réessai, une alerte perdue l'était définitivement — c'est le filet de
# sécurité, il doit survivre à un 429 de Telegram ou à une coupure passagère.
TIMEOUT_SEC = 5
TENTATIVES = 3


class Notifier:
    def __init__(self):
        self.enabled = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
        self._http = http.creer_session()
        if not self.enabled and DEBUG:
            print("[NOTIFIER] Telegram non configure (TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant)")

    def send(self, message):
        """Envoie un message Telegram en HTML. Retourne True si accepté."""
        if not self.enabled:
            if DEBUG:
                print(f"[NOTIFIER][LOCAL] {message}")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            http.demander(self._http, "POST", url, json=payload,
                          timeout=TIMEOUT_SEC, tentatives=TENTATIVES)
            return True
        except requests.exceptions.HTTPError as e:
            # Conserve le détail utile : Telegram explique le refus dans le corps
            reponse = getattr(e, "response", None)
            if DEBUG and reponse is not None:
                print(f"[NOTIFIER][ERREUR] Telegram {reponse.status_code}: {reponse.text}")
            return False
        except Exception as e:
            if DEBUG:
                print(f"[NOTIFIER][ERREUR] {e}")
            return False

    def trade_opened(self, pair, side, price, size, tp, sl):
        emoji = "🟢" if side == "buy" else "🔴"
        rr = abs(tp - price) / abs(price - sl) if abs(price - sl) > 0 else 0
        msg = (
            f"{emoji} <b>POSITION OUVERTE</b>\n"
            f"Paire: <code>{pair}</code>\n"
            f"Side: <b>{side.upper()}</b>\n"
            f"Prix: <code>{price:.2f}</code>\n"
            f"Taille: <code>{size}</code>\n"
            f"TP: <code>{tp:.2f}</code> | SL: <code>{sl:.2f}</code>\n"
            f"R:R = <b>{rr:.1f}:1</b>"
        )
        self.send(msg)

    def trade_closed(self, pair, side, entry_price, exit_price, pnl, reason):
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{emoji} <b>POSITION FERMEE</b>\n"
            f"Paire: <code>{pair}</code>\n"
            f"Side: <b>{side.upper()}</b>\n"
            f"Entree: <code>{entry_price:.2f}</code> → Sortie: <code>{exit_price:.2f}</code>\n"
            f"PnL: <b>{pnl:+.2f} USDC</b>\n"
            f"Raison: {reason}"
        )
        self.send(msg)

    def signal_alert(self, coin, score, raw_score, label, color, close_price, debug_info=None):
        """Notifie les signaux forts (score +/-2)."""
        msg = (
            f"{color} <b>SIGNAL {label.upper()}</b>\n"
            f"Coin: <code>{coin}</code>\n"
            f"Score: <b>{score}</b> (raw: {raw_score})\n"
            f"Prix: <code>{close_price:.2f}</code>"
        )
        if debug_info:
            details = "\n".join(f"  {k}: {v}" for k, v in debug_info.items()
                                if k not in ("close", "EMA9", "EMA21", "MACD", "MACD_signal",
                                             "BB_upper", "BB_lower", "BB_pctB", "BB_width",
                                             "VWAP", "ATR", "vol_ratio", "EMA9_slope"))
            msg += f"\n<pre>{details}</pre>"
        self.send(msg)

    def risk_alert(self, reason):
        self.send(f"⚠️ <b>ALERTE RISQUE</b>\n{reason}")

    def daily_summary(self, pnl, trades_count, win_rate, balance):
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"{emoji} <b>RESUME JOURNALIER</b>\n"
            f"PnL: <b>{pnl:+.2f} USDC</b>\n"
            f"Trades: {trades_count}\n"
            f"Win rate: {win_rate:.0f}%\n"
            f"Solde: <code>{balance:.2f} USDC</code>"
        )
        self.send(msg)

    def error(self, error_msg):
        self.send(f"🚨 <b>ERREUR CRITIQUE</b>\n<code>{error_msg}</code>")

    def bot_started(self, pair, balance=None):
        msg = f"🤖 <b>Bot v8 demarre</b> sur <code>{pair}</code>"
        if balance is not None:
            msg += f"\nSolde: <code>{balance:.2f} USDC</code>"
        self.send(msg)

    def bot_stopped(self, reason="arret normal"):
        self.send(f"🛑 <b>Bot v8 arrete</b> — {reason}")


if __name__ == "__main__":
    n = Notifier()
    n.send("🧪 Test notifier v8 — OK")
