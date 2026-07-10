"""
PaperTrader V10 — trading simulé sans aucun ordre réel (porté de v8.11).

Reçoit les VRAIS prix (lus depuis MongoDB, alimenté par le collector en temps
réel) mais ne touche jamais l'exchange : positions, TP/SL et PnL sont simulés.
Interface compatible avec HyperliquidTrader pour les méthodes utilisées par main.py.

Évolutions V10 :
  - arrondi des prix en chiffres significatifs (coins micro : PEPE, WIF...) ;
  - journal via TradeLogger (collection paper_trades) avec `context`
    (signal_id + snapshot features) → lien trade ↔ signal.
"""

import time
from datetime import datetime, timezone

from pymongo import MongoClient

from config import (
    PAIRS, POSITION_SIZE_PCT, RESERVE_BALANCE_PCT,
    TP_PCT, SL_PCT, PAPER_START_BALANCE,
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_1M,
    MONGO_COLLECTION_PAPER_TRADES, MONGO_COLLECTION_PAPER_STATE,
)
from datalog.trade_logger import TradeLogger
from utils.notifier import Notifier
from utils.paper_sim import compute_tp_sl, simulate_candle_fill
from utils.prices import round_price_sig


def _gross(side, entry, exit_price, size):
    return (exit_price - entry) * size if side == "buy" else (entry - exit_price) * size


