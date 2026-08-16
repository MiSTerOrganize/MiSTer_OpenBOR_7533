#!/bin/bash
# Behavioural test for the fetch_verify download helper.
# 🛑 Cuts the REAL function out of the shipped file -- never a reimplementation,
# which would pass while the shipped code rotted.
# Works on both copies: OpenBOR's (column 0, in a .sh) and PICO-8's (indented,
# inside a YAML run-block), so anchor on the name and strip the indent.
SCRIPT="$1"
[ -f "$SCRIPT" ] || { echo "usage: $0 <file containing fetch_verify>"; exit 2; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
sed -n '/^[[:space:]]*fetch_verify() {/,/^[[:space:]]*}/p' "$SCRIPT" > "$WORK/raw.sh"
sed 's/^[[:space:]]*//' "$WORK/raw.sh" > "$WORK/fn.sh"
grep -q "fetch_verify() {" "$WORK/fn.sh" || { echo "FAIL: could not extract fetch_verify"; exit 2; }
echo "extracted $(wc -l < "$WORK/fn.sh" | tr -d ' ') lines of the real function"

PASS=0; FAIL=0
check() {
    if [ "$2" = "$3" ] && [ "$4" = "$5" ]; then
        echo "  PASS  $1 (rc=$3, wget attempts=$5)"; PASS=$((PASS+1))
    else
        echo "  FAIL  $1 (rc=$3 want $2, attempts=$5 want $4)"; FAIL=$((FAIL+1))
    fi
}

run_case() {
    MODE="$1"
    CNT="$WORK/count"; : > "$CNT"
    export MODE CNT WORK
    (
      wget() {
          _o="$4"
          echo x >> "$CNT"
          n=$(wc -l < "$CNT")
          case "$MODE" in
              always_ok)  echo data > "$_o" ;;
              always_bad) echo junk > "$_o" ;;
              ok_on_3rd)  if [ "$n" -ge 3 ]; then echo data > "$_o"; else echo junk > "$_o"; fi ;;
          esac
          return 0
      }
      tar() { grep -q '^data$' "$2" 2>/dev/null; }
      sleep() { :; }
      . "$WORK/fn.sh"
      fetch_verify "$WORK/out.tar.gz" http://m1/x.tgz http://m2/x.tgz
      echo "$?" > "$WORK/rc"
    ) >/dev/null 2>&1
    RC=$(cat "$WORK/rc")
    ATTEMPTS=$(wc -l < "$CNT" | tr -d ' ')
}

run_case always_ok
check "first-mirror success -> no needless retry" 0 "$RC" 1 "$ATTEMPTS"

run_case always_bad
check "all corrupt -> fails after 3 rounds x 2 mirrors" 1 "$RC" 6 "$ATTEMPTS"

run_case ok_on_3rd
check "transient blip -> RECOVERS on round 2" 0 "$RC" 3 "$ATTEMPTS"

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
