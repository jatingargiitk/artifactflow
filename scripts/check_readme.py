"""Re-run every README block marked <!-- run: ... --> and diff its output.

A block looks like:

    <!-- run: artifactflow check examples/demo -->
    ```console
    $ artifactflow check examples/demo
    ...exact output...
    ```
"""
import os
import re
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCK = re.compile(r"<!-- run: (.+?) -->\n```console\n\$ [^\n]*\n(.*?)```", re.S)


def main() -> int:
    text = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    blocks = BLOCK.findall(text)
    if not blocks:
        print("no runnable blocks found")
        return 1
    bad = 0
    for cmd, expected in blocks:
        p = subprocess.run(shlex.split(cmd), cwd=ROOT, capture_output=True, text=True)
        got = p.stdout
        if got.rstrip("\n") != expected.rstrip("\n"):
            bad += 1
            print("MISMATCH: %s\n--- expected\n%s\n--- got\n%s" % (cmd, expected, got))
        else:
            print("ok: %s" % cmd)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
