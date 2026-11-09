"""The runtime process's side of the pipe, without a sandbox: frames in, outputs and codes out.

The host side, the sandbox and the escapes are packages/connect-engine/tests/python-*.test.ts.
"""

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

from benchvista.frames import FrameError, Frames

SRC = Path(__file__).parents[1] / "src"
EXAMPLE = Path(__file__).parents[1] / "examples" / "glitch-finder"
BOOT = "import sys; sys.path[0:0] = [sys.argv[1], sys.argv[2]]; from benchvista.runner import main; raise SystemExit(main([sys.argv[2]]))"


def frames_of(data: bytes) -> list[tuple[dict, bytes]]:
    reader = Frames(io.BytesIO(data), io.BytesIO())
    out = []
    while True:
        try:
            out.append(reader.read())
        except FrameError:
            return out


def request(step_dir: Path, case: str) -> bytes:
    manifest = json.loads((step_dir / "step.json").read_text("utf-8"))
    folder = EXAMPLE / "fixtures" / case
    lines = json.loads((folder / "lines.manifest.json").read_text("utf-8"))
    params = json.loads((folder / "case.json").read_text("utf-8"))["params"]
    buffer = io.BytesIO()
    writer = Frames(io.BytesIO(), buffer)
    writer.write({"t": "hello", "protocol": 1, "manifest": manifest, "params": params, "seed": None, "outputs": planned(manifest, lines, params)})
    writer.write({"t": "input", "port": "lines", "manifest": lines})
    for sequence, chunk in enumerate(lines["chunks"]):
        writer.write({"t": "chunk", "port": "lines", "seq": sequence, "rows": chunk["rows"]}, (folder / f"lines.{sequence}.bin").read_bytes())
    writer.write({"t": "start"})
    return buffer.getvalue()


def planned(manifest: dict, lines: dict, params: dict) -> dict:
    from benchvista.manifest import output_types, resolve_params

    return output_types(manifest, {"lines": lines["type"]}, resolve_params(manifest, params, {"lines": lines["type"]}))


def run(step_dir: Path, case: str) -> list[tuple[dict, bytes]]:
    done = subprocess.run(
        [sys.executable, "-I", "-c", BOOT, str(SRC), str(step_dir)],
        input=request(step_dir, case),
        capture_output=True,
        timeout=60,
        check=False,
    )
    return frames_of(done.stdout)


def test_the_runner_answers_with_the_outputs_the_case_expects():
    received = run(EXAMPLE, "transitions-both")
    kinds = [header["t"] for header, _ in received]
    assert kinds[0] == "ready" and kinds[-1] == "done", kinds
    output = next(header for header, _ in received if header["t"] == "output")
    assert output["port"] == "glitches" and output["rows"] == 4
    assert output["clocks"][0]["id"] == "clk:fixture"


def test_code_that_differs_from_its_manifest_never_runs(tmp_path):
    copy = tmp_path / "glitch-finder"
    shutil.copytree(EXAMPLE, copy)
    with (copy / "step.py").open("a", encoding="utf-8") as handle:
        handle.write("\nraise SystemExit('this must never run')\n")
    received = run(copy, "uniform")
    assert received[-1][0] == {"t": "error", "code": "PYTHON_SOURCE_HASH", "where": "step.py"}
