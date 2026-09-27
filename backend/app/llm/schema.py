"""Pydantic მოდელი -> "strict" JSON Schema, რომელსაც სამივე პროვაიდერი იღებს.

სამივე API-ს structured output-ს აქვს JSON Schema-ს საკუთარი ქვესიმრავლე. ყველაზე მკაცრია
OpenAI strict mode: ყველა ობიექტს სჭირდება additionalProperties=false და ყველა ველი
required-ში (არასავალდებულო ველი = nullable). ამ ფორმას Claude და Gemini-ც იღებს, ამიტომ
ვაგენერირებთ ერთ, "უმცირესი საერთო მნიშვნელის" სქემას:

* $ref/$defs იხსნება (inline) - რეკურსიული მოდელები აკრძალულია;
* ამოიღება title/default/examples და ფორმატ-ვალიდატორები, რომლებსაც ყველა არ უჭერს მხარს;
* ყველა property -> required.

საბოლოო ჭეშმარიტების წყარო მაინც Pydantic ვალიდაციაა (router.py) - სქემა მხოლოდ მიმართავს მოდელს.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_DROP_KEYS = {"title", "default", "examples", "discriminator"}
# რიცხვით/სტრიქონულ შეზღუდვებს ვტოვებთ description-ში, ვალიდაციას კი Pydantic აკეთებს.
_CONSTRAINT_KEYS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "format",
}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema(mode="validation")
    defs = raw.pop("$defs", {})
    return _normalize(raw, defs, stack=())


def _normalize(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_normalize(item, defs, stack) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        if name in stack:
            raise ValueError(f"recursive schema {name!r} is not supported by all providers")
        merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
        return _normalize(merged, defs, stack + (name,))

    out: dict[str, Any] = {}
    hints: list[str] = []
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key in _CONSTRAINT_KEYS:
            hints.append(f"{key}={value}")
            continue
        if key == "properties":
            out[key] = {name: _normalize(sub, defs, stack) for name, sub in value.items()}
        elif key in ("items", "anyOf", "allOf", "oneOf", "prefixItems"):
            out[key] = _normalize(value, defs, stack)
        else:
            out[key] = value

    # allOf ერთი ელემენტით (Pydantic ასე აგენერირებს ref+description) -> გავაბრტყელოთ
    if "allOf" in out and len(out["allOf"]) == 1:
        inner = out.pop("allOf")[0]
        out = {**inner, **out}
    # oneOf -> anyOf (ფართოდ მხარდაჭერილი)
    if "oneOf" in out:
        out["anyOf"] = out.pop("oneOf")

    if hints:
        desc = out.get("description", "")
        out["description"] = (desc + " " if desc else "") + "(" + ", ".join(hints) + ")"

    if out.get("type") == "object" or "properties" in out:
        props = out.setdefault("properties", {})
        out["type"] = "object"
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    return out
