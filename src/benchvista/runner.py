"""The runtime process a host starts in its sandbox for one step run (§5.1, §15.5).

The host's bootstrap has already applied the limits and, on Linux, the process filter
(`sandbox.py`), and put the SDK and the step's directory on `sys.path`. This then:

1. says `ready`;
2. reads `hello` (the manifest, the params as planned, the seed, the planned output types),
   each `input` with its `chunk` frames, and `start`;
3. checks every source file against the manifest's SHA-256 before importing any of it;
4. runs the step (`execute.run_step`) and sends each output as an `output` header and its
   `columnar-le/v1` chunks, then `done`; or `error` with a code.

The host validates every output again; nothing it receives here is trusted.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import time
from pathlib import Path

from . import __version__
from .dataset import Dataset, DatasetError, read
from .execute import RunError, run_step
from .frames import PROTOCOL, FrameError, Frames
from .manifest import output_types


def _source_checked(step_directory: Path, manifest: dict) -> None:
    for entry in manifest["source"]["files"]:
        path = (step_directory / entry["path"]).resolve()
        if step_directory.resolve() not in path.parents:
            raise RunError("PYTHON_SOURCE_PATH", entry["path"])
        try:
            data = path.read_bytes()
        except OSError as error:
            raise RunError("PYTHON_SOURCE_MISSING", entry["path"]) from error
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise RunError("PYTHON_SOURCE_HASH", entry["path"])


def _load(manifest: dict) -> type:
    module_name, _, class_name = manifest["source"]["entry"].partition(":")
    module = importlib.import_module(module_name)
    step = getattr(module, class_name, None)
    if not isinstance(step, type):
        raise RunError("PYTHON_SOURCE_ENTRY", manifest["source"]["entry"])
    return step


def _serve(frames: Frames, step_directory: Path) -> None:
    import numpy

    frames.write({"t": "ready", "protocol": PROTOCOL, "python": sys.version.split()[0], "sdk": __version__, "numpy": numpy.__version__})
    frames.flush()
    hello, _ = frames.read()
    if hello["t"] != "hello" or hello.get("protocol") != PROTOCOL:
        raise RunError("PYTHON_PROTOCOL")
    manifest = hello["manifest"]
    params = hello.get("params", {})
    seed = hello.get("seed")
    planned = hello["outputs"]
    declared: dict[str, dict] = {}
    received: dict[str, list[bytes]] = {}
    while True:
        header, body = frames.read()
        kind = header["t"]
        if kind == "start":
            break
        if kind == "input":
            declared[header["port"]] = header["manifest"]
            received[header["port"]] = []
        elif kind == "chunk":
            port = header["port"]
            if port not in received or header["seq"] != len(received[port]):
                raise RunError("PYTHON_PROTOCOL", "chunk out of order")
            received[port].append(body)
        elif kind == "cancel":
            raise RunError("PYTHON_CANCELLED")
        else:
            raise RunError("PYTHON_PROTOCOL", kind)
    try:
        inputs: dict[str, Dataset] = {port: read(declared[port], received[port]) for port in declared}
    except (DatasetError, ValueError) as error:
        raise RunError("PYTHON_INPUT_DECODE", str(error)) from error
    _source_checked(step_directory, manifest)
    step = _load(manifest)
    last = [0.0]

    def progress(fraction: float) -> None:
        now = time.monotonic()
        if now - last[0] >= 0.1:
            last[0] = now
            frames.write({"t": "progress", "fraction": fraction})
            frames.flush()

    result = run_step(step, manifest, inputs, params, seed=seed, on_progress=progress)
    expected = output_types(manifest, {port: dataset.type for port, dataset in inputs.items()}, params)
    for port, dataset in result.outputs.items():
        if expected.get(port) != planned.get(port):
            # The planner and this resolver disagree; the host refuses the output either way.
            raise RunError("PYTHON_OUTPUT_TYPE", port)
        manifest_out = dataset.manifest
        header = {
            "t": "output",
            "port": port,
            "type": manifest_out["type"],
            "rows": manifest_out["rows"],
            "clocks": manifest_out["clocks"],
            "quality": manifest_out["quality"],
            "chunks": [{"rows": chunk["rows"], "sha256": chunk["sha256"]} for chunk in manifest_out["chunks"]],
        }
        if "extent" in manifest_out:
            header["extent"] = manifest_out["extent"]
        if "scalar" in manifest_out:
            header["scalar"] = manifest_out["scalar"]
        frames.write(header)
        for sequence, chunk in enumerate(dataset.chunks):
            frames.write({"t": "chunk", "port": port, "seq": sequence, "rows": manifest_out["chunks"][sequence]["rows"]}, chunk)
    frames.write({"t": "done", "status": result.status, "diagnostics": result.diagnostics})
    frames.flush()


def main(argv: list[str]) -> int:
    """`argv[0]` is the step's directory (already on `sys.path`)."""
    frames = Frames(sys.stdin.buffer, sys.stdout.buffer)
    step_directory = Path(argv[0])
    try:
        _serve(frames, step_directory)
        return 0
    except RunError as error:
        code, where = error.code, error.where
    except FrameError as error:
        code, where = error.code, ""
    except MemoryError:
        code, where = "PYTHON_MEMORY", ""
    except Exception as error:  # the runner's own failure: reported, never a traceback on the pipe
        code, where = "PYTHON_RUNNER_FAILED", type(error).__name__
    try:
        frames.write({"t": "error", "code": code, "where": where[:200]})
        frames.flush()
    except (OSError, FrameError):
        pass
    return 1
