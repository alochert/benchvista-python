"""`columnar-le/v1` chunks with numpy, as `src/analysis/core/codec.ts` writes and reads them.

A chunk is the columns of a row range one after the other: each fixed-width column as
little-endian values, each text column as a little-endian u32 byte length and the UTF-8 bytes per
row, and after either, one validity byte per row (1 valid, 0 null) when the field is nullable.
There is no header: the field list and the row count decode it. The same bytes give the same
SHA-256 on every platform, which is what makes a dataset's id reproducible.

Field types and their numpy arrays:

- `f32` `f64` `i32` `u32` `i64` `u64`: arrays of that width;
- `tick`: `int64` ticks in the field's clock, never float seconds;
- `bool`: `uint8`, 0 or 1;
- `tri`: `uint8` with `TRI_LOW` 0, `TRI_HIGH` 1 and `TRI_UNKNOWN` 2;
- `complex`: `complex128` (real and imaginary interleaved on the wire);
- `enum`: `uint16` indexes into the field's `values`;
- `utf8`, `json`: Python lists of `str`.

Validity is a separate `uint8` mask; a null row's value is whatever the writer put there and is
never read as data. Missing data is never turned into zero.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Sequence

import numpy as np

TRI_LOW, TRI_HIGH, TRI_UNKNOWN = 0, 1, 2

DTYPES: dict[str, str] = {
    "f32": "<f4",
    "f64": "<f8",
    "i32": "<i4",
    "u32": "<u4",
    "i64": "<i8",
    "u64": "<u8",
    "tick": "<i8",
    "bool": "u1",
    "tri": "u1",
    "complex": "<c16",
    "enum": "<u2",
}
TEXT_TYPES = frozenset({"utf8", "json"})
FIELD_TYPES = frozenset(DTYPES) | TEXT_TYPES


class CodecError(ValueError):
    """A chunk that does not decode as its layout says (`CODEC_*`)."""


@dataclass(frozen=True)
class Layout:
    name: str
    type: str
    nullable: bool


@dataclass
class Column:
    """One column of a row range: a numpy array (or a list of text) and its validity mask."""

    name: str
    type: str
    values: np.ndarray | list[str]
    valid: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.values)


def column_array(type_: str, values: Sequence | np.ndarray) -> np.ndarray | list[str]:
    """Values as the array a column of `type_` holds, checked for width but not converted lossily."""
    if type_ in TEXT_TYPES:
        out = [str(value) for value in values]
        return out
    dtype = np.dtype(DTYPES[type_])
    array = np.asarray(values)
    if array.dtype != dtype:
        if type_ in ("f32", "f64", "complex"):
            array = array.astype(dtype)
        else:
            converted = array.astype(dtype)
            if not np.array_equal(converted.astype(array.dtype, copy=False), array):
                raise CodecError(f"CODEC_VALUE_RANGE {type_}")
            array = converted
    return np.ascontiguousarray(array)


def encode_chunk(columns: Sequence[Column], start: int, stop: int) -> bytes:
    """Rows `[start, stop)` of the columns, in order, as one chunk."""
    parts: list[bytes] = []
    for column in columns:
        if column.type in TEXT_TYPES:
            for value in column.values[start:stop]:
                encoded = str(value).encode("utf-8", errors="strict")
                parts.append(struct.pack("<I", len(encoded)))
                parts.append(encoded)
        else:
            values = column.values
            if not isinstance(values, np.ndarray) or values.dtype != np.dtype(DTYPES[column.type]):
                raise CodecError(f"CODEC_COLUMN_TYPE {column.name}")
            parts.append(np.ascontiguousarray(values[start:stop]).tobytes())
        if column.valid is not None:
            if len(column.valid) < stop:
                raise CodecError("CODEC_LENGTH")
            parts.append(np.ascontiguousarray(column.valid[start:stop], dtype="u1").tobytes())
    return b"".join(parts)


def decode_chunk(layouts: Sequence[Layout], data: bytes | bytearray | memoryview, rows: int) -> list[Column]:
    """One chunk of `rows` rows. `CODEC_TRUNCATED` on short input, `CODEC_TRAILING` on extra bytes."""
    view = memoryview(data).cast("B")
    offset = 0

    def take(length: int) -> memoryview:
        nonlocal offset
        if offset + length > len(view):
            raise CodecError("CODEC_TRUNCATED")
        out = view[offset : offset + length]
        offset += length
        return out

    columns: list[Column] = []
    for layout in layouts:
        if layout.type in TEXT_TYPES:
            texts: list[str] = []
            for _ in range(rows):
                (length,) = struct.unpack("<I", take(4))
                try:
                    texts.append(bytes(take(length)).decode("utf-8", errors="strict"))
                except UnicodeDecodeError as error:
                    raise CodecError("CODEC_UTF8") from error
            values: np.ndarray | list[str] = texts
        elif layout.type in DTYPES:
            dtype = np.dtype(DTYPES[layout.type])
            values = np.frombuffer(take(dtype.itemsize * rows), dtype=dtype, count=rows).copy()
        else:
            raise CodecError(f"CODEC_FIELD_TYPE {layout.type}")
        valid = np.frombuffer(take(rows), dtype="u1").copy() if layout.nullable else None
        columns.append(Column(layout.name, layout.type, values, valid))
    if offset != len(view):
        raise CodecError("CODEC_TRAILING")
    return columns


def concat_columns(parts: Sequence[Sequence[Column]], layouts: Sequence[Layout]) -> list[Column]:
    """Whole columns from decoded chunks, in layout order."""
    out: list[Column] = []
    for index, layout in enumerate(layouts):
        pieces = [chunk[index] for chunk in parts]
        if layout.type in TEXT_TYPES:
            values: np.ndarray | list[str] = [text for piece in pieces for text in piece.values]
        elif pieces:
            values = np.concatenate([piece.values for piece in pieces])  # type: ignore[arg-type]
        else:
            values = np.empty(0, dtype=DTYPES[layout.type])
        valid = None
        if layout.nullable:
            valid = (
                np.concatenate([piece.valid for piece in pieces if piece.valid is not None])
                if pieces
                else np.empty(0, dtype="u1")
            )
        out.append(Column(layout.name, layout.type, values, valid))
    return out
