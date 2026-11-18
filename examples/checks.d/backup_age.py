"""Example extra check: warn when a backup file is missing or too old.

Copy this file into the folder named by `extra_checks` in your settings, then add:

    [alerts.backup_age]
    file = "/var/backups/db.sql.gz"
    max_hours = 26
"""

import os
import time

NAME = "backup_age"

# Options of [alerts.backup_age]. duration and repeat_every work as for any alert.
DEFAULTS = {
    "file": "/var/backups/db.sql.gz",
    "max_hours": 26,
    "duration": 0,
    "repeat_every": 21600,
}


def check(cfg, api):
    """Return a list of api.Reading. Keys must be NAME, or start with NAME + ':'."""
    try:
        age = time.time() - os.path.getmtime(cfg["file"])
    except OSError as e:
        return [api.Reading(NAME, "Backup", True, f"cannot read {cfg['file']}: {e.strerror or e}")]
    limit = cfg["max_hours"] * 3600
    detail = f"{cfg['file']} is {api.format_duration(age)} old (limit {cfg['max_hours']}h)"
    return [api.Reading(NAME, "Backup", age > limit, detail)]
