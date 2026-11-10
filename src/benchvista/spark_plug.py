"""Spark Plugs at the author's desk (`docs/library.md` §4, §6).

`installed_source` writes the header BenchVista puts above a Spark Plug's source when it installs
it on a board, byte for byte as `src/library/spark-plug.ts` does. `simulate` runs a Spark Plug
under the board's own rules (the firmware's source profile, its restricted built-ins, declared
pins only, the time limit and the console limit) against simulated pins, for `benchvista test`.

The simulation runs in CPython, not MicroPython, and its pins are not electrical: a pass means
the program keeps to the Spark contract and gives the expected console output for the inputs a
case describes. It says nothing about wiring, timing or a real board.
"""

from __future__ import annotations

import json
import re
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .canonical import js_number
from .spark_driver import LibraryDriver, block, device_driver, driver_name, record, source_problem
from .spark_drivers import DRIVERS, device_entry, line_key  # noqa: F401 - DRIVERS is part of this module's surface

SOURCE_BYTES = 8192
OUTPUT_BYTES = 4096
MAX_SLEEP_MS = 60000
#: Simulated pads are named after their role, so a source that names a board pad directly fails.
SIMULATED_PREFIX = "SIM:"

# The firmware's lexical source profile (`firmware/spark/device/spark/scripts.py`).
UNSUPPORTED = (
    "import", "from", "class", "open", "exec", "eval", "compile", "globals", "locals", "getattr",
    "setattr", "delattr", "type", "object", "super", "vars", "dir", "help", "input", "breakpoint",
)  # fmt: skip
LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
TOKEN = re.compile("[A-Za-z_][A-Za-z_0-9]*")
SPAN = re.compile("[^A-Za-z_#\"']+")


