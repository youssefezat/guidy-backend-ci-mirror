"""Matches Cairo Governorate corridor landmarks to GTFS stops.

The governorate publishes each route as a dash-separated list of landmarks
in its own wording -- no stop ids, no coordinates, no order guarantees
beyond the obvious one. Before any of that can become GTFS, every landmark
has to be resolved to a stop this feed already has (or recognised as not
being a stop at all).

Three passes, deliberately in this order, each stricter about what it will
accept than the next is:

  1. exact      -- the landmark IS an Arabic stop name in translations.txt.
  2. normalised -- same after stripping diacritics, unifying alef/ya/ta
                   marbuta, and removing the descriptive prefixes Arabic
                   place names carry inconsistently (شارع, ميدان, كوبري...).
                   "تحرير" and "ميدان التحرير" are the same place; the
                   governorate writes one and the feed the other.
  3. fuzzy      -- difflib ratio over the normalised forms, accepted only
                   above FUZZY_MIN. This pass is the one that can be WRONG,
                   so its results are written out for review with their
                   score rather than being silently trusted.

Anything left is either a road (الأوتوستراد, كورنيش النيل -- named to
describe a corridor, never a stop) or a genuinely missing place. Roads are
dropped: inventing a stop called "the ring road" would put a boardable
point in the middle of a motorway.
"""

import collections
import csv
import difflib
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(HERE, "gtfs_data")

FUZZY_MIN = 0.86

# Landmarks that describe a road or an area rather than a point. A route
# "goes along" these; you cannot board at them. Matching them to the one
# stop that happens to share the name would put a stop in the wrong place
# and, worse, make the router think two distant routes meet there.
ROAD_WORDS = (
    "الاوتوستراد", "الأوتوستراد", "كورنيش", "الكورنيش", "الطريق الدائري",
    "الدائري", "المحور", "محور", "صلاح سالم", "طريق النصر", "الطريق",
    "كوبري اكتوبر", "كوبرى اكتوبر", "النفق", "نفق",
)

_DIAC = re.compile(r"[ً-ْـ]")
_PREFIX = re.compile(
    # (?:ال)? -- the descriptive prefix word itself sometimes carries the
    # definite article ("الشارع" as well as "شارع"), not just the name
    # after it. Previously only "حي"/"الحي" and "جراج"/"الجراج" were
    # special-cased for this; the other prefix words silently normalised
    # differently depending on which form the governorate happened to
    # write, which is exactly the inconsistency this function exists to
    # remove. Making the article optional on the whole group fixes all of
    # them at once and makes the two old special cases redundant.
    r"^(?:ال)?(شارع|ش|ميدان|م|كوبري|كوبرى|محور|طريق|منطقة|حي|مساكن|مدينة|جراج)\s+"
)


def normalise(s):
    s = unicodedata.normalize("NFKC", s)
    s = _DIAC.sub("", s)
    s = s.translate(str.maketrans("أإآٱ", "اااا"))
    s = s.replace("ى", "ي").replace("ة", "ه")
    s = re.sub(r"[()\[\]«».,،؛/\\]", " ", s)
    s = " ".join(s.split())
    # Twice: "ش ميدان التحرير" carries two of them.
    for _ in range(2):
        s = _PREFIX.sub("", s)
    # The definite article is written inconsistently on both sides.
    s = re.sub(r"^ال", "", s)
    return s.replace(" ", "")


def load_stops():
    """stop_id -> (english_name, arabic_name or None, lat, lon)."""
    stops = {}
    for r in csv.DictReader(open(os.path.join(GTFS, "stops.txt"), encoding="utf-8")):
        stops[r["stop_id"]] = {
            "name_en": r["stop_name"],
            "name_ar": None,
            "lat": float(r["stop_lat"]),
            "lon": float(r["stop_lon"]),
        }
    # translations.txt keys on the English VALUE, not on stop_id, so the
    # join goes through the name.
    by_en = collections.defaultdict(list)
    for sid, s in stops.items():
        by_en[s["name_en"]].append(sid)
    for t in csv.DictReader(open(os.path.join(GTFS, "translations.txt"), encoding="utf-8")):
        if t.get("table_name") == "stops" and t.get("field_name") == "stop_name":
            for sid in by_en.get(t.get("field_value", ""), ()):
                stops[sid]["name_ar"] = t.get("translation", "").strip() or None
    return stops


