"""Records the trips this network could not serve.

WHY THIS FILE EXISTS

Until now a failed route calculation produced nothing but an error screen.
The rider learned the app couldn't help; we learned nothing at all. That is
backwards for a feed which is a known-partial extract -- 502 of the CTA
minibus numbers between 1 and 606 are missing, and OpenStreetMap has one
bus relation for all of Greater Cairo (checked 2026-09-09), so there is no
external source to fill the gaps from. The only way the coverage improves
is if we find out where it fails, and the riders hitting the gaps are the
ones who know.

Two kinds of event are recorded, and both are coverage gaps:

  no_route  -- no itinerary at all, complete or partial.
  partial   -- an itinerary that gets close but leaves an uncovered tail.
               These matter just as much: an uncovered tail is a missing
               route with a known start and a known end, which is the most
               actionable shape a gap can have.

JSONL, appended, one event per line. Deliberately not a database: this has
to be readable by a person with a text editor and greppable on a laptop,
and it must never be able to take the API down -- every failure in here is
swallowed, because a rider's request must not fail because telemetry did.

No user id, no session id, no device id. Coordinates a rider typed and the
time they typed them, nothing that ties two requests to the same person.
"""

import datetime
import json
import os
import threading

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coverage_gaps.jsonl")

# Appends from multiple request threads. A single lock is enough here --
# the write is a few hundred bytes and never blocks on anything slow.
_lock = threading.Lock()

# A rider retrying the same impossible trip five times is one gap, not
# five. Rounding to ~100 m collapses GPS jitter without merging genuinely
# different origins.
_COORD_DP = 3


def record(kind, start_lat, start_lon, end_lat, end_lon, *, reason=None,
           uncovered_m=None, last_covered_stop=None, lang=None):
    """Append one coverage-gap event. Never raises."""
    try:
        event = {
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "start": [round(start_lat, _COORD_DP), round(start_lon, _COORD_DP)],
            "end": [round(end_lat, _COORD_DP), round(end_lon, _COORD_DP)],
        }
        if reason:
            event["reason"] = reason
        if uncovered_m is not None:
            event["uncovered_m"] = uncovered_m
        if last_covered_stop:
            event["last_covered_stop"] = last_covered_stop
        if lang:
            event["lang"] = lang

        line = json.dumps(event, ensure_ascii=False) + "\n"
        with _lock:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        # Telemetry is never load-bearing. A full disk, a locked file or a
        # permissions problem must not turn a working route request into a
        # 500.
        pass
