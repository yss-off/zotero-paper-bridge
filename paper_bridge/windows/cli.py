"""Backward-compatible CLI entrypoint for `python -m paper_bridge.windows.cli`."""

from ..client.cli import build_parser, main

__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
