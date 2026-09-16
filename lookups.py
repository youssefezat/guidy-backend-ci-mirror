"""
Two lookup tools that sit beside the trip planner rather than inside it.

  RouteBrowser  -- "what does bus 65 actually do?" Search a route by number
                   or by where it goes, then read its stops in order.
  RailPlanner   -- "get me from here to there on rail only." Was metro-only
                   (3 lines, 84 stations); now also covers the East Nile
                   Monorail, both Cairo LRT branches and the airport
                   shuttle, because the question a rider is really asking is
                   "keep me off the roads", and answering it with the metro
                   alone would refuse a New Capital trip the network can now
                   actually make.

Both read structures the engine already builds at load time and add no new
data source:

  engine.route_stop_sequence[(route_id, direction_id)] -> [canonical stop_id]
      the LONGEST stop sequence seen for that route/direction, so a
      short-turn trip doesn't truncate the browser's view of the line.
  engine.graph[stop_id] -> [(neighbour, route_id, direction_id, seconds)]
      already carries the engine's timing guards, so metro durations here
      inherit every correction applied to the main planner.

Keeping this in its own module is deliberate: raptor_engine.py is the
routing engine, and neither of these is routing in that sense. The browser
is a table of contents; the metro planner is a shortest path over 84 nodes.
"""
import heapq
import json
import re
from collections import defaultdict

from raptor_engine import BANDED_FARE_TIERS, banded_journey_fare


_STATION_DIAC = re.compile(r"[\u064b-\u0652\u0640]")
_STATION_PREFIX = re.compile(r"^(محطة|شارع|ش|ميدان|م)\s+")


def _normalise_station(name):
    """Same shape of normalisation gov_match uses, kept local so lookups
    has no import-time dependency on the data-preparation scripts."""
    t = _STATION_DIAC.sub("", name or "")
    t = t.translate(str.maketrans("\u0623\u0625\u0622\u0671", "\u0627\u0627\u0627\u0627"))
    t = t.replace("\u0649", "\u064a").replace("\u0629", "\u0647")
    t = re.sub(r"[()\[\]\u00ab\u00bb.,\u060c]", " ", t)
    t = " ".join(t.split())
    for _ in range(2):
        t = _STATION_PREFIX.sub("", t)
    t = re.sub(r"^ال", "", t)
    return t.replace(" ", "")


