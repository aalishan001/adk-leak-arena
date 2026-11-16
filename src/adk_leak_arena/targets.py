"""Memory targets: the stores the arena attacks.

A :class:`MemoryTarget` seeds the arena dataset through the store's own write path and
answers searches under a caller identity. Scenarios are *application-layer mistakes*
expressed through each store's own API (a missing user filter, a tenant-only filter, a
spoofed user id, ...). Where a store has no such concept the scenario is reported as
**n/a** for that target, never as 0%.

Every target is wrapped in a Google ADK ``BaseMemoryService`` so it runs through the same
real ADK ``Runner`` + ``preload_memory`` pipeline, and the canary classifier judges the
returned rows the same way for every backend.

Built-in targets:

* ``unprotected``: pgvector table, isolation only by the app's ``WHERE`` clause.
* ``memshield``: adk-memshield (PostgreSQL forced RLS).
* ``memshield_unpinned``: adk-memshield with ``plan_cache_mode`` set back to ``auto``
  inside the transaction (the 0.1.x behaviour), for the prepared-statement + HNSW demo.
* ``memshield_superuser``: adk-memshield pointed at the superuser (admin) DSN. Its
  posture check refuses to run; the arena reports that as a configuration verdict.
* ``qdrant``: Qdrant payload filters (``pip install 'adk-leak-arena[qdrant]'``).
* ``mem0``: Mem0 OSS over Qdrant, raw adds without LLM extraction
  (``pip install 'adk-leak-arena[mem0]'``).

Qdrant and Mem0 rely on the application passing the right filter, like the unprotected
table; the arena measures what happens when it gets that filter wrong.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from .config import ArenaConfig
from .dataset import CallerPersona, SeedRecord

# Mem0 OSS sends usage telemetry to PostHog unless this is off; the arena never phones home.
os.environ.setdefault("MEM0_TELEMETRY", "False")

# Application-layer mistakes (the arena's failure modes) and what each needs to be
# expressible: a scenario that needs a concept the store lacks is n/a for that store.
FAILURE_MODE_NEEDS: dict[str, frozenset[str]] = {
    "control": frozenset(),
    "shared_tenant_no_user_filter": frozenset({"tenant"}),
    "adversarial_planted_lure": frozenset({"tenant"}),
    "param_spoof_user_id": frozenset(),
    "swarm_missing_clearance_filter": frozenset({"clearance"}),
    "strip_all_where_filters": frozenset(),
    "same_tier_shared_poisoning": frozenset({"clearance"}),
    "missing_compartment_filter": frozenset({"compartments"}),
    "forged_identity": frozenset(),
    "pooled_session_bleed": frozenset({"sql_session"}),
}


@dataclass(frozen=True)
class Capabilities:
    """Concepts a store can express through its own API."""

    tenant: bool = True
    user: bool = True
    clearance: bool = True
    compartments: bool = True
    sql_session: bool = False
    database_enforced: bool = False

    def supports(self, failure_mode: str) -> bool:
        needs = FAILURE_MODE_NEEDS.get(failure_mode, frozenset())
        return all(getattr(self, need) for need in needs)


@dataclass(frozen=True)
class Mistake:
    """The application-layer mistake a scenario injects for one query."""

    failure_mode: str = "control"
    simulate_missing_app_filter: bool = False
    spoofed_user_id: str | None = None


@dataclass
class TargetTelemetry:
    """Minimal telemetry for non-SQL targets (mirrors memshield's QueryTelemetry keys)."""

    operation: str
    sql: str
    session_context: dict[str, str]
    latency_ms: float
    row_count: int
    simulate_missing_app_filter: bool
    table_name: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _entry_from_row(row: dict[str, Any]) -> MemoryEntry:
    meta = {k: v for k, v in row.items() if k not in ("content", "id")}
    return MemoryEntry(
        id=str(row.get("id")) if row.get("id") is not None else None,
        content=types.Content(parts=[types.Part(text=str(row["content"]))], role="user"),
        author="user",
        custom_metadata=meta,
    )


def _seed_payload(rec: SeedRecord, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "tenant_id": rec.tenant_id,
        "user_id": rec.user_id,
        "scope": rec.scope,
        "min_clearance": int(rec.min_clearance),
        "compartments": list(getattr(rec, "compartments", ()) or ()),
        "seed_key": rec.key,
        "persona_label": rec.persona_label,
        "adversarial": bool(rec.is_adversarial),
        "content": rec.content,
    }
    if extra:
        payload.update(extra)
    return payload


class MemoryTarget:
    """Base class. Subclasses implement :meth:`reset`, :meth:`seed` and :meth:`search`."""

    name: str = ""
    label: str = ""
    protected: bool = False
    capabilities: Capabilities = Capabilities()
    version: str = ""

    def __init__(self, config: ArenaConfig, *, namespace: str = "") -> None:
        self.config = config
        self.namespace = namespace

    # Store lifecycle -------------------------------------------------------------------
    def reset(self) -> None:
        raise NotImplementedError

    def seed(self, records: Sequence[SeedRecord], *, extra: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def delete_tagged(self, key: str, value: Any) -> int:
        """Remove rows whose payload ``key`` equals ``value`` (fixtures, synthetic rows)."""
        return 0

    def search(self, *, persona: CallerPersona, query: str, top_k: int, mistake: Mistake) -> list[dict[str, Any]]:
        """Return rows as dicts with ``content`` plus owner keys (tenant_id, user_id, scope,
        min_clearance, compartments, seed_key, similarity, ...)."""
        raise NotImplementedError

    def posture(self) -> dict[str, Any] | None:
        return None

    def preflight(self) -> str | None:
        """Return a reason when the target refuses to run at all (a configuration verdict)."""
        return None

    def close(self) -> None:
        """Release per-target resources (pools, clients) at the end of a run namespace."""
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "protected": self.protected,
            "version": self.version,
            "capabilities": asdict(self.capabilities),
        }

    # ADK integration -------------------------------------------------------------------
    def build_service(
        self,
        *,
        persona: CallerPersona,
        mistake: Mistake,
        top_k: int,
        embed_fn: Callable[[Sequence[str]], list[list[float]]],
    ) -> BaseMemoryService:
        return TargetMemoryService(self, persona=persona, mistake=mistake, top_k=top_k)


class TargetMemoryService(BaseMemoryService):
    """Adapts a :class:`MemoryTarget` to ADK's ``BaseMemoryService`` for one turn."""

    def __init__(self, target: MemoryTarget, *, persona: CallerPersona, mistake: Mistake, top_k: int) -> None:
        self.target = target
        self.persona = persona
        self.mistake = mistake
        self.top_k = top_k
        self.failure_mode = mistake.failure_mode
        self.simulate_missing_app_filter = mistake.simulate_missing_app_filter
        self.last_query_telemetry: TargetTelemetry | None = None
        self.last_filter: Any = None

    async def add_session_to_memory(self, session: Any) -> None:  # attack sessions are never stored
        return None

    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        t0 = time.perf_counter()
        rows = await asyncio.to_thread(
            self.target.search, persona=self.persona, query=query, top_k=self.top_k, mistake=self.mistake
        )
        latency = (time.perf_counter() - t0) * 1000.0
        # Prefer the store call alone when the target measured it (query embedding excluded,
        # like the PostgreSQL targets); Mem0 embeds inside its own search call.
        store_ms = getattr(self.target, "last_store_ms", None)
        if store_ms is not None:
            latency = store_ms
        self.last_filter = getattr(self.target, "last_filter", None)
        self.last_query_telemetry = TargetTelemetry(
            operation="search_memory",
            sql=f"{self.target.label} filter: {json.dumps(self.last_filter, default=str)}",
            session_context={},
            latency_ms=latency,
            row_count=len(rows),
            simulate_missing_app_filter=self.mistake.simulate_missing_app_filter,
            table_name=getattr(self.target, "collection", self.target.name),
        )
        return SearchMemoryResponse(memories=[_entry_from_row(r) for r in rows])


# ----------------------------------------------------------------------------- pgvector
class UnprotectedTarget(MemoryTarget):
    """The 0.1.0 left target: a plain pgvector table, filtered only by app SQL."""

    name = "unprotected"
    label = "Unprotected pgvector"
    capabilities = Capabilities(sql_session=True)

    def build_service(self, *, persona, mistake, top_k, embed_fn):
        from .unprotected_service import UnprotectedPgVectorMemoryService

        return UnprotectedPgVectorMemoryService(
            dsn=self.config.db_dsn,
            tenant_id=persona.tenant_id,
            agent_clearance=persona.clearance,
            agent_compartments=tuple(getattr(persona, "compartments", ()) or ()),
            top_k=top_k,
            failure_mode=mistake.failure_mode,  # type: ignore[arg-type]
            simulate_missing_app_filter=mistake.simulate_missing_app_filter,
            spoofed_user_id=mistake.spoofed_user_id,
            embed_fn=embed_fn,
        )

    def close(self) -> None:
        from .unprotected_service import close_bleed_pool

        close_bleed_pool(self.config.db_dsn)


class MemShieldTarget(MemoryTarget):
    """The 0.1.0 right target: adk-memshield with forced RLS."""

    name = "memshield"
    label = "adk-memshield (RLS)"
    protected = True
    capabilities = Capabilities(sql_session=True, database_enforced=True)
    plan_cache_mode: str | None = None  # None = memshield's default (force_custom_plan)

    def __init__(self, config: ArenaConfig, *, namespace: str = "") -> None:
        super().__init__(config, namespace=namespace)
        try:
            from importlib.metadata import version

            self.version = version("adk-memshield")
        except Exception:  # pragma: no cover
            self.version = "unknown"

    @property
    def dsn(self) -> str:
        return self.config.db_dsn

    def preflight(self) -> str | None:
        """Run memshield's own posture check once; a refusal is a configuration verdict."""
        if not hasattr(self, "_preflight"):
            from adk_memshield import MemShieldConfig, MemShieldConfigurationError, MemShieldStore

            try:
                MemShieldStore(
                    MemShieldConfig(dsn=self.dsn, tenant_id="arena_preflight", table_name="memories"),
                    embed_fn=lambda texts: [[0.0] * self.config.embedding_dim for _ in texts],
                ).verify_rls_posture()
                self._preflight: str | None = None
            except MemShieldConfigurationError as exc:
                self._preflight = f"adk-memshield refused to run: {exc}"
        return self._preflight

    def build_service(self, *, persona, mistake, top_k, embed_fn):
        from adk_memshield import MemShieldConfig, MemShieldMemoryService

        verifier = None
        if mistake.failure_mode == "forged_identity":
            from adk_memshield import HmacIdentityVerifier

            verifier = HmacIdentityVerifier({_ARENA_KID: _ARENA_KEY})
        service = MemShieldMemoryService(
            config=MemShieldConfig(
                dsn=self.dsn,
                tenant_id=persona.tenant_id,
                agent_clearance=persona.clearance,
                compartments=list(getattr(persona, "compartments", ()) or ()),
                top_k=top_k,
                table_name="memories",
                simulate_missing_app_filter=mistake.simulate_missing_app_filter,
                identity_verifier=verifier,
            ),
            embed_fn=embed_fn,
        )
        if self.plan_cache_mode is not None:
            service.store.plan_cache_mode = self.plan_cache_mode
        if mistake.failure_mode == "forged_identity":
            _wrap_verified_token(service, persona=persona, spoofed_user_id=mistake.spoofed_user_id)
        elif mistake.failure_mode == "pooled_session_bleed":
            _wrap_leftover_session_state(service)
        return service


# S8: a per-process HMAC key for the arena's identity tokens (never stored or logged).
_ARENA_KID = "arena"
_ARENA_KEY = os.urandom(32)


def _refusal_telemetry(reason: str, *, verified: str, requested: str, latency_ms: float) -> Any:
    from adk_memshield import QueryTelemetry

    return QueryTelemetry(
        operation="search_memory",
        sql="-- refused before any SQL ran: the requested user_id does not match the verified token",
        session_context={
            "identity": f"REFUSED ({reason})",
            "verified_token_subject": verified,
            "requested_user_id": requested,
        },
        latency_ms=latency_ms,
        row_count=0,
        simulate_missing_app_filter=False,
        table_name="memories",
    )


def _wrap_verified_token(service: Any, *, persona: CallerPersona, spoofed_user_id: str | None) -> None:
    """S8: bind the caller's signed token; the memory tool passes the prompt's user id."""
    from adk_memshield import MemShieldIdentityError, bind_identity, issue_hmac_token

    orig = service.search_memory

    async def search_memory(*, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        from .unprotected_service import extract_spoofed_user_id

        requested = spoofed_user_id or extract_spoofed_user_id(query) or user_id
        token = issue_hmac_token(
            _ARENA_KEY,
            kid=_ARENA_KID,
            user_id=persona.user_id,
            tenant_id=persona.tenant_id,
            audience=app_name,
            clearance=persona.clearance,
            compartments=tuple(getattr(persona, "compartments", ()) or ()),
        )
        t0 = time.perf_counter()
        try:
            with bind_identity(token):
                return await orig(app_name=app_name, user_id=requested, query=query)
        except MemShieldIdentityError as exc:
            service.last_query_telemetry = _refusal_telemetry(
                str(exc), verified=persona.user_id, requested=requested,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
            service.identity_refused = True
            return SearchMemoryResponse(memories=[])

    service.search_memory = search_memory


def _wrap_leftover_session_state(service: Any) -> None:
    """S9: the previous request on memshield's pool SETs another caller at session level."""
    from .unprotected_service import S9_PREVIOUS_CALLER

    orig = service.search_memory
    pool = service.store._pool

    def _plant_and_probe() -> str:
        with pool.connection() as conn:  # previous request: session-level SET (no LOCAL)
            conn.execute("SELECT set_config('app.current_user_id', %s, false)", (S9_PREVIOUS_CALLER,))
        with pool.connection() as conn:  # what the next checkout inherits
            value = conn.execute("SELECT current_setting('app.current_user_id', true)").fetchone()[0]
        return str(value or "")

    async def search_memory(*, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        inherited = await asyncio.to_thread(_plant_and_probe)
        resp = await orig(app_name=app_name, user_id=user_id, query=query)
        tel = service.last_query_telemetry
        if tel is not None:
            tel.session_context["previous_request_set"] = f"app.current_user_id='{S9_PREVIOUS_CALLER}' (session level)"
            tel.session_context["next_checkout_saw"] = inherited or "(empty: reset on return)"
        return resp

    service.search_memory = search_memory


class MemShieldUnpinnedTarget(MemShieldTarget):
    """adk-memshield with plan pinning disabled: reproduces the 0.1.x recall collapse."""

    name = "memshield_unpinned"
    label = "adk-memshield, plan pinning off (0.1.x)"
    plan_cache_mode = "auto"


class MemShieldSuperuserTarget(MemShieldTarget):
    """adk-memshield on the superuser DSN: shows the posture check refusing to run."""

    name = "memshield_superuser"
    label = "adk-memshield on a superuser DSN"

    @property
    def dsn(self) -> str:
        return self.config.admin_dsn


# ----------------------------------------------------------------------------- Qdrant
_QDRANT_LOCK = threading.Lock()


class QdrantTarget(MemoryTarget):
    """Qdrant with payload filters; one collection per arena namespace."""

    name = "qdrant"
    label = "Qdrant (payload filters)"
    capabilities = Capabilities()

    def __init__(self, config: ArenaConfig, *, namespace: str = "") -> None:
        super().__init__(config, namespace=namespace)
        from qdrant_client import QdrantClient

        self.client = QdrantClient(url=config.qdrant_url, timeout=30)
        self.collection = f"arena_{namespace}" if namespace else "arena_default"
        self.last_filter: Any = None
        try:
            info = self.client.info()
            self.version = f"qdrant {info.version}"
        except Exception:
            self.version = "qdrant (unreachable)"

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        from .embedding import get_cached_embed_fn

        return get_cached_embed_fn(self.config.embedding_model, self.config.embedding_dim)(list(texts))

    def reset(self) -> None:
        from qdrant_client import models

        with _QDRANT_LOCK:
            if self.client.collection_exists(self.collection):
                self.client.delete_collection(self.collection)
            self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(size=self.config.embedding_dim, distance=models.Distance.COSINE),
            )
            for key, schema in (
                ("tenant_id", models.PayloadSchemaType.KEYWORD),
                ("user_id", models.PayloadSchemaType.KEYWORD),
                ("scope", models.PayloadSchemaType.KEYWORD),
                ("min_clearance", models.PayloadSchemaType.INTEGER),
                ("compartments", models.PayloadSchemaType.KEYWORD),
            ):
                self.client.create_payload_index(self.collection, key, schema)

    def drop(self) -> None:
        with _QDRANT_LOCK:
            if self.client.collection_exists(self.collection):
                self.client.delete_collection(self.collection)

    def seed(self, records: Sequence[SeedRecord], *, extra: dict[str, Any] | None = None) -> None:
        import uuid

        from qdrant_client import models

        vectors = self._embed([r.content for r in records])
        points = [
            models.PointStruct(id=str(uuid.uuid4()), vector=vec, payload=_seed_payload(rec, extra))
            for rec, vec in zip(records, vectors, strict=True)
        ]
        self.client.upsert(self.collection, points=points, wait=True)

    def delete_tagged(self, key: str, value: Any) -> int:
        from qdrant_client import models

        flt = models.Filter(must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))])
        before = self.client.count(self.collection, count_filter=flt, exact=True).count
        self.client.delete(self.collection, points_selector=models.FilterSelector(filter=flt), wait=True)
        return before

    def _filter(self, persona: CallerPersona, mistake: Mistake) -> Any:
        from qdrant_client import models

        def eq(key: str, value: Any) -> Any:
            return models.FieldCondition(key=key, match=models.MatchValue(value=value))

        mode = "strip_all_where_filters" if mistake.simulate_missing_app_filter else mistake.failure_mode
        spoofing = mode in ("param_spoof_user_id", "forged_identity") and mistake.spoofed_user_id
        user = mistake.spoofed_user_id if spoofing else persona.user_id
        tenant = eq("tenant_id", persona.tenant_id)
        compartments = list(getattr(persona, "compartments", ()) or ())
        comp_ok = models.Filter(
            should=[models.IsEmptyCondition(is_empty=models.PayloadField(key="compartments"))]
            + ([models.FieldCondition(key="compartments", match=models.MatchAny(any=compartments))] if compartments else [])
        )
        clearance = models.FieldCondition(key="min_clearance", range=models.Range(lte=persona.clearance))

        if mode == "strip_all_where_filters":
            return None
        if mode in ("shared_tenant_no_user_filter", "adversarial_planted_lure"):
            return models.Filter(must=[tenant])
        shared_parts: list[Any] = [eq("scope", "tenant_shared")]
        if mode != "swarm_missing_clearance_filter":
            shared_parts.append(clearance)
        if mode != "missing_compartment_filter":
            shared_parts.append(comp_ok)
        return models.Filter(
            must=[tenant],
            should=[eq("user_id", user), models.Filter(must=shared_parts)],
        )

    def search(self, *, persona, query, top_k, mistake):
        flt = self._filter(persona, mistake)
        self.last_filter = flt.model_dump(exclude_none=True) if flt is not None else None
        vector = self._embed([query])[0]
        t0 = time.perf_counter()
        result = self.client.query_points(
            self.collection,
            query=vector,
            query_filter=flt,
            limit=top_k,
            with_payload=True,
        )
        self.last_store_ms = (time.perf_counter() - t0) * 1000.0
        rows = []
        for point in result.points:
            row = dict(point.payload or {})
            row["id"] = str(point.id)
            row["similarity"] = float(point.score)
            rows.append(row)
        return rows


