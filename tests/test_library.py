"""Library items: the package file, version hashes, Spark Plug installs and the simulated Spark.

`fixtures/library.json` was written by `node scripts/library/fixtures.ts` with the product's own
code: the version hashes and device packages here must be the same bytes.
"""

import copy
import json
import shutil
from pathlib import Path

import pytest

from benchvista import cli
from benchvista import spark_plug
from benchvista.library import LibraryError, check_package, parse_package, read_version
from benchvista.spark_plug import SparkPlugError, simulate, task_package

EXAMPLES = Path(__file__).parents[1] / "examples"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "library.json").read_text("utf-8"))


def package(item: str) -> dict:
    return json.loads((EXAMPLES / item / "benchvista.json").read_text("utf-8"))


@pytest.mark.parametrize("expected", FIXTURE["versions"], ids=lambda v: v["item"])
def test_version_hash_and_file_list_match_typescript(expected):
    version = read_version(EXAMPLES / expected["item"])
    assert version.files == expected["files"]
    assert version.sha256 == expected["sha256"]


@pytest.mark.parametrize("install", FIXTURE["installs"], ids=lambda i: f"{i['item']}-{i['board']}")
def test_installed_spark_plug_is_the_package_typescript_writes(install):
    version = read_version(EXAMPLES / install["item"])
    source = (EXAMPLES / install["item"] / version.package["entry"]).read_text("utf-8")
    task = task_package(version.package, version.sha256, source, install["pins"], install["settings"])
    assert task == install["task"]


def refusal(edit) -> str:
    item = package("pwm-adc-sweep")
    edit(item)
    with pytest.raises(LibraryError) as raised:
        check_package(item)
    return raised.value.code


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda p: p["pins"].append(dict(p["pins"][0])), "LIBRARY_PACKAGE_DUPLICATE"),
        (lambda p: p["pins"][1].update(needs="pwm"), "LIBRARY_PLUG_PIN_NEEDS"),
        (lambda p: p["settings"]["samples"].update(default=64), "LIBRARY_PLUG_SETTING_DEFAULT"),
        (lambda p: p["settings"]["settle"].update(minimum=2000), "LIBRARY_PLUG_SETTING_BOUNDS"),
        (lambda p: p["settings"]["settle"].update(unit="Hz"), "LIBRARY_PLUG_SETTING_UNIT"),
        (lambda p: p.update(translations={"nb": {"role.CLK": "Klokke"}}), "LIBRARY_PACKAGE_TRANSLATION"),
        (lambda p: p.update(translations={"sv": {"name": "Svep"}}), "LIBRARY_PACKAGE_LOCALE"),
        (lambda p: p.update(sparkTaskApi=2), "LIBRARY_PLUG_TASK_API"),
        (lambda p: p.update(entry="main.mpy"), "LIBRARY_PACKAGE_ENTRY"),
        (lambda p: p.update(entry="../main.py"), "LIBRARY_PATH"),
        (lambda p: p.update(name="x" * 49), "LIBRARY_PACKAGE_TEXT"),
        (lambda p: p.update(homepage="https://example.com"), "LIBRARY_PACKAGE_UNKNOWN_FIELD"),
        (lambda p: p.pop("timeoutMs"), "LIBRARY_PACKAGE_MISSING_FIELD"),
    ],
)
def test_package_refusals(edit, code):
    assert refusal(edit) == code


def test_an_analysis_step_package_has_no_spark_fields():
    item = package("glitch-finder")
    assert check_package(copy.deepcopy(item)) == item
    item["pins"] = []
    with pytest.raises(LibraryError) as raised:
        check_package(item)
    assert raised.value.code == "LIBRARY_PACKAGE_UNKNOWN_FIELD"


@pytest.mark.parametrize("text", [b'\xef\xbb\xbf{"kind": "spark-plug"}', b'{"kind": NaN}', b"{"])
def test_package_file_must_be_plain_json(text):
    with pytest.raises(LibraryError) as raised:
        parse_package(text)
    assert raised.value.code == "LIBRARY_PACKAGE_JSON"


def copy_item(tmp_path: Path, item: str) -> Path:
    target = tmp_path / item
    shutil.copytree(EXAMPLES / item, target, ignore=shutil.ignore_patterns("__pycache__", "build"))
    return target


def test_local_and_generated_files_are_not_part_of_a_version(tmp_path):
    folder = copy_item(tmp_path, "gpio-loopback")
    before = read_version(folder).sha256
    (folder / ".DS_Store").write_bytes(b"\0")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\0")
    (folder / "build").mkdir()
    (folder / "build" / "release.json").write_text("{}")
    assert read_version(folder).sha256 == before
    (folder / "README.md").write_text((folder / "README.md").read_text() + " ")
    assert read_version(folder).sha256 != before


def test_a_link_in_an_item_is_refused(tmp_path):
    folder = copy_item(tmp_path, "gpio-loopback")
    (folder / "linked.md").symlink_to(folder / "README.md")
    with pytest.raises(LibraryError) as raised:
        read_version(folder)
    assert raised.value.code == "LIBRARY_FILE_LINK"


def test_header_refuses_settings_outside_the_declaration():
    item = package("pwm-adc-sweep")
    pins = {"OUT": "J1.4", "IN": "J1.5"}
    for settings, code in (
        ({"samples": 33}, "LIBRARY_PLUG_SETTING_RANGE"),
        ({"samples": 2.5}, "LIBRARY_PLUG_SETTING_VALUE"),
        ({"samples": True}, "LIBRARY_PLUG_SETTING_VALUE"),
        ({"speed": 1}, "LIBRARY_PLUG_SETTING_UNKNOWN"),
    ):
        with pytest.raises(SparkPlugError) as raised:
            task_package(item, "0" * 64, "def main(spark):\n    pass\n", pins, settings)
        assert raised.value.code == code
    with pytest.raises(SparkPlugError) as raised:
        task_package(item, "0" * 64, "def main(spark):\n    pass\n", {"OUT": "J1.4"})
    assert raised.value.code == "LIBRARY_PLUG_PIN_UNBOUND"


