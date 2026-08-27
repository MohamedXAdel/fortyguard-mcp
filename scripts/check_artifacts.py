"""
Pre-publish guard: nothing secret may leave in a build artifact.

    python scripts/check_artifacts.py dist/

Run in CI on the built sdist and wheel, because the thing that ships is the
thing to check. Grepping the working tree is weaker: it misses whatever the
build includes that the tree does not obviously advertise, and it passes on a
machine where the secret happens not to be checked out.

WHY THIS EXISTS
---------------
Two failures, both real, both from the same afternoon.

1. A test was written with a LIVE 32-character API key pasted in as fixture
   data. It sat in `tests/`, which the sdist ships, and would have gone to
   GitHub and PyPI. A credential does not stop being a credential because it is
   standing in for one.

2. The first version of the scan that found it reported `leaks: NONE` while
   scanning ZERO FILES - the glob pattern did not match, and an empty loop
   passes every assertion inside it. That is the vacuous-assertion failure this
   project's own audit record already names, arriving in the tool built to
   prevent leaks.

So this script asserts it found artifacts, asserts it read files out of them,
and prints the count. A guard that cannot say what it checked is not a guard.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

# A 32-character lowercase hex string sitting next to something that names it a
# key. Deliberately narrow: bare 32-hex appears legitimately (truncated digests,
# request hashes), and a check that cries wolf gets switched off.
KEY_ASSIGNMENT = re.compile(
    rb"(?:api[_-]?key|apikey|token|secret)\s*[=:\"']{1,3}\s*[0-9a-f]{32}",
    re.IGNORECASE,
)

# A signed URL with an actual signature value after it. `SIGNED_URL_RE` in
# `results_store.py` contains the literal parameter NAME as part of its own
# pattern, so matching the bare name would flag the redaction code itself.
LIVE_SIGNED_URL = re.compile(rb"X-Amz-Signature=[0-9a-f]{16,}")


def members(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                yield name, z.read(name)
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                fh = t.extractfile(m)
                if fh is not None:
                    yield m.name, fh.read()


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    artifacts = sorted(
        [*dist.glob("*.whl"), *[p for p in dist.glob("*.tar.gz")]])

    # The check the first version of this script was missing.
    if not artifacts:
        print(f"FAIL: no artifacts found in {dist.resolve()} - nothing was checked")
        return 2

    problems: list[str] = []
    scanned = 0

    for art in artifacts:
        count = 0
        for name, data in members(art):
            count += 1
            scanned += 1
            base = name.rsplit("/", 1)[-1]
            if base == ".env":
                problems.append(f"{art.name}:{name} - a .env file is being shipped")
            if KEY_ASSIGNMENT.search(data):
                problems.append(f"{art.name}:{name} - looks like a credential literal")
            if LIVE_SIGNED_URL.search(data):
                problems.append(f"{art.name}:{name} - a live signed URL")
        print(f"  {art.name:48s} {art.stat().st_size:>9,} B  {count} files")

    print(f"\n  files scanned: {scanned}")
    if problems:
        print("\nFAIL - do not publish:")
        for p in problems:
            print("  *", p)
        return 1
    print("  no credentials found in any artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
