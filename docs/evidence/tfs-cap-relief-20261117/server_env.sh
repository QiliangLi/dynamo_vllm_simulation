#!/bin/bash
# 服务器栈环境与构建：nixl 依赖 + Dynamo 1.2 编译。
set -e
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTUP_TOOLCHAIN=1.96.1
export PYTHONHASHSEED=0 HF_HUB_OFFLINE=1 VLLM_PLUGINS=""
REPO=/root/dv-repo
DV=$REPO/dynamo_vllm_cpu_sim
PY=$DV/.venv/bin/python
BUILD=$HOME/.cache/dynamo-server-sim/build-1.2
cd "$DV"
# nixl 运行件进构建目录（构建脚本会把 python 组件也放进去）。
uv pip install --python "$PY" -q --target "$BUILD/python" --no-deps \
  'nixl==0.10.1' 'nixl-cu12==0.10.1' || echo NIXL_SKIP
echo "=== build start $(date +%T)"
bash scripts/build_server_dynamo.sh "$REPO/source_dev/dynamo" "$BUILD" "$PY"
echo "BUILD_DONE $(date +%T)"