class SparkPlugError(ValueError):
    """A Spark Plug the board would refuse, or a failed simulated run: a `SPARK_*` or `LIBRARY_*` code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} {detail}".strip())
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------------------------
# The installed file


def _setting_literal(setting: dict, value: Any) -> str:
    kind = setting["kind"]
    if kind == "toggle":
        if not isinstance(value, bool):
            raise SparkPlugError("LIBRARY_PLUG_SETTING_VALUE")
        return "True" if value else "False"
    if kind == "choice":
        if not isinstance(value, str) or value not in setting["choices"]:
            raise SparkPlugError("LIBRARY_PLUG_SETTING_VALUE")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        raise SparkPlugError("LIBRARY_PLUG_SETTING_VALUE")
    if kind == "integer" and not float(value).is_integer():
        raise SparkPlugError("LIBRARY_PLUG_SETTING_VALUE")
    low, high = setting.get("minimum"), setting.get("maximum")
    if (low is not None and value < low) or (high is not None and value > high):
        raise SparkPlugError("LIBRARY_PLUG_SETTING_RANGE")
    return js_number(float(value))


Drivers = dict[str, LibraryDriver]


def _lines(device: dict, drivers: Drivers | None) -> list[dict]:
    return (device_driver(device, drivers) or {"lines": []})["lines"]


def _driver_blocks(package: dict, drivers: Drivers | None) -> str:
    """Each Library driver the devices name, once, in the devices' order, as comment lines. On the
    board a driver goes by its name, so one name is one driver in a Spark Plug."""
    text = ""
    named: dict[str, str] = {}
    built_in = {device["driver"] for device in package.get("devices", []) if isinstance(device["driver"], str)}
    for device in package.get("devices", []):
        wanted = device["driver"]
        if isinstance(wanted, str):
            continue
        held = (drivers or {}).get(wanted["sha256"])
        if held is None or device_driver(device, drivers) is None or wanted["id"] in built_in:
            raise SparkPlugError("LIBRARY_PLUG_DRIVER", wanted["id"])
        if wanted["id"] in named:
            if named[wanted["id"]] != wanted["sha256"]:
                raise SparkPlugError("LIBRARY_PLUG_DRIVER", wanted["id"])
            continue
        named[wanted["id"]] = wanted["sha256"]
        problem = source_problem(held.source)
        if problem is not None:
            raise SparkPlugError(problem[0], problem[1])
        text += block(record(held.package, held.source_sha256), held.source)
    return text


def header(package: dict, version_sha256: str, pins: dict[str, str], settings: dict[str, Any], drivers: Drivers | None = None) -> str:
    """The lines BenchVista writes above the author's source."""
    if not re.fullmatch("[a-f0-9]{64}", version_sha256):
        raise SparkPlugError("LIBRARY_VERSION_HASH")
    roles = [pin["role"] for pin in package["pins"]]
    # A driver that is not at hand is the first thing to say: its lines are not known without it.
    for device in package.get("devices", []):
        if device_driver(device, drivers) is None:
            raise SparkPlugError("LIBRARY_PLUG_DRIVER", driver_name(device["driver"]))
    line_keys = {line_key(device["role"], line["name"]) for device in package.get("devices", []) for line in _lines(device, drivers)}
    for role in pins:
        if role not in roles and role not in line_keys:
            raise SparkPlugError("LIBRARY_PLUG_PIN_UNKNOWN", role)
    pin_text = []
    for role in roles:
        if role not in pins:
            raise SparkPlugError("LIBRARY_PLUG_PIN_UNBOUND", role)
        pin_text.append(f"{json.dumps(role)}: {json.dumps(pins[role])}")
    declared = package.get("settings", {})
    for name in settings:
        if name not in declared:
            raise SparkPlugError("LIBRARY_PLUG_SETTING_UNKNOWN", name)
    setting_text = [
        f"{json.dumps(name)}: {_setting_literal(declared[name], settings.get(name, declared[name]['default']))}"
        for name in sorted(declared)
    ]
    # Devices only where the package has them, so a task API 1 header is unchanged.
    devices = package.get("devices", [])
    device_text = (
        "DEVICES = {" + ", ".join(f"{json.dumps(device['role'])}: {_literal(device_entry(device, pins, drivers))}" for device in devices) + "}\n"
        if devices
        else ""
    )
    return (
        f"# Written by BenchVista: {package['id']} {package['version']}, sha256 {version_sha256}\n"
        f"PINS = {{{', '.join(pin_text)}}}\n"
        f"SETTINGS = {{{', '.join(setting_text)}}}\n"
        f"{device_text}{_driver_blocks(package, drivers)}\n"
    )


def _literal(value: Any) -> str:
    """JSON of strings, integers and nested dicts, as `src/library/spark-plug.ts` writes it."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key)}: {_literal(entry)}" for key, entry in value.items()) + "}"
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return json.dumps(value)
    raise SparkPlugError("LIBRARY_PLUG_DEVICE_SETTING", repr(value))


def installed_source(
    package: dict, version_sha256: str, source: str, pins: dict[str, str], settings: dict[str, Any] | None = None, drivers: Drivers | None = None
) -> str:
    text = header(package, version_sha256, pins, settings or {}, drivers) + source
    if len(text.encode("utf-8")) > SOURCE_BYTES:
        raise SparkPlugError("LIBRARY_PLUG_SOURCE_SIZE", f"{len(text.encode('utf-8'))} bytes")
    return text


def task_package(
    package: dict, version_sha256: str, source: str, pins: dict[str, str], settings: dict[str, Any] | None = None, drivers: Drivers | None = None
) -> dict:
    """Today's device package (`SparkTaskPackage`) for one choice of pads. The board's own pin
    rules are checked by BenchVista against the board profile and again by the firmware."""
    text = installed_source(package, version_sha256, source, pins, settings, drivers)
    declared = [{"pad": pins[pin["role"]], "direction": pin["direction"]} for pin in package["pins"]] + _device_pins(package, pins, drivers)
    if len(declared) > 16:
        raise SparkPlugError("LIBRARY_PLUG_PINS")
    return {
        "version": 1,
        "id": package["id"],
        "name": package["name"],
        "description": package["summary"],
        "source": text,
        "pins": declared,
        "timeoutMs": package["timeoutMs"],
    }


def _device_pins(package: dict, pins: dict[str, str], drivers: Drivers | None = None) -> list[dict]:
    """A device's bus lines as declared pads; a bus two I²C devices share is declared once."""
    declared: list[dict] = []
    seen: set[str] = set()
    for device in package.get("devices", []):
        entry = device_entry(device, pins, drivers)
        for line in _lines(device, drivers):
            pad = entry["pins"].get(line["name"])
            if pad is None or pad in seen:
                continue
            seen.add(pad)
            declared.append({"pad": pad, "direction": "input" if line["direction"] == "input" else "output"})
    return declared


