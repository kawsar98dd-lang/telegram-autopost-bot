"""Download HTMX into app/web/static/vendor/ and verify its SHA-384 checksum.

    python scripts/vendor_htmx.py [--if-missing]

The file is committed with the project, so the running application never loads
scripts from a CDN. The download is only accepted if it matches the checksum pinned
below (the integrity value published by the HTMX project for this version); on a
mismatch nothing is written.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "2.0.4"
URL = f"https://unpkg.com/htmx.org@{VERSION}/dist/htmx.min.js"
SHA384_B64 = "HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+"
TARGET = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "vendor" / "htmx.min.js"


def sha384_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--if-missing", action="store_true", help="do nothing when the file already exists")
    parser.add_argument("--verify", action="store_true", help="only check that the vendored file exists and matches the checksum")
    args = parser.parse_args()
    if args.verify:
        if not TARGET.exists():
            print(f"MISSING: {TARGET} - run: python scripts/vendor_htmx.py", file=sys.stderr)
            return 1
        if sha384_b64(TARGET.read_bytes()) != SHA384_B64:
            print(f"CHECKSUM MISMATCH: {TARGET}", file=sys.stderr)
            return 1
        print("htmx vendored and checksum verified.")
        return 0
    if args.if_missing and TARGET.exists():
        print("htmx already vendored.")
        return 0
    try:
        with urllib.request.urlopen(URL, timeout=30) as resp:  # noqa: S310 (fixed https URL)
            data = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"Could not download {URL}: {exc}\nNo file was written. Run this script where internet access is available.",
              file=sys.stderr)
        return 1
    actual = sha384_b64(data)
    if actual != SHA384_B64:
        print(f"Checksum mismatch for {URL}\n  expected {SHA384_B64}\n  got      {actual}\nNothing written.", file=sys.stderr)
        return 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(data)
    print(f"Wrote {TARGET} (htmx {VERSION}, {len(data)} bytes, sha384 verified).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
