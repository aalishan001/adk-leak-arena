# Deploying adk-leak-arena

The container image is published to GHCR by the release workflow for `linux/amd64` and `linux/arm64`:

```
ghcr.io/aalishan001/adk-leak-arena:0.2.1
ghcr.io/aalishan001/adk-leak-arena:latest
```

It runs `leak-arena serve` on port 8090 as an unprivileged user (uid 10001) and writes only under `/tmp`.

## What the web process needs

| Variable | Purpose |
| --- | --- |
| `ARENA_DB_DSN` / `MEMSHIELD_DB_DSN` | Application role (`memshield_app`: `NOSUPERUSER`, `NOBYPASSRLS`). Every measured read uses it. |
| `ARENA_ADMIN_DSN` | Schema owner. Used by `leak-arena seed`, synthetic stress loads and per-run schemas. **Only the seed job gets it in a public deployment:** with `ARENA_PUBLIC_MODE=1` the web process refuses to start if it is set. |
| `ARENA_PUBLIC_MODE` | `1` = read-only console: attacks run (deterministic engine, PostgreSQL targets, no fixture-planting S6/S7); inject, reset, rescale, benchmark jobs and history deletion return 403 and are hidden. Use it for any public deployment. |
| `ARENA_DB_CONNECT_WAIT_S` | Seconds to retry transient DB connection errors during cold-start wake-up (default `0`; set to `60` on Cloud Run + scale-to-zero AlloyDB Express so the first cold request wakes the database instead of returning 500/503 while remaining fail-closed). |
| `ARENA_STARTUP_BASELINE` | `1` runs a baseline benchmark at startup to fill the history panel. |
| `ARENA_QDRANT_URL` | Qdrant endpoint for the `qdrant` and `mem0` targets (default `http://localhost:56333`; set in the dev overlay). Unused in public mode, which offers only the PostgreSQL targets. |
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

No secret is stored in the repository. The workloads read the Secret `adk-leak-arena-db` (keys `ARENA_DB_DSN`, `ARENA_ADMIN_DSN`) and, optionally, `adk-leak-arena-gemini` (`GEMINI_API_KEY`). In the base (public mode on), the web Deployment receives only `ARENA_DB_DSN`; `ARENA_ADMIN_DSN` goes to the seed Job alone. In public mode the web process also refuses an application role that is a superuser, has `BYPASSRLS` or owns an arena table, never seeds, and reports not-ready until the seed Job has run. The dev overlay turns public mode off and gives the web Deployment the admin DSN back (inject, reset and benchmark jobs need it).

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

`leak-arena seed` (the Job) creates the `memshield_app` role if it does not exist, with the development password from `sql/init_arena.sql`. On any shared database create the role yourself first (`CREATE ROLE memshield_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS`) or rotate it right after seeding (`ALTER ROLE memshield_app PASSWORD '...'`). The application DSN must not be a superuser or `BYPASSRLS` role: adk-memshield refuses to run on one (the arena shows `refused (config)`), and the arena's posture probe reports it in the telemetry.

Validation: both overlays build with kustomize v5.8.3 and pass `kubeconform -strict` against Kubernetes 1.33.0 (`benchmarks/runs/2026-11-16/kubeconform.txt`). They were not applied to a live cluster in this release.

## Cloud Run (documented, not executed)

Cloud Run pulls images from Artifact Registry, so mirror the GHCR image through an Artifact Registry remote repository (or push a copy) first. Then, with the DSNs stored in Secret Manager and a Cloud SQL for PostgreSQL instance that has `vector` enabled:

```bash
gcloud run deploy adk-leak-arena \
  --image=REGION-docker.pkg.dev/PROJECT/ghcr-remote/aalishan001/adk-leak-arena:0.2.1 \
  --region=REGION \
  --port=8090 --cpu=2 --memory=4Gi \
  --min-instances=0 --max-instances=1 --concurrency=20 \
  --add-cloudsql-instances=PROJECT:REGION:INSTANCE \
  --set-env-vars=ARENA_PUBLIC_MODE=1,ARENA_STARTUP_BASELINE=0,ARENA_STATE_DIR=/tmp/adk-leak-arena \
  --set-secrets=ARENA_DB_DSN=arena-db-dsn:latest,MEMSHIELD_DB_DSN=arena-db-dsn:latest \
  --allow-unauthenticated
```

