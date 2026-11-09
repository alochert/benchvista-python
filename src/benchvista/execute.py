"""Running a step over datasets and turning what it emitted into datasets (§3.3, §3.7).

`run_step` is what `benchvista step test` calls in-process and what the runtime process calls
inside a host's sandbox. Outputs are built against the types the planner resolved from the
manifest: a column of the wrong width, a null in a field that cannot be null, a missing output
or an undeclared one fails the run with an exact code, and nothing is coerced. The host checks
every output again on its side; this is the author's early warning, not the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from .columnar import TEXT_TYPES, Column, CodecError, column_array
from .dataset import Dataset, DatasetError, build, stored_fields
from .manifest import ParamError, output_types, resolve_params
from .step import Context, Params, SeriesInput, Step, StepError, wrap


class RunError(Exception):
    """A run that failed: its code (a declared step error or `PYTHON_*`) and where."""

    def __init__(self, code: str, where: str = "") -> None:
        super().__init__(f"{code} {where}".strip())
        self.code = code
        self.where = where


@dataclass
class StepResult:
    outputs: dict[str, Dataset]
    status: str = "complete"
    diagnostics: list[str] = field(default_factory=list)


def _clocks(inputs: Mapping[str, Dataset]) -> dict[str, dict]:
    clocks: dict[str, dict] = {}
    for dataset in inputs.values():
        for clock in dataset.manifest["clocks"]:
            clocks.setdefault(clock["id"], clock)
    return clocks


def _referenced_clocks(data_type: dict) -> list[str]:
    ids = []
    if data_type["kind"] == "series" and "clock" in data_type["axis"]:
        ids.append(data_type["axis"]["clock"])
    for spec in data_type.get("fields", []):
        if "clock" in spec and spec["clock"] not in ids:
            ids.append(spec["clock"])
    return ids


def _with_validity(spec: dict, raw: Any, rows: int, port: str) -> Column:
    """A column of a declared field from what the step gave: an array, a list (None is null),
    a numpy masked array, or a `(values, valid)` pair."""
    type_, nullable = spec["type"], spec["nullable"]
    valid: np.ndarray | None = None
    if isinstance(raw, tuple) and len(raw) == 2:
        raw, valid = raw[0], np.asarray(raw[1], dtype="u1")
    elif isinstance(raw, np.ma.MaskedArray):
        valid = (~np.ma.getmaskarray(raw)).astype("u1")
        raw = raw.filled(0 if type_ not in TEXT_TYPES else "")
    elif isinstance(raw, list) and any(value is None for value in raw):
        valid = np.array([value is not None for value in raw], dtype="u1")
        filler: Any = "" if type_ in TEXT_TYPES else 0
        raw = [filler if value is None else value for value in raw]
    if type_ == "enum":
        members = spec["values"]
        raw = [members.index(value) if isinstance(value, str) else value for value in raw]
    if valid is not None and not nullable:
        if np.any(valid == 0):
            raise RunError("PYTHON_OUTPUT_VALUE", f"{port}.{spec['name']}: null in a field that cannot be null")
        valid = None
    if nullable and valid is None:
        valid = np.ones(rows, dtype="u1")
    try:
        values = column_array(type_, raw)
    except (CodecError, ValueError, TypeError, OverflowError) as error:
        raise RunError("PYTHON_OUTPUT_VALUE", f"{port}.{spec['name']}: {error}") from error
    if len(values) != rows:
        raise RunError("PYTHON_OUTPUT_ROWS", f"{port}.{spec['name']}")
    return Column(spec["name"], type_, values, valid)


def _table(ctx: Context, port: str, data_type: dict) -> tuple[dict[str, Column], int]:
    specs = data_type["fields"]
    names = [spec["name"] for spec in specs]
    gathered: dict[str, list[Any]] = {name: [] for name in names}
    for row in ctx.rows.get(port, []):
        if set(row) != set(names):
            raise RunError("PYTHON_OUTPUT_TYPE", f"{port}: a row's columns {sorted(row)} are not {names}")
        for name in names:
            gathered[name].append(row[name])
    for batch in ctx.tables.get(port, []):
        if set(batch) != set(names):
            raise RunError("PYTHON_OUTPUT_TYPE", f"{port}: columns {sorted(batch)} are not {names}")
        for name in names:
            gathered[name].extend(list(batch[name]))
    rows = len(gathered[names[0]]) if names else 0
    return {spec["name"]: _with_validity(spec, gathered[spec["name"]], rows, port) for spec in specs}, rows


def _series(ctx: Context, port: str, data_type: dict) -> tuple[dict[str, Column], int, dict, list[dict]]:
    output = ctx.series_outputs[port]
    axis = data_type["axis"]
    like: SeriesInput | None = output.like
    value_specs = data_type["fields"]
    names = [spec["name"] for spec in value_specs]
    expected = set(names) | ({axis["column"]} if axis["kind"] == "explicit" else set())
    if set(output.columns) != expected:
        raise RunError("PYTHON_OUTPUT_TYPE", f"{port}: columns {sorted(output.columns)} are not {sorted(expected)}")
    first = output.columns[names[0]] if names else []
    rows = len(first[0]) if isinstance(first, tuple) else len(first)
    columns = {spec["name"]: _with_validity(spec, output.columns[spec["name"]], rows, port) for spec in value_specs}
    if axis["kind"] == "uniform":
        extent = output.extent or (dict(like.extent) if like is not None and like.rows == rows else None)
        if extent is None:
            raise RunError("PYTHON_OUTPUT_TYPE", f"{port}: a uniform axis needs like= an input of as many rows, or extent=")
    elif axis["kind"] == "points":
        ticks = output.ticks
        if ticks is None and like is not None and like.rows == rows:
            ticks = like.ticks
        if ticks is None:
            raise RunError("PYTHON_OUTPUT_TYPE", f"{port}: a points axis needs its rows' ticks")
        extent = output.extent or (
            dict(like.extent) if like is not None else {"kind": "points", "start": str(int(ticks[0])), "end": str(int(ticks[-1]) + 1)}
        )
        tick_spec = {"name": axis["column"], "type": "tick", "nullable": False}
        columns[axis["column"]] = _with_validity(tick_spec, np.asarray(ticks, dtype="<i8"), rows, port)
    elif axis["kind"] == "explicit":
        spec = {"name": axis["column"], "type": "f64", "nullable": False}
        columns[axis["column"]] = _with_validity(spec, output.columns[axis["column"]], rows, port)
        extent = {"kind": "explicit"}
    else:
        extent = {"kind": "index"}
    if output.quality is not None:
        quality = list(output.quality)
    elif like is not None:
        kept = set(names)
        quality = [entry for entry in like.quality if entry.get("field") is None or entry["field"] in kept]
    else:
        quality = []
    return columns, rows, extent, quality


def _scalar_value(value: Any, data_type: dict, port: str) -> Any:
    type_ = data_type["type"]
    if value is None:
        return None
    if type_ in ("f32", "f64"):
        return float(value)
    if type_ in ("i32", "u32"):
        return int(value)
    if type_ in ("i64", "u64", "tick"):
        return str(int(value))
    if type_ == "bool":
        return bool(value)
    if type_ in ("utf8", "json", "enum"):
        if type_ == "enum" and value not in data_type.get("values", []):
            raise RunError("PYTHON_OUTPUT_VALUE", port)
        return str(value)
    raise RunError("PYTHON_OUTPUT_TYPE", port)


def run_step(
    step: type[Step],
    manifest: dict,
    inputs: Mapping[str, Dataset],
    params: Mapping[str, Any],
    *,
    seed: int | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> StepResult:
    """Run a step once over its inputs and build its outputs as datasets."""
    input_types = {name: dataset.type for name, dataset in inputs.items()}
    for name, declared in manifest["inputs"].items():
        if name not in inputs and declared["cardinality"] != "optional":
            raise RunError("RUN_INPUT_MISSING", name)
    try:
        resolved = resolve_params(manifest, params, input_types)
    except ParamError as error:
        raise RunError("NODE_PARAMS", error.name) from error
    types = output_types(manifest, input_types, resolved)
    ctx = Context(manifest["purity"], seed, {port: t["kind"] for port, t in types.items()}, on_progress)
    arguments = {name: (wrap(inputs[name]) if name in inputs else None) for name in manifest["inputs"]}
    try:
        step().run(ctx, **arguments, params=Params(resolved))
    except StepError as error:
        if error.code in manifest["errors"] or error.code.startswith("PYTHON_"):
            raise RunError(error.code) from error
        raise RunError("PYTHON_STEP_FAILED", error.code) from error
    except RunError:
        raise
    except MemoryError as error:  # the address-space limit, where the kernel enforces one
        raise RunError("PYTHON_MEMORY") from error
    except Exception as error:  # an undeclared failure of the step's own code
        raise RunError("PYTHON_STEP_FAILED", f"{type(error).__name__}: {error}") from error

    clocks = _clocks(inputs)
    outputs: dict[str, Dataset] = {}
    total_rows = 0
    total_bytes = 0
    for port, data_type in types.items():
        try:
            if data_type["kind"] == "table":
                if port not in ctx.rows and port not in ctx.tables:
                    raise RunError("PYTHON_OUTPUT_MISSING", port)
                columns, rows = _table(ctx, port, data_type)
                dataset = build(data_type, columns, rows, clocks=[clocks[i] for i in _referenced_clocks(data_type)])
            elif data_type["kind"] == "series":
                if port not in ctx.series_outputs:
                    raise RunError("PYTHON_OUTPUT_MISSING", port)
                columns, rows, extent, quality = _series(ctx, port, data_type)
                if set(columns) != {spec["name"] for spec in stored_fields(data_type)}:
                    raise RunError("PYTHON_OUTPUT_TYPE", port)
                dataset = build(
                    data_type,
                    columns,
                    rows,
                    extent=extent,
                    clocks=[clocks[i] for i in _referenced_clocks(data_type)],
                    quality=quality,
                )
            else:
                if port not in ctx.scalars:
                    raise RunError("PYTHON_OUTPUT_MISSING", port)
                dataset = build(data_type, {}, 0, scalar=_scalar_value(ctx.scalars[port], data_type, port))
        except (DatasetError, KeyError) as error:
            raise RunError("PYTHON_OUTPUT_VALUE", f"{port}: {error}") from error
        total_rows += dataset.rows
        total_bytes += sum(len(chunk) for chunk in dataset.chunks)
        outputs[port] = dataset
    budget = manifest["budget"]
    if total_rows > budget["maxRows"]:
        raise RunError("PYTHON_OUTPUT_ROWS", f"{total_rows} rows")
    if total_bytes > budget["maxOutputBytes"]:
        raise RunError("PYTHON_OUTPUT_BYTES", f"{total_bytes} bytes")
    return StepResult(outputs, "limited" if ctx.limited else "complete", list(ctx.diagnostics))