def _paren_names(name):
    """The text inside a stop name's parentheses, when it is a NAME rather
    than a qualifier.

    This feed uses "A (B)" for two different jobs. Sometimes B disambiguates
    ("الحي الثامن (مدينة نصر)" -- the 8th District in Nasr City, as opposed to
    elsewhere). Sometimes B is an alternative name for the same place
    ("طريق العروبة (شارع الثورة)" -- one road, two names people use).

    Only the second kind should be searchable as a name of its own, and the
    two are told apart by a simple, checkable rule: a qualifier names a
    district, so it appears as a bare stop name elsewhere in the feed; an
    alternative name does not. Matches found this way rank BELOW a direct
    match, so a real stop always wins over an alias.
    """
    return re.findall(r"[\(（]([^)）]+)[\)）]", name or "")


def _variants(name):
    """A stop name, plus the forms the governorate is likely to write it in.

    This feed disambiguates stops with a parenthetical -- "الحي الثامن
    (مدينة نصر)", "عتبة (الجراج)" -- because two places share a name. The
    governorate writes the bare name. Without the stripped variant, every
    disambiguated stop in the feed is unreachable from these documents,
    and those are disproportionately the BIG interchanges, which is the
    worst possible set to miss.
    """
    out = {name}
    bare = re.sub(r"\s*[\(（][^)）]*[\)）]\s*", " ", name).strip()
    if bare:
        out.add(bare)
    return out


def build_index(stops):
    exact = collections.defaultdict(list)
    normed = collections.defaultdict(list)
    bare_names = set()
    for sid, s in stops.items():
        for name in (s["name_ar"], s["name_en"]):
            if not name:
                continue
            if "(" not in name and "（" not in name:
                bare_names.add(normalise(name))
            for v in _variants(name):
                exact[v].append(sid)
                normed[normalise(v)].append(sid)

    # Second-class index: parenthetical ALIASES only -- see _paren_names.
    # A parenthetical that also exists as a bare stop name somewhere is a
    # district qualifier, not an alias, and is skipped.
    paren = collections.defaultdict(list)
    for sid, s in stops.items():
        for name in (s["name_ar"], s["name_en"]):
            for p in _paren_names(name):
                key = normalise(p)
                if len(key) >= 4 and key not in bare_names and key not in normed:
                    paren[key].append(sid)
    return exact, normed, paren


def is_road(landmark):
    n = normalise(landmark)
    return any(normalise(w) == n or normalise(w) in n for w in ROAD_WORDS)


# Containment matching -- the pass that finds a landmark sitting INSIDE a
# longer stop name. This feed names a stop by its junction ("طريق العروبة
# (شارع الثورة)") while the governorate names the street ("شارع الثورة"), so
# whole-name comparison never sees them as the same place. 1 750 was invisible
# to the matcher for exactly this reason, and with it route 37 could not reach
# the rider who reported it.
#
# The obvious danger is short strings matching half the city, so:
#   MIN  a landmark under this many characters is not distinctive enough to
#        match on containment at all (نزهة would hit every Nozha in Cairo).
#   RATIO the landmark has to be a real part of the name it matched, not an
#        incidental fragment of a much longer one.
# Ties break toward the CLOSEST-LENGTH name, i.e. the most specific stop that
# contains the landmark rather than the longest, ramblingest one.
CONTAINS_MIN_CHARS = 5
CONTAINS_MIN_RATIO = 0.45


def contains_match(n, normed, keys):
    if len(n) < CONTAINS_MIN_CHARS:
        return None
    best = None
    for key in keys:
        if n == key:
            continue
        if n in key:
            ratio = len(n) / len(key)
        elif key in n and len(key) >= CONTAINS_MIN_CHARS:
            ratio = len(key) / len(n)
        else:
            continue
        if ratio < CONTAINS_MIN_RATIO:
            continue
        score = abs(len(key) - len(n))
        if best is None or score < best[0]:
            best = (score, normed[key][0], key)
    return (best[1], best[2]) if best else None


