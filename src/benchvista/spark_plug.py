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


def header(package: dict, version_sha256: str, pins: dict[str, str], settings: dict[str, Any]) -> str:
    """The lines BenchVista writes above the author's source."""
    if not re.fullmatch("[a-f0-9]{64}", version_sha256):
        raise SparkPlugError("LIBRARY_VERSION_HASH")
    roles = [pin["role"] for pin in package["pins"]]
    for role in pins:
        if role not in roles:
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
    return (
        f"# Written by BenchVista: {package['id']} {package['version']}, sha256 {version_sha256}\n"
        f"PINS = {{{', '.join(pin_text)}}}\n"
        f"SETTINGS = {{{', '.join(setting_text)}}}\n\n"
    )


def installed_source(package: dict, version_sha256: str, source: str, pins: dict[str, str], settings: dict[str, Any] | None = None) -> str:
    text = header(package, version_sha256, pins, settings or {}) + source
    if len(text.encode("utf-8")) > SOURCE_BYTES:
        raise SparkPlugError("LIBRARY_PLUG_SOURCE_SIZE", f"{len(text.encode('utf-8'))} bytes")
    return text


def task_package(package: dict, version_sha256: str, source: str, pins: dict[str, str], settings: dict[str, Any] | None = None) -> dict:
    """Today's device package (`SparkTaskPackage`) for one choice of pads. The board's own pin
    rules are checked by BenchVista against the board profile and again by the firmware."""
    return {
        "version": 1,
        "id": package["id"],
        "name": package["name"],
        "description": package["summary"],
        "source": installed_source(package, version_sha256, source, pins, settings),
        "pins": [{"pad": pins[pin["role"]], "direction": pin["direction"]} for pin in package["pins"]],
        "timeoutMs": package["timeoutMs"],
    }


def largest_header(package: dict) -> int:
    """Bytes of the longest header any board could need: 64-character pads, longest values."""
    pins = {pin["role"]: "X" * 64 for pin in package["pins"]}
    settings: dict[str, Any] = {}
    for name, setting in package.get("settings", {}).items():
        if setting["kind"] == "choice":
            settings[name] = max(setting["choices"], key=len)
        elif setting["kind"] == "toggle":
            settings[name] = False
        else:
            settings[name] = setting["default"]
    return len(header(package, "0" * 64, pins, settings).encode("utf-8")) + 24 * len(
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


def simulate(
    package: dict,
    source: str,
    *,
    settings: dict[str, Any] | None = None,
    wiring: dict[str, str] | None = None,
    readings: dict[str, list[int]] | None = None,
) -> Run:
    """Run a Spark Plug's entry source on simulated pads, as the board would, to completion."""
    pins = {pin["role"]: SIMULATED_PREFIX + pin["role"] for pin in package["pins"]}
    text = installed_source(package, "0" * 64, source, pins, settings)
    clock = [0]
    output: list[str] = []
    sizes = {"bytes": 0, "truncated": 0}
    spark = SimulatedSpark(package["pins"], wiring or {}, readings or {}, clock)

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
        return Run(state, error, "".join(output), clock[0], steps, sizes["truncated"], spark.result_value if spark.has_result else None, spark.calls)

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
