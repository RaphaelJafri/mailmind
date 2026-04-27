"""JSON Schema validation helper.

Wraps `jsonschema` so callers get a single typed exception with the error
list flattened. We use draft-07 schemas (matching `schemas/*.schema.json`).
"""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema import Draft7Validator

from . import prompts


class SchemaValidationError(Exception):
    def __init__(self, errors: list[str], *, schema_name: str):
        super().__init__(f"{schema_name}: {len(errors)} validation error(s):\n" + "\n".join(errors))
        self.errors = errors
        self.schema_name = schema_name


def validate(payload: Any, schema_name: str) -> None:
    """Raise `SchemaValidationError` with all errors collected, not just the first."""
    schema = prompts.load_schema(schema_name)
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.absolute_path)
    if errors:
        msgs = [f"  at /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors]
        raise SchemaValidationError(msgs, schema_name=schema_name)


def is_valid(payload: Any, schema_name: str) -> bool:
    try:
        validate(payload, schema_name)
        return True
    except (SchemaValidationError, jsonschema.SchemaError):
        return False
