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


from config import (
    PAIRS, COINS, DEBUG, TP_PCT, SL_PCT, TRAIL_PCT, PAPER_MODE,
    TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
    KILL_SWITCH_FILE,
    SIGNAL_CONFIRM_COUNT, LOOP_INTERVAL, TRAILING_CHECK_INTERVAL,
    MAX_DAILY_DRAWDOWN_PCT,
    MIN_COLLATERAL, RESERVE_BALANCE_PCT,
    PULLBACK_PCT, PULLBACK_EXPIRY_SEC,
    SIGNAL_THRESHOLD_DEFAULT,
    MONGO_URL,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI, MONGO_COLLECTION_ORDERBOOK,
    COOLDOWN_BASE_SEC,
    WHALE_ENABLED,
)
from strategy.strategy_engine import StrategyEngine
from trader.ccxt_trader import HyperliquidTrader
from trader.paper_trader import PaperTrader
from collector.context_store import MarketContextStore
from collector.candle_store import CandleStore
from collector.websocket_collector import WebSocketCollector
from collector.rest_collector import RestCollector
from collector.whale_collector import WhaleCollector
from datalog.signal_logger import SignalLogger
from risk.risk_manager import RiskManager
from risk import guards
from trader.position_manager import PositionManager, empty_position
from datalog import reporting
from datalog.decision_log import log_decision
from utils import mongo
from utils.notifier import Notifier
from utils.sizing import size_factor
from utils.prices import round_price_sig
from monitor.health import HealthMonitor



class TradingBot:
    def __init__(self):
        # ── Plomberie V10 : stores partagés + logger de signaux ──
        self.context_store = MarketContextStore(COINS)
        self.candle_store = CandleStore(COINS)   # cache bougies (anti-throttle M0)
        self.signal_logger = SignalLogger()

        self.collector = WebSocketCollector(context_store=self.context_store,
                                            candle_store=self.candle_store)
        self.rest_collector = RestCollector(context_store=self.context_store)
        self.whale_collector = WhaleCollector() if WHALE_ENABLED else None
        self.trader = PaperTrader() if PAPER_MODE else HyperliquidTrader()
        self.risk = RiskManager()
        self.notifier = Notifier()
        self._shutdown = False
        self._last_daily_reset = None

        # --- État multi-paires (un dict par coin) ---
        self.positions = {coin: empty_position() for coin in COINS}
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

        # --- Gestion des positions ouvertes ---
        # Reçoit le dict `positions` lui-même : les entrées sont modifiées en
        # place des deux côtés, jamais réaffectées en bloc.
        self.pos_mgr = PositionManager(
            self.trader, self.risk, self.notifier,
            self.positions, self._cooldowns, self._last_trade_times)

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
            db = mongo.get_db()
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
                candle_store=self.candle_store,
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
                    **empty_position(),
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
                        reporting.send_daily_report(
                            self.notifier, self.trader, self.positions)
                    balance = self.trader._get_total_balance()
                    self.risk.reset_daily(balance)
                    self._last_daily_reset = today
                    if self._last_autocal_date is None or \
                       (today - self._last_autocal_date).days >= 7:
                        self._signal_threshold = reporting.auto_calibrate(
                            self._signal_threshold, self.notifier)
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
                            self.pos_mgr.manage_trailing(coin, live)
                        if self.positions[coin]["active"]:
                            self.pos_mgr.check_tp_sl_hit(coin, live)
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
            self.pos_mgr.handle_exchange_closure(coin, live_price)

        # 3. Gestion position existante
        if has_pos and self.positions[coin]["active"]:
            if self._should_reverse(coin, sig):
                print(f"[BOT][{coin}] Signal opposé confirmé — fermeture")
                result = self.trader.close_position(
                    reason="signal_reverse",
                    context=self.pos_mgr.context(coin, closing_signal_id=sig.get("signal_id")))
                if result:
                    self.risk.register_trade_result(result["pnl"])
                    guards.adjust_cooldown(self._cooldowns, coin, result["pnl"])
                self._last_trade_times[coin] = time.time()
                self.positions[coin] = empty_position()
            else:
                self.pos_mgr.manage_trailing(coin, live_price)

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
            log_decision(coin, sig, side, "refused", f"risk: {reason}", price)
            return

        # ── Circuit breaker marché ──
        tripped, cb_reasons = guards.market_breaker(sig)
        if tripped:
            joined = ", ".join(cb_reasons)
            print(f"[BOT][{coin}] 🚧 Circuit breaker marché — entrée bloquée : {joined}")
            self.notifier.risk_alert(f"Circuit breaker [{coin}] : {joined}")
            log_decision(coin, sig, side, "refused", f"circuit_breaker: {joined}", price)
            return

        # ── Filtre corrélation ──
        blocked, corr_boost = guards.correlation_filter(
            coin, side, self.positions, self._last_signal_scores)
        if blocked:
            print(f"[BOT][{coin}] ⚡ Bloqué — conflit corrélation avec paire sœur")
            log_decision(coin, sig, side, "refused", "correlation", price)
            return
        if corr_boost > 0 and DEBUG:
            print(f"  [CORR][{coin}] Signal corroboré par paire sœur → size_boost +{corr_boost*100:.0f}%")

        size_factor = min(1.0, self._compute_size_factor(sig) + corr_boost)

        # ── Garde-fou exposition globale ──
        allowed, exp_reason = guards.exposure_guard(
            coin, side, size_factor, balance, self.positions)
        if not allowed:
            print(f"[BOT][{coin}] 🛡️ Exposition — entrée bloquée : {exp_reason}")
            self.notifier.risk_alert(f"Exposition [{coin}] : {exp_reason}")
            log_decision(coin, sig, side, "refused", f"exposure: {exp_reason}", price)
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
        log_decision(coin, sig, side, "accepted", "ok", price, size_factor)

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

    def _check_mongo_health(self) -> bool:
        """Vérifie que MongoDB est joignable au démarrage (3 tentatives)."""
        if not MONGO_URL:
            print("[BOT] ❌ MONGO_URL absent de l'environnement.")
            return False
        for attempt in range(3):
            try:
                mongo.ping()
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
        mongo.close()
        print("[BOT] Arrêt complet.")


if __name__ == "__main__":
    import sys
    bot = TradingBot()
    bot.start()
    # Arrêt propre = code 0. Le redémarrage en prod est garanti par
    # fly.toml ([[restart]] policy = 'always'), pas par un code d'erreur.
    sys.exit(0)
