"""
Simulation pure pour le paper trading (v8.11) — testable sans I/O.

- compute_tp_sl     : niveaux TP/SL à partir de l'entrée et des pourcentages
- simulate_candle_fill : un TP/SL est-il touché dans une bougie ? (priorité SL = pessimiste)
- compute_pnl       : PnL net d'une position fermée, frais inclus
"""

FEE_RATE = 0.0005   # 0.05% par leg → 0.1% aller-retour (taker Hyperliquid)


def compute_tp_sl(side, entry, tp_pct, sl_pct):
    """Retourne (tp_price, sl_price) pour une position long ('buy') ou short ('sell')."""
    if side == "buy":
        return entry * (1 + tp_pct), entry * (1 - sl_pct)
    return entry * (1 - tp_pct), entry * (1 + sl_pct)


def simulate_candle_fill(position, candle, sl_priority=True):
    """Détermine si la position est clôturée par TP ou SL dans la bougie donnée.

    position : {side, tp_price, sl_price}
    candle   : {high, low}
    sl_priority : si TP et SL sont tous deux touchés dans la même bougie, on
                  suppose le SL d'abord (hypothèse pessimiste, réaliste).

    Retourne (closed: bool, exit_price: float|None, reason: str|None).
    """
    side = position["side"]
    tp, sl = position["tp_price"], position["sl_price"]
    high, low = candle["high"], candle["low"]

    if side == "buy":
        hit_tp, hit_sl = high >= tp, low <= sl
    else:
        hit_tp, hit_sl = low <= tp, high >= sl

    if hit_tp and hit_sl:
        return (True, sl, "sl") if sl_priority else (True, tp, "tp")
    if hit_sl:
        return True, sl, "sl"
    if hit_tp:
        return True, tp, "tp"
    return False, None, None


def compute_pnl(side, entry, exit_price, size, fee_rate=FEE_RATE):
    """PnL net (frais aller-retour inclus) d'une position fermée."""
    gross = (exit_price - entry) * size if side == "buy" else (entry - exit_price) * size
    fee = (size * entry + size * exit_price) * fee_rate
    return gross - fee
