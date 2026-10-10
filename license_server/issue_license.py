"""Seller tool: issue an OFFLINE signed license file for one customer. Run on the seller's own machine only.

    python license_server/issue_license.py --private-key-file ~/secrets/license_private.key \\
        --customer-ref "order-1042" --license-type standard --perpetual --out ~/licenses/order-1042.license.json
    python license_server/issue_license.py ... --expires 2027-12-31 ...        (time-limited instead of --perpetual)

* The private key is read from a FILE (never from the command line, an environment variable or a prompt echo) and is
  never printed, logged or put into an error message.
* Everything about the license is supplied deliberately: exactly one of --perpetual / --expires is required.
* The key file must be owner-only and outside the project; the output must also be outside the project (it contains a
  customer reference) and must not exist yet.
* The issued file is verified again with the public key before it is reported as written.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from safety import PROJECT_ROOT, check_private_key_file, inside_project  # noqa: E402
from signing import build_offline_payload, load_private_key, new_license_id, sign_payload  # noqa: E402

LICENSE_TYPES = ("standard", "commercial", "trial")
_REF = re.compile(r"^[^\x00-\x1f\x7f]{1,100}$")
_HOST = re.compile(r"^[a-z0-9.\-]+(:\d{1,5})?$")


def _fail(msg: str) -> int:
    print(f"Error: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None, *, now: int | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--private-key-file", required=True, help="path of the private key file (outside the project, chmod 600)")
    ap.add_argument("--customer-ref", required=True, help="your reference for the buyer, e.g. an order number (shown in the app)")
    ap.add_argument("--license-type", required=True, choices=LICENSE_TYPES)
    when = ap.add_mutually_exclusive_group(required=True)
    when.add_argument("--perpetual", action="store_true", help="the license never expires")
    when.add_argument("--expires", help="last valid day, YYYY-MM-DD (UTC, end of that day)")
    ap.add_argument("--host", default="", help="OPTIONAL: bind to the public address of the installation, e.g. poster.example.com")
    ap.add_argument("--product", default="telegram-auto-poster")
    ap.add_argument("--out", required=True, help="license file to create (outside the project, must not exist)")
    ap.add_argument("--also-base64", action="store_true", help="also write <out>.b64 (one line, for LICENSE_FILE_CONTENT)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation question")
    args = ap.parse_args(argv)

    key_path, out = Path(args.private_key_file).expanduser(), Path(args.out).expanduser()
    problem = check_private_key_file(key_path)
    if problem:
        return _fail(problem)
    if inside_project(out):
        return _fail("the output file must be outside the project folder")
    if out.exists():
        return _fail(f"refusing to overwrite {out}")
    if not _REF.match(args.customer_ref):
        return _fail("--customer-ref must be 1-100 printable characters")
    host = args.host.strip().lower()
    if host and not _HOST.match(host):
        return _fail("--host must look like poster.example.com (no scheme, no path)")
    issued = int(now if now is not None else time.time())
    expires_at = None
    if args.expires:
        try:
            day = datetime.strptime(args.expires, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            return _fail("--expires must be a date like 2027-12-31")
        expires_at = int(day.timestamp())
        if expires_at <= issued:
            return _fail("--expires is in the past")

    license_id = new_license_id()
    print(f"License ID : {license_id}\nProduct    : {args.product}\nCustomer   : {args.customer_ref}\nType       : {args.license_type}\n"
          f"Validity   : {'PERPETUAL' if expires_at is None else 'until ' + args.expires + ' (UTC)'}\n"
          f"Bound host : {host or '(not bound)'}\nOutput     : {out}")
    if not args.yes and input(f"Type the license ID ({license_id}) to confirm: ").strip() != license_id:
        return _fail("not confirmed; nothing was written")

    try:
        private = key_path.read_text(encoding="ascii").strip()
        load_private_key(private)  # validates the key without ever showing it
        doc = sign_payload(private, build_offline_payload(
            license_id=license_id, product=args.product, customer_ref=args.customer_ref, license_type=args.license_type,
            issued_at=issued, expires_at=expires_at, host=host))
    except Exception:  # noqa: BLE001 - deliberately generic: error text must never contain key material
        return _fail("the private key file is not a valid signing key")
    import json

    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    try:  # self-check with the PUBLIC key, using the very code the customer application runs
        from cryptography.hazmat.primitives import serialization
        public = base64.urlsafe_b64encode(load_private_key(private).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
        sys.path.insert(0, str(PROJECT_ROOT))
        from app.licensing.offline import verify_license_text
        verify_license_text(text, public_key=public, product=args.product, host=host or "any.example", now=issued)
    except Exception:  # noqa: BLE001
        return _fail("the issued license failed its own verification; nothing was written")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)  # not secret; the container user must be able to read it
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    if args.also_base64:
        out.with_name(out.name + ".b64").write_text(base64.b64encode(text.encode()).decode() + "\n", encoding="ascii")
    print(f"License written to {out}. Send this file to the customer; keep your private key offline and backed up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
