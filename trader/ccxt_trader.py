"""
HyperliquidTrader V10 — exécution réelle via ccxt, portée de v8.

Évolutions plomberie (le comportement de trading est inchangé) :
  - arrondi des prix TP/SL en CHIFFRES SIGNIFICATIFS (utils.prices) — les
    round(x, 2) de v8 écrasaient à zéro les prix micro (PEPE, WIF, DOGE) ;
  - journal des trades via TradeLogger, avec `context` (signal_id + snapshot
    des features à l'entrée) fourni par le bot → lien trade ↔ signal.
"""

import time
import ccxt
from config import (
    HYPERLIQUID_API_KEY,
    HYPERLIQUID_API_SECRET,
    PAIRS,
    MIN_COLLATERAL,
    POSITION_SIZE_PCT,
    RESERVE_BALANCE_PCT,
    DEBUG,
    TP_PCT,
    SL_PCT,
)
from datalog.trade_logger import TradeLogger
from utils.notifier import Notifier
from utils.min_order import min_target_size, meets_minimum
from utils.prices import round_price_sig


def _ccxt_symbol(pair):
    """Symbole ccxt d'une paire canonique HL.

    Les contrats « kilo » d'Hyperliquid (kPEPE = 1000 PEPE) s'écrivent kPEPE
    côté WS/clearinghouse mais KPEPE/USDC:USDC dans les marchés ccxt. Partout
    ailleurs (Mongo, positions, logs) on garde le nom canonique HL ; la
    conversion ne se fait qu'ici, à la frontière exchange.
    """
    base, _, rest = pair.partition("/")
    if base.startswith("k"):
        base = base.upper()
    return f"{base}/{rest}"


