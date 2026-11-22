#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
OUT_ZIP="${DIST_DIR}/zotero-paper-bridge-client.zip"

mkdir -p "${DIST_DIR}"
rm -f "${OUT_ZIP}"

python3 - "${ROOT_DIR}" "${OUT_ZIP}" <<'PY'
import sys
import zipfile
from pathlib import Path

root = Path(sys.argv[1])
out_zip = Path(sys.argv[2])

include_paths = [
    "LICENSE",
    "pyproject.toml",
    "README.md",
    "README.zh-CN.md",
    "paper_bridge",
    "scripts/client",
]

with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for rel in include_paths:
        p = root / rel
        if p.is_file():
            zf.write(p, f"zotero-paper-bridge/{rel}")
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_dir() or "__pycache__" in child.parts or child.suffix == ".pyc":
                    continue
                arc = Path("zotero-paper-bridge") / child.relative_to(root)
                zf.write(child, str(arc))

print(f"Created portable client bundle: {out_zip} ({out_zip.stat().st_size} bytes)")
PY
