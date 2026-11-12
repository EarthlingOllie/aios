"""Structured workflows: JSON Schemas on versions and nodes, input templates, and conditions on
edges.

**Schemas** are JSON Schema draft 2020-12, checked when saved (`check_schema`) and applied with
the pinned validator (`schema_errors`). A run's input is checked against its version's input
schema however it starts; an agent node with an output schema gives structured output, checked
against it; a node's input is checked against its input schema; the run's output against the
version's output schema.

**Templates** build a node's input from placeholders only, never expressions: `{{input}}` (the
run's input), `{{input.a.b}}` (a field of it), `{{nodes.<key>}}` (a predecessor's whole output)
and `{{nodes.<key>.a.b}}` (a field of it). A template is any JSON value: a string that is exactly
one placeholder becomes that value; other strings have their placeholders written in (a value
that isn't a string as compact JSON). Saving refuses references to nodes that aren't
predecessors (`template_errors`); a field missing at run time fails the step, naming the path
(`TemplateMissing`).

**Conditions** are JSONLogic, a fixed subset evaluated here (`evaluate`) rather than by a
third-party package: nothing anyone writes is ever executed. They read `output` (the edge's
source's output) and `input` (the run's input). Saving checks they parse, use only allowed
operators, are within the size cap, and read only fields the declared schemas have
(`check_condition`); a source without an output schema allows only its whole output.
"""

import json
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from aios.config import get_settings


def compact(value: Any) -> str:
    """A structured value as text: compact JSON, or the string itself."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


# --- Schemas ------------------------------------------------------------------------------------


def check_schema(schema: Any) -> list[str]:
    """Why a schema can't be saved (not a valid draft 2020-12 schema, or too large); none if it
    can."""
    if not isinstance(schema, dict | bool):
        return ["A schema must be a JSON object"]
    cap = get_settings().workflow_schema_max_bytes
    if _size(schema) > cap:
        return [f"The schema is too large (at most {cap} bytes)"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"Not a valid JSON Schema: {exc.message}"]
    return []


def _pointer(path) -> str:
    return "/" + "/".join(str(p) for p in path) if path else "/"


def schema_errors(value: Any, schema: dict | bool) -> list[tuple[str, str]]:
    """Where and why `value` doesn't fit `schema`: (JSON pointer, message) pairs, by path; none
    if it fits."""
    try:
        errors = list(Draft202012Validator(schema).iter_errors(value))
    except Exception as exc:  # e.g. a $ref nothing resolves: never fetched
        return [("/", f"The schema couldn't be applied: {exc}")]
    found = sorted(errors, key=lambda e: (list(map(str, e.absolute_path)), e.message))
    return [(_pointer(e.absolute_path), e.message) for e in found]


def describe(errors: list[tuple[str, str]]) -> str:
    return "; ".join(f"at {path}: {message}" for path, message in errors)


def object_schema(schema: dict | bool) -> bool:
    """Whether a schema asks only for objects, as a tool's arguments must be."""
    return isinstance(schema, dict) and schema.get("type") == "object"


def schema_has(schema: Any, path: list[str]) -> bool:
    """Whether `schema` declares the field at `path` (keys, or indexes into arrays): through
    `properties`, `items`/`prefixItems` and the branches of `allOf`/`anyOf`/`oneOf`."""
    if not path:
        return True
    if not isinstance(schema, dict):
        return False
    for branch in ("allOf", "anyOf", "oneOf"):
        if any(schema_has(s, path) for s in schema.get(branch, [])):
            return True
    head, rest = path[0], path[1:]
    properties = schema.get("properties", {})
    if isinstance(properties, dict) and head in properties:
        return schema_has(properties[head], rest)
    if head.isdigit():
        prefix = schema.get("prefixItems", [])
        if int(head) < len(prefix):
            return schema_has(prefix[int(head)], rest)
        if isinstance(schema.get("items"), dict):
            return schema_has(schema["items"], rest)
    return False


# --- Templates ----------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_REF = re.compile(r"(input|nodes\.[A-Za-z0-9_-]{1,64})((?:\.[^.\s{}]+)*)")


@dataclass(frozen=True)
class Ref:
    """A placeholder: the run's input (`node` None) or a predecessor's output, and the field in
    it (`path`, empty for the whole value)."""

    text: str
    node: str | None
    path: tuple[str, ...]


def _ref(text: str) -> Ref | None:
    found = _REF.fullmatch(text.strip())
    if found is None:
        return None
    head, rest = found.groups()
    node = None if head == "input" else head.removeprefix("nodes.")
    return Ref(text.strip(), node, tuple(p for p in rest.split(".") if p))


