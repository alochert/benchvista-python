"""Datasets in `benchvista-dataset-v1`, read and written as `src/analysis/core/dataset.ts` does.

A dataset is a manifest (type, axis extent, clocks, quality intervals, provenance, hashed
chunk descriptors) and its `columnar-le/v1` chunks. Its id is the SHA-256 of the canonical JSON
of its content, which excludes provenance and profile, so the same data built here and in
TypeScript has the same id. Chunks hold 65,536 rows, as TypeScript's default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .canonical import hash_id, sha256_hex
from .columnar import Column, Layout, concat_columns, decode_chunk, encode_chunk

DATASET_FORMAT = "benchvista-dataset-v1"
DEFAULT_CHUNK_ROWS = 65_536


class DatasetError(ValueError):
    """A dataset that breaks its manifest (`DATASET_*`)."""


def stored_fields(data_type: dict) -> list[dict]:
    """The fields whose columns a dataset stores, axis column first for points and explicit axes."""
    kind = data_type["kind"]
    if kind == "series":
        axis = data_type["axis"]
        if axis["kind"] == "points":
            tick = {"name": axis["column"], "type": "tick", "nullable": False, "clock": axis["clock"], "quantity": "time"}
            return [tick, *data_type["fields"]]
        if axis["kind"] == "explicit":
            column = {
                "name": axis["column"],
                "type": "f64",
                "nullable": False,
                "quantity": axis["quantity"],
                "unit": axis["unit"],
            }
            return [column, *data_type["fields"]]
        return list(data_type["fields"])
    if kind == "table":
        return list(data_type["fields"])
    return []


def layouts(data_type: dict) -> list[Layout]:
    return [Layout(f["name"], f["type"], bool(f["nullable"])) for f in stored_fields(data_type)]


@dataclass
class Dataset:
    manifest: dict
    columns: list[Column]
    chunks: list[bytes] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.manifest["id"])

    @property
    def type(self) -> dict:
        return self.manifest["type"]

    @property
    def rows(self) -> int:
        return int(self.manifest["rows"])

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise DatasetError(f"DATASET_COLUMN_MISSING {name}")


def _content(manifest: dict) -> dict:
    scalar = manifest.get("scalar")
    return {
        "format": manifest["format"],
        "type": manifest["type"],
        "extent": manifest.get("extent"),
        "rows": manifest["rows"],
        "clocks": manifest["clocks"],
        "quality": manifest["quality"],
        "scalar": scalar,
        "blob": manifest.get("blob"),
        "sealed": manifest["sealed"],
        "chunks": [chunk["sha256"] for chunk in manifest["chunks"]],
    }


def content_id(manifest: dict) -> str:
    """A manifest's content identity, independent of its provenance and profile."""
    return hash_id(_content(manifest))


def _check_column(spec: dict, column: Column, rows: int) -> None:
    if column.type != spec["type"]:
        raise DatasetError(f"DATASET_COLUMN_TYPE {spec['name']}")
    if len(column.values) != rows:
        raise DatasetError(f"DATASET_COLUMN_LENGTH {spec['name']}")
    if spec["nullable"]:
        if column.valid is None or len(column.valid) != rows:
            raise DatasetError(f"DATASET_COLUMN_VALIDITY {spec['name']}")
        if np.any(column.valid > 1):
            raise DatasetError(f"DATASET_COLUMN_VALIDITY {spec['name']}")
    elif column.valid is not None:
        raise DatasetError(f"DATASET_COLUMN_VALIDITY {spec['name']}")
    values = column.values
    if column.type == "tri" and isinstance(values, np.ndarray) and np.any(values > 2):
        raise DatasetError("DATASET_TRI_VALUE")
    if column.type == "bool" and isinstance(values, np.ndarray) and np.any(values > 1):
        raise DatasetError("DATASET_BOOL_VALUE")
    if column.type == "enum" and isinstance(values, np.ndarray):
        if np.any(values >= len(spec.get("values", []))):
            raise DatasetError("DATASET_ENUM_VALUE")
    if column.type in ("f32", "f64", "complex") and isinstance(values, np.ndarray):
        bad = ~np.isfinite(values) if column.type != "complex" else ~(np.isfinite(values.real) & np.isfinite(values.imag))
        if column.valid is not None:
            bad &= column.valid == 1
        if np.any(bad):
            raise DatasetError("DATASET_NON_FINITE")


