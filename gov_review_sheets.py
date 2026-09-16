"""Writes the two human-review sheets the import depends on.

Neither of these can be automated away, and pretending otherwise is how a
routing feed gets quietly wrong:

  gov_fuzzy_review.csv       -- matches difflib ACCEPTED. Every one of these
                                is already being treated as correct, so this
                                sheet exists to catch the ones that aren't.
                                Lowest scores first: that is where the
                                mistakes are.
  gov_unresolved_review.csv  -- landmarks nothing matched, most frequent
                                first, each with the three closest stops in
                                the feed as candidates. The top 50 rows
                                cover half of all unresolved occurrences and
                                the top 100 cover two-thirds, so this is an
                                afternoon's work, not an open-ended one.

Fill in the `stop_id` column (or write DROP for a road/area that is not a
boardable point) and the matcher picks it up on the next run.
"""

import csv
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gov_match import load_stops, build_index, normalise  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    stops = load_stops()
    # build_index gained a third return -- the parenthetical-alias index.
    _, normed, _alias = build_index(stops)
    keys = list(normed)

    def label(sid):
        s = stops[sid]
        return f"{s['name_ar'] or ''} / {s['name_en']}"

    data = json.load(open(os.path.join(HERE, "gov_match.json"), encoding="utf-8"))

    with open(os.path.join(HERE, "gov_fuzzy_review.csv"), "w",
              encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "gov_landmark", "matched_stop", "stop_id", "verdict(OK/DROP/stop_id)"])
        for m in data["fuzzy_review"]:
            w.writerow([m["score"], m["landmark"], m["matched_stop"], m["stop_id"], ""])

    with open(os.path.join(HERE, "gov_unresolved_review.csv"), "w",
              encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["occurrences", "gov_landmark", "candidate_1", "candidate_2",
                    "candidate_3", "stop_id (fill in, or DROP)"])
        for name, n in data["unresolved"]:
            near = difflib.get_close_matches(normalise(name), keys, n=3, cutoff=0.55)
            cands = [label(normed[k][0]) for k in near]
            cands += [""] * (3 - len(cands))
            w.writerow([n, name] + cands + [""])

    print("wrote gov_fuzzy_review.csv     ", len(data["fuzzy_review"]), "rows")
    print("wrote gov_unresolved_review.csv", len(data["unresolved"]), "rows")


if __name__ == "__main__":
    main()
