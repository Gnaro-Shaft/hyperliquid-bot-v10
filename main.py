"""
Trading Bot V10 — moteur de décision v8 conservé, plomberie refondue.

Câblage V10 :
  - MarketContextStore partagé : collectors → store → moteur (carry-forward) ;
  - SignalLogger : une ligne complète par évaluation, pour les 10 coins ;
  - lien trade ↔ signal : chaque position porte le signal_id et le snapshot
    des features de son signal d'entrée, propagés dans le journal des trades ;
  - WhaleCollector (positions baleines, clusters de liquidation, flux nets) ;
  - HealthMonitor V10 (alerte « collecteur muet > 5 min » par flux).
"""

import time
import signal
import threading
import asyncio
from datetime import datetime, timezone

from pymongo import MongoClient

from config import (
    PAIRS, DEBUG, TP_PCT, SL_PCT, TRAIL_PCT, PAPER_MODE,
    TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
    KILL_SWITCH_FILE,
    SIGNAL_CONFIRM_COUNT, LOOP_INTERVAL, TRAILING_CHECK_INTERVAL,
    MAX_DAILY_DRAWDOWN_PCT, BREAKEVEN_TRIGGER_PCT, BREAKEVEN_OFFSET_PCT,
    MIN_COLLATERAL, RESERVE_BALANCE_PCT, POSITION_SIZE_PCT,
    MAX_OPEN_POSITIONS, MAX_POSITIONS_PER_DIR, MAX_TOTAL_EXPOSURE_PCT,
    CB_MAX_ATR_PCT, CB_MAX_ABS_FUNDING, CB_MAX_CANDLE_RANGE_PCT, CB_MAX_SPREAD_PCT,
    CB_MIN_OB_DEPTH_RATIO,
    PULLBACK_PCT, PULLBACK_EXPIRY_SEC,
    AUTOCAL_LOOKBACK_TRADES, SIGNAL_THRESHOLD_DEFAULT,
    SIGNAL_THRESHOLD_MIN, SIGNAL_THRESHOLD_MAX,
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_TRADES, MONGO_COLLECTION_DECISIONS,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI, MONGO_COLLECTION_ORDERBOOK,
    COOLDOWN_BASE_SEC, COOLDOWN_MIN_SEC, COOLDOWN_MAX_SEC,
    COOLDOWN_LOSS_MULT, COOLDOWN_WIN_MULT,
    WHALE_ENABLED,
)
from strategy.strategy_engine import StrategyEngine
from trader.ccxt_trader import HyperliquidTrader
from trader.paper_trader import PaperTrader
from collector.context_store import MarketContextStore
from collector.websocket_collector import WebSocketCollector
from collector.rest_collector import RestCollector
from collector.whale_collector import WhaleCollector
from datalog.signal_logger import SignalLogger
from risk.risk_manager import RiskManager
from utils.notifier import Notifier
from utils.sizing import size_factor
from utils.reporting import daily_report_window
from utils.exposure import exposure_check
from utils.market_guard import market_circuit_breaker
from utils.observability import build_decision_doc
from utils.prices import round_price_sig
from monitor.health import HealthMonitor

COINS = [p.split("/")[0] for p in PAIRS]


