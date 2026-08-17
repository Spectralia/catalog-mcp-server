"""The tool surface as the model sees it.

Descriptions are not documentation here -- they are the entire interface. A
tool registered without one, or a parameter whose JSON Schema property has no
`description`, is a tool the model has to guess at. These tests fail the build
when that happens, which is the only reliable way to keep it from happening.
"""

import asyncio

import pytest

from catalog.server import mcp

EXPECTED_TOOLS = {"search_products", "get_product", "compare_products", "find_feature_gaps"}

# Emitted by the SDK when a tool's return type is a union or a non-object; it
# wraps the real schema and carries no description of its own to check.
SDK_RESULT_WRAPPER = "result"


@pytest.fixture(scope="module")
def tools() -> dict:
    """Every registered tool, keyed by name, as the client would list them."""
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def described_properties(schema: dict) -> dict[str, dict]:
    """Every property of a schema and of its nested definitions.

    Keys are qualified by the definition they came from, so two models that
    both declare a `name` field cannot mask each other.
    """
    found = dict(schema.get("properties", {}))
    for model, definition in schema.get("$defs", {}).items():
        for name, spec in definition.get("properties", {}).items():
            found[f"{model}.{name}"] = spec
    return found


def test_exactly_the_expected_tools_are_registered(tools):
    assert set(tools) == EXPECTED_TOOLS


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_every_tool_has_a_substantial_description(tools, tool_name):
    description = tools[tool_name].description

    assert description, f"{tool_name} has no description"
    # The docstring must say when to reach for the tool, not just name it; a
    # one-liner is a sign that guidance was dropped.
    assert len(description.split()) >= 30, f"{tool_name} description is too thin to guide a model"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_every_input_property_carries_a_description(tools, tool_name):
    schema = tools[tool_name].input_schema

    assert schema["type"] == "object"
    properties = described_properties(schema)
    assert properties, f"{tool_name} declares no inputs"

    undescribed = [name for name, spec in properties.items() if not spec.get("description")]
    assert not undescribed, f"{tool_name} input properties without a description: {undescribed}"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_every_output_property_carries_a_description(tools, tool_name):
    schema = tools[tool_name].output_schema

    assert schema is not None, f"{tool_name} declares no output schema"
    undescribed = [
        name
        for name, spec in described_properties(schema).items()
        if name != SDK_RESULT_WRAPPER and not spec.get("description")
    ]
    assert not undescribed, f"{tool_name} output properties without a description: {undescribed}"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_every_tool_can_return_a_tool_error(tools, tool_name):
    # Every tool's declared return type is `<Result> | ToolError`, so ToolError
    # has to appear in the advertised output schema -- otherwise a client would
    # be right to treat an error payload as a protocol violation.
    assert "ToolError" in str(tools[tool_name].output_schema)


def test_the_server_advertises_usage_instructions():
    assert mcp.instructions
    assert "search_products" in mcp.instructions
