"""What a step takes and gives, declared as data (`docs/analysis-python-steps.md` §3.1, §16).

Inputs are type patterns in the chain's own algebra (`src/analysis/core/types.ts`): a step
written for "any real series" takes a scope channel, an ADC log or a meter recording alike.
Outputs are rules the planner resolves without running Python: "the axis of `lines`", "one f32
field per chosen field of `analog`", "a tick in the clock of `lines`".

Inputs::

    Series(fields=Digital(), min_fields=1)        # digital lines, any axis
    Series(fields=Real(quantity="voltage"), axis="uniform")
    Table(requires={"value": "numeric"})
    Scalar(type="real", quantity="time")
    AnyData()

Outputs::

    Table(start="tick", width=Quantity.of("time", "s"), field="text")
    Series(axis=SameAs("analog"), fields=FieldsFrom("analog", selected_by="fields", type="f32"))
    Scalar(type="f64", unit="V")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

FIELD_TYPES = ("f32", "f64", "i32", "u32", "i64", "u64", "bool", "tri", "complex", "enum", "utf8", "json", "tick")
TYPE_CLASSES = ("real", "integer", "numeric")
AXIS_KINDS = ("uniform", "points", "explicit", "index")


class DeclarationError(ValueError):
    """A declaration the manifest cannot carry (`STEP_DECLARATION`), with what is wrong."""


def _drop_none(values: Mapping[str, Any]) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def _type_constraint(value: str | Sequence[str] | None) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value not in FIELD_TYPES and value not in TYPE_CLASSES:
            raise DeclarationError(f"unknown field type {value!r}")
        return value
    types = list(value)
    for entry in types:
        if entry not in FIELD_TYPES:
            raise DeclarationError(f"unknown field type {entry!r}")
    return types


# ---------------------------------------------------------------------------------------------
# Field constraints for input patterns


@dataclass(frozen=True)
class FieldConstraint:
    """What every field of an input (or column of a table) must be."""

    type: str | Sequence[str] | None = None
    quantity: str | Sequence[str] | None = None
    unit: Sequence[str] | None = None
    nullable: bool | None = None

    def to_json(self, minimum: int | None = None, maximum: int | None = None) -> dict:
        quantity = self.quantity if self.quantity is None or isinstance(self.quantity, str) else list(self.quantity)
        return _drop_none(
            {
                "type": _type_constraint(self.type),
                "quantity": quantity,
                "unit": None if self.unit is None else list(self.unit),
                "nullable": self.nullable,
                "min": minimum,
                "max": maximum,
            }
        )


def Digital() -> FieldConstraint:
    """Tri-state logic: low, high or unknown."""
    return FieldConstraint(type="tri")


def Real(quantity: str | Sequence[str] | None = None, unit: Sequence[str] | None = None) -> FieldConstraint:
    return FieldConstraint(type="real", quantity=quantity, unit=unit)


def IntegerField(quantity: str | Sequence[str] | None = None) -> FieldConstraint:
    return FieldConstraint(type="integer", quantity=quantity)


def Numeric(quantity: str | Sequence[str] | None = None, unit: Sequence[str] | None = None) -> FieldConstraint:
    return FieldConstraint(type="numeric", quantity=quantity, unit=unit)


def TextField() -> FieldConstraint:
    return FieldConstraint(type="utf8")


def BoolField() -> FieldConstraint:
    return FieldConstraint(type="bool")


# ---------------------------------------------------------------------------------------------
# Output columns and rules


@dataclass(frozen=True)
class SameAs:
    """An output series on the axis, clocks and coherence of an input series."""

    input: str


@dataclass(frozen=True)
class UnitOf:
    """The unit every field of an input shares (none when they differ)."""

    input: str


@dataclass(frozen=True)
class ExplicitAxis:
    column: str
    quantity: str
    unit: str
    monotonic: str = "increasing"


@dataclass(frozen=True)
class IndexAxis:
    pass


@dataclass(frozen=True)
class Column:
    """One output field."""

    type: str
    quantity: str | None = None
    unit: str | UnitOf | None = None
    has_unit: bool = False  # True when `unit=None` means "no unit" rather than "not declared"
    nullable: bool = False
    values: Sequence[str] | None = None
    clock_of: str | None = None

    def to_json(self, name: str, default_clock: str | None) -> dict:
        if self.type not in FIELD_TYPES:
            raise DeclarationError(f"unknown field type {self.type!r} for {name}")
        clock = self.clock_of if self.type == "tick" else None
        if self.type == "tick" and clock is None:
            if default_clock is None:
                raise DeclarationError(f"say which input's clock {name} counts in: Tick(clock_of=...)")
            clock = default_clock
        out: dict = {"name": name, "type": self.type, "nullable": self.nullable}
        if self.quantity is not None:
            out["quantity"] = self.quantity
        if isinstance(self.unit, UnitOf):
            out["unit"] = {"sameAs": self.unit.input}
        elif self.unit is not None or self.has_unit:
            out["unit"] = self.unit
        if self.values is not None:
            out["values"] = list(self.values)
        if clock is not None:
            out["clock"] = {"axisOf": clock}
        return out


def Tick(clock_of: str | None = None, nullable: bool = False) -> Column:
    """Integer ticks in the clock of an input's time axis: exact time, never float seconds."""
    return Column(type="tick", clock_of=clock_of, nullable=nullable)


