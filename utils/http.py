"""
Appels HTTP avec réessai — pour les API externes du bot.

Pourquoi : le 05/09/2026 à 06:01, une bouffée d'échecs de résolution DNS a fait
perdre 14 relevés de flux baleines. Le collecteur attrapait l'exception, la
journalisait, et passait au suivant : le cycle était perdu en silence.

Le DNS n'était que la cause du jour. Une coupure réseau de trois secondes, un
502 passager ou un 429 de l'API produiraient le même trou. D'où un réessai qui
couvre l'ensemble, plutôt qu'un correctif ciblé sur la résolution de noms.

Ce qui est réessayé : les erreurs de connexion (dont l'échec de résolution DNS),
les dépassements de délai, et les codes 429/500/502/503/504. Une 404 ou une 400
ne le sont pas — insister ne les changera pas.
"""

import time

import requests

# Codes qui valent la peine d'être réessayés : surcharge ou panne passagère
# côté serveur. Tout autre 4xx traduit une requête fautive.
STATUTS_REESSAYABLES = {429, 500, 502, 503, 504}

TENTATIVES_DEFAUT = 3
BACKOFF_DEFAUT = 0.5


def creer_session():
    """Session requests réutilisable : garde les connexions ouvertes, ce qui
    évite une poignée de main TLS à chaque appel."""
    return requests.Session()


def _est_reessayable(exc):
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        reponse = getattr(exc, "response", None)
        return reponse is not None and reponse.status_code in STATUTS_REESSAYABLES
    return False


def demander(session, methode, url, *, tentatives=TENTATIVES_DEFAUT,
             backoff=BACKOFF_DEFAUT, dormir=time.sleep, **kwargs):
    """Exécute une requête, en réessayant les échecs passagers.

    Retourne la réponse (statut déjà vérifié). Relève la dernière exception si
    toutes les tentatives échouent — à l'appelant de décider quoi en faire.

    `dormir` est injectable pour que les tests n'attendent pas réellement.
    """
    derniere = None
    for tentative in range(tentatives):
        try:
            reponse = session.request(methode, url, **kwargs)
            reponse.raise_for_status()
            return reponse
        except Exception as exc:
            if not _est_reessayable(exc):
                raise
            derniere = exc
            if tentative == tentatives - 1:
                break
            dormir(backoff * (2 ** tentative))
    raise derniere
