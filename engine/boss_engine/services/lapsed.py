"""WHAT LAPSED, AND WHAT TO DO (R3, BOSSAPP-1431).

A run that outlives a token, handle or hold it was given used to end with the bare code
("boss mcp gateway: token_expired", "handle_expired"): the engine reported a tool or model
failure, recovery read it as an auth problem, and nobody could tell that the run was simply
longer than the credential. Every gateway that refuses an expired credential says this sentence
after its code (the code stays first, for classifiers and audit rows).
"""
from __future__ import annotations

#: code -> the sentence: what lapsed, why, and what to do.
SENTENCES: dict[str, str] = {
    # mcp_gateway: the run token a build session calls BOSS's tools with
    "token_expired": (
        "this session's run token reached the end of its lifetime while the session was still "
        "running (it lasts BOSS_CODE_AGENT_TIMEOUT_SECONDS + 15 minutes from when the session "
        "started; time the build waited in the queue before that is not counted). Nothing was "
        "lost: retry the step (press Retry on this step) and BOSS issues a fresh token; work "
        "that needs longer needs a higher BOSS_CODE_AGENT_TIMEOUT_SECONDS in the host's .env"),
    # streaming_model_gateway, mcp_gateway's host lane, egress: the run handle a session or a
    # guest reaches its model, tools or the network with
    "handle_expired": (
        "this run's handle (its access to models, tools or the network) reached the end of its "
        "lifetime while the run was still going. Nothing was lost: retry the step (press "
        "Retry on this step) and BOSS issues a fresh handle; a run this long should be split "
        "into smaller steps"),
    "token_revoked": (
        "this run's token was revoked (the run was cancelled, finished or its grant was "
        "withdrawn); start the work again from its project's Workflows or Code Tasks if it "
        "should still run"),
    "handle_revoked": (
        "this run's handle was revoked (the run was cancelled, finished or its grant "
        "was withdrawn); start the work again from its project's Workflows or Code Tasks if it "
        "should still run"),
}


def say(reason: str) -> str:
    """`reason`, followed by what lapsed and what to do when BOSS knows; else `reason`."""
    sentence = SENTENCES.get(reason)
    return f"{reason}: {sentence}" if sentence else reason
