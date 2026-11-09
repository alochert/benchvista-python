"""Building manifests, testing steps, and the codes the author meets first."""

import json
import shutil
import textwrap
from pathlib import Path

import pytest

from benchvista import Choice, Digital, Fields, Quantity, Series, Step, Table, Text, Tick
from benchvista.cli import main
from benchvista.manifest import ManifestError, build_manifest, load_step_class, manifest_hash, source_files
from benchvista.text import revision

EXAMPLE = Path(__file__).parents[1] / "examples" / "glitch-finder"
FILES = [{"path": "step.py", "bytes": 1, "sha256": "0" * 64}]


def test_the_example_builds_to_its_committed_manifest_and_hash():
    step, entry = load_step_class(EXAMPLE, None)
    manifest = build_manifest(step, entry=entry, files=source_files(EXAMPLE))
    assert manifest == json.loads((EXAMPLE / "step.json").read_text("utf-8"))
    assert manifest_hash(manifest) == (EXAMPLE / "step.sha256").read_text("utf-8").strip()
    # Fixtures are tests, not the step's code: only step.py is hashed.
    assert [file["path"] for file in manifest["source"]["files"]] == ["step.py"]


def test_drafts_name_their_source_revision_and_go_stale_when_it_changes():
    step, entry = load_step_class(EXAMPLE, None)
    manifest = build_manifest(step, entry=entry, files=FILES)
    name = manifest["text"]["messages"]["name"]
    assert manifest["text"]["drafts"]["de"]["name"] == {"text": "Glitch-Sucher", "source": revision(name), "provenance": "author"}
    changed = {**name, "en": "Glitch detector"}
    assert revision(changed) != revision(name)


def test_the_hash_covers_the_source_and_the_params():
    step, entry = load_step_class(EXAMPLE, None)
    base = build_manifest(step, entry=entry, files=FILES)
    other_source = build_manifest(step, entry=entry, files=[{**FILES[0], "sha256": "1" * 64}])
    assert manifest_hash(base) != manifest_hash(other_source)

    class Wider(step):
        """Finds pulses shorter than a limit on digital lines."""

        params = {**step.params, "shortest": Quantity("time", "s", default=2e-6, minimum=0, label="Shortest valid pulse")}

    assert manifest_hash(build_manifest(Wider, entry=entry, files=FILES)) != manifest_hash(base)


def declared(**overrides):
    attributes = {
        "__doc__": "Measures nothing.",
        "id": "probe",
        "version": "1.0.0",
        "inputs": {"lines": Series(fields=Digital())},
        "outputs": {"rows": Table(start=Tick(), note="text")},
        "params": {},
        **overrides,
    }
    return type("Probe", (Step,), attributes)


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"params": {"limit": Quantity("time", "V", label="Limit")}}, "STEP_MANIFEST_UNIT_QUANTITY"),
        ({"params": {"limit": Quantity("time", "s", default=5, maximum=1, label="Limit")}}, "STEP_MANIFEST_BOUNDS"),
        ({"params": {"pick": Choice(["a", "b"], default="c", label="Pick")}}, "STEP_MANIFEST_DEFAULT"),
        ({"params": {"lines": Fields(of="nothing", label="Lines")}}, "STEP_MANIFEST_INPUT_REF"),
        ({"errors": {"PYTHON_BROKE": "It broke."}}, "STEP_MANIFEST_RESERVED_ERROR"),
        ({"name": Text("Probe for UART", tokens=["SPI"])}, "STEP_MANIFEST_TEXT_TOKEN"),
        ({"outputs": {}}, "STEP_MANIFEST_NO_OUTPUT"),
        ({"__doc__": None}, "STEP_MANIFEST_TEXT_MISSING"),
        ({"translations": {"de": {"param.none.label": "Nichts"}}}, "STEP_MANIFEST_DRAFT_SOURCE"),
        ({"translations": {"xx": {"name": "x"}}}, "STEP_MANIFEST_LOCALE"),
        ({"version": "1.0"}, "STEP_MANIFEST_INVALID"),
    ],
)
def test_a_bad_declaration_is_refused_with_its_code(overrides, code):
    with pytest.raises(ManifestError) as refused:
        build_manifest(declared(**overrides), entry="step:Probe", files=FILES)
    assert refused.value.code == code


