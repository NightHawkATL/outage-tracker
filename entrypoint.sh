#!/bin/sh
mkdir -p /app/data

# Start Tailscale daemon in the background
tailscaled --state=/app/data/tailscaled.state &

# Wait a few seconds for the daemon to initialize
sleep 3

# Start the Python app
exec python -u app.py