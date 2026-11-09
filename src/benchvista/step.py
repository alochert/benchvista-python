"""The `Step` base class and what a step works with while it runs.

    class GlitchFinder(Step):
        '''Finds pulses shorter than a limit on digital lines.'''

        id = "glitch-finder"
        version = "1.0.0"
        name = "Glitch finder"
        purity = "pure"
        category = "measure"
        inputs = {"lines": Series(fields=Digital(), min_fields=1)}
        outputs = {"glitches": Table(start="tick", width=Quantity.of("time", "s"), field="text")}
        params = {"shortest": Quantity("time", "s", default=1e-6, minimum=0, label="Shortest valid pulse")}

        def run(self, ctx, lines, params):
            for chunk in lines.chunks(params.fields):
                ...
                ctx.emit("glitches", start=tick, width=seconds, field=name)

Every declaration is a class attribute, so `benchvista step build` writes the manifest without
calling `run`. `run` receives the context, each input by its port name, and `params`.

Time stays exact: an axis is integer ticks in a clock with a rational seconds-per-tick, and
`ctx.seconds(ticks, input)` is for display arithmetic only. Missing data never becomes zero:
each nullable column has its validity mask beside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, ClassVar, Iterator, Mapping, Sequence

import numpy as np

from .columnar import TEXT_TYPES, Column
from .dataset import Dataset
from .declarations import Budget


class StepError(Exception):
    """A failure the step declared in `errors`, by its code; anything else is `PYTHON_STEP_FAILED`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Step:
    id: ClassVar[str]
    version: ClassVar[str]
    name: ClassVar[Any] = None
    help: ClassVar[Any] = None
    purity: ClassVar[str] = "pure"
    category: ClassVar[str] = "measure"
    inputs: ClassVar[Mapping[str, Any]] = {}
    outputs: ClassVar[Mapping[str, Any]] = {}
    params: ClassVar[Mapping[str, Any]] = {}
    errors: ClassVar[Mapping[str, Any]] = {}
    translations: ClassVar[Mapping[str, Mapping[str, str]]] = {}
    runtimes: ClassVar[Sequence[str]] = ("connect",)
    budget: ClassVar[Budget] = Budget()

    def run(self, ctx: "Context", **kwargs: Any) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------------------------
# Inputs


@dataclass(frozen=True)
class Clock:
    id: str
    seconds_per_tick: Fraction


@dataclass
class Chunk:
    """Rows `[start, stop)` of a series: their ticks and the chosen columns with their masks."""

    start: int
    stop: int
    ticks: np.ndarray
    columns: dict[str, np.ndarray | list[str]]
    valid: dict[str, np.ndarray | None]

    def __getitem__(self, name: str) -> np.ndarray | list[str]:
        return self.columns[name]


class DataInput:
    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset

    @property
    def type(self) -> dict:
        return self.dataset.type

    @property
    def rows(self) -> int:
        return self.dataset.rows

    @property
    def fields(self) -> list[str]:
        return [field["name"] for field in self.dataset.type.get("fields", [])]

    def field(self, name: str) -> dict:
        for field in self.dataset.type.get("fields", []):
            if field["name"] == name:
                return field
        raise KeyError(name)

    def column(self, name: str) -> np.ndarray | list[str]:
        return self.dataset.column(name).values

    def valid(self, name: str) -> np.ndarray | None:
        """The validity mask of a nullable column (1 valid, 0 null), or None when it cannot be null."""
        return self.dataset.column(name).valid

    @property
    def quality(self) -> list[dict]:
        return list(self.dataset.manifest["quality"])


class SeriesInput(DataInput):
    @property
    def axis(self) -> dict:
        return self.dataset.type["axis"]

    @property
    def extent(self) -> dict:
        return self.dataset.manifest["extent"]

    @property
    def clock(self) -> Clock:
        axis = self.axis
        if "clock" not in axis:
            raise ValueError("this series has no time axis")
        for clock in self.dataset.manifest["clocks"]:
            if clock["id"] == axis["clock"]:
                numerator, denominator = clock["secondsPerTick"]
                return Clock(clock["id"], Fraction(int(numerator), int(denominator)))
        raise ValueError("DATASET_CLOCK_MISSING")

    @property
    def ticks(self) -> np.ndarray:
        """Each row's tick as int64: the axis column of a points axis, origin + step × row otherwise."""
        axis, extent = self.axis, self.extent
        if axis["kind"] == "points":
            values = self.dataset.column(axis["column"]).values
            assert isinstance(values, np.ndarray)
            return values
        if axis["kind"] == "uniform":
            return np.int64(extent["origin"]) + np.int64(extent["step"]) * np.arange(self.rows, dtype=np.int64)
        raise ValueError("this series has no time axis")

    @property
    def end_tick(self) -> int:
        extent = self.extent
        if extent["kind"] == "points":
            return int(extent["end"])
        return int(extent["origin"]) + int(extent["step"]) * self.rows

    def chunks(self, fields: Sequence[str] | None = None, rows: int = 65_536) -> Iterator[Chunk]:
        """The series in row ranges, with the chosen fields (every field when None)."""
        names = list(fields) if fields is not None else self.fields
        ticks = self.ticks
        for start in range(0, self.rows, rows):
            stop = min(self.rows, start + rows)
            columns: dict[str, np.ndarray | list[str]] = {}
            valid: dict[str, np.ndarray | None] = {}
            for name in names:
                column = self.dataset.column(name)
                columns[name] = column.values[start:stop]
                valid[name] = None if column.valid is None else column.valid[start:stop]
            yield Chunk(start, stop, ticks[start:stop], columns, valid)


