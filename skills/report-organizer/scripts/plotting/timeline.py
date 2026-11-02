"""Reusable online-heavy/light timeline reader and renderer; no simulation imports."""
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path

COLORS = dict(heavy='#4C72B0', light='#DD8452', mixed='#9974A5', io='#C44E52', idle='#C7C7C7')


class DataError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise DataError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def merge(intervals):
    result = []
    for left, right in sorted(intervals):
        if right <= left:
            continue
        if result and left <= result[-1][1] + 1e-12:
            result[-1][1] = max(result[-1][1], right)
        else:
            result.append([left, right])
    return result


def length(intervals):
    return math.fsum(right-left for left, right in merge(intervals))


def subtract(intervals, cuts):
    result = []
    cuts = merge(cuts)
    for left, right in merge(intervals):
        cursor = left
        for a, b in cuts:
            if b <= cursor or a >= right:
                continue
            if a > cursor:
                result.append((cursor, min(a, right)))
            cursor = max(cursor, b)
            if cursor >= right:
                break
        if cursor < right:
            result.append((cursor, right))
    return result


def default_label(case_id, cfg):
    admission = cfg.get('admission', {})
    routing = cfg.get('routing_accounting', {})
    if routing.get('enabled') and admission.get('placement') == 'scheduler':
        suffix = '＋强制准入' if admission.get('idle_bypass') else ''
        return f"新 TFS α={routing['waiting_factor']:g}" + suffix
    if admission.get('mode') == 'tfs' and admission.get('placement') == 'gateway':
        return '旧 TFS'
    if admission.get('mode') == 'immediate':
        ordering = cfg.get('request_ordering', cfg.get('policy', ''))
        return '直接放行 ' + str(ordering).upper()
    return case_id


def discover(report=None, manifest=None):
    if manifest is not None:
        manifest = Path(manifest).resolve()
        spec = read_json(manifest)
        require(spec.get('schema_version') == 1, '绘图配置的 schema_version 必须为 1。')
        require(isinstance(spec.get('cases'), list) and spec['cases'], '配置缺少 cases 列表。')
        base = manifest.parent
        cases = []
        for item in spec['cases']:
            require(isinstance(item, dict) and item.get('id') and item.get('path'), '每个方案必须提供 id 和 path。')
            case = dict(item)
            case['path'] = (base / item['path']).resolve()
            cases.append(case)
        return dict(title=spec.get('title', '在线轻重请求执行时间线'), cases=cases,
                    base=base, output=(base/spec.get('output_dir', 'online_timeline')).resolve(),
                    manifest=manifest)
    report = Path(report).resolve()
    require(report.is_dir(), f'报告目录不存在：{report}')
    if (report/'timeline.json').is_file():
        return discover(manifest=report/'timeline.json')
    if (report/'config.json').is_file() and (report/'admission.final.json').is_file():
        return dict(title='在线轻重请求执行时间线', cases=[dict(id=report.name, path=report)],
                    base=report, output=report/'online_timeline', manifest=None)
    data = report/'report_data' if (report/'report_data').is_dir() else report
    raw = data/'raw'
    require(raw.is_dir(), '未找到 report_data/raw。也可传入单个 run 目录，或使用 --manifest。')
    if (data/'run_summary.csv').is_file():
        with (data/'run_summary.csv').open(encoding='utf-8-sig', newline='') as stream:
            ids = [row['mode'] for row in csv.DictReader(stream)]
    else:
        ids = [p.name for p in sorted(raw.iterdir()) if p.is_dir() and (p/'config.json').is_file()]
    require(ids and len(set(ids)) == len(ids), '没有方案，或方案名称重复。')
    return dict(title=report.name+'｜在线轻重请求', cases=[dict(id=key, path=raw/key) for key in ids],
                base=report, output=report/'online_timeline', manifest=None)


