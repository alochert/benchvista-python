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

## An analysis step

`benchvista.json` names the step and its licence. Inputs, outputs and parameters are declared in the
`Step` class in its entry file, and `fixtures/<case>/case.json` holds its test cases. See
`examples/glitch-finder`.

## Collections

A collection is a public GitHub repository with `collection.json` and one folder per item under
`spark-plugs/` and `analysis-steps/`. The action in this repository checks and tests every item,
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
