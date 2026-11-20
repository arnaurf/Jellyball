"""Schema migrations owned by the streaming engine lane (versions 500-599).

Add a (version, function) pair to MIGRATIONS; db.py folds every lane's list
into SCHEMA_MIGRATIONS and applies the pending ones in version order. Rules:

- Stay inside this lane's range (db.MIGRATION_RANGES). A test enforces it, so
  two lanes can never pick the same number.
- A function receives the open sqlite3 connection and must be idempotent
  (CREATE TABLE IF NOT EXISTS, a column check before ALTER TABLE): a fresh
  database runs every migration too.
- Never edit or renumber a migration that has shipped. Add a new one.
- This module must not import db (db imports it).
"""

import sqlite3


def _multiview_member_delays(conn: sqlite3.Connection) -> None:
    """Per-member delays of a Multi-View (JSON list of seconds, '' = all live),
    used to sync a picture-in-picture corner stream with the main feed."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(multiview_channels)").fetchall()}
    if "member_delays" not in existing:
        conn.execute("ALTER TABLE multiview_channels ADD COLUMN member_delays TEXT DEFAULT ''")


MIGRATIONS: list = [
    (500, _multiview_member_delays),
]
