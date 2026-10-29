# per_request_4pct

对每条请求n：h=16×floor(floor(0.96×n)/16)，u=n−h。全局比例会因块取整略高于4%。A90/B270仅为旧展示标签。

360条请求；原input和4秒到达节奏不变。人工命中，不代表Mooncake实测命中。
总input 5117755 token，新计算 207547 token，占4.055430555%；
外部读取539258683392 B。每请求只产生1个输出token，不回放原output_length。

- [trace.jsonl.gz](trace.jsonl.gz)：原实验JSONL逐字节压缩，含完整prompt_token_ids。
- [requests.csv](requests.csv)：可读的长度、命中、新计算、到达时刻和类别小表。
- [config.json](config.json)：统一90 GB/s、K=1、12实例的后续比较配置，已移除本机overlay路径。
- [config.historical.json](config.historical.json)：原实验参数（已移除本机overlay路径），历史带宽为120 GB/s。
- [manifest.json](manifest.json)：来源、规则口径、计数和SHA256。

用父目录manage.py导出未压缩JSONL。config.json不含固定请求画像；回放时须重算T0和期限。
原始JSONL可能保留历史输出长度元数据，该字段未用于本次命中或计算量构造。
