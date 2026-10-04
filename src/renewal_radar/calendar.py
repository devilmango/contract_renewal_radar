from __future__ import annotations

from datetime import datetime, timezone

from .store import Store


def export_ics(store: Store) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Contract Renewal Radar//EN", "CALSCALE:GREGORIAN"]
    for task in store.list_tasks(status="open"):
        due = task["due_date"].replace("-", "")
        title = task["title"].replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
        description = f"Review renewal before contract expiration on {task['expiration_date']}"
        description = description.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{task['id']}@contract-renewal-radar",
            f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{due}",
            f"SUMMARY:{title}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
