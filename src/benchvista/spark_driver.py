"""Spark drivers from the Library at the author's desk (`docs/spark-drivers.md` §14).

A Spark driver is one MicroPython module with a `Driver` class, written under the driver contract
(§5), for one display or sensor on I²C or SPI. `source_problem` is the contract's lexical check,
`block` writes a driver into a Spark Plug's installed header byte for byte as
`src/library/spark-driver.ts` does, and `run_case` opens a driver in CPython against a recording
bus, for `benchvista test`.

A passing case means the module keeps the contract, opens, and sent what the case expects to a
chip that answers what the case says. It is not MicroPython and says nothing about a real device.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .spark_drivers import DRIVERS, I2C_LINES

DRIVER_BYTES = 6144
#: The only import statements a driver may contain, each on a line of its own.
IMPORTS = ("import framebuf", "import struct", "from micropython import const")
#: Names a driver may not use anywhere.
REFUSED = (
    "open", "exec", "eval", "compile", "globals", "locals", "getattr", "setattr", "delattr", "type",
    "object", "super", "vars", "dir", "help", "input", "breakpoint", "print",
)  # fmt: skip
LINE_NAMES = ("MISO", "CS", "DC", "RESET")
_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
_TOKEN = re.compile("[A-Za-z_][A-Za-z_0-9]*")
_SPAN = re.compile("[^A-Za-z_#\"']+")
_ENTRY = re.compile(r"^class Driver[:(]", re.MULTILINE)
_MARKER = "#: driver "


def _quoted_end(text: str, start: int, quote: str) -> int:
    end = text.find(quote, start)
    while end >= 0:
        previous = end - 1
        while previous >= start and text[previous] == "\\":
            previous -= 1
        if (end - 1 - previous) % 2 == 0:
            return end + len(quote)
        end = text.find(quote, end + 1)
    return len(text) + 1


def source_problem(source: str) -> tuple[str, str] | None:
    """Why a driver's module does not keep the contract, as a `LIBRARY_DRIVER_*` code and the name
    or statement refused, or None. The same lexical check as BenchVista and the board run."""
    size_bytes = len(source.encode("utf-8"))
    if not 1 <= size_bytes <= DRIVER_BYTES or "\r" in source or "\x00" in source:
        return ("LIBRARY_DRIVER_SOURCE", "")
    index, size = 0, len(source)
    while index < size:
        char = source[index]
        if char == "#":
            end = source.find("\n", index)
            index = size if end < 0 else end
        elif char in ('"', "'"):
            quote = char * 3 if source[index : index + 3] == char * 3 else char
            index = _quoted_end(source, index + len(quote), quote)
        elif char in _LETTERS:
            match = _TOKEN.match(source, index)
            assert match is not None
            token = match.group(0)
            if token in ("import", "from"):
                start = source.rfind("\n", 0, index) + 1
                end = source.find("\n", index)
                line = source[start : size if end < 0 else end].strip()
                if line not in IMPORTS:
                    return ("LIBRARY_DRIVER_CONTRACT", line)
                index = size if end < 0 else end
                continue
            index += len(token)
            if len(token) > 64 or token in REFUSED or (token.startswith("_") and token != "__init__"):
                return ("LIBRARY_DRIVER_CONTRACT", token)
            if index < size and source[index] in ('"', "'") and "f" in token.lower():
                return ("LIBRARY_DRIVER_CONTRACT", token)
        else:
            match = _SPAN.match(source, index)
            assert match is not None
            span = match.group(0)
            if max(span) >= "\x80":
                return ("LIBRARY_DRIVER_CONTRACT", "")
            index += len(span)
    return None if _ENTRY.search(source) else ("LIBRARY_DRIVER_ENTRY", "")


def spec(package: dict) -> dict:
    """A driver's package as the catalogue describes one of BenchVista's, with what its bus needs."""
    wanted = package.get("lines", {})
    if package["bus"] == "i2c":
        lines = [dict(line) for line in I2C_LINES]
    else:
        lines = [{"name": "SCK", "direction": "output"}, {"name": "MOSI", "direction": "output"}]
        for name in LINE_NAMES:
            if name in wanted:
                line: dict = {"name": name, "direction": "input" if name == "MISO" else "output"}
                if wanted[name] == "optional":
                    line["optional"] = True
                lines.append(line)
    found: dict = {"bus": package["bus"], "kind": package["device"], "lines": lines}
    if "addresses" in package:
        found["addresses"] = list(package["addresses"])
    found["settings"] = {name: list(allowed) for name, allowed in package.get("settings", {}).items()}
    if "sizes" in package:
        found["sizes"] = [list(size) for size in package["sizes"]]
    found["hz"] = package.get("hz", 400_000)
    found["mode"] = package.get("mode", 0)
    found["colour"] = package.get("colour") == "rgb565"
    return found


@dataclass(frozen=True)
class LibraryDriver:
    """One exact driver version as a Spark Plug needs it: its package and its module."""

    package: dict
    version_sha256: str
    source: str
    source_sha256: str


def device_driver(device: dict, library: dict[str, LibraryDriver] | None = None) -> dict | None:
    """A device's driver: one of BenchVista's by name, or the Library driver it names."""
    named = device["driver"]
    if isinstance(named, str):
        return DRIVERS.get(named)
    held = (library or {}).get(named["sha256"])
    if held is None or held.package["id"] != named["id"] or held.package["version"] != named["version"]:
        return None
    return spec(held.package)


def driver_name(named: Any) -> str:
    """The name a device's driver goes by on the board."""
    return named if isinstance(named, str) else named["id"]


