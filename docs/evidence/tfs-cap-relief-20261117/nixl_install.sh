#!/bin/bash
set -e
export PATH="$HOME/.local/bin:$PATH"
uv pip install --python /root/dv-repo/dynamo_vllm_cpu_sim/.venv/bin/python -q \
  --target "$HOME/.cache/dynamo-server-sim/build-1.2/python" --no-deps \
  'nixl==0.10.1' 'nixl-cu12==0.10.1'
echo NIXL_OK
ls "$HOME/.cache/dynamo-server-sim/build-1.2/python" | grep -i nixl | head -4
