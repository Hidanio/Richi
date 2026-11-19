"""Optimistic revision tokens for stored Richi records and user settings."""
from datetime import datetime, timedelta
import hashlib


def next_timestamp(current, previous):
    """Advance a UTC timestamp past the canonical revisions read under the write lock."""
    for stamp in previous:
        if not isinstance(stamp, str) or len(stamp) != 27 or stamp < current or not stamp.endswith("Z"):
            continue
        try:
            parsed = datetime.fromisoformat(stamp[:-1])
        except ValueError:
            continue
        # Opaque legacy versions and a hand-written terminal timestamp can be compared
        # for equality, but cannot serve as the starting point for this clock.
        if parsed != datetime.max and parsed.isoformat(timespec="microseconds") + "Z" == stamp:
            current = (parsed + timedelta(microseconds=1)).isoformat(timespec="microseconds") + "Z"
    return current


def recovery_version(data):
    """Tie removal of an unversioned record to the payload that was actually read."""
    return "invalid-" + hashlib.sha256(data).hexdigest()[:32]
