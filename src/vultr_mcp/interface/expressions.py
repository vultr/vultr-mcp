"""The closed grammar computed fields are written in.

Computed fields exist because the agent often wants something the API does not
return -- "how many instances does this cluster have" is a count of an array it
would otherwise have to fetch in full and tally itself. The derivation is an
expression in the YAML rather than a Python helper, because a model drafting a
product area file can write ``length(instances)`` but cannot write and wire up a
helper function. The grammar is tiny and closed so that anything invented fails
the build instead of failing at runtime.

Parsing and evaluation live together here so the validator and the runtime can
never disagree about what an expression means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

EXPRESSION_FUNCTIONS = frozenset({"length", "sum", "any", "coalesce"})

_CALL = re.compile(r"^(?P<fn>[a-z]+)\((?P<args>[^)]*)\)$")
_PATH = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


@dataclass(frozen=True)
class Expression:
    """A parsed ``from:`` expression: at most one function over dotted paths."""

    source: str
    function: str | None
    paths: tuple[tuple[str, ...], ...]

    @property
    def roots(self) -> tuple[str, ...]:
        """First segment of each path -- what the validator checks exists."""
        return tuple(path[0] for path in self.paths)


def parse(source: str) -> tuple[Expression | None, str | None]:
    """Parse an expression, returning (expression, error). Exactly one is None."""
    source = source.strip()
    call = _CALL.match(source)

    if not call:
        if not _PATH.match(source):
            return None, f"'{source}' is not a dotted path or a supported call"
        return Expression(source, None, (tuple(source.split(".")),)), None

    function = call.group("fn")
    if function not in EXPRESSION_FUNCTIONS:
        allowed = ", ".join(sorted(EXPRESSION_FUNCTIONS))
        return None, f"unknown function '{function}' (allowed: {allowed})"

    raw_args = [arg.strip() for arg in call.group("args").split(",") if arg.strip()]
    if not raw_args:
        return None, f"'{function}()' needs at least one argument"
    for arg in raw_args:
        if not _PATH.match(arg):
            return None, f"'{arg}' is not a dotted path"
    if function != "coalesce" and len(raw_args) > 1:
        return None, f"'{function}()' takes exactly one argument"

    paths = tuple(tuple(arg.split(".")) for arg in raw_args)
    return Expression(source, function, paths), None


def read_path(item: Any, path: tuple[str, ...]) -> Any:
    """Read a dotted path, mapping over lists encountered along the way.

    ``sum(instances.vcpu_count)`` therefore means "the vcpu_count of every
    instance", which is the only reading that makes the function useful.
    """
    current: Any = item
    for index, segment in enumerate(path):
        if isinstance(current, list):
            remainder = path[index:]
            collected = [read_path(element, remainder) for element in current]
            return [value for value in collected if value is not None]
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current


def _flatten(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [inner for element in value for inner in _flatten(element)]
    return [value]


def evaluate(expression: Expression, item: Any) -> Any:
    """Evaluate a parsed expression against one result item.

    Returns None when the data the expression reads is absent, which the caller
    treats as "omit the field" rather than "report zero".
    """
    values = [read_path(item, path) for path in expression.paths]
    first = values[0]

    if expression.function is None:
        return first

    if expression.function == "length":
        if first is None:
            return None
        if isinstance(first, (list, dict, str)):
            return len(first)
        return 1

    if expression.function == "sum":
        numbers = [value for value in _flatten(first) if isinstance(value, (int, float))]
        if not numbers:
            return None
        total = sum(numbers)
        return int(total) if all(isinstance(n, int) for n in numbers) else total

    if expression.function == "any":
        if first is None:
            return None
        return any(bool(value) for value in _flatten(first))

    # coalesce: the first argument that resolved to something.
    for value in values:
        if value is not None and value != "" and value != []:
            return value
    return None
