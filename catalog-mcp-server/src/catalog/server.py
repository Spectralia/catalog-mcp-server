"""MCP server exposing the competitive product catalog as four typed tools.

Every tool is read-only, takes described and validated parameters, returns a
Pydantic model, and returns `ToolError` rather than raising on any failure.
Input is validated before the database is touched, so a malformed argument
costs the model a schema error rather than a query.
"""

import logging
import re
import sqlite3
import sys
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from catalog import __version__, db
from catalog.errors import (
    ToolError,
    ambiguous_input,
    guarded,
    invalid_input,
    not_found,
    wrong_owner,
)
from catalog.models import (
    ChangeEntry,
    ComparisonResult,
    Feature,
    FeatureGap,
    FeatureGapReport,
    ProductDetail,
    ProductSummary,
    SearchResult,
)

logger = logging.getLogger(__name__)

PRODUCT_ID_PATTERN = re.compile(r"^prd_[0-9]{4}$")
PRODUCT_ID_HINT = (
    "Product ids look like 'prd_0142': the prefix 'prd_' followed by exactly four digits. "
    "Call search_products to find the id you want; ids are not guessable."
)
QUERY_MIN_LENGTH = 2
QUERY_MAX_LENGTH = 100
RECENT_CHANGE_COUNT = 5

mcp = MCPServer(
    "catalog",
    version=__version__,
    instructions=(
        "Competitive product intelligence for a building-materials catalog. "
        "Start with search_products to turn a name or category into a product id, then use "
        "get_product, compare_products, or find_feature_gaps. The competitor named 'Us' is our "
        "own product line. Tools return either their result model or an object with error=true; "
        "read its 'code' and 'hint' to correct the call."
    ),
)


# --------------------------------------------------------------------------
# Validation helpers. Each returns either the cleaned value or a ToolError,
# so a tool body reads as a sequence of early returns.
# --------------------------------------------------------------------------


def _validate_product_id(value: str, param_name: str) -> str | ToolError:
    """Check an id against `^prd_[0-9]{4}$`, returning the stripped id."""
    candidate = value.strip()
    if not PRODUCT_ID_PATTERN.fullmatch(candidate):
        return invalid_input(
            f"The {param_name} '{value}' is not a well-formed product id.",
            hint=PRODUCT_ID_HINT,
        )
    return candidate


def _validate_limit(value: int, low: int, high: int) -> int | ToolError:
    """Reject an out-of-range limit rather than silently clamping it.

    Clamping would let the model believe it asked for 200 results and received
    all of them; an explicit rejection keeps its own bookkeeping honest.
    """
    if not low <= value <= high:
        return invalid_input(
            f"limit={value} is out of range; it must be between {low} and {high} inclusive.",
            hint=f"Retry with limit={high} for the widest allowed view, or a smaller number.",
        )
    return value


def _clean_query(value: str) -> str | ToolError:
    """Trim a search query and enforce its length bounds."""
    candidate = value.strip()
    if not candidate:
        return invalid_input(
            "query was empty or whitespace only.",
            hint=(
                "Pass a product name, material, or category keyword of at least "
                f"{QUERY_MIN_LENGTH} characters, for example 'decking' or 'composite'."
            ),
        )
    if len(candidate) < QUERY_MIN_LENGTH:
        return invalid_input(
            f"query '{candidate}' is too short; it must be at least {QUERY_MIN_LENGTH} characters.",
            hint="Single letters match too much to be useful. Use a whole word.",
        )
    if len(candidate) > QUERY_MAX_LENGTH:
        return invalid_input(
            f"query is {len(candidate)} characters; the maximum is {QUERY_MAX_LENGTH}.",
            hint="Search on a distinctive keyword or two rather than a whole sentence.",
        )
    return candidate


