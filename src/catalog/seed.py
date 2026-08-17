"""Build `data/catalog.db` from the checked-in `data/seed_catalog.json`.

Run it with:

    uv run python -m catalog.seed

Idempotent by construction: every run drops and recreates the tables, so the
same JSON always yields the same database and there is no migration story to
maintain for what is sample data.
"""

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from catalog.db import DEFAULT_DB_PATH, PROJECT_ROOT, database_path

logger = logging.getLogger(__name__)

DEFAULT_SEED_PATH = PROJECT_ROOT / "data" / "seed_catalog.json"

SCHEMA_SQL = """
DROP TABLE IF EXISTS change_log;
DROP TABLE IF EXISTS features;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS competitors;

CREATE TABLE competitors (
    id          TEXT PRIMARY KEY,      -- e.g. "cmp_northline"
    name        TEXT NOT NULL,
    website     TEXT,
    is_us       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE products (
    id            TEXT PRIMARY KEY,    -- e.g. "prd_0142"
    competitor_id TEXT NOT NULL REFERENCES competitors(id),
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    price_usd     REAL,
    url           TEXT,
    description   TEXT,
    last_seen_at  TEXT NOT NULL        -- ISO-8601 date
);

CREATE TABLE features (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL REFERENCES products(id),
    name        TEXT NOT NULL,         -- normalized, e.g. "moisture_resistant"
    label       TEXT NOT NULL,         -- human readable, e.g. "Moisture resistant"
    value       TEXT                   -- optional, e.g. "IP65"
);

CREATE TABLE change_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL REFERENCES products(id),
    observed_at TEXT NOT NULL,         -- ISO-8601 date
    change_type TEXT NOT NULL,         -- 'added' | 'removed' | 'price_changed' | 'feature_added'
    detail      TEXT NOT NULL
);

CREATE INDEX idx_products_competitor ON products(competitor_id);
CREATE INDEX idx_features_product     ON features(product_id);
CREATE INDEX idx_changelog_observed   ON change_log(observed_at);
"""


def load_seed_payload(seed_path: Path | None = None) -> dict[str, Any]:
    """Read the seed JSON. Raises FileNotFoundError if it is missing."""
    path = seed_path or DEFAULT_SEED_PATH
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def build_database(connection: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, int]:
    """Populate a connection from a seed payload, replacing anything already there.

    Shared with the test fixture, which builds the same schema in memory from a
    smaller payload -- so tests exercise the real DDL rather than a copy of it
    that can drift.
    """
    connection.executescript(SCHEMA_SQL)

    competitors = payload["competitors"]
    products = payload["products"]
    change_log = payload.get("change_log", [])

    connection.executemany(
        "INSERT INTO competitors (id, name, website, is_us) VALUES (?, ?, ?, ?)",
        [(c["id"], c["name"], c.get("website"), int(c.get("is_us", 0))) for c in competitors],
    )

    connection.executemany(
        """
        INSERT INTO products
            (id, competitor_id, name, category, price_usd, url, description, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                p["id"],
                p["competitor_id"],
                p["name"],
                p["category"],
                p.get("price_usd"),
                p.get("url"),
                p.get("description"),
                p["last_seen_at"],
            )
            for p in products
        ],
    )

    feature_rows = [
        (p["id"], f["name"], f["label"], f.get("value"))
        for p in products
        for f in p.get("features", [])
    ]
    connection.executemany(
        "INSERT INTO features (product_id, name, label, value) VALUES (?, ?, ?, ?)",
        feature_rows,
    )

    connection.executemany(
        "INSERT INTO change_log (product_id, observed_at, change_type, detail) VALUES (?, ?, ?, ?)",
        [(c["product_id"], c["observed_at"], c["change_type"], c["detail"]) for c in change_log],
    )

    connection.commit()
    return {
        "competitors": len(competitors),
        "products": len(products),
        "features": len(feature_rows),
        "change_log": len(change_log),
    }


def seed(db_path: Path | None = None, seed_path: Path | None = None) -> dict[str, int]:
    """Build the catalog database on disk and return the row counts written."""
    target = db_path or database_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    # The DROP statements in SCHEMA_SQL already make a rebuild logically
    # idempotent, but SQLite reuses freed pages, so dropping in place leaves a
    # byte-different file each run. Removing the generated file first makes two
    # runs of the same JSON produce identical bytes, which is worth having when
    # the question "did the catalog change?" comes up.
    target.unlink(missing_ok=True)

    payload = load_seed_payload(seed_path)
    connection = sqlite3.connect(target)
    try:
        counts = build_database(connection, payload)
    finally:
        connection.close()
    return counts


def main() -> None:
    """Entry point for `python -m catalog.seed`."""
    # stderr, never stdout: this module shares a package with a stdio server and
    # anything on stdout there would corrupt the MCP wire protocol.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    target = database_path()
    counts = seed(db_path=target)
    logger.info(
        "Seeded %s: %d competitors, %d products, %d features, %d change-log entries.",
        target if target != DEFAULT_DB_PATH else target.relative_to(PROJECT_ROOT),
        counts["competitors"],
        counts["products"],
        counts["features"],
        counts["change_log"],
    )


if __name__ == "__main__":
    main()
