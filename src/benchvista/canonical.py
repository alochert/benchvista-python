"""Canonical JSON and SHA-256 identities, byte for byte as BenchVista's TypeScript writes them.

`src/lib/canonical.ts` sorts object keys (in UTF-16 code unit order, as JavaScript compares
strings), writes `undefined` as `null`, and writes numbers and strings as `JSON.stringify` does.
Manifests, dataset ids and message revisions are SHA-256 of this text, so Python must produce
exactly the same bytes: numbers follow ECMAScript's Number::toString (`1e-6` is `0.000001`,
`1e21` is `1e+21`, `-0` is `0`), and text escapes only what `JSON.stringify` escapes.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


class CanonicalError(ValueError):
    """A value canonical JSON cannot carry exactly (NaN, infinity, an unsafe integer)."""


def js_number(value: float | int) -> str:
    """A number as ECMAScript's Number::toString writes it."""
    if isinstance(value, bool):
        raise CanonicalError("CANONICAL_BOOL_AS_NUMBER")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalError("CANONICAL_UNSAFE_INTEGER")
        return str(value)
    if math.isnan(value) or math.isinf(value):
        raise CanonicalError("CANONICAL_NOT_FINITE")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr gives the shortest digits that round-trip, as ECMAScript requires.
    digits_tuple = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in digits_tuple.digits)
    exponent = int(digits_tuple.exponent)
    k = len(digits)
    n = k + exponent  # the decimal point sits after n digits
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mark = "+" if e >= 0 else "-"
        text = (digits if k == 1 else digits[0] + "." + digits[1:]) + "e" + mark + str(abs(e))
    return sign + text


def _string(value: str) -> str:
    for char in value:
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise CanonicalError("CANONICAL_LONE_SURROGATE")
    # json.dumps escapes exactly what JSON.stringify does when ASCII is not forced.
    return json.dumps(value, ensure_ascii=False)


def _key(key: str) -> bytes:
    return key.encode("utf-16-be")


def canonical(value: Any) -> str:
    """Canonical JSON text of plain data."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return js_number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalError("CANONICAL_KEY")
        items = sorted(value.items(), key=lambda item: _key(item[0]))
        return "{" + ",".join(_string(key) + ":" + canonical(item) for key, item in items) + "}"
    raise CanonicalError(f"CANONICAL_TYPE {type(value).__name__}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hex(value: Any) -> str:
    """Bare hexadecimal SHA-256 of a value's canonical JSON (a manifest's identity)."""
    return sha256_hex(canonical(value).encode("utf-8"))


def hash_id(value: Any) -> str:
    """`sha256:` and the digest of a value's canonical JSON (a dataset's or recipe's id)."""
    return "sha256:" + canonical_hex(value)
