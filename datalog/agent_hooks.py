"""
Hooks agents LLM (V10) — sorties d'agents horodatées, prêtes pour un backtest
SANS fuite temporelle.

L'orchestrateur multi-agents n'existe pas encore (non-objectif V10) ; ce module
fournit le point d'entrée de logging pour le jour où une couche agents sera
ajoutée. Toute sortie d'agent est stockée avec :
  - produced_at : quand l'agent a produit sa sortie (UTC ms) — fourni par l'appelant
  - logged_at   : quand la ligne a été écrite (UTC ms)
  - valid_from  : à partir de quand la sortie est utilisable par une stratégie
                  (défaut = logged_at). Un backtest ne doit JAMAIS joindre une
                  sortie d'agent à un timestamp antérieur à valid_from.

Clé de jointure : (coin, valid_from) ou agent_output_id côté trades/signaux.
"""

import uuid
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING

from config import MONGO_URL, MONGO_DB, MONGO_COLLECTION_AGENT_OUTPUTS


def _utc_now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def build_agent_output_doc(agent_id, payload, *, coin=None, produced_at_ms=None,
                           valid_from_ms=None, model=None, prompt_version=None,
                           now_ms=None):
    """Construit la ligne de sortie d'agent (pure, testable)."""
    logged_at = now_ms or _utc_now_ms()
    return {
        "agent_output_id": uuid.uuid4().hex,
        "agent_id": agent_id,
        "coin": coin,                              # None = sortie globale marché
        "produced_at": int(produced_at_ms or logged_at),
        "logged_at": logged_at,
        "valid_from": int(valid_from_ms or logged_at),
        "timestamp": int(valid_from_ms or logged_at),   # clé standard (coin, timestamp)
        "model": model,
        "prompt_version": prompt_version,
        "payload": payload,                        # brut (verdict, score, texte...)
    }


class AgentOutputLogger:
    def __init__(self, mongo_db=None):
        self.db = mongo_db
        self.ready = False
        if self.db is None and MONGO_URL:
            try:
                client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")
                self.db = client[MONGO_DB]
            except Exception as e:
                print(f"[AGENT_HOOKS][ERREUR] MongoDB: {e}")
        if self.db is not None:
            try:
                self.db[MONGO_COLLECTION_AGENT_OUTPUTS].create_index(
                    [("coin", ASCENDING), ("timestamp", ASCENDING)])
                self.db[MONGO_COLLECTION_AGENT_OUTPUTS].create_index("agent_output_id",
                                                                     unique=True)
            except Exception as e:
                print(f"[AGENT_HOOKS] index: {e}")
            self.ready = True

    def log(self, agent_id, payload, **kwargs):
        """Écrit une sortie d'agent horodatée. Retourne le doc écrit."""
        doc = build_agent_output_doc(agent_id, payload, **kwargs)
        if self.ready:
            try:
                self.db[MONGO_COLLECTION_AGENT_OUTPUTS].insert_one(dict(doc))
            except Exception as e:
                print(f"[AGENT_HOOKS][ERREUR] insert: {e}")
        return doc
