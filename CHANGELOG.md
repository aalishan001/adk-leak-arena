# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Development status is now `3 - Alpha`.
- Added the `Environment :: Web Environment` and `Topic :: Software Development :: Testing` classifiers and the `ai-agents`, `llm-evaluation`, `benchmark` and `adk` keywords.

## [0.1.0] - 2026-10-25

### Added
- Dual Google ADK `Runner` + `preload_memory` pipelines comparing an unprotected `pgvector` table with `adk-memshield` (PostgreSQL RLS) on the same seeded, canary-laced dataset.
- Scenarios S1–S5 (incidental overlap, adversarial top-K lure, tool-parameter spoofing, swarm clearance read-up, no-`WHERE` chaos mode), the S6 same-tier poisoning boundary, and a benign Control.
- Metrics: queries leaking, leaked row share, rank-1 hijack, canary exfiltration, same-tier poison retrieved, Authorized Recall@K, and p50/p95 SQL latency with RLS overhead.
- Offline web console with live SQL/RLS telemetry, stress dials (`top_k`, `seed_size`), an RLS write-policy sandbox, background benchmark jobs with abort, and run history.
- `leak-arena` CLI (`serve`, `eval`) with table, Markdown and JSON output.
- Arena schema bundled in the wheel for `pip install` usage.
