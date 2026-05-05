"""CLI: prompt for a password (twice) and print its bcrypt hash.

Usage:

    python -m wildwatch_server.hash_password

Paste the resulting hash into the server's `WILDWATCH_WEB_PASSWORD_HASH`
environment variable. Treat it like a credential -- the bcrypt cost is high
enough that the hash alone is hard to crack offline, but do not commit it.
"""

from __future__ import annotations

import getpass
import logging
import sys

# Silence passlib's noisy fallback log lines about the bcrypt 4.x API change
# (it tries the legacy `bcrypt.__about__` attribute first, fails, and falls
# back to `bcrypt.__version__`). Must run before importing passlib.
logging.getLogger("passlib").setLevel(logging.ERROR)

from wildwatch_server.auth_web import hash_password  # noqa: E402


def main() -> None:
    pw = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm:  ")
    if pw != confirm:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    if len(pw) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    print(hash_password(pw))


if __name__ == "__main__":
    main()
