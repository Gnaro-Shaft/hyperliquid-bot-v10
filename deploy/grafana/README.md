# Grafana V10 — déploiement

Hébergé sur **`docker-stack`** (tailnet `100.126.56.110`) depuis le 05/09/2026.
L'hôte d'origine, homeserv01, a disparu lors de la migration Proxmox du 22/08 —
Grafana est parti avec, comme le watchdog et les crons.

Les autres machines ont été écartées : les deux hyperviseurs Proxmox n'ont pas
Docker et il n'y reste que ~550 Mio de RAM libre, et le VPS OVH héberge le bot
(on évite d'y ajouter une surface).

## Accès

`http://100.126.56.110:3002` — **sur le tailnet uniquement**. Le port est publié
sur l'IP tailnet, jamais sur `0.0.0.0` (convention docker-stack). Le défaut de
`GRAFANA_BIND_IP` est `127.0.0.1` : sans variable, rien n'est exposé.

## Installation

```bash
ssh root@100.126.56.110
mkdir -p /opt/v10-grafana && cd /opt/v10-grafana
# copier docker-compose.yml et dashboard-v10.json depuis deploy/grafana/
cat > .env <<'EOF'
MONGO_URL=...
MONGO_DB=bot_hyperliquid_v10
GRAFANA_ADMIN_PASSWORD=...
GRAFANA_BIND_IP=100.126.56.110
EOF
chmod 600 .env
docker compose --env-file .env up -d
```

Puis créer le datasource (UID **`mongo-v10`**, attendu par les 8 panneaux) et
importer le dashboard via l'API — voir la section suivante.

## Datasource MongoDB

Plugin communautaire `haohanyang-mongodb-datasource` (le plugin MongoDB du
catalogue Grafana est Enterprise/payant). Installé par `GF_INSTALL_PLUGINS`,
non signé, donc explicitement autorisé dans le compose.

Champs attendus par le plugin, à renseigner depuis `MONGO_URL` :

| champ | valeur |
|---|---|
| `connectionStringScheme` | `mongodb+srv` |
| `host` | hôte du cluster Atlas |
| `database` | `MONGO_DB` |
| `authType` | `username-password` |
| `username` / `password` | extraits de l'URL |
| `authDb` | `admin` |

L'UID **doit** être `mongo-v10` : les 8 panneaux le référencent en dur.

## Ce que montre le dashboard

Santé des collecteurs (heartbeats), évaluations de la dernière heure, régimes,
derniers signaux non neutres, raw_score, funding, spread et clusters de
liquidation par coin.

Source : le **buffer chaud** de Mongo, pas l'archive. L'historique long vit en
Parquet ; un dashboard historique demanderait DuckDB.

## Vérification

Un conteneur qui tourne ne prouve rien. Interroger les 8 panneaux par
`/api/ds/query`, en **substituant `${__from}` et `${__to}`** par des epoch en
millisecondes — sans cette substitution, l'agrégation Mongo est invalide et
cinq panneaux échouent sur `Failed to unmarshal JsonExt`. C'est le harnais qui
est en cause, pas le dashboard.

Au 05/09/2026 : 8/8 panneaux renvoient des données.

## Point de durcissement en attente

`MONGO_URL` donne un accès **complet** à la base ; Grafana n'a besoin que de
lire. Créer un utilisateur Atlas en lecture seule dédié limiterait ce qu'un
accès à docker-stack permettrait de faire.
