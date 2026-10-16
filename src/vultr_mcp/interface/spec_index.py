"""The parts of openapi.json the interface layer needs, resolved once for the validator and compiler.

Path parameters are often declared on the path item rather than the operation, and the container
key holding a response's results is derived from its schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Methods that change state: everything but GET, the same rule server.py applies (OPTIONS mints registry credentials).
WRITE_METHODS = frozenset({"post", "put", "patch", "delete", "options", "head", "trace"})
HTTP_METHODS = WRITE_METHODS | {"get"}

# Depth cap on $ref chains, so a cyclic spec cannot hang the build.
_MAX_REF_DEPTH = 20


def resolve(node: Any, spec: dict[str, Any], _depth: int = 0) -> dict[str, Any]:
    """Follow $ref chains to the object they name, or {}; merges allOf and infers a missing type."""
    while isinstance(node, dict) and "$ref" in node and _depth < _MAX_REF_DEPTH:
        target: Any = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            if not isinstance(target, dict) or part not in target:
                return {}
            target = target[part]
        node = target
        _depth += 1
    if not isinstance(node, dict):
        return {}
    if "allOf" in node and _depth < _MAX_REF_DEPTH:
        merged = {key: value for key, value in node.items() if key != "allOf"}
        properties = dict(merged.get("properties", {}))
        for part in node["allOf"]:
            part = resolve(part, spec, _depth + 1)
            properties.update(part.get("properties", {}))
            for key, value in part.items():
                if key != "properties":
                    merged.setdefault(key, value)
        if properties:
            merged["properties"] = properties
        node = merged
    if "type" not in node:
        if "items" in node:
            node = {**node, "type": "array"}
        elif "properties" in node:
            node = {**node, "type": "object"}
    return node


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
    """The 200 response, split into envelope and item levels; ``unwrapped`` when the payload is the item itself."""

    envelope: frozenset[str]
    item_fields: frozenset[str]
    container_key: str | None
    is_collection: bool
    extra_containers: tuple[str, ...] = ()
    unwrapped: bool = False

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
        """Whether the spec marks this operation deprecated."""
        return bool(self.raw.get("deprecated"))

    @property
    def parameters(self) -> dict[str, Parameter]:
        """Parameters the operation accepts, path-item level included; operation-level entries win."""
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

        # A wrapper holds nothing but what it wraps; a response with scalars of its own is the resource.
        scalars = [
            key
            for key, value in properties.items()
            if key != "meta"
            and not resolve(value, self.spec).get("properties")
            and resolve(value, self.spec).get("type") not in ("array", "object")
        ]

        # A list wraps one array beside `meta`; a get-by-id wraps one object under one key.
        if arrays:
            container, extras, is_collection = arrays[0], tuple(arrays[1:]), True
        elif objects and not scalars:
            container, extras, is_collection = objects[0], tuple(objects[1:]), False
        else:
            container, extras, is_collection = None, (), False

        item_fields: frozenset[str] = frozenset()
        unwrapped = False
        if container is not None:
            node = resolve(properties[container], self.spec)
            if is_collection:
                node = resolve(node.get("items", {}), self.spec)
            item_fields = frozenset(node.get("properties", {}))
        elif properties:
            # Properties of its own and nothing nested: the response is the resource.
            item_fields = frozenset(properties)
            unwrapped = True

        return ResponseShape(
            envelope=envelope,
            item_fields=item_fields,
            container_key=container,
            is_collection=is_collection,
            extra_containers=extras,
            unwrapped=unwrapped,
        )


@dataclass
class SpecIndex:
    """Every operation in the spec, keyed by operationId."""

    operations: dict[str, Operation] = field(default_factory=dict)
    # Every operation again, keyed "GET /path", so one the spec gave no operationId can still be named.
    routes: dict[str, Operation] = field(default_factory=dict)

    @classmethod
    def load(cls, spec: dict[str, Any]) -> "SpecIndex":
        index = cls()
        for path, path_item in spec.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, op in path_item.items():
                if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                    continue
                route = f"{method.upper()} {path}"
                operation = Operation(
                    operation_id=op.get("operationId") or route,
                    path=path,
                    method=method.lower(),
                    tags=tuple(op.get("tags") or ()),
                    raw=op,
                    spec=spec,
                    path_item=path_item,
                )
                index.routes[route] = operation
                if "operationId" in op:
                    index.operations[op["operationId"]] = operation
        return index

    def get(self, operation_id: str) -> Operation | None:
        return self.operations.get(operation_id)

    def resolve(self, key: str) -> Operation | None:
        """An operation by operationId, or by "METHOD /path" for one the spec left unnamed."""
        return self.operations.get(key) or self.routes.get(key)
