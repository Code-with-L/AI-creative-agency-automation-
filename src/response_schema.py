"""Convert pydantic models to Gemini's ``responseSchema`` subset.

Gemini's ``generationConfig.responseSchema`` enforces the output shape
server-side instead of relying on prompt instructions plus post-validation.
It accepts an OpenAPI 3.0 object/array/string subset, so this module does a
best-effort translation of a pydantic v2 JSON schema and **refuses loudly**
anything it cannot translate faithfully (dicts, ``anyOf`` unions other than
``Optional``), rather than emitting a schema Google would silently ignore.

Experimental: enabled with ``LLM_RESPONSE_SCHEMA=1`` and only applied to
requests that carry a ``response_model``. The default stays off until this
has been verified against a live entitlement.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Gemini's accepted primitive types.
_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
    "null": "null",
}


def _convert(node: dict[str, Any]) -> dict[str, Any]:
    # anyOf only ever appears for Optional[...] in our models: a single
    # non-null branch plus a null marker.
    any_of = node.get("anyOf")
    if any_of is not None:
        non_null = [item for item in any_of if item.get("type") != "null"]
        if len(non_null) != 1:
            raise ValueError(
                f"responseSchema supports Optional[X] only, got anyOf with {len(non_null)} non-null branches"
            )
        converted = _convert(non_null[0])
        converted["nullable"] = True
        return converted

    if "$ref" in node:
        raise ValueError("unresolved $ref reached the converter; pass $defs through first")

    node_type = node.get("type")
    if node_type == "object" or ("properties" in node and node_type is None):
        props = node.get("properties")
        if not isinstance(props, dict):
            raise ValueError("free-form objects (dict) are not supported")
        out: dict[str, Any] = {"type": "object", "properties": {}}
        for name, child in props.items():
            out["properties"][name] = _convert(child)
        required = node.get("required")
        if required:
            out["required"] = list(required)
        return out
    if node_type == "array":
        items = node.get("items")
        if not items:
            raise ValueError("responseSchema requires array item types")
        return {"type": "array", "items": _convert(items)}
    if node_type in _TYPE_MAP:
        out = {"type": node_type}
        if "enum" in node:
            out["enum"] = list(node["enum"])
        if "description" in node:
            out["description"] = node["description"]
        return out
    raise ValueError(f"unsupported schema node for responseSchema: {node!r}")


def to_gemini_response_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        if ref:
            name = ref.rsplit("/", 1)[-1]
            try:
                target = defs[name]
            except KeyError:
                raise ValueError(f"unresolvable $ref {ref!r} in {model.__name__} schema") from None
            return resolve(dict(target))

        node = dict(node)
        for container in ("anyOf", "allOf", "items"):
            if container not in node:
                continue
            if container in ("anyOf", "allOf"):
                node[container] = [resolve(dict(child)) for child in node[container]]
            else:
                node[container] = resolve(dict(node[container]))
        props = node.get("properties")
        if isinstance(props, dict):
            node["properties"] = {name: resolve(dict(child)) for name, child in props.items()}
        return node

    try:
        return _convert(resolve(schema))
    except ValueError as exc:
        raise ValueError(f"cannot build responseSchema for {model.__name__}: {exc}") from exc
