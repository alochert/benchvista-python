"""Library items: the package file, version hashes, Spark Plug installs and the simulated Spark.

`fixtures/library.json` was written by `node scripts/library/fixtures.ts` with the product's own
code: the version hashes and device packages here must be the same bytes.
"""

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from benchvista import cli
from benchvista import spark_driver
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


def example_drivers(item: dict) -> dict:
    """The Library drivers a Spark Plug names, from the example folders."""
    held, missing = spark_driver.find_drivers(item, [EXAMPLES])
    assert missing == []
    return held


@pytest.mark.parametrize("install", FIXTURE["installs"], ids=lambda i: f"{i['item']}-{i['board']}")
def test_installed_spark_plug_is_the_package_typescript_writes(install):
    version = read_version(EXAMPLES / install["item"])
    source = (EXAMPLES / install["item"] / version.package["entry"]).read_text("utf-8")
    task = task_package(version.package, version.sha256, source, install["pins"], install["settings"], example_drivers(version.package))
    assert task == install["task"]


@pytest.mark.parametrize("sample", FIXTURE["driverSources"], ids=lambda s: s["name"])
def test_the_driver_contract_gives_typescripts_verdict(sample):
    problem = spark_driver.source_problem(sample["source"])
    said = None if problem is None else ({"code": problem[0], "token": problem[1]} if problem[1] else {"code": problem[0]})
    assert said == sample["problem"]


def test_a_library_driver_is_written_into_the_header_and_read_back():
    install = next(i for i in FIXTURE["installs"] if i["item"] == "oled-seconds")
    ((told, text),) = spark_driver.embedded(install["task"]["source"])
    assert text == (EXAMPLES / "sh1106" / "driver.py").read_text("utf-8")
    assert told["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert (told["name"], told["version"], told["bus"], told["kind"], told["colour"]) == ("sh1106", "1.0.0", "i2c", "display", False)
    # The header holds no empty line before the author's source, and a block below it is not read.
    header = install["task"]["source"].split("\n\n", 1)[0]
    assert "#: end" in header and "def main" not in header
    assert spark_driver.embedded("PINS = {}\n\n#: driver {}\n#|x\n#: end\n") == []


def driver_refusal(edit) -> str:
    item = package("sh1106")
    edit(item)
    with pytest.raises(LibraryError) as raised:
        check_package(item)
    return raised.value.code


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda p: p.pop("addresses"), "LIBRARY_DRIVER_BUS"),
        (lambda p: p.update(bus="spi", hz=1_000_000), "LIBRARY_DRIVER_BUS"),
        (lambda p: p.update(hz=200_000), "LIBRARY_DRIVER_BUS"),
        (lambda p: p.update(mode=0), "LIBRARY_DRIVER_BUS"),
        (lambda p: p.update(lines={"CS": "required"}), "LIBRARY_DRIVER_BUS"),
        (lambda p: p.update(addresses=[60, 60]), "LIBRARY_PACKAGE_DUPLICATE"),
        (lambda p: p.pop("colour"), "LIBRARY_DRIVER_DEVICE"),
        (lambda p: p.pop("sizes"), "LIBRARY_DRIVER_DEVICE"),
        (lambda p: p.update(sizes=[[128, 32]]), "LIBRARY_DRIVER_DEVICE"),
        (lambda p: p.update(readings=["temperature_c"]), "LIBRARY_DRIVER_DEVICE"),
        (lambda p: p.update(device="sensor"), "LIBRARY_DRIVER_DEVICE"),
        (lambda p: p.update(sparkTaskApi=1), "LIBRARY_PLUG_TASK_API"),
        (lambda p: p.update(pins=[]), "LIBRARY_PACKAGE_UNKNOWN_FIELD"),
    ],
)
def test_driver_package_refusals(edit, code):
    assert driver_refusal(edit) == code


