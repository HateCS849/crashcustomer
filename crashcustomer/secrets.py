"""Ephemeral test credential redaction; never persist authorization headers."""


def scrub(value, secrets=()):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {k: "[redacted]" if str(k).lower() in {"authorization", "cookie", "set-cookie", "access_token", "refresh_token", "password"}
                else scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, secrets) for v in value]
    return value
