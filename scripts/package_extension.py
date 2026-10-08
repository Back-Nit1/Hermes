"""Deterministic extension zip: sorted entries, fixed timestamps/modes. Prints path and sha256.
Usage: python scripts/package_extension.py [outdir]"""

import hashlib
import json
import sys
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "extension"
SKIP_DIRS = {"tests"}
SKIP_FILES = {"package.json", "PLAYWRIGHT_CHECKLIST.md", "README.md", "icon-source.svg"}


def build(out_dir: Path) -> tuple[Path, str]:
    version = json.loads((SRC / "manifest.json").read_text())["version"]
    out = out_dir / f"hermes-extension-{version}.zip"
    files = sorted(
        p
        for p in SRC.rglob("*")
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(SRC).parts) and p.name not in SKIP_FILES
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            info = zipfile.ZipInfo(p.relative_to(SRC).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out_dir / f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n")
    return out, digest


if __name__ == "__main__":
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    path, digest = build(out_dir)
    print(path, digest)
