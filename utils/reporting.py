"""
Helpers de reporting purs et testables.
"""

from datetime import datetime, timezone


def daily_report_window(now: datetime):
    """Fenêtre du bilan de la VEILLE (jour complet précédant `now`).

    Retourne (day_start_ms, today_start_ms, day_label) — bornes en
    millisecondes epoch (comme les timestamps stockés en base, en Int64).
    La fenêtre du rapport est [day_start_ms, today_start_ms).

    NB : comparer en int-ms est crucial — un objet datetime brut ne matche
    AUCUN document Int64 dans MongoDB (types BSON disjoints).
    """
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)
    day_start_ms = today_start_ms - 86400 * 1000
    day_label = datetime.fromtimestamp(
        day_start_ms / 1000, timezone.utc
    ).strftime("%Y-%m-%d")
    return day_start_ms, today_start_ms, day_label
