from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

DB_PATH = Path("data/cruises.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sailings (
    cruise_id TEXT PRIMARY KEY,
    line TEXT NOT NULL,
    name TEXT,
    ship TEXT,
    departure_port TEXT,
    arrival_port TEXT,
    departure_date TEXT,
    nights INTEGER,
    itinerary TEXT,
    url TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cruise_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    one_guest_price REAL,
    two_guest_pp_price REAL,
    two_guest_total REAL,
    currency TEXT DEFAULT 'AUD',
    FOREIGN KEY (cruise_id) REFERENCES sailings(cruise_id)
);

CREATE INDEX IF NOT EXISTS idx_prices_cruise_checked
ON prices(cruise_id, checked_at);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)

    # Existing repositories may have a database created before the active flag existed.
    columns = {row[1] for row in db.execute("PRAGMA table_info(sailings)").fetchall()}
    if "active" not in columns:
        db.execute("ALTER TABLE sailings ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        db.commit()
    return db


def save_observations(records: Iterable[Dict[str, Any]], line: str | None = None) -> None:
    records = list(records)
    if not records:
        return

    checked = datetime.now(timezone.utc).isoformat()
    line = line or records[0].get("line")

    with connect() as db:
        # A current scrape defines the active catalogue for that cruise line. Old sailings
        # remain in the database for price-history purposes, but disappear from the live site.
        if line:
            db.execute("UPDATE sailings SET active=0 WHERE line=?", (line,))

        for r in records:
            db.execute(
                """
                INSERT INTO sailings (
                    cruise_id,line,name,ship,departure_port,arrival_port,
                    departure_date,nights,itinerary,url,active
                ) VALUES (?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(cruise_id) DO UPDATE SET
                    name=excluded.name,
                    ship=excluded.ship,
                    departure_port=excluded.departure_port,
                    arrival_port=excluded.arrival_port,
                    departure_date=excluded.departure_date,
                    nights=excluded.nights,
                    itinerary=excluded.itinerary,
                    url=excluded.url,
                    active=1
                """,
                (
                    r["cruise_id"],
                    r["line"],
                    r.get("name"),
                    r.get("ship"),
                    r.get("departure_port"),
                    r.get("arrival_port"),
                    r.get("departure_date"),
                    r.get("nights"),
                    r.get("itinerary"),
                    r.get("url"),
                ),
            )
            db.execute(
                """
                INSERT INTO prices (
                    cruise_id,checked_at,available,one_guest_price,
                    two_guest_pp_price,two_guest_total,currency
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    r["cruise_id"],
                    checked,
                    1 if r.get("available", True) else 0,
                    r.get("one_guest_price"),
                    r.get("two_guest_pp_price"),
                    r.get("two_guest_total"),
                    r.get("currency", "AUD"),
                ),
            )


def latest_rows():
    with connect() as db:
        rows = db.execute(
            """
        WITH ranked AS (
            SELECT p.*,
                   ROW_NUMBER() OVER (PARTITION BY cruise_id ORDER BY checked_at DESC, id DESC) rn,
                   LAG(COALESCE(two_guest_pp_price, one_guest_price))
                     OVER (PARTITION BY cruise_id ORDER BY checked_at, id) AS prev_price
            FROM prices p
        ),
        stats AS (
            SELECT cruise_id,
                   MIN(COALESCE(two_guest_pp_price, one_guest_price)) AS historical_low,
                   MAX(COALESCE(two_guest_pp_price, one_guest_price)) AS historical_high
            FROM prices
            GROUP BY cruise_id
        )
        SELECT s.*, r.checked_at, r.available,
               r.one_guest_price, r.two_guest_pp_price, r.two_guest_total, r.currency,
               r.prev_price, st.historical_low, st.historical_high
        FROM sailings s
        JOIN ranked r ON r.cruise_id=s.cruise_id AND r.rn=1
        JOIN stats st ON st.cruise_id=s.cruise_id
        WHERE s.active=1
        ORDER BY s.departure_date
        """
        ).fetchall()
        return [dict(x) for x in rows]


def history_for(cruise_id: str):
    with connect() as db:
        rows = db.execute(
            """
            SELECT checked_at, available, one_guest_price, two_guest_pp_price,
                   two_guest_total, currency
            FROM prices
            WHERE cruise_id=?
            ORDER BY checked_at
            """,
            (cruise_id,),
        ).fetchall()
        return [dict(x) for x in rows]
