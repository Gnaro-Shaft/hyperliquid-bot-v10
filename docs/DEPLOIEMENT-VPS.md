# Déploiement V10 — VPS OVH (Ubuntu 24.04 LTS)

Remplace le déploiement Fly.io, arrêté le 22/08/2026 (OOM sur 512 Mo, voir
« Historique » en fin de document).

## Architecture cible

| Machine | Rôle | Pourquoi là |
|---|---|---|
| VPS OVH (Ubuntu 24.04) | bot + collectors, export Parquet | uptime, IP fixe, 4 Go |
| homeserv03 | **watchdog externe** | doit survivre à la mort du VPS |
| homeserv02 | copie de l'archive Parquet | `/data/backups/V10-archive` |
| MongoDB Atlas M0 | buffer roulant (pas l'archive) | inchangé |

L'archive de référence est le **Parquet**, pas Mongo : la purge vide les six
collections volumineuses dès qu'un jour est archivé. Perdre le Parquet, c'est
perdre le dataset — d'où la copie sur homeserv02.

## Prérequis

- Ubuntu 24.04 LTS. **Python ≥ 3.12 obligatoire** : les versions épinglées de
  `numpy` (2.5.x) exigent 3.12. Debian 12 (Python 3.11) ne convient pas.
- Accès SSH par clé, `sudo` disponible.

## Installation

Depuis le poste de développement, code d'abord (jamais le `.env` ni le venv) :

```bash
rsync -az --delete \
  --exclude 'venv/' --exclude '.git/' --exclude 'data/' --exclude '.env' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.DS_Store' \
  --rsync-path="sudo rsync" \
  ~/projects/20-prod/V10/ ubuntu@<IP>:/opt/v10/
```

Puis le `.env`, séparément — il contient les clés Hyperliquid et Atlas :

```bash
scp ~/projects/20-prod/V10/.env ubuntu@<IP>:/tmp/.env
ssh ubuntu@<IP> 'sudo mv /tmp/.env /opt/v10/.env'
```

Enfin, sur le VPS :

```bash
sudo bash /opt/v10/deploy/install-vps.sh
```

Le script installe les paquets, crée l'utilisateur système `botv10`, active `ufw`
(SSH autorisé **avant** activation), construit le venv depuis
`requirements.txt` épinglé, **lance la suite de tests sur la machine cible**,
puis installe les unités systemd. Il ne démarre pas le bot.

## Démarrage et exploitation

```bash
sudo systemctl start v10-bot        # démarrer
journalctl -u v10-bot -f            # suivre le journal
systemctl status v10-bot            # état, mémoire, redémarrages
systemctl list-timers v10-export.timer
```

Mise à jour du code : refaire le `rsync`, puis
`sudo bash /opt/v10/deploy/install-vps.sh && sudo systemctl restart v10-bot`.

## Unités

| Unité | Rôle |
|---|---|
| `v10-bot.service` | le bot. `Restart=always`, `RestartSec=30`, `MemoryHigh=600M`, `MemoryMax=1G` |
| `v10-export.service` + `.timer` | export Mongo → Parquet, quotidien à 02:17 UTC |
| `v10-alert@.service` | alerte Telegram déclenchée par `OnFailure=` des deux précédentes |

`MemoryMax=1G` pour une empreinte mesurée à ~121 Mo chargé et ~350 Mo en régime :
la marge est large, et une fuite provoque un redémarrage tracé et notifié plutôt
qu'un OOM système silencieux.

## Ce qui ne doit PAS tourner sur le VPS

Le **watchdog externe** (`scripts/external_watchdog.py`) va sur homeserv03. Sur
le VPS il mourrait avec le bot — c'est exactement ce qui s'est produit le
22/08/2026, où le watchdog vivait sur homeserv01, machine retirée du tailnet le
jour même.

## Sauvegarde de l'archive

```bash
rsync -az /opt/v10/data/parquet/ root@homeserv02:/data/backups/V10-archive/parquet/
rsync -an --checksum ...   # vérification : aucune ligne = copies identiques
```

## Historique — pourquoi ces choix

- **22/08/2026 08:10 UTC** : la machine Fly est tuée par l'OOM killer
  (`exit_code=137, oom_killed=true`), deux fois en seize minutes. `max_retries=0`
  fait abandonner flyd : la collecte s'arrête pour de bon, sans alerte.
  → d'où `Restart=always` et l'alerte `OnFailure=`.
- **Fuite mémoire** : `_send_daily_report()` (1×/jour) et `_auto_calibrate()`
  (1×/semaine) créent un `MongoClient` jamais fermé. ~50 clients accumulés en
  six semaines. → correction prévue, `MemoryMax` en filet en attendant.
- **Versions non épinglées** : l'image Fly figeait une résolution non
  documentée. `requirements.txt` est désormais épinglé au transitif près, sur la
  résolution de juillet — sauf `pandas`, en 3.0.5 et non 3.0.4, cette dernière
  ayant été retirée de PyPI pour des segfaults datetime.

## Watchdog externe — homeserv03

Installé le 05/09/2026 sur **homeserv03** (adresse tailnet, hors dépôt), délibérément pas sur le
VPS : il doit survivre à la mort du bot **et à celle de sa machine**. Le 22/08/2026
il vivait sur homeserv01, retirée du tailnet le jour même — la collecte est restée
morte 14 jours sans que personne ne soit prévenu.

```bash
rsync -az --exclude 'venv/' --exclude '.git/' --exclude 'data/' --exclude '.env' \
  ~/projects/20-prod/V10/ root@homeserv03:/opt/v10-watchdog/
# .env RÉDUIT : MONGO_URL, MONGO_DB, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID — jamais les clés HL
bash /opt/v10-watchdog/deploy/install-watchdog.sh
```

| Unité | Rôle |
|---|---|
| `v10-watchdog.timer` | toutes les 5 min, `Persistent=true`, `OnBootSec=2min` |
| `v10-watchdog.service` | lit `collector_health` + `bot_status`, alerte sur transition |
| `v10-alert-watchdog@.service` | alerte si le watchdog **lui-même** échoue |

Surface minimale : `deploy/requirements-watchdog.txt` n'installe que pymongo,
dnspython, requests et python-dotenv — ni numpy ni pandas, donc aucune contrainte
Python 3.12 sur la machine de surveillance (homeserv03 est en 3.13).

`SuccessExitStatus=0 1` : le code 1 signifie « collecte en panne », pas « watchdog
en échec ». Sans lui, systemd déclencherait `OnFailure=` et tu recevrais deux
Telegram pour un seul incident.

Test de la chaîne, sans attendre une vraie panne :

```bash
# branche panne (envoie une vraie alerte, puis rejouer sans l'option pour le rétabli)
runuser -u v10watch -- /opt/v10-watchdog/venv/bin/python -m scripts.external_watchdog --max-age 0
runuser -u v10watch -- /opt/v10-watchdog/venv/bin/python -m scripts.external_watchdog
```
