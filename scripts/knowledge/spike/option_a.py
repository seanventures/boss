"""Option A: Graphiti (graphiti-core) with its model and embedding calls through BOSS's gateway.

Graphiti owns extraction, resolution, invalidation and search. BOSS has to supply:

* `GatewayLLMClient`, an `LLMClient` whose every call goes to a BOSS chat adapter. In product
  that is `model_routing.governed(...)`, which reserves budget and writes a receipt. Graphiti
  issues model calls concurrently (`max_coroutines`), while a governed adapter is bound to
  one SQLAlchemy connection. So each call either runs serialised on that connection or opens
  its own connection and reservation. The spike runs calls in threads, which is the
  per-call-connection shape.
* `GatewayEmbedder`, an `EmbedderClient` routed the same way. BOSS has no governed embeddings
  path yet, so this needs a new gateway capability whichever option wins.
* A graph driver. Graphiti has no Postgres or AGE driver. The spike ingests on Graphiti's
  primary Neo4j driver, in a throwaway container, to measure Graphiti's own pipeline (calls,
  tokens, overhead). Its deprecated embedded Kuzu driver fails in graphiti-core 0.31. The
  cost of writing an AGE + pgvector driver is estimated from the existing drivers' size in
  the spike report; it is not built here.
* Telemetry off: graphiti-core sends PostHog analytics unless GRAPHITI_TELEMETRY_ENABLED=false.

Spike-only: graphiti-core is not a BOSS dependency (`pip install graphiti-core kuzu` in a
throwaway venv).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import typing
from typing import Any

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig


class GatewayLLMClient(LLMClient):
    """Every Graphiti model call becomes one call on a BOSS chat adapter."""

    def __init__(self, adapter: Any):
        super().__init__(LLMConfig(model="boss-gateway", small_model="boss-gateway"), cache=False)
        self.adapter = adapter

    async def _generate_response(self, messages, response_model=None, max_tokens=0, model_size=None,
                                 *, model: str | None = None) -> dict[str, typing.Any]:
        system = "\n".join(m.content for m in messages if m.role == "system")
        prompt = "\n".join(m.content for m in messages if m.role != "system")

        def call():
            self.adapter.local.expect = response_model
            return self.adapter.create_text_response(prompt, system_prompt=system)

        _, text, _usage = await asyncio.to_thread(call)
        return json.loads(text)


class GatewayEmbedder(EmbedderClient):
    """Deterministic stand-in vectors; metered as the gateway would meter an embeddings call."""

    def __init__(self, meter: Any, dim: int = 256):
        self.meter = meter
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [(digest[i % len(digest)] - 128) / 128 for i in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    async def create(self, input_data) -> list[float]:
        texts = [input_data] if isinstance(input_data, str) else [str(x) for x in input_data]
        self.meter.record_embed(texts)
        return self._vec(texts[0])

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        self.meter.record_embed(list(input_data_list))
        return [self._vec(t) for t in input_data_list]


class NoRerank(CrossEncoderClient):
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(p, 1.0 / (i + 1)) for i, p in enumerate(passages)]


async def ingest(episodes, oracle, meter, uri: str, password: str) -> dict:
    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.nodes import EpisodeType

    driver = Neo4jDriver(uri=uri, user="neo4j", password=password)
    await driver.execute_query("MATCH (n) DETACH DELETE n")
    graphiti = Graphiti(graph_driver=driver, llm_client=GatewayLLMClient(oracle),
                        embedder=GatewayEmbedder(meter), cross_encoder=NoRerank())
    await graphiti.build_indices_and_constraints()
    per_episode = []
    for ep in episodes:
        oracle.episode = ep
        before = meter.calls
        result = await graphiti.add_episode(
            name=ep.id, episode_body=ep.body, source_description=f"BOSS {ep.kind}",
            reference_time=ep.reference_time, source=EpisodeType.text, group_id="northpeak")
        per_episode.append({"id": ep.id, "calls": meter.calls - before,
                            "edges": [e.fact for e in result.edges],
                            "invalidated": [e.fact for e in result.edges if e.invalid_at or e.expired_at]})
    # Current (not invalidated) facts, from Graphiti's own store.
    records, _, _ = await driver.execute_query(
        "MATCH (:Entity)-[r:RELATES_TO]->(:Entity) "
        "RETURN r.fact AS fact, r.invalid_at IS NOT NULL OR r.expired_at IS NOT NULL AS closed")
    await graphiti.close()
    return {"per_episode": per_episode, "facts": [dict(r) for r in records]}
