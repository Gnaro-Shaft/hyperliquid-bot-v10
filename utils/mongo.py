"""
Client MongoDB partagé pour le process du bot.

Pourquoi : `_send_daily_report()` (1×/jour) et `_auto_calibrate()` (1×/semaine)
créaient un `MongoClient` neuf à chaque appel sans jamais le fermer. Chacun
embarque son pool de connexions et ses threads de surveillance, que rien ne
libère tant que les sockets vivent. Sur les 43 jours d'uptime précédant l'OOM du
22/08/2026, une cinquantaine de clients s'étaient ainsi empilés.

Un `MongoClient` est thread-safe et gère lui-même son pool : le partager entre
les threads du bot est le mode d'emploi recommandé par pymongo, pas un
raccourci. Un client unique remplace donc la dizaine de pools distincts que le
process portait en permanence.

Les scripts de `scripts/` ne passent volontairement pas par ici : ce sont des
process courts, dont la sortie ferme tout.
"""

import threading

from pymongo import MongoClient

from config import MONGO_URL, MONGO_DB

# Aligné sur la valeur qui était déjà majoritaire dans le code. Les rares sites
# qui n'en fixaient aucune héritaient du défaut pymongo (30 s) : échouer vite et
# laisser le cycle suivant réessayer vaut mieux qu'un thread bloqué 30 s.
SERVER_SELECTION_TIMEOUT_MS = 5000

_client = None
_lock = threading.Lock()


def get_client():
    """Le client partagé, créé au premier appel (verrouillage à double contrôle).

    Si la création échoue, rien n'est mémorisé : l'appel suivant réessaiera,
    au lieu de figer un client inutilisable pour la durée du process.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = MongoClient(
                    MONGO_URL, serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS
                )
    return _client


def get_db():
    """La base de travail, via le client partagé."""
    return get_client()[MONGO_DB]


def ping():
    """Vérifie que le serveur répond. Lève si ce n'est pas le cas."""
    get_client().admin.command("ping")
    return True


def close():
    """Ferme le client partagé. À l'arrêt du process, ou entre deux tests."""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None
