"""The step manifest (`docs/analysis-python-steps.md` §16): built from a `Step` class's
declarations without running it, checked, and resolved against input types.

`build_manifest` writes exactly the shape `src/analysis/python/manifest.ts` parses; TypeScript is
the authority at every boundary, and the checks here refuse a bad declaration early, at the
author's desk, with the same codes. `output_types` resolves the declarative output rules as
`src/analysis/python/outputs.ts` does, so `benchvista step test` can hold outputs to the types
the planner will expect.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .canonical import canonical_hex
from .declarations import AnyData, DeclarationError, Scalar, Series, Table
from .params import PARAM_KINDS, Param
from .step import Span, Step
from .text import LOCALES, revision, text

FORMAT = "benchvista-python-step-v1"
PYTHON = "3.13"
BASE = "2026.1"

UNITS: dict[str, str] = {
    "V": "voltage", "mV": "voltage", "uV": "voltage",
    "A": "current", "mA": "current", "uA": "current",
    "W": "power", "mW": "power", "uW": "power",
    "dBm": "level", "dB": "ratio", "dBc": "ratio",
    "Ohm": "resistance", "kOhm": "resistance", "MOhm": "resistance",
    "Hz": "frequency", "kHz": "frequency", "MHz": "frequency", "GHz": "frequency",
    "s": "time", "ms": "time", "us": "time", "ns": "time",
    "%": "ratio", "1": "dimensionless", "count": "count", "degC": "temperature",
}  # fmt: skip
QUANTITIES = frozenset(UNITS.values()) | {"unknown"}
ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION = re.compile(r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ERROR = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED = re.compile(r"^(PYTHON|STEP|NODE|RUN|PLAN|DATASET|RECIPE|EXPR|ANALYSIS)_")
SKIP_DIRS = {"fixtures", "tests", "__pycache__", "build", "dist"}


class ManifestError(ValueError):
    """A declaration the manifest refuses: a `STEP_MANIFEST_*` code and where."""

    def __init__(self, code: str, where: str) -> None:
        super().__init__(f"{code} at {where}")
        self.code = code
        self.where = where


# ---------------------------------------------------------------------------------------------
# Building


def find_step(module: Any) -> type[Step]:
    """The one `Step` subclass a module defines."""
    found = [
        value
        for value in vars(module).values()
        if inspect.isclass(value) and issubclass(value, Step) and value is not Step and value.__module__ == module.__name__
    ]
    if len(found) != 1:
        raise ManifestError("STEP_MANIFEST_ENTRY", f"{module.__name__}: {len(found)} Step classes")
    return found[0]


def source_files(root: Path) -> list[dict]:
    """Every Python source of a step's directory, sorted, with size and SHA-256."""
    files = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        data = path.read_bytes()
        files.append({"path": relative.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    if not files:
        raise ManifestError("STEP_MANIFEST_SOURCE", str(root))
    return files


def _default_clock(inputs: Mapping[str, Any]) -> str | None:
    series = [name for name, spec in inputs.items() if isinstance(spec, Series)]
    return series[0] if len(series) == 1 else None


def _purpose(step: type[Step]) -> str:
    doc = inspect.getdoc(step) or ""
    first = doc.split("\n\n", 1)[0].strip().replace("\n", " ")
    if not first:
        raise ManifestError("STEP_MANIFEST_TEXT_MISSING", "purpose: give the class a docstring")
    return first


def build_manifest(step: type[Step], *, entry: str, files: list[dict], extras: dict | None = None) -> dict:
    """The manifest of a step class, from its declarations alone."""
    try:
        inputs = {name: spec.to_input() for name, spec in step.inputs.items()}
        clock = _default_clock(step.inputs)
        outputs = {name: spec.to_output(clock) for name, spec in step.outputs.items()}
    except DeclarationError as error:
        raise ManifestError("STEP_MANIFEST_DECLARATION", str(error)) from error
    for name, spec in step.inputs.items():
        if not isinstance(spec, (Series, Table, Scalar, AnyData)):
            raise ManifestError("STEP_MANIFEST_DECLARATION", f"inputs.{name}")
    params: dict[str, dict] = {}
    messages: dict[str, dict] = {}
    name = step.name if step.name is not None else step.id.replace("-", " ").capitalize()
    messages["name"] = text(name).to_json()
    messages["purpose"] = text(_purpose(step)).to_json()
    if step.help is not None:
        messages["help"] = text(step.help).to_json()
    for key, param in step.params.items():
        if not isinstance(param, PARAM_KINDS) or not isinstance(param, Param):
            raise ManifestError("STEP_MANIFEST_DECLARATION", f"params.{key}")
        params[key] = param.to_json()
        messages.update(param.messages(key))
    for code, sentence in step.errors.items():
        messages[f"error.{code}"] = text(sentence).to_json()
    drafts: dict[str, dict] = {}
    for locale, entries in step.translations.items():
        if locale not in LOCALES:
            raise ManifestError("STEP_MANIFEST_LOCALE", locale)
        drafts[locale] = {}
        for key, translated in entries.items():
            if key not in messages:
                raise ManifestError("STEP_MANIFEST_DRAFT_SOURCE", f"{locale}.{key}")
            drafts[locale][key] = {"text": translated, "source": revision(messages[key]), "provenance": "author"}
    manifest = {
        "format": FORMAT,
        "id": step.id,
        "version": step.version,
        "category": step.category,
        "purity": step.purity,
        "inputs": inputs,
        "outputs": outputs,
        "params": params,
        "errors": list(step.errors),
        "text": {"messages": messages, "drafts": drafts},
        "runtime": {"python": PYTHON, "base": BASE, "runtimes": list(step.runtimes), "extras": extras},
        "budget": step.budget.to_json(),
        "source": {"entry": entry, "files": files},
        "sdk": {"name": "benchvista", "version": __version__},
    }
    check_manifest(manifest)
    return manifest


def manifest_hash(manifest: dict) -> str:
    """The manifest's identity: SHA-256 of its canonical JSON, bare hexadecimal."""
    return canonical_hex(manifest)


# ---------------------------------------------------------------------------------------------
# Checking: the rules of §16 an author can break, with TypeScript's codes


def check_manifest(manifest: dict) -> None:
    def fail(code: str, where: str) -> None:
        raise ManifestError(code, where)

    if not ID.match(manifest["id"]):
        fail("STEP_MANIFEST_INVALID", "id")
    if not VERSION.match(manifest["version"]):
        fail("STEP_MANIFEST_INVALID", "version")
    if manifest["purity"] not in ("pure", "seeded", "external"):
        fail("STEP_MANIFEST_INVALID", "purity")
    if manifest["category"] not in ("convert", "decode", "measure", "transform", "compare", "report"):
        fail("STEP_MANIFEST_INVALID", "category")
    inputs, outputs, params = manifest["inputs"], manifest["outputs"], manifest["params"]
    messages = manifest["text"]["messages"]
    for group in ("inputs", "outputs", "params"):
        for key in manifest[group]:
            if not NAME.match(key):
                fail("STEP_MANIFEST_INVALID", f"{group}.{key}")
    if not outputs:
        fail("STEP_MANIFEST_NO_OUTPUT", "outputs")

    def unit_fits(unit: Any, quantity: Any, where: str) -> None:
        if unit is None or isinstance(unit, dict):
            return
        if unit not in UNITS:
            fail("STEP_MANIFEST_INVALID", where)
        if quantity is not None and UNITS[unit] != quantity:
            fail("STEP_MANIFEST_UNIT_QUANTITY", where)

    def need_input(name: str, where: str, series: bool = False) -> None:
        spec = inputs.get(name)
        if spec is None or (series and spec["pattern"]["kind"] != "series"):
            fail("STEP_MANIFEST_INPUT_REF", where)

    for key, param in params.items():
        where = f"params.{key}"
        if f"param.{key}.label" not in messages:
            fail("STEP_MANIFEST_TEXT_MISSING", where)
        kind = param["kind"]
        if kind in ("quantity", "range"):
            if param["quantity"] not in QUANTITIES:
                fail("STEP_MANIFEST_INVALID", where)
            unit_fits(param["unit"], param["quantity"], where)
        if param.get("required") and "default" in param:
            fail("STEP_MANIFEST_DEFAULT", where)
        lo, hi = param.get("minimum"), param.get("maximum")
        if lo is not None and hi is not None and lo > hi:
            fail("STEP_MANIFEST_BOUNDS", where)
        default = param.get("default")
        values = (
            [default["low"], default["high"]]
            if kind == "range" and default is not None
            else [default]
            if kind in ("quantity", "integer") and default is not None
            else []
        )
        if kind == "range" and default is not None and default["low"] > default["high"]:
            fail("STEP_MANIFEST_BOUNDS", where)
        for value in values:
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                fail("STEP_MANIFEST_BOUNDS", where)
        if kind == "choice":
            if len(set(param["choices"])) != len(param["choices"]):
                fail("STEP_MANIFEST_DUPLICATE", where)
            if default is not None and default not in param["choices"]:
                fail("STEP_MANIFEST_DEFAULT", where)
            for choice in param["choices"]:
                if f"param.{key}.choice.{choice}" not in messages:
                    fail("STEP_MANIFEST_TEXT_MISSING", f"{where}.{choice}")
        if kind in ("field", "fields"):
            need_input(param["of"], where)
        if kind == "fields" and param.get("max") is not None and param["min"] > param["max"]:
            fail("STEP_MANIFEST_BOUNDS", where)

    def check_field(field: dict, where: str) -> None:
        unit = field.get("unit")
        if isinstance(unit, dict):
            need_input(unit["sameAs"], where)
        unit_fits(unit, field.get("quantity"), where)
        if (field["type"] == "tick") != ("clock" in field):
            fail("STEP_MANIFEST_FIELD_TYPE", where)
        if "clock" in field:
            need_input(field["clock"]["axisOf"], where, series=True)
        if (field["type"] == "enum") != ("values" in field):
            fail("STEP_MANIFEST_FIELD_TYPE", where)

    for port, output in outputs.items():
        where = f"outputs.{port}"
        if output["kind"] == "scalar":
            unit = output.get("unit")
            if isinstance(unit, dict):
                need_input(unit["sameAs"], where)
            unit_fits(unit, output.get("quantity"), where)
            continue
        if output["kind"] == "series":
            axis = output["axis"]
            if "sameAs" in axis:
                need_input(axis["sameAs"], where, series=True)
            elif axis["kind"] == "explicit":
                unit_fits(axis["unit"], axis["quantity"], where)
            if isinstance(output["fields"], dict):
                rule = output["fields"]
                need_input(rule["from"], where)
                selector = rule.get("selectedBy")
                if selector is not None:
                    chosen = params.get(selector)
                    if chosen is None or chosen["kind"] != "fields" or chosen["of"] != rule["from"]:
                        fail("STEP_MANIFEST_SELECTED_BY", where)
                if rule["type"] in ("tick", "enum"):
                    fail("STEP_MANIFEST_FIELD_TYPE", where)
                continue
        names = [field["name"] for field in output["fields"]]
        if len(set(names)) != len(names):
            fail("STEP_MANIFEST_DUPLICATE", where)
        for field in output["fields"]:
            check_field(field, f"{where}.{field['name']}")

    for code in manifest["errors"]:
        if not ERROR.match(code):
            fail("STEP_MANIFEST_INVALID", f"errors.{code}")
        if RESERVED.match(code):
            fail("STEP_MANIFEST_RESERVED_ERROR", f"errors.{code}")
    for key in ("name", "purpose"):
        if key not in messages:
            fail("STEP_MANIFEST_TEXT_MISSING", key)
    for key, message in messages.items():
        for token in message["tokens"]:
            if token not in message["en"]:
                fail("STEP_MANIFEST_TEXT_TOKEN", key)
    for locale, entries in manifest["text"]["drafts"].items():
        for key, draft in entries.items():
            for token in messages[key]["tokens"]:
                if token not in draft["text"]:
                    fail("STEP_MANIFEST_TEXT_TOKEN", f"{locale}.{key}")
    runtimes = manifest["runtime"]["runtimes"]
    if not runtimes or len(set(runtimes)) != len(runtimes) or not set(runtimes) <= {"connect", "browser"}:
        fail("STEP_MANIFEST_RUNTIME", "runtime.runtimes")


# ---------------------------------------------------------------------------------------------
# Params and output types, as the planner resolves them


class ParamError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"PLAN_PARAMS at params.{name}")
        self.name = name


def _value_fields(data_type: dict | None) -> list[dict]:
    if data_type is None or data_type["kind"] not in ("series", "table"):
        return []
    return list(data_type["fields"])


def _type_fits(constraint: Any, type_: str) -> bool:
    if constraint is None:
        return True
    if constraint == "real":
        return type_ in ("f32", "f64")
    if constraint == "integer":
        return type_ in ("i32", "u32", "i64", "u64")
    if constraint == "numeric":
        return type_ in ("f32", "f64", "i32", "u32", "i64", "u64")
    if isinstance(constraint, list):
        return type_ in constraint
    return constraint == type_


def resolve_params(manifest: dict, given: Mapping[str, Any], input_types: Mapping[str, dict]) -> dict:
    """The params a step receives: checked by kind, defaults applied, a fields param that names
    nothing resolved to every fitting field. `ParamError` names the param that fails."""
    out: dict[str, Any] = {}
    for name in given:
        if name not in manifest["params"]:
            raise ParamError(name)
    for name, param in manifest["params"].items():
        kind = param["kind"]
        value = given.get(name, param.get("default"))
        if kind == "fields":
            offered = [f["name"] for f in _value_fields(input_types.get(param["of"])) if _type_fits(param.get("type"), f["type"])]
            chosen = list(value) if value is not None else offered
            if len(chosen) < max(1, param["min"]) or len(set(chosen)) != len(chosen):
                raise ParamError(name)
            if param.get("max") is not None and len(chosen) > param["max"]:
                raise ParamError(name)
            if any(field not in offered for field in chosen):
                raise ParamError(name)
            out[name] = chosen
            continue
        if value is None:
            if param.get("required"):
                raise ParamError(name)
            out[name] = None
            continue
        lo, hi = param.get("minimum"), param.get("maximum")
        if kind in ("quantity", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ParamError(name)
            if kind == "integer" and not float(value).is_integer():
                raise ParamError(name)
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                raise ParamError(name)
            out[name] = int(value) if kind == "integer" else float(value)
        elif kind == "choice":
            if value not in param["choices"]:
                raise ParamError(name)
            out[name] = value
        elif kind == "toggle":
            if not isinstance(value, bool):
                raise ParamError(name)
            out[name] = value
        elif kind == "field":
            offered = [f["name"] for f in _value_fields(input_types.get(param["of"])) if _type_fits(param.get("type"), f["type"])]
            if value not in offered:
                raise ParamError(name)
            out[name] = value
        elif kind == "range":
            low, high = (value["low"], value["high"]) if isinstance(value, dict) else value
            if low > high or any((lo is not None and v < lo) or (hi is not None and v > hi) for v in (low, high)):
                raise ParamError(name)
            out[name] = Span(float(low), float(high))
    return out


class OutputRuleError(ValueError):
    def __init__(self, port: str) -> None:
        super().__init__(f"STEP_OUTPUT_RULE at {port}")
        self.port = port


def _shared_unit(data_type: dict | None) -> str | None:
    if data_type is None:
        return None
    if data_type["kind"] == "scalar":
        return data_type.get("unit")
    units = {field.get("unit") for field in _value_fields(data_type)}
    return next(iter(units)) if len(units) == 1 else None


def _axis_clock(data_type: dict | None, port: str) -> str:
    if data_type is not None and data_type["kind"] == "series" and "clock" in data_type["axis"]:
        return data_type["axis"]["clock"]
    raise OutputRuleError(port)


def _unit_rule(rule: Any, input_types: Mapping[str, dict]) -> tuple[bool, str | None]:
    """(declared, unit) for a unit rule; `declared` is False when the manifest states nothing."""
    if rule is _ABSENT:
        return False, None
    if isinstance(rule, dict):
        return True, _shared_unit(input_types.get(rule["sameAs"]))
    return True, rule


_ABSENT = object()


def _declared_field(field: dict, input_types: Mapping[str, dict], port: str) -> dict:
    declared, unit = _unit_rule(field.get("unit", _ABSENT), input_types)
    quantity = field.get("quantity") or (UNITS[unit] if unit is not None else None)
    out: dict = {"name": field["name"], "type": field["type"], "nullable": field["nullable"]}
    if quantity is not None:
        out["quantity"] = quantity
    if declared:
        out["unit"] = unit
    if "values" in field:
        out["values"] = list(field["values"])
    if "clock" in field:
        out["clock"] = _axis_clock(input_types.get(field["clock"]["axisOf"]), port)
    return out


def _fields_from(
    manifest: dict, rule: dict, input_types: Mapping[str, dict], params: Mapping[str, Any], port: str
) -> list[dict]:
    offered = _value_fields(input_types.get(rule["from"]))
    if not offered:
        raise OutputRuleError(port)
    selector = rule.get("selectedBy")
    chosen = params.get(selector) if selector is not None else None
    declared = manifest["params"].get(selector) if selector is not None else None
    # A `fields` param left empty means every field of its input that fits its type.
    fitting = (
        [f for f in offered if _type_fits(declared.get("type"), f["type"])]
        if declared is not None and declared["kind"] == "fields"
        else offered
    )
    picked = fitting if chosen is None else [next(f for f in offered if f["name"] == name) for name in chosen]
    out = []
    for field in picked:
        type_ = field["type"] if rule["type"] == "same" else rule["type"]
        same_unit = rule["unit"] == "same"
        entry = {k: v for k, v in field.items() if k not in ("unit", "unitFrom", "values", "clock", "quantity")}
        entry["type"] = type_
        entry["nullable"] = field["nullable"] if rule["nullable"] == "same" else rule["nullable"]
        if same_unit:
            if "quantity" in field:
                entry["quantity"] = field["quantity"]
            if "unit" in field:
                entry["unit"] = field["unit"]
            if "unitFrom" in field:
                entry["unitFrom"] = field["unitFrom"]
        else:
            unit = rule["unit"]
            if unit is not None:
                entry["quantity"] = UNITS[unit]
            entry["unit"] = unit
        if type_ == field["type"] and "values" in field:
            entry["values"] = field["values"]
        if type_ == field["type"] and "clock" in field:
            entry["clock"] = field["clock"]
        out.append(entry)
    return out


def output_types(manifest: dict, input_types: Mapping[str, dict], params: Mapping[str, Any]) -> dict[str, dict]:
    """Every output's data type from the declarative rules (as `stepOutputTypes` in TypeScript)."""
    types: dict[str, dict] = {}
    for port, output in manifest["outputs"].items():
        kind = output["kind"]
        if kind == "scalar":
            declared, unit = _unit_rule(output.get("unit", _ABSENT), input_types)
            quantity = output.get("quantity") or (UNITS[unit] if unit is not None else None)
            out: dict = {"kind": "scalar", "type": output["type"]}
            if quantity is not None:
                out["quantity"] = quantity
            if declared:
                out["unit"] = unit
            if "values" in output:
                out["values"] = list(output["values"])
        elif kind == "table":
            out = {"kind": "table", "fields": [_declared_field(f, input_types, port) for f in output["fields"]]}
        else:
            axis = output["axis"]
            if "sameAs" in axis:
                source = input_types.get(axis["sameAs"])
                if source is None or source["kind"] != "series":
                    raise OutputRuleError(port)
                axis_type, coherence = source["axis"], source["coherence"]
            elif axis["kind"] == "explicit":
                axis_type, coherence = {k: axis[k] for k in ("kind", "column", "quantity", "unit", "monotonic")}, "joint"
            else:
                axis_type, coherence = {"kind": "index"}, "joint"
            fields = (
                _fields_from(manifest, output["fields"], input_types, params, port)
                if isinstance(output["fields"], dict)
                else [_declared_field(f, input_types, port) for f in output["fields"]]
            )
            out = {"kind": "series", "axis": axis_type, "fields": fields, "coherence": coherence}
        types[port] = out
    return types


def load_step_class(directory: Path, entry: str | None) -> tuple[type[Step], str]:
    """Import a step's module from its directory and find its class: `(class, "module:Class")`.

    This runs the author's module on the author's own machine (`step build`, `step test`); a
    host imports step code only inside its sandbox."""
    import importlib.util
    import sys

    module_name, _, class_name = (entry or "step:").partition(":")
    path = directory / (module_name.replace(".", "/") + ".py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ManifestError("STEP_MANIFEST_ENTRY", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(directory))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(directory))
    step = getattr(module, class_name) if class_name else find_step(module)
    return step, f"{module_name}:{step.__name__}"