class PaperTrader:
    def __init__(self):
        self.pair = None
        self.notifier = Notifier()
        self.balance = PAPER_START_BALANCE
        self.position = None        # {side, entry, size, tp_price, sl_price, open_ts, pair}
        self.last_closed = None
        self.db = None
        self._connect()
        self.logger = TradeLogger(collection=MONGO_COLLECTION_PAPER_TRADES,
                                  mongo_db=self.db)
        self._load_state()
        print(f"[PAPER] 📝 MODE PAPER TRADING actif — solde simulé {self.balance:.2f} USDC "
              f"(aucun ordre réel n'est envoyé)")

    # ── MongoDB ───────────────────────────────────────────────
    def _connect(self):
        try:
            self.db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]
        except Exception as e:
            print(f"[PAPER] Mongo indisponible: {e}")

    def _load_state(self):
        if self.db is None:
            return
        try:
            st = self.db[MONGO_COLLECTION_PAPER_STATE].find_one({"_id": "current"})
            if st:
                self.balance = st.get("balance", self.balance)
                self.position = st.get("position")
                self.pair = st.get("pair")
                print(f"[PAPER] État restauré — solde {self.balance:.2f} | "
                      f"position={'oui' if self.position else 'non'}")
        except Exception as e:
            print(f"[PAPER] load_state: {e}")

    def _save_state(self):
        if self.db is None:
            return
        try:
            self.db[MONGO_COLLECTION_PAPER_STATE].replace_one(
                {"_id": "current"},
                {"_id": "current", "balance": self.balance, "position": self.position,
                 "pair": self.pair, "updated_at": datetime.now(timezone.utc).isoformat()},
                upsert=True,
            )
        except Exception as e:
            print(f"[PAPER] save_state: {e}")

    def _coin(self):
        return self.pair.split("/")[0] if self.pair else None

    def _latest_candle(self):
        """Dernière bougie 1m (high/low/close) du coin courant, depuis Mongo."""
        if self.db is None or not self.pair:
            return None
        try:
            doc = self.db[MONGO_COLLECTION_1M].find_one(
                {"coin": self._coin()}, sort=[("timestamp", -1)])
            if not doc:
                return None
            return {"high": float(doc["high"]), "low": float(doc["low"]),
                    "close": float(doc["close"])}
        except Exception as e:
            print(f"[PAPER] latest_candle: {e}")
            return None

    # ── Solde / sizing (mêmes formules que le réel) ───────────
    def _get_total_balance(self, currency="USDC"):
        return self.balance

    def get_usable_balance(self, currency="USDC"):
        return max(self.balance * (1 - RESERVE_BALANCE_PCT), 0)

    def get_position_size(self, price):
        return round((self.get_usable_balance() * POSITION_SIZE_PCT) / price, 6) if price else 0

    # ── Ouverture ─────────────────────────────────────────────
    def place_order_with_tp_sl(self, side, price, tp_pct=None, sl_pct=None,
                               size_factor=1.0, context=None):
        if not self.pair:
            print("[PAPER] Aucune paire sélectionnée")
            return None
        size = round(self.get_position_size(price) * max(0.3, min(1.0, size_factor)), 6)
        if size <= 0:
            print("[PAPER] Solde simulé insuffisant")
            return None

        tp_pct = tp_pct or TP_PCT
        sl_pct = sl_pct or SL_PCT
        tp_price, sl_price = compute_tp_sl(side, price, tp_pct, sl_pct)
        tp_price, sl_price = round_price_sig(tp_price), round_price_sig(sl_price)

        self.position = {
            "side": side, "entry": price, "size": size,
            "tp_price": tp_price, "sl_price": sl_price,
            "open_ts": int(time.time() * 1000), "pair": self.pair,
            "signal_id": (context or {}).get("signal_id"),
        }
        self._save_state()
        self.logger.log_trade({"pair": self.pair, "side": side, "action": "open",
                               "entry_price": price, "exit_price": None, "size": size,
                               "pnl": None, "reason": "signal", "paper": True,
                               "tp_price": tp_price, "sl_price": sl_price},
                              context=context)
        try:
            self.notifier.send(f"📝 <b>[PAPER] {side.upper()}</b> {self.pair}\n"
                               f"Entrée {price:.6g} | TP {tp_price:.6g} | SL {sl_price:.6g} | size {size}")
        except Exception:
            pass
        print(f"[PAPER] {side.upper()} {size} {self.pair} @ {price} | TP {tp_price} SL {sl_price}")
        return {"order": {"id": "paper"}, "size": size, "entry_price": price, "side": side,
                "tp_price": tp_price, "sl_price": sl_price,
                "tp_order_id": "paper", "sl_order_id": "paper"}

    # ── Clôture TP/SL simulée (détectée par has_open_position) ──
    def _maybe_close_on_price(self):
        if not self.position:
            return False
        candle = self._latest_candle()
        if not candle:
            return False
        closed, exit_price, reason = simulate_candle_fill(self.position, candle)
        if not closed:
            return False
        pos = self.position
        pnl = _gross(pos["side"], pos["entry"], exit_price, pos["size"])
        self.balance += pnl
        self.last_closed = {"price": exit_price, "amount": pos["size"],
                            "side": pos["side"], "timestamp": int(time.time() * 1000)}
        # On NE log PAS ici : le bot loggue la clôture via _handle_exchange_closure
        self.position = None
        self._save_state()
        return True

    # ── Interface attendue par main.py ────────────────────────
    def has_open_position(self):
        self._maybe_close_on_price()        # simule l'exécution TP/SL par l'« exchange »
        if not self.position:
            return False, None
        p = self.position
        candle = self._latest_candle()
        mark = candle["close"] if candle else p["entry"]
        return True, {"side": "long" if p["side"] == "buy" else "short",
                      "entry_price": p["entry"], "contracts": p["size"],
                      "mark_price": mark,
                      "unrealized_pnl": _gross(p["side"], p["entry"], mark, p["size"])}

    def close_position(self, reason="manual", context=None):
        if not self.position:
            return None
        candle = self._latest_candle()
        exit_price = candle["close"] if candle else self.position["entry"]
        pos = self.position
        pnl = _gross(pos["side"], pos["entry"], exit_price, pos["size"])
        self.balance += pnl
        self.last_closed = {"price": exit_price, "amount": pos["size"],
                            "side": pos["side"], "timestamp": int(time.time() * 1000)}
        merged_ctx = dict(context or {})
        merged_ctx.setdefault("signal_id", pos.get("signal_id"))
        self.logger.log_trade({"pair": pos["pair"], "side": pos["side"], "action": "close",
                               "entry_price": pos["entry"], "exit_price": exit_price,
                               "size": pos["size"], "pnl": pnl, "reason": reason,
                               "paper": True},
                              context=merged_ctx)
        try:
            self.notifier.send(f"📝 <b>[PAPER] CLÔTURE</b> {pos['pair']} ({reason})\n"
                               f"{pos['entry']:.6g} → {exit_price:.6g} | PnL {pnl:+.4f} | "
                               f"Solde {self.balance:.2f}")
        except Exception:
            pass
        print(f"[PAPER] Clôture {reason} {pos['entry']:.6g}→{exit_price:.6g} | "
              f"PnL {pnl:+.4f} | solde {self.balance:.2f}")
        self.position = None
        self._save_state()
        return {"pnl": pnl, "order": {"id": "paper"}}

    def update_sl(self, new_sl_price, old_sl_order_id=None):
        if self.position:
            self.position["sl_price"] = round_price_sig(new_sl_price)
            self._save_state()
        return {"id": "paper"}

    def update_tp(self, new_tp_price, old_tp_order_id=None):
        if self.position:
            self.position["tp_price"] = round_price_sig(new_tp_price)
            self._save_state()
        return {"id": "paper"}

    def cancel_open_orders(self):
        return  # rien à annuler en paper

    def fetch_positions(self):
        return []

    def get_last_closed_trade(self, since_ms=None):
        return self.last_closed

    def select_pair(self):
        if not self.pair:
            self.pair = PAIRS[0]
        return self.pair
