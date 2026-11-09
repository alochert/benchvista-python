"""Library items: the `benchvista.json` package file, an item's files and its version hash
(`docs/library.md`).

A Library item is a folder with the Python source of a Spark Plug or an analysis step and one
`benchvista.json`. `src/library/package.ts` is the authority at every BenchVista boundary; the
checks here refuse the same packages at the author's desk, with the same codes for every rule
beyond the field shapes, and the version hash is byte for byte the one `src/library/version.ts`
computes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_hex
from .manifest import UNITS
from .text import LOCALES

PACKAGE_FILE = "benchvista.json"
VERSION_FORMAT = "benchvista-library-version-v1"
KINDS = ("spark-plug", "analysis-step")
MAX_FILES = 256
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
#: Generated or local folders and files that are never part of a version.
SKIP_DIRS = {"__pycache__", "build", "dist"}

ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION = re.compile(r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROLE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CHOICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}$")
LICENSE = re.compile(r"^(LicenseRef-[A-Za-z0-9.-]{1,64}|[A-Za-z0-9][A-Za-z0-9.+-]{0,63})$")
PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f\u2028\u2029]")
INTEGER_LIMIT = 10**9


class LibraryError(ValueError):
    """A package or folder the Library refuses: a `LIBRARY_*` code and where."""

    def __init__(self, code: str, where: str) -> None:
        super().__init__(f"{code} at {where}")
        self.code = code
        self.where = where


@dataclass(frozen=True)
class Version:
    package: dict
    files: list[dict]
    sha256: str

    @property
    def label(self) -> str:
        return f"{self.package['id']}@{self.package['version']}"


# ---------------------------------------------------------------------------------------------
# The package file


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) or (isinstance(value, float) and value.is_integer())


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def check_package(value: Any) -> dict:
    """Check a parsed `benchvista.json`; returns it unchanged, or raises `LibraryError`."""

    def fail(code: str, where: str) -> None:
        raise LibraryError(code, where)

    def strict(obj: Any, required: set[str], optional: set[str], where: str) -> None:
        if not isinstance(obj, dict):
            fail("LIBRARY_PACKAGE_SHAPE", where)
        for key in required - set(obj):
            fail("LIBRARY_PACKAGE_MISSING_FIELD", f"{where}.{key}" if where else key)
        for key in set(obj) - required - optional:
            fail("LIBRARY_PACKAGE_UNKNOWN_FIELD", f"{where}.{key}" if where else key)

    def display(value: Any, maximum: int, where: str) -> None:
        if not isinstance(value, str) or not 1 <= len(value.encode("utf-16-le", "surrogatepass")) // 2 <= maximum or not value.strip() or CONTROL.search(value):
            fail("LIBRARY_PACKAGE_TEXT", where)

    def matches(pattern: re.Pattern[str], value: Any, code: str, where: str) -> None:
        if not isinstance(value, str) or not pattern.match(value):
            fail(code, where)

    def path(value: Any, where: str) -> None:
        if not isinstance(value, str) or len(value) > 256 or not PATH.match(value) or any(part == ".." or part.startswith(".") for part in value.split("/")):
            fail("LIBRARY_PATH", where)

    if not isinstance(value, dict) or value.get("kind") not in KINDS:
        fail("LIBRARY_PACKAGE_KIND", "kind")
    kind = value["kind"]
    common = {"kind", "id", "version", "name", "summary", "license", "entry"}
    optional = {"tags", "translations"}
    if kind == "spark-plug":
        strict(value, common | {"pins", "timeoutMs", "sparkTaskApi"}, optional | {"settings"}, "")
    else:
        strict(value, common, optional, "")
    matches(ID, value["id"], "LIBRARY_PACKAGE_ID", "id")
    matches(VERSION, value["version"], "LIBRARY_PACKAGE_VERSION", "version")
    display(value["name"], 48, "name")
    display(value["summary"], 240, "summary")
    matches(LICENSE, value["license"], "LIBRARY_PACKAGE_LICENSE", "license")
    path(value["entry"], "entry")
    if not value["entry"].endswith(".py"):
        fail("LIBRARY_PACKAGE_ENTRY", "entry")
    tags = value.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 8:
        fail("LIBRARY_PACKAGE_SHAPE", "tags")
    for index, tag in enumerate(tags):
        matches(TAG, tag, "LIBRARY_PACKAGE_TAG", f"tags.{index}")
    if len(set(tags)) != len(tags):
        fail("LIBRARY_PACKAGE_DUPLICATE", "tags")
    texts = {"name", "summary"}

    if kind == "spark-plug":
        pins = value["pins"]
        if not isinstance(pins, list) or len(pins) > 16:
            fail("LIBRARY_PACKAGE_SHAPE", "pins")
        roles: set[str] = set()
        for index, pin in enumerate(pins):
            where = f"pins.{index}"
            strict(pin, {"role", "direction", "needs", "label"}, set(), where)
            matches(ROLE, pin["role"], "LIBRARY_PLUG_ROLE", f"{where}.role")
            if pin["direction"] not in ("input", "output"):
                fail("LIBRARY_PACKAGE_SHAPE", f"{where}.direction")
            if pin["needs"] not in ("digital", "pwm", "adc"):
                fail("LIBRARY_PACKAGE_SHAPE", f"{where}.needs")
            display(pin["label"], 64, f"{where}.label")
            if pin["role"] in roles:
                fail("LIBRARY_PACKAGE_DUPLICATE", f"{where}.role")
            roles.add(pin["role"])
            texts.add(f"role.{pin['role']}")
            if (pin["needs"] == "pwm" and pin["direction"] != "output") or (pin["needs"] == "adc" and pin["direction"] != "input"):
                fail("LIBRARY_PLUG_PIN_NEEDS", f"{where}.needs")
        timeout = value["timeoutMs"]
        if not _is_int(timeout) or not 100 <= timeout <= 600_000:
            fail("LIBRARY_PACKAGE_SHAPE", "timeoutMs")
        if value["sparkTaskApi"] != 1 or isinstance(value["sparkTaskApi"], bool):
            fail("LIBRARY_PLUG_TASK_API", "sparkTaskApi")
        settings = value.get("settings", {})
        if not isinstance(settings, dict):
            fail("LIBRARY_PACKAGE_SHAPE", "settings")
        for name, setting in settings.items():
            where = f"settings.{name}"
            matches(NAME, name, "LIBRARY_PLUG_SETTING_NAME", where)
            check_setting(setting, where, fail, strict, display)
            texts.add(f"setting.{name}.label")
            if "help" in setting:
                texts.add(f"setting.{name}.help")
            for choice in setting.get("labels", {}) if setting["kind"] == "choice" else ():
                texts.add(f"setting.{name}.choice.{choice}")

    translations = value.get("translations", {})
    if not isinstance(translations, dict):
        fail("LIBRARY_PACKAGE_SHAPE", "translations")
    for locale, entries in translations.items():
        if locale not in LOCALES:
            fail("LIBRARY_PACKAGE_LOCALE", f"translations.{locale}")
        if not isinstance(entries, dict):
            fail("LIBRARY_PACKAGE_SHAPE", f"translations.{locale}")
        for key, text in entries.items():
            if len(key) > 160 or key not in texts:
                fail("LIBRARY_PACKAGE_TRANSLATION", f"translations.{locale}.{key}")
            display(text, 480, f"translations.{locale}.{key}")
    return value


def check_setting(setting: Any, where: str, fail, strict, display) -> None:  # noqa: ANN001
    text = {"label"}
    if not isinstance(setting, dict):
        fail("LIBRARY_PACKAGE_SHAPE", where)
    kind = setting.get("kind")
    if kind == "quantity":
        strict(setting, {"kind", "quantity", "unit", "default"} | text, {"minimum", "maximum", "help"}, where)
        if setting["unit"] not in UNITS:
            fail("LIBRARY_PACKAGE_SHAPE", f"{where}.unit")
        if UNITS[setting["unit"]] != setting["quantity"]:
            fail("LIBRARY_PLUG_SETTING_UNIT", f"{where}.unit")
        numbers = _is_number
    elif kind == "integer":
        strict(setting, {"kind", "default"} | text, {"minimum", "maximum", "help"}, where)

        def numbers(value: Any) -> bool:
            return _is_int(value) and -INTEGER_LIMIT <= value <= INTEGER_LIMIT and not isinstance(value, bool)

    elif kind == "choice":
        strict(setting, {"kind", "choices", "default"} | text, {"labels", "help"}, where)
        choices = setting["choices"]
        if not isinstance(choices, list) or not 1 <= len(choices) <= 64:
            fail("LIBRARY_PACKAGE_SHAPE", f"{where}.choices")
        for choice in choices:
            if not isinstance(choice, str) or not CHOICE.match(choice):
                fail("LIBRARY_PACKAGE_SHAPE", f"{where}.choices")
        if len(set(choices)) != len(choices):
            fail("LIBRARY_PACKAGE_DUPLICATE", f"{where}.choices")
        if not isinstance(setting["default"], str) or not CHOICE.match(setting["default"]):
            fail("LIBRARY_PACKAGE_SHAPE", f"{where}.default")
        if setting["default"] not in choices:
            fail("LIBRARY_PLUG_SETTING_DEFAULT", f"{where}.default")
        labels = setting.get("labels", {})
        if not isinstance(labels, dict):
            fail("LIBRARY_PACKAGE_SHAPE", f"{where}.labels")
        for choice, label in labels.items():
            if choice not in choices:
                fail("LIBRARY_PLUG_SETTING_CHOICE", f"{where}.labels.{choice}")
            display(label, 64, f"{where}.labels.{choice}")
        numbers = None
    elif kind == "toggle":
        strict(setting, {"kind", "default"} | text, {"help"}, where)
        if not isinstance(setting["default"], bool):
            fail("LIBRARY_PACKAGE_SHAPE", f"{where}.default")
        numbers = None
    else:
        fail("LIBRARY_PACKAGE_SHAPE", f"{where}.kind")
        return
    display(setting["label"], 64, f"{where}.label")
    if "help" in setting:
        display(setting["help"], 240, f"{where}.help")
    if numbers is not None:
        for key in ("default", "minimum", "maximum"):
            if key in setting and not numbers(setting[key]):
                fail("LIBRARY_PACKAGE_SHAPE", f"{where}.{key}")
        low, high = setting.get("minimum"), setting.get("maximum")
        if low is not None and high is not None and low > high:
            fail("LIBRARY_PLUG_SETTING_BOUNDS", where)
        if (low is not None and setting["default"] < low) or (high is not None and setting["default"] > high):
            fail("LIBRARY_PLUG_SETTING_DEFAULT", f"{where}.default")


def parse_package(data: bytes) -> dict:
    """The checked package of a `benchvista.json`'s bytes: UTF-8 JSON, no byte order mark."""
    def constant(name: str) -> Any:
        raise ValueError(name)  # NaN and Infinity are not JSON

    try:
        value = json.loads(data.decode("utf-8"), parse_constant=constant)
    except (UnicodeDecodeError, ValueError) as error:
        raise LibraryError("LIBRARY_PACKAGE_JSON", PACKAGE_FILE) from error
    return check_package(value)


