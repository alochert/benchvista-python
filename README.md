# benchvista

Write, check, test and publish [BenchVista](https://benchvista.com) **Spark Plugs** (Python programs
for a Spark board) and **Python analysis steps**. Both kinds are a folder with Python source and one
`benchvista.json`. A version is a number plus a SHA-256 over every file in the folder, so anyone can
check that a copy is exact.

This repository is published from the BenchVista product repository. Pull requests are welcome
here; accepted changes are applied there and published again.

## Install

Python 3.13 and [uv](https://docs.astral.sh/uv/):

```sh
uv tool install --python 3.13 git+https://github.com/alochert/benchvista-python
```

## Commands

| Command                                         | Does                                                                         |
| ----------------------------------------------- | ---------------------------------------------------------------------------- |
| `benchvista new spark-plug <dir>`               | Starts a Spark Plug: `benchvista.json`, `main.py`, a README and one test     |
| `benchvista new analysis-step <dir>`            | Starts an analysis step: `benchvista.json`, `step.py` and a README           |
| `benchvista new spark-driver <dir>`             | Starts a Spark driver: `benchvista.json`, `driver.py`, a README and one test |
| `benchvista check <dir>`                        | Checks the package and source; prints the version hash and every file's hash |
| `benchvista test <dir>`                         | Runs the item's tests                                                        |
| `benchvista build <dir>`                        | Writes `build/release.json` and `build/checksums.txt`                        |
| `benchvista collection build <repo> --out site` | Checks, tests and builds a whole collection's site                           |

Exit status 0 on success, 1 when a test fails, 2 when an item is refused. A refusal names a stable
code, such as `LIBRARY_PLUG_PIN_UNKNOWN`, and where it applies.

## A Spark Plug

```json
{
  "kind": "spark-plug",
  "id": "blink-led",
  "version": "0.1.0",
  "name": "Blink an LED",
  "summary": "Blinks one output pin and prints each blink.",
  "license": "CC0-1.0",
  "entry": "main.py",
  "pins": [
    { "role": "OUT", "direction": "output", "needs": "digital", "label": "LED through 1 kohm" }
  ],
  "settings": {
    "blinks": { "kind": "integer", "default": 5, "minimum": 1, "maximum": 50, "label": "Blinks" }
  },
  "timeoutMs": 60000,
  "sparkTaskApi": 1
}
```

```python
def main(spark):
    for count in range(SETTINGS["blinks"]):
        spark.write(PINS["OUT"], 1)
        yield spark.sleep_ms(250)
        spark.write(PINS["OUT"], 0)
        yield spark.sleep_ms(250)
        print("blink", count + 1)
```

Pins are named by role. The user picks a pad for each role once per board, and BenchVista writes
`PINS` and `SETTINGS` above your file when it installs it, so one Spark Plug works on every board.
`needs` is `digital`, `pwm` (an output) or `adc` (an input).

`benchvista test` runs each `tests/<case>.json` on a simulated Spark in CPython that keeps the
board's rules: no imports, classes or private names, declared pins only, the time limit and the
console limit. Wire an input to an output role, or give it readings:

```json
{
  "wiring": { "IN": "OUT" },
  "readings": { "IN": [0, 1] },
  "expect": { "lines": 5, "contains": ["blink 5"] }
}
```

A pass says the program keeps to the Spark contract. It says nothing about wiring, timing or a real
board.

### Devices

A Spark Plug with `"sparkTaskApi": 2` may use devices through drivers built into the board's Spark
application (0.2.0 or later, ESP32 and RP2 boards): `ssd1306` and `st7789` displays and a `bme280`
sensor. Declare each by role; the user picks a pad for each of its bus lines, and BenchVista writes
`DEVICES` above your file:

```json
"devices": [{ "role": "DISPLAY", "driver": "ssd1306", "label": "128 x 64 OLED", "settings": { "height": 64 } }]
```

`spark.device("DISPLAY")` returns what you draw on (`fill`, `pixel`, `hline`, `vline`, `line`,
`rect`, `fill_rect`, `text`, `show`, `width`, `height`); a sensor's `read()` returns named values.
`spark.i2c(scl, sda)` and `spark.spi(sck, mosi)` give raw buses on declared pads. In tests a
display keeps the texts of each frame it showed, and a case can check the last one; `devices`
gives a sensor's readings:

```json
{
  "devices": { "AIR": { "temperature_c": [21.5] } },
  "expect": { "display": { "DISPLAY": ["Count 3"] } }
}
```

See `examples/oled-counter`.

A device may also name a driver from the Library by one exact version, in place of a built-in
name. BenchVista then writes that driver into the file it installs (Spark application 0.3.0 or
later):

```json
"devices": [{
  "role": "DISPLAY",
  "driver": { "id": "sh1106", "version": "1.0.0", "sha256": "<the driver's version hash>" },
  "label": "128 x 64 SH1106 OLED"
}]
```

`benchvista` needs the driver's folder to check and test such a Spark Plug. It looks in
`../../spark-drivers/<id>/` (a collection's layout), beside the Spark Plug's folder, and in every
`--drivers <folder>`, and takes only the folder whose version hash is the one named. See
`examples/oled-seconds`.

## A Spark driver

A Spark driver is one MicroPython module for one display or sensor on I2C or SPI. Its
`benchvista.json` says what it is for:

```json
{
  "kind": "spark-driver",
  "id": "sh1106",
  "version": "1.0.0",
  "name": "SH1106 OLED display",
  "summary": "Drives a 128 x 64 SH1106 OLED display on I2C.",
  "license": "CC0-1.0",
  "entry": "driver.py",
  "device": "display",
  "bus": "i2c",
  "addresses": [60, 61],
  "settings": { "width": [128], "height": [64] },
  "sizes": [[128, 64]],
  "colour": "mono",
  "sparkTaskApi": 2
}
```

A display gives its panel `sizes` and its `colour` (`mono` or `rgb565`); a sensor gives its
`readings`, the names `read()` returns. An SPI driver gives `hz`, may give `mode`, and names the
`lines` it uses beyond SCK and MOSI (`MISO`, `CS`, `DC`, `RESET`, each `required` or `optional`).

The module has a class `Driver`. BenchVista calls `Driver(bus, settings)` with a handle that
reaches only this device, and the chosen settings:

| Bus  | The handle offers                                                                             |
| ---- | --------------------------------------------------------------------------------------------- |
| I2C  | `writeto(data)`, `readfrom(n)`, `writeto_mem(register, data)`, `readfrom_mem(register, n)`    |
| SPI  | `write(data)`, `pin(name, 0 or 1)`, `has(name)`, and `read(n)` and `transfer(data)` with MISO |
| Both | `wait_ms(ms)`, up to 200 ms                                                                   |

A display's driver has `width`, `height`, `frame` (a `framebuf.FrameBuffer` that the Spark Plug's
drawing calls go to) and `show()`, and `colour(rgb565)` when its colours are RGB565. A sensor's
has `read()`, which returns a dictionary of its readings.

**The rules for a driver's module.** BenchVista and the board both check them, and refuse a driver
that breaks one:

- at most 6,144 bytes, with LF line ends;
- the only imports are `import framebuf`, `import struct` and `from micropython import const`,
  each alone on a line;
- no `open`, `exec`, `eval`, `compile`, `globals`, `locals`, `getattr`, `setattr`, `delattr`,
  `type`, `object`, `super`, `vars`, `dir`, `help`, `input`, `breakpoint` or `print`;
- no name that starts with an underscore except `__init__`, no f-strings, and nothing outside
  ASCII except in comments and strings.

A driver and the Spark Plug that uses it share the board's 8,192 bytes.

A test case is `tests/<case>.json`. `benchvista test` opens the driver in CPython on a recording
bus: it checks what the driver sent, not a real device.

```json
{
  "registers": { "0": "1900" },
  "calls": [["read"]],
  "expect": { "reading": { "temperature_c": 25.0 } }
}
```

`registers` is what an I2C chip answers from a register on, in hex. `calls` are a display's drawing
calls and `show`, or a sensor's `read`. `expect` may give `error`, `writes` (bus writes), `bytes`
(bytes sent, a register write counting its register), `starts` (hex the sent bytes begin with) and
`reading`. See `examples/sh1106`.

## An analysis step

`benchvista.json` names the step and its licence. Inputs, outputs and parameters are declared in the
`Step` class in its entry file, and `fixtures/<case>/case.json` holds its test cases. See
`examples/glitch-finder`.

## Collections

A collection is a public GitHub repository with `collection.json` and one folder per item under
`spark-plugs/`, `spark-drivers/` and `analysis-steps/`. The action in this repository checks and tests every item,
publishes each new version as a GitHub Release named `<id>@<version>`, attests the index and builds
a site for GitHub Pages:

```yaml
- uses: alochert/benchvista-python@<commit>
  with:
    release: ${{ github.ref == 'refs/heads/main' }}
```

Publishing the same version number with different files is refused. Start from the official
collection, [alochert/benchvista-collection](https://github.com/alochert/benchvista-collection),
with **Use this template**.

Checksums and attestations prove which files you have and where they came from. They do not prove
that the code is correct or safe to run.

## Licence

Apache-2.0. The examples are CC0-1.0.
