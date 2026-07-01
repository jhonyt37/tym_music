#!/usr/bin/env bash
# Run all TYM Music tests. Exit code 0 = all green, non-zero = failures.
set -e
cd "$(dirname "$0")"

PASS=0
FAIL=0

run_suite() {
  local label="$1"; shift
  echo ""
  echo "══════════════════════════════════════════════════"
  echo " $label"
  echo "══════════════════════════════════════════════════"
  if "$@"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
}

# 1. Pure JS logic tests (no server, no DOM)
run_suite "Frontend pure logic (Node.js)" node test_logic.js

# 2. HTML + CSS structure tests (no server)
run_suite "HTML/CSS structure (Python)" python3 test_html.py -v

# 3. API integration tests (starts a real server)
run_suite "API integration (Python)" python3 test_api.py -v

echo ""
echo "══════════════════════════════════════════════════"
echo " Suites: $((PASS+FAIL)) total, $PASS passed, $FAIL failed"
echo "══════════════════════════════════════════════════"
[ "$FAIL" -eq 0 ]
