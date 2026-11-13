"""boss.extension/v1 (BOSSAPP-861): general-purpose extensions, BESIDE boss.plugin/v1.

    contract   the manifest (boss-extension.yaml) and the closed capability list
    sdk        what an extension author subclasses
    registry   install (with checksum + consent), the trust gate, enable/disable per project
    broker     one RPC = one isolated child process (child.py), platform/runner.py's isolation
    tools      tools.register: BOSS's MCP endpoint for an extension's tools
    events     events.subscribe: the filtered, acked feed of the events table
    jobs       jobs.schedule (+ the event pump): leased so one process fires
    webhooks   webhooks.receive: per-hook secrets verified BOSS-side
    state      state.read / state.write: the namespaced, size-capped KV
    ui         ui.slot.register: sandboxed-iframe panels
"""
