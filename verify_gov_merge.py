import datetime
from raptor_engine import GTFSRaptorEngine
NOON = datetime.datetime(2026, 9, 9, 12, 0, 0)
CASES = {
    "Kafr Tohormos -> El-Nozha (route 37's own extent)": ((30.01637,31.17475),(30.12798,31.36017)),
    "8th District Nasr City -> Kafr Tohormos (the rider)": ((30.0664834,31.3807572),(30.0204667,31.1733851)),
}
for feed, label in (("gtfs_data.backup-20260910-034943", "BASE (BEFORE MERGE)"),
                    ("gtfs_data", "MERGED (WITH GOV ROUTES)")):
    e = GTFSRaptorEngine(feed, use_osrm=False)
    e.load_data()
    for name,(O,D) in CASES.items():
        r=e.run_raptor_by_coords(O[0],O[1],D[0],D[1],lang="en",now=NOON)
        print(f"\n[{label}] {name}")
        if not r.get("success"): print("   FAILED", r.get("reason")); continue
        for opt in r["options"][:1]:
            legs=[i for i in opt["instructions"] if i.get("route_id")]
            print(f"   {opt['time']} min, {opt.get('fare_egp')} EGP, {len(legs)} vehicle(s)")
            for i in legs:
                print(f"     {i['vehicle_type']:9} {str(i.get('route_number')):6} {i.get('route_id','')[:14]:14} {i.get('route_description','')[:44]}")
