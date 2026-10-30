# agentx_random_360_12s_12_180GBps

> 2026-10-30更新：上述核心运行代码已同步；本负载专用合同仍未完成交付运行器验收，保持仅校验/导出。下文“尚未移植代码”是加入输入时的历史说明。

固定360条AgentX请求，12秒到达，180GB/s单共享带宽池，12个逻辑实例。输入为2026-10-30已验收三方案实验的原文件，没有重新抽样或修改命中率。

|项目|值|
|---|---|
|选取位置|抽中40主链组后，按全局请求序号30%位置起取第2608—2967条|
|输入token|23,255,872|
|有效远端命中token|22,588,592，97.130703%|
|新计算token|667,280|
|累计KV读取|2,480,769,527,808 B，2480.769528 GB|
|资源|12实例；K1；block16；30159逻辑块；每步最多4条/8192新token|
|计算|glm5_direct_sum_v1；launch_saving_fraction=1；TP1/DP8/EP8参考画像|
|执行|78层流水；关闭本地prefix caching；EDF；每请求输出1 token|
|方案|直接准入、TFS α=0.1、TFS α=1；TFS首token释放，关闭强制准入|
|反馈|关闭四类在线执行反馈；原生Dynamo最终答复及后台EOF收尾|
|预热与SLO|72条预热，排空和历史/GC重置后360正式；期限=arrival+4×参考服务时间|

原始数据源为`semianalysisai/cc-traces-weka-062126`，固定revision和抽样种子416见manifest。360条保留原相对间隔并缩放到0～12秒，不是均匀每秒30条，也不是12秒完成承诺。

命中来自完整主链组及子代理历史`hash_ids`的转换结果。有效远端前缀按block16取整，并至少保留一个新块。假设窗口前前缀已经在远端，非片段冷启动或实测存储命中。合成token只承载长度，不复现工具调用和Agent依赖。A90/B270是整段输入外读压力P75分组，不是在线TFS重读标记。

## 文件与导出

- `trace.jsonl.gz`：原实验JSONL逐字节压缩，含完整token数组、时间和命中来源。
- `requests.csv`：360条可读明细，包括源行号、到达、h/u和读取量。
- `config.json`、`config.historical.json`：直接准入默认配置，已移除本机Dynamo overlay路径；原配置SHA保留在manifest。
- `run_contract.json`：三个方案的完整配置及运行合同。
- `manifest.json`：来源、计数、模型边界和文件SHA256。

从仓库根目录执行，输出目录必须不存在：

```bash
python3 benchmark/manage.py verify
python3 benchmark/manage.py export agentx_random_360_12s_12_180GBps --output temp/agentx360_input
```

导出包含`trace.jsonl`、`config.json`和`run_contract.json`，不会启动仿真。解压后SHA256为`eeafecc40e2eb2e5185875e872716056531daecb13bcb768fa9555efacf2e707`，与原验收实验完全一致。

## 运行兼容边界

本次只加入固定输入、配置、校验和导出，不移植研究版本运行代码。当前交付运行器仍使用旧batch-v2与同步反馈，尚不支持本case所需合同。显式选择本case会在启动前拒绝；`--case all`仍只运行原三个Mooncake case。不能按旧默认90GB/s、4秒或旧计时回放后宣称复现。

39条超过原公式入口131072 token，最长300480。原实验只把冻结副本的长度guard及max_model_len调至300481，算子系数不改，属于公式外推。仅调配置不能让旧计时模块支持它。复现还需要直接组合计时、关闭执行反馈、最终答复/后台EOF与统一Kahan诊断计数支持。

已有三方案实验在独立研究版本上各完成72预热加360正式；不能将其当作当前交付运行器的验收。输入校验使用父目录 manage.py verify；统一索引在父目录 benchmark目录.md，完整原始实验事件不随输入发布。
