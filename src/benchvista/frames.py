"""The pipe protocol between a host and its runtime process (`docs/analysis-python-steps.md` §15.5).

A frame is a little-endian u32 header length, the header as UTF-8 JSON, a little-endian u32
body length and the body. A header is at most 1 MiB and a body at most 64 MiB (one chunk); a
frame past either is `PYTHON_FRAME_LIMIT`. Bodies are `columnar-le/v1` chunks.
"""

from __future__ import annotations

import json
import struct
from typing import BinaryIO

PROTOCOL = 1
MAX_HEADER = 1 << 20
MAX_BODY = 64 << 20


class FrameError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Frames:
    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self.reader = reader
        self.writer = writer

    def _exact(self, size: int) -> bytes:
        buffer = bytearray(size)
        view = memoryview(buffer)
        got = 0
        while got < size:
            count = self.reader.readinto(view[got:])  # type: ignore[attr-defined]
            if not count:
                raise FrameError("PYTHON_PIPE_CLOSED")
            got += count
        return bytes(buffer)

    def read(self) -> tuple[dict, bytes]:
        (header_length,) = struct.unpack("<I", self._exact(4))
        if header_length > MAX_HEADER:
            raise FrameError("PYTHON_FRAME_LIMIT")
        header = json.loads(self._exact(header_length))
        if not isinstance(header, dict) or not isinstance(header.get("t"), str):
            raise FrameError("PYTHON_FRAME_INVALID")
        (body_length,) = struct.unpack("<I", self._exact(4))
        if body_length > MAX_BODY:
            raise FrameError("PYTHON_FRAME_LIMIT")
        return header, (self._exact(body_length) if body_length else b"")

    def write(self, header: dict, body: bytes | memoryview = b"") -> None:
        encoded = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_HEADER or len(body) > MAX_BODY:
            raise FrameError("PYTHON_FRAME_LIMIT")
        self.writer.write(struct.pack("<I", len(encoded)))
        self.writer.write(encoded)
        self.writer.write(struct.pack("<I", len(body)))
        if len(body):
            self.writer.write(body)

    def flush(self) -> None:
        self.writer.flush()
