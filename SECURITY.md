# Security Policy

## Threat Model & Design Defenses

`zotero-paper-bridge` is explicitly engineered for **shared multi-user Linux research servers** (such as university GPU clusters) connecting back to a researcher's personal desktop running Zotero 7.

| Threat | Defense Built Into `zotero-paper-bridge` |
| :--- | :--- |
| **Remote shell exposure on personal PC** | No SSH server (`sshd`) is installed or exposed on the client. Only narrow HTTP endpoints bound strictly to `127.0.0.1` are forwarded via `ssh -R`. |
| **Other users on the shared Linux server calling your download helper** | Dual isolation: (1) Reverse-forwarded Unix Domain Socket inside `~/.local/run/paper-bridge/` (`chmod 700`), AND (2) mandatory 256-bit Bearer Token (`chmod 600` on Linux, current-user-only NTFS ACL on Windows) verified in constant time (`hmac.compare_digest`). |
| **Other users reading or injecting items into Zotero port `23119`** | Zotero read-back runs through a **private Unix Domain Socket** (`zotero-read.sock`, `chmod 700` dir) connected to a **read-only proxy** on the client (`127.0.0.1:23120`) that allows only `GET`/`HEAD /api/*` and rejects all `POST`/`PUT`/`DELETE`/`PATCH` and `/connector/*` endpoints with `HTTP 403`. |
| **Arbitrary URL fetching or local SSRF via helper** | Remote downloads enforce an academic host allowlist (`ALLOWED_ACADEMIC_SUFFIXES`) and reject `localhost`, loopback IPs, `file://`, and non-HTTP(S) schemes. |
| **Leaking the master Bearer Token to the local browser** | The local manual-review HTML checklist uses a separate per-batch ephemeral `local_nonce` and never renders or transmits the master Bearer Token. |
| **Bypassing publisher security controls** | Strictly prohibited by design. Whenever a publisher requires institutional sign-in, SSO, or human verification (CAPTCHA / Cloudflare / Akamai), silent fetching halts immediately and hands off to the user's real browser. |

## Reporting a Vulnerability

Please do not open public GitHub issues for security vulnerabilities. Report them privately to the repository maintainers via GitHub Security Advisories (or private contact on the repository profile) with reproduction steps and affected versions.
