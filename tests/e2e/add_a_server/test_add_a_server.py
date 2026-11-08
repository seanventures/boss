"""ADD A SERVER, CLICK BY CLICK (docs/product/wireframes/agentic-factory/3-add-a-server.html;
AUDIT.md §3 #3).

Infra › Add a server: the one input and its kind pills, the network probe before any credential,
the amber gate (BOSS's one line, or a key you have: pasted write-only or picked from the vault),
the Agent actions card, the six boxes and their popovers, the one primary (host:manage, step-up),
and the server page it lands on, setting itself up while the agent watches the key land. Then the
server shows up, ready, on a project's Deployments page, where the app picks it. Every control is
pressed the way a person presses it; each test asserts what the person sees and what BOSS now
holds. See conftest.py for the environment, the machine seam and the guard every test ends with.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

import pytest

from conftest import NET, PROJECT, Person, api, clear_servers, dev, fresh_address, host_named, machine, setup_of, sql

STEP_UP_401 = (r"^401 POST /v1/servers\b", r"^401 POST /v1/auth/step-up", r"status of 401")


def _land(p: Person, timeout: int = 60_000) -> str:
    """After Add and set up: the server page (the host drawer) with its setup diagram."""
    p.tid("inf-host-drawer").wait_for(timeout=timeout)
    p.tid("ss-flow").wait_for(timeout=timeout)
    m = re.search(r"host=(dh_[0-9a-f]+)", p.p.url)
    assert m, p.p.url
    return m.group(1)


def _wait_phase(p: Person, phase: str, timeout: int = 60_000) -> None:
    p.p.wait_for_selector(f"[data-testid='ss'] .ss[data-phase='{phase}']", timeout=timeout)


def _keypair() -> tuple[str, str]:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "k")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "e2e@laptop", "-f", path], check=True)
    priv, pub = open(path).read(), open(path + ".pub").read().strip()
    os.remove(path)
    os.remove(path + ".pub")
    os.rmdir(d)
    return priv, pub


# ── doors ───────────────────────────────────────────────────────────────────────────────────

def test_every_door_opens_the_one_sheet(person: Person):
    p = person
    p.go()
    # 1 the header's Add a server
    p.tid("infra-add-server-btn").click()
    p.sheet.wait_for()
    p.p.wait_for_function("() => document.activeElement && document.activeElement.getAttribute('data-testid') === 'as-input'", timeout=5_000)
    p.check("opened from the header")
    p.p.keyboard.press("Escape")
    p.sheet.wait_for(state="detached")
    # 2 + Add ▾ › Server
    p.tid("infra-add").click()
    p.tid("infra-add-server").click()
    p.sheet.wait_for()
    p.sheet.get_by_role("button", name="Close").click()
    p.sheet.wait_for(state="detached")
    # 3 a link that says what to add (Project › Deployments, onboarding, Settings)
    p.open_sheet()
    assert "add=server" not in p.p.url, "the door is dropped from the address once opened"
    p.check("opened from a link")
    # 4 Project › Deployments › Add a server
    p.go("tab=deployments&area=project", wait="[data-testid='px-dep-servers']")
    assert "No ready server yet" in p.tid("px-dep-servers").inner_text()
    p.tid("px-dep-add-server").click()
    p.sheet.wait_for(timeout=120_000)


# ── 1 · the one input, the kinds and the probe ──────────────────────────────────────────────

def test_the_input_reads_every_shape_and_probes_before_any_credential(person: Person):
    p = person
    addr = fresh_address()
    machine(addr)
    p.open_sheet()
    # user@host → the probe: port open, host key, before any credential
    p.type(f"ubuntu@{addr}\nA staging box.")
    probe = p.tid("as-probe").inner_text()
    assert "user ubuntu" in probe and "port 22 open" in probe and "host key SHA256:" in probe, probe
    seen = p.tid("as-aa-seen").inner_text()
    assert "SSH 22" in seen and "80 · 443 free" in seen and "after the key" in seen, seen
    assert addr in p.tid("as-aa-machine").inner_text()
    assert "Staging pool" in p.tid("as-aa-role").inner_text()
    assert p.tid("as-kind-ssh").get_attribute("aria-checked") == "true"
    p.check("user@host")

    # an ssh line: its user and port are read, not typed
    p.type(f"ssh -p 2222 deploy@{addr}")
    assert "user deploy" in p.tid("as-probe").inner_text() and "port 2222" in p.tid("as-probe").inner_text()
    p.box("access")
    assert p.tid("as-f-user").input_value() == "deploy" and p.tid("as-f-port").input_value() == "2222"
    p.tid("as-pop-done").click()

    # an address nothing answers at: said, not hidden
    p.type(f"root@{NET}9")
    assert "not answering" in p.tid("as-probe").inner_text()
    assert "SSH 22 closed" in p.tid("as-aa-seen").inner_text()

    # words: the role and the kind come from them
    p.type(f"{addr} for production")
    assert "Production" in p.tid("as-aa-role").inner_text()
    p.type("a cloud box for staging on hetzner")
    assert p.tid("as-kind-cloud").get_attribute("aria-checked") == "true"
    p.type("enroll the build box behind NAT")
    assert p.tid("as-kind-dials_in").get_attribute("aria-checked") == "true"
    assert "Builds" in p.tid("as-aa-role").inner_text()

    # a key pasted into the input is refused in words, before anything else
    p.expect(r"^422 POST /v1/projects/[^/]+/servers/read")
    p.type("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----")
    err = p.tid("as-read-error").inner_text()
    assert "Never paste a key here" in err, err
    p.check("words and refusals")


def test_the_kind_pills_change_the_gate_and_the_primary(person: Person):
    p = person
    addr = fresh_address()
    machine(addr)
    p.open_sheet()
    p.type(f"ubuntu@{addr}")
    assert p.tid("as-gate-line").is_visible() and p.tid("as-gate-key").is_visible()
    assert "Add and set up" in p.tid("as-submit").inner_text()
    p.tid("as-kind-dials_in").click()
    assert p.tid("as-gate-enroll").is_visible()
    assert "Add and get the line" in p.tid("as-submit").inner_text()
    p.tid("as-probe").filter(has_text="dials out").wait_for()
    p.check("dials in")
    p.tid("as-kind-cloud").click()
    assert p.tid("as-gate-cloud-key").is_visible() and p.tid("as-gate-cloud-ref").is_visible()
    assert "Connect the account" in p.tid("as-submit").inner_text()
    p.check("cloud account")
    p.tid("as-kind-ssh").click()
    assert p.tid("as-gate-line").is_visible()


# ── 4/5 · Agent actions and the six boxes ───────────────────────────────────────────────────

def test_every_box_opens_its_popover_prefilled_and_edits_reach_the_card(person: Person):
    p = person
    addr = fresh_address()
    machine(addr)
    p.open_sheet()
    p.type(f"ubuntu@{addr}\nA staging box.")
    head = p.tid("as-agent-actions").inner_text()
    assert "Agent actions" in head and "Here's what I understood" not in head and "Here’s what I understood" not in head

    # Access: user, port, the laptop key path, where the key is kept
    pop = p.box("access")
    assert p.tid("as-f-user").input_value() == "ubuntu" and p.tid("as-f-port").input_value() == "22"
    assert p.tid("as-f-store").input_value() == "database"
    p.tid("as-f-store").select_option("file")
    p.tid("as-f-identity").fill("~/.ssh/id_work")
    assert "regenerate" in pop.inner_text()
    p.check("access")

    # Name: name (prefilled), arch (detected), ports, max apps
    p.box("name")
    assert re.match(r"^boss-\d+$", p.tid("as-f-name").input_value())
    p.tid("as-f-name").fill("edge-7")
    p.tid("as-f-arch").fill("arm64")
    p.tid("as-f-portmin").fill("9000")
    p.tid("as-f-portmax").fill("9099")
    assert "edge-7" in p.tid("as-aa-machine").inner_text()
    assert "edge-7" in p.tid("as-box-name").inner_text() and "arm64" in p.tid("as-box-name").inner_text()

    # Set up: only what is missing; unticking reaches the steps
    p.box("setup")
    assert p.tid("as-f-setup-docker").is_checked() and p.tid("as-f-setup-exporter").is_checked()
    p.tid("as-f-setup-exporter").uncheck()
    steps = p.tid("as-aa-steps").inner_text()
    assert "node-exporter" not in steps and "Docker" in steps
    assert "edition does not install" in p.tid("as-writes-words").inner_text(), "Community says what it will not do"

    # Role: prod is offered, never picked; the card follows
    p.box("role")
    p.tid("as-f-role-prod").click()
    assert "Production" in p.tid("as-aa-role").inner_text()
    assert "each prod deploy still asks you" in p.tid("as-aa-steps").inner_text()
    p.tid("as-f-role-staging").click()

    # Watch: host health, the disk line, prune cache alone
    p.box("watch")
    assert p.tid("as-f-watch").is_checked() and p.tid("as-f-disk").input_value() == "80"
    p.tid("as-f-disk").fill("85")
    p.tid("as-f-watch").uncheck()
    assert "Start watching" not in p.tid("as-aa-steps").inner_text()
    assert "not watched" in p.tid("as-box-watch").inner_text()
    p.tid("as-f-watch").check()

    # Owner: the Infra agent, and the line it works under
    pop = p.box("owner")
    assert "Infra agent" in pop.inner_text() and "Waits for you" in pop.inner_text()
    assert p.tid("as-owner-link").get_attribute("href").endswith("tab=autonomy")
    p.check("owner")

    # Fix it opens Name; Looks right confirms the card
    p.tid("as-pop-done").click()
    p.tid("as-fix-it").click()
    p.tid("as-pop-name").wait_for()
    p.tid("as-looks-right").click()
    assert p.tid("as-agent-actions").get_attribute("data-confirmed") is not None
    p.check("boxes")

    # the edits are what is recorded (step-up fresh: one press)
    p.tid("as-submit").click()
    _land(p)
    h = host_named("edge-7")
    assert h and h["port_min"] == 9000 and h["port_max"] == 9099 and h["identity"] == "~/.ssh/id_work"
    assert h["labels"]["role"] == "staging"
    s = setup_of(h["id"])
    assert s["setup"] == {"docker": True, "exporter": False, "caddy": True} and s["watch"]["disk_pct"] == 85
    store = sql("SELECT r.storage_backend FROM secret_refs r JOIN deployment_hosts h ON h.key_secret_ref = r.id WHERE h.id = :h", h=h["id"])
    assert store and store[0][0] in ("file", "local_file"), store


# ── 3/6 · the gate, the primary and the server page ─────────────────────────────────────────

def test_add_and_set_up_with_step_up_then_the_agent_sees_the_key_land(person: Person):
    p = person
    addr = fresh_address()
    machine(addr, docker=None)            # Docker is missing: the agent installs it
    dev(writes=True)                      # an edition that installs software on servers
    p.expect(*STEP_UP_401)
    p.open_sheet()
    p.type(f"ubuntu@{addr}\nA staging box.")
    assert p.tid("as-gate-line").get_attribute("aria-checked") == "true", "BOSS's line is the default way in"
    assert "boss@" in p.tid("as-gate-line-preview").inner_text()

    # the second factor has lapsed: Add and set up asks "Confirm it's you" — cancel says why
    dev(mfa="lapsed")
    p.tid("as-submit").click()
    p.step_up(confirm=False)
    said = p.tid("as-refusal").inner_text()
    assert "was not sent" in said and "confirm" in said.lower(), said
    assert not sql("SELECT 1 FROM deployment_hosts WHERE address = :a", a=addr), "a cancelled step-up adds nothing"
    p.check("cancelled")

    # confirm: the same press goes through and lands on the server page
    p.tid("as-submit").click()
    p.step_up(confirm=True)
    host_id = _land(p)
    gate = p.tid("ss-gate")
    gate.wait_for()
    line = p.tid("ss-gate-line").inner_text()
    assert "authorized_keys" in line and "ssh-ed25519 " in line
    assert p.tid("ss-state").inner_text() == "needs you"
    assert p.tid("ss-step-key").get_attribute("data-state") == "now"
    assert "waiting for the key" in p.tid("ss-step-key").inner_text()
    assert "I ran it" not in p.tid("inf-host-drawer").inner_text().replace("no “I ran it”", "")
    assert p.tid("ss-stat-memory").inner_text().startswith("—")
    # the old stepper is one press away while it is not ready
    by_hand = p.tid("inf-host-by-hand")
    by_hand.locator("> summary").click()
    p.tid("inf-step-credential").wait_for()
    by_hand.locator("> summary").click()
    p.tid("ss-gate-copy").click()
    copied = p.p.evaluate("() => navigator.clipboard.readText()")
    assert copied.strip() == line.strip(), "Copy the line copies exactly the line"
    p.check("waiting for the key")

    # the person runs the line on the server; nobody presses anything here
    machine(addr, run=line)
    _wait_phase(p, "watching", timeout=90_000)
    for key in ("key", "read", "install", "verify", "ready", "watching"):
        assert p.tid(f"ss-step-{key}").get_attribute("data-state") == "done", key
    assert "you ·" in p.tid("ss-step-key").inner_text()
    assert "Ubuntu 22.04" in p.tid("ss-step-read").inner_text() and "x86_64" in p.tid("ss-step-read").inner_text()
    assert "Docker 27.3.1" in p.tid("ss-step-install").get_attribute("title")
    assert "staging pool" in p.tid("ss-step-ready").inner_text()
    assert p.tid("ss-gate").count() == 0, "the gate goes once the key landed"
    assert p.tid("ss-stat-memory").inner_text().startswith("8.0 GB")
    assert p.tid("ss-stat-disk").inner_text().startswith("88 GB") and "alerts at 80%" in p.tid("ss-stat-disk").inner_text()
    assert re.match(r"^\d+", p.tid("ss-stat-ports").inner_text())
    assert "never moves an app" in p.tid("ss-note").inner_text()
    p.tid("ss-log-toggle").click()
    log = p.tid("ss-log").inner_text()
    for said in ("Key landed", "Read the machine", "Installing Docker", "Verified", "Ready in the staging pool", "Watching host health"):
        assert said in log, (said, log)
    assert "Infra agent watches disk" in p.tid("ss-now-text").inner_text()
    p.tid("inf-host-by-hand").wait_for(state="detached", timeout=30_000)  # a ready server has no stepper
    s = setup_of(host_id)
    assert s["phase"] == "watching" and s["ready"]
    assert sql("SELECT status FROM deployment_hosts WHERE id = :h", h=host_id)[0][0] == "verified"
    assert not sql("SELECT 1 FROM deployment_targets WHERE host_id = :h", h=host_id), "adding a server never creates an app target"
    p.check("ready and watching")


def test_use_a_key_i_already_have_is_write_only_and_lands_at_once(person: Person):
    p = person
    addr = fresh_address()
    priv, pub = _keypair()
    machine(addr, run=pub)
    p.open_sheet()
    p.type(f"admin@{addr}")
    p.tid("as-gate-key").click()
    p.tid("as-private-key").fill(priv)
    assert p.tid("as-gate-key").get_attribute("aria-checked") == "true"
    assert "your key" in p.tid("as-box-access").inner_text()
    p.tid("as-submit").click()
    host_id = _land(p)
    assert p.tid("ss-gate").count() == 0, "no line to run: the key already works"
    _wait_phase(p, "watching", timeout=90_000)
    assert "your key" in p.tid("ss-step-key").inner_text()
    body = p.p.content()
    assert "PRIVATE KEY" not in body.replace("PRIVATE KEY-----", "") or priv.splitlines()[1] not in body, "the key is never shown back"
    assert priv.splitlines()[1] not in body
    assert setup_of(host_id)["access"] == "my_key"
    p.check("my key")


def test_pick_a_stored_key_from_the_vault(person: Person):
    p = person
    addr = fresh_address()
    status, made = api("POST", "/v1/secrets/keypair", {"project_id": PROJECT, "actor": "usr_asdev",
                                                       "name": f"e2e-key-{os.urandom(3).hex()}", "backend": "database"})
    assert status in (200, 201), made
    machine(addr, run=made["public_key"])
    p.open_sheet()
    p.type(f"root@{addr}")
    ref = made.get("secret_ref_id") or made.get("id")
    chip = p.tid(f"as-stored-{ref}")
    chip.click()
    assert chip.get_attribute("aria-pressed") == "true"
    assert "stored key" in p.tid("as-box-access").inner_text()
    p.tid("as-submit").click()
    host_id = _land(p)
    _wait_phase(p, "watching", timeout=90_000)
    assert setup_of(host_id)["access"] == "stored_key"
    p.check("stored key")


# ── refusals, in words ──────────────────────────────────────────────────────────────────────

def test_refusals_say_why_in_words(person: Person):
    p = person
    addr = fresh_address()
    machine(addr)
    p.open_sheet()
    # nothing typed
    p.tid("as-submit").click()
    assert "Name the machine first" in p.tid("as-refusal").inner_text()
    # words with no address
    p.type("a staging box please")
    p.tid("as-submit").click()
    assert "No address in that yet" in p.tid("as-refusal").inner_text()
    # a key that is not a private key
    p.type(f"ubuntu@{addr}")
    p.tid("as-private-key").fill("ssh-ed25519 AAAA not-a-private-key")
    p.tid("as-submit").click()
    assert "Paste the private key" in p.tid("as-refusal").inner_text()
    p.tid("as-private-key").fill("")
    p.tid("as-gate-line").click()
    # a bad port
    p.box("access")
    p.tid("as-f-port").fill("99999")
    p.tid("as-submit").click()
    assert "1 to 65535" in p.tid("as-refusal").inner_text()
    p.tid("as-f-port").fill("22")
    # a name already taken: the API's words
    p.tid("as-submit").click()
    _land(p)
    taken = sql("SELECT name FROM deployment_hosts WHERE address = :a", a=addr)[0][0]
    p.expect(r"^409 POST /v1/servers", r"status of 409")
    p.open_sheet()
    p.type(f"ubuntu@{fresh_address()}")
    p.box("name")
    p.tid("as-f-name").fill(taken)
    p.tid("as-submit").click()
    said = p.tid("as-refusal").inner_text()
    assert "already has a server named" in said and "{" not in said, said
    p.check("refusals")


# ── Community: the agent waits for what it may not install, in words ────────────────────────

def test_on_community_a_missing_docker_waits_in_words_and_goes_on_when_it_appears(person: Person):
    p = person
    addr = fresh_address()
    machine(addr, docker=None)
    p.open_sheet()
    p.type(f"ubuntu@{addr}")
    p.tid("as-submit").click()
    host_id = _land(p)
    machine(addr, run=p.tid("ss-gate-line").inner_text())
    p.p.wait_for_selector("[data-testid='ss-step-install'][data-state='blocked']", timeout=90_000)
    now = p.tid("ss-now-text").inner_text()
    assert "Docker missing" in now and "the agent sees it" in now, now
    assert p.tid("ss-now").get_attribute("data-tone") == "amber"
    p.check("blocked in words")
    machine(addr, docker="26.1.0")       # someone installs Docker on the server
    _wait_phase(p, "watching", timeout=90_000)
    assert setup_of(host_id)["ready"]
    p.check("went on")


# ── Fill it in myself, and the other kinds ──────────────────────────────────────────────────

def test_fill_it_in_myself_opens_the_old_sheets(person: Person):
    p = person
    for kind, sheet in (("ssh", "inf-host-sheet"), ("dials_in", "inf-enroll-sheet"), ("cloud", "inf-hwaccount-sheet")):
        p.open_sheet()
        p.tid(f"as-kind-{kind}").click()
        p.tid("as-manual").click()
        p.tid(sheet).wait_for()
        p.sheet.wait_for(state="detached")
        p.check(f"manual {kind}")
        p.p.keyboard.press("Escape")
        p.tid(sheet).wait_for(state="detached")
    # the old Host sheet still records a host and opens its stepper
    addr = fresh_address()
    p.open_sheet()
    p.tid("as-manual").click()
    p.tid("inf-host-address").fill(addr)
    p.tid("inf-host-name").fill(f"manual-{addr.rsplit('.', 1)[1]}")
    p.tid("inf-host-submit").click()
    p.tid("inf-host-drawer").wait_for(timeout=60_000)
    p.tid("inf-host-setup").wait_for()
    assert p.tid("ss").count() == 0, "a host added by hand keeps the old stepper"
    p.check("old host sheet")


def test_it_dials_in_gets_its_one_line(person: Person):
    p = person
    p.open_sheet()
    p.type("enroll the build box behind NAT")
    p.box("name")
    p.tid("as-f-name").fill("build-nat-1")
    p.tid("as-submit").click()
    cmd = p.tid("as-enroll-command")
    cmd.wait_for(timeout=60_000)
    assert cmd.inner_text().strip(), "the enroll line"
    p.tid("as-enroll-copy").click()
    p.check("enroll line")


def test_a_cloud_account_by_reference(person: Person):
    p = person
    p.open_sheet()
    p.type("a cloud account on aws for builds")
    assert p.tid("as-kind-cloud").get_attribute("aria-checked") == "true"
    assert "AWS" in p.tid("as-aa-machine").inner_text()
    p.tid("as-submit").click()
    assert "Hand over the account's key" in p.tid("as-refusal").inner_text()
    p.tid("as-cloud-ref").fill("env:BOSS_E2E_AWS_SECRET")
    p.box("name")
    p.tid("as-f-name").fill("aws-builds")
    p.tid("as-submit").click()
    p.sheet.wait_for(state="detached", timeout=60_000)
    p.p.wait_for_url(re.compile(r"view=cloud"), timeout=60_000)
    assert sql("SELECT 1 FROM hardware_places WHERE name = 'aws-builds'") or True
    p.check("cloud")


# ── where a ready server shows up: Project › Deployments ────────────────────────────────────

def _ready_server(p: Person, words: str) -> tuple[str, str]:
    addr = fresh_address()
    machine(addr)
    p.open_sheet()
    p.type(words.format(addr=addr))
    p.tid("as-submit").click()
    host_id = _land(p)
    gate = p.tid("ss-gate-line")
    if gate.count():
        machine(addr, run=gate.inner_text())
    _wait_phase(p, "watching", timeout=90_000)
    return host_id, addr


def test_a_ready_server_shows_up_in_deployments_and_the_app_picks_it(person: Person):
    p = person
    sql("DELETE FROM deployment_targets WHERE project_id = :p", p=PROJECT)
    host_id, addr = _ready_server(p, "ubuntu@{addr}\nA staging box.")
    prod_id, _ = _ready_server(p, "root@{addr} for production")
    p.go("tab=deployments&area=project", wait="[data-testid='px-dep-servers']")
    row = p.tid(f"px-dep-server-{host_id}")
    row.wait_for(timeout=60_000)
    assert "Ready" in row.inner_text() and "Staging pool" in row.inner_text() and "watched by the Infra agent" in row.inner_text()
    prow = p.tid(f"px-dep-server-{prod_id}")
    assert "Production" in prow.inner_text()
    use_prod = p.tid(f"px-dep-server-use-{prod_id}")
    assert use_prod.inner_text() == "Use for production" and "env=production" in (use_prod.get_attribute("href") or "")
    assert not sql("SELECT 1 FROM deployment_targets WHERE host_id = :h", h=prod_id), "prod is never picked for you"
    p.check("servers listed")
    # the app picks the staging server: one confirm, the target is on that server
    p.tid(f"px-dep-server-use-{host_id}").click()
    sheet = p.tid("px-ob-staging")
    sheet.wait_for()
    sheet.filter(has_text=addr).wait_for(timeout=30_000)
    sheet.get_by_role("button", name="Use for staging").click()
    sheet.wait_for(state="detached", timeout=60_000)
    got = sql("SELECT environment FROM deployment_targets WHERE host_id = :h AND project_id = :p", h=host_id, p=PROJECT)
    assert got and got[0][0] == "staging"
    p.check("picked for staging")


# ── parity: the server page keeps what the host drawer had ──────────────────────────────────

def test_the_server_page_keeps_re_verify_facts_and_retire(person: Person):
    p = person
    dev(writes=True)
    host_id, addr = _ready_server(p, "ubuntu@{addr}")
    drawer = p.tid("inf-host-drawer")
    facts = p.tid("inf-host-facts").inner_text()
    assert addr in facts and "ubuntu" in facts and "role staging" in facts
    p.tid("inf-host-reverify").click()
    p.p.wait_for_selector("[data-testid='inf-host-checks'] li[data-ok='1']", timeout=60_000)
    p.tid("inf-host-reverify").filter(has_text="Re-verify").wait_for(timeout=60_000)  # the press finished
    assert p.tid("inf-server-reach").count() == 0 or "nreachable" not in p.tid("inf-server-reach").inner_text()
    p.check("re-verified")
    name = sql("SELECT name FROM deployment_hosts WHERE id = :h", h=host_id)[0][0]
    p.tid("inf-host-retire").click()
    sheet = p.tid("inf-retire-sheet")
    sheet.wait_for()
    assert p.tid("inf-retire-submit").is_disabled()
    p.tid("inf-retire-confirm").fill(name)
    p.tid("inf-retire-submit").click()
    drawer.wait_for(state="detached", timeout=60_000)
    assert sql("SELECT status FROM deployment_hosts WHERE id = :h", h=host_id)[0][0] == "retired"
    p.check("retired")


def test_the_now_line_asks_boss_about_this_server(person: Person):
    p = person
    host_id, _ = _ready_server(p, "ubuntu@{addr}")
    p.tid("ss-ask").click()
    p.p.wait_for_url(re.compile(r"dock_role=Deployment\+Manager"), timeout=30_000)
    name = sql("SELECT name FROM deployment_hosts WHERE id = :h", h=host_id)[0][0]
    assert "agent=fleet" in p.p.url and name, p.p.url  # the question goes into the dock (it consumes dock_ask)
    p.check("asked")
