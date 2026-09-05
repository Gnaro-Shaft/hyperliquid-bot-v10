"""
Journal des décisions d'entrée (acceptées comme refusées).

Extrait de `main.py`, où ces deux méthodes ne touchaient aucun attribut de
`TradingBot` : elles n'avaient rien à y faire. Le document lui-même est
construit par `utils.observability.build_decision_doc`.

Une décision refusée est aussi intéressante qu'une décision acceptée : c'est
elle qui dit pourquoi le bot n'est pas entré, information absente des trades.
"""

import time
from datetime import datetime, timezone

from config import MONGO_COLLECTION_DECISIONS
from utils.mongo import get_db
from utils.observability import build_decision_doc


def log_decision(coin, sig, side, action, reason, price, size_factor=None):
    """Journalise une décision d'entrée en Mongo. N'échoue jamais bruyamment :
    perdre une ligne de journal ne doit pas interrompre un cycle de trading."""
    try:
        doc = build_decision_doc(
            coin, sig, side, action, reason, price, size_factor,
            int(time.time() * 1000),
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        get_db()[MONGO_COLLECTION_DECISIONS].insert_one(doc)
    except Exception as e:
        print(f"[BOT][{coin}] Erreur log decision: {e}")
