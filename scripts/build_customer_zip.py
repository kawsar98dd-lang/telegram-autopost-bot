"""Seller tool: build the CUSTOMER source ZIP from this project and refuse to build anything unsafe.

    python scripts/build_customer_zip.py --out ../release [--private-key-file ~/secrets/license_private.key]

Included: the application, migrations, deployment files (Docker, optional Caddy HTTPS) and customer documentation (an explicit list below).
Never included: license_server/ (seller tools), docs/SELLER_GUIDE.md, tests/, CI files, test-deployment helpers, secrets of any kind.
The build FAILS if: a forbidden file would be packed, a secret pattern or the seller's private key text appears in any packed
file, packed code imports an excluded module, or the build has no valid license public key (override only with
--allow-empty-public-key for non-release test builds). Writes <zip> and <zip>.sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "app" / "__init__.py"

INCLUDE_DIRS = ("app", "migrations", "docs")
INCLUDE_FILES = (".env.example", ".dockerignore", ".gitignore", "CHANGELOG.md", "Dockerfile", "README.md", "docker-compose.yml",
                 "requirements.txt", "docker-compose.https.yml", "deploy/Caddyfile", "scripts/generate_keys.py", "scripts/vendor_htmx.py")
# Inside included directories, these are seller-only / test-only and are left out:
EXCLUDE_PATHS = ("docs/STEP6_GITHUB_ANDROID_GUIDE_BN.md", "docs/SELLER_GUIDE.md", "docs/STEP3_REAL_TELEGRAM_VERIFICATION.md",
                 "docs/STEP3_REAL_TELEGRAM_VERIFICATION_RENDER.md", "docs/STEP3_REAL_TELEGRAM_VERIFICATION_ANDROID_CLOUD.md")
FORBIDDEN_NAME = re.compile(r"(^|/)(\.env(\..*)?|.*\.(session|session-journal|key|pem|db|sqlite3?|pyc|log)|license\.json|"
                            r".*\.license\.json(\.b64)?|__pycache__|\.git|test-report\.json)$")
ALLOWED_NAME = {".env.example"}
SECRET_PATTERNS = (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"), re.compile(rb"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"))
EXCLUDED_IMPORT = re.compile(rb"^\s*(from|import)\s+(license_server|tests)\b", re.M)
PUBLIC_KEY = re.compile(r'^LICENSE_PUBLIC_KEY\s*=\s*"([A-Za-z0-9_-]{43})"', re.M)


class ReleaseError(Exception):
    pass


def collect(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_DIRS:
        files += [p for p in sorted((root / name).rglob("*")) if p.is_file()]
    files += [root / n for n in INCLUDE_FILES]
    out = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        if rel in EXCLUDE_PATHS:
            continue
        if not p.is_file():
            raise ReleaseError(f"expected file is missing: {rel}")
        if rel not in ALLOWED_NAME and FORBIDDEN_NAME.search(rel):
            if "__pycache__" in rel or rel.endswith(".pyc"):
                continue  # generated caches are skipped, not an error
            raise ReleaseError(f"forbidden file would be packed: {rel}")
        out.append(p)
    return sorted(set(out))


def check_contents(root: Path, files: list[Path], private_key_text: str = "") -> None:
    for p in files:
        data, rel = p.read_bytes(), p.relative_to(root).as_posix()
        for pat in SECRET_PATTERNS:
            if pat.search(data):
                raise ReleaseError(f"secret-looking content in {rel}")
        if private_key_text and private_key_text.encode() in data:
            raise ReleaseError(f"the private signing key text appears in {rel}")
        if p.suffix == ".py" and EXCLUDED_IMPORT.search(data):
            raise ReleaseError(f"{rel} imports an excluded module (license_server / tests)")


def build(root: Path, out_dir: Path, *, private_key_file: Path | None = None, allow_empty_public_key: bool = False) -> tuple[Path, str]:
    consts = (root / "app" / "licensing" / "constants.py").read_text(encoding="utf-8")
    if not PUBLIC_KEY.search(consts) and not allow_empty_public_key:
        raise ReleaseError("app/licensing/constants.py has no LICENSE_PUBLIC_KEY: run license_server/keygen.py --write-constants first")
    secret = ""
    if private_key_file is not None:
        secret = private_key_file.expanduser().read_text(encoding="ascii").strip()
        if not secret:
            raise ReleaseError("the private key file is empty")
    files = collect(root)
    check_contents(root, files, secret)
    version = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "app" / "__init__.py").read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"telegram-auto-poster-customer-{version.group(1) if version else 'dev'}.zip"
    if target.exists():
        raise ReleaseError(f"refusing to overwrite {target}")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            info = zipfile.ZipInfo("telegram-auto-poster/" + p.relative_to(root).as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, p.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    return target, digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--private-key-file", help="optional: refuse to build if this key's text appears in any packed file")
    ap.add_argument("--allow-empty-public-key", action="store_true", help="NON-release test builds only")
    args = ap.parse_args(argv)
    try:
        target, digest = build(ROOT, Path(args.out).expanduser(),
                               private_key_file=Path(args.private_key_file) if args.private_key_file else None,
                               allow_empty_public_key=args.allow_empty_public_key)
    except ReleaseError as exc:
        print(f"RELEASE REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"Built {target}\nSHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