def test_a_tick_needs_a_clock_when_no_single_series_input_gives_one():
    step = declared(inputs={"a": Series(fields=Digital()), "b": Series(fields=Digital())})
    with pytest.raises(ManifestError) as refused:
        build_manifest(step, entry="step:Probe", files=FILES)
    assert refused.value.code == "STEP_MANIFEST_DECLARATION"


def test_step_build_writes_the_manifest_and_step_test_passes_the_example(tmp_path, capsys):
    copy = tmp_path / "glitch-finder"
    shutil.copytree(EXAMPLE, copy)
    (copy / "step.json").unlink()
    assert main(["step", "build", str(copy)]) == 0
    assert json.loads((copy / "step.json").read_text("utf-8")) == json.loads((EXAMPLE / "step.json").read_text("utf-8"))
    assert main(["step", "test", str(copy)]) == 0
    out = capsys.readouterr().out
    assert out.count("pass ") == 5 and "FAIL" not in out


IMPURE = '''
import time

from benchvista import Scalar, Series, Real, Step


class Impure(Step):
    """Reads the clock, which a pure step must not."""

    id = "impure"
    version = "1.0.0"
    inputs = {"analog": Series(fields=Real())}
    outputs = {"now": Scalar(type="i64")}

    def run(self, ctx, analog, params):
        ctx.scalar("now", time.perf_counter_ns())
'''


def test_step_test_fails_a_pure_step_that_does_not_reproduce(tmp_path, capsys):
    folder = tmp_path / "impure"
    (folder / "fixtures" / "one").mkdir(parents=True)
    (folder / "step.py").write_text(textwrap.dedent(IMPURE), "utf-8")
    source = Path(__file__).parent / "fixtures" / "datasets"
    for path in source.glob("analog.*"):
        shutil.copy(path, folder / "fixtures" / "one" / path.name)
    (folder / "fixtures" / "one" / "case.json").write_text(json.dumps({"inputs": {"analog": "analog"}}), "utf-8")
    assert main(["step", "test", str(folder)]) == 1
    assert "not reproducible" in capsys.readouterr().out


UNDECLARED = '''
from benchvista import Series, Real, Step, Table, StepError


class Fails(Step):
    """Raises a code it never declared."""

    id = "fails"
    version = "1.0.0"
    inputs = {"analog": Series(fields=Real())}
    outputs = {"rows": Table(value="f64")}
    errors = {"TOO_QUIET": "The signal is too quiet to measure."}

    def run(self, ctx, analog, params):
        raise StepError("{code}")
'''


@pytest.mark.parametrize("code, expected", [("TOO_QUIET", "TOO_QUIET"), ("SURPRISE", "PYTHON_STEP_FAILED")])
def test_a_step_error_keeps_its_code_only_when_declared(tmp_path, capsys, code, expected):
    folder = tmp_path / "fails"
    (folder / "fixtures" / "one").mkdir(parents=True)
    (folder / "step.py").write_text(textwrap.dedent(UNDECLARED).replace("{code}", code), "utf-8")
    for path in (Path(__file__).parent / "fixtures" / "datasets").glob("analog.*"):
        shutil.copy(path, folder / "fixtures" / "one" / path.name)
    case = {"inputs": {"analog": "analog"}, "error": expected}
    (folder / "fixtures" / "one" / "case.json").write_text(json.dumps(case), "utf-8")
    assert main(["step", "test", str(folder)]) == 0
    assert f"failed as expected with {expected}" in capsys.readouterr().out


WRONG_WIDTH = '''
from benchvista import Series, Real, Step, Table


class Wrong(Step):
    """Emits text into a number column."""

    id = "wrong"
    version = "1.0.0"
    inputs = {"analog": Series(fields=Real())}
    outputs = {"rows": Table(value="i32")}

    def run(self, ctx, analog, params):
        ctx.emit("rows", value=2**40)
'''


def test_an_output_value_that_does_not_fit_its_field_fails_the_run(tmp_path, capsys):
    folder = tmp_path / "wrong"
    (folder / "fixtures" / "one").mkdir(parents=True)
    (folder / "step.py").write_text(textwrap.dedent(WRONG_WIDTH), "utf-8")
    for path in (Path(__file__).parent / "fixtures" / "datasets").glob("analog.*"):
        shutil.copy(path, folder / "fixtures" / "one" / path.name)
    case = {"inputs": {"analog": "analog"}, "error": "PYTHON_OUTPUT_VALUE"}
    (folder / "fixtures" / "one" / "case.json").write_text(json.dumps(case), "utf-8")
    assert main(["step", "test", str(folder)]) == 0
