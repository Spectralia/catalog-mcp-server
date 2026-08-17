"""The db layer on its own terms -- no MCP, no Pydantic, just rows."""

import pytest

from catalog import db
from catalog.errors import CatalogUnavailableError


def test_build_database_loads_every_table(catalog_db):
    counts = {
        table: catalog_db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("competitors", "products", "features", "change_log")
    }
    assert counts == {"competitors": 4, "products": 6, "features": 15, "change_log": 8}


def test_list_competitors_is_ordered_by_name(catalog_db):
    assert db.competitor_names(catalog_db) == [
        "Cascade Building Products",
        "Cascade Roofing",
        "Northline",
        "Us",
    ]


def test_us_competitor_finds_the_flagged_row(catalog_db):
    us = db.us_competitor(catalog_db)
    assert us is not None
    assert us["id"] == "cmp_us"
    assert us["is_us"] == 1


def test_find_competitors_matches_exact_id(catalog_db):
    matches = db.find_competitors(catalog_db, "cmp_northline")
    assert [row["name"] for row in matches] == ["Northline"]


def test_find_competitors_matches_name_case_insensitively(catalog_db):
    assert [row["id"] for row in db.find_competitors(catalog_db, "  northLINE ")] == [
        "cmp_northline"
    ]


def test_find_competitors_matches_a_whole_word_of_a_longer_name(catalog_db):
    # "Cascade" appears in two names, so this is the ambiguous case.
    matches = db.find_competitors(catalog_db, "Cascade")
    assert {row["id"] for row in matches} == {"cmp_cascade", "cmp_cascade_roof"}


def test_find_competitors_rejects_a_truncated_name(catalog_db):
    # Whole-word matching, not substring: "Northlin" must not silently become
    # "Northline", or a typo would resolve to the wrong company.
    assert db.find_competitors(catalog_db, "Northlin") == []


def test_find_competitors_rejects_blank_input(catalog_db):
    assert db.find_competitors(catalog_db, "   ") == []


def test_search_products_counts_before_limiting(catalog_db):
    total, rows = db.search_products(catalog_db, "deck", limit=2)
    assert total == 4
    assert len(rows) == 2


def test_search_products_orders_by_name(catalog_db):
    _, rows = db.search_products(catalog_db, "deck", limit=10)
    assert [row["name"] for row in rows] == [
        "Cascade Deck",
        "North Deck",
        "Our Deck Board",
        "Roof Deck Tile",
    ]


def test_search_products_escapes_like_wildcards(catalog_db):
    # A bare "%" would match every row if it were passed through to LIKE.
    total, rows = db.search_products(catalog_db, "%", limit=10)
    assert (total, rows) == (0, [])


def test_search_products_filters_by_competitor_and_category(catalog_db):
    total, rows = db.search_products(
        catalog_db, "our", competitor_id="cmp_us", category="Roofing", limit=10
    )
    assert total == 1
    assert rows[0]["id"] == "prd_0005"


def test_get_product_returns_none_for_an_unknown_id(catalog_db):
    assert db.get_product(catalog_db, "prd_9999") is None


def test_get_product_joins_the_competitor(catalog_db):
    row = db.get_product(catalog_db, "prd_0002")
    assert row["competitor_name"] == "Northline"
    assert row["is_us"] == 0


def test_get_recent_changes_returns_newest_first_within_the_limit(catalog_db):
    changes = db.get_recent_changes(catalog_db, "prd_0002", limit=5)
    assert len(changes) == 5
    dates = [row["observed_at"] for row in changes]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == "2026-07-01"


def test_products_in_category_excludes_the_owning_competitor(catalog_db):
    rows = db.products_in_category(catalog_db, "Decking", exclude_competitor_id="cmp_us")
    assert [row["id"] for row in rows] == ["prd_0002", "prd_0003", "prd_0004"]


def test_products_in_category_can_narrow_to_one_competitor(catalog_db):
    rows = db.products_in_category(
        catalog_db, "Decking", exclude_competitor_id="cmp_us", competitor_id="cmp_cascade"
    )
    assert [row["id"] for row in rows] == ["prd_0003"]


def test_features_for_products_handles_an_empty_batch(catalog_db):
    assert db.features_for_products(catalog_db, []) == []


def test_features_for_products_batches_by_id(catalog_db):
    rows = db.features_for_products(catalog_db, ["prd_0001", "prd_0004"])
    assert [(row["product_id"], row["label"]) for row in rows] == [
        ("prd_0001", "Alpha"),
        ("prd_0001", "Beta"),
        ("prd_0004", "Delta"),
        ("prd_0004", "Gamma"),
    ]


def test_connect_raises_a_recoverable_error_when_the_database_is_missing(missing_database):
    with pytest.raises(CatalogUnavailableError) as excinfo:
        with db.connect():
            pass
    assert "does not exist" in str(excinfo.value)
