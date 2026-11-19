import hashlib
import json
import sys
import urllib.request


SOURCE = "https://raw.githubusercontent.com/moby/profiles/2ab3e516a3b49550c35b5e4511a0a7d136a02c8d/seccomp/default.json"
SHA256 = "3320527d4568265ac97a077609b1bfd4cfd0d789eeb9239b7e094b1526675406"


def profile(source):
    if hashlib.sha256(source).hexdigest() != SHA256:
        raise ValueError("Moby seccomp source checksum mismatch")
    policy = json.loads(source)
    policy["syscalls"].extend([
        {"names": ["mount", "umount2", "pivot_root", "sethostname", "setdomainname"],
         "action": "SCMP_ACT_ALLOW"},
        {"names": ["unshare"], "action": "SCMP_ACT_ALLOW", "args": [
            {"index": 0, "value": 0xffffffff ^ 0x7e020000, "valueTwo": 0,
             "op": "SCMP_CMP_MASKED_EQ"}]},
        {"names": ["clone"], "action": "SCMP_ACT_ALLOW", "args": [
            {"index": 0, "value": 0x10000000, "valueTwo": 0x10000000,
             "op": "SCMP_CMP_MASKED_EQ"}]},
    ])
    return policy


def main():
    with urllib.request.urlopen(SOURCE, timeout=30) as response:
        policy = profile(response.read())
    json.dump(policy, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