def _strings(template: Any) -> Iterator[str]:
    if isinstance(template, str):
        yield template
    elif isinstance(template, list):
        for item in template:
            yield from _strings(item)
    elif isinstance(template, dict):
        for item in template.values():
            yield from _strings(item)


def template_errors(template: Any, predecessors: list[str]) -> list[str]:
    """Why a node's template can't be saved: a placeholder that isn't one (no expressions), one
    naming a node that isn't a predecessor, or the template too large."""
    errors = []
    cap = get_settings().workflow_schema_max_bytes
    if _size(template) > cap:
        errors.append(f"The template is too large (at most {cap} bytes)")
    for string in _strings(template):
        for found in _PLACEHOLDER.finditer(string):
            ref = _ref(found.group(1))
            if ref is None:
                errors.append(
                    f"{{{{{found.group(1)}}}}} isn't a placeholder: use {{{{input}}}},"
                    " {{input.field}}, {{nodes.<key>}} or {{nodes.<key>.field}}"
                )
            elif ref.node is not None and ref.node not in predecessors:
                errors.append(f"{{{{{ref.text}}}}} refers to {ref.node!r}, not a predecessor")
    return errors


class TemplateMissing(Exception):
    """A placeholder whose field isn't there at run time."""

    def __init__(self, ref: Ref) -> None:
        super().__init__(f"The template's {{{{{ref.text}}}}} isn't there: no field {ref.text}")
        self.ref = ref


_MISSING = object()


def _get(value: Any, path: tuple[str, ...] | list[str]) -> Any:
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return _MISSING
    return value


@dataclass(frozen=True)
class Rendered:
    """A rendered template: its value (the node's input), the text a person sees, and the text
    its agent's model is sent, with every predecessor's output in it wrapped as untrusted."""

    value: Any
    text: str
    message: str


def render(
    template: Any,
    input_value: Any,
    outputs: dict[str, Any],
    wrap: Callable[[str, str], str],
    *,
    derived_input: bool = False,
) -> Rendered:
    """Render a template with the run's input and the active predecessors' outputs (by key).
    `wrap(text, node key)` marks a predecessor's output as untrusted in the model's message,
    and the input too if it's `derived_input` (a subworkflow's, built from its node's
    predecessors), with the key "input". Raises `TemplateMissing`."""
    derived = False

    def look(ref: Ref) -> Any:
        nonlocal derived
        if ref.node is None:
            value = _get(input_value, ref.path)
            derived = derived or derived_input
        else:
            value = _get(outputs[ref.node], ref.path) if ref.node in outputs else _MISSING
            derived = True
        if value is _MISSING:
            raise TemplateMissing(ref)
        return value

    def string(text: str, wrapped: bool) -> str:
        def one(found: re.Match) -> str:
            ref = _ref(found.group(1))
            value = compact(look(ref))
            key = ref.node if ref.node is not None else ("input" if derived_input else None)
            return wrap(value, key) if wrapped and key is not None else value

        return _PLACEHOLDER.sub(one, text)

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            whole = _PLACEHOLDER.fullmatch(node)
            if whole is not None:
                return look(_ref(whole.group(1)))
            return string(node, wrapped=False)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    value = walk(template)
    text = compact(value)
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole is not None:
            ref = _ref(whole.group(1))
            key = ref.node if ref.node is not None else ("input" if derived_input else None)
            message = wrap(text, key) if key is not None else text
        else:
            message = string(template, wrapped=True)
    else:
        message = wrap(text, "template") if derived else text
    return Rendered(value, text, message)


# --- Conditions ---------------------------------------------------------------------------------


class ConditionError(Exception):
    """A condition that can't be evaluated on what it was given."""


OPERATORS = frozenset(
    {
        "var",
        "==",
        "!=",
        "===",
        "!==",
        "<",
        "<=",
        ">",
        ">=",
        "!",
        "!!",
        "and",
        "or",
        "if",
        "in",
        "missing",
        "missing_some",
    }
)
ROOTS = ("output", "input")
# A schema refused for itself, whose fields conditions aren't checked against.
UNCHECKED = object()


def _rule(expr: Any) -> tuple[str, list] | None:
    """An operation (operator, arguments), or None for a literal."""
    if isinstance(expr, dict) and len(expr) == 1:
        [(op, args)] = expr.items()
        return op, args if isinstance(args, list) else [args]
    return None


