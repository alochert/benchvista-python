"""The Linux seccomp filter the trusted runner installs, read instruction by instruction.

`benchvista.sandbox._program` builds the classic BPF program; a small interpreter here runs it on
the syscalls a step might try, so each verdict is checked without a Linux kernel. The kernel's
own behaviour under it is python-sandbox.test.ts's, on SV1.
"""

from benchvista.sandbox import (
    ARCHES,
    CLONE_THREAD,
    ENOSYS,
    EPERM,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    SECCOMP_RET_KILL_PROCESS,
    X32_SYSCALL_BIT,
    _program,
)

LD, JEQ, JSET, RET = 0x20, 0x15, 0x45, 0x06


def verdict(arch_name: str, nr: int, flags: int = 0, audit: int | None = None) -> int:
    """Runs the program on `seccomp_data`: nr at offset 0, arch at 4, the first argument at 16."""
    arch = ARCHES[arch_name]
    data = {0: nr, 4: arch["audit"] if audit is None else audit, 16: flags}
    program = _program(arch)
    accumulator = 0
    pc = 0
    while True:
        code, true_jump, false_jump, k = program[pc]
        if code == LD:
            accumulator = data[k]
            pc += 1
        elif code == JEQ:
            pc += 1 + (true_jump if accumulator == k else false_jump)
        elif code == JSET:
            pc += 1 + (true_jump if accumulator & k else false_jump)
        elif code == RET:
            return k
        else:
            raise AssertionError(f"unexpected instruction {code:#x}")


def test_new_processes_and_exec_are_refused_and_threads_are_not():
    for name in ("aarch64", "x86_64"):
        arch = ARCHES[name]
        assert verdict(name, arch["clone"], CLONE_THREAD | 0x100) == SECCOMP_RET_ALLOW
        assert verdict(name, arch["clone"], 0x11) == SECCOMP_RET_ERRNO | EPERM
        assert verdict(name, arch["clone3"]) == SECCOMP_RET_ERRNO | ENOSYS
        for denied in arch["denied"]:
            assert verdict(name, denied) == SECCOMP_RET_ERRNO | EPERM
        # An ordinary call (read is 63 on aarch64, 0 on x86_64) passes.
        assert verdict(name, 63 if name == "aarch64" else 0) == SECCOMP_RET_ALLOW


def test_another_architecture_is_killed():
    assert verdict("x86_64", 0, audit=0x40000003) == SECCOMP_RET_KILL_PROCESS


def test_x32_system_calls_are_killed_on_x86_64():
    # execve through the x32 table would otherwise get past the x86_64 numbers.
    assert verdict("x86_64", X32_SYSCALL_BIT | 59) == SECCOMP_RET_KILL_PROCESS
    assert verdict("x86_64", X32_SYSCALL_BIT | 0) == SECCOMP_RET_KILL_PROCESS
    # aarch64 has no such table: the bit is just an unknown, harmless number there.
    assert verdict("aarch64", X32_SYSCALL_BIT | 63) == SECCOMP_RET_ALLOW
