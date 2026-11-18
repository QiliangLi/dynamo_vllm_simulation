#!/bin/bash
# 查看服务器运行失败的进程日志。用法：bash diag.sh <run-dir>
D="$1"
echo "== files"
find "$D" -maxdepth 2 -type f | head -20
for f in "$D"/*/frontend.log "$D"/*/central.log "$D"/*/worker0.log; do
  [ -f "$f" ] || continue
  echo "== tail $f"
  tail -15 "$f"
done
