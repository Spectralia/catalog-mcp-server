"""Input and output schemas for the catalog tools.

Every field carries a `Field(description=...)`. These descriptions are not
documentation for maintainers -- they are emitted into the JSON Schema the
model reads before it calls a tool, and they are the only thing telling it what
`total_matched` means or which direction `price_delta_usd` runs.

The domain lives here and in `data/seed_catalog.json`. Retargeting this server
at a different vertical means changing those two files and the tool docstrings.
"""

from pydantic import BaseModel, Field


class ProductSummary(BaseModel):
    """A product reduced to the fields worth spending context on."""

    product_id: str = Field(
        description="Stable catalog id, e.g. 'prd_0142'. Pass this to get_product or compare_products."
    )
    name: str = Field(description="Product name as the competitor lists it.")
    competitor: str = Field(
        description="Name of the company selling it. 'Us' means our own product line."
    )
    category: str = Field(
        description="Product category, e.g. 'Decking'. Categories are exact strings; "
        "use one from a search result when filtering."
    )
    price_usd: float | None = Field(
        description="Observed list price in USD, in the unit the category is sold by "
        "(per linear foot, per square foot, per roofing square, or each). "
        "Null when no price was published."
    )
    last_seen_at: str = Field(
        description="ISO-8601 date the listing was last observed. Older dates mean staler data."
    )


class Feature(BaseModel):
    """One capability claimed for a product."""

    name: str = Field(
        description="Normalized feature key, e.g. 'moisture_resistant'. Comparable across competitors."
    )
    label: str = Field(
        description="Human-readable feature name, e.g. 'Moisture resistant'. Use this when writing to the user."
    )
    value: str | None = Field(
        description="Optional qualifier for the claim, e.g. 'Class A' or 'R-6.5'. Null when the feature is a plain yes."
    )


class ChangeEntry(BaseModel):
    """One observed change to a product listing."""

    observed_at: str = Field(description="ISO-8601 date the change was observed.")
    change_type: str = Field(
        description="One of 'added', 'removed', 'price_changed', 'feature_added'."
    )
    detail: str = Field(description="Plain-language description of what changed.")


class ProductDetail(ProductSummary):
    """One product with its full feature list and recent change history."""

    url: str | None = Field(description="Source listing URL, or null if none was recorded.")
    description: str | None = Field(
        description="Short marketing description as published by the competitor."
    )
    features: list[Feature] = Field(
        description="Every feature recorded for this product, ordered by label."
    )
    recent_changes: list[ChangeEntry] = Field(
        description="The five most recent observed changes, newest first. Empty if nothing has changed."
    )


class SearchResult(BaseModel):
    """The result of a catalog search."""

    query_echo: str = Field(
        description="The query string as it was actually applied, after trimming whitespace."
    )
    total_matched: int = Field(
        description="How many products matched in total, before the limit was applied. "
        "If this exceeds 'returned', narrow the query or raise the limit rather than assuming you have seen everything."
    )
    returned: int = Field(description="How many products are in 'results'.")
    results: list[ProductSummary] = Field(
        description="Matching products, ordered by name. Empty when nothing matched, which is not an error."
    )


class ComparisonResult(BaseModel):
    """A feature-level diff between two products."""

    product_a: ProductSummary = Field(description="The product passed as product_id_a.")
    product_b: ProductSummary = Field(description="The product passed as product_id_b.")
    shared_features: list[str] = Field(
        description="Feature labels present on both products, alphabetically ordered."
    )
    only_in_a: list[str] = Field(
        description="Feature labels on product A that product B lacks, alphabetically ordered."
    )
    only_in_b: list[str] = Field(
        description="Feature labels on product B that product A lacks, alphabetically ordered."
    )
    price_delta_usd: float | None = Field(
        description="Price of B minus price of A. Positive means B is more expensive. "
        "Null when either product has no published price, in which case do not infer a direction."
    )
    summary: str = Field(
        description="One sentence stating the headline difference, safe to quote to the user."
    )


class FeatureGap(BaseModel):
    """One feature competitors ship that our product does not."""

    feature_label: str = Field(
        description="Human-readable name of the missing feature, e.g. 'Slip resistant'."
    )
    offered_by: list[str] = Field(
        description="Names of the competitors offering it, alphabetically ordered."
    )
    competitor_count: int = Field(
        description="How many distinct competitors offer it. Higher means a stronger market signal."
    )
    example_product_id: str = Field(
        description="A competitor product id that has this feature. Pass it to get_product to see how they position it."
    )


class FeatureGapReport(BaseModel):
    """Features competitors offer in a category that our product does not."""

    our_product: ProductSummary = Field(
        description="The product from our own line that the gaps were computed against."
    )
    gaps: list[FeatureGap] = Field(
        description="Missing features, most widely adopted first, ties broken alphabetically. "
        "Truncated to the requested limit."
    )
    total_gaps_found: int = Field(
        description="How many distinct gaps exist in total, before the limit was applied. "
        "Zero means our product matches or exceeds every competitor feature in its category."
    )