def largest_header(package: dict, drivers: Drivers | None = None) -> int:
    """Bytes of the longest header any board could need: 64-character pads, longest values, and
    every Library driver the devices name."""
    pins = {pin["role"]: "X" * 64 for pin in package["pins"]}
    for device in package.get("devices", []):
        for line in _lines(device, drivers):
            pins[line_key(device["role"], line["name"])] = "X" * 64
    settings: dict[str, Any] = {}
    for name, setting in package.get("settings", {}).items():
        if setting["kind"] == "choice":
            settings[name] = max(setting["choices"], key=len)
        elif setting["kind"] == "toggle":
            settings[name] = False
        else:
            settings[name] = setting["default"]
    return len(header(package, "0" * 64, pins, settings, drivers).encode("utf-8")) + 24 * len(
        [s for s in package.get("settings", {}).values() if s["kind"] in ("quantity", "integer")]
    )


# ---------------------------------------------------------------------------------------------
# The firmware's source profile


def _quoted_end(text: str, start: int, quote: str) -> int:
    """The firmware's `wire.quoted_end`: the index after an unescaped closing quote. The compiler
    still judges the literal's syntax."""
    end = text.find(quote, start)
    while end >= 0:
        previous = end - 1
        while previous >= start and text[previous] == "\\":
            previous -= 1
        if (end - 1 - previous) % 2 == 0:
            return end + len(quote)
        end = text.find(quote, end + 1)
    return len(text) + 1


def source_profile(source: str) -> None:
    """Refuse what the board refuses before compiling: imports, classes, private names, f-strings
    and non-ASCII identifiers (`scripts.py` `source_profile`)."""
    index, size = 0, len(source)
    while index < size:
        char = source[index]
        if char == "#":
            endings = [at for at in (source.find("\r", index), source.find("\n", index)) if at >= 0]
            index = min(endings) if endings else size
        elif char in ('"', "'"):
            quote = char * 3 if source[index : index + 3] == char * 3 else char
            index = _quoted_end(source, index + len(quote), quote)
        elif char in LETTERS:
            match = TOKEN.match(source, index)
            assert match is not None
            token = match.group(0)
            index += len(token)
            if token in UNSUPPORTED or token.startswith("_"):
                raise SparkPlugError("SPARK_SCRIPT_UNSUPPORTED_TOKEN", token)
            if index < size and source[index] in ('"', "'") and "f" in token.lower():
                raise SparkPlugError("SPARK_SCRIPT_FSTRING_UNSUPPORTED")
        else:
            match = SPAN.match(source, index)
            assert match is not None
            span = match.group(0)
            if max(span) >= "\x80":
                raise SparkPlugError("SPARK_SCRIPT_IDENTIFIER_UNSUPPORTED")
            index += len(span)


# ---------------------------------------------------------------------------------------------
# The simulated Spark


@dataclass
class Run:
    state: str  # "complete" or "fault"
    error: str | None
    output: str
    elapsed_ms: int
    steps: int
    truncated_bytes: int
    result: Any = None
    calls: list[tuple] = field(default_factory=list)
    #: Each display's shown frames: the texts drawn in each frame, in order, as (x, y, text).
    frames: dict[str, list[list[tuple[int, int, str]]]] = field(default_factory=dict)


