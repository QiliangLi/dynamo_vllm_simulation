# global4_abc

全局额度204699。最短30条为C全计算；随后90条为B分配剩余额度；其余240条为A，仅计算1–16个token。

360条请求；原input和4秒到达节奏不变。人工命中，不代表Mooncake实测命中。
总input 5117755 token，新计算 204699 token，占3.999781154%；
外部读取539571462144 B。每请求只产生1个输出token，不回放原output_length。

- [trace.jsonl.gz](trace.jsonl.gz)：原实验JSONL逐字节压缩，含完整prompt_token_ids。
- [requests.csv](requests.csv)：可读的长度、命中、新计算、到达时刻和类别小表。
- [config.json](config.json)：统一90 GB/s、K=1、12实例的后续比较配置，已移除本机overlay路径。
- [config.historical.json](config.historical.json)：原实验参数（已移除本机overlay路径），历史带宽为90 GB/s。
- [manifest.json](manifest.json)：来源、规则口径、计数和SHA256。

用父目录manage.py导出未压缩JSONL。config.json不含固定请求画像；回放时须重算T0和期限。
原始JSONL可能保留历史输出长度元数据，该字段未用于本次命中或计算量构造。
