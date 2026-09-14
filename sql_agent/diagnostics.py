"""Complete internal diagnostics with credential redaction, never prose clipping."""
from utils.logging import SensitiveDataFilter


def redact_diagnostic(value) -> str:
    text = str(value or "")
    for pattern, replacement in SensitiveDataFilter.PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in SensitiveDataFilter.literal_secrets():
        text = text.replace(secret, SensitiveDataFilter.REDACTED)
    return text