With the Cloud SQL connector the DSN host is the socket directory, for example `postgresql://memshield_app:<password>@/<db>?host=/cloudsql/PROJECT:REGION:INSTANCE`. The service gets only the application DSN. Seed once, before the service becomes ready, with a Cloud Run job that alone holds the admin DSN:

```bash
gcloud run jobs create adk-leak-arena-seed \
  --image=REGION-docker.pkg.dev/PROJECT/ghcr-remote/aalishan001/adk-leak-arena:0.2.1 \
  --region=REGION --command=leak-arena --args=seed \
  --add-cloudsql-instances=PROJECT:REGION:INSTANCE \
  --set-secrets=ARENA_DB_DSN=arena-db-dsn:latest,ARENA_ADMIN_DSN=arena-admin-dsn:latest
gcloud run jobs execute adk-leak-arena-seed --region=REGION --wait
```

## AlloyDB Express (pre-build risk check, 2026-11-19)

Checked against `https://docs.cloud.google.com/alloydb/docs/express-overview` and linked AlloyDB docs before provisioning:

| # | Check | Status | Findings and doc reference |
| :--- | :--- | :---: | :--- |
| 1 | Free tier, pricing, scale-to-zero, regions | **Yes** | Free tier: 50 compute units/month + 2 GB storage (first 12 months per billing account), pay-as-you-go above that. Auto-scales to zero compute ($0.00/hr) after 15 minutes idle and resumes on new connections (~20 s). One-click upgrade to production AlloyDB supported. Preview regions: `asia-southeast1` (Singapore, nearest to Bengaluru; `asia-south1` is not in the Express preview list), `asia-east1`, `asia-east2`, `asia-northeast1`, `australia-southeast1`, `europe-west2`, `us-west1`. Ref: `https://docs.cloud.google.com/alloydb/docs/express-overview` |
| 2 | PostgreSQL version | **Yes** | Defaults to PostgreSQL 18. Ref: `https://docs.cloud.google.com/alloydb/docs/express-overview` |
| 3 | Custom roles + `FORCE ROW LEVEL SECURITY` + `NOBYPASSRLS` | **Yes (verify live)** | `postgres` belongs to `alloydbsuperuser` (managed equivalent, not true `SUPERUSER`). Standard SQL `CREATE ROLE memshield_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`, table ownership by the admin/owner role, `ALTER TABLE ... FORCE ROW LEVEL SECURITY`, and policies are supported; never create `memshield_app` with `alloydbsuperuser` membership. Verify `rolsuper=f`, `rolbypassrls=f`, non-membership in `alloydbsuperuser`, and `relforcerowsecurity=t` live after creation. Ref: `https://cloud.google.com/alloydb/docs/manage-users-built-in` |
| 4 | Extensions (`vector`, `alloydb_scann`, `google_ml_integration` / `ai.if()`) | **Yes (verify versions live)** | Express includes AlloyDB AI extensions: `vector` (0.8+ with `hnsw.iterative_scan`), `alloydb_scann` (Google ScaNN index), and `google_ml_integration` exposing `ai.if(prompt TEXT, model_id VARCHAR(100) DEFAULT NULL) RETURNS bool`. Exact `extversion` and Vertex AI permissions for `ai.if()` on Express will be verified live on the instance. Ref: `https://cloud.google.com/alloydb/docs/ai/work-with-embeddings`, `https://cloud.google.com/alloydb/docs/ai/evaluate-semantic-conditions` |
| 5 | Cloud Run connectivity | **Yes** | Express does not expose raw public IP allowlists; external connectivity uses Public DNS (or Private Service Connect) authenticated via IAM (`roles/alloydb.client`) and mTLS 1.3 through the AlloyDB Auth Proxy (`gcr.io/alloydb-connectors/alloydb-auth-proxy`). Preferred setup: Auth Proxy sidecar in a Cloud Run multi-container YAML so the app keeps a plain `127.0.0.1` DSN with no `0.0.0.0/0` network exposure. Ref: `https://cloud.google.com/alloydb/docs/auth-proxy/connect` |
| 6 | Laptop connectivity for tests | **Yes** | Run `alloydb-auth-proxy` locally using Application Default Credentials on a checked free port (never `5432`, e.g. `15432`). Ref: `https://cloud.google.com/alloydb/docs/auth-proxy/connect` |

