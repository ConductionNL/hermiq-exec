#!/bin/sh
set -e

echo "Starting hermiq-exec ExApp..."
echo "APP_ID: ${APP_ID:-hermiq_exec}"
echo "APP_HOST: ${APP_HOST:-0.0.0.0}"
echo "APP_PORT: ${APP_PORT:-23000}"

exec python3 ex_app/lib/main.py
