"""The drivers a Spark Plug may name for a device (`docs/spark-drivers.md` §4, §5, stage 1).

The same catalogue as `src/library/spark-drivers.ts`; a test compares them. Each driver says its
bus, its lines with their direction (`both` is bidirectional, I²C SDA), its I²C addresses (the
first is the default), its settings (each one's first value is the default) and, for a display, the
panel sizes `[width, height]` its settings may make.
"""

from __future__ import annotations

I2C_LINES = ({"name": "SCL", "direction": "output"}, {"name": "SDA", "direction": "both"})
_I2C = I2C_LINES

DRIVERS: dict[str, dict] = {
    "ssd1306": {
        "bus": "i2c",
        "kind": "display",
        "lines": list(_I2C),
        "addresses": [0x3C, 0x3D],
        "settings": {"width": [128], "height": [64, 32]},
        "sizes": [[128, 64], [128, 32]],
    },
    "st7789": {
        "bus": "spi",
        "kind": "display",
        "lines": [
            {"name": "SCK", "direction": "output"},
            {"name": "MOSI", "direction": "output"},
            {"name": "CS", "direction": "output"},
            {"name": "DC", "direction": "output"},
            {"name": "RESET", "direction": "output", "optional": True},
        ],
        "settings": {"width": [240, 135], "height": [320, 240, 280], "rotation": [0, 1, 2, 3]},
        "sizes": [[240, 320], [240, 240], [240, 280], [135, 240]],
    },
    "bme280": {
        "bus": "i2c",
        "kind": "sensor",
        "lines": list(_I2C),
        "addresses": [0x76, 0x77],
        "settings": {},
    },
}


def line_key(role: str, line: str) -> str:
    """A device's line as the install choice names it: `DISPLAY.SCL`."""
    return f"{role}.{line}"


def device_problem(device: dict, driver: dict) -> tuple[str, str] | None:
    """Why a device's own address or settings do not fit its driver, as a `LIBRARY_PLUG_*` code
    and where below the device, or None."""

    def whole(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    if "address" in device and (not whole(device["address"]) or device["address"] not in driver.get("addresses", [])):
        return ("LIBRARY_PLUG_DEVICE_ADDRESS", "address")
    settings = device.get("settings", {})
    for name, value in settings.items():
        if name not in driver["settings"] or not whole(value) or value not in driver["settings"][name]:
            return ("LIBRARY_PLUG_DEVICE_SETTING", f"settings.{name}")
    if "sizes" in driver:
        chosen = [settings.get(name, driver["settings"][name][0]) for name in ("width", "height")]
        if chosen not in driver["sizes"]:
            return ("LIBRARY_PLUG_DEVICE_SETTING", "settings")
    return None


def device_entry(device: dict, pins: dict[str, str], library: dict | None = None) -> dict:
    """The header's entry for one device: its driver, address, pads and every setting. `library`
    holds the Library drivers at hand by version hash (`spark_driver.LibraryDriver`)."""
    from .spark_driver import device_driver, driver_name
    from .spark_plug import SparkPlugError

    driver = device_driver(device, library)
    if driver is None:
        raise SparkPlugError("LIBRARY_PLUG_DRIVER", driver_name(device["driver"]))
    problem = device_problem(device, driver)
    if problem is not None:
        raise SparkPlugError(problem[0], problem[1])
    pads: dict[str, str] = {}
    for line in driver["lines"]:
        key = line_key(device["role"], line["name"])
        if key not in pins:
            if line.get("optional"):
                continue
            raise SparkPlugError("LIBRARY_PLUG_PIN_UNBOUND", key)
        pads[line["name"]] = pins[key]
    settings = {name: device.get("settings", {}).get(name, allowed[0]) for name, allowed in sorted(driver["settings"].items())}
    entry: dict = {"driver": driver_name(device["driver"])}
    if driver["bus"] == "i2c":
        entry["address"] = device.get("address", driver["addresses"][0])
    entry["pins"] = pads
    entry["settings"] = settings
    return entry
