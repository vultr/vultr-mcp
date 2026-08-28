"""The parts of openapi.json the interface layer needs, resolved once.

The validator and the compiler ask the same questions of the spec -- what
parameters does this operation take, what does its 200 response look like, does
it change state -- so they ask them here rather than each growing its own copy.

Two things this deliberately gets right that a naive reading of the spec does
not:

* **Path-item parameters.** 234 of Vultr's 330 paths declare their path
  parameters on the path item, not on the operation. Reading only
  ``operation.parameters`` loses ``{pullzone-id}`` and friends, which would make
  every by-id tool look like it was referencing a parameter that doesn't exist.
* **The container key.** A list response is an envelope -- ``{"clusters": [...],
  "meta": {...}}`` -- so shaping has to know which key holds the results. That
  key is derived from the response schema, never hard-coded per tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Methods that change state. `access` must agree with the operation's method, so
# a write cannot be mislabelled `read` and slip past the read-only gate.
WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})

# Depth cap on $ref chains, so a cyclic spec cannot hang the build.
_MAX_REF_DEPTH = 20


def resolve(node: Any, spec: dict[str, Any], _depth: int = 0) -> dict[str, Any]:
    """Follow $ref chains to the object they name, or {} if unresolvable."""
    while isinstance(node, dict) and "$ref" in node and _depth < _MAX_REF_DEPTH:
        target: Any = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            if not isinstance(target, dict) or part not in target:
                return {}
            target = target[part]
        node = target
        _depth += 1
    return node if isinstance(node, dict) else {}


@dataclass(frozen=True)
class Parameter:
    """One API parameter, with the location the runtime needs to place it."""

    name: str
    location: str  # "query" | "path" | "header" | "cookie"
    required: bool
    schema: dict[str, Any]
    description: str = ""


@dataclass(frozen=True)
class ResponseShape:
    """The 200 response of an operation, split into the levels tools reference.

    ``output.include`` names fields at both levels -- ``meta`` sits on the
    envelope, ``label`` sits on each result item -- so both are kept.
    """

    envelope: frozenset[str]
    item_fields: frozenset[str]
    container_key: str | None
    is_collection: bool
    extra_containers: tuple[str, ...] = ()

    @property
    def known_fields(self) -> frozenset[str]:
        return self.envelope | self.item_fields


@dataclass
class Operation:
    """One openapi operation, resolved enough to compile and validate against."""

    operation_id: str
    path: str
    method: str
    tags: tuple[str, ...]
    raw: dict[str, Any]
    spec: dict[str, Any]
    path_item: dict[str, Any]

    _parameters: dict[str, Parameter] | None = field(default=None, repr=False)
    _response: ResponseShape | None = field(default=None, repr=False)

    @property
    def is_write(self) -> bool:
        return self.method in WRITE_METHODS

    @property
    def has_request_body(self) -> bool:
        return "requestBody" in self.raw

    @property
    def is_deprecated(self) -> bool:
        """Whether the spec marks this operation deprecated.

        Vultr flags all 23 of them with the OpenAPI field rather than only in
        prose, so this is reliable enough to act on.
        """
        return bool(self.raw.get("deprecated"))

    @property
    def parameters(self) -> dict[str, Parameter]:
        """Parameters the operation accepts, path-item level included.

        Operation-level entries win on a name collision, which is what the
        OpenAPI spec prescribes.
        """
        if self._parameters is None:
            resolved: dict[str, Parameter] = {}
            for source in (
                self.path_item.get("parameters", []),
                self.raw.get("parameters", []),
            ):
                for entry in source:
                    entry = resolve(entry, self.spec)
                    name = entry.get("name")
                    if not name:
                        continue
                    location = entry.get("in", "query")
                    resolved[name] = Parameter(
                        name=name,
                        location=location,
                        required=bool(entry.get("required", location == "path")),
                        schema=resolve(entry.get("schema", {}), self.spec),
                        description=" ".join((entry.get("description") or "").split()),
                    )
            self._parameters = resolved
        return self._parameters

    @property
    def response(self) -> ResponseShape:
        """Shape of the 200 response."""
        if self._response is None:
            self._response = self._build_response()
        return self._response

    def _build_response(self) -> ResponseShape:
        body = resolve(self.raw.get("responses", {}).get("200", {}), self.spec)
        schema = resolve(
            body.get("content", {}).get("application/json", {}).get("schema", {}),
            self.spec,
        )
        properties = schema.get("properties", {})
        envelope = frozenset(properties)

        arrays: list[str] = []
        objects: list[str] = []
        for key, value in properties.items():
            value = resolve(value, self.spec)
            if value.get("type") == "array":
                arrays.append(key)
            elif key != "meta" and value.get("type") == "object":
                objects.append(key)

        # A list response is the common case: one array of results beside
        # `meta`. A get-by-id response wraps a single object under one key.
        if arrays:
            container, extras, is_collection = arrays[0], tuple(arrays[1:]), True
        elif objects:
            container, extras, is_collection = objects[0], tuple(objects[1:]), False
        else:
            container, extras, is_collection = None, (), False

        item_fields: frozenset[str] = frozenset()
        if container is not None:
            node = resolve(properties[container], self.spec)
            if is_collection:
                node = resolve(node.get("items", {}), self.spec)
            item_fields = frozenset(node.get("properties", {}))

        return ResponseShape(
            envelope=envelope,
            item_fields=item_fields,
            container_key=container,
            is_collection=is_collection,
            extra_containers=extras,
        )


@dataclass
class SpecIndex:
    """Every operation in the spec, keyed by operationId."""

    operations: dict[str, Operation] = field(default_factory=dict)

    @classmethod
    def load(cls, spec: dict[str, Any]) -> "SpecIndex":
        index = cls()
        for path, path_item in spec.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, op in path_item.items():
                if not isinstance(op, dict) or "operationId" not in op:
                    continue
                index.operations[op["operationId"]] = Operation(
                    operation_id=op["operationId"],
                    path=path,
                    method=method.lower(),
                    tags=tuple(op.get("tags") or ()),
                    raw=op,
                    spec=spec,
                    path_item=path_item,
                )
        return index

    def get(self, operation_id: str) -> Operation | None:
        return self.operations.get(operation_id)