## Google Cloud: Cloud Run + AlloyDB Express (verified 2026-11-19; cold-start hardened 2026-11-20)

Live public read-only console: **`https://adk-leak-arena-268336845218.asia-southeast1.run.app`** (`asia-southeast1`, Singapore — prototype on synthetic data because AlloyDB Express is not yet available in Mumbai; production target: Mumbai (`asia-south1`) for India DPDP data residency).

### 1. Provisioning (executed via Google Cloud Data Agent Kit AlloyDB MCP and `gcloud`)

- **Project:** `memshield-buildathon` (with a ₹1,000 billing budget alert at 25% / 50% / 90% / 100%).
- **AlloyDB Express cluster & primary instance:** `projects/memshield-buildathon/locations/asia-southeast1/clusters/memshield-express/instances/primary` — PostgreSQL 18.4, 1 vCPU / 4 GB, `expressConfig.enabled=true`, `publicDnsEnabled=true`, `alloydb.iam_authentication=on` (provisioned in < 2 seconds).
- **Extensions verified live:** `vector 0.8.7.google-1`, `alloydb_scann 0.1.4`, `google_ml_integration 1.6.5`.
- **Least-privilege application role (created via SQL, never `gcloud ... --superuser`):**
  ```sql
  CREATE DATABASE memshield;
  \c memshield
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE EXTENSION IF NOT EXISTS alloydb_scann CASCADE;
  CREATE EXTENSION IF NOT EXISTS google_ml_integration;
  CREATE ROLE memshield_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '<generated>';
  GRANT CONNECT ON DATABASE memshield TO memshield_app;
  GRANT USAGE ON SCHEMA public TO memshield_app;
  ```
- **Secrets (piped via stdin, never echoed or committed):**
  - `arena-db-dsn`: `postgresql://memshield_app:<pw>@127.0.0.1:5432/memshield`
  - `arena-admin-dsn`: `postgresql://postgres:<pw>@127.0.0.1:5432/memshield`
- **Dedicated Cloud Run service account:** `adk-leak-arena-sa@memshield-buildathon.iam.gserviceaccount.com` granted `roles/alloydb.client` and `roles/secretmanager.secretAccessor` **only** on `arena-db-dsn` (never `arena-admin-dsn`).

### 2. Sidecar deployment (`deploy/cloudrun/service.yaml`)

The multi-container Cloud Run YAML in [`deploy/cloudrun/service.yaml`](cloudrun/service.yaml) runs:
- `arena` (port `8090`, 2 vCPU / 4 GiB, `ARENA_PUBLIC_MODE=1`, `ARENA_STARTUP_BASELINE=0`, `ARENA_DB_CONNECT_WAIT_S=60`, `minScale=0`, `maxScale=1`, `startup-cpu-boost=true`, image pushed to Artifact Registry `asia-southeast1-docker.pkg.dev/memshield-buildathon/memshield-repo/adk-leak-arena:0.2.1-q0`);
- `alloydb-proxy` (`gcr.io/alloydb-connectors/alloydb-auth-proxy:1.18.0`, `--address=127.0.0.1 --port=5432 --public-ip --health-check --http-port=9090`), with `container-dependencies` ensuring `arena` waits for the Auth Proxy startup probe (`periodSeconds: 2, timeoutSeconds: 1, initialDelaySeconds: 0` on both containers).
- **Cold-start resilience (fail-closed):** when `ARENA_DB_CONNECT_WAIT_S > 0` in public mode, `arena` still rejects `ARENA_ADMIN_DSN` and `ARENA_PRE_WRITE_HOOK` synchronously at startup, then runs a fast DB probe and defers retry-backed posture verification (`connect_with_retry`, up to 60 s) to the first request touching the database (`/readyz`, `/api/state`, `/api/attack`). No data or state is ever served until `check_public_posture` passes; this keeps `minScale=0` without tripping Cloud Run's cold-start queue or failing when AlloyDB Express resumes from scale-to-zero.

