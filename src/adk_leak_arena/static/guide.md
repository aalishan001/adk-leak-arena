# MemShield suite, step 3: Architecture

Language models never connect to a database: they answer only from the text the application puts into the prompt. MemShield enforces multi-tenant and multi-agent memory isolation inside PostgreSQL with FORCE ROW LEVEL SECURITY, verified identity tokens, a pre-write Memory Guard and continuous canary monitoring.

## 1. Agent request flow
The agent (for example a Google ADK Runner) searches memory for the top-k facts by meaning, builds the prompt, sends it to the model, runs tools and writes new facts back to the vector store. Preventing leaks means enforcing authorization on the read (step 3) and on the write (step 7b). Whatever enters the prompt can surface in the model's output.

## 2. Failure points of a shared vector store (A to E)
Most multi-tenant AI applications keep every customer in one shared vector table and trust application code to add WHERE tenant_id = ? on every query. A single missed filter, spoofed user ID, privileged database role, raw SQL tool or poisoned write exposes data across tenants. Vector distance search has no notion of the caller's identity, so application-layer filtering must succeed on every path; a leak needs only one miss.

Documented failures the page cites:
- Asana MCP server (Jun 2025): a missing tenant boundary check on an AI connector lookup exposed tasks and projects across roughly 1,000 organizations (failure A).
- Supabase MCP with Cursor (Jul 2025): an agent holding a privileged service_role credential bypassed PostgreSQL RLS through prompt injection (failures C and D).
- AgentPoison (NeurIPS, Dec 2024): under 0.1% poisoned entries in agent memory reached at least 80% attack success with no retraining (failure E).

## 3. MemShield enforcement
Verified identity claims (tenant_id, user_id, clearance, compartments) from a signed JWT are bound into each PostgreSQL transaction with set_config(..., is_local => true), under a restricted memshield_app role (NOSUPERUSER, NOBYPASSRLS, not the table owner). Before writes land, the Memory Guard inspects candidate text with Gemini Flash-Lite under a tighten-only contract. LeakArena CI gates and the hourly MemShield Monitor canary and posture checks verify it continuously.

## 4. Silent row filtering on reads
On SELECT, PostgreSQL applies the RLS policy before distance ranking returns rows: unauthorized rows never reach the prompt, and there's no "access denied" to reveal they exist. On INSERT and UPDATE, a policy violation fails closed with a PostgreSQL error.

## 5. Silo vs pool, and isolation within a company
A database per customer (silo) multiplies cost across thousands of tenants. Even a company with its own database can't have one per employee or sub-agent, so clearance levels (1 to 5) and compartments are still needed inside the database.

## 6. Scope
Enforced today:
- Cross-tenant and cross-user isolation on SELECT, INSERT, UPDATE and DELETE through FORCE ROW LEVEL SECURITY
- No read-up, no write-up, no cross-compartment read, no low-clearance declassification on update
- Fail-closed pre-write screening (memshield-guard) that blocks prompt injections and down-labels overshared PII and credentials
- Connection-pool cleanup (RESET ALL), custom plan pinning (force_custom_plan) and refusal to start on admin or bypass roles

Outside the memory store, or on the roadmap:
- Token issuance: identity verification trusts your identity provider's signing keys
- Response caches, conversation logs, the ADK SessionService and direct company-database tools need their own controls
- Roadmap: a session write-down taint floor, tear lines and in-VPC Gemma screening

## Pages
- Step 1, Live Red-Team Console: / (markdown: /index.md)
- Step 2, Continuous Monitor and DPDP reports: https://memshield-monitor-268336845218.asia-southeast1.run.app/
- Step 3, this page: /guide

Source: https://github.com/aalishan001/adk-memshield and https://github.com/aalishan001/adk-leak-arena