def Enum(values: Sequence[str], nullable: bool = False) -> Column:
    return Column(type="enum", values=tuple(values), nullable=nullable)


def NoUnit(type: str = "f64", nullable: bool = False) -> Column:
    """A number that states it has no unit."""
    return Column(type=type, unit=None, has_unit=True, nullable=nullable)


SHORTHAND = {"text": "utf8", "string": "utf8", "number": "f64", "float": "f64", "int": "i64"}


def column(spec: Column | str) -> Column:
    """A column from its declaration or a shorthand: "tick", "text", "f64", "i64", "bool", ..."""
    if isinstance(spec, Column):
        return spec
    if isinstance(spec, str):
        type_ = SHORTHAND.get(spec, spec)
        if type_ not in FIELD_TYPES:
            raise DeclarationError(f"unknown column type {spec!r}")
        return Column(type=type_)
    raise DeclarationError(f"a column is a Column or a type name, not {type(spec).__name__}")


@dataclass(frozen=True)
class FieldsFrom:
    """One output field per value field of an input, narrowed by a `Fields` param if named."""

    input: str
    selected_by: str | None = None
    type: str = "same"
    unit: str | None = "same"
    nullable: bool | str = "same"

    def to_json(self) -> dict:
        out: dict = {"from": self.input, "type": self.type, "unit": self.unit, "nullable": self.nullable}
        if self.selected_by is not None:
            out["selectedBy"] = self.selected_by
        return out


# ---------------------------------------------------------------------------------------------
# Data kinds: inputs (patterns) and outputs (rules)


class Series:
    """As an input: a series pattern. As an output: a series on a declared axis."""

    def __init__(
        self,
        fields: FieldConstraint | FieldsFrom | Mapping[str, Column | str] | None = None,
        *,
        axis: str | Sequence[str] | SameAs | ExplicitAxis | IndexAxis | None = None,
        interpolation: str | None = None,
        min_fields: int | None = None,
        max_fields: int | None = None,
        coherence: str | None = None,
        optional: bool = False,
    ) -> None:
        self.fields = fields
        self.axis = axis
        self.interpolation = interpolation
        self.min_fields = min_fields
        self.max_fields = max_fields
        self.coherence = coherence
        self.optional = optional

    def to_input(self) -> dict:
        if isinstance(self.fields, (FieldsFrom, Mapping)) or isinstance(self.axis, (SameAs, ExplicitAxis, IndexAxis)):
            raise DeclarationError("an input series declares a pattern: fields=Real(...), axis='uniform'")
        pattern: dict = {"kind": "series"}
        axis = _drop_none(
            {
                "kind": None if self.axis is None else (self.axis if isinstance(self.axis, str) else list(self.axis)),
                "interpolation": self.interpolation,
            }
        )
        if axis:
            pattern["axis"] = axis
        if self.fields is not None or self.min_fields is not None or self.max_fields is not None:
            constraint = self.fields if isinstance(self.fields, FieldConstraint) else FieldConstraint()
            pattern["fields"] = constraint.to_json(self.min_fields, self.max_fields)
        if self.coherence is not None:
            pattern["coherence"] = self.coherence
        return {"pattern": pattern, "cardinality": "optional" if self.optional else "one"}

    def to_output(self, default_clock: str | None) -> dict:
        axis = self.axis
        if isinstance(axis, SameAs):
            axis_json: dict = {"sameAs": axis.input}
        elif isinstance(axis, ExplicitAxis):
            axis_json = {
                "kind": "explicit",
                "column": axis.column,
                "quantity": axis.quantity,
                "unit": axis.unit,
                "monotonic": axis.monotonic,
            }
        elif isinstance(axis, IndexAxis):
            axis_json = {"kind": "index"}
        else:
            raise DeclarationError("an output series names its axis: SameAs(input), ExplicitAxis(...) or IndexAxis()")
        if isinstance(self.fields, FieldsFrom):
            fields: Any = self.fields.to_json()
        elif isinstance(self.fields, Mapping) and self.fields:
            fields = [column(spec).to_json(name, default_clock) for name, spec in self.fields.items()]
        else:
            raise DeclarationError("an output series declares its fields: FieldsFrom(...) or {name: column}")
        return {"kind": "series", "axis": axis_json, "fields": fields}


