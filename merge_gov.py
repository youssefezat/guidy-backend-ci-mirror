"""Replaces the reconstructed routes in gtfs_data with a freshly built set.

IDEMPOTENT ON PURPOSE. gtfs_data already contains GOV_* rows from the last
merge, so appending again would duplicate every route -- and a duplicated
route is not a harmless extra row: it doubles the trips the router considers
and skews the cross-route medians the surveyed routes are corrected against.

So every run strips the existing reconstructed rows first, then appends the
new ones. Running it twice in a row leaves the feed identical, which is the
property that makes it safe to re-run after any change to reconstruct.py.

Surveyed data is never touched: rows are removed only when their agency_id,
route_id or trip_id is recognisably reconstructed.
"""

import csv
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "gtfs_data")
NEW = os.path.join(HERE, "gtfs_gov_new")
GOV_AGENCIES = {"GOV_CTA", "GOV_CTA_M", "GOV"}   # "GOV" = the first merge's id


def is_reconstructed(row):
    if row.get("agency_id") in GOV_AGENCIES:
        return True
    for key in ("route_id", "trip_id"):
        if str(row.get(key, "")).startswith("GOV_"):
            return True
    # translations.txt rows carry no id; they are matched by the value they
    # translate, so they are handled separately below.
    return False


def rewrite(name, new_rows_path, drop=is_reconstructed):
    dst = os.path.join(FEED, name)
    if not os.path.exists(dst):
        return 0, 0
    with open(dst, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        kept = [r for r in reader if not drop(r)]
    removed_marker = len(kept)

    added = []
    if new_rows_path and os.path.exists(new_rows_path):
        with open(new_rows_path, encoding="utf-8", newline="") as fh:
            added = list(csv.DictReader(fh))

    with open(dst, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in kept + added:
            w.writerow([r.get(h, "") for h in header])
    return removed_marker, len(added)


def main():
    if not os.path.isdir(NEW):
        sys.exit(f"no {NEW} -- run build_gov_gtfs.py first")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(HERE, f"gtfs_data.backup-{stamp}")
    # Two runs inside the same second would collide, and a merge that dies on
    # its backup step is a merge that has already half-run once.
    n = 1
    while os.path.exists(backup):
        backup = os.path.join(HERE, f"gtfs_data.backup-{stamp}-{n}")
        n += 1
    shutil.copytree(FEED, backup)
    print(f"backup: {os.path.basename(backup)}")

    # translations for reconstructed routes are identified by the value they
    # translate ("Minibus 37", "Kafr Tuhurmis - El-Nozha"), so the set of
    # values currently being added is what decides removal.
    new_tx = os.path.join(NEW, "translations.txt")
    gov_values = set()
    if os.path.exists(new_tx):
        for r in csv.DictReader(open(new_tx, encoding="utf-8")):
            gov_values.add((r["table_name"], r["field_name"], r["field_value"]))
    # ...plus anything a PREVIOUS run added, which is why the old file's rows
    # are matched on the same shape rather than on an id they do not have.
    def drop_tx(row):
        return (row.get("table_name"), row.get("field_name"),
                row.get("field_value")) in gov_values

    for name in ("agency.txt", "routes.txt", "trips.txt",
                 "stop_times.txt", "frequencies.txt"):
        kept, added = rewrite(name, os.path.join(NEW, name))
        print(f"  {name:18} kept {kept:6}  + added {added:5}")
    kept, added = rewrite("translations.txt", new_tx, drop=drop_tx)
    print(f"  {'translations.txt':18} kept {kept:6}  + added {added:5}")

    n = sum(1 for r in csv.DictReader(open(os.path.join(FEED, "routes.txt"), encoding="utf-8")))
    g = sum(1 for r in csv.DictReader(open(os.path.join(FEED, "routes.txt"), encoding="utf-8"))
            if str(r["route_id"]).startswith("GOV_"))
    print(f"\nfeed now: {n} routes, {g} reconstructed")


if __name__ == "__main__":
    main()