def _count(expr: Any) -> int:
    if isinstance(expr, list):
        return 1 + sum(_count(e) for e in expr)
    rule = _rule(expr)
    if rule is not None:
        return 1 + sum(_count(a) for a in rule[1])
    return 1


def check_condition(expr: Any, input_schema: Any, output_schema: Any) -> list[str]:
    """Why an edge's condition can't be saved: an operator outside the subset, too large, or a
    `var` reading a field the schemas don't declare (only the whole `output` or `input` without
    one). `input_schema` is the version's, `output_schema` the edge's source's."""
    errors: list[str] = []
    settings = get_settings()
    if _count(expr) > settings.workflow_condition_max_nodes:
        return [
            f"The condition is too large (at most {settings.workflow_condition_max_nodes} parts)"
        ]
    if _size(expr) > settings.workflow_schema_max_bytes:
        return ["The condition is too large"]
    schemas = {"output": output_schema, "input": input_schema}

    def var(args: list) -> None:
        if not args or not isinstance(args[0], str):
            errors.append('A var\'s path must be text, such as "output.score"')
            return
        path = args[0]
        root, *rest = path.split(".")
        if root not in ROOTS:
            errors.append(f"{path!r} can't be read: conditions read output and input")
        elif schemas[root] is UNCHECKED:
            pass
        elif rest and schemas[root] is None:
            errors.append(f"{path!r} can't be read: the {root} has no {root} schema")
        elif rest and not schema_has(schemas[root], rest):
            errors.append(f"{path!r} isn't in the {root} schema")

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        rule = _rule(node)
        if rule is None:
            return
        op, args = rule
        if op not in OPERATORS:
            errors.append(f"The operator {op!r} is not allowed")
            return
        if op == "var":
            var(args)
            if len(args) > 1:
                walk(args[1:])
            return
        walk(args)

    walk(expr)
    return errors


def truthy(value: Any) -> bool:
    """JSONLogic's truthiness: as JavaScript's, except an empty array is false."""
    if isinstance(value, dict):
        return True
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip()) if value.strip() else 0.0
        except ValueError:
            pass
    raise ConditionError(f"{compact(value)} isn't a number")


def _loose_eq(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list | dict) or isinstance(b, list | dict):
        return a == b
    try:
        return _number(a) == _number(b)
    except ConditionError:
        return False


def _strict_eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    return type(a) is type(b) and a == b


def _order(a: Any, b: Any) -> tuple[Any, Any]:
    for value in (a, b):
        if value is None or isinstance(value, list | dict):
            raise ConditionError(f"{compact(value)} can't be compared")
    if isinstance(a, str) and isinstance(b, str):
        return a, b
    return _number(a), _number(b)


def _compare(op: str, values: list) -> bool:
    if len(values) < 2:
        raise ConditionError(f"{op} needs two values")
    pairs = list(zip(values, values[1:], strict=False)) if op in ("<", "<=") else [values[:2]]
    for a, b in pairs:
        x, y = _order(a, b)
        if not {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]:
            return False
    return True


def evaluate(expr: Any, data: Any) -> Any:
    """Evaluate a condition on `data` ({"output": ..., "input": ...}). Raises `ConditionError`
    if it can't be."""
    if isinstance(expr, list):
        return [evaluate(e, data) for e in expr]
    rule = _rule(expr)
    if rule is None:
        return expr
    op, args = rule
    if op not in OPERATORS:
        raise ConditionError(f"The operator {op!r} is not allowed")
    if op == "and":
        if not args:
            raise ConditionError("and needs a value")
        value = None
        for arg in args:
            value = evaluate(arg, data)
            if not truthy(value):
                return value
        return value
    if op == "or":
        if not args:
            raise ConditionError("or needs a value")
        value = None
        for arg in args:
            value = evaluate(arg, data)
            if truthy(value):
                return value
        return value
    if op == "if":
        if not args:
            raise ConditionError("if needs a value")
        for i in range(0, len(args) - 1, 2):
            if truthy(evaluate(args[i], data)):
                return evaluate(args[i + 1], data)
        return evaluate(args[-1], data) if len(args) % 2 else None
    values = [evaluate(a, data) for a in args]
    if op == "var":
        path = values[0] if values else ""
        default = values[1] if len(values) > 1 else None
        if path in ("", None):
            return data
        found = _get(data, str(path).split("."))
        return default if found is _MISSING or found is None else found
    if op in ("==", "!=", "===", "!=="):
        if len(values) < 2:
            raise ConditionError(f"{op} needs two values")
        equal = (_loose_eq if op in ("==", "!=") else _strict_eq)(values[0], values[1])
        return equal if op in ("==", "===") else not equal
    if op in ("<", "<=", ">", ">="):
        return _compare(op, values)
    if op == "!":
        return not truthy(values[0] if values else None)
    if op == "!!":
        return truthy(values[0] if values else None)
    if op == "in":
        if len(values) < 2:
            raise ConditionError("in needs two values")
        needle, haystack = values[0], values[1]
        if isinstance(haystack, str):
            if not isinstance(needle, str):
                raise ConditionError(f"{compact(needle)} can't be looked for in text")
            return needle in haystack
        if isinstance(haystack, list):
            return needle in haystack
        raise ConditionError(f"in needs a list or text, not {compact(haystack)}")
    if op == "missing":
        paths = values[0] if len(values) == 1 and isinstance(values[0], list) else values
        return [p for p in paths if _get(data, str(p).split(".")) in (_MISSING, None, "")]
    # missing_some
    if len(values) < 2 or not isinstance(values[1], list):
        raise ConditionError("missing_some needs a number and a list of fields")
    need, paths = values[0], values[1]
    missing = [p for p in paths if _get(data, str(p).split(".")) in (_MISSING, None, "")]
    return [] if len(paths) - len(missing) >= _number(need) else missing


