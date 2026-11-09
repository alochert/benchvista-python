"""What the trusted runner applies to its own process before any step code runs (§17.1).

Loaded by path from the host's bootstrap, before the `benchvista` package (and numpy) is
imported, so it imports nothing of the package. Every limit is set with its hard limit equal to
its soft one, so nothing later in the process can raise it again, and the kernel enforces it,
except the CPU limit's hard one, a few seconds above the soft: at the soft limit the kernel sends
SIGXCPU, which ends the process. Linux kills a process that ignored it at the hard limit; macOS
does not reliably, so the host samples the process's CPU time and stops it at its budget itself,
and only what the host saw makes an end a timeout (`process.ts`).

- `RLIMIT_CPU` (seconds of CPU), `RLIMIT_FSIZE` (the largest file), `RLIMIT_NOFILE`, no core files;
- `RLIMIT_AS` on Linux; macOS does not enforce address-space limits, so there the host bounds
  memory by sampling the process (phase 0 findings);
- on Linux, a seccomp filter that refuses new processes and `exec` (`deny_new_processes`), and on
  x86_64 every x32 system call (numbers with bit 30 set), which would otherwise name the same
  calls by other numbers.

The operating system sandbox around the process (sandbox-exec, bubblewrap) is the boundary;
this narrows what the process can do inside it.
"""

from __future__ import annotations

import ctypes
import os
import platform
import resource
import struct
import sys

PR_SET_NO_NEW_PRIVS = 38
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_TSYNC = 1
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM, ENOSYS = 1, 38
CLONE_THREAD = 0x00010000
X32_SYSCALL_BIT = 0x40000000
CPU_HARD_MARGIN_SECONDS = 5
ARCHES = {
    "aarch64": {"audit": 0xC00000B7, "seccomp": 277, "clone": 220, "clone3": 435, "denied": [221, 281], "x32": False},
    "x86_64": {"audit": 0xC000003E, "seccomp": 317, "clone": 56, "clone3": 435, "denied": [57, 58, 59, 322], "x32": True},
}


def _limit(which: int, value: int) -> None:
    resource.setrlimit(which, (value, value))


def apply_limits(cpu_seconds: int, file_bytes: int, open_files: int, memory_bytes: int | None) -> None:
    _limit(resource.RLIMIT_CORE, 0)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + CPU_HARD_MARGIN_SECONDS))
    _limit(resource.RLIMIT_FSIZE, file_bytes)
    _limit(resource.RLIMIT_NOFILE, open_files)
    if memory_bytes is not None and sys.platform.startswith("linux"):
        _limit(resource.RLIMIT_AS, memory_bytes)


def _program(arch: dict) -> list[tuple[int, int, int, int]]:
    ld, jeq, jset, ret = 0x20, 0x15, 0x45, 0x06
    body: list[tuple] = [
        ("ld", 4),
        ("jeq", arch["audit"], None, "kill"),
        ("ld", 0),
        *([("jset", X32_SYSCALL_BIT, "kill", None)] if arch["x32"] else []),
        ("jeq", arch["clone3"], "enosys", None),
        ("jeq", arch["clone"], "clone", None),
        *[("jeq", nr, "eperm", None) for nr in arch["denied"]],
        ("ret", SECCOMP_RET_ALLOW),
        ("label", "clone"),
        ("ld", 16),
        ("jset", CLONE_THREAD, None, "eperm"),
        ("ret", SECCOMP_RET_ALLOW),
        ("label", "eperm"),
        ("ret", SECCOMP_RET_ERRNO | EPERM),
        ("label", "enosys"),
        ("ret", SECCOMP_RET_ERRNO | ENOSYS),
        ("label", "kill"),
        ("ret", SECCOMP_RET_KILL_PROCESS),
    ]
    labels: dict[str, int] = {}
    index = 0
    for entry in body:
        if entry[0] == "label":
            labels[entry[1]] = index
        else:
            index += 1
    out: list[tuple[int, int, int, int]] = []
    for entry in body:
        if entry[0] == "label":
            continue
        at = len(out)

        def jump(target: str | None) -> int:
            if target is None:
                return 0
            distance = labels[target] - at - 1
            if not 0 <= distance <= 255:
                raise ValueError("PYTHON_SECCOMP")
            return distance

        if entry[0] == "ld":
            out.append((ld, 0, 0, entry[1]))
        elif entry[0] == "ret":
            out.append((ret, 0, 0, entry[1]))
        else:
            out.append((jeq if entry[0] == "jeq" else jset, jump(entry[2]), jump(entry[3]), entry[1]))
    return out


def deny_new_processes() -> None:
    """Linux: refuse fork, vfork, posix_spawn and exec for every thread, for good."""
    arch = ARCHES.get(platform.machine())
    if arch is None:
        raise OSError("PYTHON_SECCOMP_ARCH")
    instructions = _program(arch)
    raw = b"".join(struct.pack("<HBBI", *instruction) for instruction in instructions)
    filters = ctypes.create_string_buffer(raw, len(raw))

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    program = SockFprog(len(instructions), ctypes.cast(filters, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
    if libc.syscall(arch["seccomp"], SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC, ctypes.byref(program)) != 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
