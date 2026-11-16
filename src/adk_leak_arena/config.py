"""Environment and runtime configuration for adk-leak-arena."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "options"]
    query.append(("options", f"-csearch_path={schema},public"))
    return urlunparse(parsed._replace(query=urlencode(query)))


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
    admin_dsn: str = field(
        default_factory=lambda: os.environ.get(
            "ARENA_ADMIN_DSN",
            "postgresql://postgres:postgres_admin_pass@localhost:54330/memshield",
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
    # Read-only console for public deployments: hides the write sandbox (/api/inject).
    public_mode: bool = field(default_factory=lambda: _env_flag("ARENA_PUBLIC_MODE", "0"))
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
