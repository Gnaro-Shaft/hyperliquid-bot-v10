"""Réessai des appels HTTP — non-régression de la perte de flux du 05/09/2026.

Ce jour-là, une bouffée d'échecs DNS a fait perdre 14 relevés de flux baleines :
l'exception était journalisée, le cycle abandonné, et rien ne le signalait
ailleurs que dans les logs.
"""

import pytest
import requests

from utils import http


class FausseReponse:
    def __init__(self, statut=200):
        self.status_code = statut

    def raise_for_status(self):
        if self.status_code >= 400:
            erreur = requests.exceptions.HTTPError(f"statut {self.status_code}")
            erreur.response = self
            raise erreur


class FausseSession:
    """Rejoue une séquence d'issues : exceptions ou réponses."""

    def __init__(self, issues):
        self.issues = list(issues)
        self.appels = 0

    def request(self, methode, url, **kwargs):
        self.appels += 1
        issue = self.issues.pop(0)
        if isinstance(issue, Exception):
            raise issue
        return issue


@pytest.fixture
def sommeils():
    """Capture les durées d'attente au lieu de les subir."""
    durees = []
    return durees, durees.append


def test_reessaie_apres_un_echec_dns(sommeils):
    """Le cas exact du 05/09 : NameResolutionError remontée en ConnectionError."""
    durees, dormir = sommeils
    session = FausseSession([
        requests.exceptions.ConnectionError("Failed to resolve 'api.hyperliquid.xyz'"),
        FausseReponse(200),
    ])
    reponse = http.demander(session, "POST", "https://exemple.invalide",
                            dormir=dormir)
    assert reponse.status_code == 200
    assert session.appels == 2


def test_abandonne_apres_le_nombre_de_tentatives(sommeils):
    durees, dormir = sommeils
    session = FausseSession([requests.exceptions.ConnectionError("dns")] * 3)
    with pytest.raises(requests.exceptions.ConnectionError):
        http.demander(session, "GET", "https://exemple.invalide",
                      tentatives=3, dormir=dormir)
    assert session.appels == 3
    assert len(durees) == 2          # on ne dort pas après la dernière tentative


def test_backoff_exponentiel(sommeils):
    durees, dormir = sommeils
    session = FausseSession([requests.exceptions.Timeout("lent")] * 4)
    with pytest.raises(requests.exceptions.Timeout):
        http.demander(session, "GET", "u", tentatives=4, backoff=0.5, dormir=dormir)
    assert durees == [0.5, 1.0, 2.0]


def test_ne_reessaie_pas_une_404(sommeils):
    """Insister sur une ressource absente ne fait que retarder l'échec."""
    durees, dormir = sommeils
    session = FausseSession([FausseReponse(404)])
    with pytest.raises(requests.exceptions.HTTPError):
        http.demander(session, "GET", "u", dormir=dormir)
    assert session.appels == 1
    assert durees == []


def test_reessaie_un_503(sommeils):
    durees, dormir = sommeils
    session = FausseSession([FausseReponse(503), FausseReponse(200)])
    assert http.demander(session, "GET", "u", dormir=dormir).status_code == 200
    assert session.appels == 2


def test_reessaie_un_429(sommeils):
    """L'API Hyperliquid limite le débit : un 429 mérite d'attendre, pas d'abandonner."""
    durees, dormir = sommeils
    session = FausseSession([FausseReponse(429), FausseReponse(200)])
    assert http.demander(session, "GET", "u", dormir=dormir).status_code == 200


def test_une_seule_tentative_si_demande(sommeils):
    durees, dormir = sommeils
    session = FausseSession([requests.exceptions.ConnectionError("dns")])
    with pytest.raises(requests.exceptions.ConnectionError):
        http.demander(session, "GET", "u", tentatives=1, dormir=dormir)
    assert session.appels == 1
    assert durees == []


def test_succes_immediat_ne_dort_pas(sommeils):
    durees, dormir = sommeils
    session = FausseSession([FausseReponse(200)])
    http.demander(session, "GET", "u", dormir=dormir)
    assert session.appels == 1 and durees == []


def test_les_collectors_passent_tous_par_le_helper():
    """Garde anti-régression : aucun collector ne doit appeler requests en direct.

    Un appel direct n'a pas de réessai — et pour rest_collector, un échec coûte
    le cycle entier, soit 300 s de funding et d'open interest manquants sur les
    dix coins, alors que ces données alimentent le moteur de décision.
    """
    import pathlib
    racine = pathlib.Path(__file__).resolve().parent.parent / "collector"
    fautifs = [
        f.name for f in racine.glob("*.py")
        if "requests." in f.read_text(encoding="utf-8")
    ]
    assert not fautifs, f"appel direct à requests dans : {fautifs}"
