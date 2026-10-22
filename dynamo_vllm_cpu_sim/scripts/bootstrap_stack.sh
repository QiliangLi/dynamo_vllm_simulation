#!/usr/bin/env bash
# 进程栈环境安装（W0 的可复现脚本；在 bootstrap.sh 已建好 .venv 之后执行）。
#
#   bash scripts/bootstrap_stack.sh
#
# 内容（全部有固定版本/SHA 来源，2026-10-20 在 macOS 26.6 ARM 验证）：
# 1. ai-dynamo 1.5.0 组件包（frontend/router/vllm/mocker）：从固定 SHA 的
#    upstream/dynamo 源码 --no-deps 安装（根 pyproject 的 vllm extra 绑定
#    vllm==0.28.0，与本地 0.20.2 不兼容，故必须 --no-deps）。
# 2. OpenAI 服务/worker 路径缺的第三方包：watchfiles、
#    prometheus-fastapi-instrumentator。
# 3. torchvision==0.26.0 --no-deps（匹配 torch 2.11.0；dynamo.vllm 的多模态
#    工具链模块级导入需要，本栈不使用其功能）+ pillow。
# 注意：macOS 需先按包内 README 完成 dynamo runtime 的 select-service 自编；
# Linux 的 PyPI wheel 开箱即用。

set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "先运行 bash scripts/bootstrap.sh（进程栈复用其 .venv 与固定源码）" >&2
  exit 1
fi
if [ ! -d upstream/dynamo ]; then
  echo "缺 upstream/dynamo（bootstrap.sh 的 fetch_sources 会按 SHA 下载）" >&2
  exit 1
fi

# 代理环境按需自行 export HTTPS_PROXY/HTTP_PROXY
uv pip install --python .venv/bin/python --no-deps ./upstream/dynamo
uv pip install --python .venv/bin/python --no-deps watchfiles prometheus-fastapi-instrumentator
if [ "$(uname -s)" = "Linux" ]; then
  # The PyPI Linux wheel expects CUDA; this stack uses the pinned CPU torch wheel.
  uv pip install --python .venv/bin/python --no-deps --index-url https://download.pytorch.org/whl/cpu 'torchvision==0.26.0'
else
  uv pip install --python .venv/bin/python --no-deps 'torchvision==0.26.0'
fi
uv pip install --python .venv/bin/python --no-deps pillow

# 自检：入口可导入 + SimExecutor 限定名可解析
.venv/bin/python -c "
import dynamo.frontend.main  # noqa: F401
import dynamo.vllm.main      # noqa: F401
from vllm.v1.executor.abstract import Executor
from vllm.utils.import_utils import resolve_obj_by_qualname
cls = resolve_obj_by_qualname('sim.stack.executor.SimExecutor')
assert issubclass(cls, Executor)
print('stack bootstrap OK: ai-dynamo components importable; SimExecutor resolvable')
"
