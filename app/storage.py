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
"""


def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
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


def create_batch(connection, name, definition, households, answers_by_household):
    """Insert a batch in one transaction.

    ``households`` is a list of ``(household_no, row_number)``.
    ``answers_by_household`` maps household_no to
    ``{(prefix_tuple, qid): value}``.
    """
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO batches (name, definition) VALUES (?, ?)",
            (name, json.dumps(definition, ensure_ascii=False)))
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


def save_answers(connection, household_id, changes):
    """Persist ``{key: value_or_None}`` for one household.

    ``key`` is ``(prefix_tuple, qid)``; ``None`` deletes.  All changes are
    written in a single transaction, so a failure leaves the previous answers
    fully intact.
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


def db_path_from_env():
    return os.environ.get("SURVEY_DB", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "survey.sqlite3"))