def record(package: dict, source_sha256: str) -> dict:
    """What the board is told about an embedded driver, in the key order BenchVista writes."""
    found = spec(package)
    told: dict = {
        "name": package["id"],
        "version": package["version"],
        "sha256": source_sha256,
        "bus": found["bus"],
        "kind": found["kind"],
        "hz": found["hz"],
    }
    if found["bus"] == "i2c":
        told["addresses"] = found.get("addresses", [])
    else:
        told["mode"] = found["mode"]
        told["lines"] = {
            line["name"]: "optional" if line.get("optional") else "required"
            for line in found["lines"]
            if line["name"] not in ("SCK", "MOSI")
        }
    told["settings"] = {name: found["settings"][name] for name in sorted(found["settings"])}
    if "sizes" in found:
        told["sizes"] = found["sizes"]
    if found["kind"] == "display":
        told["colour"] = found["colour"]
    return told


def block(told: dict, source: str) -> str:
    """A driver as the header carries it: comment lines, never an empty one."""
    head = json.dumps(told, separators=(",", ":"), ensure_ascii=False)
    if any(not " " <= char <= "~" for char in head):
        raise ValueError("LIBRARY_DRIVER_SOURCE")
    return _MARKER + head + "\n" + "".join(f"#|{line}\n" for line in source.split("\n")) + "#: end\n"


def embedded(installed: str) -> list[tuple[Any, str]]:
    """The drivers a header carries, as the board reads them: each record and its module's text."""
    blank = installed.find("\n\n")
    header = installed if blank < 0 else installed[: blank + 1]
    found: list[tuple[Any, str]] = []

    def line_start(start: int) -> int:
        at = header.find("\n" + _MARKER, start)
        return -1 if at < 0 else at + 1

    at = 0 if header.startswith(_MARKER) else line_start(0)
    while at >= 0:
        line_end = header.find("\n", at)
        end = -1 if line_end < 0 else header.find("\n#: end\n", line_end)
        if end < 0:
            raise ValueError("LIBRARY_DRIVER_SOURCE")
        body = header[line_end + 1 : end + 1]
        if not body.startswith("#|"):
            raise ValueError("LIBRARY_DRIVER_SOURCE")
        try:
            told = json.loads(header[at + len(_MARKER) : line_end])
        except ValueError as error:
            raise ValueError("LIBRARY_DRIVER_SOURCE") from error
        found.append((told, body[2:-1].replace("\n#|", "\n")))
        at = line_start(end)
    return found


def find_drivers(package: dict, folders: list[Path]) -> tuple[dict[str, LibraryDriver], list[dict]]:
    """The Library drivers a Spark Plug's devices name, read from `<folder>/<id>/`, and the
    references no folder holds. A folder whose version hash differs is not that driver."""
    from .library import LibraryError, read_version

    held: dict[str, LibraryDriver] = {}
    missing: list[dict] = []
    for device in package.get("devices", []):
        named = device["driver"]
        if isinstance(named, str) or named["sha256"] in held:
            continue
        for folder in folders:
            root = folder / named["id"]
            if not (root / "benchvista.json").is_file():
                continue
            try:
                version = read_version(root)
            except LibraryError:
                continue
            item = version.package
            if item["kind"] != "spark-driver" or item["version"] != named["version"] or version.sha256 != named["sha256"]:
                continue
            data = (root / item["entry"]).read_bytes()
            held[named["sha256"]] = LibraryDriver(item, version.sha256, data.decode("utf-8"), hashlib.sha256(data).hexdigest())
            break
        else:
            if named not in missing:
                missing.append(named)
    return held, missing


