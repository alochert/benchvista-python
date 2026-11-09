"""The `benchvista` command (`docs/library.md` §6, `docs/analysis-python-steps.md` §15.3).

Library items, both kinds (a folder with `benchvista.json`):

    benchvista new spark-plug <dir>      start a Spark Plug: benchvista.json, main.py, README, a test
    benchvista new analysis-step <dir>   start an analysis step: benchvista.json, step.py, README
    benchvista check <dir>               check the package and source; print the version hash
    benchvista test <dir>                run the item's tests (a simulated Spark, or step fixtures)
    benchvista build <dir> [--out DIR]   write build/release.json and build/checksums.txt
    benchvista collection build <repo> --out <site>
                                         check, test and publish a collection repository's site

Exit status 0 on success, 1 when a test fails, 2 when the item is refused.

Analysis steps without a package file, as before:

    benchvista step build <dir> [--entry module:Class]
        Writes <dir>/step.json (the manifest) and <dir>/step.sha256 (its identity). Nothing of the
        step's `run` is called: the manifest is read from the class's declarations.

    benchvista step test <dir> [--entry module:Class]
        Runs every case in <dir>/fixtures/<case>/case.json, twice. A pure step must give the same
        output datasets both times, and a seeded step the same for the same seed. Every output is
        built against the types the planner resolves from the manifest, within the budget, and an
        `expect` in the case must match the output's dataset id exactly.

A step case is `{"params": {...}, "inputs": {"port": "<base>"}, "seed": 0, "expect": {"port": "sha256:..."}}`
with each input as `<base>.manifest.json` and `<base>.<sequence>.bin` beside it, the files
`node scripts/analysis/run.ts --out` writes. A Spark Plug case is `tests/<case>.json`:
`{"settings": {...}, "wiring": {"IN": "OUT"}, "readings": {"IN": [0, 1]}, "expect": {...}}`, where
`expect` may give `state`, `error`, `output`, `lines`, `contains` and `result`.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

from .dataset import Dataset, read
from .execute import RunError, run_step
from .library import PACKAGE_FILE, LibraryError, Version, checksums_text, read_version
from .manifest import ManifestError, build_manifest, load_step_class, manifest_hash, source_files
from .spark_plug import SOURCE_BYTES, SparkPlugError, largest_header, simulate, source_profile

RELEASE_FORMAT = "benchvista-library-release-v1"


class Refused(Exception):
    def __init__(self, code: str, where: str) -> None:
        super().__init__(f"{code} at {where}")
        self.code = code
        self.where = where


# ---------------------------------------------------------------------------------------------
# Analysis steps


def _build(directory: Path, entry: str | None) -> tuple[dict, str, type]:
    step, resolved_entry = load_step_class(directory, entry)
    manifest = build_manifest(step, entry=resolved_entry, files=source_files(directory))
    return manifest, manifest_hash(manifest), step


def _load_dataset(folder: Path, base: str) -> Dataset:
    manifest = json.loads((folder / f"{base}.manifest.json").read_text("utf-8"))
    chunks = [(folder / f"{base}.{index}.bin").read_bytes() for index in range(len(manifest["chunks"]))]
    return read(manifest, chunks)


def build_command(directory: Path, entry: str | None) -> int:
    try:
        manifest, digest, _ = _build(directory, entry)
    except ManifestError as error:
        print(f"refused: {error.code} at {error.where}", file=sys.stderr)
        return 2
    (directory / "step.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", "utf-8")
    (directory / "step.sha256").write_text(digest + "\n", "utf-8")
    print(f"{manifest['id']}@{manifest['version']} sha256 {digest}")
    return 0


def test_command(directory: Path, entry: str | None) -> int:
    try:
        manifest, digest, step = _build(directory, entry)
    except ManifestError as error:
        print(f"refused: {error.code} at {error.where}", file=sys.stderr)
        return 2
    cases = sorted(path.parent for path in (directory / "fixtures").glob("*/case.json"))
    if not cases:
        print("no fixtures: add fixtures/<case>/case.json", file=sys.stderr)
        return 1
    failed = 0
    print(f"{manifest['id']}@{manifest['version']} sha256 {digest}")
    for folder in cases:
        case = json.loads((folder / "case.json").read_text("utf-8"))
        inputs = {port: _load_dataset(folder, base) for port, base in case.get("inputs", {}).items()}
        seed = case.get("seed")
        try:
            first = run_step(step, manifest, inputs, case.get("params", {}), seed=seed)
            second = run_step(step, manifest, inputs, case.get("params", {}), seed=seed)
        except RunError as error:
            expected_error = case.get("error")
            if expected_error == error.code:
                print(f"pass {folder.name}: failed as expected with {error.code}")
                continue
            print(f"FAIL {folder.name}: {error.code} {error.where}")
            failed += 1
            continue
        if case.get("error") is not None:
            print(f"FAIL {folder.name}: expected {case['error']}, the step completed")
            failed += 1
            continue
        ids = {port: dataset.id for port, dataset in first.outputs.items()}
        problems = []
        if manifest["purity"] != "external":
            again = {port: dataset.id for port, dataset in second.outputs.items()}
            if again != ids:
                problems.append(f"not reproducible: {ids} then {again}")
        for port, expected in case.get("expect", {}).items():
            if ids.get(port) != expected:
                problems.append(f"{port} is {ids.get(port)}, expected {expected}")
        if problems:
            failed += 1
            print(f"FAIL {folder.name}: " + "; ".join(problems))
        else:
            summary = ", ".join(f"{port} {dataset.rows} rows {dataset.id}" for port, dataset in first.outputs.items())
            print(f"pass {folder.name} ({first.status}): {summary}")
    return 1 if failed else 0


def _step_entry(package: dict) -> str:
    """`benchvista.json`'s entry file as the loader's `module:` (its one Step class)."""
    return package["entry"][: -len(".py")].replace("/", ".") + ":"


def _check_step(directory: Path, version: Version) -> tuple[dict, str]:
    package = version.package
    try:
        manifest, digest, _ = _build(directory, _step_entry(package))
    except ManifestError as error:
        raise Refused(error.code, error.where) from error
    for key in ("id", "version"):
        if manifest[key] != package[key]:
            raise Refused("LIBRARY_STEP_IDENTITY", f"{key}: the Step class says {manifest[key]!r}, {PACKAGE_FILE} says {package[key]!r}")
    return manifest, digest


# ---------------------------------------------------------------------------------------------
# Spark Plugs


PIN_USE = re.compile(r"""\bPINS\s*\[\s*(["'])([^"']*)\1\s*\]""")
SETTING_USE = re.compile(r"""\bSETTINGS\s*\[\s*(["'])([^"']*)\1\s*\]""")


def _plug_source(directory: Path, version: Version) -> str:
    package = version.package
    try:
        source = (directory / package["entry"]).read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refused("LIBRARY_PLUG_SOURCE_TEXT", package["entry"]) from error
    return source


def _check_plug(directory: Path, version: Version) -> str:
    package = version.package
    entry = package["entry"]
    for file in version.files:
        path = file["path"]
        if path.endswith(".py") and path != entry and not path.startswith("tests/"):
            raise Refused("LIBRARY_PLUG_EXTRA_SOURCE", f"{path}: a board receives only {entry}")
    source = _plug_source(directory, version)
    try:
        source_profile(source)
    except SparkPlugError as error:
        raise Refused(error.code, f"{entry} {error.detail}".strip()) from error
    try:
        tree = ast.parse(source, entry)
    except SyntaxError as error:
        raise Refused("SPARK_SCRIPT_SYNTAX", f"{entry}:{error.lineno} {error.msg}") from error
    if not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body):
        raise Refused("SPARK_SCRIPT_MAIN", f"{entry}: define main(spark)")
    roles = {pin["role"] for pin in package["pins"]}
    for match in PIN_USE.finditer(source):
        if match.group(2) not in roles:
            raise Refused("LIBRARY_PLUG_PIN_UNKNOWN", f"{entry}: PINS[{match.group(2)!r}] is not a declared role")
    settings = set(package.get("settings", {}))
    for match in SETTING_USE.finditer(source):
        if match.group(2) not in settings:
            raise Refused("LIBRARY_PLUG_SETTING_UNKNOWN", f"{entry}: SETTINGS[{match.group(2)!r}] is not declared")
    size = len(source.encode("utf-8")) + largest_header(package)
    if size > SOURCE_BYTES:
        raise Refused("LIBRARY_PLUG_SOURCE_SIZE", f"{entry}: {size} bytes with the header, the board takes {SOURCE_BYTES}")
    return source


def _plug_cases(directory: Path) -> list[tuple[str, dict]]:
    paths = sorted((directory / "tests").glob("*.json"))
    if not paths:
        return [("defaults", {})]
    return [(path.stem, json.loads(path.read_text("utf-8"))) for path in paths]


def _plug_test(directory: Path, version: Version, source: str) -> int:
    failed = 0
    for name, case in _plug_cases(directory):
        expect = case.get("expect", {})
        run = simulate(
            version.package,
            source,
            settings=case.get("settings"),
            wiring=case.get("wiring"),
            readings=case.get("readings"),
        )
        problems = []
        state = expect.get("state", "fault" if "error" in expect else "complete")
        if run.state != state:
            problems.append(f"ended {run.state}{' with ' + run.error if run.error else ''}, expected {state}")
        if "error" in expect and (run.error or "").split(" ")[0] != expect["error"]:
            problems.append(f"error {run.error}, expected {expect['error']}")
        if "output" in expect and run.output != expect["output"]:
            problems.append(f"output {run.output!r}, expected {expect['output']!r}")
        if "lines" in expect and len(run.output.splitlines()) != expect["lines"]:
            problems.append(f"{len(run.output.splitlines())} lines, expected {expect['lines']}")
        for text in expect.get("contains", []):
            if text not in run.output:
                problems.append(f"output lacks {text!r}")
        if "result" in expect and run.result != expect["result"]:
            problems.append(f"result {run.result!r}, expected {expect['result']!r}")
        if run.truncated_bytes:
            problems.append(f"{run.truncated_bytes} console bytes over the board's limit")
        if problems:
            failed += 1
            print(f"FAIL {name}: " + "; ".join(problems))
        else:
            print(f"pass {name}: {run.state} after {run.elapsed_ms} ms simulated, {run.steps} steps, {len(run.output.splitlines())} lines")
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# Items


def _read(directory: Path) -> Version:
    if not (directory / PACKAGE_FILE).is_file():
        raise Refused("LIBRARY_PACKAGE_MISSING", f"{directory / PACKAGE_FILE}: run `benchvista new` or add one")
    try:
        return read_version(directory)
    except LibraryError as error:
        raise Refused(error.code, error.where) from error


def _refused(error: Refused) -> int:
    print(f"refused: {error.code} at {error.where}", file=sys.stderr)
    return 2


def check_item(directory: Path) -> int:
    try:
        version = _read(directory)
        if version.package["kind"] == "spark-plug":
            _check_plug(directory, version)
        else:
            _check_step(directory, version)
    except Refused as error:
        return _refused(error)
    print(f"{version.package['kind']} {version.label} sha256 {version.sha256}")
    for file in version.files:
        print(f"  {file['sha256']}  {file['path']} ({file['bytes']} bytes)")
    return 0


def test_item(directory: Path) -> int:
    try:
        version = _read(directory)
        if version.package["kind"] == "spark-plug":
            source = _check_plug(directory, version)
        else:
            _check_step(directory, version)
    except Refused as error:
        return _refused(error)
    print(f"{version.package['kind']} {version.label} sha256 {version.sha256}")
    if version.package["kind"] == "spark-plug":
        print("Simulated Spark in CPython: the board's rules, no real pins or timing.")
        return _plug_test(directory, version, source)
    return test_command(directory, _step_entry(version.package))


def release_record(directory: Path) -> dict:
    """The release record of an item's folder: what a collection publishes for one version."""
    version = _read(directory)
    record: dict[str, Any] = {
        "format": RELEASE_FORMAT,
        "kind": version.package["kind"],
        "id": version.package["id"],
        "version": version.package["version"],
        "sha256": version.sha256,
        "package": version.package,
        "files": version.files,
    }
    if version.package["kind"] == "spark-plug":
        _check_plug(directory, version)
    else:
        manifest, digest = _check_step(directory, version)
        record["step"] = {"manifest": manifest, "sha256": digest}
    return record


def build_item(directory: Path, out: Path | None) -> int:
    try:
        record = release_record(directory)
    except Refused as error:
        return _refused(error)
    target = out or directory / "build"
    target.mkdir(parents=True, exist_ok=True)
    (target / "release.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", "utf-8")
    version = Version(package=record["package"], files=record["files"], sha256=record["sha256"])
    (target / "checksums.txt").write_text(checksums_text(version), "utf-8")
    print(f"{record['kind']} {version.label} sha256 {record['sha256']}")
    print(f"wrote {target / 'release.json'} and {target / 'checksums.txt'}")
    return 0


# ---------------------------------------------------------------------------------------------
# New items


def _title(item_id: str) -> str:
    return item_id.replace("-", " ").capitalize()


def _plug_files(item_id: str) -> dict[str, str]:
    package = {
        "kind": "spark-plug",
        "id": item_id,
        "version": "0.1.0",
        "name": _title(item_id)[:48],
        "summary": "Blinks one output pin and prints each blink.",
        "license": "CC0-1.0",
        "entry": "main.py",
        "pins": [{"role": "OUT", "direction": "output", "needs": "digital", "label": "Output that blinks"}],
        "settings": {
            "blinks": {"kind": "integer", "default": 5, "minimum": 1, "maximum": 50, "label": "Blinks"},
            "period": {"kind": "quantity", "quantity": "time", "unit": "ms", "default": 500, "minimum": 20, "maximum": 1000, "label": "Period"},
        },
        "timeoutMs": 60000,
        "sparkTaskApi": 1,
    }
    main = (
        "# BenchVista writes PINS and SETTINGS above this file when it installs it on a board.\n"
        "def main(spark):\n"
        '    half = round(SETTINGS["period"] / 2)\n'
        '    for count in range(SETTINGS["blinks"]):\n'
        '        spark.write(PINS["OUT"], 1)\n'
        "        yield spark.sleep_ms(half)\n"
        '        spark.write(PINS["OUT"], 0)\n'
        "        yield spark.sleep_ms(half)\n"
        '        print("blink", count + 1)\n'
    )
    readme = (
        f"# {package['name']}\n\n{package['summary']}\n\n"
        "## Wiring\n\nOUT drives an LED through a 1 kohm resistor to ground. Use 3.3 V logic only.\n\n"
        "## Output\n\nOne line per blink: `blink 1` to `blink 5` with the default settings.\n"
    )
    test = {"expect": {"state": "complete", "lines": 5, "contains": ["blink 5"]}}
    return {
        PACKAGE_FILE: json.dumps(package, indent=2) + "\n",
        "main.py": main,
        "README.md": readme,
        "tests/defaults.json": json.dumps(test, indent=2) + "\n",
    }


def _step_files(item_id: str) -> dict[str, str]:
    name = _title(item_id)[:48]
    package = {
        "kind": "analysis-step",
        "id": item_id,
        "version": "0.1.0",
        "name": name,
        "summary": "Lists the lowest and highest value of each numeric field.",
        "license": "CC0-1.0",
        "entry": "step.py",
    }
    step = (
        '"""An analysis step for BenchVista (`docs/analysis-python-steps.md`)."""\n\n'
        "import numpy as np\n\n"
        "from benchvista import Column, FieldConstraint, Series, Step, Table\n\n\n"
        "class FieldRange(Step):\n"
        '    """Lists the lowest and highest value of each numeric field."""\n\n'
        f"    id = {item_id!r}\n"
        '    version = "0.1.0"\n'
        f"    name = {name!r}\n"
        '    purity = "pure"\n'
        '    category = "measure"\n'
        '    inputs = {"signal": Series(fields=FieldConstraint(type=["f64"]), min_fields=1)}\n'
        '    outputs = {"ranges": Table(field="text", lowest=Column("f64"), highest=Column("f64"))}\n\n'
        "    def run(self, ctx, signal, params):\n"
        "        names, lowest, highest = [], [], []\n"
        "        for field in signal.fields:\n"
        "            values = np.asarray(signal.column(field), dtype=np.float64)\n"
        "            if len(values):\n"
        "                names.append(field)\n"
        "                lowest.append(float(values.min()))\n"
        "                highest.append(float(values.max()))\n"
        '        ctx.table("ranges", field=names, lowest=np.array(lowest), highest=np.array(highest))\n'
    )
    readme = (
        f"# {name}\n\n{package['summary']}\n\n"
        "Add test cases under `fixtures/<case>/case.json`; see `docs/analysis-python-steps.md` §15.3.\n"
    )
    return {PACKAGE_FILE: json.dumps(package, indent=2) + "\n", "step.py": step, "README.md": readme}


def new_item(kind: str, directory: Path) -> int:
    item_id = directory.name
    if not re.match(r"^[a-z][a-z0-9-]{0,63}$", item_id):
        print(f"refused: LIBRARY_PACKAGE_ID at {item_id}: use lowercase letters, digits and hyphens", file=sys.stderr)
        return 2
    if directory.exists() and any(directory.iterdir()):
        print(f"refused: LIBRARY_NEW_NOT_EMPTY at {directory}", file=sys.stderr)
        return 2
    files = _plug_files(item_id) if kind == "spark-plug" else _step_files(item_id)
    for path, text in files.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    print(f"new {kind} {item_id} in {directory}")
    print(f"next: edit it, then `benchvista check {directory}` and `benchvista test {directory}`")
    return 0


def build_collection(args: argparse.Namespace) -> int:
    from .collection import CollectionError, build

    try:
        index = build(
            args.directory.resolve(),
            args.out.resolve(),
            releases=args.releases.resolve() if args.releases else None,
            new_releases=args.new_releases.resolve() if args.new_releases else None,
            commit=args.commit,
            attested=args.attested,
            run_tests=not args.no_tests,
        )
    except CollectionError as error:
        print(f"refused: {error.code} at {error.where}", file=sys.stderr)
        return 2
    versions = sum(len(item["versions"]) for item in index["items"])
    print(f"collection {index['name']}: {len(index['items'])} items, {versions} versions, site in {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchvista", description="Write, check and test Spark Plugs and analysis steps.")
    commands = parser.add_subparsers(dest="group", required=True)
    new = commands.add_parser("new", help="start a Spark Plug or an analysis step in a new folder")
    new.add_argument("kind", choices=("spark-plug", "analysis-step"))
    new.add_argument("directory", type=Path)
    for name, text in (("check", "check an item and print its version hash"), ("test", "run an item's tests"), ("build", "write an item's release record and checksums")):
        command = commands.add_parser(name, help=text)
        command.add_argument("directory", type=Path)
        if name == "build":
            command.add_argument("--out", type=Path, default=None, help="folder for release.json and checksums.txt (default <dir>/build)")
    collection = commands.add_parser("collection", help="build a collection repository's published site")
    collection_actions = collection.add_subparsers(dest="action", required=True)
    site = collection_actions.add_parser("build")
    site.add_argument("directory", type=Path, help="the collection repository")
    site.add_argument("--out", type=Path, required=True, help="the site folder to write")
    site.add_argument("--releases", type=Path, default=None, help="published release records, <tag>/release.json")
    site.add_argument("--new-releases", type=Path, default=None, help="where to write new versions' release records")
    site.add_argument("--commit", default=None, help="the commit being built")
    site.add_argument("--attested", action="store_true", help="the workflow attests collection.json and checksums.txt")
    site.add_argument("--no-tests", action="store_true", help="check and hash only")
    step = commands.add_parser("step", help="build and test Python analysis steps without benchvista.json")
    actions = step.add_subparsers(dest="action", required=True)
    for action in ("build", "test"):
        command = actions.add_parser(action)
        command.add_argument("directory", type=Path)
        command.add_argument("--entry", default=None, help="module:Class, when the directory's step.py is not it")
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    if args.group == "new":
        return new_item(args.kind, directory)
    if args.group == "check":
        return check_item(directory)
    if args.group == "test":
        return test_item(directory)
    if args.group == "build":
        return build_item(directory, args.out.resolve() if args.out else None)
    if args.group == "collection":
        return build_collection(args)
    if args.action == "build":
        return build_command(directory, args.entry)
    return test_command(directory, args.entry)


if __name__ == "__main__":
    raise SystemExit(main())