def test_a_driver_on_spi_says_its_speed_and_lines():
    item = package("sh1106")
    item.pop("addresses")
    item.update(bus="spi", hz=8_000_000, mode=3, lines={"CS": "required", "DC": "required", "RESET": "optional"})
    found = spark_driver.spec(check_package(item))
    assert [line["name"] for line in found["lines"]] == ["SCK", "MOSI", "CS", "DC", "RESET"]
    assert found["lines"][-1] == {"name": "RESET", "direction": "output", "optional": True}
    assert (found["hz"], found["mode"]) == (8_000_000, 3)
    told = spark_driver.record(item, "0" * 64)
    assert list(told) == ["name", "version", "sha256", "bus", "kind", "hz", "mode", "lines", "settings", "sizes", "colour"]


def test_a_spark_plug_names_one_exact_driver_version():
    item = package("oled-seconds")
    drivers = example_drivers(item)
    pins = {"DISPLAY.SCL": "J1.4", "DISPLAY.SDA": "J1.5"}
    source = "def main(spark):\n    pass\n"

    def refused(change=None, held=drivers) -> str:
        edited = copy.deepcopy(item)
        if change:
            change(edited["devices"][0])
        with pytest.raises(SparkPlugError) as raised:
            task_package(edited, "0" * 64, source, pins, None, held)
        return raised.value.code

    assert refused(held={}) == "LIBRARY_PLUG_DRIVER"
    assert refused(lambda d: d["driver"].update(sha256="0" * 64)) == "LIBRARY_PLUG_DRIVER"
    assert refused(lambda d: d["driver"].update(version="1.0.1")) == "LIBRARY_PLUG_DRIVER"
    assert refused(lambda d: d.update(address=0x76)) == "LIBRARY_PLUG_DEVICE_ADDRESS"
    assert refused(lambda d: d.update(settings={"height": 32})) == "LIBRARY_PLUG_DEVICE_SETTING"
    assert refused(lambda d: d.update(settings={"rotation": 0})) == "LIBRARY_PLUG_DEVICE_SETTING"
    # A driver and a Spark Plug share the board's 8,192 bytes.
    with pytest.raises(SparkPlugError) as raised:
        task_package(item, "0" * 64, source + "# filler\n" * 700, pins, None, drivers)
    assert raised.value.code == "LIBRARY_PLUG_SOURCE_SIZE"
    # One name is one driver on the board: not BenchVista's own and the Library's under one name.
    twice = copy.deepcopy(item)
    twice["devices"][0]["driver"]["id"] = "ssd1306"
    held = {key: spark_driver.LibraryDriver({**d.package, "id": "ssd1306"}, d.version_sha256, d.source, d.source_sha256) for key, d in drivers.items()}
    twice["devices"].append({"role": "OTHER", "driver": "ssd1306", "label": "Another display", "address": 0x3D})
    with pytest.raises(SparkPlugError) as raised:
        task_package(twice, "0" * 64, source, {**pins, "OTHER.SCL": "J1.4", "OTHER.SDA": "J1.5"}, None, held)
    assert raised.value.code == "LIBRARY_PLUG_DRIVER"


def test_a_driver_reference_is_checked_in_the_package():
    def refusal_of(change) -> str:
        item = package("oled-seconds")
        change(item["devices"][0])
        with pytest.raises(LibraryError) as raised:
            check_package(item)
        return raised.value.code

    assert refusal_of(lambda d: d["driver"].update(sha256="xyz")) == "LIBRARY_PACKAGE_SHAPE"
    assert refusal_of(lambda d: d["driver"].update(extra=1)) == "LIBRARY_PACKAGE_UNKNOWN_FIELD"
    assert refusal_of(lambda d: d["driver"].pop("version")) == "LIBRARY_PACKAGE_MISSING_FIELD"
    assert refusal_of(lambda d: d.update(address=3)) == "LIBRARY_PACKAGE_SHAPE"