def build(
    data_type: dict,
    columns: dict[str, Column],
    rows: int,
    *,
    extent: dict | None = None,
    clocks: list[dict] | None = None,
    quality: list[dict] | None = None,
    scalar: Any = None,
    provenance: dict | None = None,
    profile: str | None = None,
    sealed: bool = True,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
) -> Dataset:
    """Assemble a dataset: every column checked against its field, chunked, hashed and identified."""
    specs = stored_fields(data_type)
    if set(columns) != {spec["name"] for spec in specs}:
        raise DatasetError("DATASET_COLUMN_SET")
    ordered = []
    for spec in specs:
        column = columns[spec["name"]]
        _check_column(spec, column, rows)
        ordered.append(Column(spec["name"], column.type, column.values, column.valid))
    if data_type["kind"] == "series":
        axis = data_type["axis"]
        if extent is None or extent["kind"] != axis["kind"]:
            raise DatasetError("DATASET_EXTENT_AXIS")
        if axis["kind"] == "uniform" and int(extent["count"]) != rows:
            raise DatasetError("DATASET_UNIFORM_COUNT")
        if axis["kind"] == "points":
            ticks = columns[axis["column"]].values
            assert isinstance(ticks, np.ndarray)
            start, end = int(extent["start"]), int(extent["end"])
            if end < start:
                raise DatasetError("DATASET_POINTS_RANGE")
            if rows and (int(ticks.min()) < start or int(ticks.max()) >= end):
                raise DatasetError("DATASET_POINTS_BOUNDS")
            if rows > 1 and not np.all(np.diff(ticks) > 0):
                raise DatasetError("DATASET_POINTS_ORDER")
        clock_ids = {clock["id"] for clock in clocks or []}
        if "clock" in axis and axis["clock"] not in clock_ids:
            raise DatasetError("DATASET_AXIS_CLOCK")
    elif extent is not None:
        raise DatasetError("DATASET_EXTENT_KIND")
    chunks: list[bytes] = []
    descriptors: list[dict] = []
    for start in range(0, rows, chunk_rows):
        stop = min(rows, start + chunk_rows)
        data = encode_chunk(ordered, start, stop)
        chunks.append(data)
        descriptors.append(
            {
                "sequence": len(descriptors),
                "rows": stop - start,
                "encoding": "columnar-le/v1",
                "bytes": len(data),
                "sha256": sha256_hex(data),
            }
        )
    manifest: dict = {
        "format": DATASET_FORMAT,
        "type": data_type,
        "rows": rows,
        "clocks": list(clocks or []),
        "quality": list(quality or []),
        "sealed": sealed,
        "chunks": descriptors,
        "provenance": provenance or {"origin": {"kind": "derived"}, "producedBy": None, "notes": []},
    }
    if extent is not None:
        manifest["extent"] = extent
    if data_type["kind"] == "scalar":
        if isinstance(scalar, float) and not math.isfinite(scalar):
            raise DatasetError("DATASET_NON_FINITE")
        manifest["scalar"] = scalar
    if profile is not None:
        manifest["profile"] = profile
    manifest["id"] = content_id(manifest)
    return Dataset(manifest, ordered, chunks)


def read(manifest: dict, chunks: list[bytes]) -> Dataset:
    """A dataset from its manifest and chunk bytes, every chunk and the id verified."""
    if manifest.get("format") != DATASET_FORMAT:
        raise DatasetError("DATASET_FORMAT")
    descriptors = manifest["chunks"]
    if len(chunks) != len(descriptors):
        raise DatasetError("DATASET_CHUNK_COUNT")
    parts = []
    shapes = layouts(manifest["type"])
    for descriptor, data in zip(descriptors, chunks):
        if len(data) != descriptor["bytes"]:
            raise DatasetError("DATASET_CHUNK_BYTES")
        if sha256_hex(data) != descriptor["sha256"]:
            raise DatasetError("DATASET_CHUNK_HASH")
        parts.append(decode_chunk(shapes, data, descriptor["rows"]))
    columns = concat_columns(parts, shapes)
    if content_id(manifest) != manifest["id"]:
        raise DatasetError("DATASET_ID_MISMATCH")
    return Dataset(manifest, columns, list(chunks))
