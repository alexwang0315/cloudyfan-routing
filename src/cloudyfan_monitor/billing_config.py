"""Load only explicitly selected read-only OpenRouter billing credentials."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex


KEYS = ("OPENROUTER_BILLING_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_API_KEY")


class BillingConfigurationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BillingCredentials:
    api_key: str = field(repr=False)
    management_key: str | None = field(default=None, repr=False)


def load_billing_credentials(*, key_file=None, environ=None):
    """An explicit file is authoritative; otherwise read just three env names.

    Values are literal shlex tokens. No shell, substitutions, other service
    settings, implicit .env search or file/environment account mixing occurs.
    """
    if key_file is None:
        environment = os.environ if environ is None else environ
        values = {name: environment.get(name) for name in KEYS}
    else:
        try:
            path = Path(key_file)
            if not path.is_file():
                raise OSError
            with path.open(encoding="utf-8") as stream:
                text = stream.read(65537)
        except (OSError, TypeError, UnicodeError):
            raise BillingConfigurationError("billing_key_file_unreadable") from None
        if len(text) > 65536:
            raise BillingConfigurationError("billing_key_file_too_large")
        values = {}
        for line in text.splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?(" + "|".join(KEYS) + r")\s*=(.*)", line)
            if match is None:
                continue
            name, raw = match.groups()
            if name in values:
                raise BillingConfigurationError("billing_key_duplicate")
            try:
                parts = shlex.split(raw, comments=True, posix=True)
            except ValueError:
                raise BillingConfigurationError("billing_key_file_invalid") from None
            if len(parts) > 1:
                raise BillingConfigurationError("billing_key_file_invalid")
            values[name] = parts[0] if parts else ""
    api_key = values.get(KEYS[0]) or values.get(KEYS[1])
    if not api_key:
        raise BillingConfigurationError("billing_key_missing")
    management_key = values.get(KEYS[2]) or None
    for value in (api_key, management_key):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 4096
                                 or any(not 33 <= ord(c) <= 126 for c in value)):
            raise BillingConfigurationError("billing_key_invalid")
    return BillingCredentials(api_key, management_key)
