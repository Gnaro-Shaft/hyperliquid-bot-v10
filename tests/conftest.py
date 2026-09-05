"""
Isolation de la suite de tests vis-à-vis de MongoDB.

Pourquoi : le 05/09/2026, `pytest` a inséré 30 faux trades dans la base de
PRODUCTION. `tests/test_paper_trader.py` neutralise bien `PaperTrader._connect`,
mais `self.db` reste alors à None et `TradeLogger.__init__` ouvre son *propre*
client vers MONGO_URL, puis écrit. Le nettoyage a ensuite emporté un trade
légitime. Une suite de tests ne doit jamais pouvoir atteindre une base réelle.

Comment : `MongoClient` est remplacé par une classe qui lève. Le code applicatif
attrape déjà ces exceptions et retombe sur `db = None` — soit exactement le
comportement « Mongo injoignable », qui est le cas nominal en test.

Le remplacement vise `pymongo` (pour les imports tardifs) ET chaque module déjà
chargé qui expose l'attribut. Le balayage de sys.modules évite une liste à tenir
à jour : un module ajouté demain sera couvert sans qu'on y pense.
"""

import sys

import pytest

# Préfixes des modules du projet. Les dépendances tierces ne sont pas touchées :
# seul le code maison doit être empêché d'atteindre une base.
PREFIXES_PROJET = (
    "main", "config", "collector.", "datalog.", "monitor.", "risk.",
    "strategy.", "trader.", "utils.", "scripts.",
)

MESSAGE = (
    "Connexion MongoDB interdite pendant les tests. "
    "La suite doit rester hermétique : si ce code a besoin d'une base, "
    "injectez un double (voir tests/test_purge.py) au lieu d'un vrai client."
)


class MongoClientInterdit:
    """Substitut de pymongo.MongoClient qui refuse toute connexion."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(MESSAGE)


def _modules_a_proteger():
    """Modules du projet, déjà chargés, exposant un attribut MongoClient."""
    for nom, module in list(sys.modules.items()):
        if module is None or not nom.startswith(PREFIXES_PROJET):
            continue
        if hasattr(module, "MongoClient"):
            yield nom, module


@pytest.fixture(autouse=True, scope="session")
def _interdire_mongo():
    """Coupe l'accès à MongoDB pour toute la session de tests."""
    import pymongo

    originaux = [(pymongo, pymongo.MongoClient)]
    for _, module in _modules_a_proteger():
        originaux.append((module, module.MongoClient))

    for cible, _ in originaux:
        cible.MongoClient = MongoClientInterdit

    yield

    for cible, original in originaux:
        cible.MongoClient = original