def test_a_driver_case_reports_what_the_driver_sent():
    item = package("sh1106")
    source = (EXAMPLES / "sh1106" / "driver.py").read_text("utf-8")
    assert spark_driver.run_case(item, source, {}) == []
    problems = spark_driver.run_case(item, source, {"expect": {"writes": 1, "starts": "ff"}})
    assert len(problems) == 2 and "bus writes" in problems[0]
    # A sensor that cannot find its chip says so with the board's own code.
    sensor = {**package("sh1106"), "device": "sensor", "readings": ["temperature_c"]}
    for key in ("sizes", "colour", "settings"):
        sensor.pop(key)
    module = (
        "import struct\n\n\nclass Driver:\n    def __init__(self, bus, settings):\n        self.bus = bus\n"
        "        if bus.readfrom_mem(0xd0, 1) != b'\\x60':\n            raise ValueError('SPARK_DEVICE_IDENTITY')\n\n"
        "    def read(self):\n        return {'temperature_c': struct.unpack('>h', self.bus.readfrom_mem(0, 2))[0] / 256}\n"
    )
    assert spark_driver.run_case(sensor, module, {"expect": {"error": "SPARK_DEVICE_IDENTITY"}}) == []
    found = {"registers": {"208": "60", "0": "1900"}, "expect": {"reading": {"temperature_c": 25.0}}}
    assert spark_driver.run_case(sensor, module, found) == []
    wrong = spark_driver.run_case(sensor, module, {"registers": {"208": "60"}, "expect": {"reading": {"temperature_c": 25.0}}})
    assert wrong == ["reading temperature_c is 0.0, expected 25.0"]


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
        (lambda p: p.update(sparkTaskApi=3), "LIBRARY_PLUG_TASK_API"),
        (lambda p: p.update(entry="main.mpy"), "LIBRARY_PACKAGE_ENTRY"),
        (lambda p: p.update(entry="../main.py"), "LIBRARY_PATH"),
        (lambda p: p.update(name="x" * 49), "LIBRARY_PACKAGE_TEXT"),
        (lambda p: p.update(homepage="https://example.com"), "LIBRARY_PACKAGE_UNKNOWN_FIELD"),
        (lambda p: p.pop("timeoutMs"), "LIBRARY_PACKAGE_MISSING_FIELD"),
    ],
)
def test_package_refusals(edit, code):
    assert refusal(edit) == code


def device_refusal(edit) -> str:
    item = package("oled-counter")
    edit(item)
    with pytest.raises(LibraryError) as raised:
        check_package(item)
    return raised.value.code


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda p: p.update(sparkTaskApi=1), "LIBRARY_PLUG_TASK_API"),
        (lambda p: p["devices"][0].update(driver="sh1106"), "LIBRARY_PLUG_DRIVER"),
        (lambda p: p["devices"][0].update(address=0x76), "LIBRARY_PLUG_DEVICE_ADDRESS"),
        (lambda p: p["devices"][0].update(settings={"height": 48}), "LIBRARY_PLUG_DEVICE_SETTING"),
        (lambda p: p["devices"][0].update(settings={"contrast": 1}), "LIBRARY_PLUG_DEVICE_SETTING"),
        (lambda p: p["devices"][0].update(role="display"), "LIBRARY_PLUG_ROLE"),
        (lambda p: p["devices"].append(dict(p["devices"][0])), "LIBRARY_PACKAGE_DUPLICATE"),
        (lambda p: p["devices"].extend({"role": f"D{n}", "driver": "st7789", "label": "Display"} for n in range(3)), "LIBRARY_PLUG_PINS"),
    ],
)
def test_device_refusals(edit, code):
    assert device_refusal(edit) == code


def test_the_driver_catalogue_is_the_one_typescript_has():
    assert spark_plug.DRIVERS == FIXTURE["drivers"]


def test_a_display_size_must_be_one_of_its_panels():
    item = package("oled-counter")
    item["devices"] = [{"role": "TFT", "driver": "st7789", "label": "Colour display", "settings": {"width": 135}}]
    with pytest.raises(LibraryError) as raised:
        check_package(item)
    assert raised.value.code == "LIBRARY_PLUG_DEVICE_SETTING"
    item["devices"][0]["settings"]["height"] = 240
    check_package(item)


def test_a_device_is_a_display_through_its_driver_in_the_simulator():
    item = package("oled-counter")
    source = (EXAMPLES / "oled-counter" / "main.py").read_text("utf-8")
    run = simulate(item, source, settings={"count": 2, "interval": 100})
    assert run.state == "complete"
    assert run.frames["DISPLAY"] == [
        [(0, 0, "BenchVista"), (0, 16, "Count 1")],
        [(0, 0, "BenchVista"), (0, 16, "Count 2")],
    ]


