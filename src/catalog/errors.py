"""Structured errors returned to the model instead of raised at it.

A tool that raises hands the model a protocol-level failure with nothing in it
to act on. Every failure path in this server returns a `ToolError` instead: a
machine-checkable `code`, a message naming the offending value, and a `hint`
saying what to call next. The model can then recover on its own turn.
"""

import logging
from collections.abc import Callable
from enum import Enum
from typing import Literal, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ResultT = TypeVar("ResultT")

# Repeated in hints whenever the database is missing, so the model can tell the
# user exactly one command rather than guessing at a setup step.
SEED_COMMAND = "uv run python -m catalog.seed"


class CatalogUnavailableError(Exception):
    """The catalog database is missing or unreadable.

    Defined here rather than in `db.py` so the db layer stays a leaf module and
    the tool layer has a single place to import failure vocabulary from.
    """


class ErrorCode(str, Enum):
    """Stable, machine-checkable failure categories.

    The distinction the model relies on: `INVALID_INPUT` means the argument was
    the wrong shape or out of range and should be reformatted; `NOT_FOUND`
    means the argument was well formed but names nothing in the catalog.
    """

    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS_INPUT = "AMBIGUOUS_INPUT"
    WRONG_OWNER = "WRONG_OWNER"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class ToolError(BaseModel):
    """A failed tool call, returned as data rather than raised.

    Every tool in this server returns either its result model or this. Check
    the `error` discriminator first: if it is present and true, read `code` to
    decide whether to retry, and `hint` for the corrective call to make.
    """

    error: Literal[True] = Field(
        default=True,
        description="Always true. Discriminates a failure from a successful result.",
    )
    code: ErrorCode = Field(
        description=(
            "Failure category. INVALID_INPUT: the argument was malformed or out of range, "
            "fix it and retry. NOT_FOUND: the argument was well formed but matches nothing. "
            "AMBIGUOUS_INPUT: the argument matched more than one record, be more specific. "
            "WRONG_OWNER: the product belongs to the wrong side of the comparison. "
            "DATA_UNAVAILABLE: the catalog could not be read, retrying will not help."
        )
    )
    message: str = Field(
        description="What went wrong, naming the specific value that caused it."
    )
    hint: str | None = Field(
        default=None,
        description="The corrective action to take next, usually another tool call.",
    )


def invalid_input(message: str, hint: str | None = None) -> ToolError:
    """Build an INVALID_INPUT error: the argument was malformed or out of range."""
    return ToolError(code=ErrorCode.INVALID_INPUT, message=message, hint=hint)


def not_found(message: str, hint: str | None = None) -> ToolError:
    """Build a NOT_FOUND error: the argument was well formed but matches nothing."""
    return ToolError(code=ErrorCode.NOT_FOUND, message=message, hint=hint)


def ambiguous_input(message: str, hint: str | None = None) -> ToolError:
    """Build an AMBIGUOUS_INPUT error: the argument matched several records."""
    return ToolError(code=ErrorCode.AMBIGUOUS_INPUT, message=message, hint=hint)


def wrong_owner(message: str, hint: str | None = None) -> ToolError:
    """Build a WRONG_OWNER error: the product belongs to the wrong party."""
    return ToolError(code=ErrorCode.WRONG_OWNER, message=message, hint=hint)


def data_unavailable(message: str, hint: str | None = None) -> ToolError:
    """Build a DATA_UNAVAILABLE error: the catalog itself could not be read."""
    return ToolError(code=ErrorCode.DATA_UNAVAILABLE, message=message, hint=hint)


def guarded(tool_name: str, run: Callable[[], ResultT]) -> ResultT | ToolError:
    """Run a tool body so that no exception can escape to the transport.

    Anything unexpected is logged to stderr with its traceback -- for the
    operator -- and reduced to a generic DATA_UNAVAILABLE for the model, which
    has no use for our stack frames and should not see internal detail.
    """
    try:
        return run()
    except CatalogUnavailableError as exc:
        return data_unavailable(
            str(exc),
            hint=f"Build the catalog database first: {SEED_COMMAND}",
        )
    except Exception:
        logger.exception("Unhandled error in tool %s", tool_name)
        return data_unavailable(
            f"The {tool_name} tool failed while reading the catalog.",
            hint=(
                "This is a server-side fault, not a bad argument. Retrying the same call "
                "will not help; tell the user the catalog server needs attention."
            ),
        )
