#!/usr/bin/env python3
"""
Build the .rvpkg (a plain zip of PACKAGE + the Python mode) into dist/.
Works the same on Linux, macOS and Windows - no `zip` command needed.

    python build.py
"""
import pathlib
import re
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"

package_text = (SRC / "PACKAGE").read_text()
version = re.search(r"^version:\s*([\w.]+)", package_text, re.M).group(1)

DIST.mkdir(exist_ok=True)
out = DIST / f"annotated_dailies_export-{version}.rvpkg"

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("PACKAGE", "annotated_dailies_export.py"):
        z.write(SRC / name, arcname=name)  # files must sit at the zip root

print(f"Built {out}")
