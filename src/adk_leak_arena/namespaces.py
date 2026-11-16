"""Per-run isolation: every benchmark run gets its own namespace.

A run namespace is a PostgreSQL schema (``arena_run_<id>``) holding the run's own
``memories`` and ``memories_unprotected`` tables, reached through a ``search_path``
connection option, plus per-run Qdrant collections for the non-SQL targets. Concurrent
runs (a CLI ``eval`` while the server runs a job, or two server jobs) therefore never see
each other's rows or Scenario 6 fixtures. Everything is dropped when the run ends.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Iterator, Sequence

import psycopg

from .config import ArenaConfig
from .dataset import SEED_RECORDS
from .targets import TargetSet

logger = logging.getLogger(__name__)


def _close_pools(dsn: str) -> None:
    """Close adk-memshield's shared pool for a run DSN (the schema is about to be dropped)."""
    try:
        from adk_memshield import core

        pool = core._SHARED_POOLS.pop(dsn, None)
        if pool is not None:
            pool.close()
    except Exception:  # pragma: no cover - best effort
        logger.debug("could not close pool for run DSN", exc_info=True)


def create_run_schema(config: ArenaConfig) -> str:
    schema = f"arena_run_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(config.admin_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO memshield_app")
    return schema


def drop_run_schema(config: ArenaConfig, schema: str) -> None:
    with psycopg.connect(config.admin_dsn, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@contextlib.contextmanager
def run_namespace(
    config: ArenaConfig, target_names: Sequence[str], *, isolated: bool = True
) -> Iterator[tuple[ArenaConfig, TargetSet]]:
    """Create, seed and finally drop a run namespace; yields (run config, targets).

    With ``isolated=False`` the run uses the shared default tables (the 0.1.0 behaviour of
    the interactive console) and only the non-SQL targets get per-run collections.
    """
    from .runner_pair import ensure_schema_and_seed

    schema = create_run_schema(config) if isolated else ""
    run_cfg = config.for_schema(schema) if schema else config
    tset: TargetSet | None = None
    try:
        ensure_schema_and_seed(run_cfg)
        tset = TargetSet(run_cfg, tuple(target_names), namespace=schema or f"shared_{uuid.uuid4().hex[:8]}")
        for target in tset.external():
            target.reset()
            target.seed(SEED_RECORDS)
        yield run_cfg, tset
    finally:
        if tset is not None:
            for target in tset.external():
                with contextlib.suppress(Exception):
                    target.drop()
            tset.close()
        if schema:
            _close_pools(run_cfg.db_dsn)
            _close_pools(run_cfg.admin_dsn)
            from .scale import forget_namespace

            forget_namespace(run_cfg)
            drop_run_schema(config, schema)
