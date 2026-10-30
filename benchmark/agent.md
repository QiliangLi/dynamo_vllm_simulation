# AI 使用 benchmark

先读 README.md，再读 benchmark目录.md。具体负载只从目录及对应文件夹的 manifest.json 选择，不把本文件当作固定负载名单。

## 使用已有负载

1. 确认研究问题、benchmark 名称和目录中的支持状态。random 不代表随机打乱，人工标签不代表在线 TFS 分类。
2. 从仓库根目录运行 `python benchmark/manage.py verify` 和 `python benchmark/manage.py catalog --check`。
3. 读取所选文件夹的 manifest.json、config.json，以及存在时的 run_contract.json。核对计算模型、反馈、命中、SLO 和资源条件。
4. 使用 `python benchmark/manage.py export <名称> --output <新目录>` 导出。拒绝覆盖已有目录；本机 overlay 路径仅通过 `--dynamo-python` 显式注入。
5. 完整运行使用仓库 docs/实验分支使用说明.md 中的入口。支持状态为 export_only 时，先说明缺失的运行合同，不能套用其他负载的默认配置。导出成功不代表可运行或仿真通过。

## 增加负载

1. 按 README 中的六段命名创建一个新文件夹。保留数据来源、构造规则和完整运行配置；不得为得到预期排名改旧输入。
2. 放入 trace.jsonl.gz、requests.csv、config.json、config.historical.json、manifest.json 和该负载的 README.md。特殊运行合同另放 run_contract.json。
3. manifest 保留来源版本、输入 SHA256、文件 SHA256、统计总量、名称字段、描述、类别语义、反馈模式和支持状态。格式参考已有 manifest；不知道是否兼容时使用 export_only，不声称已支持。
4. 运行校验后执行 `python benchmark/manage.py catalog` 更新总目录，再执行 `catalog --check`。新类别或字段需要解释时，更新目录生成函数中的附录定义；普通新增负载不改总 README 或本文件。
5. 旧名称与不同输入发生冲突时，不静默覆盖。先确认版本方案，保留历史输入和来源。

## 边界

不得改 token 内容、请求顺序、h/u、计划时刻或完整配置来通过检查。不以人工类别代替在线状态。不沿用其他模型或带宽下的旧 T0/期限。不删除失败证据。不自动升级依赖或启动性能矩阵。只整理 benchmark 不自动生成 skill。
