#!/usr/bin/env python3
"""Test runner for Iona.

Each test is a `.iona` program in this directory paired with a `.expected`
file holding its exact stdout.  The runner compiles, links, and runs each
program and diffs the output.
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IONAC = os.path.join(ROOT, "ionac.py")


def run_one(src):
    base = os.path.splitext(os.path.basename(src))[0]
    with tempfile.TemporaryDirectory() as td:
        exe = os.path.join(td, base)
        build = subprocess.run(
            [sys.executable, IONAC, src, "-o", exe],
            capture_output=True, text=True,
        )
        if build.returncode != 0:
            return False, "build failed:\n" + build.stderr
        run = subprocess.run([exe], capture_output=True, text=True)
        expected_path = os.path.splitext(src)[0] + ".expected"
        with open(expected_path) as f:
            expected = f.read()
        if run.stdout != expected:
            return False, f"output mismatch:\n--- expected ---\n{expected}--- got ---\n{run.stdout}"
        return True, ""


def main():
    tests = sorted(f for f in os.listdir(HERE) if f.endswith(".iona"))
    failures = 0
    for t in tests:
        ok, msg = run_one(os.path.join(HERE, t))
        if ok:
            print(f"PASS  {t}")
        else:
            failures += 1
            print(f"FAIL  {t}\n{msg}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
