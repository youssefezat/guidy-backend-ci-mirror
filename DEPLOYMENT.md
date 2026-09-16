# Deploying the Guidy Backend

## Quick start (Docker)

1. Point a DNS A record for your domain (e.g. `api.guidyapp.com`) at
   this server's public IP. Caddy (step 5) needs this to already be live
   before it can get a certificate — it'll retry if not, but nothing
   will be reachable over HTTPS until DNS resolves.
2. Make sure `gtfs_data/` (in this folder) has the GTFS feed files:
   `stops.txt`, `routes.txt`, `trips.txt`, `stop_times.txt`.
3. One-time OSRM data setup — see the comment block at the top of
   `docker-compose.yml` (pulls the Egypt OSM extract and builds the
   routing graph OSRM needs; only needs to be redone when you want to
   refresh the underlying map data).
4. Copy `.env.example` to `.env`, and at minimum set `DOMAIN` to the
   real domain from step 1 (the other defaults work for a
   single-machine deployment as-is).
5. `docker compose up --build -d`

This starts three containers: Caddy (the only one with published ports —
80 and 443), the FastAPI backend, and OSRM. Caddy automatically requests
and renews a Let's Encrypt certificate for `DOMAIN` and reverse-proxies
HTTPS traffic to the backend; the backend and OSRM are only reachable
from other containers on the compose network, not from the internet.
First boot takes a few extra seconds while Caddy obtains the cert — watch
progress with `docker compose logs -f caddy`.

Verify HTTPS is actually working before pointing the app at it:

```
curl https://your-domain.com/api/health
```

If that hangs or errors, check `docker compose logs caddy` first — the
most common cause is DNS not pointing at this server yet, or ports
80/443 being blocked by a firewall or cloud provider security group
(Caddy needs port 80 open too, even for HTTPS, since that's how Let's
Encrypt's HTTP-01 challenge is served).

## Running without Docker

```
pip install -r requirements.txt
export GTFS_PATH=/path/to/your/gtfs/folder
export OSRM_BASE_URL=http://localhost:5000   # or USE_OSRM=false to skip it
python main.py
```

## Verifying it's healthy

```
curl http://localhost:8000/api/health
```

Should return `{"status": "ok", "stops_loaded": <some number > 0>}`. If
`stops_loaded` is 0 or the server won't start, check `GTFS_PATH` points
at a folder that actually has the four GTFS files in it.

## What's configurable (see `.env.example` for details)

| Variable | Purpose | Default |
|---|---|---|
| `DOMAIN` | Public domain Caddy requests an HTTPS cert for | *(must be set — see step 1 above)* |
| `GTFS_PATH` | Folder with the GTFS feed | *(dev machine path — must be set)* |
| `USE_OSRM` | Enable/disable real walking directions | `true` |
| `OSRM_BASE_URL` | Where to reach OSRM | `http://localhost:5000` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated allowed origins | `*` |
| `LOG_LEVEL` | Python logging level | `INFO` |
| `RATE_LIMIT_PER_MINUTE` | Requests/min per IP on `/api/route` | `30` |

## Connecting the Flutter app to this backend

The app's base URL is set at build time, not hardcoded:

```
flutter build apk --dart-define=API_BASE_URL=https://your-domain.com/api
```

See `lib/screens/api_service.dart` for the fallback default (a dev-only
LAN IP that won't work for anyone but the original dev machine).

## Before this handles real production traffic

- ~~Put it behind HTTPS~~ — done: Caddy (see `Caddyfile` and the `caddy`
  service in `docker-compose.yml`) terminates TLS automatically. Nothing
  else exposes a port to the internet.
- Narrow `CORS_ALLOWED_ORIGINS` if you ever add a web client (mobile
  apps aren't subject to CORS, so `*` is harmless for app-only traffic).
- Consider adding rate-limiting/abuse protection in front of the whole
  service too (e.g. at a reverse proxy or CDN/WAF layer) in addition to
  the in-process limiting already in `main.py` — that in-process limiter
  resets if the container restarts and won't help against a distributed
  attack.
- Resolve the GTFS data licensing question (CC BY-NC 4.0, non-commercial)
  before this serves a monetized app in production — flagged earlier in
  this conversation, still unresolved.

## GTFS data licensing

Resolved: official written approval obtained directly from Transport for
Cairo (TfC) to use their GTFS feed in this app, including the metro data
added on top of their original bus/microbus feed. No longer a blocker for
monetized/production use.