def test_a_device_the_package_does_not_declare_is_refused():
    item = package("oled-counter")
    run = simulate(item, 'def main(spark):\n    spark.device("SENSOR")\n')
    assert (run.state, run.error) == ("fault", "SPARK_DEVICE_UNKNOWN")


def test_a_display_refuses_a_colour_it_cannot_show():
    item = package("oled-counter")
    run = simulate(item, 'def main(spark):\n    spark.device("DISPLAY").fill(2)\n')
    assert (run.state, run.error) == ("fault", "SPARK_DEVICE_ARGUMENT")


def test_a_sensor_reads_the_case_readings_in_turn():
    item = package("oled-counter")
    item["devices"] = [{"role": "AIR", "driver": "bme280", "label": "Air sensor"}]
    source = 'def main(spark):\n    for n in range(3):\n        print(spark.device("AIR").read()["temperature_c"])\n'
    run = simulate(item, source, settings={"count": 1, "interval": 100}, devices={"AIR": {"temperature_c": [21.5, 22.0]}})
    assert run.output == "21.5\n22.0\n22.0\n"


def test_a_raw_bus_needs_declared_pads():
    item = package("oled-counter")
    run = simulate(item, 'def main(spark):\n    spark.i2c("J1.4", "J1.5")\n')
    assert (run.state, run.error) == ("fault", "SPARK_SCRIPT_PIN_PERMISSION")
    run = simulate(item, 'def main(spark):\n    spark.i2c(DEVICES["DISPLAY"]["pins"]["SCL"], DEVICES["DISPLAY"]["pins"]["SDA"])\n')
    assert run.state == "complete"


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


@pytest.mark.parametrize("item", ["console-messages", "gpio-loopback", "pwm-adc-sweep", "glitch-finder", "oled-counter", "sh1106", "oled-seconds"])
def test_examples_pass_their_own_tests(item):
    assert run_cli("test", str(EXAMPLES / item)) == 0


def test_new_spark_driver_passes_its_own_test_and_builds(tmp_path, capsys):
    folder = tmp_path / "room-sensor"
    assert run_cli("new", "spark-driver", str(folder)) == 0
    assert run_cli("check", str(folder)) == 0
    assert run_cli("test", str(folder)) == 0
    assert run_cli("build", str(folder)) == 0
    release = json.loads((folder / "build" / "release.json").read_text())
    assert (release["kind"], release["package"]["device"]) == ("spark-driver", "sensor")
    # A driver outside the contract is refused by name.
    (folder / "driver.py").write_text("import os\n" + (folder / "driver.py").read_text())
    capsys.readouterr()
    assert run_cli("check", str(folder)) == 2
    assert "LIBRARY_DRIVER_CONTRACT at driver.py import os" in capsys.readouterr().err


def test_a_spark_plug_needs_the_folder_of_the_driver_it_names(tmp_path, capsys):
    shutil.copytree(EXAMPLES / "oled-seconds", tmp_path / "plugs" / "oled-seconds")
    assert run_cli("check", str(tmp_path / "plugs" / "oled-seconds")) == 2
    assert "LIBRARY_PLUG_DRIVER at sh1106 1.0.0" in capsys.readouterr().err
    # Beside the Spark Plug, in a collection's spark-drivers folder, or in --drivers.
    shutil.copytree(EXAMPLES / "sh1106", tmp_path / "elsewhere" / "sh1106")
    assert run_cli("test", str(tmp_path / "plugs" / "oled-seconds"), "--drivers", str(tmp_path / "elsewhere")) == 0
    shutil.copytree(EXAMPLES / "sh1106", tmp_path / "plugs" / "sh1106")
    assert run_cli("check", str(tmp_path / "plugs" / "oled-seconds")) == 0
    # A changed driver is another version: the Spark Plug no longer finds the one it names.
    (tmp_path / "plugs" / "sh1106" / "README.md").write_text("changed\n")
    assert run_cli("check", str(tmp_path / "plugs" / "oled-seconds")) == 2