class Classifier:
    def __init__(self, status, config):
        admission = config.get('admission', {})
        self.rule = admission.get('classification')
        require(self.rule in ('arrival', 'dynamic', 'bandwidth_line'),
                '缺少可识别的 admission.classification；不使用历史 A/B 猜测。')
        self.reference_only = admission.get('mode') == 'immediate'
        self.sa = config.get('compute', {}).get('mode') == 'sa_e26f'
        self.status = status
        self.thresholds = [e for e in status.get('events', []) if e.get('event') == 'threshold_updated']
        self.times = [e['t'] for e in self.thresholds]
        require(self.times == sorted(self.times), '动态阈值事件顺序错误。')

    def threshold(self, gateway_model_time):
        index = bisect_right(self.times, gateway_model_time)-1
        require(index >= 0, '计算发生前没有动态阈值记录，不能恢复轻重分类。')
        return self.thresholds[index]

    def at(self, request_id, gateway_model_time):
        if self.rule in ('arrival', 'bandwidth_line'):
            # bandwidth_line（tfs-lease 臂）与 arrival 同为到达冻结 hint（v2），布尔重读标记同源。
            hint = self.status.get('storage_hints', {}).get(request_id, {})
            require(type(hint.get('is_heavy_read')) is bool, f'{request_id} 缺少布尔重读标记，不能用历史 A/B 替代。')
            return 'heavy' if hint['is_heavy_read'] else 'light'
        theta = self.threshold(gateway_model_time)['theta']
        profile = self.status['records'][request_id]['profile']
        require('pressure_Bps' in profile, f'{request_id} 缺少 pressure_Bps。')
        p = profile['pressure_Bps']
        heavy = theta is not None and p >= theta and (self.sa or p > 0)
        return 'heavy' if heavy else 'light'