# ---------------------------------------------------------------------------------------------
# Opening a driver in CPython


class DriverCaseError(ValueError):
    """A driver case that did not run as expected, or a call the board would refuse."""


class _Frame:
    """`framebuf.FrameBuffer` as far as a case needs it: drawing calls are counted, not drawn."""

    def __init__(self, buffer: Any, width: int, height: int, mode: int, stride: int | None = None) -> None:
        self.buffer, self.width, self.height, self.mode = buffer, width, height, mode
        self.calls = 0

    def _drawn(self, *arguments: Any, **named: Any) -> None:
        self.calls += 1

    fill = pixel = hline = vline = line = rect = fill_rect = text = blit = scroll = ellipse = poly = _drawn


def _modules() -> dict[str, types.ModuleType]:
    framebuf = types.ModuleType("framebuf")
    framebuf.FrameBuffer = _Frame  # type: ignore[attr-defined]
    for value, name in enumerate(("MONO_VLSB", "RGB565", "GS4_HMSB", "MONO_HLSB", "MONO_HMSB", "GS2_HMSB", "GS8")):
        setattr(framebuf, name, value)
    micropython = types.ModuleType("micropython")
    micropython.const = lambda value: value  # type: ignore[attr-defined]
    return {"framebuf": framebuf, "micropython": micropython, "struct": struct}


@dataclass
class RecordingBus:
    """A driver's bus handle (`DeviceI2C`, `DeviceSPI`) that keeps what was sent. An I²C chip
    answers from `registers`; everything else reads as zeros."""

    bus: str
    registers: dict[int, bytes] = field(default_factory=dict)
    controls: tuple[str, ...] = ()
    writes: list[bytes] = field(default_factory=list)
    waited_ms: int = 0

    def _data(self, data: Any) -> bytes:
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) > 16384:
            raise DriverCaseError("SPARK_DEVICE_ARGUMENT")
        return bytes(data)

    def _count(self, size: Any) -> int:
        if type(size) is not int or not 0 <= size <= 16384:
            raise DriverCaseError("SPARK_DEVICE_ARGUMENT")
        return size

    def _register(self, at: Any) -> int:
        if self.bus != "i2c" or type(at) is not int or not 0 <= at <= 255:
            raise DriverCaseError("SPARK_DEVICE_ARGUMENT")
        return at

    def writeto(self, data: Any) -> None:
        self._register(0)
        self.writes.append(self._data(data))

    def readfrom(self, size: Any) -> bytes:
        self._register(0)
        return bytes(self._count(size))

    def writeto_mem(self, at: Any, data: Any) -> None:
        self.writes.append(bytes((self._register(at),)) + self._data(data))

    def readfrom_mem(self, at: Any, size: Any) -> bytes:
        start = self._register(at)
        answer = bytearray(self._count(size))
        for base, held in self.registers.items():
            for offset, value in enumerate(held):
                if 0 <= base + offset - start < len(answer):
                    answer[base + offset - start] = value
        return bytes(answer)

    def _spi(self) -> None:
        if self.bus != "spi":
            raise DriverCaseError("SPARK_DEVICE_ARGUMENT")

    def write(self, data: Any) -> None:
        self._spi()
        self.writes.append(self._data(data))

    def read(self, size: Any) -> bytes:
        self._spi()
        if "MISO" not in self.controls:
            raise DriverCaseError("SPARK_DEVICE_ARGUMENT")
        return bytes(self._count(size))

    def transfer(self, data: Any) -> bytes:
        self.write(data)
        return self.read(len(data))

    def has(self, name: Any) -> bool:
        return name in self.controls and name != "MISO"

    def pin(self, name: Any, value: Any) -> None:
        self._spi()
        if not self.has(name) or type(value) is not int or value not in (0, 1):
            raise DriverCaseError("SPARK_DEVICE_ARGUMENT")

    def wait_ms(self, milliseconds: Any) -> None:
        if type(milliseconds) is not int or not 0 <= milliseconds <= 200:
            raise DriverCaseError("SPARK_DEVICE_ARGUMENT")
        self.waited_ms += milliseconds


