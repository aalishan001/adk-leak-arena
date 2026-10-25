# Security Policy

## Intended use

adk-leak-arena is a **defensive evaluation tool**. It reproduces known multi-tenant memory-leak patterns against a synthetic, self-contained dataset so that teams can measure whether their Google ADK memory layer enforces tenant, user and clearance boundaries.

Use it only against databases and services you own or are authorized to test. The arena is not designed to attack third-party systems, and its "unprotected" target is intentionally vulnerable: never point it at production data or expose the console (`leak-arena serve`) to an untrusted network.

The bundled credentials (`memshield_app` / `memshield_pass`, `postgres` / `postgres_admin_pass`) are local development defaults. Change them in any shared environment.

## Supported versions

| Version | Supported |
| :--- | :---: |
| 0.1.x | Yes |

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub Security Advisories:

**https://github.com/aalishan001/adk-leak-arena/security/advisories/new**

Include the affected version, a description of the issue, and steps to reproduce. You should receive an acknowledgement within 7 days. Please do not disclose the issue publicly until a fix is released.

Vulnerabilities in the RLS policies or `MemShieldMemoryService` itself belong to the companion project: https://github.com/aalishan001/adk-memshield/security/advisories/new
