"""A step's parameters: a closed set of kinds, each drawn with the designer's own controls.

| Kind       | Declared with                                  | The designer draws        |
| ---------- | ---------------------------------------------- | ------------------------- |
| `quantity` | `Quantity(kind, unit, default=, minimum=, ...)` | a value box with its unit |
| `integer`  | `Integer(default=, minimum=, maximum=)`        | a number box              |
| `choice`   | `Choice(["a", "b"] or {"a": "Label"}, ...)`    | a select                  |
| `toggle`   | `Toggle(default=)`                             | a checkbox                |
| `field`    | `Field(of="input", type=...)`                  | one field of the input    |
| `fields`   | `Fields(of="input", type=..., min=1)`          | several fields            |
| `range`    | `Range(kind, unit, default=(low, high))`       | two value boxes           |

There is no dictionary, free JSON or "any" parameter (`docs/analysis-python-steps.md` §3.4). A
value with a unit is a number in the declared unit; the designer converts what is typed. A
parameter without a default is required unless declared `optional=True`. Every parameter has a
`label` and may have `help`, both user-facing text (`Text` or a plain English string).
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .declarations import Column, _type_constraint
from .text import Text, text


class Param:
    """The common part of every kind: its label and help."""

    def __init__(self, label: Text | str, help: Text | str | None) -> None:
        self.label = label
        self.help = help

    def messages(self, name: str) -> dict[str, dict]:
        out = {f"param.{name}.label": text(self.label).to_json()}
        if self.help is not None:
            out[f"param.{name}.help"] = text(self.help).to_json()
        return out

    def to_json(self) -> dict:
        raise NotImplementedError


def _number(value: float | int) -> float | int:
    return value if isinstance(value, int) and not isinstance(value, bool) else float(value)


class Quantity(Param):
    def __init__(
        self,
        kind: str,
        unit: str,
        *,
        label: Text | str,
        default: float | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        optional: bool = False,
        help: Text | str | None = None,
    ) -> None:
        super().__init__(label, help)
        self.kind, self.unit = kind, unit
        self.default, self.minimum, self.maximum, self.optional = default, minimum, maximum, optional

    @staticmethod
    def of(kind: str, unit: str, *, type: str = "f64", nullable: bool = False) -> Column:
        """An output column of a quantity in a unit, such as `Quantity.of("time", "s")`."""
        return Column(type=type, quantity=kind, unit=unit, nullable=nullable)

    def to_json(self) -> dict:
        out: dict = {
            "kind": "quantity",
            "quantity": self.kind,
            "unit": self.unit,
            "required": self.default is None and not self.optional,
        }
        for key, value in (("default", self.default), ("minimum", self.minimum), ("maximum", self.maximum)):
            if value is not None:
                out[key] = _number(value)
        return out


class Integer(Param):
    def __init__(
        self,
        *,
        label: Text | str,
        default: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
        optional: bool = False,
        help: Text | str | None = None,
    ) -> None:
        super().__init__(label, help)
        self.default, self.minimum, self.maximum, self.optional = default, minimum, maximum, optional

    def to_json(self) -> dict:
        out: dict = {"kind": "integer", "required": self.default is None and not self.optional}
        for key, value in (("default", self.default), ("minimum", self.minimum), ("maximum", self.maximum)):
            if value is not None:
                out[key] = int(value)
        return out


class Choice(Param):
    def __init__(
        self,
        choices: Sequence[str] | Mapping[str, Text | str],
        *,
        label: Text | str,
        default: str | None = None,
        optional: bool = False,
        help: Text | str | None = None,
    ) -> None:
        super().__init__(label, help)
        self.choices: tuple[tuple[str, Text | str], ...] = (
            tuple(choices.items()) if isinstance(choices, Mapping) else tuple((value, value) for value in choices)
        )
        self.default, self.optional = default, optional

    def messages(self, name: str) -> dict[str, dict]:
        out = super().messages(name)
        for value, label in self.choices:
            out[f"param.{name}.choice.{value}"] = text(label).to_json()
        return out

    def to_json(self) -> dict:
        out: dict = {
            "kind": "choice",
            "choices": [value for value, _ in self.choices],
            "required": self.default is None and not self.optional,
        }
        if self.default is not None:
            out["default"] = self.default
        return out


class Toggle(Param):
    def __init__(self, *, label: Text | str, default: bool = False, help: Text | str | None = None) -> None:
        super().__init__(label, help)
        self.default = default

    def to_json(self) -> dict:
        return {"kind": "toggle", "default": bool(self.default)}


class Field(Param):
    def __init__(
        self,
        *,
        of: str,
        label: Text | str,
        type: str | Sequence[str] | None = None,
        optional: bool = False,
        help: Text | str | None = None,
    ) -> None:
        super().__init__(label, help)
        self.of, self.type, self.optional = of, type, optional

    def to_json(self) -> dict:
        out: dict = {"kind": "field", "of": self.of, "required": not self.optional}
        constraint = _type_constraint(self.type)
        if constraint is not None:
            out["type"] = constraint
        return out


class Fields(Param):
    def __init__(
        self,
        *,
        of: str,
        label: Text | str,
        type: str | Sequence[str] | None = None,
        min: int = 0,
        max: int | None = None,
        help: Text | str | None = None,
    ) -> None:
        super().__init__(label, help)
        self.of, self.type, self.min, self.max = of, type, min, max

    def to_json(self) -> dict:
        out: dict = {"kind": "fields", "of": self.of, "min": int(self.min)}
        constraint = _type_constraint(self.type)
        if constraint is not None:
            out["type"] = constraint
        if self.max is not None:
            out["max"] = int(self.max)
        return out


class Range(Param):
    def __init__(
        self,
        kind: str,
        unit: str,
        *,
        label: Text | str,
        default: tuple[float, float] | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        optional: bool = False,
        help: Text | str | None = None,
    ) -> None:
        super().__init__(label, help)
        self.kind, self.unit = kind, unit
        self.default, self.minimum, self.maximum, self.optional = default, minimum, maximum, optional

    def to_json(self) -> dict:
        out: dict = {
            "kind": "range",
            "quantity": self.kind,
            "unit": self.unit,
            "required": self.default is None and not self.optional,
        }
        if self.default is not None:
            low, high = self.default
            out["default"] = {"low": _number(low), "high": _number(high)}
        for key, value in (("minimum", self.minimum), ("maximum", self.maximum)):
            if value is not None:
                out[key] = _number(value)
        return out


PARAM_KINDS = (Quantity, Integer, Choice, Toggle, Field, Fields, Range)