class _StepBudget(Exception):
    pass


#: Wall-clock limit for one step between yields. The board allows 100 ms and a watchdog resets it;
#: here a generous limit only stops a loop that never yields from hanging the command.
STEP_SECONDS = 2.0


@contextmanager
def _step_guard() -> Iterator[None]:
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expired(signum: int, frame: Any) -> None:
        raise _StepBudget

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, STEP_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class SimulatedSpark:
    """The task API (`spark.read`, `write`, `pwm`, `adc_read_u16`, `sleep_ms`, `result`) on
    simulated pads. An input follows the output role it is wired to, or a list of readings."""

    def __init__(self, pins: list[dict], wiring: dict[str, str], readings: dict[str, list[int]], clock: list[int]) -> None:
        self._pins = {SIMULATED_PREFIX + pin["role"]: pin for pin in pins}
        self._wiring = {SIMULATED_PREFIX + role: SIMULATED_PREFIX + source for role, source in wiring.items()}
        self._readings = {SIMULATED_PREFIX + role: list(values) for role, values in readings.items()}
        self._levels: dict[str, float] = {}
        self._modes: dict[str, str] = {}
        self._clock = clock
        self.calls: list[tuple] = []
        self.has_result = False
        self.result_value: Any = None
        self.devices: dict[str, Any] = {}
        #: The pads a raw bus may use: the declared pins and the devices' bus lines.
        self.bus_pads: set[str] = set(self._pins)

    def _check(self, pad: Any, direction: str, mode: str) -> dict:
        pin = self._pins.get(pad) if isinstance(pad, str) else None
        if pin is None or pin["direction"] != direction:
            raise SparkPlugError("SPARK_SCRIPT_PIN_PERMISSION", str(pad))
        if self._modes.setdefault(pad, mode) != mode:
            raise SparkPlugError("SPARK_SCRIPT_PIN_MODE", str(pad))
        return pin

    def _reading(self, pad: str, scale: int) -> int:
        values = self._readings.get(pad)
        if values:
            value = values.pop(0) if len(values) > 1 else values[0]
            return int(value)
        source = self._wiring.get(pad)
        level = self._levels.get(source, 0.0) if source else 0.0
        return round(level * scale)

    def read(self, pad: Any) -> int:
        self._check(pad, "input", "digital-input")
        value = self._reading(pad, 1)
        self.calls.append((self._clock[0], "read", pad, value))
        return 1 if value else 0

    def write(self, pad: Any, value: Any) -> None:
        self._check(pad, "output", "digital-output")
        if type(value) is not int or value not in (0, 1):
            raise SparkPlugError("SPARK_LEVEL")
        self._levels[pad] = float(value)
        self.calls.append((self._clock[0], "write", pad, value))

    def pwm(self, pad: Any, frequency_hz: Any, duty_u16: Any) -> None:
        pin = self._check(pad, "output", "pwm")
        if pin["needs"] != "pwm":
            raise SparkPlugError("LIBRARY_PLUG_PIN_NEEDS", f"{pad} is declared {pin['needs']}")
        if type(frequency_hz) is not int or not 1 <= frequency_hz <= 10_000_000:
            raise SparkPlugError("SPARK_FREQUENCY")
        if type(duty_u16) is not int or not 0 <= duty_u16 <= 65535:
            raise SparkPlugError("SPARK_DUTY")
        self._levels[pad] = duty_u16 / 65535
        self.calls.append((self._clock[0], "pwm", pad, frequency_hz, duty_u16))

    def adc_read_u16(self, pad: Any) -> int:
        pin = self._check(pad, "input", "adc")
        if pin["needs"] != "adc":
            raise SparkPlugError("LIBRARY_PLUG_PIN_NEEDS", f"{pad} is declared {pin['needs']}")
        value = max(0, min(65535, self._reading(pad, 65535)))
        self.calls.append((self._clock[0], "adc", pad, value))
        return value

    @staticmethod
    def sleep_ms(milliseconds: Any) -> int:
        if type(milliseconds) is not int or not 0 <= milliseconds <= MAX_SLEEP_MS:
            raise SparkPlugError("SPARK_INTEGER")
        return milliseconds

    def result(self, value: Any) -> None:
        self.has_result = True
        self.result_value = value

    def device(self, role: Any) -> Any:
        """The device the package declares under `role`, through its driver (task API 2)."""
        found = self.devices.get(role) if isinstance(role, str) else None
        if found is None:
            raise SparkPlugError("SPARK_DEVICE_UNKNOWN", str(role))
        return found

    def _bus_pads(self, pads: tuple) -> None:
        for pad in pads:
            if pad is not None and (not isinstance(pad, str) or pad not in self.bus_pads):
                raise SparkPlugError("SPARK_SCRIPT_PIN_PERMISSION", str(pad))
        if len({pad for pad in pads if pad is not None}) != len([pad for pad in pads if pad is not None]):
            raise SparkPlugError("SPARK_PIN_COLLISION")

    def i2c(self, scl: Any, sda: Any, frequency_hz: Any = 100_000) -> SimulatedBus:
        """A raw I²C bus on two declared pads (task API 2)."""
        self._bus_pads((scl, sda))
        if type(frequency_hz) is not int or not 10_000 <= frequency_hz <= 1_000_000:
            raise SparkPlugError("SPARK_FREQUENCY")
        return SimulatedBus("i2c", self.calls, self._clock)

    def spi(self, sck: Any, mosi: Any, miso: Any = None, baudrate: Any = 1_000_000, mode: Any = 0) -> SimulatedBus:
        """A raw SPI bus on declared pads (task API 2)."""
        self._bus_pads((sck, mosi, miso))
        if type(baudrate) is not int or not 10_000 <= baudrate <= 80_000_000 or mode not in (0, 1, 2, 3):
            raise SparkPlugError("SPARK_FREQUENCY")
        return SimulatedBus("spi", self.calls, self._clock)


