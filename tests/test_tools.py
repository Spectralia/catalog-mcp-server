"""The four tools, through their public callables.

Each tool gets at least a happy path, a not-found path, a malformed-input path,
and a boundary or ordering case. Every failure assertion also checks that the
call *returned* rather than raised: an MCP tool that raises gives the model
nothing to recover from, so "did not raise" is part of the contract.
"""

import pytest

from catalog import db
from catalog.errors import ErrorCode, ToolError
from catalog.models import ComparisonResult, FeatureGapReport, ProductDetail, SearchResult
from catalog.server import compare_products, find_feature_gaps, get_product, search_products

pytestmark = pytest.mark.usefixtures("catalog_db")


def assert_tool_error(result, code: ErrorCode) -> ToolError:
    """Assert a call failed as data, with the expected code and a usable message."""
    assert isinstance(result, ToolError), f"expected a ToolError, got {type(result).__name__}"
    assert result.error is True
    assert result.code is code
    assert result.message.strip()
    return result


# --------------------------------------------------------------------------
# search_products
# --------------------------------------------------------------------------


def test_search_products_returns_matching_summaries():
    result = search_products(query="shingle")

    assert isinstance(result, SearchResult)
    assert result.query_echo == "shingle"
    assert result.total_matched == 2
    assert result.returned == 2
    assert [product.name for product in result.results] == ["North Shingle", "Our Shingle"]
    assert result.results[1].competitor == "Us"
    assert result.results[1].price_usd == 100.00


def test_search_products_trims_the_query_before_matching():
    assert search_products(query="  shingle  ").query_echo == "shingle"


def test_search_products_rejects_an_unknown_competitor_by_name():
    error = assert_tool_error(
        search_products(query="deck", competitor="Northlin"), ErrorCode.NOT_FOUND
    )
    # The message has to carry the valid values, or the model cannot self-correct.
    assert "Northlin" in error.message
    assert "Northline" in error.message
    assert error.hint


def test_search_products_reports_an_ambiguous_competitor():
    error = assert_tool_error(
        search_products(query="deck", competitor="Cascade"), ErrorCode.AMBIGUOUS_INPUT
    )
    assert "Cascade Building Products" in error.message
    assert "Cascade Roofing" in error.message


def test_search_products_rejects_an_empty_query():
    assert_tool_error(search_products(query="   "), ErrorCode.INVALID_INPUT)


def test_search_products_rejects_a_single_character_query():
    assert_tool_error(search_products(query="d"), ErrorCode.INVALID_INPUT)


def test_search_products_rejects_an_overlong_query():
    assert_tool_error(search_products(query="d" * 101), ErrorCode.INVALID_INPUT)


@pytest.mark.parametrize("limit", [0, -1, 51, 1000])
def test_search_products_rejects_an_out_of_range_limit(limit):
    error = assert_tool_error(
        search_products(query="deck", limit=limit), ErrorCode.INVALID_INPUT
    )
    assert str(limit) in error.message


def test_search_products_reports_truncation_through_total_matched():
    result = search_products(query="deck", limit=2)

    assert result.total_matched == 4
    assert result.returned == 2
    assert len(result.results) == 2
    # The pair must be honest with each other, or the model cannot tell it was truncated.
    assert result.total_matched > result.returned


def test_search_products_accepts_the_boundary_limits():
    assert search_products(query="deck", limit=1).returned == 1
    assert search_products(query="deck", limit=50).returned == 4


def test_search_products_returns_an_empty_list_rather_than_an_error():
    result = search_products(query="nothingmatchesthis")

    assert isinstance(result, SearchResult)
    assert (result.total_matched, result.returned, result.results) == (0, 0, [])


def test_search_products_filters_by_competitor_and_category():
    result = search_products(query="deck", competitor="Us", category="Decking")

    assert result.total_matched == 1
    assert result.results[0].product_id == "prd_0001"


# --------------------------------------------------------------------------
# get_product
# --------------------------------------------------------------------------


def test_get_product_returns_full_detail():
    result = get_product(product_id="prd_0001")

    assert isinstance(result, ProductDetail)
    assert result.name == "Our Deck Board"
    assert result.competitor == "Us"
    assert result.url == "https://us.example.com/our-deck-board"
    assert [feature.label for feature in result.features] == ["Alpha", "Beta"]
    assert result.features[1].value == "B1"


