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

## Purge du buffer Mongo — chaînée à l'export

`v10-purge.service` n'a **pas de timer propre** : elle est déclenchée par
`OnSuccess=` de `v10-export.service`. La purge ne doit tourner qu'après un
archivage réussi — le script refuse déjà de supprimer un jour dont la partition
Parquet est absente ou incomplète, le chaînage systemd exprime la même exigence
un cran plus haut.

**`--keep-days 2`, et non le défaut de 4.** Mesuré le 05/09/2026 : la collecte
produit **109 Mo/jour** (4,5 Mo/h sur 11 h d'observation). Conserver 4 jours
réclamerait ~437 Mo, soit 85 % du tier Atlas M0 (512 Mo). Le moteur n'a besoin
que de ~37 h (150 bougies de 15 min) ; deux jours laissent ~218 Mo de marge.

Sans purge, le plafond M0 est atteint en **4,2 jours**.

Le code de sortie 1 signifie « des jours n'ont pas été purgés faute d'archive
complète ». C'est volontairement traité comme un échec : l'alerte Telegram part.
Un avertissement récurrent signale que l'export ne suit pas.

### Archive consolidée sur le VPS (05/09/2026)

`/opt/v10/data/parquet` porte désormais **l'archive complète** : 4 657 fichiers,
547 Mo, du 09/07 au 05/09, **5 415 300 lignes**. Transfert vérifié par
`rsync --checksum` — aucune divergence.

Elle était auparavant scindée : historique sur le Mac, courant sur le VPS. Le
VPS ne pouvait donc purger que ce qu'il avait lui-même archivé, ce qui a obligé
à copier à la main les partitions du 21-22/08 pour débloquer 681 documents.

La copie du Mac (`~/V10-archive/parquet`) est conservée telle quelle.

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

## Résolveurs DNS — à refaire après toute réinstallation

Cloud-init ne configure **qu'un seul** résolveur sur `ens3` (`213.186.33.99`,
OVH), sans repli : s'il ne répond pas, plus rien ne résout. Le 05/09/2026, une
bouffée d'échecs a fait perdre 14 relevés de flux baleines.

Ajouter un drop-in netplan, qui prend le pas sur celui de cloud-init et survit à
ses régénérations :

```yaml
# /etc/netplan/99-dns.yaml  (chmod 600)
network:
  version: 2
  ethernets:
    ens3:
      nameservers:
        addresses: [213.186.33.99, 9.9.9.9, 149.112.112.112]
```

```bash
sudo chmod 600 /etc/netplan/99-dns.yaml
sudo netplan generate          # valide la syntaxe SANS appliquer
sudo netplan apply             # reconfigure l'interface
resolvectl status | grep "DNS Servers"
```

**`netplan apply` peut couper SSH.** Sur une machine distante, armer un
coupe-circuit avant d'appliquer :

```bash
sudo nohup sh -c 'sleep 90; [ -f /tmp/netplan-confirme ] || { rm -f /etc/netplan/99-dns.yaml; netplan apply; }' &
# puis, une fois la connexion vérifiée :
sudo touch /tmp/netplan-confirme
```

Vérifier que les serveurs de secours sont réellement joignables — les déclarer
ne suffit pas si le filtrage sortant les bloque :

```bash
python3 -c "import socket; [socket.create_connection((ip,53),4).close() for ip in ('9.9.9.9','149.112.112.112')]"
```

Quad9 (fondation suisse, à but non lucratif) plutôt que Cloudflare ou Google :
les requêtes DNS révèlent les hôtes que la machine contacte — Hyperliquid, le
cluster Atlas, l'API Telegram.

Ceci ne remplace pas le réessai applicatif (`utils/http.py`) : la redondance DNS
ne couvre qu'une cause d'échec parmi d'autres.
