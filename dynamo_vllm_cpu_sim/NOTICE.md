# 来源与范围

上游 Dynamo、vLLM、vLLM-Ascend 源码由安装脚本从各自固定 commit 获取，保留各项目原始许可证和版权声明。发布包不内置这些仓库或第三方 wheel。

`patched/ascend_balance.patch` 是对 Apache-2.0 许可的 vLLM-Ascend 文件的局部修改；生成的完整文件保留原始头部。许可证副本见 `licenses/Ascend-LICENSE`。补丁仅完成本包 CPU 实验验证。

`sim/`、`scripts/`、示例配置、测试与设计说明是为本次方案编写的集成代码。结果目录是合成负载的验证记录，不是硬件 benchmark。