class SimulatedDisplay:
    """A display's drawing calls (`framebuf`'s and `show`), as the board's display drivers offer
    them (`docs/spark-drivers.md` §5). Nothing is rasterised: each shown frame keeps the texts it
    drew, which a case can check, and every call is checked as the board checks it."""

    def __init__(self, width: int, height: int, colour: bool) -> None:
        self.width = width
        self.height = height
        self._colour = colour
        self._texts: list[tuple[int, int, str]] = []
        self.frames: list[list[tuple[int, int, str]]] = []

    def _int(self, *values: Any) -> None:
        for value in values:
            if type(value) is not int or not -4096 <= value <= 4096:
                raise SparkPlugError("SPARK_DEVICE_ARGUMENT")

    def _colour_value(self, colour: Any) -> None:
        maximum = 0xFFFF if self._colour else 1
        if type(colour) is not int or not 0 <= colour <= maximum:
            raise SparkPlugError("SPARK_DEVICE_ARGUMENT")

    def fill(self, colour: Any) -> None:
        self._colour_value(colour)
        self._texts = []

    def pixel(self, x: Any, y: Any, colour: Any) -> None:
        self._int(x, y)
        self._colour_value(colour)

    def hline(self, x: Any, y: Any, w: Any, colour: Any) -> None:
        self._int(x, y, w)
        self._colour_value(colour)

    def vline(self, x: Any, y: Any, h: Any, colour: Any) -> None:
        self._int(x, y, h)
        self._colour_value(colour)

    def line(self, x1: Any, y1: Any, x2: Any, y2: Any, colour: Any) -> None:
        self._int(x1, y1, x2, y2)
        self._colour_value(colour)

    def rect(self, x: Any, y: Any, w: Any, h: Any, colour: Any) -> None:
        self._int(x, y, w, h)
        self._colour_value(colour)

    def fill_rect(self, x: Any, y: Any, w: Any, h: Any, colour: Any) -> None:
        self._int(x, y, w, h)
        self._colour_value(colour)

    def text(self, text: Any, x: Any, y: Any, colour: Any = 1) -> None:
        if type(text) is not str or len(text) > 64:
            raise SparkPlugError("SPARK_DEVICE_ARGUMENT")
        self._int(x, y)
        self._colour_value(colour)
        self._texts.append((x, y, text))

    def show(self) -> None:
        self.frames.append(list(self._texts))