def test_get_product_rejects_an_unknown_id():
    error = assert_tool_error(get_product(product_id="prd_9999"), ErrorCode.NOT_FOUND)
    assert "prd_9999" in error.message
    assert "search_products" in (error.hint or "")


@pytest.mark.parametrize("bad_id", ["prd_99", "0142", "PRD_0142", "prd_01423", "prd_abcd", ""])
def test_get_product_rejects_a_malformed_id(bad_id):
    # A distinct message from the unknown-id case: one says "reformat", the
    # other says "go look it up".
    error = assert_tool_error(get_product(product_id=bad_id), ErrorCode.INVALID_INPUT)
    assert "prd_0142" in (error.hint or "")


def test_get_product_caps_recent_changes_at_five_newest_first():
    result = get_product(product_id="prd_0002")

    assert len(result.recent_changes) == 5
    assert [change.observed_at for change in result.recent_changes] == [
        "2026-07-01",
        "2026-06-01",
        "2026-05-01",
        "2026-04-01",
        "2026-03-01",
    ]


def test_get_product_returns_an_empty_change_list_when_nothing_changed():
    assert get_product(product_id="prd_0001").recent_changes == []


# --------------------------------------------------------------------------
# compare_products
# --------------------------------------------------------------------------


def test_compare_products_splits_features_three_ways():
    result = compare_products(product_id_a="prd_0001", product_id_b="prd_0002")

    assert isinstance(result, ComparisonResult)
    assert result.shared_features == ["Alpha", "Beta"]
    assert result.only_in_a == []
    assert result.only_in_b == ["Epsilon", "Gamma", "Zeta"]
    assert result.price_delta_usd == 2.50
    assert "costs $2.50 more than" in result.summary


def test_compare_products_rejects_an_unknown_id():
    error = assert_tool_error(
        compare_products(product_id_a="prd_0001", product_id_b="prd_9999"), ErrorCode.NOT_FOUND
    )
    assert "product_id_b" in error.message


def test_compare_products_rejects_a_malformed_id():
    assert_tool_error(
        compare_products(product_id_a="deck", product_id_b="prd_0002"), ErrorCode.INVALID_INPUT
    )


def test_compare_products_rejects_two_identical_ids():
    error = assert_tool_error(
        compare_products(product_id_a="prd_0001", product_id_b="prd_0001"),
        ErrorCode.INVALID_INPUT,
    )
    assert "must differ" in error.message


def test_compare_products_is_symmetric():
    forward = compare_products(product_id_a="prd_0001", product_id_b="prd_0002")
    reverse = compare_products(product_id_a="prd_0002", product_id_b="prd_0001")

    assert forward.shared_features == reverse.shared_features
    assert forward.only_in_a == reverse.only_in_b
    assert forward.only_in_b == reverse.only_in_a
    assert forward.price_delta_usd == -reverse.price_delta_usd


def test_compare_products_leaves_the_delta_null_when_a_price_is_missing():
    result = compare_products(product_id_a="prd_0001", product_id_b="prd_0004")

    assert result.price_delta_usd is None
    assert "no comparable published price" in result.summary


# --------------------------------------------------------------------------
# find_feature_gaps
# --------------------------------------------------------------------------


def test_find_feature_gaps_reports_what_rivals_offer():
    result = find_feature_gaps(our_product_id="prd_0001")

    assert isinstance(result, FeatureGapReport)
    assert result.our_product.competitor == "Us"
    assert result.total_gaps_found == 4

    top = result.gaps[0]
    assert top.feature_label == "Gamma"
    assert top.competitor_count == 3
    assert top.offered_by == ["Cascade Building Products", "Cascade Roofing", "Northline"]
    assert top.example_product_id == "prd_0002"


def test_find_feature_gaps_sorts_by_reach_then_alphabetically():
    gaps = find_feature_gaps(our_product_id="prd_0001", limit=20).gaps

    assert [gap.feature_label for gap in gaps] == ["Gamma", "Delta", "Epsilon", "Zeta"]
    counts = [gap.competitor_count for gap in gaps]
    assert counts == sorted(counts, reverse=True)
    # Delta and Epsilon both sit at two competitors; alphabetical order breaks the tie.
    assert (gaps[1].feature_label, gaps[1].competitor_count) == ("Delta", 2)
    assert (gaps[2].feature_label, gaps[2].competitor_count) == ("Epsilon", 2)


