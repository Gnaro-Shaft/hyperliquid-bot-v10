#!/usr/bin/env bash
# Installation du bot V10 sur un VPS Ubuntu 24.04 LTS (Python 3.12).
#
# Le code doit déjà être présent dans /opt/v10 (rsync depuis le poste de dev)
# ainsi que le fichier .env. Ce script est idempotent : le relancer après une
# mise à jour du code reconstruit l'environnement sans rien casser.
#
# Usage :  sudo bash /opt/v10/deploy/install-vps.sh

set -euo pipefail

APP_DIR=/opt/v10
APP_USER=botv10
UNITS_DIR="$APP_DIR/deploy/systemd"

[[ $EUID -eq 0 ]] || { echo "À lancer en root (sudo)."; exit 1; }

# Exécution en tant qu'utilisateur applicatif. `runuser` (util-linux) est présent
# partout et n'exige pas sudo — absent des images minimales type Proxmox, où tout
# se fait en root de toute façon. Repli sur sudo si runuser manquait.
as_app() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$APP_USER" -- "$@"
    else
        sudo -u "$APP_USER" "$@"
    fi
}


echo "── Paquets système ──"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-venv python3-pip rsync ufw >/dev/null

PY_MAJ=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MIN=$(python3 -c 'import sys; print(sys.version_info[1])')
echo "Python système : $PY_MAJ.$PY_MIN"
# Comparaison numérique : un test de chaînes classerait "3.9" au-dessus de "3.12".
if (( PY_MAJ < 3 || (PY_MAJ == 3 && PY_MIN < 12) )); then
    echo "ERREUR : Python >= 3.12 requis (numpy 2.5.x l'exige). Réinstaller l'OS en Ubuntu 24.04." >&2
    exit 1
fi

echo "── Utilisateur dédié ──"
id "$APP_USER" &>/dev/null || \
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "── Pare-feu ──"
# L'ordre importe : autoriser SSH AVANT d'activer, sous peine de se verrouiller.
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ufw status | head -5

echo "── Secrets ──"
if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "ERREUR : $APP_DIR/.env absent. Le copier avant de relancer." >&2
    exit 1
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

mkdir -p "$APP_DIR/data/parquet"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "── Environnement Python ──"
as_app python3 -m venv "$APP_DIR/venv"
as_app "$APP_DIR/venv/bin/pip" install -q --upgrade pip
as_app "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
as_app "$APP_DIR/venv/bin/pip" check

echo "── Vérification (suite de tests sur la machine cible) ──"
# MONGO_URL neutralisé : la suite ouvre sinon un vrai client vers Atlas et y
# insère de faux trades (TradeLogger crée son propre client quand db is None).
# Les tests passent sans Mongo — c'est aussi ce qui les rend hermétiques.
cd "$APP_DIR"
as_app env MONGO_URL= "$APP_DIR/venv/bin/python" -m pytest tests/ -q

echo "── Unités systemd ──"
install -m 644 "$UNITS_DIR"/v10-bot.service     /etc/systemd/system/
install -m 644 "$UNITS_DIR"/v10-alert@.service  /etc/systemd/system/
install -m 644 "$UNITS_DIR"/v10-export.service  /etc/systemd/system/
install -m 644 "$UNITS_DIR"/v10-export.timer    /etc/systemd/system/
systemctl daemon-reload
# --now sur le timer : `enable` seul ne l'arme qu'au prochain démarrage.
# Le bot, lui, reste volontairement à l'arrêt (démarrage explicite).
systemctl enable v10-bot.service >/dev/null
systemctl enable --now v10-export.timer >/dev/null

echo
echo "Installation terminée. Le bot n'est PAS démarré."
echo "  démarrer : systemctl start v10-bot"
echo "  journal  : journalctl -u v10-bot -f"
echo "  état     : systemctl status v10-bot"