#: What a BME280 reads when a case gives no value: temperature in °C, pressure in Pa, humidity in %.
BME280_READINGS = {"temperature_c": 20.0, "pressure_pa": 101325.0, "humidity_pct": 40.0}


class SimulatedSensor:
    """A sensor's `read()`: its driver's named values from the case's `devices` readings, each a
    list taken in turn (the last one repeats). A value the case leaves out is the default."""

    def __init__(self, readings: dict[str, list[float]], defaults: dict[str, float] | None = None) -> None:
        self._readings = {name: list(values) for name, values in readings.items()}
        self._defaults = BME280_READINGS if defaults is None else defaults

    def read(self) -> dict:
        reading = {}
        for name, default in self._defaults.items():
            values = self._readings.get(name) or [default]
            reading[name] = float(values.pop(0) if len(values) > 1 else values[0])
        return reading


class SimulatedBus:
    """A raw I²C or SPI bus on declared pads (`spark.i2c`, `spark.spi`): writes are recorded and
    reads answer zeros, so a Spark Plug's own protocol code runs without its chip."""

    def __init__(self, kind: str, calls: list[tuple], clock: list[int]) -> None:
        self._kind, self._calls, self._clock = kind, calls, clock

    def _bytes(self, data: Any) -> bytes:
        if type(data) is not bytes or len(data) > 4096:
            raise SparkPlugError("SPARK_DEVICE_ARGUMENT")
        return data

    def _count(self, count: Any) -> int:
        if type(count) is not int or not 0 <= count <= 4096:
            raise SparkPlugError("SPARK_DEVICE_ARGUMENT")
        return count

    def scan(self) -> list:
        return []

    def writeto(self, address: Any, data: Any) -> None:
        self._calls.append((self._clock[0], "i2c-write", address, self._bytes(data)))

    def readfrom(self, address: Any, count: Any) -> bytes:
        return bytes(self._count(count))

    def writeto_mem(self, address: Any, register: Any, data: Any) -> None:
        self._calls.append((self._clock[0], "i2c-write", address, register, self._bytes(data)))

    def readfrom_mem(self, address: Any, register: Any, count: Any) -> bytes:
        return bytes(self._count(count))

    def write(self, data: Any) -> None:
        self._calls.append((self._clock[0], "spi-write", self._bytes(data)))

    def read(self, count: Any) -> bytes:
        return bytes(self._count(count))

    def transfer(self, data: Any) -> bytes:
        self._calls.append((self._clock[0], "spi-write", self._bytes(data)))
        return bytes(len(data))


