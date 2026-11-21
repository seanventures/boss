"""The seccomp profile for engine inner-sandbox support (docs/community/ENGINE_SANDBOX.md).

It is Docker's own default profile (moby/profiles, Apache-2.0, vendored in vendor/ with its
license) plus a few rules that let a coding engine's command sandbox (bubblewrap) create its
namespaces. Nothing is fetched at install time: the source is in the repository, its SHA-256 is
checked here, and the generated profile is committed as engine-seccomp.json. A test regenerates
it and fails when the committed file differs.

    python3 scripts/sandbox/engine_seccomp.py            # print the profile
    python3 scripts/sandbox/engine_seccomp.py --write    # rewrite engine-seccomp.json
    python3 scripts/sandbox/engine_seccomp.py --check    # exit 1 when the committed file is stale
"""
import hashlib
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
#: moby/profiles at 2ab3e516a3b49550c35b5e4511a0a7d136a02c8d, seccomp/default.json.
SOURCE = HERE / "vendor" / "moby-seccomp-default.json"
SOURCE_COMMIT = "2ab3e516a3b49550c35b5e4511a0a7d136a02c8d"
SHA256 = "3320527d4568265ac97a077609b1bfd4cfd0d789eeb9239b7e094b1526675406"
OUTPUT = HERE / "engine-seccomp.json"


#: CLONE_NEWUSER. A namespace may be created only together with a new user namespace.
CLONE_NEWUSER = 0x10000000


def additions():
    """The rules added to Docker's default profile, and why each is as narrow as it is.

    * `clone` and `unshare` are allowed only when the flags include CLONE_NEWUSER (a masked
      compare on the flags argument). A namespace without a new user namespace (CLONE_NEWNS,
      CLONE_NEWNET... on their own) stays denied at this layer, as in Docker's default.
    * `mount`, `umount2` and `pivot_root` take pointer arguments, which seccomp cannot inspect,
      so they cannot be scoped here. The remaining guard is the kernel's own check: each needs
      CAP_SYS_ADMIN in the user namespace that owns the mount namespace, which a container
      process has only inside a user namespace it created itself. No capability is added.
    * NOT added: `sethostname`/`setdomainname` (Codex 0.158's bubblewrap does not pass
      --unshare-uts or --hostname), `setns`, `clone3` (still ENOSYS; bubblewrap falls back to
      clone), `keyctl`, `bpf`, `ptrace`, any `includes.caps` rule.
    """
    return [
        {"names": ["mount", "umount2", "pivot_root"], "action": "SCMP_ACT_ALLOW",
         "comment": "BOSS engine inner sandbox: the kernel still requires CAP_SYS_ADMIN in the "
                    "owning user namespace"},
        {"names": ["unshare"], "action": "SCMP_ACT_ALLOW", "args": [
            {"index": 0, "value": CLONE_NEWUSER, "valueTwo": CLONE_NEWUSER,
             "op": "SCMP_CMP_MASKED_EQ"}],
         "comment": "BOSS engine inner sandbox: only with CLONE_NEWUSER"},
        {"names": ["clone"], "action": "SCMP_ACT_ALLOW", "args": [
            {"index": 0, "value": CLONE_NEWUSER, "valueTwo": CLONE_NEWUSER,
             "op": "SCMP_CMP_MASKED_EQ"}],
         "comment": "BOSS engine inner sandbox: only with CLONE_NEWUSER",
         "excludes": {"arches": ["s390", "s390x"]}},
    ]


def profile(source):
    if hashlib.sha256(source).hexdigest() != SHA256:
        raise ValueError("Moby seccomp source checksum mismatch")
    policy = json.loads(source)
    policy["syscalls"].extend(additions())
    return policy


def render(source=None):
    data = SOURCE.read_bytes() if source is None else source
    return json.dumps(profile(data), indent=2) + "\n"


def main(argv):
    text = render()
    if "--write" in argv:
        OUTPUT.write_text(text)
        return 0
    if "--check" in argv:
        if not OUTPUT.exists() or OUTPUT.read_text() != text:
            print(f"{OUTPUT.name} is stale: run python3 scripts/sandbox/engine_seccomp.py --write",
                  file=sys.stderr)
            return 1
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
