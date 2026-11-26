# Security Policy

## Threat Model & Design Defenses

`zotero-paper-bridge` is engineered for **multi-user Linux research servers** running a local Zotero 7 library alongside AI coding assistants.

| Threat | Defense Built Into `zotero-paper-bridge` |
| :--- | :--- |
| **Other users on a shared Linux server calling your download helper** | Dual isolation: (1) Private Unix Domain Socket (`~/.local/run/paper-bridge/helper.sock`, `chmod 600` inside a `chmod 700` directory) with no TCP helper port exposed by default, AND (2) mandatory 256-bit Bearer Token (`chmod 600`) verified in constant time (`hmac.compare_digest`). |
| **Read-only MCP tool mutating or injecting items into Zotero** | Zotero read-back runs through a **private Unix Domain Socket** (`zotero-read.sock`, `chmod 600` inside `chmod 700` dir) served by a **read-only proxy** that allows only `GET`/`HEAD /api/*` and rejects all `POST`/`PUT`/`DELETE`/`PATCH` and `/connector/*` endpoints with `HTTP 403`. |
| **Arbitrary URL fetching or local SSRF via helper** | Remote downloads enforce an academic host allowlist (`ALLOWED_ACADEMIC_SUFFIXES`) on initial requests and every redirect hop, rejecting `localhost`, loopback IPs, `file://`, and non-HTTP(S) schemes. |
| **TLS downgrade or broken CA stores** | HTTPS requests always verify certificates strictly. When relocated Conda/Python builds lack a default CA store, `build_ssl_context()` falls back to standard OS trust bundles without relaxing verification. |
| **Leaking the master Bearer Token to browser checklists** | When TCP checklist pages are explicitly enabled, they use a separate per-batch ephemeral `local_nonce` and never render or transmit the master Bearer Token. |
| **Bypassing publisher security controls** | Strictly prohibited by design. Whenever a publisher requires institutional sign-in, SSO, or human verification (CAPTCHA / Cloudflare / Akamai), silent fetching halts immediately and marks the paper `needs_manual`. |

## Reporting a Vulnerability

Please do not open public GitHub issues for security vulnerabilities. Report them privately to the repository maintainers via GitHub Security Advisories (or private contact on the repository profile) with reproduction steps and affected versions.
