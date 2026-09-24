# 验证记录

日期：2026-09-24。以下为 CPU 仿真虚拟时间，**不是 NPU 实测性能**。

环境：`Linux-6.18.44-x86_64-with-glibc2.39`；Python 3.12.14；torch 2.11.0+cpu；ai-dynamo-runtime 1.5.0；vLLM 0.20.2+empty。

每项 12 个请求，每请求 8 个输出 token，两个逻辑 worker。具体配置和请求级记录保存在各 results 子目录。

| 模式 | 完成数 | 平均 TTFT (ms) | p95 TTFT (ms) | makespan (ms) | 读取量 (MiB) |
|---|---:|---:|---:|---:|---:|
| vLLM 原始调度器 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| Ascend 默认委托路径 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| Ascend balance + 显式补丁 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| vLLM 原生 priority | 12 | 5.234561 | 13.674912 | 15.164912 | 48 |
| 慢共享链路 | 12 | 252.649073 | 672.180640 | 673.670640 | 64 |
| 默认路径复跑 | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |
| 新建虚拟环境 bootstrap | 12 | 5.364061 | 13.674912 | 15.164912 | 48 |

通过的集成断言：所有请求完成且输出数正确；真实远端 KV 等待和接收事件存在；首 token 不早于依赖 KV 接收；每 worker 未完成请求为 0、可回收块为 127/128（一个 null sentinel）；Dynamo active_requests、potential_prefill_tokens、potential_decode_blocks 及 pending_count 均为 0。

存储单元测试 3/3 通过：中途新增并发读会改变已有读的完成时间；同路径按 FIFO 服务；启动延迟与共享链路上限生效。

慢存储将 shared_link_Bps 从 10,000,000,000 调到 100,000,000。共享链路变慢还会改变原生负载与后续路由/本地缓存复用，所以读量由 48 MiB 变成 64 MiB；这不是固定读量的纯带宽微基准。

priority 在此小例子中的均值差异只用于说明真实策略路径可执行，不能证明其优于 FCFS。p95 对 12 个请求也不具备业务统计代表性。

默认复跑的汇总时间相同；原生 picker 的平局选择可能让请求的 worker ID 对调，因此没有宣称逐事件 bitwise deterministic。

Ascend balance 使用 scripts/apply_ascend_patch.py 产生的显式副本，原始 upstream 文件未修改；未做 NPU 验证。其他模式均使用原始目标调度源码。

验证范围不含完整 EngineCore、全量 Ascend 插件、HTTP 服务、cache-aware Dynamo、PD、多卡通信、逐层 overlap、写回、取消、失败和抢占压力。详细边界见设计文档。

追加验收：在不含 upstream、.venv 或 patched 目录的新工程副本中运行同一 bootstrap.sh，成功按三个固定 SHA 下载源码、安装依赖、通过 doctor 并跑通全部请求和集成断言。报告见 results/clean_bootstrap，下载记录见 results/source-downloads.json。网络使用当前执行环境提供的 HTTPS 代理；工程脚本本身不设置代理。
