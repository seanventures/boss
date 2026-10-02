"""Remove the operator's private host from a Community export candidate.

The private API keeps its routes. This module only rewrites bytes that are
about to be published. A mention it cannot remove without leaving a broken
file fails the export.
"""

from __future__ import annotations

import json
import re

TOKEN = re.compile(r"goldeneye", re.IGNORECASE)
ROUTE = re.compile(r"/v1/goldeneye", re.IGNORECASE)


class PrivateHostLeak(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# Published manuals name these counts without naming the private host.
# The private files stay on the larger contract; only the export changes.
DOCUMENTED_OPERATION_COUNTS = {
    "docs/community/TERMINAL_CAPABILITY_MATRIX.md": (
        ("**994**", "**983**"),
        ("**981**", "**970**"),
        ("all 994", "all 983"),
    ),
    "docs/community/MCP.md": (
        ("**994 Community**", "**983 Community**"),
        ("| Community | 860 | 64 | 2 | 68 |", "| Community | 851 | 64 | 0 | 68 |"),
    ),
    "docs/community/CONTROL_SURFACES.md": (
        ("all 994", "all 983"),
        ("981 `/v1/`", "970 `/v1/`"),
        ("860 are eligible", "851 are eligible"),
        ("2 remain held", "none remain held"),
    ),
    "docs/landing/index.html": (
        ('<b data-count="api">994</b>', '<b data-count="api">983</b>'),
    ),
    "docs/community/FEATURES.md": (
        ("All 994 mounted", "All 983 mounted"),
        (
            "860 use API policy, 64 need separate exact-request human confirmation, 2 remain held for route safety work",
            "851 use API policy, 64 need separate exact-request human confirmation, none remain held",
        ),
    ),
}


def _apply_documented_counts(path: str, text: str) -> str:
    for old, new in DOCUMENTED_OPERATION_COUNTS.get(path, ()):
        text = text.replace(old, new)
    return text


def redact_private_host(path: str, data: bytes) -> bytes:
    if not TOKEN.search(data.decode("latin1")):
        if path not in DOCUMENTED_OPERATION_COUNTS:
            return data
        text = data.decode("utf-8")
        updated = _apply_documented_counts(path, text)
        if updated == text:
            return data
        return updated.encode("utf-8")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrivateHostLeak(f"{path}: private host name is in non-text bytes") from exc
    text = _structural(path, text)
    text, bound = _replace_prose(text)
    if bound or TOKEN.search(text):
        sample = _sample(text)
        raise PrivateHostLeak(f"{path}: private host name remains ({sample})")
    if path.endswith(".py"):
        try:
            compile(text, path, "exec")
        except SyntaxError as exc:
            raise PrivateHostLeak(f"{path}: redaction broke Python at line {exc.lineno}") from exc
    encoded = text.encode("utf-8")
    if TOKEN.search(encoded.decode("latin1")):
        raise PrivateHostLeak(f"{path}: private host name remains after encoding")
    return encoded


def _sample(text: str) -> str:
    match = TOKEN.search(text)
    if match is None:
        return "bound token"
    start = max(0, match.start() - 40)
    end = min(len(text), match.end() + 40)
    return text[start:end].replace("\n", " ")


def _structural(path: str, text: str) -> str:
    if path.endswith(".py"):
        text = _strip_python(path, text)
    elif path.endswith(".json"):
        text = _strip_json(text)
    elif path == "api/openapi.yaml" or path.endswith("openapi.community.yaml"):
        text = _strip_openapi(text)
    elif path.endswith(".sql"):
        text = _strip_sql(text)
    elif path.endswith((".ts", ".tsx")):
        text = _strip_typescript(path, text)
    elif path.endswith(".md"):
        text = _strip_markdown(path, text)
    elif path == "Dockerfile.community":
        text = text.replace("COPY packages/goldeneye ./packages/goldeneye\n", "")
    return text.replace("/goldeneye/", "/workspace/")


def _strip_markdown(path: str, text: str) -> str:
    text = re.sub(r"(?m)^\| Goldeneye \|.*\n", "", text)
    text = re.sub(r"(?m)^\| V1-OUT-010 \|.*\n", "", text)
    text = text.replace("elastic / Goldeneye", "elastic fabric")
    return _apply_documented_counts(path, text)


def _strip_sql(text: str) -> str:
    return re.sub(
        r"(?m)^-- -+\n-- Goldeneye bare-metal PaaS \(BOSS control plane\)\n-- -+\n\n"
        r"(?:.*\n)*?(?=-- Fail-closed:)",
        "",
        text,
    )


def _strip_openapi(text: str) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    skip_schema = False
    for line in lines:
        if skip_schema:
            if line.strip() == "" or line.startswith("     ") or line.startswith("\t"):
                continue
            skip_schema = False
        if skipping:
            if line.strip() == "" or line.startswith("   "):
                continue
            skipping = False
        if re.match(r"  /v1/goldeneye(?:/|\{|\s|$)", line, re.IGNORECASE):
            skipping = True
            continue
        if re.match(r"    Goldeneye[A-Za-z0-9]*:\s*$", line):
            skip_schema = True
            continue
        kept.append(line)
    return "".join(kept)


def _strip_json(text: str) -> str:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrivateHostLeak(f"JSON mention could not be parsed: {exc}") from exc
    cleaned = _clean_json(document)
    if isinstance(cleaned, dict) and isinstance(cleaned.get("operations"), list) and isinstance(cleaned.get("counts"), dict):
        counts: dict[str, int] = {}
        for operation in cleaned["operations"]:
            if isinstance(operation, dict):
                status = str(operation.get("mcp_status") or "")
                counts[status] = counts.get(status, 0) + 1
        # The generator counts present statuses only, so a status the redaction emptied is
        # dropped rather than kept at zero; otherwise the published matrix fails the public
        # tree's own `audit_mcp_api_parity.py --check-community`.
        cleaned["counts"] = {key: counts[key] for key in cleaned["counts"] if counts.get(key)}
        if "operation_count" in cleaned:
            cleaned["operation_count"] = len(cleaned["operations"])
    rendered = json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n"
    return rendered


def _clean_json(node: object) -> object:
    if isinstance(node, dict):
        path = node.get("path")
        domain = node.get("domain")
        if isinstance(path, str) and "goldeneye" in path.lower():
            return _DROP
        if isinstance(domain, str) and domain.lower() == "goldeneye":
            return _DROP
        cleaned: dict[str, object] = {}
        for key, value in node.items():
            if "goldeneye" in str(key).lower():
                continue
            child = _clean_json(value)
            if child is _DROP:
                continue
            cleaned[key] = child
        return cleaned
    if isinstance(node, list):
        items = []
        for item in node:
            child = _clean_json(item)
            if child is not _DROP:
                items.append(child)
        return items
    if isinstance(node, str) and TOKEN.search(node):
        if re.search(r"goldeneye[A-Za-z0-9_.]|[/]goldeneye", node, re.IGNORECASE):
            return _DROP
        return TOKEN.sub("reference host", node)
    return node


class _Drop:
    pass


_DROP = _Drop()


def _strip_python(path: str, text: str) -> str:
    if path == "engine/tests/test_ports.py":
        text = _delete_span(
            text,
            "#: A named integration may say its own name",
            "def _engine_sources():",
        )
        text = _delete_span(
            text,
            "def test_only_the_platform_integration_names_the_platform():",
            "def test_default_classes_are_the_documented_oss_implementations():",
        )
    if path == "engine/tests/test_infra_ui_v1_isolated.py":
        text = text.replace(
            '    goldeneye = (ROOT / "engine/boss_engine/services/goldeneye_paas.py").read_text(\n'
            '        encoding="utf-8"\n'
            "    )\n",
            "",
        )
        text = text.replace(
            '    for name, source in (("elastic_fabric", elastic), ("goldeneye_paas", goldeneye)):\n',
            '    for name, source in (("elastic_fabric", elastic),):\n',
        )
    if path == "engine/boss_engine/services/api_catalog.py":
        text = text.replace(
            '    "goldeneye": {\n'
            '        "title": "Goldeneye bare-metal PaaS",\n'
            '        "description": "Nodes, modules/packages, app deploys, fleet status.",\n'
            '        "prefixes": ["/v1/goldeneye"],\n'
            "    },\n",
            "",
        )
    if path == "engine/boss_engine/services/agent_kms.py":
        text = text.replace(
            '"protocol": "Goldeneye ops-agent /secret"',
            '"protocol": "Platform ops-agent /secret"',
        )
        text = text.replace(
            '"stars_note": "BOSS Goldeneye kit ops-agent"',
            '"stars_note": "BOSS platform kit ops-agent"',
        )
    if path == "engine/boss_engine/services/console_v2.py":
        text = text.replace("|goldeneye profile check", "")
    if path == "scripts/tests/test_metatron_tui.py":
        text = text.replace("len(records) == 994", "len(records) == 983")
        text = text.replace('== {"openapi": 994, "operator": 981}', '== {"openapi": 983, "operator": 970}')
        text = text.replace('assert "994" in features', 'assert "983" in features')
        text = text.replace(
            'assert "994" in terminal and "981" in terminal',
            'assert "983" in terminal and "970" in terminal',
        )
        text = text.replace("len(operator_records) == 981", "len(operator_records) == 970")
    if path == "scripts/tests/test_mcp_api_parity.py":
        text = _delete_span(
            text,
            '    assert rows[("POST", "/v1/goldeneye/apps/deploy")]["mcp_status"] == (\n',
            '    assert rows[("POST", "/v1/activity/retention/prune")]["mcp_status"] == "excluded"\n',
        )
        text = text.replace(
            'assert sum(row["mcp_status"] == "operator_only_unavailable"\n'
            '               for row in community["operations"]) == 2',
            'assert sum(row["mcp_status"] == "operator_only_unavailable"\n'
            '               for row in community["operations"]) == 0',
        )
        text = text.replace('assert len(overlay["operations"]) == 19', 'assert len(overlay["operations"]) == 14')
    if path == "scripts/tests/test_typed_request_bodies_wave3.py":
        text = "\n".join(
            line for line in text.splitlines(keepends=True)
            if "/v1/goldeneye" not in line.lower()
        ) if False else text
        text = re.sub(r"(?m)^.*\/v1\/goldeneye.*\n", "", text)
    if path == "apps/api/tests/test_e2e_infra_ui_v1.py":
        # Line deletion would leave execute() with no argument and would
        # rename the status tuple into invalid Python. Remove the whole
        # statements. The dedicated status test is dropped with its function.
        text = _replace_exact(
            text,
            "GET /v1/elastic/status, /v1/goldeneye/status, /v1/provider-usage/summary\n",
            "GET /v1/elastic/status, /v1/provider-usage/summary\n",
        )
        text = _replace_exact(
            text,
            '        connection.execute(text("DROP TABLE IF EXISTS goldeneye_deploys CASCADE"))\n'
            '        connection.execute(text("DROP TABLE IF EXISTS goldeneye_apps CASCADE"))\n'
            '        connection.execute(\n'
            '            text("DROP TABLE IF EXISTS goldeneye_package_installs CASCADE")\n'
            '        )\n'
            '        connection.execute(text("DROP TABLE IF EXISTS goldeneye_nodes CASCADE"))\n',
            "",
        )
        text = _replace_exact(
            text,
            '    goldeneye = client.get("/v1/goldeneye/status", params={"project_id": PROJECT_ID})\n',
            "",
        )
        text = _replace_exact(
            text,
            '        (goldeneye, "goldeneye"),\n',
            "",
        )
    text = _strip_python_definitions(text)
    text = _rewrite_python_comments(text)
    text = text.replace("/goldeneye/", "/workspace/")
    text = re.sub(r"(?m)^.*\/v1\/goldeneye.*\n", "", text)
    text = re.sub(r"(?m)^.*\bgoldeneye_[A-Za-z0-9_]+.*\n", "", text)
    return text


def _replace_exact(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise PrivateHostLeak(f"expected private-host block was not found: {old[:60]}")
    return text.replace(old, new, 1)


def _delete_span(text: str, start: str, end: str) -> str:
    start_at = text.find(start)
    end_at = text.find(end, start_at if start_at >= 0 else 0)
    if start_at < 0 or end_at < 0 or end_at < start_at:
        raise PrivateHostLeak(f"expected private-host block was not found: {start[:60]}")
    return text[:start_at] + text[end_at:]


def _scan_signature(line: str, paren: int) -> tuple[int, bool]:
    """Track parentheses so a type annotation colon does not end the signature."""
    closed = False
    for char in line.split("#", 1)[0]:
        if char == "(":
            paren += 1
        elif char == ")":
            paren -= 1
        elif char == ":" and paren <= 0:
            closed = True
            break
    return paren, closed


def _strip_python_definitions(text: str) -> str:
    lines = text.splitlines(keepends=True)
    drop: set[int] = set()
    index = 0
    decorators: list[int] = []
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if stripped.startswith("@"):
            decorators.append(index)
            index += 1
            continue
        match = re.match(r"(?:async\s+def|def|class)\s+(\w+)", stripped)
        if match and TOKEN.search(match.group(1)):
            indent = len(line) - len(stripped)
            drop.update(decorators)
            drop.add(index)
            paren, closed = _scan_signature(line, 0)
            index += 1
            while index < len(lines) and not closed:
                drop.add(index)
                paren, closed = _scan_signature(lines[index], paren)
                index += 1
            if not closed:
                raise PrivateHostLeak("private-host function signature did not close")
            while index < len(lines):
                nxt = lines[index]
                if nxt.strip() == "":
                    following = index + 1
                    while following < len(lines) and lines[following].strip() == "":
                        following += 1
                    if following >= len(lines):
                        drop.add(index)
                        index += 1
                        continue
                    next_stripped = lines[following].lstrip()
                    next_indent = len(lines[following]) - len(next_stripped)
                    if next_indent <= indent:
                        break
                    drop.add(index)
                    index += 1
                    continue
                next_indent = len(nxt) - len(nxt.lstrip())
                if next_indent <= indent:
                    break
                drop.add(index)
                index += 1
            decorators = []
            continue
        if re.match(r"(?:from|import)\s+", stripped) and TOKEN.search(line):
            drop.add(index)
            index += 1
            decorators = []
            continue
        assigned = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
        if assigned and TOKEN.search(assigned.group(1)):
            drop.add(index)
            index += 1
            decorators = []
            continue
        decorators = []
        index += 1
    return "".join(line for number, line in enumerate(lines) if number not in drop)


def _rewrite_python_comments(text: str) -> str:
    rewritten = []
    for line in text.splitlines(keepends=True):
        marker = line.find("#")
        if marker >= 0 and TOKEN.search(line[marker:]):
            code, comment = line[:marker], line[marker:]
            if not TOKEN.search(code):
                comment = re.sub(r"goldeneye[A-Za-z0-9_]*", "the private host", comment, flags=re.IGNORECASE)
                line = code + comment
        rewritten.append(line)
    return "".join(rewritten)


def _strip_typescript(path: str, text: str) -> str:
    text = text.replace("|goldeneye profile check", "")
    text = text.replace(', "goldeneye profile check"', "")
    if path == "apps/web/lib/infra-fabric.ts":
        text = _strip_infra_fabric_module(text)
    elif path == "apps/web/app/infra-fabric-ui.tsx":
        text = _strip_infra_fabric_ui(text)
    elif path == "apps/web/tests/infra-fabric.test.ts":
        text = _strip_infra_fabric_test(text)
    elif path == "apps/web/app/work-orchestration-ui.tsx":
        text = text.replace('placeholder="goldeneye"', 'placeholder="workspace"')
    return text


def _strip_infra_fabric_module(text: str) -> str:
    text = text.replace(" * Goldeneye PaaS, and provider usage ledger.\n", " * and provider usage ledger.\n")
    text = _delete_balanced_declaration(text, "export type GoldeneyeStatusFeed")
    text = _delete_balanced_declaration(text, "export function emptyGoldeneyeStatus")
    text = _delete_balanced_declaration(text, "export async function fetchGoldeneyeStatus")
    text = text.replace("  goldeneye: GoldeneyeStatusFeed;\n", "")
    text = text.replace('      goldeneye: emptyGoldeneyeStatus("not requested", resolved),\n', "")
    text = text.replace(
        "  const [multiCloud, elastic, goldeneye, providerUsage] = await Promise.all([\n"
        "    fetchMultiCloudCatalog(),\n"
        "    fetchElasticStatus(resolved),\n"
        "    fetchGoldeneyeStatus(resolved),\n"
        "    fetchProviderUsageSummary(resolved),\n"
        "  ]);\n",
        "  const [multiCloud, elastic, providerUsage] = await Promise.all([\n"
        "    fetchMultiCloudCatalog(),\n"
        "    fetchElasticStatus(resolved),\n"
        "    fetchProviderUsageSummary(resolved),\n"
        "  ]);\n",
    )
    text = text.replace(
        "  if (goldeneye.status === \"error\" && goldeneye.blockedReason) {\n"
        "    partialFailures.push(`Goldeneye: ${goldeneye.blockedReason}`);\n"
        "  }\n",
        "",
    )
    text = text.replace("    elastic,\n    goldeneye,\n    providerUsage,\n", "    elastic,\n    providerUsage,\n")
    return text


def _strip_infra_fabric_ui(text: str) -> str:
    text = text.replace(" * elastic fabric, Goldeneye PaaS,\n", " * elastic fabric,\n")
    text = text.replace("  const { multiCloud, elastic, goldeneye, providerUsage, partialFailures } = feed;\n",
                        "  const { multiCloud, elastic, providerUsage, partialFailures } = feed;\n")
    text = text.replace("    elastic.apiReachable ||\n    goldeneye.apiReachable ||\n", "    elastic.apiReachable ||\n")
    text = text.replace(
        "        Multi-cloud catalog, elastic worker fabric, Goldeneye bare-metal PaaS, and\n",
        "        Multi-cloud catalog, elastic worker fabric, and\n",
    )
    text = text.replace(
        '          <code className="mono">/v1/elastic/*</code>,{" "}\n'
        '          <code className="mono">/v1/goldeneye/*</code>, and{" "}\n',
        '          <code className="mono">/v1/elastic/*</code>, and{" "}\n',
    )
    start = text.find("        {/* Goldeneye */}")
    end = text.find("        {/* Provider usage */}")
    if start < 0 or end < 0 or end < start:
        raise PrivateHostLeak("infra fabric panel block was not found")
    text = text[:start] + text[end:]
    text = text.replace('        <code className="mono">docs/api/GOLDENEYE.md</code>\n', "")
    return text


def _strip_infra_fabric_test(text: str) -> str:
    text = text.replace("    fetchGoldeneyeStatus,\n", "")
    text = text.replace("    emptyGoldeneyeStatus,\n", "")
    text = _delete_it_blocks(text, re.compile(r"goldeneye", re.IGNORECASE))
    text = re.sub(
        r"\n      if \(String\(url\)\.includes\(\"/v1/goldeneye/status\"\)\) \{\n(?:.*\n)*?      \}\n",
        "\n",
        text,
    )
    text = text.replace('    expect(feed.goldeneye.status).toBe("not_configured");\n', "")
    text = text.replace('    expect(feed.goldeneye.hostConnected).toBe(false);\n', "")
    text = text.replace('    expect(emptyGoldeneyeStatus("x").realHostEffects).toBe(false);\n', "")
    text = text.replace('    expect(ui).toContain("docs/api/GOLDENEYE.md");\n', "")
    text = text.replace('    expect(ui).toContain("aria-label=\\"Goldeneye PaaS\\"");\n', "")
    text = text.replace('    expect(ui).toContain(\'data-empty-state="goldeneye-not-configured"\');\n', "")
    text = text.replace('    expect(ui).toContain("Real host effects");\n', "")
    return text


def _delete_it_blocks(text: str, title: re.Pattern[str]) -> str:
    cursor = 0
    pieces: list[str] = []
    while True:
        found = text.find("it(", cursor)
        if found < 0:
            pieces.append(text[cursor:])
            break
        title_match = re.match(r"""it\(\s*(['"`])(.*?)\1""", text[found:], re.DOTALL)
        if title_match is None:
            pieces.append(text[cursor:found + 3])
            cursor = found + 3
            continue
        block_end = _match_delim(text, found + 2, "(", ")")
        if block_end < len(text) and text[block_end] == ";":
            block_end += 1
        if block_end < len(text) and text[block_end] == "\n":
            block_end += 1
        if title.search(title_match.group(2)):
            pieces.append(text[cursor:found])
        else:
            pieces.append(text[cursor:block_end])
        cursor = block_end
    return "".join(pieces)


def _delete_balanced_declaration(text: str, signature: str) -> str:
    start = text.find(signature)
    if start < 0:
        raise PrivateHostLeak(f"missing declaration {signature}")
    line_start = text.rfind("\n", 0, start)
    comment_start = line_start
    # Drop the doc comment immediately above the declaration.
    previous = text.rfind("\n", 0, max(0, line_start - 1))
    if previous >= 0 and text[previous + 1:line_start].strip() == "*/":
        comment = text.rfind("/**", 0, previous)
        if comment >= 0:
            comment_start = text.rfind("\n", 0, comment)
    brace = text.find("{", start)
    if brace < 0:
        raise PrivateHostLeak(f"declaration has no body: {signature}")
    end = _match_delim(text, brace, "{", "}")
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[: comment_start + 1] + text[end:]


def _match_delim(text: str, index: int, open_char: str, close_char: str) -> int:
    if text[index] != open_char:
        # `it(` passes the index of `(`.
        raise PrivateHostLeak("delimiter mismatch")
    depth = 0
    while index < len(text):
        char = text[index]
        if char in ("'", '"', "`"):
            index = _skip_string(text, index)
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise PrivateHostLeak("unbalanced delimiter")


def _skip_string(text: str, index: int) -> int:
    quote = text[index]
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        if quote == "`" and text[index] == "$" and index + 1 < len(text) and text[index + 1] == "{":
            index = _match_delim(text, index + 1, "{", "}")
            continue
        index += 1
    return index


def _replace_prose(text: str) -> tuple[str, bool]:
    bound = False

    def replace(match: re.Match[str]) -> str:
        nonlocal bound
        start, end = match.span()
        left = text[start - 1:start]
        right = text[end:end + 1]
        if left.isalnum() or left == "_" or right.isalnum() or right == "_":
            bound = True
            return match.group(0)
        if left == "/" or right == "/":
            bound = True
            return match.group(0)
        if right == "." and end + 1 < len(text) and (text[end + 1].isalnum() or text[end + 1] == "_"):
            bound = True
            return match.group(0)
        return "reference host"

    return TOKEN.sub(replace, text), bound
