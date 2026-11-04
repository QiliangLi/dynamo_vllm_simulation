# 共享存储单画像功能验证

所有实例共用存储，每请求只生成一个画像、一次分类和历史登记。没有候选画像或 commit。

smoke-r1 与 alpha1-r1 均为两实例八请求；分别验证 α=0.1 与 α=1。
bias-half-r1 只上报一半缓存事件，验证允许预计和实际命中偏差，八条全部完成。
legacy-r1 关闭新画像模式，验证十二条旧路径及原生计分/资源账本排空。
三组新路径的 tier_prescore.check.json 核对每请求一次分类、首次 scorer 前分类、所有候选标记相同。
八请求的历史版本恰为 1～8。重复共享前缀事件没有使预计读取量按实例数翻倍。

unit.log：361 项回归通过，108.755 秒。build.json：固定上游的独立 native dev/debug=0 构建。
frontend.prescore.log 是原日志中分类、评分、booking 和选择报告的行摘录。
测试 STORAGE 事件为专用夹具，计算与 KV 读取仍为 CPU 仿真；不证明真机性能。
未重跑完整性能矩阵。完整本机审计目录：
/home/zed/.local/share/state-aware-network/audits/dynamo-tier-shared-20261104。
