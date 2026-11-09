"""Python against the TypeScript product: canonical JSON, chunks, dataset ids, output types.

Every fixture here was written by `node scripts/analysis/python-fixtures.ts` with the product's
own code; a test that fails means Python and TypeScript would disagree on bytes or identities.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from benchvista.canonical import CanonicalError, canonical, canonical_hex, js_number
from benchvista.columnar import CodecError, Layout, decode_chunk, encode_chunk
from benchvista.dataset import build, content_id, read
from benchvista.manifest import output_types

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = Path(__file__).parents[1] / "examples" / "glitch-finder"


def load(name: str):
    return json.loads((FIXTURES / name).read_text("utf-8"))


@pytest.mark.parametrize("case", load("canonical.json"), ids=lambda case: case["name"])
def test_canonical_json_matches_typescript(case):
    encoded = case["input"]
    # JavaScript has one number type: every number is a float, as here.
    value = float(encoded["number"]) if "number" in encoded else encoded["value"]
    assert canonical(value) == case["canonical"]
    assert canonical_hex(value) == case["sha256"]


def test_canonical_json_refuses_what_it_cannot_carry():
    for value in (float("nan"), float("inf"), 2**53, "\ud800"):
        with pytest.raises(CanonicalError):
            canonical(value)
    assert js_number(1.0) == "1" and js_number(-0.0) == "0" and js_number(1e-7) == "1e-7"


def decoded_values(column):
    if column.type in ("utf8", "json"):
        return list(column.values)
    if column.type == "complex":
        return [[value.real, value.imag] for value in column.values]
    if column.type in ("i64", "u64", "tick"):
        return [str(int(value)) for value in column.values]
    return ["-0" if (value == 0 and np.signbit(value)) else value.item() for value in column.values]


@pytest.mark.parametrize("entry", load("chunks.json")["columns"], ids=lambda entry: entry["layout"][0]["type"])
def test_each_field_type_decodes_and_encodes_byte_for_byte(entry):
    data = (FIXTURES / entry["file"]).read_bytes()
    layout = [Layout(**spec) for spec in entry["layout"]]
    (column,) = decode_chunk(layout, data, entry["rows"])
    expected = entry["values"]
    got = decoded_values(column)
    if column.type == "f32":
        expected = [value if value == "-0" else float(np.float32(value)) for value in expected]
    assert got == expected
    if entry["valid"] is None:
        assert column.valid is None
    else:
        assert column.valid.tolist() == entry["valid"]
    assert encode_chunk([column], 0, entry["rows"]) == data


def test_a_chunk_of_every_column_round_trips_and_refuses_short_and_long_input():
    spec = load("chunks.json")
    layouts = [Layout(**entry["layout"][0]) for entry in spec["columns"]]
    data = (FIXTURES / spec["all"]["file"]).read_bytes()
    columns = decode_chunk(layouts, data, spec["all"]["rows"])
    assert encode_chunk(columns, 0, spec["all"]["rows"]) == data
    with pytest.raises(CodecError, match="CODEC_TRUNCATED"):
        decode_chunk(layouts, data[:-1], spec["all"]["rows"])
    with pytest.raises(CodecError, match="CODEC_TRAILING"):
        decode_chunk(layouts, data + b"\0", spec["all"]["rows"])


def test_a_typescript_dataset_reads_and_rebuilds_to_the_same_id():
    folder = FIXTURES / "datasets"
    manifest = json.loads((folder / "analog.manifest.json").read_text("utf-8"))
    chunks = [(folder / f"analog.{i}.bin").read_bytes() for i in range(len(manifest["chunks"]))]
    dataset = read(manifest, chunks)
    assert content_id(manifest) == manifest["id"]
    assert dataset.rows == 70000 and len(chunks) == 2
    ch1 = dataset.column("CH1")
    assert ch1.valid is not None and int(ch1.valid.sum()) == 70000 - 71
    rebuilt = build(
        manifest["type"],
        {column.name: column for column in dataset.columns},
        dataset.rows,
        extent=manifest["extent"],
        clocks=manifest["clocks"],
        quality=manifest["quality"],
        provenance=manifest["provenance"],
    )
    assert rebuilt.id == manifest["id"]
    assert rebuilt.chunks == chunks
    tampered = bytearray(chunks[0])
    tampered[0] ^= 1
    with pytest.raises(ValueError, match="DATASET_CHUNK_HASH"):
        read(manifest, [bytes(tampered), chunks[1]])


@pytest.mark.parametrize("case", load("output-types.json"))
def test_output_types_resolve_as_the_planner_resolves_them(case):
    manifest = json.loads((EXAMPLE / "step.json").read_text("utf-8"))
    fields = [field["name"] for field in case["inputs"]["lines"]["fields"]]
    params = {**case["params"], "fields": case["params"].get("fields", fields)}
    assert output_types(manifest, case["inputs"], params) == case["outputs"]
