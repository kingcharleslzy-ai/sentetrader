from __future__ import annotations

from getpass import getpass

from trader_runtime.auth import hash_owner_password


def main() -> int:
    password = getpass("AI Trader owner password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("passwords_do_not_match")
    print(hash_owner_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