def load_case(spec):
    root = Path(spec['path'])
    require(root.is_dir(), f'原始日志目录不存在：{root}')
    sources = {}

    def track(path):
        path = Path(path)
        require(path.is_file(), f'缺少必要日志：{path}')
        stat = path.stat()
        sources[path] = (stat.st_size, stat.st_mtime_ns)
        return path

    cfg = read_json(track(root/'config.json'))
    status = read_json(track(root/'admission.final.json'))
    storage_path = root/'storage.projection.json'
    if not storage_path.is_file():
        storage_path = root/'storage.final.json'
    storage = read_json(track(storage_path))
    require('layer_read_history' in storage and 'hit_bindings' in storage,
            '当前适配器需要逐层读取日志 layer_read_history 和 hit_bindings，不支持旧 bulk/DES 格式。')
    clock_path = root/'clock.json'
    scale = float(cfg.get('wall_time_scale', 1.))
    require(math.isfinite(scale) and scale > 0, 'wall_time_scale 必须为正数。')
    if clock_path.is_file():
        clock = read_json(track(clock_path))
        origin = clock['gateway_origin_monotonic_s']
        require(math.isclose(float(clock.get('scale', scale)), scale), '网关与配置的时间缩放比例不同。')
    else:
        require('origin_monotonic_s' in status, '缺少网关时钟原点；需要 clock.json 或状态中的 origin_monotonic_s。')
        origin = status['origin_monotonic_s']
    client_path = next((p for p in (root/'client_report.json', root/'measured/report.json') if p.is_file()), None)
    if spec.get('formal_ids'):
        ids = set(spec['formal_ids'])
        id_source = 'manifest.formal_ids'
    else:
        require(client_path is not None, '无法确定正式请求集合：需要 client_report.json、measured/report.json 或配置中的 formal_ids。')
        client = read_json(track(client_path))
        ids = set(client['requests'])
        require(len(ids) == client['n_total'] == client['n_ok'], '正式客户端记录未全部成功，不输出看似完整的时间线。')
        id_source = str(client_path.name)
    require(ids and ids <= status['records'].keys(), '正式请求集合为空，或状态中缺少请求。')
    records = {rid: status['records'][rid] for rid in sorted(ids)}
    require(all(r.get('first_token_s') is not None for r in records.values()), '正式请求缺少首 token 记录。')
    first = min(r['profile']['arrival_s'] for r in records.values())
    end = max(r['first_token_s'] for r in records.values())
    span = end-first
    require(span > 0, '正式执行窗口为空。')
    classifier = Classifier(status, cfg)
    bindings = storage['hit_bindings']

    def source_id(native):
        if native in status['records']:
            return native
        key = native.replace('-', '').lower()[:32]
        require(key in bindings, f'无法关联请求 ID：{native}')
        return bindings[key]

    workers = int(cfg['workers'])
    require(workers > 0, 'workers 必须为正数。')
    comp = [];layers = [];observed = defaultdict(list);seen = defaultdict(set);request_workers = defaultdict(set)
    steps = set();log_paths = sorted(root.glob('worker*.steps.jsonl'))
    require(log_paths, '缺少 worker*.steps.jsonl；汇总 CSV 无法恢复准确时间线。')
    for path in log_paths:
        track(path);opened = {}
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                event = json.loads(line);kind = event.get('event')
                if kind not in ('layer_compute_start', 'layer_compute_complete'):
                    continue
                key = (event['step'], event['layer'])
                if kind == 'layer_compute_start':
                    require(key not in opened, f'{path.name} 出现重复层开始事件：{key}')
                    opened[key] = event
                    continue
                require(key in opened, f'{path.name} 缺少对应层开始事件：{key}')
                begin = opened.pop(key)
                member_ids = [source_id(rid) for rid in begin['req_ids']]
                if not set(member_ids) & ids:
                    continue
                require(set(member_ids) <= ids, '同一批混有预热与正式请求，不能准确切割正式计算时间。')
                worker = int(event['worker'])
                require(0 <= worker < workers, f'实例编号越界：{worker}')
                gateway_time = (begin['occurred_mono']-origin)*scale
                left = gateway_time-first;right = (event['occurred_mono']-origin)*scale-first
                duration = float(begin['duration_model_s'])
                require(-1e-6 <= left <= left+duration <= right+1e-6 and right <= span+1e-6,
                        '层计算时钟、缩放或正式执行窗口不一致。')
                classes = {rid: classifier.at(rid, gateway_time) for rid in member_ids}
                state = next(iter(set(classes.values()))) if len(set(classes.values())) == 1 else 'mixed'
                for rid, value in classes.items():
                    seen[rid].add(value);request_workers[rid].add(worker)
                comp.append(dict(worker=worker, start=left, end=left+duration, state=state))
                observed[worker].append((left, right));steps.add((worker, event['step']))
                layers.append(dict(case=spec['id'], worker=worker, step=event['step'], layer=event['layer'],
                    start_s=left, model_s=duration, state=state,
                    heavy_requests=sum(v == 'heavy' for v in classes.values()),
                    light_requests=sum(v == 'light' for v in classes.values()),
                    request_ids=json.dumps(member_ids, separators=(',', ':'))))
        require(not opened, f'{path.name} 存在未结束的层计算。')
    require(set(seen) == ids, '部分正式请求没有计算记录。')
    io = [];read_bytes = 0
    shift = (storage['t0_monotonic_s']-origin)*scale-first
    for event in storage['layer_read_history']:
        rid = source_id(event['rid'])
        if rid not in ids:
            continue
        left = shift+event['submitted_s'];right = shift+event['completed_s'];worker = int(event['worker'])
        require(0 <= worker < workers and -1e-6 <= left < right <= span+1e-6, '读取区间超出正式窗口。')
        io.append(dict(worker=worker, start=left, end=right, state='io'))
        read_bytes += event['bytes']
    accounts = []
    for worker in range(workers):
        cc = [(r['start'], r['end']) for r in comp if r['worker'] == worker]
        require(abs(length(cc)-math.fsum(b-a for a, b in cc)) < 1e-7, '同一实例的名义计算区间发生重叠。')
        red = length(subtract([(r['start'], r['end']) for r in io if r['worker'] == worker], cc))
        model = length(cc);gray = span-model-red
        require(gray >= -1e-7, '时间账本出现负数。')
        accounts.append(dict(case=spec['id'], worker=worker, model_s=model, red_s=red, gray_s=max(0., gray)))
    request_rows = []
    for rid, record in records.items():
        require(len(request_workers[rid]) == 1, f'{rid} 跨实例执行，当前适配器不能恢复迁移语义。')
        worker = next(iter(request_workers[rid]))
        require(record.get('worker_id') in (None, -1, worker), f'{rid} 的状态与计算日志实例编号不同。')
        request_rows.append(dict(case=spec['id'], id=rid, worker=worker,
            historical_class=record['profile'].get('class_id', ''), compute_classes='/'.join(sorted(seen[rid])),
            changed_while_computing=len(seen[rid]) > 1,
            ttft_ms=(record['first_token_s']-record['profile']['arrival_s'])*1000))
    fingerprints = {}
    for path, stamp in sources.items():
        require((path.stat().st_size, path.stat().st_mtime_ns) == stamp, f'日志在绘图时发生变化，请等待运行结束：{path}')
        fingerprints[str(path)] = dict(sha256=digest(path), bytes=stamp[0])
    summary = dict(case=spec['id'], label=spec.get('label') or default_label(spec['id'], cfg),
        requests=len(ids), workers=workers, steps=len(steps), layers=len(layers), span_s=span,
        wall_time_scale=scale, rule=classifier.rule, classification_reference_only=classifier.reference_only,
        model_s=math.fsum(r['model_s'] for r in layers), red_s=math.fsum(r['red_s'] for r in accounts),
        gray_s=math.fsum(r['gray_s'] for r in accounts),
        compute_heavy_any=sum('heavy' in value for value in seen.values()),
        compute_light_any=sum('light' in value for value in seen.values()),
        changed_requests=sum(len(value) > 1 for value in seen.values()),
        read_bytes=read_bytes, formal_ids_source=id_source)
    return dict(summary=summary, comp=comp, io=io, layers=layers, requests=request_rows,
                accounts=accounts, sources=fingerprints)


