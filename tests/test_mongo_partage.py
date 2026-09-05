"""Non-régression de la fuite de clients MongoDB.

Incident du 22/08/2026 : `_send_daily_report()` (1×/jour) et `_auto_calibrate()`
(1×/semaine) créaient un MongoClient neuf sans jamais le fermer. Sur 43 jours
d'uptime, une cinquantaine de clients — avec leurs pools et leurs threads —
s'étaient empilés jusqu'à l'OOM sur les 512 Mo de la machine.
"""

import pytest

from utils import mongo


class ClientFactice:
    """Compte ses instanciations, comme le ferait un pool réel."""

    instances = 0

    def __init__(self, *args, **kwargs):
        ClientFactice.instances += 1
        self.closed = False

    def __getitem__(self, nom):
        return {"_base": nom}

    def close(self):
        self.closed = True


@pytest.fixture
def client_compte(monkeypatch):
    ClientFactice.instances = 0
    monkeypatch.setattr(mongo, "MongoClient", ClientFactice)
    mongo.close()
    yield ClientFactice
    mongo.close()


def test_un_seul_client_pour_de_multiples_appels(client_compte):
    """Le cœur de la fuite : N appels ne doivent produire qu'UN client."""
    for _ in range(50):          # ~50 jours de rapports quotidiens
        mongo.get_db()
    assert client_compte.instances == 1


def test_get_client_renvoie_toujours_le_meme_objet(client_compte):
    assert mongo.get_client() is mongo.get_client()


def test_close_ferme_reellement_et_permet_un_nouveau_client(client_compte):
    premier = mongo.get_client()
    mongo.close()
    assert premier.closed is True
    second = mongo.get_client()
    assert second is not premier
    assert client_compte.instances == 2


def test_un_echec_de_creation_n_est_pas_memorise(monkeypatch):
    """Un client inutilisable ne doit pas être figé pour la vie du process."""
    def refuse(*a, **k):
        raise RuntimeError("Mongo injoignable")

    monkeypatch.setattr(mongo, "MongoClient", refuse)
    mongo.close()
    with pytest.raises(RuntimeError):
        mongo.get_client()
    # Le suivant doit réessayer, pas renvoyer un état corrompu
    monkeypatch.setattr(mongo, "MongoClient", ClientFactice)
    ClientFactice.instances = 0
    assert mongo.get_client() is not None
    assert ClientFactice.instances == 1
    mongo.close()


def test_main_ne_peut_plus_creer_son_propre_client():
    """Garde anti-réintroduction : main.py ne doit plus importer MongoClient.

    Les cinq sites de création qu'il portait sont passés par utils.mongo ;
    réintroduire un import direct rouvrirait la porte à la fuite.
    """
    import main
    assert not hasattr(main, "MongoClient"), (
        "main.py réimporte MongoClient — utiliser utils.mongo.get_db()"
    )
