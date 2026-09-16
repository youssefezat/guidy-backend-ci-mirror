"""
Pull rail / monorail / tram / ferry transit out of the Egypt OSM extract.

WHY THIS EXISTS AND NOT AN OVERPASS FETCH
-----------------------------------------
overpass-api.de, openstreetmap.org and gitlab.com are all unreachable from
both the cloud sandbox and the desktop bridge's Linux VM. But the OSRM
routing data the backend already ships includes the full Geofabrik Egypt
extract (osrm_data/egypt-latest.osm.pbf). That file contains every tag
Overpass would have returned, so the network round trip is unnecessary --
the data was already on disk.

WHAT IT COLLECTS
----------------
1. Every `type=route` relation whose `route` is one of the rail-ish modes.
   Guidy's GTFS feed already covers bus/minibus/microbus and the 3 metro
   lines; the gap is monorail, LRT, tram, mainline rail and ferries.
2. Every node member of those relations, so stations arrive with real
   coordinates rather than hand-typed ones.
3. Every independently station-tagged node in the Greater Cairo bbox, so a
   station that exists in OSM but hasn't been added to a route relation yet
   is still visible. A brand-new line (the East Nile monorail opened to
   passengers in May 2026) is exactly the case where relation membership
   lags the station nodes.

Arabic names are kept throughout -- half the value of OSM here is name:ar,
which the app needs for its Arabic locale.
"""

import json
import sys
from collections import Counter

import osmium

PBF = sys.argv[1] if len(sys.argv) > 1 else "egypt-latest.osm.pbf"
OUT = sys.argv[2] if len(sys.argv) > 2 else "osm_transit_raw.json"

# The modes Guidy's feed is missing. 'subway' is included deliberately:
# the existing metro rows come from the Transport for Cairo feed, and
# having OSM's version alongside lets the two be cross-checked.
WANT_ROUTES = {"monorail", "light_rail", "tram", "train", "subway", "ferry"}

# Greater Cairo, matching the bbox fetch_osm_routes.ps1 already uses.
S, W, N, E = 29.70, 30.80, 30.35, 31.80

STATION_NODE = (
    ("railway", {"station", "halt", "tram_stop", "stop", "subway_entrance"}),
    ("public_transport", {"station", "stop_position", "platform"}),
    ("amenity", {"ferry_terminal"}),
)


def is_station_node(tags):
    for key, values in STATION_NODE:
        if tags.get(key) in values:
            return True
    return False


def in_gcr(lat, lon):
    return S <= lat <= N and W <= lon <= E


print(f"pass 1/2  relations from {PBF}", flush=True)
routes = []
want_nodes = set()
want_ways = set()

for obj in osmium.FileProcessor(PBF, osmium.osm.RELATION):
    tags = dict(obj.tags)
    if tags.get("type") != "route":
        continue
    mode = tags.get("route")
    if mode not in WANT_ROUTES:
        continue
    members = []
    for m in obj.members:
        members.append({"type": m.type, "ref": m.ref, "role": m.role})
        if m.type == "n":
            want_nodes.add(m.ref)
        elif m.type == "w":
            want_ways.add(m.ref)
    routes.append({"id": obj.id, "tags": tags, "members": members})

print(f"          {len(routes)} route relations, "
      f"{len(want_nodes)} node members, {len(want_ways)} way members",
      flush=True)
print("          by mode:", dict(Counter(r["tags"].get("route") for r in routes)),
      flush=True)

print("pass 2/2  nodes", flush=True)
nodes = {}
n_seen = 0
for obj in osmium.FileProcessor(PBF, osmium.osm.NODE):
    n_seen += 1
    nid = obj.id
    is_member = nid in want_nodes
    if not is_member:
        # Cheap guard first: tag lookup is far more expensive than a
        # coordinate compare, and the overwhelming majority of Egypt's
        # nodes are nowhere near Greater Cairo.
        loc = obj.location
        if not loc.valid() or not in_gcr(loc.lat, loc.lon):
            continue
        tags = dict(obj.tags)
        if not is_station_node(tags):
            continue
    else:
        tags = dict(obj.tags)
    loc = obj.location
    nodes[nid] = {
        "id": nid,
        "lat": loc.lat if loc.valid() else None,
        "lon": loc.lon if loc.valid() else None,
        "tags": tags,
        "route_member": is_member,
    }

print(f"          scanned {n_seen:,} nodes, kept {len(nodes):,}", flush=True)

out = {
    "source": "OpenStreetMap via Geofabrik Egypt extract "
              "(osrm_data/egypt-latest.osm.pbf, already shipped with the backend)",
    "licence": "ODbL - https://www.openstreetmap.org/copyright",
    "bbox_for_untagged_station_scan": f"{S},{W},{N},{E}",
    "modes_requested": sorted(WANT_ROUTES),
    "routes": routes,
    "nodes": nodes,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"wrote {OUT}", flush=True)
