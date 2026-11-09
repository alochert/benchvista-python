"""BenchVista Python analysis steps (`docs/analysis-python-steps.md`).

Write a step by subclassing `Step`; declare its inputs, outputs, parameters and text as class
attributes; `benchvista step build` writes its manifest and `benchvista step test` runs its
fixtures.
"""

__version__ = "0.1.0"

from .declarations import (  # noqa: E402
    AnyData,
    BoolField,
    Budget,
    Column,
    Digital,
    Enum,
    ExplicitAxis,
    FieldConstraint,
    FieldsFrom,
    IndexAxis,
    IntegerField,
    NoUnit,
    Numeric,
    Real,
    SameAs,
    Scalar,
    Series,
    Table,
    TextField,
    Tick,
    UnitOf,
)
from .params import Choice, Field, Fields, Integer, Quantity, Range, Toggle  # noqa: E402
from .step import Context, Params, ScalarInput, SeriesInput, Span, Step, StepError, TableInput  # noqa: E402
from .text import Text  # noqa: E402

__all__ = [
    "AnyData",
    "BoolField",
    "Budget",
    "Choice",
    "Column",
    "Context",
    "Digital",
    "Enum",
    "ExplicitAxis",
    "Field",
    "FieldConstraint",
    "Fields",
    "FieldsFrom",
    "IndexAxis",
    "Integer",
    "IntegerField",
    "NoUnit",
    "Numeric",
    "Params",
    "Quantity",
    "Range",
    "Real",
    "SameAs",
    "Scalar",
    "ScalarInput",
    "Series",
    "SeriesInput",
    "Span",
    "Step",
    "StepError",
    "Table",
    "TableInput",
    "Text",
    "TextField",
    "Tick",
    "Toggle",
    "UnitOf",
    "__version__",
]
