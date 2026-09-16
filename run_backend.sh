#!/usr/bin/env bash
# Run the Guidy backend on a Mac/Linux box, reachable from phones on the LAN.
#   ./run_backend.sh            (first run creates .venv)
#   or double-click "Start Backend.command" in Finder.
# macOS will ask whether Python may accept incoming connections: Allow.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
for p in /opt/homebrew/bin /usr/local/bin; do [ -d "$p" ] && PATH="$p:$PATH"; done

hold() { [ -t 0 ] && read -r -p "Press Return to close this window..." _; exit "${1:-1}"; }

if curl -fsS -m 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
  echo "A backend is already running on port 8000 -- nothing to do."
  hold 0
fi

# Newest Python available (the Xcode tools' python3 is old).
py=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && { py="$c"; break; }
done
[ -n "$py" ] || { echo "Python 3 not found: brew install python@3.12"; hold 1; }

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv with $($py --version) ..."
  "$py" -m venv .venv || { echo "venv creation failed"; hold 1; }
fi
. .venv/bin/activate
echo "Installing/updating packages ..."
pip install -q --disable-pip-version-check -r requirements.txt || { echo "pip install failed"; hold 1; }

export GTFS_PATH="${GTFS_PATH:-gtfs_data}"
export USE_OSRM="${USE_OSRM:-false}"   # no local OSRM: straight-line walking legs
iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
ip=$(ipconfig getifaddr "${iface:-en0}" 2>/dev/null || ipconfig getifaddr en0 2>/dev/null \
     || hostname -I 2>/dev/null | awk '{print $1}' || true)
echo
echo "================================================================"
echo " Guidy backend"
echo "   phones on this Wi-Fi : http://${ip:-<this-machine-ip>}:8000/api"
echo "   check in a browser   : http://127.0.0.1:8000/api/health"
echo "   stop                 : Ctrl+C or close this window"
echo "================================================================"
echo
python -m uvicorn main:app --host 0.0.0.0 --port 8000
hold $?
