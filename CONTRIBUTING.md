# Contributing to `zotero-paper-bridge`

Contributions are welcome—especially publisher metadata improvements, macOS/Linux client verification reports, and AI-agent integration guides!

## Ground Rules

1. **Zero third-party runtime dependencies for core**: Keep `paper_bridge/common`, `paper_bridge/server` (excluding `mcp_bridge.py`), and `paper_bridge/client` strictly on the Python 3.10+ standard library so researchers can run the client with zero `pip install` friction.
2. **Strict publisher compliance**: Never add CAPTCHA solvers, anti-bot evasion, TLS fingerprint spoofing, or unauthorized/pirated paper sources. When a publisher gates access, hand off cleanly to the user's real browser.
3. **Never hardcode personal paths or secrets**: Keep all scripts, templates, and tests completely generic (`$HOME`, dynamic SSH remote home lookup, temp directories in tests).

## Development Setup

```bash
git clone <repo-url>
cd zotero-paper-bridge
python3 -m pip install -e ".[dev]"
pytest -v
ruff check .
```

## Areas Where Help Is Especially Valuable

- **macOS & Linux desktop client testing**: Verify `scripts/client/start-paper-bridge.sh`, native notifications (`osascript` / `notify-send`), and Zotero 7 Connector write behavior on macOS and Linux desktops.
- **Additional metadata/OA resolvers**: Improve DOI extraction and bibliographic normalization for ACM, Springer, Elsevier, USENIX, and OpenReview.
- **Zotero 7 plugin/attachment improvements**: Cleanly support cross-session PDF attachment if Zotero 7 exposes a sanctioned local API for it in the future.
