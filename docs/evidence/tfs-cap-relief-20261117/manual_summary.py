"""手动汇总一个服务器 run 目录的 report.json（绕过 summarize 失败）。"""
import json
import math
import sys

folder = sys.argv[1]
r = json.loads(open(f'{folder}/report.json').read())
reqs = r['requests']
ok = sum(x.get('ok') for x in reqs.values())
ttft = sorted(x['ttft_s'] for x in reqs.values() if x.get('ok') and x.get('ttft_s') is not None)
print('n=%d ok=%d mean=%.0fms p95=%.0fms elapsed=%.2fs' % (
    len(reqs), ok, 1000 * sum(ttft) / len(ttft),
    1000 * ttft[math.ceil(len(ttft) * .95) - 1], r['model_elapsed_s']))