# ---------------------------------------------------------------------------------------------
# Files and the version hash


def item_files(root: Path) -> list[tuple[str, bytes]]:
    """Every file of an item's folder that belongs to a version, sorted by path.

    Hidden files and folders, `__pycache__`, compiled `.pyc` files and the generated `build/` and
    `dist/` folders are left out. A link is refused: a version is the bytes in its folder.
    """
    found: list[tuple[str, bytes]] = []
    total = 0

    def walk(folder: Path, prefix: str) -> None:
        nonlocal total
        for entry in sorted(folder.iterdir(), key=lambda item: item.name):
            name = entry.name
            if name.startswith(".") or name.endswith(".pyc"):
                continue
            relative = prefix + name
            if entry.is_symlink():
                raise LibraryError("LIBRARY_FILE_LINK", relative)
            if entry.is_dir():
                if name in SKIP_DIRS:
                    continue
                walk(entry, relative + "/")
                continue
            if not entry.is_file():
                raise LibraryError("LIBRARY_FILE_KIND", relative)
            if len(relative) > 256 or not PATH.match(relative):
                raise LibraryError("LIBRARY_PATH", relative)
            data = entry.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise LibraryError("LIBRARY_LIMIT_FILE", relative)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise LibraryError("LIBRARY_LIMIT_TOTAL", relative)
            found.append((relative, data))
            if len(found) > MAX_FILES:
                raise LibraryError("LIBRARY_LIMIT_FILES", relative)

    walk(root, "")
    found.sort(key=lambda item: item[0])
    folded = [path.lower() for path, _ in found]
    if len(set(folded)) != len(folded):
        raise LibraryError("LIBRARY_PATH_DUPLICATE", str(root))
    return found


def version_hash(package: dict, files: list[dict]) -> str:
    """The version's identity: SHA-256 of the canonical JSON of kind, id, version and files."""
    listed = sorted(({"path": f["path"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in files), key=lambda f: f["path"])
    record = {"format": VERSION_FORMAT, "kind": package["kind"], "id": package["id"], "version": package["version"], "files": listed}
    return canonical_hex(record)


def read_version(root: Path) -> Version:
    """Read and check an item's folder: its package, its files with their hashes, its version hash."""
    entries = item_files(root)
    contents = dict(entries)
    if PACKAGE_FILE not in contents:
        raise LibraryError("LIBRARY_PACKAGE_MISSING", str(root / PACKAGE_FILE))
    package = parse_package(contents[PACKAGE_FILE])
    if package["entry"] not in contents:
        raise LibraryError("LIBRARY_PACKAGE_ENTRY", package["entry"])
    files = [{"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in entries]
    return Version(package=package, files=files, sha256=version_hash(package, files))


def checksums_text(version: Version) -> str:
    """The version's files in `sha256sum` format, for `sha256sum -c`."""
    return "".join(f"{f['sha256']}  {f['path']}\n" for f in version.files)