class Table:
    """As an input: a table pattern. As an output: a table with these columns, in order."""

    def __init__(
        self,
        columns: Mapping[str, Column | str] | None = None,
        *,
        fields: FieldConstraint | None = None,
        requires: Mapping[str, str | Sequence[str]] | None = None,
        optional: bool = False,
        **named: Column | str,
    ) -> None:
        self.columns = {**(columns or {}), **named}
        self.fields = fields
        self.requires = requires
        self.optional = optional

    def to_input(self) -> dict:
        if self.columns:
            raise DeclarationError("an input table declares a pattern: fields=..., requires={...}")
        pattern: dict = {"kind": "table"}
        if self.fields is not None:
            pattern["fields"] = self.fields.to_json()
        if self.requires:
            pattern["columns"] = [
                _drop_none({"name": name, "type": _type_constraint(type_)}) for name, type_ in self.requires.items()
            ]
        return {"pattern": pattern, "cardinality": "optional" if self.optional else "one"}

    def to_output(self, default_clock: str | None) -> dict:
        if not self.columns:
            raise DeclarationError("an output table declares its columns")
        return {"kind": "table", "fields": [column(spec).to_json(name, default_clock) for name, spec in self.columns.items()]}


class Scalar:
    """As an input: a scalar pattern (a type class allowed). As an output: one typed value."""

    def __init__(
        self,
        type: str | Sequence[str] | None = None,
        *,
        quantity: str | None = None,
        unit: str | UnitOf | None = None,
        values: Sequence[str] | None = None,
        optional: bool = False,
    ) -> None:
        self.type = type
        self.quantity = quantity
        self.unit = unit
        self.values = values
        self.optional = optional

    def to_input(self) -> dict:
        pattern: dict = {"kind": "scalar"}
        scalar = _drop_none({"type": _type_constraint(self.type), "quantity": self.quantity})
        if scalar:
            pattern["scalar"] = scalar
        return {"pattern": pattern, "cardinality": "optional" if self.optional else "one"}

    def to_output(self, default_clock: str | None) -> dict:
        if not isinstance(self.type, str) or self.type not in FIELD_TYPES:
            raise DeclarationError("an output scalar names one field type")
        out: dict = {"kind": "scalar", "type": self.type}
        if self.quantity is not None:
            out["quantity"] = self.quantity
        if isinstance(self.unit, UnitOf):
            out["unit"] = {"sameAs": self.unit.input}
        elif self.unit is not None:
            out["unit"] = self.unit
        if self.values is not None:
            out["values"] = list(self.values)
        return out


@dataclass(frozen=True)
class AnyData:
    optional: bool = False

    def to_input(self) -> dict:
        return {"pattern": {"kind": "any"}, "cardinality": "optional" if self.optional else "one"}


@dataclass(frozen=True)
class Budget:
    """What one run may use. A host's own limits apply as well; the lower one wins."""

    max_rows: int = 10_000_000
    max_seconds: int = 60
    max_memory_bytes: int = 1024 * 1024 * 1024
    max_output_bytes: int = 256 * 1024 * 1024

    def to_json(self) -> dict:
        return {
            "maxRows": self.max_rows,
            "maxSeconds": self.max_seconds,
            "maxMemoryBytes": self.max_memory_bytes,
            "maxOutputBytes": self.max_output_bytes,
        }


__all__ = [
    "AnyData",
    "BoolField",
    "Budget",
    "Column",
    "DeclarationError",
    "Digital",
    "Enum",
    "ExplicitAxis",
    "FieldConstraint",
    "FieldsFrom",
    "IndexAxis",
    "IntegerField",
    "NoUnit",
    "Numeric",
    "Real",
    "SameAs",
    "Scalar",
    "Series",
    "Table",
    "TextField",
    "Tick",
    "UnitOf",
    "column",
]
