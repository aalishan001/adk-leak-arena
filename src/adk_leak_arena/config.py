"""Environment and runtime configuration for adk-leak-arena."""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

# Gemini API model used by `--engine gemini_api` unless GEMINI_MODEL overrides it. A current
# generally available Flash model (gemini-2.5-flash, the 0.1.0 default, is retired).
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no", "")


def with_search_path(dsn: str, schema: str) -> str:
    """Return ``dsn`` with ``options=-csearch_path=<schema>,public`` (run-scoped tables)."""
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    parsed = urlparse(dsn)
    if not parsed.scheme.startswith("postgres"):
        raise ValueError("run namespaces need a postgresql:// URI DSN")
    pairs = parse_qsl(parsed.query)
    kept = [o for k, v in pairs if k == "options" for o in v.split() if not o.startswith("-csearch_path=")]
    query = [(k, v) for k, v in pairs if k != "options"]
    query.append(("options", " ".join([f"-csearch_path={schema},public", *kept])))
    return urlunparse(parsed._replace(query=urlencode(query, quote_via=quote)))


@dataclass
class ArenaConfig:
    """Runtime configuration for the ADK Leak Arena server and evaluator."""

    app_name: str = field(
        default_factory=lambda: os.environ.get("ARENA_APP_NAME", "acme_enterprise_copilot")
    )
    db_dsn: str = field(
        default_factory=lambda: os.environ.get(
            "ARENA_DB_DSN",
            os.environ.get(
                "MEMSHIELD_DB_DSN",
                "postgresql://memshield_app:memshield_pass@localhost:54330/memshield",
            ),
        )
    )
    # Schema owner, used for seeding, synthetic loads and run schemas. Public mode has no
    # default: the web process must not hold it (seed with `leak-arena seed` instead).
    admin_dsn: str = field(
        default_factory=lambda: os.environ.get(
            "ARENA_ADMIN_DSN",
            ""
            if _env_flag("ARENA_PUBLIC_MODE", "0")
            else "postgresql://postgres:postgres_admin_pass@localhost:54330/memshield",
        )
    )
    ollama_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "OLLAMA_BASE_URL", "http://host.docker.internal:11434"
        )
    )
    ollama_model: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_MODEL", "ollama_chat/gemma3:4b")
    )
    gemini_api_key: str = field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY", "")
    )
    gemini_model: str = field(
        default_factory=lambda: os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    )
    # Pacing and retry settings for `--engine gemini_api` (free-tier RPM headroom; 0 disables).
    gemini_rpm: float = field(
        default_factory=lambda: max(0.0, float(os.environ.get("ARENA_GEMINI_RPM", "11.5")))
    )
    gemini_max_retries: int = field(
        default_factory=lambda: max(0, int(os.environ.get("ARENA_GEMINI_MAX_RETRIES", "6")))
    )
    # Qdrant server for the `qdrant` and `mem0` targets.
    qdrant_url: str = field(
        default_factory=lambda: os.environ.get("ARENA_QDRANT_URL", "http://localhost:56333")
    )
    # Local state (Mem0's history database).
    state_dir: str = field(
        default_factory=lambda: os.environ.get(
            "ARENA_STATE_DIR", os.path.join(os.path.expanduser("~"), ".cache", "adk-leak-arena")
        )
    )
    # How many targets of one run execute concurrently.
    target_concurrency: int = field(
        default_factory=lambda: max(1, int(os.environ.get("ARENA_TARGET_CONCURRENCY", "4")))
    )
    # Run one deterministic reference benchmark at server startup so Run History is populated.
    startup_baseline: bool = field(
        default_factory=lambda: _env_flag("ARENA_STARTUP_BASELINE", "1")
    )
    # Read-only console for public deployments: inject, reset, rescale, benchmark jobs and
    # history deletion return 403 (and are hidden in the console); attacks still run.
    public_mode: bool = field(default_factory=lambda: _env_flag("ARENA_PUBLIC_MODE", "0"))
    # Optional generic pre_write hook (`module:attr`) passed to adk-memshield. Off by default;
    # refused when public_mode is enabled so a public service never loads external code.
    pre_write_hook: str = field(default_factory=lambda: os.environ.get("ARENA_PRE_WRITE_HOOK", "").strip())
    # Postgres schema holding this run's tables ("" = the default search_path).
    schema: str = ""
    top_k: int = 3
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    def for_schema(self, schema: str) -> ArenaConfig:
        """A copy of this config whose connections resolve tables in ``schema`` first."""
        return replace(
            self,
            schema=schema,
            db_dsn=with_search_path(self.db_dsn, schema),
            admin_dsn=with_search_path(self.admin_dsn, schema),
        )


def load_pre_write_hook(config: ArenaConfig) -> Any | None:
    """Resolve optional ``ARENA_PRE_WRITE_HOOK`` (``module:attr``); refused in public mode."""
    spec = (config.pre_write_hook or "").strip()
    if not spec:
        return None
    if config.public_mode:
        raise ValueError("ARENA_PRE_WRITE_HOOK is refused when ARENA_PUBLIC_MODE=1")
    if ":" not in spec:
        raise ValueError(f"ARENA_PRE_WRITE_HOOK must be 'module:attr', got {spec!r}")
    mod_name, attr_name = spec.split(":", 1)
    if not mod_name or not attr_name:
        raise ValueError(f"ARENA_PRE_WRITE_HOOK must be 'module:attr', got {spec!r}")
    module = importlib.import_module(mod_name)
    hook = getattr(module, attr_name)
    if not callable(hook):
        raise TypeError(f"ARENA_PRE_WRITE_HOOK {spec!r} is not callable")
    return hook
