"""
Aides au dimensionnement minimum d'ordre — pures et testables.

Hyperliquid exige un notionnel minimum (~$10). On vise une marge au-dessus pour
résister à l'arrondi de précision de l'exchange (sinon rejet « Order must have minimum »).
"""


def min_target_size(min_col, price, margin=1.20):
    """Taille (en contrats) visant min_col × marge de notionnel au prix donné."""
    if price <= 0:
        return 0.0
    return (min_col * margin) / price


def meets_minimum(size, price, min_col):
    """True si la taille atteint le notionnel minimum requis."""
    return size * price >= min_col
