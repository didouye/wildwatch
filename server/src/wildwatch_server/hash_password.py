"""CLI: prompt for a password (twice) and print its bcrypt hash.

Usage:

    python -m wildwatch_server.hash_password

Paste the resulting hash into the server's `WILDWATCH_WEB_PASSWORD_HASH`
environment variable. Treat it like a credential -- the bcrypt cost is high
enough that the hash alone is hard to crack offline, but do not commit it.
"""

from __future__ import annotations

import getpass
import sys

from wildwatch_server.auth_web import hash_password


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
