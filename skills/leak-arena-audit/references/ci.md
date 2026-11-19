# GitHub Actions CI Gate (`adk-leak-arena`)

Matches the workflow in `adk-leak-arena/.github/workflows/ci.yml`. Fails pull requests with exit code `1` on any protected-target leak or recall regression and exit code `2` on an insecure database role/posture.

```yaml
name: Memory Isolation Gate

on:
  push:
    branches: [main]
  pull_request:

jobs:
  leak-arena-gate:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres_admin_pass
          POSTGRES_DB: memshield
        ports:
          - 54330:5432
        options: >-
          --health-cmd "pg_isready -U postgres -d memshield"
          --health-interval 2s
          --health-timeout 3s
          --health-retries 20

    env:
      ARENA_DB_DSN: postgresql://memshield_app:memshield_pass@localhost:54330/memshield
      ARENA_ADMIN_DSN: postgresql://postgres:postgres_admin_pass@localhost:54330/memshield
      ARENA_STARTUP_BASELINE: "0"
      FASTEMBED_CACHE_PATH: /home/runner/.cache/fastembed

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install adk-leak-arena
        run: python -m pip install "adk-leak-arena>=0.2.1"

      - name: Seed arena schema
        run: leak-arena seed

      - name: Run default isolation gate (S1-S6 + Control)
        run: leak-arena eval --min-recall 1.0 --format markdown

      - name: Run v0.2 scenarios gate (S7-S9)
        run: leak-arena eval --scenarios S7,S8,S9 --targets unprotected,memshield --min-recall 1.0 --format markdown
```
