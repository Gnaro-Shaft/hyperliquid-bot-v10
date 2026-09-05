"""
Décisions de la logique d'entrée : confirmation, cooldown, pullback, inversion.

Extraites de _try_open_position (97 lignes), _should_reverse et
_check_pending_entry, où elles étaient enchevêtrées avec les appels au risk
manager, au notifier et à l'exchange — donc invérifiables autrement qu'en
laissant tourner le bot.

Aucune de ces fonctions ne modifie quoi que ce soit : elles répondent à
« combien de signaux consécutifs ? », « le cooldown est-il écoulé ? »,
« le pullback est-il atteint ? ». L'orchestration reste ailleurs.
"""

from utils.prices import round_price_sig


def signal_est_fort(score):
    """Seuls les signaux ±2 (confirmés par le moteur) ouvrent une position."""
    return score in (2, -2)


def side_depuis_score(score):
    """+2 → achat, -2 → vente."""
    return "buy" if score == 2 else "sell"


def maj_streak(score, direction_courante, streak_courant):
    """Compteur de signaux forts consécutifs de même sens.

    Retourne (streak, direction). Un changement de sens repart à 1, pas à 0 :
    le signal qui change de camp compte déjà pour lui-même.
    """
    if score == direction_courante:
        return streak_courant + 1, direction_courante
    return 1, score


def est_confirme(streak, seuil):
    return streak >= seuil


def cooldown_restant(maintenant, dernier_trade, cooldown):
    """Secondes restantes avant de pouvoir réentrer. 0 si le délai est écoulé."""
    ecoule = maintenant - dernier_trade
    return max(0.0, cooldown - ecoule)


def taille_avec_boost(facteur_base, boost):
    """Le boost de corrélation ne peut pas faire dépasser la taille pleine."""
    return min(1.0, facteur_base + boost)


def cible_pullback(side, prix, pullback_pct):
    """Prix visé pour l'entrée en repli, arrondi en chiffres significatifs.

    L'arrondi coin-agnostique (V10) évite les prix aberrants sur PEPE ou WIF,
    dont les cotations ont bien plus de décimales que BTC.
    """
    facteur = (1 - pullback_pct) if side == "buy" else (1 + pullback_pct)
    return round_price_sig(prix * facteur)


def pullback_atteint(direction, live_price, cible):
    """Le prix est-il revenu jusqu'au niveau visé ?"""
    if direction == "buy":
        return live_price <= cible
    return live_price >= cible


def maj_reverse_streak(position_active, side, score, streak_courant):
    """Nouveau compteur de signaux OPPOSÉS consécutifs.

    Sans position ouverte, ou dès qu'un signal cesse d'être opposé, le compteur
    retombe à zéro : seule une série ininterrompue déclenche la sortie. La
    décision d'inverser revient à inversion_confirmee().
    """
    if not position_active:
        return 0
    oppose = (side == "buy" and score == -2) or (side == "sell" and score == 2)
    return streak_courant + 1 if oppose else 0


def inversion_confirmee(streak, seuil):
    return streak >= seuil


def parametres_trailing(atr_pct, plancher_distance, plancher_trigger, plancher_step):
    """Paramètres du stop suiveur, proportionnels à la volatilité mesurée.

    Chaque valeur a un plancher venu de la configuration : sur un marché très
    calme, l'ATR seul donnerait des seuils si serrés que le stop se
    déclencherait sur du bruit.
    """
    return {
        "trail_distance": max(atr_pct * 1.5, plancher_distance),
        "trail_trigger":  max(atr_pct * 2.0, plancher_trigger),
        "trail_step":     max(atr_pct * 0.5, plancher_step),
    }
