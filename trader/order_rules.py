"""
Décisions de passage d'ordre : symbole, taille, prix TP/SL.

Extraites de ccxt_trader.py, où elles étaient enchevêtrées avec les appels
réseau — donc invérifiables sans exchange, sur le code qui passera les ordres
réels.

Aucune de ces fonctions ne connaît ccxt : elles reçoivent des nombres et
rendent des nombres. C'est ce qui les rend testables hors ligne, et c'est le
seul moyen d'avoir un filet sur cette partie avant de quitter le paper mode.
"""

from utils.min_order import min_target_size
from utils.prices import round_price_sig

FACTEUR_MIN = 0.3      # jamais moins de 30 % de la taille de base
FACTEUR_MAX = 1.0      # ni plus que la taille pleine


def symbole_ccxt(pair):
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


def taille_ordre(base_size, prix, size_factor, min_collateral):
    """Taille à envoyer, ou None avec la raison du refus.

    Hyperliquid impose un notionnel minimal (~10 USDC). Trois issues :
    - la taille demandée le respecte : on la garde ;
    - elle est trop petite mais le solde permet le minimum : on remonte ;
    - même la taille pleine est sous le minimum : on renonce, plutôt que
      d'envoyer un ordre qui sera rejeté.

    Retourne (taille, note). `note` explique un ajustement ou un refus ; elle
    est journalisée par l'appelant.
    """
    taille = base_size * max(FACTEUR_MIN, min(FACTEUR_MAX, size_factor))
    if taille <= 0:
        return None, "pas assez de solde pour trader"

    cible = min_target_size(min_collateral, prix)
    if taille < cible:
        if base_size >= cible:
            return cible, f"taille remontée au minimum (+marge) : {cible * prix:.2f} USDC"
        return None, (f"solde insuffisant pour le minimum "
                      f"({base_size * prix:.2f} USDC < {min_collateral} USDC)")
    return taille, None


def prix_tp_sl(side, prix, tp_pct, sl_pct, arrondir=round_price_sig):
    """Prix de take-profit et de stop-loss, et sens de l'ordre de clôture.

    `arrondir` est injectable : le trader y passe la précision réelle de
    l'exchange, les tests un arrondi déterministe.
    """
    if side == "buy":
        return arrondir(prix * (1 + tp_pct)), arrondir(prix * (1 - sl_pct)), "sell"
    return arrondir(prix * (1 - tp_pct)), arrondir(prix * (1 + sl_pct)), "buy"


def est_ordre_stop(order):
    """Cet ordre ouvert est-il le stop-loss de la position ?

    Heuristique de repli quand l'ID de l'ordre a été perdu : on se fie au type
    annoncé par l'exchange, et à défaut au fait qu'un ordre `reduceOnly` qui ne
    se présente pas comme un take-profit est vraisemblablement le stop.
    """
    otype = (order.get("type") or "").lower()
    return bool("stop" in otype or (
        order.get("reduceOnly") and "take" not in otype and "profit" not in otype))


def est_ordre_take_profit(order):
    """Symétrique de est_ordre_stop, pour le take-profit."""
    otype = (order.get("type") or "").lower()
    return bool("take" in otype or "profit" in otype or (
        order.get("reduceOnly") and "stop" not in otype))


def montant_sur_exchange(exchange, symbole, taille, prix, min_collateral):
    """Arrondit la taille à la précision réelle de l'exchange, sans passer sous
    le notionnel minimal.

    L'arrondi peut faire chuter la taille sous le minimum : on remonte alors
    d'un cran de précision plutôt que d'envoyer un ordre qui sera rejeté. Si la
    précision est indisponible, repli sur six décimales.

    Seule fonction du module à toucher l'exchange, et uniquement pour lire ses
    règles d'arrondi — d'où l'injection plutôt qu'un import.
    """
    try:
        montant = float(exchange.amount_to_precision(symbole, taille))
        if montant * prix < min_collateral:
            precision = (exchange.market(symbole).get("precision") or {}).get("amount")
            if isinstance(precision, int):
                cran = 10 ** (-precision)
            elif isinstance(precision, (int, float)) and precision and precision > 0:
                cran = precision
            else:
                cran = 1e-6
            montant = float(exchange.amount_to_precision(symbole, montant + cran))
        return montant
    except Exception as e:
        print(f"[TRADER] montant_sur_exchange, repli round(6): {e}")
        return round(taille, 6)
