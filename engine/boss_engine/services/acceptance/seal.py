"""THE SEAL (N1, PR #154 review): no unix sockets for the acceptance sandbox.

The sealed uid's network fence is iptables, and iptables never sees AF_UNIX: another uid's
socket in /tmp (mode 777) or an ABSTRACT socket (no file at all) was reachable from the session.
Every sealed command therefore starts under a seccomp filter, installed by `SCRIPT` (run as the
sealed uid by boss-sandbox-exec, then exec'ing the command; children inherit it):

  socket(AF_UNIX, ...)  -> EPERM   (filesystem and abstract sockets alike: nothing to connect)
  io_uring_setup        -> EPERM   (io_uring would issue socket/connect past seccomp)
  x32 syscalls (x86_64) -> EPERM
  an unknown architecture -> the process is killed (fail closed)

socketpair() stays allowed (an anonymous pair reaches nobody), and TCP is iptables' (loopback,
own listeners only). Raw BPF through ctypes `prctl` — no libseccomp binding needed. If the
filter cannot be installed the command never runs (exit 126).
"""
from __future__ import annotations

#: Exit code of a command whose seal could not be installed: it did not run.
SEAL_FAILED = 126

SCRIPT = r'''
import ctypes, os, struct, sys
LD, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06
ALLOW, KILL, EPERM = 0x7FFF0000, 0x80000000, 0x00050001
ARCHES = {0xC000003E: (41, 425, True), 0xC00000B7: (198, 425, False)}
prog = []
def ins(c, k=0, jt=0, jf=0):
    prog.append([c, jt, jf, k]); return len(prog) - 1
ins(LD, 4)
heads = {a: ins(JEQ, a) for a in ARCHES}
ins(RET, KILL)
for arch, (sock, uring, x86) in ARCHES.items():
    prog[heads[arch]][1] = len(prog) - heads[arch] - 1
    ins(LD, 0)
    if x86:
        ins(JGE, 0x40000000, 0, 1); ins(RET, EPERM)
    ins(JEQ, uring, 0, 1); ins(RET, EPERM)
    ins(JEQ, sock, 0, 3); ins(LD, 16); ins(JEQ, 1, 0, 1); ins(RET, EPERM)
    ins(RET, ALLOW)
buf = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *p) for p in prog))
class F(ctypes.Structure):
    _fields_ = [("n", ctypes.c_ushort), ("f", ctypes.c_void_p)]
fp = F(len(prog), ctypes.addressof(buf))
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(fp), 0, 0) != 0:
    sys.stderr.write("boss: the acceptance seal could not be installed; the command did not run\n")
    os._exit(126)
argv = sys.argv[sys.argv.index("--") + 1:]
try:
    os.execvp(argv[0], argv)
except OSError as e:
    sys.stderr.write(f"{argv[0]}: {e.strerror}\n"); os._exit(127)
'''


def wrap(argv: list[str]) -> list[str]:
    """The argv that runs `argv` under the seal (isolated python: no site, no env imports)."""
    return ["python3", "-I", "-S", "-c", SCRIPT, "--", *argv]
