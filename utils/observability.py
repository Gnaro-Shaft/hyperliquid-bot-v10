"""
Builders purs pour l'observabilité — testables sans I/O (portés de v8.10).

- build_decision_doc : un enregistrement du journal de décision (collection `decisions`)
- build_bot_status   : le doc heartbeat de l'état du bot (collection `bot_status`)

V10 : build_decision_doc porte aussi signal_id + regime (lien décision ↔ signal).
"""


# Motifs de refus reconnus par le GCN Dashboard (ALLOWED_DECISION_MOTIFS).
_DASHBOARD_MOTIFS = {"risk", "circuit_breaker", "correlation", "exposure"}


def build_decision_doc(coin, sig, side, action, reason, price,
                       size_factor, now_ms, now_str):
    """Construit un enregistrement de décision (ouverture acceptée / refusée).

    action : "accepted" | "refused"
    reason : "ok" ou le motif du refus (gate concerné, ex "exposure: max positions")
    """
    dbg = sig.get("debug", {}) or {}
    motif = (reason or "").split(":")[0].strip()
    motif = motif if motif in _DASHBOARD_MOTIFS else None
    return {
        "timestamp":    int(now_ms),
        "created_at":   int(now_ms),    # contrat dashboard (champ de tri)
        "datetime":     now_str,
        "coin":         coin,
        "action":       action,
        "status":       action,         # contrat dashboard
        "reason":       reason,
        "motif":        motif,          # contrat dashboard (catégorie du refus)
        "side":         side,
        "score":        sig.get("score"),
        "raw_score":    sig.get("raw_score"),
        "signal_id":    sig.get("signal_id"),   # V10 : lien décision ↔ signal
        "regime":       sig.get("regime"),      # V10 : régime sur chaque ligne
        "price":        float(price) if price is not None else None,
        "size_factor":  round(size_factor, 3) if size_factor is not None else None,
        "tp_pct":       sig.get("dynamic_tp"),
        "sl_pct":       sig.get("dynamic_sl"),
        "atr_pct":      dbg.get("atr_pct"),
        "funding_rate": dbg.get("funding_rate"),
        "ml_gate":      dbg.get("gate_ml"),
    }


def build_bot_status(metrics, risk_status, positions, kill_switch, now_ms, now_str):
    """Construit le doc heartbeat de l'état du bot (upsert _id='current')."""
    positions_detail = []
    for coin, p in (positions or {}).items():
        if p.get("active"):
            positions_detail.append({
                "coin":  coin,
                "side":  p.get("side"),
                "entry": p.get("entry"),
                "size":  p.get("size"),
            })

    pnl_today = risk_status.get("pnl_today")
    return {
        "_id":                "current",
        "timestamp":          int(now_ms),
        "datetime":           now_str,
        "running":            True,
        "ws_alive":           metrics.get("ws_alive"),
        "mongo_ok":           metrics.get("mongo_ok"),
        "last_1m_age_s":      metrics.get("last_1m_age_s"),
        "last_15m_age_s":     metrics.get("last_15m_age_s"),
        "stale_streams":      metrics.get("stale_streams"),   # V10
        "balance":            metrics.get("balance"),
        "pnl_today":          pnl_today,
        "daily_pnl":          pnl_today,          # alias contrat widget BotStatus
        "consecutive_losses": risk_status.get("consecutive_losses"),
        "paused":             risk_status.get("paused"),
        "kill_switch":        kill_switch,
        "open_positions":     len(positions_detail),
        "n_open_positions":   len(positions_detail),
        "positions":          positions_detail,
    }
