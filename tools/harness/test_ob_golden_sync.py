"""The golden gate's single run must be made exactly the way the goldens were.

trace_shard.sh produces the goldens (two runs per PAK, compared for determinism);
ob_golden_check.sh replays one run against them. A golden is only comparable to a
run made under the same conditions, so every condition that shapes the trace is
asserted identical here: the environment line, the timeout, the state wipe, and
the golden file-name derivation. Exit 0 = in sync, 1 = drift.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SHARD = open(os.path.join(HERE, "trace_shard.sh"), encoding="utf-8").read()
CHECK = open(os.path.join(HERE, "ob_golden_check.sh"), encoding="utf-8").read()


def env_of(text):
    """The OB_* assignments on the traced run, normalised (paths stripped)."""
    m = re.search(r"OB_PAK=\"\$pak\"(.*?)timeout (\d+) /tmp/ob", text, re.S)
    if not m:
        return None
    body = m.group(1).replace("\\\n", " ")
    pairs = sorted(re.findall(r"(OB_[A-Z]+)=(\S+)", body))
    return [(k, "<path>" if k == "OB_TEST" else v) for k, v in pairs], m.group(2)


def wipe_of(text):
    m = re.search(r"rm -rf (/media/fat/config/\*.*?)2>/dev/null", text, re.S)
    return " ".join(m.group(1).split()) if m else None


def safe_of(text):
    m = re.search(r'safe="\$\(echo "\$base" \| tr ([^|]+)\| tr -cd ([^)]+)\)"', text)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


checks = [("traced-run environment + timeout", env_of), ("state wipe", wipe_of),
          ("golden file-name derivation", safe_of)]
fail = 0
for label, fn in checks:
    a, b = fn(SHARD), fn(CHECK)
    ok = a is not None and a == b
    fail += not ok
    print("%s  %s\n      trace_shard.sh     %s\n      ob_golden_check.sh %s" % (
        "PASS" if ok else "FAIL", label, a, b))
if 'mktemp' not in CHECK:
    print("FAIL  the check run must write its trace to a fresh temp file")
    fail += 1
sys.exit(1 if fail else 0)