def _clean_optional(value: str | None) -> str | None:
    """Treat an all-whitespace optional filter as if it had been omitted."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_competitor(
    connection: sqlite3.Connection, value: str
) -> sqlite3.Row | ToolError:
    """Resolve a competitor name or id, or explain why it could not be resolved."""
    matches = db.find_competitors(connection, value)
    known = ", ".join(db.competitor_names(connection))

    if not matches:
        return not_found(
            f"Unknown competitor '{value}'. Valid competitors: {known}.",
            hint=(
                "Retry with one of those names, or omit the competitor filter to search all "
                "of them at once."
            ),
        )
    if len(matches) > 1:
        candidates = ", ".join(row["name"] for row in matches)
        return ambiguous_input(
            f"The competitor '{value}' matches more than one company: {candidates}.",
            hint="Retry with the full name exactly as listed.",
        )
    return matches[0]


def _summary(row: sqlite3.Row) -> ProductSummary:
    """Project a joined product row onto the summary schema."""
    return ProductSummary(
        product_id=row["id"],
        name=row["name"],
        competitor=row["competitor_name"],
        category=row["category"],
        price_usd=row["price_usd"],
        last_seen_at=row["last_seen_at"],
    )


def _missing_product(product_id: str, param_name: str) -> ToolError:
    """The standard NOT_FOUND for a well-formed id that matches no row."""
    return not_found(
        f"No product with id '{product_id}' exists in the catalog ({param_name}).",
        hint=(
            "The id is well formed but unknown. Call search_products with a name, material, "
            "or category keyword to get a real id, then retry."
        ),
    )


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
def search_products(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text keyword matched case-insensitively as a substring of the product "
                "name, category, or description. Between 2 and 100 characters after trimming. "
                "Use one or two distinctive words, e.g. 'composite' or 'lap siding'."
            )
        ),
    ],
    competitor: Annotated[
        str | None,
        Field(
            description=(
                "Restrict to one competitor by name or id, e.g. 'Northline' or 'cmp_northline'. "
                "Pass 'Us' for our own product line. Omit to search every competitor."
            )
        ),
    ] = None,
    category: Annotated[
        str | None,
        Field(
            description=(
                "Exact category filter, e.g. 'Decking' or 'Roofing'. Case-insensitive but not "
                "fuzzy: use a category string copied from an earlier result. Omit for all categories."
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of products to return, between 1 and 50. Defaults to 10. "
                "Out-of-range values are rejected rather than clamped."
            )
        ),
    ] = 10,
) -> SearchResult | ToolError:
    """Find products in the competitive catalog by keyword.

    Use this first whenever you have a product name, material, or category but
    not a product id. Every other tool here needs an id, and this is where ids
    come from -- do not guess them.

    Returns compact summaries rather than full records, so a broad search stays
    cheap; follow up with get_product on anything worth a closer look. Compare
    `total_matched` against `returned`: if it is larger, you are looking at a
    truncated view and should narrow the query before drawing conclusions. An
    empty result list is a valid answer, not an error -- it means the catalog
    has nothing matching that keyword.
    """
    return guarded(
        "search_products",
        lambda: _search_products_impl(query, competitor, category, limit),
    )


def _search_products_impl(
    query: str, competitor: str | None, category: str | None, limit: int
) -> SearchResult | ToolError:
    cleaned_query = _clean_query(query)
    if isinstance(cleaned_query, ToolError):
        return cleaned_query

    checked_limit = _validate_limit(limit, 1, 50)
    if isinstance(checked_limit, ToolError):
        return checked_limit

    competitor_filter = _clean_optional(competitor)
    category_filter = _clean_optional(category)

    with db.connect() as connection:
        competitor_id = None
        if competitor_filter is not None:
            resolved = _resolve_competitor(connection, competitor_filter)
            if isinstance(resolved, ToolError):
                return resolved
            competitor_id = resolved["id"]

        total, rows = db.search_products(
            connection,
            cleaned_query,
            competitor_id=competitor_id,
            category=category_filter,
            limit=checked_limit,
        )

    return SearchResult(
        query_echo=cleaned_query,
        total_matched=total,
        returned=len(rows),
        results=[_summary(row) for row in rows],
    )


@mcp.tool()
def get_product(
    product_id: Annotated[
        str,
        Field(
            description=(
                "Catalog id of the product, formatted 'prd_' followed by exactly four digits, "
                "e.g. 'prd_0142'. Obtain it from search_products rather than guessing."
            )
        ),
    ],
) -> ProductDetail | ToolError:
    """Fetch one product with its full feature list and recent change history.

    Use this once search_products has given you an id and you need the detail a
    summary omits: every recorded feature, the source URL, the published
    description, and the five most recent observed changes.

    Prefer this over a broad search when the user asks about a specific product.
    It returns one record, so it is the cheapest way to get complete
    information about a single item.
    """
    return guarded("get_product", lambda: _get_product_impl(product_id))


def _get_product_impl(product_id: str) -> ProductDetail | ToolError:
    checked_id = _validate_product_id(product_id, "product_id")
    if isinstance(checked_id, ToolError):
        return checked_id

    with db.connect() as connection:
        row = db.get_product(connection, checked_id)
        if row is None:
            return _missing_product(checked_id, "product_id")

        features = db.get_features(connection, checked_id)
        changes = db.get_recent_changes(connection, checked_id, RECENT_CHANGE_COUNT)

    summary = _summary(row)
    return ProductDetail(
        **summary.model_dump(),
        url=row["url"],
        description=row["description"],
        features=[
            Feature(name=f["name"], label=f["label"], value=f["value"]) for f in features
        ],
        recent_changes=[
            ChangeEntry(
                observed_at=c["observed_at"],
                change_type=c["change_type"],
                detail=c["detail"],
            )
            for c in changes
        ],
    )


@mcp.tool()
def compare_products(
    product_id_a: Annotated[
        str,
        Field(
            description=(
                "Catalog id of the first product, e.g. 'prd_0101'. Feature differences are "
                "reported relative to this one."
            )
        ),
    ],
    product_id_b: Annotated[
        str,
        Field(
            description=(
                "Catalog id of the second product, e.g. 'prd_0122'. Must differ from "
                "product_id_a. Price delta is computed as B minus A."
            )
        ),
    ],
) -> ComparisonResult | ToolError:
    """Diff two products feature by feature, with the price difference.

    Use this when the user asks how two specific products stack up, or after
    find_feature_gaps points you at a competitor product worth examining
    head-to-head against ours.

    Feature sets are split three ways -- shared, only on A, only on B -- and
    `price_delta_usd` is B minus A, so a positive number means B costs more.
    That delta is null when either product has no published price; do not infer
    a direction in that case. Compare the two products' categories before
    quoting a delta: prices are per unit, and units differ across categories.
    """
    return guarded(
        "compare_products", lambda: _compare_products_impl(product_id_a, product_id_b)
    )


def _compare_products_impl(product_id_a: str, product_id_b: str) -> ComparisonResult | ToolError:
    checked_a = _validate_product_id(product_id_a, "product_id_a")
    if isinstance(checked_a, ToolError):
        return checked_a

    checked_b = _validate_product_id(product_id_b, "product_id_b")
    if isinstance(checked_b, ToolError):
        return checked_b

    if checked_a == checked_b:
        return invalid_input(
            f"product_id_a and product_id_b are both '{checked_a}'; the two ids must differ.",
            hint=(
                "Comparing a product with itself yields nothing. Call search_products to find "
                "a second product, or call get_product if you only wanted one product's details."
            ),
        )

    with db.connect() as connection:
        row_a = db.get_product(connection, checked_a)
        if row_a is None:
            return _missing_product(checked_a, "product_id_a")

        row_b = db.get_product(connection, checked_b)
        if row_b is None:
            return _missing_product(checked_b, "product_id_b")

        labels_a = {f["label"] for f in db.get_features(connection, checked_a)}
        labels_b = {f["label"] for f in db.get_features(connection, checked_b)}

    summary_a = _summary(row_a)
    summary_b = _summary(row_b)

    shared = sorted(labels_a & labels_b)
    only_in_a = sorted(labels_a - labels_b)
    only_in_b = sorted(labels_b - labels_a)

    price_delta = None
    if row_a["price_usd"] is not None and row_b["price_usd"] is not None:
        price_delta = round(row_b["price_usd"] - row_a["price_usd"], 2)

    return ComparisonResult(
        product_a=summary_a,
        product_b=summary_b,
        shared_features=shared,
        only_in_a=only_in_a,
        only_in_b=only_in_b,
        price_delta_usd=price_delta,
        summary=_comparison_sentence(summary_a, summary_b, shared, only_in_a, only_in_b, price_delta),
    )


def _comparison_sentence(
    product_a: ProductSummary,
    product_b: ProductSummary,
    shared: list[str],
    only_in_a: list[str],
    only_in_b: list[str],
    price_delta: float | None,
) -> str:
    """Compose the one-sentence headline, deterministically."""
    if price_delta is None:
        price_clause = "has no comparable published price against"
    elif price_delta > 0:
        price_clause = f"costs ${price_delta:.2f} more than"
    elif price_delta < 0:
        price_clause = f"costs ${abs(price_delta):.2f} less than"
    else:
        price_clause = "costs the same as"

    return (
        f"{product_b.name} ({product_b.competitor}) {price_clause} "
        f"{product_a.name} ({product_a.competitor}); they share {len(shared)} features, "
        f"{product_b.name} adds {len(only_in_b)} and lacks {len(only_in_a)}."
    )


@mcp.tool()
def find_feature_gaps(
    our_product_id: Annotated[
        str,
        Field(
            description=(
                "Catalog id of one of OUR products -- it must belong to the competitor named "
                "'Us'. Find it with search_products(query=..., competitor='Us'). Passing a "
                "competitor's product id is rejected."
            )
        ),
    ],
    competitor: Annotated[
        str | None,
        Field(
            description=(
                "Restrict the comparison to one competitor by name or id, e.g. 'Northline'. "
                "Omit to compare against every competitor, which is the usual case."
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of gaps to return, between 1 and 20. Defaults to 5. "
                "Out-of-range values are rejected rather than clamped."
            )
        ),
    ] = 5,
) -> FeatureGapReport | ToolError:
    """Find features competitors offer on a product of ours that we do not.

    This is the tool to reach for when the user asks what we are missing, where
    we are behind, or what to add next. It takes one of OUR products -- from
    the competitor named 'Us' -- and reports features that rival products in
    the same category advertise and ours does not.

    Comparison is scoped to the product's own category, because a feature is
    only a gap against products a buyer would actually cross-shop. Gaps are
    ordered by how many distinct competitors ship the feature, most first: a
    feature five competitors carry is a far stronger signal than one a single
    competitor carries. Ties are broken alphabetically, so the ordering is
    stable across calls. `total_gaps_found` counts every gap before the limit;
    zero means we match or exceed every competitor feature in the category.
    """
    return guarded(
        "find_feature_gaps",
        lambda: _find_feature_gaps_impl(our_product_id, competitor, limit),
    )


def _find_feature_gaps_impl(
    our_product_id: str, competitor: str | None, limit: int
) -> FeatureGapReport | ToolError:
    checked_id = _validate_product_id(our_product_id, "our_product_id")
    if isinstance(checked_id, ToolError):
        return checked_id

    checked_limit = _validate_limit(limit, 1, 20)
    if isinstance(checked_limit, ToolError):
        return checked_limit

    competitor_filter = _clean_optional(competitor)

    with db.connect() as connection:
        row = db.get_product(connection, checked_id)
        if row is None:
            return _missing_product(checked_id, "our_product_id")

        us = db.us_competitor(connection)
        us_name = us["name"] if us is not None else "Us"

        if not row["is_us"]:
            return wrong_owner(
                f"Product '{checked_id}' ({row['name']}) belongs to {row['competitor_name']}, "
                f"not to our own product line. find_feature_gaps reports what competitors have "
                f"that we lack, so a competitor's product cannot be the subject.",
                hint=(
                    f"Call search_products(query='{row['category']}', competitor='{us_name}') to "
                    f"list our products in that category, then pass one of those ids as "
                    f"our_product_id. To compare two specific products in either direction, use "
                    f"compare_products instead."
                ),
            )

        competitor_id = None
        if competitor_filter is not None:
            resolved = _resolve_competitor(connection, competitor_filter)
            if isinstance(resolved, ToolError):
                return resolved
            if resolved["is_us"]:
                return invalid_input(
                    f"The competitor filter '{competitor_filter}' resolves to {us_name}, our own "
                    f"product line, so there is nothing to compare against.",
                    hint=(
                        "Omit the competitor filter to compare against every competitor, or name "
                        "a rival company."
                    ),
                )
            competitor_id = resolved["id"]

        our_labels = {f["label"] for f in db.get_features(connection, checked_id)}
        rivals = db.products_in_category(
            connection,
            category=row["category"],
            exclude_competitor_id=row["competitor_id"],
            competitor_id=competitor_id,
        )
        rival_by_id = {rival["id"]: rival for rival in rivals}
        rival_features = db.features_for_products(connection, list(rival_by_id))

    # Rows arrive ordered by product id, so the first product to mention a
    # feature becomes its example and the choice is stable across calls.
    offered_by: dict[str, set[str]] = {}
    example_for: dict[str, str] = {}
    for feature_row in rival_features:
        label = feature_row["label"]
        if label in our_labels:
            continue
        rival = rival_by_id[feature_row["product_id"]]
        offered_by.setdefault(label, set()).add(rival["competitor_name"])
        example_for.setdefault(label, rival["id"])

    ordered = sorted(offered_by.items(), key=lambda item: (-len(item[1]), item[0]))

    return FeatureGapReport(
        our_product=_summary(row),
        gaps=[
            FeatureGap(
                feature_label=label,
                offered_by=sorted(competitors),
                competitor_count=len(competitors),
                example_product_id=example_for[label],
            )
            for label, competitors in ordered[:checked_limit]
        ],
        total_gaps_found=len(ordered),
    )


def _configure_logging() -> None:
    """Send all logging to stderr.

    Under the stdio transport, stdout *is* the wire protocol -- a stray byte
    there corrupts the session. Without this call Python's last-resort handler
    would already write to stderr, so importing this module is safe either way;
    this makes the guarantee explicit and lowers the level to INFO.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    """Run the server over stdio."""
    _configure_logging()
    logger.info("Starting catalog MCP server (database: %s)", db.database_path())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
