#!/usr/bin/env python3
"""Fetch immutable upstream trees; never overwrite an unrecognized checkout."""

import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def fetch(item):
    name, version = item
    target = ROOT / "upstream" / name
    marker = target / ".sim-upstream.json"
    if target.exists():
        if marker.exists() and json.loads(marker.read_text())["sha"] == version["sha"]:
            print(f"{name}: pinned tree already present")
            return
        raise RuntimeError(
            f"{target} exists without matching provenance; move it aside explicitly"
        )
    url = f"https://codeload.github.com/{version['repo']}/tar.gz/{version['sha']}"
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(temp, filter="data")
        trees = list(Path(temp).iterdir())
        if len(trees) != 1:
            raise RuntimeError("unexpected source archive layout")
        trees[0].rename(target)
    marker.write_text(
        json.dumps(
            {**version, "archive_sha256": hashlib.sha256(data).hexdigest()}, indent=2
        )
        + "\n"
    )
    print(f"{name}: {version['sha']}")


if __name__ == "__main__":
    versions = json.loads((ROOT / "versions.json").read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch, versions.items()))
