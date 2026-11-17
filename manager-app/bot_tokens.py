"""Used only by ../run_manager.cmd when the bot starts: prints <NAME>=<value> lines for the tokens stored in the
manager app (DPAPI-decrypted, so it works only on this PC under this Windows account). Prints nothing on any problem:
the bot then falls back to avito-bot/.env. Never run this by hand in a shared terminal: it prints secrets."""
import sys

try:
    from secrets_store import decrypted_tokens

    for name, value in decrypted_tokens().items():
        if "\n" not in value and "\r" not in value and '"' not in value:
            sys.stdout.write(f"{name}={value}\n")
except Exception:
    pass
