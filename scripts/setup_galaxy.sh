#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker build --platform linux/amd64 -f scripts/Dockerfile.galaxy -t kline-galaxy:1.1.9-tables .