# ----------------------------------------------------------------------------- Mem0
class Mem0Target(MemoryTarget):
    """Mem0 OSS (raw adds, ``infer=False``) over Qdrant, one collection per namespace.

    Mem0 scopes memories by ``user_id`` and refuses a search without an entity id, so the
    "no filter" mistake is expressed as the wildcard ``user_id="*"``. Tenant, scope and
    clearance live in metadata; a correct app runs an own-rows search plus a shared-rows
    search and merges them. Mem0 has no compartment concept (S7 is n/a).
    """

    name = "mem0"
    label = "Mem0 OSS (user_id + metadata filters)"
    capabilities = Capabilities(compartments=False)

    def __init__(self, config: ArenaConfig, *, namespace: str = "") -> None:
        super().__init__(config, namespace=namespace)
        from importlib.metadata import version
        from urllib.parse import urlparse

        from mem0 import Memory

        url = urlparse(config.qdrant_url)
        self.collection = f"arena_mem0_{namespace}" if namespace else "arena_mem0_default"
        os.makedirs(config.state_dir, exist_ok=True)
        self._mem0_config = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": self.collection,
                    "host": url.hostname,
                    "port": url.port or 6333,
                    "embedding_model_dims": config.embedding_dim,
                },
            },
            "embedder": {"provider": "fastembed", "config": {"model": config.embedding_model}},
            # Never called: memories are added with infer=False (no LLM extraction).
            "llm": {"provider": "ollama", "config": {"model": "unused", "ollama_base_url": "http://127.0.0.1:9"}},
            "history_db_path": os.path.join(config.state_dir, f"mem0_history_{namespace or 'default'}.db"),
        }
        self._memory_cls = Memory
        self.memory = Memory.from_config(self._mem0_config)
        self.version = f"mem0ai {version('mem0ai')}"
        self.last_filter: Any = None

    def reset(self) -> None:
        # Mem0 has no tenant-wide reset: drop the collection and let Mem0 recreate it.
        self.drop()
        self.memory = self._memory_cls.from_config(self._mem0_config)

    def drop(self) -> None:
        from qdrant_client import QdrantClient

        client = QdrantClient(url=self.config.qdrant_url, timeout=30)
        if client.collection_exists(self.collection):
            client.delete_collection(self.collection)

    def seed(self, records: Sequence[SeedRecord], *, extra: dict[str, Any] | None = None) -> None:
        for rec in records:
            meta = _seed_payload(rec, extra)
            content = meta.pop("content")
            user = meta.pop("user_id")
            self.memory.add(content, user_id=user, metadata=meta, infer=False)

    def delete_tagged(self, key: str, value: Any) -> int:
        removed = 0
        for item in self.memory.get_all(filters={"user_id": "*", key: value}, top_k=10_000).get("results", []):
            self.memory.delete(item["id"])
            removed += 1
        return removed

    def _search(self, query: str, top_k: int, filters: dict[str, Any]) -> list[dict[str, Any]]:
        res = self.memory.search(query, top_k=top_k, filters=filters, threshold=0.0)
        rows = []
        for item in res.get("results", []):
            row = dict(item.get("metadata") or {})
            row["user_id"] = item.get("user_id")
            row["content"] = item.get("memory")
            row["id"] = item.get("id")
            row["similarity"] = float(item.get("score") or 0.0)
            rows.append(row)
        return rows

    def search(self, *, persona, query, top_k, mistake):
        mode = "strip_all_where_filters" if mistake.simulate_missing_app_filter else mistake.failure_mode
        tenant = persona.tenant_id
        spoofing = mode in ("param_spoof_user_id", "forged_identity") and mistake.spoofed_user_id
        user = mistake.spoofed_user_id if spoofing else persona.user_id
        if mode == "strip_all_where_filters":
            filters = [{"user_id": "*"}]
        elif mode in ("shared_tenant_no_user_filter", "adversarial_planted_lure"):
            filters = [{"user_id": "*", "tenant_id": tenant}]
        else:
            shared: dict[str, Any] = {"user_id": "*", "tenant_id": tenant, "scope": "tenant_shared"}
            if mode != "swarm_missing_clearance_filter":
                shared["min_clearance"] = {"lte": persona.clearance}
            filters = [{"user_id": user, "tenant_id": tenant}, shared]
        self.last_filter = filters
        merged: dict[str, dict[str, Any]] = {}
        for f in filters:
            for row in self._search(query, top_k, f):
                merged.setdefault(str(row["id"]), row)
        ranked = sorted(merged.values(), key=lambda r: r["similarity"], reverse=True)
        return ranked[:top_k]


