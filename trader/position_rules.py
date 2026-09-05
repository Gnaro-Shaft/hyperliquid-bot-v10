"""
Décisions arithmétiques de gestion d'une position ouverte.

Le trailing stop est, d'après le README, « la seule sortie performante de v8 ».
Il vivait dans une méthode de 118 lignes qui mêlait calculs, appels à l'exchange
et journalisation — donc invérifiable autrement qu'en production, sur de
l'argent.

Ce module ne contient que les décisions : aucune ne connaît l'exchange, aucune
ne modifie quoi que ce soit. Elles répondent à « quel niveau ? » et « faut-il
agir ? ». L'orchestration et les effets restent ailleurs.

Le comportement est repris à l'identique, asymétries comprises — elles sont
signalées là où elles se trouvent plutôt que corrigées en passant.
"""

from utils.prices import round_price_sig


def gain_pct(entry, last_price, side):
    """Gain relatif d'une position, positif quand elle est dans le vert."""
    if not entry:
        return 0.0
    return (last_price - entry) / entry if side == "buy" else (entry - last_price) / entry


def est_nouveau_sommet(side, last_price, best_price, entry):
    """Le prix vient-il d'aller plus loin que tout ce qu'on a vu ?

    `best_price` peut être None sur une position fraîche : on retombe alors sur
    le prix d'entrée.
    """
    best = best_price or entry
    return last_price > best if side == "buy" else last_price < best


def tp_ratchet(side, last_price, initial_tp_dist, tp_courant):
    """Nouveau take-profit à viser depuis un sommet, ou None s'il n'améliore rien.

    Le TP suit le prix à la moitié de sa distance initiale — d'où le facteur 0.5.

    Asymétrie conservée de v8 : côté achat la comparaison se fait contre
    `tp_courant` tel quel (0 sur une position neuve, donc tout TP l'améliore),
    côté vente contre l'infini si la valeur est absente. Une position vendeuse
    dont le TP vaut 0 ne verra donc jamais son TP bouger.
    """
    if not initial_tp_dist or initial_tp_dist <= 0:
        return None
    if side == "buy":
        nouveau = last_price * (1 + initial_tp_dist * 0.5)
        return nouveau if nouveau > (tp_courant or 0) else None
    nouveau = last_price * (1 - initial_tp_dist * 0.5)
    reference = float("inf") if tp_courant is None else tp_courant
    return nouveau if nouveau < reference else None


def niveau_breakeven(side, entry, offset_pct):
    """Stop ramené au prix d'entrée, décalé pour couvrir les frais."""
    facteur = (1 + offset_pct) if side == "buy" else (1 - offset_pct)
    return round_price_sig(entry * facteur)


def breakeven_ameliore(side, nouveau_sl, sl_actuel):
    """Le breakeven ne doit jamais reculer le stop."""
    return nouveau_sl > sl_actuel if side == "buy" else nouveau_sl < sl_actuel


def niveau_trailing(side, last_price, trail_distance):
    """Niveau de stop suiveur pour un prix donné."""
    facteur = (1 - trail_distance) if side == "buy" else (1 + trail_distance)
    return last_price * facteur


def trailing_doit_monter(side, nouveau, actuel, entry, trail_step):
    """Ne déplacer le stop que si le gain dépasse un pas minimal.

    Sans ce pas, le stop bougerait à chaque tick — bruit inutile et autant
    d'appels à l'exchange.
    """
    marge = entry * trail_step
    return nouveau > actuel + marge if side == "buy" else nouveau < actuel - marge


def trailing_touche(side, last_price, trailing):
    """Le prix a-t-il franchi le stop suiveur ?"""
    if trailing is None:
        return False
    return last_price <= trailing if side == "buy" else last_price >= trailing


def tp_sl_franchi(side, last_price, current_tp, sl_price):
    """(tp_atteint, sl_atteint) d'après le prix live.

    Un niveau à 0 signifie « non placé » et ne déclenche donc rien.
    """
    tp = current_tp or 0
    sl = sl_price or 0
    if side == "buy":
        return (tp > 0 and last_price >= tp), (sl > 0 and last_price <= sl)
    if side == "sell":
        return (tp > 0 and last_price <= tp), (sl > 0 and last_price >= sl)
    return False, False


def detention_expiree(open_time, maintenant, duree_max):
    """La position a-t-elle dépassé la durée de détention maximale ?

    `open_time` peut manquer — position reprise après un redémarrage, entrée
    mal enregistrée. On ne ferme alors rien : fermer sur une date inventée
    serait pire que laisser courir, et le cas se voit dans les logs.

    `duree_max` à 0 ou None désactive le plafond.
    """
    if not open_time or not duree_max:
        return False
    return (maintenant - open_time) >= duree_max


def pnl_realise(side, entry, exit_price, size):
    """PnL d'une position fermée, dans la devise de cotation."""
    return (exit_price - entry) * size if side == "buy" else (entry - exit_price) * size
