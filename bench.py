import json, time, sys
from raptor_engine import GTFSRaptorEngine
eng = GTFSRaptorEngine('gtfs_data', use_osrm=False)
eng.load_data()
CASES = [
 ('nasr_zamalek', 30.0663104, 31.3806677, 30.0717052, 31.220952),
 ('maadi_zamalek', 29.9603, 31.2577, 30.0616, 31.2194),
 ('helwan_shubra', 29.8488, 31.3343, 30.1225, 31.2447),
 ('downtown_hop', 30.0444, 31.2357, 30.0524, 31.2468),
 ('nasr_faisal', 30.0731, 31.3467, 30.0175, 31.2037),
 ('october_downtown', 29.9285, 30.9188, 30.0444, 31.2357),
 ('newcairo_giza', 30.0100, 31.4200, 29.9900, 31.2100),
 ('shorouq_dokki', 30.1400, 31.6000, 30.0384, 31.2122),
]
out, total = {}, 0.0
for name, a, b, c, d in CASES:
    t = time.time()
    r = eng.run_raptor_by_coords(a, b, c, d, lang='en')
    el = (time.time() - t) * 1000
    total += el
    out[name] = {
        'ms': round(el, 1),
        'options': [
            {'type': o['type'], 'price': o['price'], 'time': o['time'],
             'dist': o['distance_m'],
             'steps': [(i['action'], i.get('vehicle_type'), i.get('route_number'), i.get('station'))
                       for i in o['instructions']]}
            for o in r.get('options', [])
        ] if r.get('success') else None,
        'error': r.get('error'),
    }
print(f'TOTAL {total:.0f}ms across {len(CASES)} routes')
json.dump(out, open(sys.argv[1], 'w'), indent=1)
