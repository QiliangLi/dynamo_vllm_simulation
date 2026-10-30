# 标准报告数据 v1

`python skills/report-organizer/scripts/generate_report.py --dataset dataset.json --output 新目录`

顶层字段：`schema_version:1`、`title`、`cases`。可选 `metadata`（口径说明键值表）、`narrative`（0/6/7/8/10章节正文）、`execution_boundary`（说明实验是否本任务新跑，与离线制图阶段分开）。每个case代表一个独立冻结运行，不表示路由重复或统计聚合。不同工况建议分别生成，再在总报告引用。

case字段：

| 字段 | 要求 |
|---|---|
| `summary.case` / `summary.label` | 唯一方案ID / 显示名称 |
| `summary.workers` | 正整数，包含本轮未执行的实例 |
| `summary.span_s` | 首条正式网关到达到最后首token的秒数 |
| `summary.rule` | 有时间线时为`arrival`或`dynamic` |
| `summary.classification_reference_only` | 有时间线时必须为布尔值；直接放行显示参考分类时为true |
| `requests` | 正式请求列表，不含预热；必须非空 |
| `comp` / `io` | 区间列表，缺日志时两者均为null；确认没有IO时io=[] |
| `bandwidth` | 实际速率区间；缺少时为null |
| `bandwidth_unavailable` | 缺少带宽数据的具体原因 |
| `config` / `run_metadata` | 原运行配置 / seed、repeat、工况和版本等已知标识 |
| `color` | 可选Matplotlib色值；否则使用稳定方案顺序配色 |
| `sources` | 可选来源路径到`{sha256,bytes}`的映射；只作证据索引，重绘不要求原路径仍在 |
| `layers` | 可选逐层明细，仅用于CSV附表；绘图使用comp/io |

requests每行必须有：

- `id`、`worker`、`cohort`。
- `arrival_s`、`completion_s`、`deadline_s`。三者以本方案首条正式网关到达为0。completion指首token，不是整个decode结束。arrival最小为0，completion最大等于span_s。
- 可选 `compute_classes`（逐请求观察到的在线分类）、`gateway_wait_ms`（中央提交−网关到达）、`arrival_lateness_ms`（客户端实际发送−计划发送）、`read_bytes`。

`cohort`是显式业务或历史分组。保留名称`all`只用于总体，不能作为cohort名称。在线轻重不能从cohort推断。可选数值缺少时省略字段，不能用0补值。

comp/io每行有`worker`、`start`、`end`、`state`。时间单位秒，原点同requests。comp.state只允许heavy/light/mixed；io.state为io。各实例计算不能重叠。IO允许重叠，时间账本先取并集再扣除名义计算。不要把一个批次的计算时长复制给每条请求。

bandwidth包含`capacity_Bps`、`read_bytes`、`intervals`和可选`source`。每个interval为`start_s`、`end_s`、`actual_Bps`、`demand_Bps`。区间按时间排序、不重叠、速率有限非负；省略区间视为0。read_bytes需等于实际速率积分；误差阈值为max(1e-6 B, read_bytes×1e-11)。缺少真实速率或可证明的模型行为时填null，不凭读取字节平均摊平。

示例：

```json
{
  "schema_version": 1,
  "title": "单方案边界样本",
  "cases": [{
    "summary": {"case":"demo","label":"示例","workers":2,"span_s":4,
      "rule":"arrival","classification_reference_only":false},
    "requests": [{"id":"r1","worker":0,"cohort":"业务组1",
      "arrival_s":0,"completion_s":4,"deadline_s":3}],
    "comp": [{"worker":0,"start":1,"end":2,"state":"heavy"}],
    "io": [{"worker":0,"start":0.5,"end":1.5,"state":"io"}],
    "bandwidth": null,
    "bandwidth_unavailable": "缺少实际速率区间",
    "run_metadata": {"seed":416,"repeat":0}
  }]
}
```

数据校验不证明因果关系，也不自动证明不同case仅有一个变量不同。人工分析通过narrative提供，标准包保留该正文以支持再次生成。

可选`auxiliary_tables`保存补充证据表，结构为`{"表名": [{"列名": "值"}]}`。生成工具将其写入`report_data/表名.csv`。表名只允许小写字母、数字、下划线和连字符，不可覆盖主表。可用于客户端计时等附表；补充表不改变主表或既有图表的统计口径。