class TradingBot:
    def __init__(self):
        # ── Plomberie V10 : store de contexte partagé + logger de signaux ──
        self.context_store = MarketContextStore(COINS)
        self.signal_logger = SignalLogger()

        self.collector = WebSocketCollector(context_store=self.context_store)
        self.rest_collector = RestCollector(context_store=self.context_store)
        self.whale_collector = WhaleCollector() if WHALE_ENABLED else None
        self.trader = PaperTrader() if PAPER_MODE else HyperliquidTrader()
        self.risk = RiskManager()
        self.notifier = Notifier()
        self._shutdown = False
        self._last_daily_reset = None

        # --- État multi-paires (un dict par coin) ---
        self.positions = {coin: self._empty_position() for coin in COINS}
        self.engines = {}                          # initialisés dans start()
        self._signal_streaks = {c: 0 for c in COINS}
        self._signal_dirs    = {c: 0 for c in COINS}
        self._reverse_streaks = {c: 0 for c in COINS}
        self._last_trade_times = {c: 0 for c in COINS}

        # --- Auto-calibration & corrélation ---
        self._last_signal_scores = {c: 0 for c in COINS}
        self._signal_threshold = SIGNAL_THRESHOLD_DEFAULT
        self._last_autocal_date = None

        # --- Cooldown dynamique ---
        self._cooldowns = {c: COOLDOWN_BASE_SEC for c in COINS}

        # --- Compteur d'erreurs ---
        self._err_count = 0

    # ──────────────────────────────────────────────────────────
    # Démarrage
    # ──────────────────────────────────────────────────────────

    def start(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        # Health-check MongoDB — dépendance dure : toute la pipeline live passe
        # par Mongo (collector → MongoDB → stratégie). Sans elle, aucun signal.
        if not self._check_mongo_health():
            try:
                self.notifier.error(
                    "🔴 <b>DÉMARRAGE AVORTÉ</b>\n"
                    "MongoDB injoignable — le bot ne peut pas produire de signaux.\n"
                    "Nouvelle tentative au prochain redémarrage."
                )
            except Exception:
                pass
            print("[BOT] ❌ Arrêt : MongoDB indisponible au démarrage.")
            time.sleep(30)   # throttle la boucle de restart Fly (policy=always)
            import sys
            sys.exit(1)

        # Amorcer le carry-forward depuis Mongo (dernières valeurs connues)
        try:
            db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]
            self.context_store.seed_from_mongo(
                db, MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI,
                MONGO_COLLECTION_ORDERBOOK)
            print("[BOT] 🔁 ContextStore amorcé depuis MongoDB (carry-forward)")
        except Exception as e:
            print(f"[BOT] ContextStore non amorcé ({e}) — il se remplira en live")

        # Collectors en threads dédiés
        threading.Thread(target=self._run_collector, daemon=True).start()
        threading.Thread(target=self.rest_collector.collect_loop, daemon=True).start()
        if self.whale_collector is not None:
            threading.Thread(target=self.whale_collector.collect_loop,
                             daemon=True, name="WhaleCollector").start()
            print("[BOT] 🐋 WhaleCollector démarré (positions + liquidations + flux)")

        # Attendre les premières données WS
        print("[BOT] Attente des premières données du collector...")
        for _ in range(60):
            if self.collector.is_alive:
                break
            time.sleep(2)
        if not self.collector.is_alive:
            print("[BOT] Collector muet après 2 min — démarrage quand même...")

        # Init engines pour chaque coin — store + logger partagés (V10)
        for coin in COINS:
            self.engines[coin] = StrategyEngine(
                coin=coin,
                context_store=self.context_store,
                signal_logger=self.signal_logger,
            )

        # Healthcheck autonome — surveillance + alertes Telegram sur transition
        self._health_monitor = HealthMonitor(bot=self, notifier=self.notifier)
        threading.Thread(
            target=self._health_monitor.run_loop,
            daemon=True,
            name="HealthMonitor",
        ).start()
        print("[BOT] 🚑 HealthMonitor démarré (collecteur muet > 5 min ⇒ alerte)")

        # Init risk manager
        balance = self.trader._get_total_balance()
        self.risk.reset_daily(balance)
        self._last_daily_reset = datetime.now(timezone.utc).date()

        # Sync positions existantes sur l'exchange
        self._sync_positions_on_start()

        pairs_str = " | ".join(COINS)
        mode = "📝 PAPER" if PAPER_MODE else "💸 LIVE"
        self.notifier.send(f"🚀 <b>Bot V10 démarré [{mode}]</b> — {pairs_str} | Solde: <code>{balance:.2f} USDC</code>")
        print(f"\n=== Trading Bot V10 [{mode}] | {pairs_str} | Solde: {balance:.2f} USDC ===\n")

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        self._trading_loop()

    def _sync_positions_on_start(self):
        """Détecte les positions ouvertes sur toutes les paires au redémarrage."""
        for pair in PAIRS:
            coin = pair.split("/")[0]
            self.trader.pair = pair
            has_pos, pos_info = self.trader.has_open_position()
            if has_pos and pos_info:
                self.positions[coin] = {
                    **self._empty_position(),
                    "active": True,
                    "entry": pos_info["entry_price"],
                    "side": "buy" if pos_info["side"] == "long" else "sell",
                    "size": abs(pos_info.get("contracts", 0)),
                    "open_time": time.time(),
                }
                print(f"[BOT] [{coin}] Position existante: {pos_info['side']} "
                      f"@ {pos_info['entry_price']:.6g} | PnL: {pos_info['unrealized_pnl']:+.2f}")

    # ──────────────────────────────────────────────────────────
    # Boucle principale
    # ──────────────────────────────────────────────────────────

    def _trading_loop(self):
        while not self._shutdown:
            try:
                # Reset journalier + rapport + auto-calibration
                today = datetime.now(timezone.utc).date()
                if today != self._last_daily_reset:
                    if self._last_daily_reset is not None:
                        self._send_daily_report()
                    balance = self.trader._get_total_balance()
                    self.risk.reset_daily(balance)
                    self._last_daily_reset = today
                    if self._last_autocal_date is None or \
                       (today - self._last_autocal_date).days >= 7:
                        self._auto_calibrate()
                        self._last_autocal_date = today
                    self.notifier.send(f"📅 <b>Nouveau jour</b> — Solde: <code>{balance:.2f} USDC</code>")

                if self._check_kill_switch():
                    time.sleep(60)
                    continue

                if not self.collector.is_alive:
                    print("[BOT] ⚠️ Collector inactif — données potentiellement stales")

                # Traiter chaque paire — isolation par coin (une erreur ne bloque pas les autres)
                for pair in PAIRS:
                    coin = pair.split("/")[0]
                    try:
                        self._process_pair(pair, coin)
                    except Exception as e:
                        import traceback
                        print(f"[BOT][ERREUR][{coin}] {e}")
                        print(traceback.format_exc())

            except Exception as e:
                import traceback
                print(f"[BOT][ERREUR] {e}")
                print(traceback.format_exc())
                self._err_count += 1
                if self._err_count <= 3 or self._err_count % 10 == 0:
                    self.notifier.error(f"[{self._err_count}] {str(e)[:200]}")

            # Boucle courte : positions actives OU pending entries
            any_active = any(self.positions[c]["active"] for c in COINS)
            any_pending = any(self.positions[c].get("pending_entry") for c in COINS)
            if any_active or any_pending:
                for _ in range(int(LOOP_INTERVAL / TRAILING_CHECK_INTERVAL)):
                    time.sleep(TRAILING_CHECK_INTERVAL)
                    if self._shutdown:
                        break
                    for pair in PAIRS:
                        coin = pair.split("/")[0]
                        pos = self.positions[coin]
                        live = self.collector.get_live_price(coin)
                        if not live:
                            continue
                        self.trader.pair = pair
                        if pos.get("pending_entry") and not pos["active"]:
                            self._check_pending_entry(coin, live)
                        if not pos["active"]:
                            continue
                        if pos["trailing_active"]:
                            self._manage_trailing(coin, live)
                        if self.positions[coin]["active"]:
                            self._check_tp_sl_hit(coin, live)
            else:
                time.sleep(LOOP_INTERVAL)

        self._cleanup()

    # ──────────────────────────────────────────────────────────
    # Traitement d'une paire
    # ──────────────────────────────────────────────────────────

    def _process_pair(self, pair, coin):
        """Cycle complet pour une paire : signal → sync → gestion → ouverture."""
        self.trader.pair = pair

        # Vérifier le solde disponible pour cette paire
        balance = self.trader._get_total_balance()
        usable = balance * (1 - RESERVE_BALANCE_PCT)
        min_col = MIN_COLLATERAL.get(pair, 10)
        if usable < min_col and not self.positions[coin]["active"]:
            if DEBUG:
                print(f"  [{coin}] Solde insuffisant ({usable:.2f} < {min_col}) — skip")
            return

        # 1. Signal (avec seuil auto-calibré) — journalisé par le moteur (V10)
        sig = self.engines[coin].compute_signals(score_threshold=self._signal_threshold)
        last_price = sig["debug"].get("close", 0)
        live_price = self.collector.get_live_price(coin) or last_price
        self._last_signal_scores[coin] = sig["score"]
        # Annuler pending entry si signal a changé de sens
        pending = self.positions[coin].get("pending_entry")
        if pending and sig["score"] != 0 and sig["score"] != pending.get("score"):
            if (pending["direction"] == "buy" and sig["score"] < 0) or \
               (pending["direction"] == "sell" and sig["score"] > 0):
                self.positions[coin]["pending_entry"] = None
                print(f"[BOT][{coin}] ⚠️ Pending annulé — signal a changé de sens")

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        live_tag = f" | live: {live_price:.6g}" if live_price != last_price else ""
        print(f"[{ts}][{coin}] {sig['color']} Score: {sig['score']} (raw: {sig['raw_score']}) | "
              f"{sig['label']} | {last_price:.6g}{live_tag}")

        # 2. Sync exchange
        has_pos, pos_info = self.trader.has_open_position()

        # Position fermée par l'exchange (TP/SL atteint)
        if not has_pos and self.positions[coin]["active"]:
            self._handle_exchange_closure(coin, live_price)

        # 3. Gestion position existante
        if has_pos and self.positions[coin]["active"]:
            if self._should_reverse(coin, sig):
                print(f"[BOT][{coin}] Signal opposé confirmé — fermeture")
                result = self.trader.close_position(
                    reason="signal_reverse",
                    context=self._position_context(coin, closing_signal_id=sig.get("signal_id")))
                if result:
                    self.risk.register_trade_result(result["pnl"])
                    self._adjust_cooldown(coin, result["pnl"])
                self._last_trade_times[coin] = time.time()
                self.positions[coin] = self._empty_position()
            else:
                self._manage_trailing(coin, live_price)

        # 4. Ouverture si pas de position
        elif not has_pos:
            self._try_open_position(coin, sig, live_price)

        if DEBUG:
            risk_status = self.risk.status()
            trailing_state = "ON" if self.positions[coin]["trailing_active"] else "OFF"
            print(f"  [DEBUG][{coin}] pos={has_pos} | trailing={trailing_state} | "
                  f"losses={risk_status['consecutive_losses']} | pnl_day={risk_status['pnl_today']:+.2f}")

    # ──────────────────────────────────────────────────────────
    # Logique de trading (moteur conservé)
    # ──────────────────────────────────────────────────────────

    def _position_context(self, coin, closing_signal_id=None):
        """Contexte de journalisation d'une position (lien trade ↔ signal V10)."""
        pos = self.positions.get(coin, {})
        ctx = {
            "coin": coin,
            "signal_id": pos.get("signal_id"),
            "signal_score": pos.get("signal_score"),
            "raw_score": pos.get("raw_score"),
            "regime": pos.get("regime"),
            "entry_features": pos.get("entry_features"),
        }
        if closing_signal_id:
            ctx["closing_signal_id"] = closing_signal_id
        return ctx

    def _should_reverse(self, coin, sig):
        """Ferme la position seulement après SIGNAL_CONFIRM_COUNT signaux opposés consécutifs."""
        pos = self.positions[coin]
        if not pos["active"]:
            self._reverse_streaks[coin] = 0
            return False

        side = pos["side"]
        is_opposite = (side == "buy" and sig["score"] == -2) or \
                      (side == "sell" and sig["score"] == 2)

        if is_opposite:
            self._reverse_streaks[coin] += 1
        else:
            self._reverse_streaks[coin] = 0
            return False

        if self._reverse_streaks[coin] >= SIGNAL_CONFIRM_COUNT:
            self._reverse_streaks[coin] = 0
            return True

        if DEBUG:
            print(f"  [REVERSE][{coin}] Signal opposé {self._reverse_streaks[coin]}/{SIGNAL_CONFIRM_COUNT}")
        return False

    def _try_open_position(self, coin, sig, price):
        """Tente d'ouvrir une position si le signal est fort ET confirmé."""
        if self.positions[coin].get("pending_entry"):
            return

        if sig["score"] not in (2, -2):
            self._signal_streaks[coin] = 0
            self._signal_dirs[coin] = 0
            return

        if sig["score"] == self._signal_dirs[coin]:
            self._signal_streaks[coin] += 1
        else:
            self._signal_streaks[coin] = 1
            self._signal_dirs[coin] = sig["score"]

        if self._signal_streaks[coin] < SIGNAL_CONFIRM_COUNT:
            if DEBUG:
                print(f"  [CONFIRM][{coin}] Signal fort {sig['score']} "
                      f"({self._signal_streaks[coin]}/{SIGNAL_CONFIRM_COUNT})")
            return

        # Cooldown dynamique
        elapsed = time.time() - self._last_trade_times[coin]
        cooldown = self._cooldowns[coin]
        if elapsed < cooldown:
            remaining = int(cooldown - elapsed)
            if DEBUG:
                print(f"  [COOLDOWN][{coin}] {remaining}s restantes (cooldown={cooldown:.0f}s)")
            return

        # Reset streaks
        self._signal_streaks[coin] = 0
        self._reverse_streaks[coin] = 0

        self.notifier.signal_alert(
            coin, sig["score"], sig["raw_score"],
            sig["label"], sig["color"], price, sig["debug"]
        )

        side = "buy" if sig["score"] == 2 else "sell"

        # Risk manager
        balance = self.trader._get_total_balance()
        can_trade, reason = self.risk.can_trade(current_balance=balance)
        if not can_trade:
            print(f"[BOT][{coin}] Trading bloqué: {reason}")
            self.notifier.risk_alert(reason)
            self._log_decision(coin, sig, side, "refused", f"risk: {reason}", price)
            return

        # ── Circuit breaker marché ──
        tripped, cb_reasons = self._check_market_breaker(sig)
        if tripped:
            joined = ", ".join(cb_reasons)
            print(f"[BOT][{coin}] 🚧 Circuit breaker marché — entrée bloquée : {joined}")
            self.notifier.risk_alert(f"Circuit breaker [{coin}] : {joined}")
            self._log_decision(coin, sig, side, "refused", f"circuit_breaker: {joined}", price)
            return

        # ── Filtre corrélation ──
        blocked, corr_boost = self._check_correlation(coin, side)
        if blocked:
            print(f"[BOT][{coin}] ⚡ Bloqué — conflit corrélation avec paire sœur")
            self._log_decision(coin, sig, side, "refused", "correlation", price)
            return
        if corr_boost > 0 and DEBUG:
            print(f"  [CORR][{coin}] Signal corroboré par paire sœur → size_boost +{corr_boost*100:.0f}%")

        size_factor = min(1.0, self._compute_size_factor(sig) + corr_boost)

        # ── Garde-fou exposition globale ──
        allowed, exp_reason = self._check_exposure(coin, side, size_factor, balance)
        if not allowed:
            print(f"[BOT][{coin}] 🛡️ Exposition — entrée bloquée : {exp_reason}")
            self.notifier.risk_alert(f"Exposition [{coin}] : {exp_reason}")
            self._log_decision(coin, sig, side, "refused", f"exposure: {exp_reason}", price)
            return

        # ── Pullback entry — arrondi coin-agnostique (V10) ──
        if side == "buy":
            target = round_price_sig(price * (1 - PULLBACK_PCT))
        else:
            target = round_price_sig(price * (1 + PULLBACK_PCT))

        self.positions[coin]["pending_entry"] = {
            "direction": side,
            "target_price": target,
            "expiry_ts": time.time() + PULLBACK_EXPIRY_SEC,
            "sig": sig,
            "size_factor": size_factor,
            "score": sig["score"],
        }
        print(f"[BOT][{coin}] ⏳ En attente pullback @ {target:.6g} "
              f"(actuel={price:.6g}, expiry={PULLBACK_EXPIRY_SEC}s)")
        self._log_decision(coin, sig, side, "accepted", "ok", price, size_factor)

    def _check_pending_entry(self, coin, live_price):
        """Déclenche l'entrée quand le pullback est atteint ou expiré."""
        pos = self.positions[coin]
        pending = pos.get("pending_entry")
        if not pending:
            return

        direction = pending["direction"]
        target = pending["target_price"]
        expiry = pending["expiry_ts"]
        sig = pending["sig"]
        size_factor = pending["size_factor"]

        pullback_hit = (direction == "buy" and live_price <= target) or \
                       (direction == "sell" and live_price >= target)
        expired = time.time() > expiry

        if pullback_hit:
            print(f"[BOT][{coin}] 🎯 Pullback @ {live_price:.6g} (target={target:.6g}) — entrée!")
        elif expired:
            print(f"[BOT][{coin}] ⏰ Pullback expiré — entrée au marché @ {live_price:.6g}")
        else:
            return  # Pas encore

        pos["pending_entry"] = None
        self._execute_entry(coin, sig, live_price, size_factor)

    def _execute_entry(self, coin, sig, price, size_factor=1.0):
        """Place réellement l'ordre d'entrée et met à jour l'état de la position."""
        side = "buy" if sig["score"] == 2 else "sell"
        tp = sig.get("dynamic_tp") or TP_PCT
        sl = sig.get("dynamic_sl") or SL_PCT

        # Lien trade ↔ signal (V10) : id + snapshot des features à l'entrée
        entry_context = {
            "coin": coin,
            "signal_id": sig.get("signal_id"),
            "signal_score": sig.get("score"),
            "raw_score": sig.get("raw_score"),
            "regime": sig.get("regime"),
            "entry_features": sig.get("debug"),
        }

        result = self.trader.place_order_with_tp_sl(
            side, price, tp_pct=tp, sl_pct=sl, size_factor=size_factor,
            context=entry_context)
        if result:
            self._last_trade_times[coin] = time.time()
            atr_pct = sig["debug"].get("atr_pct", 0.001)
            # Planchers alignés sur config : le trade doit être bien en profit avant de protéger
            trail_distance = max(atr_pct * 1.5, TRAIL_PCT)           # min 0.6%
            trail_trigger  = max(atr_pct * 2.0, TRAILING_TRIGGER_PCT) # min 1.2%
            trail_step     = max(atr_pct * 0.5, TRAILING_STEP_PCT)    # min 0.3%

            self.positions[coin].update({
                "active": True,
                "entry": price,
                "side": side,
                "size": result.get("size", 0),
                "trailing": None,
                "trailing_active": False,
                "trail_distance": trail_distance,
                "trail_trigger": trail_trigger,
                "trail_step": trail_step,
                "open_time": time.time(),
                "best_price": price,
                "initial_tp_dist": tp,
                "current_tp": result.get("tp_price", 0),
                "sl_price": result.get("sl_price", 0),
                "sl_order_id": result.get("sl_order_id"),
                "tp_order_id": result.get("tp_order_id"),
                "breakeven_done": False,
                "pending_entry": None,
                # Lien trade ↔ signal (V10)
                "signal_id": sig.get("signal_id"),
                "signal_score": sig.get("score"),
                "raw_score": sig.get("raw_score"),
                "regime": sig.get("regime"),
                "entry_features": sig.get("debug"),
            })
            rr = tp / sl if sl > 0 else 0
            print(f"[BOT][{coin}] ✅ {side.upper()} @ {price:.6g} | TP: {result['tp_price']:.6g} | "
                  f"SL: {result['sl_price']:.6g} | R:R={rr:.1f}:1 | "
                  f"trail: {trail_distance*100:.2f}% / trigger: {trail_trigger*100:.2f}%")

    def _check_correlation(self, coin, direction):
        """Filtre corrélation (gestion risque drawdown) — conservé de v8.5.

        - BLOQUÉ si une paire sœur a une position MÊME DIRECTION active ;
        - size_boost=0.15 si une paire sœur a un signal confirmé même sens
          sans position active.
        """
        for other_coin in COINS:
            if other_coin == coin:
                continue
            other_pos  = self.positions[other_coin]
            other_score = self._last_signal_scores.get(other_coin, 0)

            if other_pos["active"] and other_pos.get("side"):
                other_side = other_pos["side"]
                same_dir = (direction == "buy"  and other_side == "buy") or \
                           (direction == "sell" and other_side == "sell")
                if same_dir:
                    print(f"  [CORR][{coin}] Bloqué — {other_coin} déjà {other_side} "
                          f"(même direction, risque drawdown concentré)")
                    return True, 0.0  # BLOQUÉ

            if not other_pos["active"]:
                if (direction == "buy"  and other_score == 2) or \
                   (direction == "sell" and other_score == -2):
                    return False, 0.15  # Boost +15% — signal confirmé par paire sœur

        return False, 0.0

    def _decisions_col(self):
        """Collection Mongo du journal de décision (client mis en cache)."""
        if getattr(self, "_dec_client", None) is None:
            self._dec_client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        return self._dec_client[MONGO_DB][MONGO_COLLECTION_DECISIONS]

    def _log_decision(self, coin, sig, side, action, reason, price, size_factor=None):
        """Journalise une décision d'entrée (acceptée/refusée) en Mongo."""
        try:
            doc = build_decision_doc(
                coin, sig, side, action, reason, price, size_factor,
                int(time.time() * 1000),
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._decisions_col().insert_one(doc)
        except Exception as e:
            print(f"[BOT][{coin}] Erreur log decision: {e}")

    def _check_market_breaker(self, sig):
        """Circuit breaker marché : bloque l'entrée si conditions extrêmes."""
        dbg = sig.get("debug", {})
        metrics = {
            "atr_pct":          dbg.get("atr_pct"),
            "funding_rate":     dbg.get("funding_rate"),
            "candle_range_pct": dbg.get("candle_range_pct"),
            "spread_pct":       dbg.get("spread_pct"),
            "ob_depth_ratio":   dbg.get("ob_depth_ratio"),
        }
        thresholds = {
            "max_atr_pct":          CB_MAX_ATR_PCT,
            "max_abs_funding":      CB_MAX_ABS_FUNDING,
            "max_candle_range_pct": CB_MAX_CANDLE_RANGE_PCT,
            "max_spread_pct":       CB_MAX_SPREAD_PCT,
            "min_ob_depth_ratio":   CB_MIN_OB_DEPTH_RATIO,
        }
        return market_circuit_breaker(metrics, thresholds)

    def _notional(self, balance, sf):
        """Notionnel estimé d'une position : usable × POSITION_SIZE_PCT × clamp(factor)."""
        usable = balance * (1 - RESERVE_BALANCE_PCT)
        return usable * POSITION_SIZE_PCT * max(0.3, min(1.0, sf))

    def _check_exposure(self, coin, side, cand_size_factor, balance):
        """Garde-fou exposition globale — conservé de v8.9."""
        open_positions = []
        for c in COINS:
            if c == coin:
                continue
            pos = self.positions[c]
            if pos.get("active") and pos.get("side"):
                notional = float(pos.get("size", 0)) * float(pos.get("entry", 0) or 0)
                open_positions.append({"side": pos["side"], "notional": notional})
            else:
                pending = pos.get("pending_entry")
                if pending and pending.get("direction"):
                    notional = self._notional(balance, pending.get("size_factor", 1.0))
                    open_positions.append({"side": pending["direction"], "notional": notional})

        candidate_notional = self._notional(balance, cand_size_factor)
        return exposure_check(
            open_positions, side, candidate_notional, balance,
            MAX_OPEN_POSITIONS, MAX_POSITIONS_PER_DIR, MAX_TOTAL_EXPOSURE_PCT,
        )

    def _handle_exchange_closure(self, coin, fallback_price):
        """Traite une fermeture détectée sur l'exchange (TP/SL atteint)."""
        pos = self.positions[coin]
        entry = pos.get("entry", 0)
        side = pos.get("side", "buy")
        size = pos.get("size", 0)
        open_time = pos.get("open_time", time.time() - 3600)

        since_ms = int(open_time * 1000)
        last_fill = self.trader.get_last_closed_trade(since_ms=since_ms)
        exit_price = last_fill["price"] if (last_fill and last_fill["price"] > 0) else fallback_price

        pnl = (exit_price - entry) * size if side == "buy" else (entry - exit_price) * size

        print(f"[BOT][{coin}] ⚡ Fermé par l'exchange | {entry:.6g} → {exit_price:.6g} | PnL: {pnl:+.4f}")

        self.trader.cancel_open_orders()
        self.notifier.trade_closed(self.trader.pair, side, entry, exit_price, pnl, "tp_sl_exchange")
        self.risk.register_trade_result(pnl)
        self._adjust_cooldown(coin, pnl)
        self._last_trade_times[coin] = time.time()
        self.trader.logger.log_trade({
            "pair": self.trader.pair,
            "side": side,
            "action": "close",
            "entry_price": entry,
            "exit_price": exit_price,
            "size": size,
            "pnl": pnl,
            "reason": "tp_sl_exchange",
        }, context=self._position_context(coin))
        self.positions[coin] = self._empty_position()

    def _check_tp_sl_hit(self, coin, live_price):
        """Détecte si le prix live a croisé le TP ou SL — confirme avec l'exchange."""
        pos = self.positions[coin]
        side = pos.get("side")
        current_tp = pos.get("current_tp", 0)
        sl_price = pos.get("sl_price", 0)

        tp_hit = sl_hit = False
        if side == "buy":
            tp_hit = current_tp > 0 and live_price >= current_tp
            sl_hit = sl_price > 0 and live_price <= sl_price
        elif side == "sell":
            tp_hit = current_tp > 0 and live_price <= current_tp
            sl_hit = sl_price > 0 and live_price >= sl_price

        if tp_hit or sl_hit:
            tag = "TP" if tp_hit else "SL"
            if DEBUG:
                print(f"[BOT][{coin}] ⚡ Prix live {live_price:.6g} a croisé le {tag}")
            # Laisser l'exchange confirmer la fermeture (TP/SL exchange)
            has_pos, _ = self.trader.has_open_position()
            if not has_pos and self.positions[coin]["active"]:
                self._handle_exchange_closure(coin, live_price)

    def _compute_size_factor(self, sig):
        """Facteur de taille [0.3, 1.0] : signal × volatilité (ATR) × drawdown."""
        risk_status = self.risk.status()
        factor = size_factor(
            raw_score              = sig.get("raw_score", 10),
            dynamic_sl             = sig.get("dynamic_sl"),
            sl_pct                 = SL_PCT,
            pnl_today              = risk_status.get("pnl_today", 0),
            daily_start_balance    = risk_status.get("daily_start_balance"),
            max_daily_drawdown_pct = MAX_DAILY_DRAWDOWN_PCT,
        )
        # Taille adaptée au régime de marché
        factor = round(factor * sig.get("regime_size_mult", 1.0), 2)
        if DEBUG:
            dyn_sl = sig.get("dynamic_sl") or SL_PCT
            print(f"  [SIZE] raw={sig.get('raw_score', 10)} SL={dyn_sl*100:.2f}% "
                  f"pnl_day={risk_status.get('pnl_today', 0):+.2f} "
                  f"regime×{sig.get('regime_size_mult', 1.0)} → {factor:.2f}")
        return factor

    def _manage_trailing(self, coin, last_price):
        """Trailing profit + breakeven stop + trailing stop pour une paire.

        MOTEUR CONSERVÉ (seule sortie performante de v8) — seul l'arrondi des
        prix devient coin-agnostique (round_price_sig).
        """
        pos = self.positions[coin]
        entry = pos["entry"]
        side = pos["side"]

        # Garde-fou : si entry est manquant ou nul, on ne peut rien calculer
        if not entry or not side or not last_price:
            return

        trail_dist = pos.get("trail_distance", TRAIL_PCT)
        trail_trig = pos.get("trail_trigger", TRAILING_TRIGGER_PCT)
        trail_step = pos.get("trail_step", TRAILING_STEP_PCT)

        gain_pct = (last_price - entry) / entry if side == "buy" else (entry - last_price) / entry

        # --- Trailing Profit ---
        best = pos.get("best_price") or entry  # fallback si best_price est None
        initial_tp_dist = pos.get("initial_tp_dist", 0)

        if side == "buy" and last_price > best:
            pos["best_price"] = last_price
            if initial_tp_dist > 0:
                new_tp = last_price * (1 + initial_tp_dist * 0.5)
                if new_tp > pos.get("current_tp", 0):
                    new_tp_order = self.trader.update_tp(
                        new_tp, old_tp_order_id=pos.get("tp_order_id")
                    )
                    if new_tp_order is not None:
                        pos["current_tp"] = new_tp
                        pos["tp_order_id"] = new_tp_order.get("id")
                        print(f"[BOT][{coin}] 🎯 TP → {new_tp:.6g}")
                    else:
                        print(f"[BOT][{coin}] ⚠️ TP update ECHEC @ {new_tp:.6g}")

        elif side == "sell" and last_price < best:
            pos["best_price"] = last_price
            if initial_tp_dist > 0:
                new_tp = last_price * (1 - initial_tp_dist * 0.5)
                if new_tp < pos.get("current_tp", float("inf")):
                    new_tp_order = self.trader.update_tp(
                        new_tp, old_tp_order_id=pos.get("tp_order_id")
                    )
                    if new_tp_order is not None:
                        pos["current_tp"] = new_tp
                        pos["tp_order_id"] = new_tp_order.get("id")
                        print(f"[BOT][{coin}] 🎯 TP → {new_tp:.6g}")
                    else:
                        print(f"[BOT][{coin}] ⚠️ TP update ECHEC @ {new_tp:.6g}")

        # --- Breakeven Stop ---
        if not pos.get("breakeven_done", False) and gain_pct >= BREAKEVEN_TRIGGER_PCT:
            breakeven_sl = round_price_sig(entry * (1 + BREAKEVEN_OFFSET_PCT)) if side == "buy" \
                           else round_price_sig(entry * (1 - BREAKEVEN_OFFSET_PCT))
            old_sl = pos.get("sl_price", 0)
            is_better = (side == "buy" and breakeven_sl > old_sl) or \
                        (side == "sell" and breakeven_sl < old_sl)
            if is_better:
                print(f"[BOT][{coin}] 🛡️ Breakeven @ {breakeven_sl:.6g} (gain: {gain_pct*100:.2f}%)")
                new_sl_order = self.trader.update_sl(
                    breakeven_sl,
                    old_sl_order_id=pos.get("sl_order_id")
                )
                if new_sl_order is not None:
                    # Confirmer seulement si l'exchange a bien placé le nouvel ordre
                    pos["sl_price"] = breakeven_sl
                    pos["sl_order_id"] = new_sl_order.get("id")
                    pos["breakeven_done"] = True
                else:
                    print(f"[BOT][{coin}] ⚠️ Breakeven SL ECHEC — sera retenté au prochain cycle")

        # --- Trailing Stop ---
        if not pos["trailing_active"] and gain_pct >= trail_trig:
            pos["trailing"] = last_price * (1 - trail_dist) if side == "buy" \
                              else last_price * (1 + trail_dist)
            pos["trailing_active"] = True
            print(f"[BOT][{coin}] 📈 Trailing activé @ {pos['trailing']:.6g} "
                  f"(gain: {gain_pct*100:.2f}%)")

        if pos["trailing_active"]:
            trailing = pos["trailing"]
            if side == "buy":
                new_trailing = last_price * (1 - trail_dist)
                if new_trailing > trailing + (entry * trail_step):
                    pos["trailing"] = new_trailing
                    print(f"[BOT][{coin}] 📈 Trailing → {new_trailing:.6g}")
                elif last_price <= trailing:
                    print(f"[BOT][{coin}] 🔔 Trailing touché ({last_price:.6g} <= {trailing:.6g})")
                    result = self.trader.close_position(
                        reason="trailing_stop", context=self._position_context(coin))
                    if result:
                        self.risk.register_trade_result(result["pnl"])
                        self._adjust_cooldown(coin, result["pnl"])
                    self._last_trade_times[coin] = time.time()
                    self.positions[coin] = self._empty_position()
            elif side == "sell":
                new_trailing = last_price * (1 + trail_dist)
                if new_trailing < trailing - (entry * trail_step):
                    pos["trailing"] = new_trailing
                    print(f"[BOT][{coin}] 📉 Trailing → {new_trailing:.6g}")
                elif last_price >= trailing:
                    print(f"[BOT][{coin}] 🔔 Trailing touché ({last_price:.6g} >= {trailing:.6g})")
                    result = self.trader.close_position(
                        reason="trailing_stop", context=self._position_context(coin))
                    if result:
                        self.risk.register_trade_result(result["pnl"])
                        self._adjust_cooldown(coin, result["pnl"])
                    self._last_trade_times[coin] = time.time()
                    self.positions[coin] = self._empty_position()

    # ──────────────────────────────────────────────────────────
    # Rapport journalier & Auto-calibration
    # ──────────────────────────────────────────────────────────

    def _send_daily_report(self):
        """Envoie un résumé journalier Telegram (trades, PnL, win rate)."""
        try:
            client = MongoClient(MONGO_URL)
            db = client[MONGO_DB]
            day_start_ms, today_start_ms, day_label = daily_report_window(
                datetime.now(timezone.utc)
            )
            trades = list(db[MONGO_COLLECTION_TRADES].find(
                {"action": "close",
                 "timestamp": {"$gte": day_start_ms, "$lt": today_start_ms}}
            ))
            balance = self.trader._get_total_balance()
            if not trades:
                self.notifier.send(
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
            for c in COINS:
                p = self.positions[c]
                if p["active"]:
                    open_parts.append(f"{c} {p['side'].upper()} @ {p['entry']:.6g}")
            open_str = " | ".join(open_parts) if open_parts else "Aucune"

            emoji = "📈" if total_pnl >= 0 else "📉"
            msg = (
                f"{emoji} <b>Bilan de la veille ({day_label})</b>\n"
                f"Trades: {len(trades)} | ✅ {len(wins)} gagnants / ❌ {len(losses)} perdants\n"
                f"Win rate: <b>{win_rate:.1f}%</b>\n"
                f"PnL total: <b>{total_pnl:+.4f} USDC</b>\n"
                f"Solde: <code>{balance:.2f} USDC</code>\n"
                f"Positions ouvertes: {open_str}"
            )
            self.notifier.send(msg)
        except Exception as e:
            print(f"[BOT] Daily report error: {e}")

    def _auto_calibrate(self):
        """Ajuste le seuil de signal selon les performances récentes."""
        try:
            client = MongoClient(MONGO_URL)
            db = client[MONGO_DB]
            trades = list(db[MONGO_COLLECTION_TRADES].find(
                {"action": "close"}
            ).sort("timestamp", -1).limit(AUTOCAL_LOOKBACK_TRADES))

            if len(trades) < 5:
                print("[BOT] Auto-cal: pas assez de trades pour calibrer")
                return

            wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
            win_rate = wins / len(trades)
            old_threshold = self._signal_threshold

            if win_rate < 0.40:
                self._signal_threshold = min(SIGNAL_THRESHOLD_MAX, self._signal_threshold + 1)
            elif win_rate > 0.60:
                self._signal_threshold = max(SIGNAL_THRESHOLD_MIN, self._signal_threshold - 1)

            if self._signal_threshold != old_threshold:
                self.notifier.send(
                    f"🔧 <b>Auto-calibration</b>\n"
                    f"Win rate ({len(trades)} trades): <b>{win_rate*100:.1f}%</b>\n"
                    f"Seuil: {old_threshold} → <b>{self._signal_threshold}</b>"
                )
            print(f"[BOT] Auto-cal: seuil={self._signal_threshold} | "
                  f"win_rate={win_rate*100:.1f}% ({wins}/{len(trades)})")
        except Exception as e:
            print(f"[BOT] Auto-cal error: {e}")

    def _adjust_cooldown(self, coin, pnl):
        """Allonge le cooldown après une perte, le réduit après un gain."""
        old = self._cooldowns[coin]
        if pnl < 0:
            new = min(COOLDOWN_MAX_SEC, old * COOLDOWN_LOSS_MULT)
            direction = "⬆"
        else:
            new = max(COOLDOWN_MIN_SEC, old * COOLDOWN_WIN_MULT)
            direction = "⬇"
        self._cooldowns[coin] = round(new)
        print(f"  [COOLDOWN][{coin}] {direction} {old:.0f}s → {new:.0f}s "
              f"({'perte' if pnl < 0 else 'gain'} {pnl:+.4f})")

    def _check_mongo_health(self) -> bool:
        """Vérifie que MongoDB est joignable au démarrage (3 tentatives)."""
        if not MONGO_URL:
            print("[BOT] ❌ MONGO_URL absent de l'environnement.")
            return False
        for attempt in range(3):
            try:
                client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")
                print("[BOT] ✅ MongoDB joignable.")
                return True
            except Exception as e:
                print(f"[BOT] ⚠️ MongoDB injoignable (tentative {attempt + 1}/3): {e}")
                time.sleep(5)
        return False

    def _check_kill_switch(self):
        import os
        if os.path.exists(KILL_SWITCH_FILE):
            print("[BOT] 🛑 KILL SWITCH actif")
            return True
        return False

    def _empty_position(self):
        return {
            "active": False,
            "entry": None,
            "side": None,
            "size": 0,
            "trail_distance": TRAIL_PCT,
            "trail_trigger": TRAILING_TRIGGER_PCT,
            "trail_step": TRAILING_STEP_PCT,
            "trailing": None,
            "trailing_active": False,
            "open_time": None,
            "best_price": None,
            "initial_tp_dist": 0,
            "current_tp": 0,
            "sl_price": 0,
            "sl_order_id": None,        # ID ordre SL sur l'exchange
            "tp_order_id": None,        # ID ordre TP sur l'exchange
            "breakeven_done": False,
            "pending_entry": None,
            # Lien trade ↔ signal (V10)
            "signal_id": None,
            "signal_score": None,
            "raw_score": None,
            "regime": None,
            "entry_features": None,
        }

    def _run_collector(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.collector.collect())
        except Exception as e:
            print(f"[COLLECTOR][FATAL] {e}")
        finally:
            loop.close()

    def _handle_shutdown(self, signum, frame):
        print(f"\n[BOT] Signal {signum} reçu, arrêt en cours...")
        self._shutdown = True

    def _cleanup(self):
        self.collector.stop()
        self.rest_collector.stop()
        if self.whale_collector is not None:
            self.whale_collector.stop()
        balance = self.trader._get_total_balance()
        risk_status = self.risk.status()
        self.notifier.bot_stopped(
            f"PnL jour: {risk_status['pnl_today']:+.2f} | Solde: {balance:.2f}"
        )
        print("[BOT] Arrêt complet.")


if __name__ == "__main__":
    import sys
    bot = TradingBot()
    bot.start()
    # Arrêt propre = code 0. Le redémarrage en prod est garanti par
    # fly.toml ([[restart]] policy = 'always'), pas par un code d'erreur.
    sys.exit(0)
