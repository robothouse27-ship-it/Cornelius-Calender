"""Shared fixtures + helpers for the fetcher smoke tests.

We pin FAMILYCAL_TZ *before* importing fetcher so the module-level TZ is
deterministic regardless of the machine's environment, then expose a small
ICS-builder so individual tests can describe feeds inline (no network).
"""
import os
import textwrap

# Pin the timezone before fetcher reads it at import time.
os.environ.setdefault("FAMILYCAL_TZ", "America/Los_Angeles")

import fetcher  # noqa: E402  (import after env is set, on purpose)


def make_ics(*vevents):
    """Wrap one or more VEVENT bodies in a minimal valid VCALENDAR."""
    body = "\n".join(textwrap.dedent(v).strip() for v in vevents)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//cornelius-tests//EN\r\n"
        + body.replace("\n", "\r\n") + "\r\n"
        "END:VCALENDAR\r\n"
    )


def vevent(uid, dtstart, dtend=None, summary=None, rrule=None, value_date=False):
    """Build a single VEVENT body line-set. dtstart/dtend are raw ICS strings."""
    prop = ";VALUE=DATE" if value_date else ""
    lines = [f"BEGIN:VEVENT", f"UID:{uid}", f"DTSTART{prop}:{dtstart}"]
    if dtend is not None:
        lines.append(f"DTEND{prop}:{dtend}")
    if summary is not None:
        lines.append(f"SUMMARY:{summary}")
    if rrule is not None:
        lines.append(f"RRULE:{rrule}")
    lines.append("END:VEVENT")
    return "\n".join(lines)