# --- Saving a workflow --------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeDeclared:
    """What a node declares, for `check_workflow`: its output schema is the one its outgoing
    edges' conditions read (a workflow node without one, its version's)."""

    key: str
    input_schema: Any = None
    output_schema: Any = None
    input_template: Any = None


def check_workflow(
    nodes: list[NodeDeclared],
    edges: list[tuple[str, str, Any]],
    input_schema: Any,
    output_schema: Any,
) -> list[tuple[str, str]]:
    """Why a workflow's schemas, templates and conditions can't be saved, each with the item it's
    about ("input_schema", "nodes.<key>.input_template", "edges.<source>→<target>.condition");
    none if they can. The graph itself is already checked (`validate_graph`)."""
    errors: list[tuple[str, str]] = []
    for name, schema in (("input_schema", input_schema), ("output_schema", output_schema)):
        if schema is not None:
            errors += [(name, e) for e in check_schema(schema)]
    sources: dict[str, list[str]] = {n.key: [] for n in nodes}
    for source, target, _ in edges:
        sources[target].append(source)
    by_key = {n.key: n for n in nodes}
    usable_input = input_schema
    if input_schema is not None and check_schema(input_schema):
        usable_input = UNCHECKED
    for n in nodes:
        for name in ("input_schema", "output_schema"):
            schema = getattr(n, name)
            if schema is not None:
                errors += [(f"nodes.{n.key}.{name}", e) for e in check_schema(schema)]
        if n.input_template is not None:
            item = f"nodes.{n.key}.input_template"
            errors += [(item, e) for e in template_errors(n.input_template, sources[n.key])]
    for source, target, condition in edges:
        if condition is None:
            continue
        declared = by_key[source].output_schema
        usable = declared if declared is not None and not check_schema(declared) else None
        if declared is not None and usable is None:
            usable = UNCHECKED  # its schema is refused already
        item = f"edges.{source}→{target}.condition"
        errors += [(item, e) for e in check_condition(condition, usable_input, usable)]
    return errors


# --- Structured output --------------------------------------------------------------------------

# The tool an agent node with an output schema gives its output by.
OUTPUT_TOOL = "workflow_output"


def output_format(schema: Any):
    """The agent factory's structured response format for an output schema: a tool,
    `OUTPUT_TOOL`, taking the output (an object schema as its arguments; anything else as their
    `value`). Validated here (`schema_errors`) rather than by the factory, which doesn't for a
    JSON Schema."""
    from langchain.agents.structured_output import ToolStrategy

    return ToolStrategy(_tool_schema(schema), handle_errors=False)


def _tool_schema(schema: Any) -> dict:
    described = {
        "title": OUTPUT_TOOL,
        "description": "Give your final output, fitting its schema. Call this once, last.",
    }
    if object_schema(schema):
        return {**schema, **described}
    inner = dict(schema) if isinstance(schema, dict) else {}
    defs = {k: inner.pop(k) for k in ("$defs", "definitions") if k in inner}
    return {
        "type": "object",
        **described,
        "properties": {"value": inner if isinstance(schema, dict) else {}},
        "required": ["value"],
        **defs,
    }


def output_value(response: Any, schema: Any) -> Any:
    """The output from the tool's arguments (`output_format`)."""
    if object_schema(schema):
        return response
    return response.get("value") if isinstance(response, dict) else response