TARGET_CLASSES: dict[str, type[MemoryTarget]] = {
    "unprotected": UnprotectedTarget,
    "memshield": MemShieldTarget,
    "memshield_unpinned": MemShieldUnpinnedTarget,
    "memshield_superuser": MemShieldSuperuserTarget,
    "qdrant": QdrantTarget,
    "mem0": Mem0Target,
}
DEFAULT_TARGETS: tuple[str, ...] = ("unprotected", "memshield")
SQL_TARGETS = frozenset({"unprotected", "memshield", "memshield_unpinned", "memshield_superuser"})


def parse_targets(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_TARGETS
    names = [v.strip() for v in (value.split(",") if isinstance(value, str) else value) if v.strip()]
    unknown = [n for n in names if n not in TARGET_CLASSES]
    if unknown:
        raise ValueError(f"unknown target(s) {unknown}; choose from {sorted(TARGET_CLASSES)}")
    if len(names) < 2:
        raise ValueError("pick at least two targets")
    return tuple(dict.fromkeys(names))


@dataclass
class TargetSet:
    """Instantiated targets for one run (shared by every query of the run)."""

    config: ArenaConfig
    names: tuple[str, ...]
    namespace: str = ""
    targets: dict[str, MemoryTarget] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in self.names:
            self.targets[name] = TARGET_CLASSES[name](self.config, namespace=self.namespace)

    def external(self) -> list[MemoryTarget]:
        return [t for n, t in self.targets.items() if n not in SQL_TARGETS]

    def close(self) -> None:
        for target in self.targets.values():
            try:
                target.close()
            except Exception:  # pragma: no cover - best effort
                pass
