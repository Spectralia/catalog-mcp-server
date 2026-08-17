"""SQLite access layer.

Deliberately free of MCP and Pydantic imports: this module knows about rows and
SQL, the tool layer knows about schemas, and the two are testable apart. Every
query takes an open connection so a caller can run several against one
transaction -- and so tests can hand it an in-memory database.
"""

import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from catalog.errors import SEED_COMMAND, CatalogUnavailableError

# src/catalog/db.py -> src/catalog -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "catalog.db"
DB_PATH_ENV_VAR = "CATALOG_DB_PATH"

# Set by tests via use_connection(); None in normal operation.
_connection_override: sqlite3.Connection | None = None


def database_path() -> Path:
    """Resolve the catalog database path, honouring the CATALOG_DB_PATH override."""
    override = os.environ.get(DB_PATH_ENV_VAR)
    return Path(override) if override else DEFAULT_DB_PATH


def use_connection(connection: sqlite3.Connection | None) -> None:
    """Point every subsequent `connect()` at this connection.

    The seam tests use to run against an in-memory database. Passing None
    restores normal file-backed behaviour.
    """
    # A module-level singleton is the seam here: the tool layer calls connect()
    # with no arguments by design, so there is nowhere else to thread it.
    global _connection_override  # noqa: PLW0603
    _connection_override = connection


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a read-only connection to the catalog.

    Raises CatalogUnavailableError -- never at import time -- if the database
    has not been built yet, so a missing file surfaces as a recoverable tool
    error rather than a server that will not start.
    """
    if _connection_override is not None:
        yield _connection_override
        return

    path = database_path()
    if not path.exists():
        raise CatalogUnavailableError(
            f"The catalog database does not exist at {path}. It is generated, not checked in."
        )

    try:
        # mode=ro: every tool here is read-only, so let SQLite enforce that.
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise CatalogUnavailableError(
            f"The catalog database at {path} could not be opened ({exc}). "
            f"Rebuild it with: {SEED_COMMAND}"
        ) from exc

    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards so a query of '100%' does not match everything."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_PRODUCT_COLUMNS = """
    p.id            AS id,
    p.competitor_id AS competitor_id,
    p.name          AS name,
    p.category      AS category,
    p.price_usd     AS price_usd,
    p.url           AS url,
    p.description   AS description,
    p.last_seen_at  AS last_seen_at,
    c.name          AS competitor_name,
    c.is_us         AS is_us
"""


def list_competitors(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every competitor, ordered by name."""
    return connection.execute(
        "SELECT id, name, website, is_us FROM competitors ORDER BY name"
    ).fetchall()


def competitor_names(connection: sqlite3.Connection) -> list[str]:
    """Every competitor name, ordered -- used to make 'unknown competitor' errors actionable."""
    return [row["name"] for row in list_competitors(connection)]


