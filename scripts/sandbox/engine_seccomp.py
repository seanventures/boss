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


def additions():
    """The rules added to Docker's default profile."""
    return [
        {"names": ["mount", "umount2", "pivot_root", "sethostname", "setdomainname"],
         "action": "SCMP_ACT_ALLOW"},
        {"names": ["unshare"], "action": "SCMP_ACT_ALLOW", "args": [
            {"index": 0, "value": 0xffffffff ^ 0x7e020000, "valueTwo": 0,
             "op": "SCMP_CMP_MASKED_EQ"}]},
        {"names": ["clone"], "action": "SCMP_ACT_ALLOW", "args": [
            {"index": 0, "value": 0x10000000, "valueTwo": 0x10000000,
             "op": "SCMP_CMP_MASKED_EQ"}]},
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
