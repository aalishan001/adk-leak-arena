# Deploying adk-leak-arena

The container image is published to GHCR by the release workflow for `linux/amd64` and `linux/arm64`:

```
ghcr.io/aalishan001/adk-leak-arena:0.2.0
ghcr.io/aalishan001/adk-leak-arena:latest
```

It runs `leak-arena serve` on port 8090 as an unprivileged user (uid 10001) and writes only under `/tmp`.

## What the web process needs

| Variable | Purpose |
| --- | --- |
| `ARENA_DB_DSN` / `MEMSHIELD_DB_DSN` | Application role (`memshield_app`: `NOSUPERUSER`, `NOBYPASSRLS`). Every measured read uses it. |
| `ARENA_ADMIN_DSN` | Schema owner. Used by `leak-arena seed`, synthetic stress loads and per-run schemas. |
| `ARENA_PUBLIC_MODE` | `1` = read-only console: attacks run; inject, reset, rescale and benchmark jobs return 403 and are hidden. Use it for any public deployment. |
| `ARENA_STARTUP_BASELINE` | `1` runs a baseline benchmark at startup to fill the history panel. |
| `ARENA_QDRANT_URL` | Optional: enables the `qdrant` and `mem0` targets. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Optional: the `gemini_api` engine (default model `gemini-3.8-flash`). |

Probes: `GET /healthz` (process up) and `GET /readyz` (the application DSN answers `SELECT 1`; 503 otherwise).

## One replica

Run history and the single benchmark slot live in the web process's memory. Two replicas would split them (a job started on one pod is invisible to the other, and both could rescale the same tables). The Deployment therefore runs **exactly one replica** with the `Recreate` strategy, so a rollout never overlaps two pods; Cloud Run gets `--max-instances=1` for the same reason. To serve more people, run more arenas, each against its own database. Benchmarks and `leak-arena eval` runs from CI do not need the web process at all: they create their own schema per run.

## Kubernetes (kustomize)

```
deploy/
  base/                 web Deployment + Service, seed Job, ConfigMap (public mode on)
  overlays/dev/         + PostgreSQL (pgvector) StatefulSet and Qdrant, public mode off
  overlays/production/  base only: points at managed PostgreSQL through a Secret
```

No secret is stored in the repository. The workloads read the Secret `adk-leak-arena-db` (keys `ARENA_DB_DSN`, `ARENA_ADMIN_DSN`) and, optionally, `adk-leak-arena-gemini` (`GEMINI_API_KEY`).

Development cluster:

```bash
cp deploy/overlays/dev/dev.env.example deploy/overlays/dev/dev.env   # gitignored; set your own passwords
kubectl apply -k deploy/overlays/dev
kubectl -n adk-leak-arena-dev wait --for=condition=complete job/adk-leak-arena-seed --timeout=10m
kubectl -n adk-leak-arena-dev port-forward svc/adk-leak-arena 8090:80
```

Production (managed PostgreSQL 16+ with the `vector` extension available):

```bash
kubectl create namespace adk-leak-arena
kubectl -n adk-leak-arena create secret generic adk-leak-arena-db \
  --from-literal=ARENA_DB_DSN='postgresql://memshield_app:<password>@<host>:5432/<db>' \
  --from-literal=ARENA_ADMIN_DSN='postgresql://<owner>:<password>@<host>:5432/<db>'
kubectl apply -k deploy/overlays/production
```

`leak-arena seed` (the Job) creates the `memshield_app` role if it does not exist, with the development password from `sql/init_arena.sql`. On any shared database create the role yourself first (`CREATE ROLE memshield_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS`) or rotate it right after seeding (`ALTER ROLE memshield_app PASSWORD '...'`). The admin DSN must not be a role the arena uses for reads: the posture probe and adk-memshield both refuse a superuser or `BYPASSRLS` application role.

Validation: both overlays build with kustomize v5.8.3 and pass `kubeconform -strict` against Kubernetes 1.33.0 (`benchmarks/runs/2026-11-16/kubeconform.txt`). They were not applied to a live cluster in this release.

## Cloud Run (documented, not executed)

Cloud Run pulls images from Artifact Registry, so mirror the GHCR image through an Artifact Registry remote repository (or push a copy) first. Then, with the DSNs stored in Secret Manager and a Cloud SQL for PostgreSQL instance that has `vector` enabled:

```bash
gcloud run deploy adk-leak-arena \
  --image=REGION-docker.pkg.dev/PROJECT/ghcr-remote/aalishan001/adk-leak-arena:0.2.0 \
  --region=REGION \
  --port=8090 --cpu=2 --memory=4Gi \
  --min-instances=0 --max-instances=1 --concurrency=20 \
  --add-cloudsql-instances=PROJECT:REGION:INSTANCE \
  --set-env-vars=ARENA_PUBLIC_MODE=1,ARENA_STARTUP_BASELINE=0,ARENA_STATE_DIR=/tmp/adk-leak-arena \
  --set-secrets=ARENA_DB_DSN=arena-db-dsn:latest,MEMSHIELD_DB_DSN=arena-db-dsn:latest,ARENA_ADMIN_DSN=arena-admin-dsn:latest \
  --allow-unauthenticated
```

With the Cloud SQL connector the DSN host is the socket directory, for example `postgresql://memshield_app:<password>@/<db>?host=/cloudsql/PROJECT:REGION:INSTANCE`. Seed once with a Cloud Run job running `leak-arena seed` and the same secrets.
