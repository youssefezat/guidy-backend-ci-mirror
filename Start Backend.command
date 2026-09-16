#!/bin/bash
# Double-click in Finder to start the Guidy backend on port 8000.
cd "$(dirname "$0")" || exit 1
exec bash ./run_backend.sh