def render(cases, output, title, dpi=160):
    from . import report_style as style
    style.configure()
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.patches import Patch
    output = Path(output)
    horizon = math.ceil(max(case['summary']['span_s'] for case in cases)*1.03*10)/10
    # Keep vertical room proportional to worker count instead of fixing twelve rows.
    ratios = [max(2.2, case['summary']['workers']*.16+.65) for case in cases]
    height = sum(ratios)+1.2
    fig, axes = plt.subplots(len(cases), 1, figsize=(16.5, height), sharex=True,
                             gridspec_kw={'height_ratios': ratios}, squeeze=False)
    fig.subplots_adjust(left=.085, right=.985, bottom=.55/height, top=1-.95/height, hspace=.42)
    for axis_row, case in zip(axes, cases):
        ax = axis_row[0];summary = case['summary'];workers = summary['workers']
        first = {worker:min((r['start'] for r in case['comp'] if r['worker']==worker), default=math.inf) for worker in range(workers)}
        order = sorted(range(workers), key=lambda worker:(first[worker], worker))
        positions = {worker:workers-1-i for i, worker in enumerate(order)}
        ax.set_facecolor(COLORS['idle'])
        for dataset in (case['io'], case['comp']):
            polygons = [];colors = []
            for row in dataset:
                left,right=row['start']*1000,row['end']*1000;y=positions[row['worker']]
                polygons.append([(left,y-.42),(right,y-.42),(right,y+.42),(left,y+.42)])
                colors.append(COLORS[row['state']])
            ax.add_collection(PolyCollection(polygons, facecolors=colors, edgecolors='none', rasterized=True))
        ax.axvline(summary['span_s']*1000,color='#333333',ls='--',lw=1)
        ax.set(yticks=[positions[worker] for worker in order],
            yticklabels=[f'实例{worker}'+('（未执行）' if first[worker]==math.inf else '') for worker in order],
            ylim=(-.6,workers-.4),xlim=(0,horizon*1000),ylabel='按首次计算排序')
        ax.tick_params(axis='y',labelsize=7);ax.grid(axis='x',alpha=.15)
        rule='到达时冻结分类' if summary['rule']=='arrival' else '每层开始时动态分类'
        if summary['classification_reference_only']:rule+='；直接放行，仅作参考'
        ax.set_title(f"{summary['label']}｜完成 {summary['span_s']*1000:.1f} ms｜{rule}",loc='left',fontsize=10)
    axes[-1][0].set_xlabel('从首条正式请求网关到达起的模型时间（ms）；虚线后仅为统一横轴补齐')
    legend=[Patch(color=COLORS[key],label=label) for key,label in [
        ('heavy','重读请求计算'),('light','轻读请求计算'),('mixed','轻重混批计算'),
        ('io','在途IO／无名义计算'),('idle','空闲、排队或控制开销')]]
    fig.suptitle(title,y=1-.13/height,fontsize=12)
    fig.legend(handles=legend,ncol=5,loc='upper center',bbox_to_anchor=(.5,1-.4/height),frameon=False,fontsize=9)
    fig.savefig(output,dpi=dpi,facecolor='white')
    plt.close(fig)


def csvout(path, values):
    require(values, f'没有可写入的数据：{path.name}')
    with Path(path).open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(values[0]));writer.writeheader();writer.writerows(values)