```bash
gcloud run services replace deploy/cloudrun/service.yaml \
  --region=asia-southeast1 --project=memshield-buildathon
gcloud run services add-iam-policy-binding adk-leak-arena \
  --region=asia-southeast1 --member=allUsers --role=roles/run.invoker \
  --project=memshield-buildathon
```

### 3. Live verification & posture evidence

- Posture & refusal log: [`benchmarks/runs/2026-11-19/alloydb-posture.txt`](../benchmarks/runs/2026-11-19/alloydb-posture.txt) — `memshield_app` has `rolsuper=False`, `rolbypassrls=False`, not in `alloydbsuperuser` or `postgres`, owns no tables; `memories` has `relrowsecurity=True, relforcerowsecurity=True`; `inspect_rls_posture.ok=True`; public mode refuses startup when given `ARENA_ADMIN_DSN` or `postgres`.
- Live Cloud Run verification: [`benchmarks/runs/2026-11-19/cloudrun-verify.txt`](../benchmarks/runs/2026-11-19/cloudrun-verify.txt) — `GET /readyz` returns `200` (`{"status":"ready","public_mode":true}`), S5 returns `LEAK` (3/3) vs `ISOLATED` (0/3, `rls_effective=True`), and `POST /api/reset` returns `403`.
- Cold-start acceptance test after 21.5 min completely idle (`minScale=0`, AlloyDB Express auto-suspended): [`benchmarks/runs/2026-11-20/cloudrun-coldstart.txt`](../benchmarks/runs/2026-11-20/cloudrun-coldstart.txt) — **5/5 HTTP 200, zero 500/503s**:
  1. `GET /` (cold container + HTML): `HTTP 200` in **12.93 s** (83,429 bytes)
  2. `GET /api/state` (cold DB posture verification): `HTTP 200` in **243.2 ms** (`rls_effective=True, role=memshield_app`)
  3. `POST /api/attack` S5 (first cold S5 click): `HTTP 200` in **4.70 s** (`unprotected` leak 3/3, `sql=24.6 ms` | `memshield` isolated 0/3, `sql=79.6 ms`)
  4. `GET /readyz`: `HTTP 200` in **396.8 ms**
  5. `POST /api/attack` S5 (warm repeat): `HTTP 200` in **207.8 ms** (`unprotected` leak 3/3, `sql=6.5 ms` | `memshield` isolated 0/3, `sql=10.3 ms`)

### 4. Estimated cost and teardown

- **Idle monthly cost:** ₹0/month (AlloyDB Express scales to zero after 15 min idle with 50 free CUs/mo + 2 GB free storage; Cloud Run `minScale=0`; 2 Secret Manager versions within free tier; Artifact Registry ~0.45 GB within 0.5 GB free tier).
- **Active demo cost:** within free tier for the 2-day event (list price above free tier: ~₹24/demo hour for Cloud Run 3 vCPU total + 4.5 GiB).
- **Teardown commands:**
  ```bash
  gcloud run services delete adk-leak-arena --region=asia-southeast1 --project=memshield-buildathon --quiet
  gcloud beta alloydb instances delete primary --cluster=memshield-express --region=asia-southeast1 --project=memshield-buildathon --quiet
  gcloud beta alloydb clusters delete memshield-express --region=asia-southeast1 --project=memshield-buildathon --force --quiet
  ```
