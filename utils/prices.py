"""
Arrondi de prix coin-agnostique (V10) — pur et testable.

v8 arrondissait les prix TP/SL/breakeven à 2 ou 4 décimales, ce qui fonctionnait
pour BTC/ETH/SOL mais écrase à zéro les prix « micro » (PEPE ~1e-5, WIF, DOGE).
Hyperliquid accepte 5 chiffres significatifs max sur les prix : on arrondit donc
en CHIFFRES SIGNIFICATIFS, pas en décimales.
"""

import math

HL_PRICE_SIG_FIGS = 5


def round_price_sig(price, sig_figs=HL_PRICE_SIG_FIGS):
    """Arrondit un prix à N chiffres significatifs (5 = convention Hyperliquid).

    round_price_sig(102345.67) → 102350.0
    round_price_sig(0.00001234567) → 1.2346e-05
    """
    if price is None or price == 0 or not math.isfinite(price):
        return price
    exponent = math.floor(math.log10(abs(price)))
    ndigits = sig_figs - 1 - exponent
    return round(price, ndigits)