def match_all(gov_path):
    stops = load_stops()
    exact, normed, paren = build_index(stops)
    normed_keys = list(normed)

    docs = json.load(open(gov_path, encoding="utf-8"))["documents"]
    results = {}
    tally = collections.Counter()
    fuzzy_review = []
    unresolved = collections.Counter()

    for tag in ("nakl_gama3y", "al_nakl_al3am"):
        out = []
        for rec in docs[tag]:
            landmarks = [p.strip() for p in re.split(r"[–\-]", rec["corridor"]) if p.strip()]
            resolved = []
            for lm in landmarks:
                if is_road(lm):
                    tally["road"] += 1
                    resolved.append({"landmark": lm, "stop_id": None, "how": "road"})
                    continue
                if lm in exact:
                    tally["exact"] += 1
                    # EVERY candidate is kept, not just the first. A landmark
                    # naming a long street ("فيصل") matches many stops along
                    # it, and picking one arbitrarily drags the corridor to a
                    # random point on that street -- observed on route 37,
                    # where it sent the path 6 km west and straight back.
                    # Which candidate is right depends on the rest of the
                    # corridor, so the choice is deferred to reconstruct.py.
                    resolved.append({"landmark": lm, "stop_id": exact[lm][0],
                                     "candidates": exact[lm][:12], "how": "exact"})
                    continue
                n = normalise(lm)
                if n in normed:
                    tally["normalised"] += 1
                    resolved.append({"landmark": lm, "stop_id": normed[n][0],
                                     "candidates": normed[n][:12], "how": "normalised"})
                    continue
                if n in paren:
                    tally["alias"] += 1
                    resolved.append({"landmark": lm, "stop_id": paren[n][0],
                                     "candidates": paren[n][:12], "how": "alias"})
                    continue
                hit = contains_match(n, normed, normed_keys)
                if hit:
                    sid, key = hit
                    tally["contains"] += 1
                    resolved.append({"landmark": lm, "stop_id": sid,
                                     "candidates": normed[key][:12], "how": "contains"})
                    continue
                near = difflib.get_close_matches(n, normed_keys, n=1, cutoff=FUZZY_MIN)
                if near:
                    sid = normed[near[0]][0]
                    score = difflib.SequenceMatcher(None, n, near[0]).ratio()
                    tally["fuzzy"] += 1
                    fuzzy_review.append({
                        "landmark": lm, "matched_stop": stops[sid]["name_ar"] or stops[sid]["name_en"],
                        "stop_id": sid, "score": round(score, 3),
                    })
                    resolved.append({"landmark": lm, "stop_id": sid, "candidates": normed[near[0]][:12],
                                     "how": "fuzzy", "score": round(score, 3)})
                    continue
                tally["unresolved"] += 1
                unresolved[lm] += 1
                resolved.append({"landmark": lm, "stop_id": None, "how": "unresolved"})

            usable = [r for r in resolved if r["stop_id"]]
            out.append({
                "route": rec["route"], "route_num": rec["route_num"],
                "fare_egp": rec.get("fare_egp"), "corridor": rec["corridor"],
                "landmarks": resolved,
                "n_landmarks": len(landmarks), "n_resolved": len(usable),
            })
        results[tag] = out

    return results, tally, fuzzy_review, unresolved, stops


if __name__ == "__main__":
    results, tally, fuzzy, unresolved, stops = match_all(os.path.join(HERE, "gov_routes_2026-09.json"))
    total = sum(tally.values())
    print(f"landmarks: {total}")
    for k in ("exact", "normalised", "alias", "contains", "fuzzy", "road", "unresolved"):
        print(f"  {k:12} {tally[k]:5}  {tally[k]*100//max(total,1):3}%")

    print("\nroutes by how much of the corridor resolved:")
    buckets = collections.Counter()
    for tag, recs in results.items():
        for r in recs:
            frac = r["n_resolved"] / max(r["n_landmarks"], 1)
            buckets[">=80%" if frac >= .8 else ">=60%" if frac >= .6 else
                    ">=40%" if frac >= .4 else "<40%"] += 1
    for k in (">=80%", ">=60%", ">=40%", "<40%"):
        print(f"  {k:8} {buckets[k]:4} routes")

    json.dump({"routes": results,
               "fuzzy_review": sorted(fuzzy, key=lambda x: x["score"]),
               "unresolved": unresolved.most_common()},
              open(os.path.join(HERE, "gov_match.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nfuzzy matches needing review: {len(fuzzy)}")
    print(f"distinct unresolved landmarks: {len(unresolved)}")
    print("wrote gov_match.json")
