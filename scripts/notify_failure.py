"""
Alerte Telegram sur échec d'une unité systemd (déclenché par OnFailure=).

Appelé par deploy/systemd/v10-alert@.service avec le nom de l'unité en échec.
Réutilise le Notifier existant plutôt que de refaire un appel HTTP : même
configuration, même format, un seul endroit à corriger.

Usage :
  python -m scripts.notify_failure v10-bot.service
"""

import socket
import subprocess
import sys
from datetime import datetime, timezone

from utils.notifier import Notifier


def unit_state(unit):
    """Etat et code de sortie de l'unité, ou None si systemctl indisponible."""
    try:
        out = subprocess.run(
            ["systemctl", "show", unit,
             "--property=Result,ExecMainStatus,NRestarts,ActiveState"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return dict(l.split("=", 1) for l in out.stdout.strip().splitlines() if "=" in l)
    except Exception:
        return None


def journal_tail(unit, lines=12):
    """Dernières lignes du journal de l'unité, pour diagnostiquer sans se connecter."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def build_message(unit):
    host = socket.gethostname()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"🚨 <b>V10 — échec de {unit}</b>",
             f"Machine : <code>{host}</code>",
             f"Horodatage : {now}"]

    state = unit_state(unit)
    if state:
        result = state.get("Result", "?")
        status = state.get("ExecMainStatus", "?")
        restarts = state.get("NRestarts", "?")
        lines.append(f"Cause : <code>{result}</code> (code {status})")
        lines.append(f"Redémarrages : {restarts}")
        # 137 = SIGKILL, signature de l'OOM killer (cf. incident du 22/08/2026)
        if status == "137" or result == "oom-kill":
            lines.append("⚠️ <b>Signature OOM</b> — vérifier MemoryMax et la fuite mémoire.")

    tail = journal_tail(unit)
    if tail:
        extrait = tail[-800:]
        lines.append(f"\n<pre>{extrait}</pre>")
    return "\n".join(lines)


def main():
    unit = sys.argv[1] if len(sys.argv) > 1 else "inconnue"
    ok = Notifier().send(build_message(unit))
    # Ne jamais faire échouer l'unité d'alerte : elle est appelée par OnFailure=,
    # un échec ici masquerait l'incident d'origine dans le journal.
    print(f"[NOTIFY_FAILURE] {unit} — Telegram {'envoyé' if ok else 'NON envoyé'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