def us_competitor(connection: sqlite3.Connection) -> sqlite3.Row | None:
    """The competitor row flagged `is_us`, i.e. our own product line."""
    return connection.execute(
        "SELECT id, name, website, is_us FROM competitors WHERE is_us = 1 ORDER BY id LIMIT 1"
    ).fetchone()


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens of a name, e.g. 'Cascade Building Products'."""
    return {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}


def find_competitors(connection: sqlite3.Connection, value: str) -> list[sqlite3.Row]:
    """Resolve a competitor by id or name.

    Returns every plausible match so the caller can distinguish "no such
    competitor" from "you were ambiguous". Exact id and exact name win
    outright; otherwise every word given must appear as a whole word in the
    name, which lets 'Cascade' resolve to 'Cascade Building Products' while a
    truncation like 'Northlin' still fails against 'Northline'. Matching on
    whole words rather than substrings matters here: a typo that silently
    resolves to the wrong company is worse than one that returns an error the
    model can correct. The table is small enough that matching in Python keeps
    all the rules in one readable place.
    """
    needle = value.strip().lower()
    if not needle:
        return []

    rows = list_competitors(connection)
    exact = [row for row in rows if needle in (row["id"].lower(), row["name"].lower())]
    if exact:
        return exact

    tokens = _tokenize(needle)
    if not tokens:
        return []
    return [row for row in rows if tokens <= _tokenize(row["name"])]


def search_products(
    connection: sqlite3.Connection,
    query: str,
    competitor_id: str | None = None,
    category: str | None = None,
    limit: int = 10,
) -> tuple[int, list[sqlite3.Row]]:
    """Substring-search products, returning (total matched, the first `limit` rows).

    The total is counted before limiting so the caller can tell the model it is
    looking at a truncated view.
    """
    pattern = f"%{_escape_like(query.lower())}%"
    rows = connection.execute(
        f"""
        SELECT {_PRODUCT_COLUMNS}
        FROM products p
        JOIN competitors c ON c.id = p.competitor_id
        WHERE (
                LOWER(p.name) LIKE :pattern ESCAPE '\\'
                OR LOWER(p.category) LIKE :pattern ESCAPE '\\'
                OR LOWER(COALESCE(p.description, '')) LIKE :pattern ESCAPE '\\'
              )
          AND (:competitor_id IS NULL OR p.competitor_id = :competitor_id)
          AND (:category IS NULL OR LOWER(p.category) = LOWER(:category))
        ORDER BY p.name, p.id
        """,
        {"pattern": pattern, "competitor_id": competitor_id, "category": category},
    ).fetchall()
    return len(rows), rows[:limit]


def get_product(connection: sqlite3.Connection, product_id: str) -> sqlite3.Row | None:
    """One product joined to its competitor, or None if the id matches nothing."""
    return connection.execute(
        f"""
        SELECT {_PRODUCT_COLUMNS}
        FROM products p
        JOIN competitors c ON c.id = p.competitor_id
        WHERE p.id = :product_id
        """,
        {"product_id": product_id},
    ).fetchone()


def get_features(connection: sqlite3.Connection, product_id: str) -> list[sqlite3.Row]:
    """Every feature on a product, ordered by label for stable output."""
    return connection.execute(
        """
        SELECT name, label, value
        FROM features
        WHERE product_id = :product_id
        ORDER BY label, name
        """,
        {"product_id": product_id},
    ).fetchall()


def get_recent_changes(
    connection: sqlite3.Connection, product_id: str, limit: int = 5
) -> list[sqlite3.Row]:
    """The most recent change-log entries for a product, newest first."""
    return connection.execute(
        """
        SELECT observed_at, change_type, detail
        FROM change_log
        WHERE product_id = :product_id
        ORDER BY observed_at DESC, id DESC
        LIMIT :limit
        """,
        {"product_id": product_id, "limit": limit},
    ).fetchall()


def products_in_category(
    connection: sqlite3.Connection,
    category: str,
    exclude_competitor_id: str,
    competitor_id: str | None = None,
) -> list[sqlite3.Row]:
    """Rival products in one category, optionally narrowed to a single competitor.

    `exclude_competitor_id` drops our own line so a gap analysis never reports
    our own features back to us.
    """
    return connection.execute(
        f"""
        SELECT {_PRODUCT_COLUMNS}
        FROM products p
        JOIN competitors c ON c.id = p.competitor_id
        WHERE p.category = :category
          AND p.competitor_id != :exclude_competitor_id
          AND (:competitor_id IS NULL OR p.competitor_id = :competitor_id)
        ORDER BY p.id
        """,
        {
            "category": category,
            "exclude_competitor_id": exclude_competitor_id,
            "competitor_id": competitor_id,
        },
    ).fetchall()


def features_for_products(
    connection: sqlite3.Connection, product_ids: Sequence[str]
) -> list[sqlite3.Row]:
    """Features for a batch of products in one round trip, ordered by product id."""
    if not product_ids:
        return []
    placeholders = ",".join("?" for _ in product_ids)
    return connection.execute(
        f"""
        SELECT product_id, name, label, value
        FROM features
        WHERE product_id IN ({placeholders})
        ORDER BY product_id, label
        """,
        tuple(product_ids),
    ).fetchall()