LOOPBACK = package("gpio-loopback")


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ('def main(spark):\n    spark.write("J1.4", 1)\n', "SPARK_SCRIPT_PIN_PERMISSION"),
        ('def main(spark):\n    spark.write(PINS["IN"], 1)\n', "SPARK_SCRIPT_PIN_PERMISSION"),
        ('def main(spark):\n    spark.pwm(PINS["OUT"], 1000, 100)\n', "LIBRARY_PLUG_PIN_NEEDS"),
        ("import time\ndef main(spark):\n    pass\n", "SPARK_SCRIPT_UNSUPPORTED_TOKEN"),
        ('def main(spark):\n    print(f"{1}")\n', "SPARK_SCRIPT_FSTRING_UNSUPPORTED"),
        ("def main(spark):\n    while True:\n        yield spark.sleep_ms(1000)\n", "SPARK_SCRIPT_TIMEOUT"),
        ("def main(spark):\n    yield 70000\n", "SPARK_INTEGER"),
        ("def main(spark):\n    open('x')\n", "SPARK_SCRIPT_UNSUPPORTED_TOKEN"),
        ("def main(spark):\n    return len(globals)\n", "SPARK_SCRIPT_UNSUPPORTED_TOKEN"),
        ("def main(spark):\n    return __name__\n", "SPARK_SCRIPT_UNSUPPORTED_TOKEN"),
        ("def main(spark):\n    return sorted([2, 1])\n", "SPARK_SCRIPT_EXCEPTION"),
    ],
)
def test_simulation_faults_as_the_board_would(source, code):
    run = simulate(LOOPBACK, source)
    assert run.state == "fault"
    assert run.error.split(" ")[0] == code


def test_a_step_that_never_yields_is_stopped(monkeypatch):
    monkeypatch.setattr(spark_plug, "STEP_SECONDS", 0.2)
    run = simulate(LOOPBACK, "def main(spark):\n    while True:\n        pass\n")
    assert (run.state, run.error) == ("fault", "SPARK_SCRIPT_STEP_BUDGET")


def test_simulated_inputs_follow_wiring_and_readings():
    source = (EXAMPLES / "gpio-loopback" / "main.py").read_text("utf-8")
    assert simulate(LOOPBACK, source, wiring={"IN": "OUT"}).output.endswith("PASS mismatches 0\n")
    stuck_high = simulate(LOOPBACK, source, readings={"IN": [1]})
    assert stuck_high.output.endswith("FAIL mismatches 10\n")


def run_cli(*args: str) -> int:
    return cli.main(list(args))


def test_check_refuses_an_undeclared_role_and_a_mismatched_step(tmp_path, capsys):
    folder = copy_item(tmp_path, "gpio-loopback")
    main = folder / "main.py"
    main.write_text(main.read_text().replace('PINS["IN"]', 'PINS["INPUT"]'))
    assert run_cli("check", str(folder)) == 2
    assert "LIBRARY_PLUG_PIN_UNKNOWN" in capsys.readouterr().err
    step = copy_item(tmp_path, "glitch-finder")
    item = json.loads((step / "benchvista.json").read_text())
    item["version"] = "1.1.0"
    (step / "benchvista.json").write_text(json.dumps(item))
    assert run_cli("check", str(step)) == 2
    assert "LIBRARY_STEP_IDENTITY" in capsys.readouterr().err


def test_check_refuses_extra_source_a_board_would_not_receive(tmp_path, capsys):
    folder = copy_item(tmp_path, "gpio-loopback")
    (folder / "helpers.py").write_text("X = 1\n")
    assert run_cli("check", str(folder)) == 2
    assert "LIBRARY_PLUG_EXTRA_SOURCE" in capsys.readouterr().err


def test_new_spark_plug_passes_its_own_test_and_builds(tmp_path, capsys):
    folder = tmp_path / "blink-led"
    assert run_cli("new", "spark-plug", str(folder)) == 0
    assert run_cli("test", str(folder)) == 0
    assert run_cli("build", str(folder)) == 0
    release = json.loads((folder / "build" / "release.json").read_text())
    assert release["sha256"] == read_version(folder).sha256
    assert release["package"]["id"] == "blink-led"
    checksums = (folder / "build" / "checksums.txt").read_text().splitlines()
    assert [line.split("  ")[1] for line in checksums] == ["README.md", "benchvista.json", "main.py", "tests/defaults.json"]
    assert run_cli("new", "spark-plug", str(folder)) == 2
    assert "LIBRARY_NEW_NOT_EMPTY" in capsys.readouterr().err


def test_new_analysis_step_checks_and_builds_with_its_manifest(tmp_path):
    folder = tmp_path / "field-range"
    assert run_cli("new", "analysis-step", str(folder)) == 0
    assert run_cli("check", str(folder)) == 0
    assert run_cli("build", str(folder)) == 0
    release = json.loads((folder / "build" / "release.json").read_text())
    assert release["step"]["manifest"]["id"] == "field-range"
    assert release["step"]["manifest"]["format"] == "benchvista-python-step-v1"


@pytest.mark.parametrize("item", ["console-messages", "gpio-loopback", "pwm-adc-sweep", "glitch-finder"])
def test_examples_pass_their_own_tests(item):
    assert run_cli("test", str(EXAMPLES / item)) == 0
