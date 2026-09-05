"""
Garde-fous d'entrée — adaptateurs entre l'état du bot et les primitives pures.

Les règles elles-mêmes vivent déjà dans `utils/` (`exposure_check`,
`market_circuit_breaker`) : ce module ne fait que rassembler l'état des
positions et la configuration pour les alimenter. Extrait de `main.py` où ces
adaptateurs étaient noyés parmi 29 méthodes, ce qui les rendait intestables —
aucun d'eux n'était couvert avant cette extraction.

Aucune de ces fonctions ne connaît `TradingBot` : elles reçoivent le
dictionnaire des positions, jamais l'objet. C'est ce qui les rend vérifiables.
"""

from config import (
    COINS,
    CB_MAX_ATR_PCT, CB_MAX_ABS_FUNDING, CB_MAX_CANDLE_RANGE_PCT,
    CB_MAX_SPREAD_PCT, CB_MIN_OB_DEPTH_RATIO,
    RESERVE_BALANCE_PCT, POSITION_SIZE_PCT,
    MAX_OPEN_POSITIONS, MAX_POSITIONS_PER_DIR, MAX_TOTAL_EXPOSURE_PCT,
    COOLDOWN_MAX_SEC, COOLDOWN_MIN_SEC, COOLDOWN_LOSS_MULT, COOLDOWN_WIN_MULT,
)
from utils.exposure import exposure_check
from utils.market_guard import market_circuit_breaker


def correlation_filter(coin, direction, positions, last_signal_scores, coins=COINS):
    """Filtre corrélation (gestion risque drawdown) — conservé de v8.5.

    - BLOQUÉ si une paire sœur a une position MÊME DIRECTION active ;
    - size_boost=0.15 si une paire sœur a un signal confirmé même sens
      sans position active.

    Retourne (bloqué: bool, boost: float).
    """
    for other_coin in coins:
        if other_coin == coin:
            continue
        other_pos = positions[other_coin]
        other_score = last_signal_scores.get(other_coin, 0)

        if other_pos["active"] and other_pos.get("side"):
            other_side = other_pos["side"]
            same_dir = (direction == "buy" and other_side == "buy") or \
                       (direction == "sell" and other_side == "sell")
            if same_dir:
                print(f"  [CORR][{coin}] Bloqué — {other_coin} déjà {other_side} "
                      f"(même direction, risque drawdown concentré)")
                return True, 0.0  # BLOQUÉ

        if not other_pos["active"]:
            if (direction == "buy" and other_score == 2) or \
               (direction == "sell" and other_score == -2):
                return False, 0.15  # Boost +15% — signal confirmé par paire sœur

    return False, 0.0


def market_breaker(sig):
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


def notional(balance, sf):
    """Notionnel estimé d'une position : usable × POSITION_SIZE_PCT × clamp(factor)."""
    usable = balance * (1 - RESERVE_BALANCE_PCT)
    return usable * POSITION_SIZE_PCT * max(0.3, min(1.0, sf))


def exposure_guard(coin, side, cand_size_factor, balance, positions, coins=COINS):
    """Garde-fou exposition globale — conservé de v8.9.

    Compte les positions actives ET les entrées en attente (pending_entry) :
    une entrée validée mais pas encore remplie engage déjà du capital.
    """
    open_positions = []
    for c in coins:
        if c == coin:
            continue
        pos = positions[c]
        if pos.get("active") and pos.get("side"):
            notionnel = float(pos.get("size", 0)) * float(pos.get("entry", 0) or 0)
            open_positions.append({"side": pos["side"], "notional": notionnel})
        else:
            pending = pos.get("pending_entry")
            if pending and pending.get("direction"):
                notionnel = notional(balance, pending.get("size_factor", 1.0))
                open_positions.append({"side": pending["direction"], "notional": notionnel})

    candidate_notional = notional(balance, cand_size_factor)
    return exposure_check(
        open_positions, side, candidate_notional, balance,
        MAX_OPEN_POSITIONS, MAX_POSITIONS_PER_DIR, MAX_TOTAL_EXPOSURE_PCT,
    )


def adjust_cooldown(cooldowns, coin, pnl):
    """Allonge le cooldown après une perte, le réduit après un gain.

    Modifie `cooldowns` en place et retourne la nouvelle valeur.
    """
    old = cooldowns[coin]
    if pnl < 0:
        new = min(COOLDOWN_MAX_SEC, old * COOLDOWN_LOSS_MULT)
        direction = "⬆"
    else:
        new = max(COOLDOWN_MIN_SEC, old * COOLDOWN_WIN_MULT)
        direction = "⬇"
    cooldowns[coin] = round(new)
    print(f"  [COOLDOWN][{coin}] {direction} {old:.0f}s → {new:.0f}s "
          f"({'perte' if pnl < 0 else 'gain'} {pnl:+.4f})")
    return cooldowns[coin]
