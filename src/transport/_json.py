"""Strict JSON snapshot helper shared by transport backends."""

from math import isfinite

from .base import JSONValue, TransportMessageError


def _copy_json(value: JSONValue, ancestors: set[int]) -> JSONValue:
    """Copy strict JSON values without coercing keys, tuples, or nonfinite floats."""
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and isfinite(value):
        return value
    if type(value) not in (dict, list):
        raise TransportMessageError("Message contains a non-JSON value")
    identity = id(value)
    if identity in ancestors:
        raise TransportMessageError("Message contains a circular reference")
    ancestors.add(identity)
    try:
        if type(value) is list:
            return [_copy_json(item, ancestors) for item in value]
        if any(type(key) is not str for key in value):
            raise TransportMessageError("JSON object keys must be strings")
        return {key: _copy_json(item, ancestors) for key, item in value.items()}
    finally:
        ancestors.remove(identity)
