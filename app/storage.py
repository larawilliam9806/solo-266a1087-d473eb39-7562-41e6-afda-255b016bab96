"""SQLite persistence for survey batches, households, and path answers."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    has_refusals INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS households (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    household_no TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    UNIQUE (batch_id, household_no)
);

CREATE TABLE IF NOT EXISTS answers (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL,
    qid TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (household_id, prefix, qid)
);

CREATE TABLE IF NOT EXISTS refusals (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL,
    qid TEXT NOT NULL,
    target_qid TEXT NOT NULL,
    PRIMARY KEY (household_id, prefix, qid)
);
"""

# Column added later; applied to databases created before refusals existed.
_MIGRATIONS = (
    "ALTER TABLE batches ADD COLUMN has_refusals INTEGER NOT NULL DEFAULT 0",
)


def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    existing = {row["name"] for row in connection.execute(
        "PRAGMA table_info(batches)")}
    for statement in _MIGRATIONS:
        column = statement.split("ADD COLUMN")[1].split()[0]
        if column not in existing:
            connection.execute(statement)
    connection.commit()
    return connection


@contextmanager
def transaction(connection):
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _prefix_key(prefix):
    return json.dumps(list(prefix), ensure_ascii=False, separators=(",", ":"))


def _prefix_tuple(key):
    try:
        value = json.loads(key)
    except (TypeError, ValueError):
        return ()
    return tuple(str(item) for item in value)


def create_batch(connection, name, definition, households,
                 answers_by_household, refusals_by_household=None,
                 has_refusals=False):
    """Insert a batch in one transaction.

    ``households`` is a list of ``(household_no, row_number)``.
    ``answers_by_household`` maps household_no to
    ``{(prefix_tuple, qid): value}``; ``refusals_by_household`` maps it to
    ``{(prefix_tuple, qid): (source_qid, target_qid)}``.
    """
    refusals_by_household = refusals_by_household or {}
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO batches (name, definition, has_refusals) "
            "VALUES (?, ?, ?)",
            (name, json.dumps(definition, ensure_ascii=False),
             1 if has_refusals else 0))
        batch_id = cursor.lastrowid
        for household_no, row_number in households:
            cursor = connection.execute(
                "INSERT INTO households (batch_id, household_no, row_number) "
                "VALUES (?, ?, ?)",
                (batch_id, household_no, row_number))
            household_id = cursor.lastrowid
            rows = [
                (household_id, _prefix_key(prefix), qid, value)
                for (prefix, qid), value in
                (answers_by_household.get(household_no) or {}).items()
            ]
            connection.executemany(
                "INSERT OR REPLACE INTO answers "
                "(household_id, prefix, qid, value) VALUES (?, ?, ?, ?)", rows)
            refusal_rows = [
                (household_id, _prefix_key(prefix), qid, pair[1])
                for (prefix, qid), pair in
                refusals_by_household.get(household_no, {}).items()
            ]
            connection.executemany(
                "INSERT OR REPLACE INTO refusals "
                "(household_id, prefix, qid, target_qid) "
                "VALUES (?, ?, ?, ?)", refusal_rows)
    return batch_id


def get_batch(connection, batch_id):
    row = connection.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "definition": json.loads(row["definition"]),
        "has_refusals": bool(row["has_refusals"]),
        "created_at": row["created_at"],
    }


def list_batches(connection):
    rows = connection.execute(
        "SELECT b.id, b.name, b.created_at, COUNT(h.id) AS households "
        "FROM batches b LEFT JOIN households h ON h.batch_id = b.id "
        "GROUP BY b.id ORDER BY b.id DESC").fetchall()
    return [dict(row) for row in rows]


def list_households(connection, batch_id):
    rows = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? ORDER BY row_number, id", (batch_id,)).fetchall()
    return [dict(row) for row in rows]


def get_household(connection, batch_id, household_id):
    row = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? AND id = ?",
        (batch_id, household_id)).fetchone()
    return dict(row) if row else None


def load_answers(connection, household_id):
    rows = connection.execute(
        "SELECT prefix, qid, value FROM answers WHERE household_id = ?",
        (household_id,)).fetchall()
    return {(_prefix_tuple(row["prefix"]), row["qid"]): row["value"]
            for row in rows}


def load_refusals(connection, household_id):
    """Refusal records as ``{(prefix_tuple, qid): (qid, target_qid)}``."""
    rows = connection.execute(
        "SELECT prefix, qid, target_qid FROM refusals "
        "WHERE household_id = ?", (household_id,)).fetchall()
    return {(_prefix_tuple(row["prefix"]), row["qid"]):
            (row["qid"], row["target_qid"]) for row in rows}


def save_answers(connection, household_id, changes, refusal_changes=None):
    """Persist ``{key: value_or_None}`` for one household.

    ``key`` is ``(prefix_tuple, qid)``; ``None`` deletes.  ``refusal_changes``
    holds refusal deletions (the editor never creates new refusals).  All
    changes are written in a single transaction, so a failure leaves the
    previous answers and refusal records fully intact.
    """
    with transaction(connection):
        for (prefix, qid), value in changes.items():
            key = _prefix_key(prefix)
            if value is None:
                connection.execute(
                    "DELETE FROM answers WHERE household_id = ? "
                    "AND prefix = ? AND qid = ?",
                    (household_id, key, qid))
            else:
                connection.execute(
                    "INSERT INTO answers (household_id, prefix, qid, value) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (household_id, prefix, qid) "
                    "DO UPDATE SET value = excluded.value",
                    (household_id, key, qid, value))
        for (prefix, qid) in (refusal_changes or {}):
            connection.execute(
                "DELETE FROM refusals WHERE household_id = ? "
                "AND prefix = ? AND qid = ?",
                (household_id, _prefix_key(prefix), qid))


def db_path_from_env():
    return os.environ.get("SURVEY_DB", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "survey.sqlite3"))
