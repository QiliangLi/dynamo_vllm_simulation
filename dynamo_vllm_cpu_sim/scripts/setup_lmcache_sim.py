#!/usr/bin/env python3
"""Fetch pinned LMCache Python source and isolated CPU-import dependencies."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sim.stack.lmcache_bridge import PIN

DEPS = ["sortedcontainers==2.4.0", "aiofile==3.12.3", "caio==0.12.10", "nvtx==0.2.16",
        "redis==8.1.0", "awscrt==0.38.0"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=Path.home()/".local/share/state-aware-network")
    args = parser.parse_args()
    source = args.cache_root/"sources/lmcache-v0.3.11"
    dependencies = args.cache_root/"lmcache-prototype-deps"
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        subprocess.run(["git", "-c", "http.sslBackend=gnutls", "clone", "--depth", "1", "--branch", "v0.3.11",
                        "https://github.com/LMCache/LMCache.git", str(source)], check=True)
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != PIN:
        raise RuntimeError("existing LMCache checkout differs from pin")
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(dependencies), *DEPS], check=True)
    print(json.dumps(dict(source=str(source), dependencies=str(dependencies), commit=PIN, packages=DEPS)))


if __name__ == "__main__":
    main()
