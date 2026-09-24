#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'; exit 1; }
if [ ! -x .venv/bin/python ]; then uv venv --python 3.12 .venv; fi
.venv/bin/python scripts/fetch_sources.py
uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cpu --no-deps 'torch==2.11.0+cpu'
uv pip install --python .venv/bin/python --no-deps -r requirements.lock
VLLM_TARGET_DEVICE=empty SETUPTOOLS_SCM_PRETEND_VERSION=0.20.2 \
  uv pip install --python .venv/bin/python --no-deps --no-build-isolation -e upstream/vllm
export PYTHONHASHSEED=0 VLLM_PLUGINS='' HF_HUB_OFFLINE=1
.venv/bin/python scripts/doctor.py
.venv/bin/python -m sim.run --output results/bootstrap
.venv/bin/python scripts/check_results.py results/bootstrap