class TransitLookups:
    # Cost of changing lines, in "hops", used only to rank otherwise-equal
    # metro paths. Cairo's interchanges involve real walking (Sadat and
    # Al-Shohadaa especially), so a route with one fewer change is usually
    # the better answer even when it is a stop or two longer.
    LINE_CHANGE_PENALTY_HOPS = 3

    # Fallback per-hop time when the graph has no usable duration for a
    # metro edge. The Cairo metro runs roughly 2 minutes between stations.
    DEFAULT_HOP_SEC = 120
    # Interchange time: platform-to-platform walking plus waiting.
    INTERCHANGE_SEC = 300

    # Longest walk the planner will use to join two rail stations that the
    # feed models as separate stops: Cairo Stadium metro to the monorail
    # station above it is 133m, and the LRT-to-monorail interchange at Arts
    # and Culture City is 628m. Matches the engine's own
    # RAIL_INTERCHANGE_WALK_M so the rail planner and the trip planner agree
    # on which interchanges exist -- the two disagreeing about whether a
    # connection is possible would be worse than either bound being wrong.
    RAIL_LINK_WALK_M = 750

    def __init__(self, engine):
        self.e = engine
        self._rail_adj = defaultdict(list)    # stop_id -> [(neighbour, route_id|WALK, sec)]
        self._rail_stops = set()
        self._metro_stops = set()             # metro-only subset, for filtering
        self._line_terminus = {}              # (route_id, direction_id) -> terminus stop_id
        self._patterns = {}                   # (route_id, direction_id) -> [stop lists]
        self._station_notes, self._transit_facts = self._load_station_notes()
        self._build_patterns()
        self._build_rail_graph()

    # ------------------------------------------------------------------
    # Stop patterns
    # ------------------------------------------------------------------
    def _build_patterns(self):
        """
        EVERY distinct stop sequence a route runs, not just its longest.

        engine.route_stop_sequence deliberately keeps only the longest
        variant per direction -- that is the right answer for the routing
        engine, which wants one representative picture of a line so a
        short-turn trip can't truncate it.

        It is the wrong answer for a browser. Metro Line 3 branches at Kit
        Kat: one branch to Rod El-Farag Corridor (29 stops), one to Cairo
        University (28). Keeping only the longest meant the Cairo
        University branch -- a real branch, carrying real passengers --
        simply did not appear in the app, while the trip planner happily
        routed people along it. Three microbus route-directions had the
        same problem.

        Built from the engine's own parsed trips, so it needs no second
        pass over the GTFS files.
        """
        seen = defaultdict(set)
        for trip_id, route_id in self.e.trips.items():
            st_list = self.e.stop_times.get(trip_id)
            if not st_list:
                continue
            direction_id = self.e.trip_direction.get(trip_id, '')
            ordered = []
            for stop_id, _seq, _arr, _dep in sorted(st_list, key=lambda r: r[1]):
                canonical = self.e.stop_alias.get(stop_id, stop_id)
                if not ordered or ordered[-1] != canonical:
                    ordered.append(canonical)
            # `>= 1`, not `> 1`. Three Mwasalat Misr routes in 10th of
            # Ramadan have every stop clustered into one canonical hub, so
            # their whole pattern collapses to a single stop. Dropping them
            # would make three routes vanish from the browser -- the same
            # silent disappearance this method exists to fix. They are kept,
            # and a stop_count of 1 is itself the signal to a rider that the
            # feed doesn't really have that line.
            if ordered:
                seen[(route_id, direction_id)].add(tuple(ordered))

        # Longest first: the main variant should lead, short-turns follow.
        self._patterns = {
            key: sorted((list(p) for p in pats), key=len, reverse=True)
            for key, pats in seen.items()
        }

    def _longest_pattern(self, route_id, direction_id):
        pats = self._patterns.get((route_id, direction_id))
        return pats[0] if pats else []

    # ------------------------------------------------------------------
    # Rail
    # ------------------------------------------------------------------
    def _build_rail_graph(self):
        """
        Rail-only adjacency, taken from the engine's own graph so the edge
        times are the corrected ones rather than a fresh guess.

        WALKING EDGES ARE INCLUDED, and they have to be. The metro-only
        version could get away without them because the metro's five
        interchanges are single clustered stop_ids -- change at Sadat and
        you never leave the node. That stops being true the moment a second
        operator's stations arrive: the LRT's Adly Mansour is its own stop
        55m from the metro's, and the monorail's Cairo Stadium is 133m from
        the metro's Stadium. With transit edges alone the rail network would
        look like three disconnected islands, and the planner would refuse
        every metro-to-monorail journey as "not connected" -- the one
        question these new lines exist to answer.

        Only short walks between two rail stations are taken, so this never
        turns into "walk 2km to a different line".
        """
        rail_ids = self.e.rail_route_ids or self.e.metro_route_ids
        metro_ids = self.e.metro_route_ids

        for stop_id, edges in self.e.graph.items():
            for neighbour, route_id, direction_id, seconds in edges:
                if route_id in rail_ids:
                    self._rail_adj[stop_id].append((neighbour, route_id, seconds))
                    self._rail_stops.add(stop_id)
                    self._rail_stops.add(neighbour)
                    if route_id in metro_ids:
                        self._metro_stops.add(stop_id)
                        self._metro_stops.add(neighbour)

        # Second pass: the set of rail stops has to be complete before
        # walking links can be filtered down to rail-to-rail.
        for stop_id in list(self._rail_stops):
            for neighbour, route_id, direction_id, seconds in self.e.graph.get(stop_id, []):
                if route_id != "WALK" or neighbour not in self._rail_stops:
                    continue
                a, b = self.e.stops.get(stop_id), self.e.stops.get(neighbour)
                if not a or not b:
                    continue
                if self.e._haversine(a["lat"], a["lon"], b["lat"], b["lon"]) > self.RAIL_LINK_WALK_M:
                    continue
                self._rail_adj[stop_id].append((neighbour, "WALK", seconds))

        for (route_id, direction_id), seq in self.e.route_stop_sequence.items():
            if route_id in rail_ids and seq:
                self._line_terminus[(route_id, direction_id)] = seq[-1]

    def _lines_serving(self, stop_id):
        return sorted({r for _, r, _ in self._rail_adj.get(stop_id, [])
                       if r != "WALK"})

    def rail_stations(self, lang="en", modes=None):
        """
        Every rail station, with the lines it serves. Interchanges are the
        ones with more than one.

        `modes` filters by vehicle type ({"metro"}, {"monorail"}, ...) so the
        app can still offer a metro-only view -- a rider who wants the
        certainty of a train they know shouldn't have to scroll past 38
        stations they have no intention of using.
        """
        out = []
        for stop_id in self._rail_stops:
            info = self.e.stops.get(stop_id)
            if not info:
                continue
            lines = self._lines_serving(stop_id)
            if modes is not None:
                lines = [r for r in lines
                         if self.e._route_vtype.get(r) in modes]
                if not lines:
                    continue
            out.append({
                "stop_id": stop_id,
                "name": self.e._localized_stop_name(stop_id, lang),
                "lat": info["lat"],
                "lon": info["lon"],
                "lines": [self._line_label(r, lang) for r in lines],
                "modes": sorted({self.e._route_vtype.get(r) for r in lines}),
                "is_interchange": len({self.e._route_vtype.get(r) for r in lines}) > 1
                                  or len(lines) > 1,
                # What a rider actually navigates by. Nobody thinks "Mar
                # Girgis", they think "the Hanging Church".
                "note": self._note_for(self.e._localized_stop_name(stop_id, lang)),
            })
        out.sort(key=lambda s: s["name"])
        return out

    def metro_stations(self, lang="en"):
        """Metro-only station list. Kept as its own name because the API
        endpoint /api/metro/stations and the app's Metro tab both call it."""
        return self.rail_stations(lang=lang, modes={"metro"})

    def _localized_route_name(self, route_id, lang):
        """
        Route description in the requested language.

        The engine parses 892 Arabic route names out of translations.txt at
        load time and, until this existed, nothing in this module ever read
        them: search_routes, route_detail and _line_label all returned
        `info['long_name']` raw. So the Lines browser and the line detail
        page showed English corridor names to Arabic users -- "Moassasa -
        Nahda City" instead of "المؤسسة - مدينة النهضة" -- while the stop
        names beside them translated correctly, because those went through
        _localized_stop_name.

        It surfaced while checking the new rail lines, but it was never
        specific to them: it affected every route in the feed.
        """
        name = (self.e.routes.get(route_id, {}).get("long_name") or "").strip()
        if lang == "ar":
            return self.e.route_long_name_ar.get(name, name)
        return name

    def _localized_route_number(self, route_id, lang):
        name = (self.e.routes.get(route_id, {}).get("short_name") or "").strip()
        if lang == "ar":
            return self.e.route_short_name_ar.get(name, name)
        return name

    def _line_label(self, route_id, lang):
        r = self.e.routes.get(route_id, {})
        return {
            "route_id": route_id,
            "name": self._localized_route_number(route_id, lang) or route_id,
            "description": self._localized_route_name(route_id, lang),
            # The app colours and labels a leg by mode, and "LRT" vs "Metro"
            # is the difference between the right platform and the wrong one.
            "vehicle_type": r.get("vehicle_type", "metro"),
        }

    def plan_rail(self, from_stop, to_stop, lang="en", modes=None):
        """
        Shortest rail-only path, ranked by hops plus a penalty per line
        change. Returns None when either endpoint isn't on the rail network
        or the two aren't connected -- callers turn that into a real message
        rather than an empty itinerary.

        `modes` restricts which vehicle types may be ridden, so the old
        metro-only behaviour is still available as plan_rail(..., {"metro"}).
        """
        # Resolve through the engine's stop clustering first. The LRT's Adly
        # Mansour sits 55m from the metro's and the engine collapses them
        # into one canonical stop, so the id the app holds for an LRT
        # station can be an alias that appears nowhere in the graph. Looking
        # it up unresolved returned "not on the rail network" for the single
        # most important interchange in the city.
        from_stop = self.e.stop_alias.get(from_stop, from_stop)
        to_stop = self.e.stop_alias.get(to_stop, to_stop)

        stops = self._rail_stops if modes is None else {
            s for s in self._rail_stops
            if any(self.e._route_vtype.get(r) in modes
                   for r in self._lines_serving(s))
        }
        if from_stop not in stops or to_stop not in stops:
            return None
        if from_stop == to_stop:
            return None

        # State is (stop, line) rather than just stop: arriving at Sadat on
        # Line 1 and arriving on Line 2 are different situations, and only
        # the pair knows whether the next edge is a change.
        best = {}
        heap = [(0, 0, from_stop, None, [])]
        found = None

        while heap:
            cost, seconds, stop, line, path = heapq.heappop(heap)
            key = (stop, line)
            if key in best and best[key] <= cost:
                continue
            best[key] = cost

            if stop == to_stop:
                found = (seconds, path)
                break

            for neighbour, route_id, edge_sec in self._rail_adj.get(stop, []):
                if route_id == "WALK":
                    # A station-to-station walk is an interchange, not a
                    # hop: it costs the change penalty and real walking
                    # time, but it must not count toward any mode's fare
                    # band. Charging it as a hop would have made a rider
                    # crossing the concourse at Adly Mansour pay for a stop
                    # they never rode.
                    if neighbour not in stops:
                        continue
                    heapq.heappush(heap, (
                        cost + self.LINE_CHANGE_PENALTY_HOPS,
                        seconds + (edge_sec or self.INTERCHANGE_SEC),
                        neighbour, None, path + [(stop, neighbour, "WALK")],
                    ))
                    continue
                if modes is not None and self.e._route_vtype.get(route_id) not in modes:
                    continue
                changed = line is not None and route_id != line
                step_cost = cost + 1 + (self.LINE_CHANGE_PENALTY_HOPS if changed else 0)
                step_sec = seconds + (edge_sec or self.DEFAULT_HOP_SEC) + (
                    self.INTERCHANGE_SEC if changed else 0)
                heapq.heappush(heap, (
                    step_cost, step_sec, neighbour, route_id,
                    path + [(stop, neighbour, route_id)],
                ))

        if not found:
            return None
        seconds, path = found
        return self._shape_metro_result(from_stop, to_stop, path, seconds, lang)

    def plan_metro(self, from_stop, to_stop, lang="en"):
        """Metro-only journey. Kept for /api/metro/plan and the Metro tab."""
        return self.plan_rail(from_stop, to_stop, lang=lang, modes={"metro"})

    def _shape_metro_result(self, from_stop, to_stop, path, seconds, lang):
        """Turn the edge list into rider-facing legs, one per line ridden."""
        legs = []
        interchanges = []
        current = None
        pending_walk = None

        for a, b, route_id in path:
            if route_id == "WALK":
                # Close the current leg and remember that the next boarding
                # is reached on foot, so the interchange can say so.
                if current is not None:
                    legs.append(current)
                    current = None
                pending_walk = (a, b)
                continue
            if current is None or current["route_id"] != route_id:
                if current is not None:
                    legs.append(current)
                if legs:
                    prev = legs[-1]
                    interchanges.append({
                        "station": self._station_brief(a, lang),
                        "from_line": self._line_label(prev["route_id"], lang),
                        "to_line": self._line_label(route_id, lang),
                        # True when the change means leaving one station and
                        # walking to another -- Adly Mansour metro to Adly
                        # Mansour LRT. A rider needs to know that before they
                        # are standing on a platform looking for a sign.
                        "on_foot": pending_walk is not None,
                    })
                pending_walk = None
                current = {
                    "route_id": route_id,
                    "line": self._line_label(route_id, lang),
                    "stops": [self._station_brief(a, lang)],
                }
            current["stops"].append(self._station_brief(b, lang))
        if current is not None:
            legs.append(current)

        # Fare is banded per mode on the hops ridden ON THAT MODE, and each
        # mode is a separate ticket -- see BANDED_FARE_TIERS in the engine.
        # The same functions the trip planner uses, so the two can never
        # disagree about the price of the same ride.
        hops_by_mode = {}
        for leg in legs:
            leg["num_stops"] = len(leg["stops"]) - 1
            leg["towards"] = self._towards(leg["route_id"], leg["stops"], lang)
            vt = leg["line"]["vehicle_type"]
            hops_by_mode[vt] = hops_by_mode.get(vt, 0) + leg["num_stops"]

        fare_breakdown = []
        total_fare = 0
        for vt, hops in sorted(hops_by_mode.items()):
            fare = banded_journey_fare(vt, hops)
            total_fare += fare
            fare_breakdown.append({"vehicle_type": vt, "stops": hops,
                                   "fare_egp": fare})

        return {
            "success": True,
            "from": self._station_brief(from_stop, lang),
            "to": self._station_brief(to_stop, lang),
            "total_stops": sum(hops_by_mode.values()),
            "interchange_count": len(interchanges),
            "time_min": max(1, round(seconds / 60)),
            "fare_egp": total_fare,
            # Itemized, because a 3-mode journey quoting one number leaves a
            # rider unable to check it against the three tickets they buy.
            "fare_breakdown": fare_breakdown,
            "legs": legs,
            "interchanges": interchanges,
        }

    def _load_station_notes(self):
        """Landmark notes for major stations, from the Cairo Governorate
        information sheet -- "محطة مار جرجس: في وسط القاهرة القبطية".

        This is the kind of thing a GTFS feed structurally cannot carry and
        a rider actually navigates by: nobody thinks "Mar Girgis", they
        think "the Hanging Church". Keyed on the normalised Arabic name
        rather than a stop_id, because the sheet names stations the way
        people say them and the feed disambiguates them its own way.

        Absent file is not an error -- same fallback philosophy as the OSRM
        edge cache. The notes are enrichment, never load-bearing.
        """
        try:
            with open(f"{self.e.folder}/station_notes.json", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}, {}
        return blob.get("station_notes", {}), blob

    def _station_brief(self, stop_id, lang):
        info = self.e.stops.get(stop_id, {})
        brief = {
            "stop_id": stop_id,
            "name": self.e._localized_stop_name(stop_id, lang) if info else stop_id,
            "lat": info.get("lat"),
            "lon": info.get("lon"),
        }
        note = self._note_for(brief["name"])
        if note:
            brief["note"] = note
        return brief

    def _note_for(self, name):
        """Landmark note for a station name, or None.

        Tries the name as written and then with any parenthetical removed:
        this feed disambiguates stations that share a name -- "السادات
        (التحرير)" -- while the information sheet writes the bare one. Same
        mismatch that made every disambiguated stop invisible to the
        governorate corridor matcher.
        """
        if not name:
            return None
        for form in (name, re.sub(r"\s*[\(（][^)）]*[\)）]\s*", " ", name).strip()):
            hit = self._station_notes.get(_normalise_station(form))
            if hit:
                return hit["note_ar"]
        return None

    def _towards(self, route_id, leg_stops, lang):
        """Which end of the line this leg is heading for -- the thing
        actually printed on the platform sign, and the only way a rider
        knows which side to stand on."""
        if len(leg_stops) < 2:
            return None
        first_id, second_id = leg_stops[0]["stop_id"], leg_stops[1]["stop_id"]
        for (rid, direction_id), seq in self.e.route_stop_sequence.items():
            if rid != route_id:
                continue
            try:
                i, j = seq.index(first_id), seq.index(second_id)
            except ValueError:
                continue
            if j > i:
                terminus = self._line_terminus.get((rid, direction_id))
                if terminus:
                    return self.e._localized_stop_name(terminus, lang)
        return None

    # ------------------------------------------------------------------
    # Route browser
    # ------------------------------------------------------------------
    VEHICLE_SYNONYMS = {
        "bus": "bus اتوبيس أتوبيس باص cta هيئة النقل العام",
        "minibus": "minibus مينيباص ميني باص مينى باص مشروع",
        "microbus": "microbus ميكروباص ميكرو باص سيرفيس سرفيس مشروع",
        "metro": "metro مترو الأنفاق مترو",
        "monorail": "monorail مونوريل قطار مونوريل",
        "lrt": "lrt القطار الكهربائي الخفيف القطار الخفيف قطار",
        "tram": "tram ترام",
        "apm": "apm قطار المطار shuttle",
    }

    @staticmethod
    def _normalize_search_text(text: str) -> str:
        if not text:
            return ""
        t = text.lower()
        t = re.sub(r"[إأآا]", "ا", t)
        t = re.sub(r"[ىي]", "ي", t)
        t = re.sub(r"[ةه]", "ه", t)
        t = re.sub(r"[\u064B-\u065F\u0670]", "", t)
        t = t.replace("mini bus", "minibus")
        t = t.replace("micro bus", "microbus")
        t = t.replace("ميني باص", "مينيباص")
        t = t.replace("مينى باص", "مينيباص")
        t = t.replace("ميكرو باص", "ميكروباص")
        t = re.sub(r"[^\w\s]", " ", t)
        return " ".join(t.split())

    def search_routes(self, query="", lang="en", limit=40, vehicle_type=None):
        """
        Match on route number AND on where the route goes, supporting bilingual
        tokens, Arabic normalization (yaa/alif maqsura, hamzas), and compound
        spacing (e.g. 'mini bus' vs 'minibus', 'ميني باص' vs 'مينى باص').
        """
        q_norm = self._normalize_search_text(query)
        q_tokens = q_norm.split() if q_norm else []
        results = []
        seen = set()

        for (route_id, direction_id), seq in sorted(
                ((k, v[0]) for k, v in self._patterns.items()), key=lambda kv: kv[0]):
            if route_id in seen or not seq:
                continue
            info = self.e.routes.get(route_id, {})
            vtype = info.get("vehicle_type", "bus")
            if vehicle_type and vtype != vehicle_type:
                continue

            number = self._localized_route_number(route_id, lang)
            description = self._localized_route_name(route_id, lang)
            first = self.e._localized_stop_name(seq[0], lang) if seq[0] in self.e.stops else ""
            last = self.e._localized_stop_name(seq[-1], lang) if seq[-1] in self.e.stops else ""

            if q_norm:
                opp_lang = "ar" if lang == "en" else "en"
                opp_number = self._localized_route_number(route_id, opp_lang)
                opp_description = self._localized_route_name(route_id, opp_lang)
                opp_first = self.e._localized_stop_name(seq[0], opp_lang) if seq[0] in self.e.stops else ""
                opp_last = self.e._localized_stop_name(seq[-1], opp_lang) if seq[-1] in self.e.stops else ""

                haystack_raw = " ".join([
                    number, description, first, last,
                    opp_number, opp_description, opp_first, opp_last,
                    (info.get("short_name") or ""), (info.get("long_name") or ""),
                    vtype,
                    self.VEHICLE_SYNONYMS.get(vtype, ""),
                ])
                h_norm = self._normalize_search_text(haystack_raw)

                if not all(tok in h_norm for tok in q_tokens):
                    continue

            seen.add(route_id)
            results.append({
                "_rank": self._match_rank(q_norm, number, description, info.get("short_name"), vtype),
                "route_id": route_id,
                "number": number or None,
                "description": description,
                "vehicle_type": vtype,
                "from": first,
                "to": last,
                "stop_count": len(seq),
                "typical_headway_min": self.e.route_typical_headway_min.get((route_id, direction_id)),
                "reconstructed": route_id in self.e.reconstructed_route_ids,
                # Same rule as route_detail(): flat-fare modes (bus,
                # minibus) get a real per-boarding number; banded modes
                # (metro/monorail/lrt/apm) and microbus are priced by
                # hops/distance, not a single figure, so this is null and
                # the app falls back to its own range text for those. This
                # used to be missing entirely, which is why the browse list
                # card showed a hardcoded, silently-stale fare guess while
                # the detail screen (which always had this field) showed
                # the real, current one.
                "fare_egp": None if (vtype in BANDED_FARE_TIERS or vtype == "microbus")
                            else self.e._estimate_leg_fare(route_id, 0),
            })

        # Relevance first, then numbered routes, then coverage.
        results.sort(key=lambda r: (r["_rank"], r["number"] is None, -r["stop_count"]))
        for r in results:
            r.pop("_rank", None)
        return results[:limit]

    @classmethod
    def _match_rank(cls, q_norm, number, description, short_name="", vehicle_type=""):
        """0 = number match, 1 = vehicle type keyword match, 2 = corridor / stop match."""
        if not q_norm:
            return 2
        for n in (number, short_name):
            if not n:
                continue
            norm_n = cls._normalize_search_text(n)
            if norm_n == q_norm:
                return 0
            tokens = norm_n.split()
            if q_norm in tokens:
                return 0
            q_toks = q_norm.split()
            if all(t in tokens for t in q_toks):
                return 0

        # Vehicle type keyword match: searching "metro", "monorail", "lrt", "bus", "minibus"
        if vehicle_type:
            syns = cls._normalize_search_text(cls.VEHICLE_SYNONYMS.get(vehicle_type, ""))
            q_toks = q_norm.split()
            if any(t in syns.split() for t in q_toks):
                return 1

        return 2

    def route_detail(self, route_id, lang="en"):
        """Every direction of one route, each as an ordered stop list."""
        info = self.e.routes.get(route_id)
        if not info:
            return None

        # One entry per distinct stop pattern, not per direction: a branch
        # or a short-turn is a different thing to ride and deserves its own
        # tab. `variant_of` lets the app say "this is the second way route X
        # runs in this direction" rather than showing two identical-looking
        # headings.
        directions = []
        for (rid, direction_id), patterns in sorted(self._patterns.items()):
            if rid != route_id:
                continue
            for index, seq in enumerate(patterns):
                if not seq:
                    continue
                valid_stops = [s for s in seq if s in self.e.stops]
                if len(valid_stops) < 2:
                    continue
                pts = self.e._shape_segment(route_id, valid_stops[0], valid_stops[-1], direction_id=direction_id)
                start_s = self.e.stops[valid_stops[0]]
                end_s = self.e.stops[valid_stops[-1]]

                # Ensure the sliced shape starts and ends near the actual terminal stops
                if len(pts) > 2:
                    d_start = self.e._haversine(start_s["lat"], start_s["lon"], pts[0]["lat"], pts[0]["lon"])
                    d_end = self.e._haversine(end_s["lat"], end_s["lon"], pts[-1]["lat"], pts[-1]["lon"])
                    if d_start > 800 or d_end > 800:
                        pts = []

                if len(pts) <= 2:
                    pts = [{"lat": self.e.stops[s]["lat"], "lon": self.e.stops[s]["lon"]}
                           for s in valid_stops if "lat" in self.e.stops[s] and "lon" in self.e.stops[s]]
                else:
                    # Enforce exact endpoint alignment with terminal stops
                    pts[0] = {"lat": start_s["lat"], "lon": start_s["lon"]}
                    pts[-1] = {"lat": end_s["lat"], "lon": end_s["lon"]}

                # Guard against acute 180-degree needle spikes (haywire spurs)
                if len(pts) > 4:
                    cleaned_pts = [pts[0]]
                    i = 1
                    while i < len(pts) - 1:
                        prev_p = cleaned_pts[-1]
                        curr_p = pts[i]
                        next_p = pts[i + 1]
                        d_ab = self.e._haversine(prev_p["lat"], prev_p["lon"], curr_p["lat"], curr_p["lon"])
                        d_bc = self.e._haversine(curr_p["lat"], curr_p["lon"], next_p["lat"], next_p["lon"])
                        d_ac = self.e._haversine(prev_p["lat"], prev_p["lon"], next_p["lat"], next_p["lon"])
                        if d_ab > 800 and d_bc > 800 and d_ac < 120:
                            # Prune acute out-and-back needle spike vertex
                            i += 1
                            continue
                        cleaned_pts.append(curr_p)
                        i += 1
                    cleaned_pts.append(pts[-1])
                    pts = cleaned_pts
                directions.append({
                    "direction_id": direction_id,
                    "variant": index,
                    "is_main_variant": index == 0,
                    "towards": self.e._localized_stop_name(seq[-1], lang) if seq[-1] in self.e.stops else None,
                    # Where it STARTS matters when two variants share a
                    # terminus. Both inbound Line 3 patterns end at Adly
                    # Mansour; only the origin (Rod El-Farag Corridor vs
                    # Cairo University) tells them apart, and without it the
                    # app would show two identical-looking tabs.
                    "from": self.e._localized_stop_name(seq[0], lang) if seq[0] in self.e.stops else None,
                    "typical_headway_min": self.e.route_typical_headway_min.get((rid, direction_id)),
                    "stops": [self._station_brief(s, lang) for s in valid_stops],
                    "points": pts,
                })

        if not directions:
            return None

        vehicle_type = info.get("vehicle_type", "bus")
        return {
            "success": True,
            "route_id": route_id,
            # See the note in search_routes: this is what lets the detail
            # screen mark a line provisional instead of presenting an
            # estimate as a surveyed fact.
            "reconstructed": route_id in self.e.reconstructed_route_ids,
            "number": self._localized_route_number(route_id, lang) or None,
            "description": self._localized_route_name(route_id, lang),
            "vehicle_type": vehicle_type,
            # Formal bus and minibus charge a flat fare per boarding, so one
            # number describes the whole line. Every banded mode (metro,
            # monorail, LRT) is priced by hops and the microbus by distance
            # -- for those, any single number would be a guess dressed up as
            # a fact, so the field is null and the app says what the fare
            # depends on instead.
            "fare_egp": None if (vehicle_type in BANDED_FARE_TIERS
                                 or vehicle_type == "microbus")
                        else self.e._estimate_leg_fare(route_id, 0),
            # ...but the bands themselves are worth showing, so the app can
            # render "20 EGP up to 5 stations, 40 up to 10..." rather than a
            # bare "depends on distance". Free modes report an explicit 0.
            "fare_bands": [
                {"up_to_stops": max_hops, "fare_egp": fare}
                for max_hops, fare in BANDED_FARE_TIERS.get(vehicle_type, [])
            ] or None,
            "directions": directions,
        }
