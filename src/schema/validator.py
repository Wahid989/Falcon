"""FALCON message validator (FALCON-019).

Validates decoded messages against the v1 JSON Schema
(`src/schema/falcon-v1.json`). The schema is loaded and compiled once at module
import for performance; the same :func:`validate` is used on both the send and
receive sides.

    from src.schema.validator import validate

    ok, errors = validate(message)   # message is a decoded dict, not JSON text
    if not ok:
        ...  # errors is a list of human-readable strings naming the bad field

Parsing JSON text into a dict (and handling malformed bytes) is the caller's
job at the framing layer; this module validates structure and vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

SCHEMA_PATH = Path(__file__).with_name("falcon-v1.json")


def _build_validator() -> Draft202012Validator:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


# Compiled once at import so repeated validation is cheap.
_VALIDATOR = _build_validator()


def _location(error: ValidationError) -> str:
    """Dotted path to the offending field, e.g. ``payload.command``."""
    return ".".join(str(part) for part in error.absolute_path)


def _format(error: ValidationError) -> str:
    """Render one validation error as ``<field>: <message>`` when a field is known."""
    location = _location(error)
    return f"{location}: {error.message}" if location else error.message


def validate(message: Any) -> tuple[bool, list[str]]:
    """Validate a decoded FALCON message against the v1 schema.

    Returns ``(ok, errors)`` where ``ok`` is ``True`` and ``errors`` is empty for
    a valid message, and ``ok`` is ``False`` with one or more error strings
    (each naming the failing field where one applies) otherwise.
    """
    errors = sorted(
        _VALIDATOR.iter_errors(message),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    return (not errors, [_format(error) for error in errors])


def is_valid(message: Any) -> bool:
    """Convenience boolean form of :func:`validate`."""
    return _VALIDATOR.is_valid(message)
