#!/bin/sh
# Every test file, each a standalone script, with the failures printed rather
# than the first failure hiding the rest. `test_*` only: `harness.py` is a
# module these import and running it would report a green result for nothing.
#
# Prefers a local .venv, because the dependencies live in the image and a
# developer box has to get them from somewhere.
set -u
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x .venv/bin/python3 ]; then PY=.venv/bin/python3; else PY=python3; fi
fi
status=0
passed=0
for t in tests/test_*.py; do
    if out=$("$PY" "$t" 2>&1); then
        echo "PASS  $t"
        passed=$((passed + 1))
    else
        status=1
        echo "FAIL  $t"
        echo "$out" | sed 's/^/      /'
    fi
done
echo "--- $passed file(s) passed"
exit "$status"