def simulate(
    package: dict,
    source: str,
    *,
    settings: dict[str, Any] | None = None,
    wiring: dict[str, str] | None = None,
    readings: dict[str, list[int]] | None = None,
    devices: dict[str, dict[str, list[float]]] | None = None,
    drivers: Drivers | None = None,
) -> Run:
    """Run a Spark Plug's entry source on simulated pads, as the board would, to completion.
    A device answers through a simulated driver; `devices` gives a sensor's readings by role.
    `drivers` holds the Library drivers the devices name: their declarations decide the simulated
    device, and their modules are written into the installed file but not run here."""
    pins = {pin["role"]: SIMULATED_PREFIX + pin["role"] for pin in package["pins"]}
    for device in package.get("devices", []):
        for line in _lines(device, drivers):
            key = line_key(device["role"], line["name"])
            pins[key] = SIMULATED_PREFIX + key
    text = installed_source(package, "0" * 64, source, pins, settings, drivers)
    clock = [0]
    output: list[str] = []
    sizes = {"bytes": 0, "truncated": 0}
    spark = SimulatedSpark(package["pins"], wiring or {}, readings or {}, clock)
    for device in package.get("devices", []):
        driver = device_driver(device, drivers)
        if driver is None:
            raise SparkPlugError("LIBRARY_PLUG_DRIVER", str(device["driver"]))
        entry = device_entry(device, pins, drivers)
        spark.bus_pads.update(entry["pins"].values())
        named = device["driver"]
        if driver["kind"] == "display":
            colour = named != "ssd1306" if isinstance(named, str) else driver["colour"]
            spark.devices[device["role"]] = SimulatedDisplay(entry["settings"]["width"], entry["settings"]["height"], colour)
        else:
            held = None if isinstance(named, str) else (drivers or {})[named["sha256"]]
            defaults = None if held is None else {name: 0.0 for name in held.package["readings"]}
            spark.devices[device["role"]] = SimulatedSensor((devices or {}).get(device["role"], {}), defaults)

    def emit(*values: Any, sep: str = " ", end: str = "\n") -> None:
        if type(sep) is not str or type(end) is not str:
            raise SparkPlugError("SPARK_SCRIPT_PRINT")
        data = (sep.join(str(value) for value in values) + end).encode("utf-8")
        room = max(0, OUTPUT_BYTES - sizes["bytes"])
        accepted = data[: min(len(data), 512, room)]
        sizes["bytes"] += len(accepted)
        sizes["truncated"] += len(data) - len(accepted)
        output.append(accepted.decode("utf-8", errors="ignore"))

    safe = {
        "abs": abs, "all": all, "any": any, "bool": bool, "bytes": bytes, "dict": dict,
        "enumerate": enumerate, "float": float, "int": int, "len": len, "list": list, "max": max,
        "min": min, "range": range, "round": round, "str": str, "sum": sum, "tuple": tuple,
        "zip": zip, "Exception": Exception, "ValueError": ValueError, "print": emit,
    }  # fmt: skip
    namespace: dict[str, Any] = dict(safe)
    namespace["__builtins__"] = safe
    steps = 0

    def finish(state: str, error: str | None) -> Run:
        frames = {role: found.frames for role, found in spark.devices.items() if isinstance(found, SimulatedDisplay)}
        return Run(state, error, "".join(output), clock[0], steps, sizes["truncated"], spark.result_value if spark.has_result else None, spark.calls, frames)

    try:
        source_profile(text)
        with _step_guard():
            exec(compile(text, "<spark-script>", "exec"), namespace)  # noqa: S102 - the author's own code, at their desk
            main = namespace.get("main")
            if not callable(main):
                return finish("fault", "SPARK_SCRIPT_MAIN")
            iterator: Any = main(spark)
        if not isinstance(iterator, Iterator):
            if iterator is not None:
                spark.result(iterator)
            return finish("complete", None)
        while True:
            if clock[0] >= package["timeoutMs"]:
                return finish("fault", "SPARK_SCRIPT_TIMEOUT")
            try:
                with _step_guard():
                    delay = next(iterator)
            except StopIteration as done:
                if done.value is not None:
                    spark.result(done.value)
                return finish("complete", None)
            if type(delay) is not int or not 0 <= delay <= MAX_SLEEP_MS:
                return finish("fault", "SPARK_INTEGER")
            steps += 1
            clock[0] += max(delay, 1)
    except SparkPlugError as error:
        return finish("fault", error.code)
    except _StepBudget:
        return finish("fault", "SPARK_SCRIPT_STEP_BUDGET")
    except SyntaxError:
        return finish("fault", "SPARK_SCRIPT_SYNTAX")
    except Exception as error:  # noqa: BLE001 - any exception in user code faults the run, as on the board
        return finish("fault", f"SPARK_SCRIPT_EXCEPTION {type(error).__name__}: {error}")
