#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv/bin/python"
export AWS_REGION="${AWS_REGION:-us-east-1}"

echo "============================================================"
echo " Autonomous Incident Commander — Local Test"
echo "============================================================"
echo "Region:  $AWS_REGION"
echo "Python:  $VENV"
echo ""

# Run the commander with the real EventBridge alarm event
exec "$VENV" "$DIR/test_commander.py"
