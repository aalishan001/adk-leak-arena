#!/bin/sh
# Re-runs the 0.2.0 numbers in BENCHMARKS.md from a checkout, in any environment that
# reaches the arena DB and Qdrant and has `pip install -e '.[dev,mem0]'`, for example:
#
#   docker run --rm --network adk-leak-arena_default -v "$PWD:/app" -w /app --user root \
#     -e ARENA_DB_DSN=postgresql://memshield_app:memshield_pass@db:5432/memshield \
#     -e ARENA_ADMIN_DSN=postgresql://postgres:postgres_admin_pass@db:5432/memshield \
#     -e ARENA_QDRANT_URL=http://qdrant:6333 adk-leak-arena-web \
#     sh -c "pip install -q --no-deps -e . && sh benchmarks/run_v020.sh benchmarks/runs/$(date +%F)"
#
# (the image does not contain benchmarks/, so mount the checkout). ONLY=<file> re-runs one output.
# Each output file starts with the command and the package versions it ran with.
set -u
OUT="${1:-benchmarks/runs/$(date +%F)}"
mkdir -p "$OUT"
export NO_COLOR=1

versions() {
  python - <<'PY'
import importlib.metadata as md, platform
for pkg in ("adk-leak-arena", "adk-memshield", "google-adk", "psycopg", "qdrant-client", "mem0ai", "fastembed"):
    try:
        print(f"# {pkg} {md.version(pkg)}")
    except md.PackageNotFoundError:
        print(f"# {pkg} not installed")
print(f"# python {platform.python_version()}")
PY
  python - <<'PY'
import psycopg
from adk_leak_arena.config import ArenaConfig
cfg = ArenaConfig()
with psycopg.connect(cfg.db_dsn) as c:
    print("#", c.execute("SELECT version()").fetchone()[0].split(",")[0])
    print("# pgvector", c.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0])
try:
    from qdrant_client import QdrantClient
    print("# qdrant server", QdrantClient(url=cfg.qdrant_url, timeout=5).info().version)
except Exception as exc:
    print("# qdrant server unreachable:", type(exc).__name__)
PY
}

run() {
  name="$1"; shift
  # ONLY=<file name> re-runs a single output.
  if [ -n "${ONLY:-}" ] && [ "$ONLY" != "$name" ]; then return 0; fi
  {
    echo "# command: $*"
    echo "# date: $(date -u +%FT%TZ)"
    versions
    echo
    "$@"
    status=$?
    echo
    echo "# exit code: $status"
  } > "$OUT/$name" 2>&1
  echo "$name: $(tail -1 "$OUT/$name")"
}

run eval-default.txt leak-arena eval
run eval-v020-scenarios-5-targets.txt leak-arena eval --scenarios S7,S8,S9 \
  --targets unprotected,memshield,memshield_superuser,qdrant,mem0
run eval-all-scenarios-5-targets.txt leak-arena eval --scenarios all \
  --targets unprotected,memshield,memshield_unpinned,qdrant,mem0
run eval-10k-hnsw-3-targets.txt leak-arena eval --seed-size 10000 --hnsw --top-k 10 --repeats 1 \
  --targets unprotected,memshield,qdrant
run eval-v020-scenarios-json.txt leak-arena eval --scenarios S7,S8,S9 --format json   --targets unprotected,memshield,memshield_superuser,qdrant,mem0
run eval-2000-stress.txt leak-arena eval --top-k 10 --seed-size 2000 --repeats 1
run explain-2000.txt python benchmarks/explain_plans.py
run explain-2000-analyze.txt python benchmarks/explain_plans.py --analyze
run pytest.txt python -m pytest -q -p no:cacheprovider
