#!/usr/bin/env bash
# Installation du watchdog externe V10 sur une machine de SURVEILLANCE.
#
# Cette machine doit être distincte de celle qui héberge le bot : le watchdog
# couvre le cas où le process — ou la machine entière — disparaît. Le 22/08/2026
# il vivait sur homeserv01, retirée du tailnet le jour même : la collecte est
# restée morte 14 jours sans que personne ne soit prévenu.
#
# Le code doit déjà être dans /opt/v10-watchdog, ainsi qu'un .env RÉDUIT
# (MONGO_URL, MONGO_DB, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID). Surtout pas les clés
# Hyperliquid : le watchdog n'en a aucun usage.
#
# Usage :  sudo bash /opt/v10-watchdog/deploy/install-watchdog.sh

set -euo pipefail

APP_DIR=/opt/v10-watchdog
APP_USER=v10watch
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
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip >/dev/null

# Pas de contrainte 3.12 ici : le watchdog n'embarque ni numpy ni pandas.
PY_MAJ=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MIN=$(python3 -c 'import sys; print(sys.version_info[1])')
echo "Python système : $PY_MAJ.$PY_MIN"
if (( PY_MAJ < 3 || (PY_MAJ == 3 && PY_MIN < 9) )); then
    echo "ERREUR : Python >= 3.9 requis." >&2
    exit 1
fi

echo "── Utilisateur dédié ──"
id "$APP_USER" &>/dev/null || \
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "── Secrets ──"
[[ -f "$APP_DIR/.env" ]] || { echo "ERREUR : $APP_DIR/.env absent." >&2; exit 1; }
if grep -q "^HYPERLIQUID_API" "$APP_DIR/.env"; then
    echo "AVERTISSEMENT : le .env contient des clés Hyperliquid, inutiles ici." >&2
    echo "                Moindre privilège : les retirer de cette machine." >&2
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "── Environnement Python (surface minimale) ──"
as_app python3 -m venv "$APP_DIR/venv"
as_app "$APP_DIR/venv/bin/pip" install -q --upgrade pip
as_app "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/deploy/requirements-watchdog.txt"
as_app "$APP_DIR/venv/bin/pip" check

echo "── Unités systemd ──"
install -m 644 "$UNITS_DIR"/v10-watchdog.service        /etc/systemd/system/
install -m 644 "$UNITS_DIR"/v10-watchdog.timer          /etc/systemd/system/
install -m 644 "$UNITS_DIR"/v10-alert-watchdog@.service /etc/systemd/system/
systemctl daemon-reload
# --now : sans lui, le timer ne serait armé qu'au prochain démarrage.
systemctl enable --now v10-watchdog.timer >/dev/null

echo
echo "Installation terminée."
systemctl list-timers v10-watchdog.timer --no-pager | head -3
echo
echo "  exécution immédiate : systemctl start v10-watchdog.service"
echo "  test bout-en-bout   : runuser -u $APP_USER -- $APP_DIR/venv/bin/python -m scripts.external_watchdog --force-alert"
echo "  journal             : journalctl -u v10-watchdog -n 20"
