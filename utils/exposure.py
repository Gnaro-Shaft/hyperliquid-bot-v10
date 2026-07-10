"""
Garde-fou d'exposition globale (v8.9) — fonction pure et testable.

Limite, au-delà de la gestion de corrélation BTC/SOL existante :
  - le nombre total de positions simultanées,
  - le nombre de positions dans une même direction (long/short),
  - l'exposition notionnelle totale en % du solde.
"""


def exposure_check(open_positions, candidate_side, candidate_notional, balance,
                   max_positions, max_per_direction, max_total_exposure_pct):
    """Retourne (autorisé: bool, raison: str) pour l'ouverture d'une position.

    Args:
        open_positions : liste de dicts {"side": "buy"|"sell", "notional": float}
                         (positions actives + entrées en attente)
        candidate_side : "buy" ou "sell" de la position candidate
        candidate_notional : notionnel estimé de la candidate (taille × prix)
        balance        : solde total courant (USDC)
        max_positions  : nb max de positions simultanées
        max_per_direction : nb max de positions dans la même direction
        max_total_exposure_pct : exposition notionnelle max (fraction du solde)
    """
    n_open = len(open_positions)
    if n_open + 1 > max_positions:
        return False, f"max positions simultanées atteint ({n_open}/{max_positions})"

    same_dir = sum(1 for p in open_positions if p.get("side") == candidate_side)
    if same_dir + 1 > max_per_direction:
        return False, (f"max positions {candidate_side} atteint "
                       f"({same_dir}/{max_per_direction})")

    total_notional = sum(p.get("notional", 0.0) for p in open_positions) + candidate_notional
    if balance > 0 and total_notional > balance * max_total_exposure_pct:
        return False, (f"exposition totale {total_notional:.0f} dépasserait "
                       f"{max_total_exposure_pct*100:.0f}% du solde ({balance:.0f})")

    return True, "OK"