class HyperliquidTrader:
    def __init__(self):
        self.exchange = ccxt.hyperliquid({
            "walletAddress": HYPERLIQUID_API_KEY,
            "privateKey": HYPERLIQUID_API_SECRET,
            "enableRateLimit": True,
        })
        self.logger = TradeLogger()
        self.notifier = Notifier()
        self.pair = None  # Determine dynamiquement
        try:
            self.exchange.load_markets()
        except Exception as e:
            print(f"[TRADER] load_markets au démarrage échoué (réessai plus tard): {e}")

    def _sym(self, pair=None):
        """Symbole ccxt de la paire courante (ou fournie)."""
        return _ccxt_symbol(pair or self.pair)

    def _round_price(self, price):
        """Arrondit un prix aux règles de l'exchange, fallback 5 chiffres significatifs."""
        try:
            return float(self.exchange.price_to_precision(self._sym(), price))
        except Exception:
            return round_price_sig(price)

    def select_pair(self):
        """Choisit la premiere paire pour laquelle on a assez de collateral."""
        balance = self._get_total_balance()
        usable = balance * (1 - RESERVE_BALANCE_PCT)

        for pair in PAIRS:
            min_col = MIN_COLLATERAL.get(pair, 10)
            if usable >= min_col:
                if self.pair != pair and DEBUG:
                    print(f"[TRADER] Paire selectionnee : {pair} (solde utilisable: {usable:.2f})")
                self.pair = pair
                return pair

        print(f"[TRADER] Solde insuffisant ({usable:.2f}) pour toutes les paires")
        self.pair = None
        return None

    def _get_total_balance(self, currency="USDC"):
        try:
            balance = self.exchange.fetch_balance()
            return float(balance["total"].get(currency, 0))
        except Exception as e:
            print(f"[TRADER][ERREUR] fetch_balance: {e}")
            return 0

    def get_usable_balance(self, currency="USDC"):
        total = self._get_total_balance(currency)
        reserve = total * RESERVE_BALANCE_PCT
        usable = total - reserve
        if DEBUG:
            print(f"[TRADER] Solde total={total:.2f}, reserve={reserve:.2f}, utilisable={usable:.2f}")
        return max(usable, 0)

    def get_position_size(self, price):
        balance = self.get_usable_balance()
        amount = (balance * POSITION_SIZE_PCT) / price
        if DEBUG:
            print(f"[TRADER] Position size: {amount:.6f} ({self.pair})")
        return round(amount, 6)

    def _safe_amount(self, size, price, min_col):
        """Arrondit la taille à la précision réelle de l'exchange en garantissant
        un notionnel >= min_col. Remonte d'un cran de précision si l'arrondi est
        passé sous le minimum. Fallback round(6) si la précision est indisponible."""
        try:
            amt = float(self.exchange.amount_to_precision(self._sym(), size))
            if amt * price < min_col:
                market = self.exchange.market(self._sym())
                prec = (market.get("precision") or {}).get("amount")
                if isinstance(prec, int):
                    inc = 10 ** (-prec)
                elif isinstance(prec, (int, float)) and prec and prec > 0:
                    inc = prec
                else:
                    inc = 1e-6
                amt = float(self.exchange.amount_to_precision(self._sym(), amt + inc))
            return amt
        except Exception as e:
            print(f"[TRADER] _safe_amount fallback round(6): {e}")
            return round(size, 6)

    def place_order_with_tp_sl(self, side, price, tp_pct=None, sl_pct=None,
                               size_factor=1.0, context=None):
        """Ouvre une position + TP/SL. Retourne dict avec les infos ou None.

        context : dict optionnel (signal_id, entry_features, ...) fusionné dans
        le journal des trades — lien trade ↔ signal (V10).
        """
        if not self.pair:
            print("[TRADER] Aucune paire selectionnee")
            return None

        size = self.get_position_size(price) * max(0.3, min(1.0, size_factor))
        if size <= 0:
            print("[TRADER] Pas assez de solde pour trader")
            return None

        # ── Gestion robuste du minimum d'ordre (Hyperliquid : ~$10 notionnel) ──
        min_col = MIN_COLLATERAL.get(self.pair, 10)
        base_size = self.get_position_size(price)            # taille pleine (sans factor)
        target = min_target_size(min_col, price)             # +20% de marge sur le minimum

        if size < target:
            if base_size >= target:
                size = target
                print(f"[TRADER] Taille remontée au minimum (+marge): {size * price:.2f} USDC")
            else:
                print(f"[TRADER] Solde insuffisant pour le minimum "
                      f"({base_size * price:.2f} USDC < {min_col} USDC) — trade ignoré")
                return None

        size = self._safe_amount(size, price, min_col)
        if not size or not meets_minimum(size, price, min_col):
            print(f"[TRADER] Taille finale sous le minimum après arrondi — trade ignoré")
            return None

        tp_pct = tp_pct or TP_PCT
        sl_pct = sl_pct or SL_PCT

        # Ordre principal
        try:
            main_order = self.exchange.create_order(
                symbol=self._sym(),
                type="market",
                side=side,
                amount=size,
                price=price,
                params={"maxSlippagePcnt": 0.01}
            )
            print(f"[TRADER] {side.upper()} {size} {self.pair} @ {price} (order: {main_order.get('id')})")
        except Exception as e:
            print(f"[TRADER][ERREUR] Ordre principal: {e}")
            self.notifier.error(f"Ordre {side} echoue: {e}")
            return None

        # Calcul TP/SL — arrondi coin-agnostique (V10)
        if side == "buy":
            tp_price = self._round_price(price * (1 + tp_pct))
            sl_price = self._round_price(price * (1 - sl_pct))
            closing_side = "sell"
        else:
            tp_price = self._round_price(price * (1 - tp_pct))
            sl_price = self._round_price(price * (1 + sl_pct))
            closing_side = "buy"

        # Take Profit
        tp_order_id = None
        try:
            tp_order = self.exchange.create_order(
                symbol=self._sym(),
                type="market",
                side=closing_side,
                amount=size,
                price=tp_price,
                params={"takeProfitPrice": tp_price, "reduceOnly": True}
            )
            tp_order_id = tp_order.get("id")
            print(f"[TRADER] TP place @ {tp_price} (order: {tp_order_id})")
        except Exception as e:
            print(f"[TRADER][ERREUR] TP: {e}")

        # Stop Loss
        sl_order_id = None
        try:
            sl_order = self.exchange.create_order(
                symbol=self._sym(),
                type="market",
                side=closing_side,
                amount=size,
                price=sl_price,
                params={"stopLossPrice": sl_price, "reduceOnly": True}
            )
            sl_order_id = sl_order.get("id")
            print(f"[TRADER] SL place @ {sl_price} (order: {sl_order_id})")
        except Exception as e:
            print(f"[TRADER][ERREUR] SL: {e}")

        # Notification
        self.notifier.trade_opened(self.pair, side, price, size, tp_price, sl_price)

        # Log — enrichi du contexte signal (V10)
        self.logger.log_trade({
            "pair": self.pair,
            "side": side,
            "action": "open",
            "entry_price": price,
            "exit_price": None,
            "size": size,
            "pnl": None,
            "reason": "signal",
            "duration_sec": None,
            "tp_price": tp_price,
            "sl_price": sl_price,
        }, context=context)

        return {
            "order": main_order,
            "size": size,
            "entry_price": price,
            "side": side,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_order_id": tp_order_id,
            "sl_order_id": sl_order_id,
        }

    def close_position(self, reason="manual", context=None):
        """Ferme la position ET annule les ordres TP/SL orphelins."""
        if not self.pair:
            return None

        # 1. Annuler tous les ordres ouverts sur cette paire (TP/SL)
        self.cancel_open_orders()

        # 2. Fetcher le prix actuel AVANT de fermer (plus fiable que markPrice)
        try:
            ticker = self.exchange.fetch_ticker(self._sym())
            current_price = float(ticker.get("last", 0))
        except Exception:
            current_price = 0

        # 3. Fermer la position
        positions = self.fetch_positions()
        for pos in positions:
            if pos.get("symbol") == self._sym() and float(pos.get("contracts", 0)) > 0:
                amt = abs(float(pos["contracts"]))
                side = "sell" if pos.get("side") == "long" else "buy"
                entry_price = float(pos.get("entryPrice", 0))

                try:
                    order = self.exchange.create_order(
                        symbol=self._sym(),
                        type="market",
                        side=side,
                        amount=amt,
                        price=current_price,
                        params={"maxSlippagePcnt": 0.01}
                    )

                    fill_price = float(order.get("average", 0) or order.get("price", 0) or current_price)
                    if fill_price == 0:
                        fill_price = current_price

                    if pos.get("side") == "long":
                        pnl = (fill_price - entry_price) * amt
                    else:
                        pnl = (entry_price - fill_price) * amt

                    print(f"[TRADER] Position fermee {side} {amt} @ {fill_price:.6g} (entry: {entry_price:.6g}) | PnL: {pnl:+.4f} | Raison: {reason}")

                    self.notifier.trade_closed(self.pair, pos.get("side", side), entry_price, fill_price, pnl, reason)

                    self.logger.log_trade({
                        "pair": self.pair,
                        "side": pos.get("side", side),
                        "action": "close",
                        "entry_price": entry_price,
                        "exit_price": fill_price,
                        "size": amt,
                        "pnl": pnl,
                        "reason": reason,
                        "duration_sec": None,
                    }, context=context)

                    return {"pnl": pnl, "order": order}
                except Exception as e:
                    print(f"[TRADER][ERREUR] Close: {e}")
                    self.notifier.error(f"Fermeture echouee: {e}")
                    return None
        return None

    def update_sl(self, new_sl_price, old_sl_order_id=None):
        """Met à jour le Stop Loss (annule l'ancien par ID ou par type, place le nouveau)."""
        if not self.pair:
            return None
        new_sl_price = self._round_price(new_sl_price)
        try:
            if old_sl_order_id:
                try:
                    self.exchange.cancel_order(old_sl_order_id, self._sym())
                    if DEBUG:
                        print(f"[TRADER] Ancien SL annulé (by ID): {old_sl_order_id}")
                except Exception:
                    pass  # Peut déjà être exécuté/annulé — on continue quand même
            else:
                open_orders = self.exchange.fetch_open_orders(self._sym())
                for order in open_orders:
                    otype = order.get("type", "").lower()
                    if "stop" in otype or (
                        order.get("reduceOnly") and "take" not in otype and "profit" not in otype
                    ):
                        self.exchange.cancel_order(order["id"], self._sym())
                        if DEBUG:
                            print(f"[TRADER] Ancien SL annulé (by type): {order['id']}")

            positions = self.fetch_positions()
            for pos in positions:
                contracts = float(pos.get("contracts") or 0)
                if pos.get("symbol") == self._sym() and contracts > 0:
                    side_close = "sell" if pos.get("side") == "long" else "buy"
                    sl_order = self.exchange.create_order(
                        self._sym(), "market", side_close, contracts,
                        price=new_sl_price,
                        params={"stopLossPrice": new_sl_price, "reduceOnly": True}
                    )
                    if DEBUG:
                        print(f"[TRADER] Nouveau SL @ {new_sl_price:.6g} (order: {sl_order.get('id')})")
                    return sl_order
        except Exception as e:
            print(f"[TRADER][ERREUR] update_sl: {e}")
            return None

    def update_tp(self, new_tp_price, old_tp_order_id=None):
        """Met a jour le Take Profit (annule l'ancien par ID ou par type, place le nouveau)."""
        if not self.pair:
            return None
        new_tp_price = self._round_price(new_tp_price)
        try:
            if old_tp_order_id:
                try:
                    self.exchange.cancel_order(old_tp_order_id, self._sym())
                    if DEBUG:
                        print(f"[TRADER] Ancien TP annule (by ID): {old_tp_order_id}")
                except Exception:
                    pass
            else:
                open_orders = self.exchange.fetch_open_orders(self._sym())
                for order in open_orders:
                    otype = order.get("type", "").lower()
                    if "take" in otype or "profit" in otype or (
                        order.get("reduceOnly") and "stop" not in otype
                    ):
                        self.exchange.cancel_order(order["id"], self._sym())
                        if DEBUG:
                            print(f"[TRADER] Ancien TP annule (by type): {order['id']}")

            positions = self.fetch_positions()
            for pos in positions:
                contracts = float(pos.get("contracts") or 0)
                if pos.get("symbol") == self._sym() and contracts > 0:
                    side_close = "sell" if pos.get("side") == "long" else "buy"
                    tp_order = self.exchange.create_order(
                        self._sym(), "limit", side_close, contracts,
                        price=new_tp_price,
                        params={"reduceOnly": True}
                    )
                    if DEBUG:
                        print(f"[TRADER] Nouveau TP @ {new_tp_price:.6g} (order: {tp_order.get('id')})")
                    return tp_order
        except Exception as e:
            print(f"[TRADER][ERREUR] update_tp: {e}")
            return None

    def cancel_open_orders(self):
        """Annule tous les ordres ouverts (TP/SL) sur la paire."""
        if not self.pair:
            return
        try:
            open_orders = self.exchange.fetch_open_orders(self._sym())
            for order in open_orders:
                try:
                    self.exchange.cancel_order(order["id"], self._sym())
                    if DEBUG:
                        print(f"[TRADER] Ordre annule: {order['id']} ({order.get('type', '?')})")
                except Exception as e:
                    print(f"[TRADER][ERREUR] Cancel order {order['id']}: {e}")
            if open_orders and DEBUG:
                print(f"[TRADER] {len(open_orders)} ordres annules sur {self.pair}")
        except Exception as e:
            print(f"[TRADER][ERREUR] fetch_open_orders: {e}")

    def fetch_positions(self):
        try:
            return self.exchange.fetch_positions([self._sym()]) if self.pair else []
        except Exception as e:
            print(f"[TRADER][ERREUR] fetch_positions: {e}")
            return []

    def has_open_position(self):
        """Retourne (bool, position_info) pour la paire courante ou toute paire configuree."""
        pairs_to_check = [self.pair] if self.pair else PAIRS
        for pair in pairs_to_check:
            try:
                positions = self.exchange.fetch_positions([_ccxt_symbol(pair)])
                for pos in positions:
                    contracts = float(pos.get("contracts") or 0)
                    if contracts > 0:
                        if not self.pair:
                            self.pair = pair
                        return True, {
                            "side": pos.get("side"),
                            "entry_price": float(pos.get("entryPrice") or 0),
                            "contracts": contracts,
                            "mark_price": float(pos.get("markPrice") or 0),
                            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
                        }
            except Exception as e:
                print(f"[TRADER][ERREUR] fetch_positions({pair}): {e}")
        return False, None

    def get_last_closed_trade(self, since_ms=None):
        """Récupère le dernier trade fermé depuis l'exchange (fills)."""
        try:
            if since_ms is None:
                since_ms = int((time.time() - 3600) * 1000)  # derniere heure
            trades = self.exchange.fetch_my_trades(self._sym(), since=since_ms, limit=50)
            if not trades:
                return None
            last = trades[-1]
            return {
                "price": float(last.get("price", 0)),
                "amount": float(last.get("amount", 0)),
                "side": last.get("side"),
                "cost": float(last.get("cost", 0)),
                "fee": float(last.get("fee", {}).get("cost", 0)) if last.get("fee") else 0,
                "timestamp": last.get("timestamp"),
            }
        except Exception as e:
            print(f"[TRADER][ERREUR] get_last_closed_trade: {e}")
            return None


if __name__ == "__main__":
    trader = HyperliquidTrader()
    pair = trader.select_pair()
    print(f"Paire: {pair}")
    print(f"Solde: {trader.get_usable_balance()}")
    print(f"Positions: {trader.fetch_positions()}")
