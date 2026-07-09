"""Human-facing booking reference codes.

Codes are formatted into a short customer-friendly string and backed by a
database uniqueness constraint on the booking table.
"""
import uuid


def next_reference_code() -> str:
    return f"CW-{uuid.uuid4().hex.upper()}"
