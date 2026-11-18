#!/bin/bash
# 经 git SSH 浅克隆上游 ai-dynamo/dynamo，提取缺失源文件到 source_dev 对应位置。
set -e
UP=/tmp/dynamo-upstream
rm -rf "$UP"
# 优先匹配 1.2.0 版本 tag，否则退回 main。
TAG=$(git ls-remote --tags git@github.com:ai-dynamo/dynamo.git 2>/dev/null \
      | awk -F/ '{print $NF}' | grep -x 'v\?1\.2\.0' | tail -1)
if [ -n "$TAG" ]; then
  echo "cloning tag $TAG"
  git clone --quiet --depth 1 --branch "$TAG" git@github.com:ai-dynamo/dynamo.git "$UP"
else
  echo "no 1.2.0 tag; cloning main (upstream $(git ls-remote git@github.com:ai-dynamo/dynamo.git HEAD | cut -c1-8))"
  git clone --quiet --depth 1 git@github.com:ai-dynamo/dynamo.git "$UP"
fi
FILES=(
  "lib/mocker/src/replay/offline/core.rs"
  "lib/mocker/src/scheduler/sglang/core.rs"
  "lib/mocker/src/scheduler/vllm/core.rs"
  "lib/bench/src/kv_router_common.rs"
  "lib/kv-router/src/standalone_indexer/runtime/discovery.rs"
  "lib/kv-router/src/standalone_indexer/runtime/query_engine.rs"
  "lib/kv-router/src/standalone_indexer/runtime/subscriber.rs"
)
DEST=/mnt/d/Coding/dynamo_vllm_simulation/source_dev/dynamo
for f in "${FILES[@]}"; do
  if [ -f "$UP/$f" ]; then
    cp "$UP/$f" "$DEST/$f"
    echo "OK  $f"
  else
    echo "MISSING-UPSTREAM $f"
  fi
done
