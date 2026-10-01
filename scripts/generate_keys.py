"""Generate secrets for .env.

    python scripts/generate_keys.py            # print values
    python scripts/generate_keys.py --init-env # create .env from .env.example with fresh secrets
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def new_values() -> dict[str, str]:
    alnum = string.ascii_letters + string.digits
    return {
        "APP_SECRET": secrets.token_urlsafe(48),
        "SESSION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "POSTGRES_PASSWORD": "".join(secrets.choice(alnum) for _ in range(32)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-env", action="store_true")
    args = parser.parse_args()
    values = new_values()
    if not args.init_env:
        for k, v in values.items():
            print(f"{k}={v}")
        return 0
    target, example = ROOT / ".env", ROOT / ".env.example"
    if target.exists():
        print(".env already exists - not touching it.", file=sys.stderr)
        return 1
    lines = []
    for line in example.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in values:
            line = f"{key}={values[key]}"
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)
    print("Created .env with fresh secrets. Back it up: losing SESSION_ENCRYPTION_KEY makes stored Telegram sessions unreadable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