def test_find_feature_gaps_ignores_other_categories():
    labels = {gap.feature_label for gap in find_feature_gaps(our_product_id="prd_0001", limit=20).gaps}

    # Omega exists only on a Roofing product; it is not a gap for a Decking board.
    assert "Omega" not in labels


def test_find_feature_gaps_truncates_but_still_reports_the_total():
    result = find_feature_gaps(our_product_id="prd_0001", limit=2)

    assert len(result.gaps) == 2
    assert result.total_gaps_found == 4


def test_find_feature_gaps_can_narrow_to_one_competitor():
    result = find_feature_gaps(our_product_id="prd_0001", competitor="Northline")

    assert [gap.feature_label for gap in result.gaps] == ["Epsilon", "Gamma", "Zeta"]
    assert all(gap.offered_by == ["Northline"] for gap in result.gaps)


def test_find_feature_gaps_scopes_to_the_products_own_category():
    result = find_feature_gaps(our_product_id="prd_0005")

    # Our Shingle is in Roofing, so only the Roofing rival is considered.
    assert result.total_gaps_found == 1
    assert result.gaps[0].feature_label == "Omega"
    assert result.gaps[0].example_product_id == "prd_0006"


def test_find_feature_gaps_reports_zero_when_no_rival_products_exist():
    # Cascade Building Products sells nothing in Roofing, so there is no gap to find.
    result = find_feature_gaps(our_product_id="prd_0005", competitor="Cascade Building Products")

    assert result.total_gaps_found == 0
    assert result.gaps == []


def test_find_feature_gaps_rejects_a_competitors_product():
    error = assert_tool_error(
        find_feature_gaps(our_product_id="prd_0002"), ErrorCode.WRONG_OWNER
    )
    # This is the confusable mistake, so the message must name both sides.
    assert "Northline" in error.message
    assert "North Deck" in error.message
    assert "competitor='Us'" in (error.hint or "")


def test_find_feature_gaps_rejects_an_unknown_id():
    assert_tool_error(find_feature_gaps(our_product_id="prd_9999"), ErrorCode.NOT_FOUND)


def test_find_feature_gaps_rejects_a_malformed_id():
    assert_tool_error(find_feature_gaps(our_product_id="prd_1"), ErrorCode.INVALID_INPUT)


@pytest.mark.parametrize("limit", [0, 21])
def test_find_feature_gaps_rejects_an_out_of_range_limit(limit):
    assert_tool_error(
        find_feature_gaps(our_product_id="prd_0001", limit=limit), ErrorCode.INVALID_INPUT
    )


def test_find_feature_gaps_rejects_filtering_on_our_own_line():
    error = assert_tool_error(
        find_feature_gaps(our_product_id="prd_0001", competitor="Us"), ErrorCode.INVALID_INPUT
    )
    assert "own product line" in error.message


# --------------------------------------------------------------------------
# Failure containment
# --------------------------------------------------------------------------


def test_an_unexpected_exception_becomes_a_tool_error(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated sqlite failure")

    monkeypatch.setattr(db, "get_product", explode)

    error = assert_tool_error(get_product(product_id="prd_0001"), ErrorCode.DATA_UNAVAILABLE)
    assert "simulated" not in error.message  # internals stay out of the model's context
    assert "get_product" in error.message


def test_a_missing_database_becomes_a_tool_error(tmp_path, monkeypatch):
    # Drop the injected connection so the tool falls through to the real path.
    db.use_connection(None)
    monkeypatch.setenv(db.DB_PATH_ENV_VAR, str(tmp_path / "nonexistent" / "catalog.db"))

    error = assert_tool_error(search_products(query="deck"), ErrorCode.DATA_UNAVAILABLE)
    # The hint has to name the exact command, or the model has to guess at setup.
    assert "catalog.seed" in (error.hint or "")


def test_an_unexpected_exception_is_logged_with_its_traceback(monkeypatch, caplog):
    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated sqlite failure")

    monkeypatch.setattr(db, "get_product", explode)
    get_product(product_id="prd_0001")

    assert "simulated sqlite failure" in caplog.text