def load(source: str) -> Any:
    """The driver's `Driver` class, compiled with stand-ins for `framebuf` and `micropython`."""
    problem = source_problem(source)
    if problem is not None:
        raise DriverCaseError(" ".join(problem).strip())
    saved = {name: sys.modules.get(name) for name in ("framebuf", "micropython")}
    stand_ins = _modules()
    try:
        sys.modules.update({name: stand_ins[name] for name in saved})
        namespace: dict[str, Any] = {"__name__": "spark_driver"}
        exec(compile(source, "<spark-driver>", "exec"), namespace)  # noqa: S102 - the author's own code, at their desk
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    driver = namespace.get("Driver")
    if not isinstance(driver, type):
        raise DriverCaseError("LIBRARY_DRIVER_ENTRY")
    return driver


DISPLAY_CALLS = ("fill", "pixel", "hline", "vline", "line", "rect", "fill_rect", "text")


def run_case(package: dict, source: str, case: dict) -> list[str]:
    """Open the driver as a case says and make its calls; returns what differs from `expect`."""
    found = spec(package)
    settings = {name: allowed[0] for name, allowed in found["settings"].items()}
    for name, value in case.get("settings", {}).items():
        if name not in found["settings"] or value not in found["settings"][name]:
            raise DriverCaseError(f"LIBRARY_PLUG_DEVICE_SETTING {name}")
        settings[name] = value
    if "sizes" in found and [settings["width"], settings["height"]] not in found["sizes"]:
        raise DriverCaseError("LIBRARY_PLUG_DEVICE_SETTING size")
    declared = [line["name"] for line in found["lines"] if line["name"] in LINE_NAMES]
    connected = case.get("lines", declared)
    for line in found["lines"]:
        if line["name"] in LINE_NAMES and line["name"] not in connected and not line.get("optional"):
            raise DriverCaseError(f"LIBRARY_PLUG_PIN_UNBOUND {line['name']}")
    registers = {int(at): bytes.fromhex(held) for at, held in case.get("registers", {}).items()}
    bus = RecordingBus(found["bus"], registers, tuple(name for name in connected if name in declared))
    expect = case.get("expect", {})
    problems: list[str] = []
    error: str | None = None
    reading: Any = None
    try:
        driver = load(source)(bus, dict(settings))
        calls = case.get("calls", [["fill", 0], ["show"]] if found["kind"] == "display" else [["read"]])
        for call in calls:
            name, arguments = call[0], call[1:]
            if found["kind"] == "display" and name in DISPLAY_CALLS:
                getattr(driver.frame, name)(*arguments)
            elif found["kind"] == "display" and name == "show":
                driver.show()
            elif found["kind"] == "sensor" and name == "read":
                reading = dict(driver.read())
            else:
                raise DriverCaseError(f"unknown call {name}")
        if found["kind"] == "display":
            if (driver.width, driver.height) != (settings.get("width"), settings.get("height")):
                problems.append(f"the driver says {driver.width} x {driver.height}, the settings {settings.get('width')} x {settings.get('height')}")
            if found["colour"] != hasattr(driver, "colour"):
                problems.append("a display with RGB565 colours has colour(rgb565); a one-bit display does not")
    except DriverCaseError as failure:
        error = str(failure).split(" ")[0]
        if "error" not in expect:
            raise
    except ValueError as failure:
        error = str(failure)
        if "error" not in expect:
            raise DriverCaseError(f"the driver raised ValueError({error!r})") from failure
    if expect.get("error") != error:
        problems.append(f"error {error}, expected {expect.get('error')}")
    sent = b"".join(bus.writes)
    if "writes" in expect and len(bus.writes) != expect["writes"]:
        problems.append(f"{len(bus.writes)} bus writes, expected {expect['writes']}")
    if "bytes" in expect and len(sent) != expect["bytes"]:
        problems.append(f"{len(sent)} bytes sent, expected {expect['bytes']}")
    if "starts" in expect and not sent.hex().startswith(expect["starts"].lower()):
        problems.append(f"sent {sent[:16].hex()}…, expected it to start {expect['starts']}")
    for name, value in expect.get("reading", {}).items():
        got = (reading or {}).get(name)
        if not isinstance(got, (int, float)) or abs(got - value) > max(0.01, abs(value) * 1e-3):
            problems.append(f"reading {name} is {got}, expected {value}")
    if found["kind"] == "sensor" and reading is not None and sorted(reading) != sorted(package["readings"]):
        problems.append(f"read() gave {sorted(reading)}, the package says {sorted(package['readings'])}")
    return problems
