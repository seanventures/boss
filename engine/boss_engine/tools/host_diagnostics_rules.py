"""Read-only host inspection & diagnostic command rules.

Pure Python, zero-DB, zero-side-effect specification shared by BOSS's
``inspect_host`` / ``collect_diagnostics`` tools (``MT-1``) and exported via
:func:`policy_rule_table` so Rust ``metatron-policy`` (``MT-7``/``MT-9``) can
reuse the exact verb allowlist, flag rules, and numeric caps without diverging.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import shlex
from typing import Any

SHELL_META: frozenset[str] = frozenset("&;|<>`$\\")

MAX_INTERVAL_SECONDS = 10
MAX_SAMPLE_COUNT = 10
MAX_SAMPLE_DURATION_SECONDS = 30

TOP_MAX_COUNT = 5
TOP_MAX_DELAY_SECONDS = 5
TOP_MIN_WIDTH = 40
TOP_MAX_WIDTH = 512
TOP_SORT_FIELDS: frozenset[str] = frozenset({
    "%CPU", "%MEM", "RES", "VIRT", "SHR", "TIME+", "PID",
})

SAR_NETWORK_KEYWORDS: frozenset[str] = frozenset({
    "DEV", "EDEV", "SOCK", "TCP", "UDP",
})
VMSTAT_UNIT_KEYWORDS: frozenset[str] = frozenset({"k", "K", "m", "M"})
MPSTAT_CPU_RE = re.compile(r"^(?:ALL|[0-9]+(?:[-,][0-9]+)*)$")

SYSSTAT_VERBS: frozenset[str] = frozenset({"sar", "iostat", "mpstat"})
SYSSTAT_PACKAGE_HINT = (
    "Install 'sysstat' on the target host (for example: "
    "'sudo apt-get install -y sysstat' or 'sudo dnf install -y sysstat'). "
    "BOSS does not install host packages automatically."
)

JOURNALCTL_FLAGS: frozenset[str] = frozenset({
    "-b", "--boot", "-k", "--dmesg", "-q", "--quiet", "-r", "--reverse",
    "-x", "--catalog", "--no-pager", "--utc",
})
JOURNALCTL_VALUE_FLAGS: frozenset[str] = frozenset({
    "-u", "--unit", "-n", "--lines", "-p", "--priority", "-o", "--output",
    "--since", "--until",
})

INSPECT_HOST_VERBS: tuple[str, ...] = (
    "docker ps", "docker logs", "docker inspect", "docker stats --no-stream",
    "docker images", "df", "free", "uptime", "uname", "ls", "cat /etc/os-release",
    "systemctl status", "journalctl", "ss -", "netstat", "ps aux", "whoami",
    "sar", "vmstat", "iostat", "mpstat", "top -b",
)


@dataclass(frozen=True)
class NumericTailSpec:
    allow_zero: bool
    require_pair: bool
    max_interval: int
    max_count: int
    max_duration: int


@dataclass(frozen=True)
class VerbRule:
    verb: str
    category: str
    requires_package: str | None
    required_flags: tuple[str, ...]
    boolean_flags: frozenset[str]
    value_flags: dict[str, str]
    forbidden_flags: frozenset[str]
    numeric_tail: NumericTailSpec | None
    allow_clustered_boolean_flags: bool = False


_DIAGNOSTIC_TAIL = NumericTailSpec(
    allow_zero=True,
    require_pair=True,
    max_interval=MAX_INTERVAL_SECONDS,
    max_count=MAX_SAMPLE_COUNT,
    max_duration=MAX_SAMPLE_DURATION_SECONDS,
)

DIAGNOSTIC_VERB_RULES: dict[str, VerbRule] = {
    "sar": VerbRule(
        verb="sar",
        category="diagnostics",
        requires_package="sysstat",
        required_flags=(),
        boolean_flags=frozenset({"-u", "-r", "-q", "-b", "-d", "-h", "-p"}),
        value_flags={"-n": "enum:DEV,EDEV,SOCK,TCP,UDP"},
        forbidden_flags=frozenset({"-o", "-f"}),
        numeric_tail=_DIAGNOSTIC_TAIL,
        allow_clustered_boolean_flags=True,
    ),
    "vmstat": VerbRule(
        verb="vmstat",
        category="diagnostics",
        requires_package="sysstat",
        required_flags=(),
        boolean_flags=frozenset({"-a", "-d", "-D", "-s", "-t", "-w"}),
        value_flags={"-S": "enum:k,K,m,M"},
        forbidden_flags=frozenset({"-p", "-m"}),
        numeric_tail=_DIAGNOSTIC_TAIL,
        allow_clustered_boolean_flags=True,
    ),
    "iostat": VerbRule(
        verb="iostat",
        category="diagnostics",
        requires_package="sysstat",
        required_flags=(),
        boolean_flags=frozenset({
            "-c", "-d", "-h", "-k", "-m", "-t", "-x", "-y", "-z",
        }),
        value_flags={},
        forbidden_flags=frozenset({"-o"}),
        numeric_tail=_DIAGNOSTIC_TAIL,
        allow_clustered_boolean_flags=True,
    ),
    "mpstat": VerbRule(
        verb="mpstat",
        category="diagnostics",
        requires_package="sysstat",
        required_flags=(),
        boolean_flags=frozenset({"-A", "-u"}),
        value_flags={"-P": "regex:^(?:ALL|[0-9]+(?:[-,][0-9]+)*)$"},
        forbidden_flags=frozenset(),
        numeric_tail=_DIAGNOSTIC_TAIL,
        allow_clustered_boolean_flags=True,
    ),
    "top": VerbRule(
        verb="top",
        category="diagnostics",
        requires_package=None,
        required_flags=("-b", "-n"),
        boolean_flags=frozenset({"-b", "-H", "-c", "-i", "-1"}),
        value_flags={
            "-n": f"int:1..{TOP_MAX_COUNT}",
            "-d": f"int:1..{TOP_MAX_DELAY_SECONDS}",
            "-w": f"int:{TOP_MIN_WIDTH}..{TOP_MAX_WIDTH}",
            "-o": "enum:%CPU,%MEM,RES,VIRT,SHR,TIME+,PID",
        },
        forbidden_flags=frozenset({"-u", "-U"}),
        numeric_tail=None,
        allow_clustered_boolean_flags=False,
    ),
}


def _parse_positive_int(token: str) -> int | None:
    if not re.fullmatch(r"[0-9]+", token):
        return None
    val = int(token)
    return val if val >= 1 else None


def _expand_boolean_token(
    token: str,
    rule: VerbRule,
) -> list[str] | None:
    """Return expanded single-char flags if token is valid boolean flags, else None."""
    if token in rule.boolean_flags:
        return [token]
    if (
        rule.allow_clustered_boolean_flags
        and len(token) > 2
        and token.startswith("-")
        and not token.startswith("--")
    ):
        expanded = [f"-{ch}" for ch in token[1:]]
        if any(flag in rule.forbidden_flags for flag in expanded):
            return None
        if any(flag in rule.value_flags for flag in expanded):
            return None
        if all(flag in rule.boolean_flags for flag in expanded):
            return expanded
    return None


def _validate_numeric_tail(
    verb: str,
    positionals: list[str],
    tail: NumericTailSpec,
) -> str | None:
    if not positionals:
        return None if tail.allow_zero else f"{verb} requires interval and count arguments"
    if len(positionals) == 1:
        if tail.require_pair:
            return (
                f"{verb} with a single interval streams indefinitely; provide both "
                f"interval (1..{tail.max_interval}) and count (1..{tail.max_count})"
            )
    if len(positionals) > 2:
        return f"{verb} accepts at most interval and count arguments"

    interval = _parse_positive_int(positionals[0])
    count = _parse_positive_int(positionals[1])
    if interval is None or count is None:
        return (
            f"{verb} interval and count must be positive integers "
            f"(1..{tail.max_interval} and 1..{tail.max_count})"
        )
    if interval > tail.max_interval:
        return f"{verb} interval {interval} exceeds maximum {tail.max_interval}s"
    if count > tail.max_count:
        return f"{verb} count {count} exceeds maximum {tail.max_count}"
    if interval * count > tail.max_duration:
        return (
            f"{verb} total sample duration {interval * count}s exceeds "
            f"maximum {tail.max_duration}s"
        )
    return None


def _validate_diagnostic_verb(words: list[str]) -> str | None:
    """Validate one of the sysstat/top diagnostic verbs. Returns error string or None."""
    verb = words[0]
    rule = DIAGNOSTIC_VERB_RULES[verb]
    seen_flags: set[str] = set()
    value_map: dict[str, str] = {}
    positionals: list[str] = []

    index = 1
    while index < len(words):
        token = words[index]

        if positionals:
            # Once numeric tail begins, flags or extra non-numeric tokens are rejected.
            if token.startswith("-"):
                return f"{verb} flags must precede interval and count arguments"
            positionals.append(token)
            index += 1
            continue

        if token in rule.forbidden_flags or any(
            token.startswith(f) and len(token) > len(f) for f in rule.forbidden_flags
        ):
            return f"{verb} option {token!r} is not permitted in read-only inspection"

        # Support attached numeric short options on `top`, e.g. `-n1`, `-d2`, `-w160`.
        attached_flag: str | None = None
        attached_val: str | None = None
        if verb == "top" and len(token) > 2 and token[:2] in rule.value_flags:
            attached_flag = token[:2]
            attached_val = token[2:]

        if attached_flag is not None or token in rule.value_flags:
            flag = attached_flag if attached_flag is not None else token
            if attached_val is not None:
                val = attached_val
                index += 1
            else:
                if index + 1 >= len(words):
                    return f"{verb} {flag} requires a value"
                val = words[index + 1]
                index += 2
            err = _validate_value_flag(verb, flag, val)
            if err:
                return err
            seen_flags.add(flag)
            value_map[flag] = val
            continue

        expanded = _expand_boolean_token(token, rule)
        if expanded is not None:
            seen_flags.update(expanded)
            index += 1
            # `sar -u ALL [interval count]`
            if (
                verb == "sar"
                and "-u" in expanded
                and index < len(words)
                and words[index] == "ALL"
            ):
                index += 1
            continue

        if token.startswith("-"):
            return f"{verb} option {token!r} is not permitted in read-only inspection"

        if rule.numeric_tail is None:
            return f"{verb} unexpected argument {token!r}"

        positionals.append(token)
        index += 1

    for req in rule.required_flags:
        if req not in seen_flags:
            return f"{verb} requires {req} in read-only batch mode"

    if verb == "top":
        n_val = int(value_map["-n"])
        d_val = int(value_map["-d"]) if "-d" in value_map else 1
        if n_val * d_val > MAX_SAMPLE_DURATION_SECONDS:
            return (
                f"top total duration {n_val * d_val}s exceeds maximum "
                f"{MAX_SAMPLE_DURATION_SECONDS}s"
            )

    if rule.numeric_tail is not None:
        return _validate_numeric_tail(verb, positionals, rule.numeric_tail)

    return None


def _validate_value_flag(verb: str, flag: str, val: str) -> str | None:
    if not val or (val.startswith("-") and not (verb == "top" and flag == "-o")):
        return f"{verb} {flag} requires a valid value"
    if verb == "sar" and flag == "-n":
        if val not in SAR_NETWORK_KEYWORDS:
            allowed = ", ".join(sorted(SAR_NETWORK_KEYWORDS))
            return f"sar -n keyword must be one of: {allowed}"
        return None
    if verb == "vmstat" and flag == "-S":
        if val not in VMSTAT_UNIT_KEYWORDS:
            return "vmstat -S unit must be one of: k, K, m, M"
        return None
    if verb == "mpstat" and flag == "-P":
        if not MPSTAT_CPU_RE.fullmatch(val):
            return "mpstat -P must be ALL or numeric CPU list"
        return None
    if verb == "top":
        if flag == "-n":
            parsed = _parse_positive_int(val)
            if parsed is None or parsed > TOP_MAX_COUNT:
                return f"top -n must be an integer between 1 and {TOP_MAX_COUNT}"
            return None
        if flag == "-d":
            parsed = _parse_positive_int(val)
            if parsed is None or parsed > TOP_MAX_DELAY_SECONDS:
                return f"top -d must be an integer between 1 and {TOP_MAX_DELAY_SECONDS}"
            return None
        if flag == "-w":
            parsed = _parse_positive_int(val)
            if parsed is None or parsed < TOP_MIN_WIDTH or parsed > TOP_MAX_WIDTH:
                return f"top -w must be an integer between {TOP_MIN_WIDTH} and {TOP_MAX_WIDTH}"
            return None
        if flag == "-o":
            if val not in TOP_SORT_FIELDS:
                allowed = ", ".join(sorted(TOP_SORT_FIELDS))
                return f"top -o sort field must be one of: {allowed}"
            return None
    return f"{verb} option {flag!r} is not supported"


def validate_inspect_host_command(raw_value: object) -> tuple[str | None, str | None]:
    """Return one shell-safe read command, or a reason before any credential/SSH access.

    SSH transports a *single string* interpreted by a remote shell. Reject control chars and
    shell metacharacters before tokenizing, verify the complete verb and its read-only options,
    then quote every token for that shell.
    """
    if not isinstance(raw_value, str):
        return None, "a read-only command is required"
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in SHELL_META for ch in raw_value):
        return None, "shell metacharacters and control characters are not permitted"
    try:
        words = shlex.split(raw_value.strip(), posix=True)
    except ValueError:
        return None, "unbalanced command quoting is not permitted"
    if not words:
        return None, "a read-only command is required"

    verb = words[0]
    if verb in DIAGNOSTIC_VERB_RULES:
        reason = _validate_diagnostic_verb(words)
        if reason:
            return None, reason
        return shlex.join(words), None

    if verb == "docker":
        allowed = len(words) >= 2 and words[1] in {"ps", "logs", "inspect", "images"}
        allowed = allowed or (len(words) >= 3 and words[1:3] == ["stats", "--no-stream"])
    elif verb == "cat":
        allowed = words == ["cat", "/etc/os-release"]
    elif verb == "systemctl":
        allowed = (
            len(words) >= 2
            and words[1] == "status"
            and all(re.fullmatch(r"[A-Za-z0-9_.@:-]+", unit) for unit in words[2:])
        )
    elif verb == "journalctl":
        allowed = True
        index = 1
        while index < len(words):
            option = words[index]
            if option in JOURNALCTL_FLAGS:
                index += 1
            elif option in JOURNALCTL_VALUE_FLAGS and index + 1 < len(words):
                value = words[index + 1]
                if not value or value.startswith("-"):
                    allowed = False
                    break
                index += 2
            elif any(
                option.startswith(flag + "=") and option[len(flag) + 1:]
                for flag in JOURNALCTL_VALUE_FLAGS
                if flag.startswith("--")
            ):
                index += 1
            else:
                allowed = False
                break
    elif verb == "ss":
        # `ss -K`/`ss --kill` terminates sockets; only listing switches belong here.
        allowed = len(words) >= 2 and all(
            re.fullmatch(r"-[tulnpaHs46]+", option)
            or option in {
                "--tcp", "--udp", "--listening", "--numeric",
                "--processes", "--all", "--summary", "--no-header",
            }
            for option in words[1:]
        )
    elif verb == "ps":
        allowed = words == ["ps", "aux"]
    elif verb in {"df", "free", "uptime", "uname", "ls", "netstat", "whoami"}:
        allowed = True
    else:
        allowed = False
    if not allowed:
        return None, "only read-only commands are allowed here: " + ", ".join(
            INSPECT_HOST_VERBS
        )
    return shlex.join(words), None


def detect_missing_sysstat(
    command: str,
    exit_code: int | None,
    stderr: str,
) -> dict[str, str] | None:
    """Distinguish a missing remote sysstat binary from a missing local ssh client."""
    lowered = (stderr or "").lower()
    if "ssh` client is not installed" in lowered or "ssh client is not installed" in lowered:
        return None
    try:
        words = shlex.split((command or "").strip(), posix=True)
    except ValueError:
        words = (command or "").strip().split()
    if not words:
        return None
    verb = words[0]
    if verb not in SYSSTAT_VERBS:
        return None
    if (
        exit_code == 127
        or "command not found" in lowered
        or ": not found" in lowered
        or lowered.strip().endswith("not found")
    ):
        return {
            "package": "sysstat",
            "verb": verb,
            "install_hint": SYSSTAT_PACKAGE_HINT,
        }
    return None


def policy_rule_table() -> dict[str, Any]:
    """Return a JSON-serializable rule table for Rust ``metatron-policy`` (MT-7/MT-9)."""
    verbs: dict[str, Any] = {}
    for name, rule in DIAGNOSTIC_VERB_RULES.items():
        entry = asdict(rule)
        entry["required_flags"] = list(rule.required_flags)
        entry["boolean_flags"] = sorted(rule.boolean_flags)
        entry["forbidden_flags"] = sorted(rule.forbidden_flags)
        verbs[name] = entry

    verbs["ss"] = {
        "verb": "ss",
        "category": "network",
        "requires_package": None,
        "required_flags": [],
        "boolean_flags": [
            "-t", "-u", "-l", "-n", "-p", "-a", "-H", "-s", "-4", "-6",
            "--tcp", "--udp", "--listening", "--numeric",
            "--processes", "--all", "--summary", "--no-header",
        ],
        "value_flags": {},
        "forbidden_flags": ["-K", "--kill"],
        "numeric_tail": None,
        "allow_clustered_boolean_flags": True,
    }
    verbs["journalctl"] = {
        "verb": "journalctl",
        "category": "logs",
        "requires_package": None,
        "required_flags": [],
        "boolean_flags": sorted(JOURNALCTL_FLAGS),
        "value_flags": {flag: "string" for flag in sorted(JOURNALCTL_VALUE_FLAGS)},
        "forbidden_flags": ["--vacuum-size", "--vacuum-time", "--rotate"],
        "numeric_tail": None,
        "allow_clustered_boolean_flags": False,
    }
    verbs["docker"] = {
        "verb": "docker",
        "category": "containers",
        "requires_package": None,
        "allowed_subcommands": ["ps", "logs", "inspect", "images", "stats --no-stream"],
        "forbidden_flags": [],
        "numeric_tail": None,
    }
    verbs["systemctl"] = {
        "verb": "systemctl",
        "category": "services",
        "requires_package": None,
        "allowed_subcommands": ["status"],
        "unit_regex": r"[A-Za-z0-9_.@:-]+",
        "forbidden_flags": [],
        "numeric_tail": None,
    }
    verbs["cat"] = {
        "verb": "cat",
        "category": "system",
        "requires_package": None,
        "exact_commands": [["cat", "/etc/os-release"]],
        "forbidden_flags": [],
        "numeric_tail": None,
    }
    verbs["ps"] = {
        "verb": "ps",
        "category": "processes",
        "requires_package": None,
        "exact_commands": [["ps", "aux"]],
        "forbidden_flags": [],
        "numeric_tail": None,
    }
    for basic in ("df", "free", "uptime", "uname", "ls", "netstat", "whoami"):
        verbs[basic] = {
            "verb": basic,
            "category": "system",
            "requires_package": None,
            "required_flags": [],
            "boolean_flags": [],
            "value_flags": {},
            "forbidden_flags": [],
            "numeric_tail": None,
        }

    return {
        "version": 1,
        "shell_meta": sorted(SHELL_META),
        "allowlist_prefixes": list(INSPECT_HOST_VERBS),
        "caps": {
            "max_interval_seconds": MAX_INTERVAL_SECONDS,
            "max_sample_count": MAX_SAMPLE_COUNT,
            "max_sample_duration_seconds": MAX_SAMPLE_DURATION_SECONDS,
            "top_max_count": TOP_MAX_COUNT,
            "top_max_delay_seconds": TOP_MAX_DELAY_SECONDS,
            "top_min_width": TOP_MIN_WIDTH,
            "top_max_width": TOP_MAX_WIDTH,
            "top_sort_fields": sorted(TOP_SORT_FIELDS),
            "sar_network_keywords": sorted(SAR_NETWORK_KEYWORDS),
        },
        "sysstat": {
            "verbs": sorted(SYSSTAT_VERBS),
            "package": "sysstat",
            "install_hint": SYSSTAT_PACKAGE_HINT,
        },
        "verbs": verbs,
    }