class TableInput(DataInput):
    pass


class ScalarInput(DataInput):
    @property
    def value(self) -> Any:
        return self.dataset.manifest.get("scalar")

    @property
    def unit(self) -> str | None:
        return self.dataset.type.get("unit")


def wrap(dataset: Dataset) -> DataInput:
    kind = dataset.type["kind"]
    if kind == "series":
        return SeriesInput(dataset)
    if kind == "table":
        return TableInput(dataset)
    if kind == "scalar":
        return ScalarInput(dataset)
    return DataInput(dataset)


# ---------------------------------------------------------------------------------------------
# Params


@dataclass(frozen=True)
class Span:
    low: float
    high: float


class Params:
    """The resolved params, by attribute: `params.shortest`. A range is a `Span` (`.low`, `.high`)."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def as_dict(self) -> dict:
        return dict(self._values)


# ---------------------------------------------------------------------------------------------
# The context and the outputs it collects


@dataclass
class SeriesOutput:
    columns: dict[str, Any]
    like: SeriesInput | None
    extent: dict | None
    ticks: Any
    quality: list[dict] | None


class Context:
    """What a running step reaches: its outputs, progress, seeded randomness and clock arithmetic."""

    def __init__(
        self,
        purity: str,
        seed: int | None,
        output_kinds: Mapping[str, str],
        on_progress: Callable[[float], None] | None = None,
    ) -> None:
        self._on_progress = on_progress
        self._purity = purity
        self._seed = seed
        self._kinds = dict(output_kinds)
        self._rng: np.random.Generator | None = None
        self.rows: dict[str, list[dict]] = {}
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.series_outputs: dict[str, SeriesOutput] = {}
        self.scalars: dict[str, Any] = {}
        self.progress_value = 0.0
        self.diagnostics: list[str] = []
        self.limited = False

    def _port(self, port: str, kind: str) -> None:
        if port not in self._kinds:
            raise StepError("PYTHON_OUTPUT_UNDECLARED")
        if self._kinds[port] != kind:
            raise StepError("PYTHON_OUTPUT_TYPE")

    def emit(self, port: str, **row: Any) -> None:
        """One row of a table output; a nullable column takes None for null."""
        self._port(port, "table")
        self.rows.setdefault(port, []).append(row)

    def table(self, port: str, **columns: Any) -> None:
        """Rows of a table output as whole columns (arrays or lists of equal length)."""
        self._port(port, "table")
        self.tables.setdefault(port, []).append(columns)

    def series(
        self,
        port: str,
        columns: Mapping[str, Any],
        *,
        like: SeriesInput | None = None,
        extent: dict | None = None,
        ticks: Any = None,
        quality: list[dict] | None = None,
    ) -> None:
        """A series output, whole. `like` gives it an input's axis extent (and, unless `quality`
        is given, that input's quality intervals for the fields it keeps); a points axis takes its
        rows' `ticks`."""
        self._port(port, "series")
        if port in self.series_outputs:
            raise StepError("PYTHON_OUTPUT_TWICE")
        self.series_outputs[port] = SeriesOutput(dict(columns), like, extent, ticks, quality)

    def scalar(self, port: str, value: Any) -> None:
        self._port(port, "scalar")
        self.scalars[port] = value

    def progress(self, fraction: float) -> None:
        self.progress_value = min(1.0, max(0.0, float(fraction)))
        if self._on_progress is not None:
            self._on_progress(self.progress_value)

    def limit(self, code: str) -> None:
        """Stop early with an honest `limited` status and a diagnostic code such as `ROW_LIMIT`."""
        self.limited = True
        self.diagnostics.append(code)

    @property
    def rng(self) -> np.random.Generator:
        """Randomness for a `seeded` step, seeded by the engine; a `pure` step has none."""
        if self._purity != "seeded" or self._seed is None:
            raise StepError("PYTHON_PURITY")
        if self._rng is None:
            self._rng = np.random.Generator(np.random.PCG64(self._seed))
        return self._rng

    @staticmethod
    def seconds(ticks: Any, of: SeriesInput) -> Any:
        """Ticks as float seconds in an input's clock, for display arithmetic only."""
        per_tick = of.clock.seconds_per_tick
        return np.asarray(ticks, dtype=np.float64) * (per_tick.numerator / per_tick.denominator)


__all__ = [
    "Chunk",
    "Clock",
    "Column",
    "Context",
    "DataInput",
    "Params",
    "ScalarInput",
    "SeriesInput",
    "Span",
    "Step",
    "StepError",
    "TEXT_TYPES",
    "TableInput",
    "wrap",
]
