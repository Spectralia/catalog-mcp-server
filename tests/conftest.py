"""Shared fixtures.

Tests run against a small in-memory database built from the fixture payload
below, never against `data/catalog.db`. Two reasons: the seed file is sample
data that should be free to change without breaking assertions, and a fixture
small enough to hold in your head makes an ordering assertion readable.

The in-memory database is built with the same `build_database` the real seed
uses, so the DDL under test is the DDL that ships.
"""

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from catalog import db
from catalog.seed import build_database

# Four competitors, two of which share the word "Cascade" so that ambiguous
# competitor resolution has something to be ambiguous about.
FIXTURE_PAYLOAD: dict[str, Any] = {
    "competitors": [
        {"id": "cmp_us", "name": "Us", "website": "https://us.example.com", "is_us": 1},
        {
            "id": "cmp_northline",
            "name": "Northline",
            "website": "https://northline.example.com",
            "is_us": 0,
        },
        {
            "id": "cmp_cascade",
            "name": "Cascade Building Products",
            "website": "https://cascadebp.example.com",
            "is_us": 0,
        },
        {
            "id": "cmp_cascade_roof",
            "name": "Cascade Roofing",
            "website": "https://cascaderoof.example.com",
            "is_us": 0,
        },
    ],
    "products": [
        {
            "id": "prd_0001",
            "competitor_id": "cmp_us",
            "name": "Our Deck Board",
            "category": "Decking",
            "price_usd": 5.00,
            "url": "https://us.example.com/our-deck-board",
            "description": "House brand composite board.",
            "last_seen_at": "2026-08-01",
            "features": [
                {"name": "alpha", "label": "Alpha", "value": None},
                {"name": "beta", "label": "Beta", "value": "B1"},
            ],
        },
        {
            "id": "prd_0002",
            "competitor_id": "cmp_northline",
            "name": "North Deck",
            "category": "Decking",
            "price_usd": 7.50,
            "url": "https://northline.example.com/north-deck",
            "description": "Premium capped board.",
            "last_seen_at": "2026-08-02",
            "features": [
                {"name": "alpha", "label": "Alpha", "value": None},
                {"name": "beta", "label": "Beta", "value": None},
                {"name": "epsilon", "label": "Epsilon", "value": None},
                {"name": "gamma", "label": "Gamma", "value": "G1"},
                {"name": "zeta", "label": "Zeta", "value": None},
            ],
        },
        {
            "id": "prd_0003",
            "competitor_id": "cmp_cascade",
            "name": "Cascade Deck",
            "category": "Decking",
            "price_usd": 6.00,
            "url": "https://cascadebp.example.com/cascade-deck",
            "description": "Value board.",
            "last_seen_at": "2026-08-03",
            "features": [
                {"name": "alpha", "label": "Alpha", "value": None},
                {"name": "delta", "label": "Delta", "value": None},
                {"name": "epsilon", "label": "Epsilon", "value": None},
                {"name": "gamma", "label": "Gamma", "value": None},
            ],
        },
        {
            # No price: exercises the null price_delta_usd path.
            "id": "prd_0004",
            "competitor_id": "cmp_cascade_roof",
            "name": "Roof Deck Tile",
            "category": "Decking",
            "price_usd": None,
            "url": None,
            "description": "Paver tile for flat roofs.",
            "last_seen_at": "2026-08-04",
            "features": [
                {"name": "delta", "label": "Delta", "value": None},
                {"name": "gamma", "label": "Gamma", "value": None},
            ],
        },
        {
            "id": "prd_0005",
            "competitor_id": "cmp_us",
            "name": "Our Shingle",
            "category": "Roofing",
            "price_usd": 100.00,
            "url": "https://us.example.com/our-shingle",
            "description": "House brand architectural shingle.",
            "last_seen_at": "2026-08-05",
            "features": [{"name": "alpha", "label": "Alpha", "value": None}],
        },
        {
            # Omega lives in Roofing only: it must never surface as a Decking gap.
            "id": "prd_0006",
            "competitor_id": "cmp_northline",
            "name": "North Shingle",
            "category": "Roofing",
            "price_usd": 120.00,
            "url": "https://northline.example.com/north-shingle",
            "description": "Premium architectural shingle.",
            "last_seen_at": "2026-08-06",
            "features": [{"name": "omega", "label": "Omega", "value": None}],
        },
    ],
    # Seven entries on prd_0002 so the "five most recent" cap has something to cut.
    "change_log": [
        {
            "product_id": "prd_0002",
            "observed_at": f"2026-0{month}-01",
            "change_type": "price_changed",
            "detail": f"Change number {month}.",
        }
        for month in range(1, 8)
    ]
    + [
        {
            "product_id": "prd_0003",
            "observed_at": "2026-07-15",
            "change_type": "feature_added",
            "detail": "Cascade Deck added Epsilon.",
        }
    ],
}

# Gap ordering this data is designed to produce for prd_0001, which has Alpha
# and Beta, against the other Decking products: Gamma (3 competitors), Delta
# (2), Epsilon (2), Zeta (1). Delta and Epsilon tie, and the rows arrive with
# Epsilon first, so a count-only sort would order them wrongly -- which is what
# makes the alphabetical tie-break assertion in test_tools.py meaningful.


@pytest.fixture
def catalog_db() -> Iterator[sqlite3.Connection]:
    """An in-memory catalog, injected into the db layer for the duration of a test."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    build_database(connection, FIXTURE_PAYLOAD)

    db.use_connection(connection)
    try:
        yield connection
    finally:
        db.use_connection(None)
        connection.close()


@pytest.fixture
def missing_database(tmp_path, monkeypatch) -> None:
    """Point the db layer at a path that does not exist, with no override in place."""
    db.use_connection(None)
    monkeypatch.setenv(db.DB_PATH_ENV_VAR, str(tmp_path / "nonexistent" / "catalog.db"))
