#!/usr/bin/env python3
"""Text-only trace datasets, immutable schedules, and HTTP completion replay."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import time
import urllib.error
import urllib.request
import uuid

VERSION = '2.0-workload-only'

LEGACY_EMPTY_ERROR = 'ValueError: No generated text in stream; first content time unavailable'

def classify_record(record):
    """Reinterpret only the known v1.0 measurement error; preserve raw JSONL."""
    r=dict(record)
    legacy=(r.get('success') is False and r.get('error')==LEGACY_EMPTY_ERROR
            and isinstance(r.get('http_status'),int) and 200<=r['http_status']<300
            and r.get('finish_reason') in ('length','stop') and r.get('stream_done') is True
            and r.get('content_chunks')==0 and r.get('ttft_ms') is None)
    if legacy:
        r.update(success=True,error=None,legacy_measurement_error_reclassified=True,
                 measurement_warning=LEGACY_EMPTY_ERROR)
    r['request_completed']=bool(r.get('success'))
    r['ttft_available']=r.get('ttft_ms') is not None
    return r

def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(x):
    return hashlib.sha256(canonical(x).encode()).hexdigest()

def read_json(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))

def write_json(p, x):
    Path(p).write_text(json.dumps(x, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')

def open_url(req, timeout):
    # Cluster-local requests must not accidentally traverse a desktop/WSL proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=timeout)

def jsonl(p):
    with Path(p).open(encoding='utf-8') as f:
        for line in f:
            if line.strip(): yield json.loads(line)

def lcp(a, b):
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))

def tokenizer_at(path, trust=False):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(path, trust_remote_code=trust, local_files_only=True)

def tokenizer_identity(path, tok):
    files={}
    for p in sorted(Path(path).glob('*')):
        if p.is_file() and p.name in ('tokenizer.json','tokenizer_config.json','special_tokens_map.json',
                                     'added_tokens.json','vocab.json','merges.txt','chat_template.jinja'):
            files[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
    if not files: raise ValueError('No tokenizer files found; use a local model/tokenizer directory')
    return dict(files=files, cls=type(tok).__name__)

def encode(tok, text, special):
    return list(tok.encode(text, add_special_tokens=special))

def decode(tok, ids):
    return tok.decode(ids, skip_special_tokens=False, clean_up_tokenization_spaces=False)

def safe_pool(tok):
    # Use ordinary text pieces; never send token IDs to the HTTP service.
    candidates=[]; specials=set(tok.all_special_ids)
    for idx in sorted(set(tok.get_vocab().values())):
        if idx in specials: continue
        s=decode(tok,[idx])
        if re.fullmatch(r' [A-Za-z][A-Za-z0-9]{1,18}',s) and encode(tok,s,False)==[idx]:
            candidates.append(idx)
    if len(candidates)<32:
        raise ValueError('Need >=32 stable leading-space text tokens for this tokenizer')
    return candidates

def build_request(tok, pool, h, u, block, rng, special, seen):
    probe=decode(tok,[pool[0],pool[1]])
    bare=encode(tok,probe,False); wrapped=encode(tok,probe,special)
    if wrapped[-len(bare):]!=bare:
        raise ValueError('Tokenizer adds trailing special tokens; this text-prefix generator cannot guarantee exact H/U')
    leading=wrapped[:-len(bare)]
    if h and h<len(leading)+1: raise ValueError('H is too short for tokenizer special-token prefix')
    for _ in range(100):
        prefix=rng.choices(pool,k=max(0,h-len(leading))) if h else []
        first_a,first_b=rng.sample(pool,2)
        full_ids=prefix+[first_b]+rng.choices(pool,k=h+u-len(leading)-len(prefix)-1)
        prompt=decode(tok,full_ids)
        ids=encode(tok,prompt,special)
        if len(ids)!=h+u: continue
        preload=decode(tok,prefix+[first_a]) if h else None
        pre_ids=encode(tok,preload,special) if h else []
        if h and (len(pre_ids)!=h+1 or lcp(pre_ids,ids)!=h): continue
        key=digest(ids[:block])
        if key in seen: continue
        seen.add(key)
        return dict(prompt=prompt,preload_prompt=preload,hit_tokens=h,miss_tokens=u,
                    prompt_tokens=len(ids),preload_tokens=len(pre_ids),common_prefix_tokens=h,
                    reusable_prefix_tokens=h,token_ids_sha256=digest(ids),first_block_sha256=key)
    raise ValueError('Could not obtain exact text roundtrip and prefix boundary after 100 attempts')

def generate(a, tok=None, pool=None):
    if a.hit<0 or a.miss<1 or a.count<1 or a.block_size<1 or a.hit%a.block_size or a.max_tokens<1:
        raise ValueError('Require H>=0, U/count/block>0, and H aligned to block_size')
    tok=tok if tok is not None else tokenizer_at(a.tokenizer,a.trust_remote_code)
    identity=tokenizer_identity(a.tokenizer,tok)
    pool=pool if pool is not None else safe_pool(tok)
    profile=dict(tokenizer=identity,add_special_tokens=a.add_special_tokens,block_size=a.block_size)
    profile_id=digest(profile)
    root=Path(a.dataset);root.mkdir(parents=True,exist_ok=True)
    seen=set()
    for m in root.glob('*/manifest.json'):
        old=read_json(m)
        if old['status']=='complete' and old['profile_id']==profile_id:
            for r in jsonl(m.parent/'requests.jsonl'): seen.add(r['first_block_sha256'])
    batch_id=digest(dict(profile_id=profile_id,hit=a.hit,miss=a.miss,count=a.count,seed=a.seed))[:24]
    out=root/batch_id;out.mkdir(exist_ok=False)
    manifest=dict(version=VERSION,status='generating',batch_id=batch_id,profile_id=profile_id,
                  profile=profile,tokenizer_path=str(Path(a.tokenizer).resolve()),seed=a.seed,
                  hit_tokens=a.hit,miss_tokens=a.miss,count=a.count)
    write_json(out/'manifest.json',manifest)
    rng=random.Random(a.seed)
    try:
        with (out/'requests.jsonl').open('x',encoding='utf-8') as f:
            for i in range(a.count):
                r=build_request(tok,pool,a.hit,a.miss,a.block_size,rng,a.add_special_tokens,seen)
                r.update(profile_id=profile_id,block_size=a.block_size,
                         add_special_tokens=a.add_special_tokens,max_tokens=a.max_tokens)
                r['request_id']='req_'+digest(r)[:32]
                f.write(canonical(r)+'\n');f.flush()
                print(f'{i+1}/{a.count} {r["request_id"]} H={a.hit} U={a.miss}',flush=True)
        manifest.update(status='complete',requests_sha256=hashlib.sha256((out/'requests.jsonl').read_bytes()).hexdigest())
    except BaseException as exc:
        manifest.update(status='failed',error=repr(exc));raise
    finally: write_json(out/'manifest.json',manifest)
    print('DATASET_BATCH='+str(out))

def assemble(a):
    spec=read_json(a.spec); rng=random.Random(spec.get('seed',20261110))
    available=[]
    for p in sorted(Path(a.dataset).glob('*/manifest.json')):
        m=read_json(p)
        if m['status']!='complete': continue
        if hashlib.sha256((p.parent/'requests.jsonl').read_bytes()).hexdigest()!=m['requests_sha256']:
            raise ValueError(f'Dataset hash mismatch: {p}')
        available.extend(jsonl(p.parent/'requests.jsonl'))
    available.sort(key=lambda r:r['request_id']);used=set();rows=[]
    for g in spec['groups']:
        count=g['count']
        if type(count) is not int or count<1: raise ValueError('Group count must be positive integer')
        candidates=[r for r in available if r['hit_tokens']==g['hit'] and r['miss_tokens']==g['miss']
                    and r['request_id'] not in used and (not g.get('profile_id') or r['profile_id']==g['profile_id'])]
        if len(candidates)<count: raise ValueError(f'Not enough unique requests for {g}: available={len(candidates)}')
        chosen=rng.sample(candidates,count)
        if 'arrivals_ms' in g:
            arrivals=g['arrivals_ms']
            if len(arrivals)!=count: raise ValueError('arrivals_ms length must equal count')
        else:
            start=g.get('start_ms',0);duration=g.get('duration_ms',0)
            if duration<0: raise ValueError('Negative duration')
            arrivals=[start+duration*i/max(1,count-1) for i in range(count)]
        for r,at in zip(chosen,arrivals):
            if not isinstance(at,(float,int)) or not math.isfinite(at) or at<0: raise ValueError('Invalid arrival time')
            row=dict(r,request_class=g['class'],arrival_offset_ms=at,slo_ms=g.get('slo_ms'))
            if row['slo_ms'] is not None and (not math.isfinite(row['slo_ms']) or row['slo_ms']<=0):raise ValueError('SLO must be positive')
            rows.append(row);used.add(r['request_id'])
    if not rows or len({r['profile_id'] for r in rows})!=1: raise ValueError('Trace must use exactly one tokenizer/block profile')
    rows.sort(key=lambda r:(r['arrival_offset_ms'],r['request_id']))
    write_trace(a.output, spec, rows)

def write_trace(output, spec, rows):
    content_sha256=digest(rows)
    trace_id='trace_'+content_sha256[:32]
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    (out/'requests.jsonl').write_text(''.join(canonical(r)+'\n' for r in rows),encoding='utf-8')
    write_json(out/'manifest.json',dict(version=VERSION,trace_id=trace_id,count=len(rows),spec=spec,
                profile_id=rows[0]['profile_id'],content_sha256=content_sha256))
    print(f'TRACE_ID={trace_id}\nTRACE={out}')
    return trace_id

def load_trace(path):
    p=Path(path);m=read_json(p/'manifest.json');rows=list(jsonl(p/'requests.jsonl'))
    if len(rows)!=m['count'] or digest(rows)!=m['content_sha256'] or m['trace_id']!='trace_'+digest(rows)[:32]:
        raise ValueError('Trace contents changed after ID was fixed')
    if len({r['request_id'] for r in rows})!=len(rows):raise ValueError('Duplicate request IDs')
    return m,rows

def validate(a):
    m,rows=load_trace(a.trace)
    context=m.get('spec',{}).get('max_model_len')
    if context is not None:
        for r in rows:
            if max(r['prompt_tokens'],r['preload_tokens'])+r['max_tokens']>context:
                raise ValueError('Context limit exceeded: '+r['request_id'])
    if a.tokenizer:
        tok=tokenizer_at(a.tokenizer,a.trust_remote_code)
        for r in rows:
            ids=encode(tok,r['prompt'],r['add_special_tokens'])
            pre=encode(tok,r['preload_prompt'],r['add_special_tokens']) if r['preload_prompt'] else []
            if digest(ids)!=r['token_ids_sha256'] or len(ids)!=r['hit_tokens']+r['miss_tokens'] or lcp(pre,ids)!=r['hit_tokens']:
                raise ValueError('Tokenizer/prefix mismatch: '+r['request_id'])
            if context is not None and (
                    len(ids)!=r['prompt_tokens'] or len(pre)!=r['preload_tokens']
                    or (pre and len(pre)!=r['hit_tokens']+1)
                    or max(len(ids),len(pre))+r['max_tokens']>context):
                raise ValueError('Context or preload length mismatch: '+r['request_id'])
    print(f'VALID trace={m["trace_id"]} count={len(rows)} tokenizer_checked={bool(a.tokenizer)}')

def sse_events(response):
    data=[]
    for raw in response:
        line=raw.decode('utf-8').rstrip('\r\n')
        if not line:
            if data: yield '\n'.join(data);data=[]
        elif line.startswith('data:'): data.append(line[5:].lstrip())
    if data: yield '\n'.join(data)

def send_http(endpoint,model,prompt,max_tokens,timeout,extra,headers,base,request_key,expected_tokens,sse_path=None):
    start=time.perf_counter();first=None;usage=None;response_id=None;finish=None;done=False;chunks=0;choices_seen=0
    payload=dict(model=model,prompt=prompt,max_tokens=max_tokens,temperature=0.0,stream=True)
    if set(extra)&set(payload): raise ValueError('body_extra cannot override model/prompt/max_tokens/temperature/stream')
    payload.update(extra)
    hdr={'Content-Type':'application/json','X-Request-ID':request_key,**headers}
    req=urllib.request.Request(endpoint,data=canonical(payload).encode(),headers=hdr,method='POST')
    status=None;error=None;first_sse=None
    raw_log=Path(sse_path).open('x',encoding='utf-8') if sse_path else None
    try:
        with open_url(req,timeout=timeout) as response:
            status=response.status
            for event in sse_events(response):
                now=time.perf_counter()
                if first_sse is None:first_sse=now
                if raw_log:
                    raw_log.write(canonical(dict(elapsed_ms=(now-start)*1000,data=event))+'\n');raw_log.flush()
                if event=='[DONE]':done=True;break
                obj=json.loads(event)
                if obj.get('error'):raise ValueError(canonical(obj['error']))
                response_id=obj.get('id',response_id)
                if obj.get('usage'):usage=obj['usage']
                for choice in obj.get('choices',[]):
                    choices_seen+=1
                    text=choice.get('text')
                    if text is None:text=(choice.get('delta') or {}).get('content')
                    if text:
                        chunks+=1
                        if first is None:first=time.perf_counter()
                    if choice.get('finish_reason') is not None:finish=choice['finish_reason']
        if not done and finish is None:raise ValueError('Stream closed without DONE or finish_reason')
        if not choices_seen:raise ValueError('Stream ended without any completion choice')
    except Exception as exc:
        if isinstance(exc,urllib.error.HTTPError):status=exc.code
        error=f'{type(exc).__name__}: {exc}'
    finally:
        if raw_log:raw_log.close()
    end=time.perf_counter()
    reported=usage.get('prompt_tokens') if isinstance(usage,dict) else None
    return dict(success=error is None,request_completed=error is None,error=error,http_status=status,response_id=response_id,
                client_request_key=request_key,first_sse_ms=(first_sse-start)*1000 if first_sse is not None else None,
                ttft_available=first is not None,
                measurement_warning='Completed without visible text; TTFT unavailable' if error is None and first is None else None,
                actual_send_ms=(start-base)*1000,first_content_ms=(first-base)*1000 if first else None,
                ttft_ms=(first-start)*1000 if first else None,end_ms=(end-base)*1000,e2e_ms=(end-start)*1000,
                usage=usage,expected_prompt_tokens=expected_tokens,
                usage_length_matches=None if reported is None else reported==expected_tokens,
                content_chunks=chunks,finish_reason=finish,stream_done=done)

def capture_metrics(urls,out,stage,headers):
    for i,url in enumerate(urls):
        try:
            req=urllib.request.Request(url,headers=headers)
            with open_url(req,timeout=10) as r: data=r.read()
            (out/f'metrics_{stage}_{i}.txt').write_bytes(data)
        except Exception as exc:(out/f'metrics_{stage}_{i}.error.txt').write_text(str(exc),encoding='utf-8')

def stats(values):
    if not values:return None
    s=sorted(values)
    def q(p):
        k=(len(s)-1)*p;i=int(k);return s[i]+(s[min(i+1,len(s)-1)]-s[i])*(k-i)
    return dict(n=len(s),mean=statistics.mean(s),median=statistics.median(s),p95=q(.95),p99=q(.99),min=s[0],max=s[-1])

def summarize(out):
    out=Path(out);meta=read_json(out/'run.json');rows=[classify_record(r) for r in jsonl(out/'requests.jsonl')]
    skipped=meta.get('skipped_prepare_requests',[])
    summaries={}
    for name in ['ALL']+sorted({r['class'] for r in rows+skipped}):
        subset=rows if name=='ALL' else [r for r in rows if r['class']==name]
        ok=[r for r in subset if r['success']]
        slo=[r for r in subset if r.get('slo_ms') is not None]
        summaries[name]=dict(attempted=len(subset),succeeded=len(ok),failed=len(subset)-len(ok),
            skipped_prepare_failed=sum(name=='ALL' or r['class']==name for r in skipped),
            ttft_ms=stats([r['ttft_ms'] for r in ok if r['ttft_ms'] is not None]),
            ttft_unavailable=sum(r['ttft_ms'] is None for r in ok),e2e_ms=stats([r['e2e_ms'] for r in ok]),
            send_lateness_ms=stats([r['send_lateness_ms'] for r in subset]),
            slo_pass=sum(r['success'] and r['arrival_to_first_content_ms'] is not None and r['arrival_to_first_content_ms']<=r['slo_ms'] for r in slo),slo_total=len(slo),
            slo_unknown=sum(r['success'] and r['arrival_to_first_content_ms'] is None for r in slo),
            slo_unassigned=len(subset)-len(slo),
            usage_length_mismatches=sum(r['usage_length_matches'] is False for r in subset))
    span=max((r['end_ms'] for r in rows),default=0)
    result=dict(run_id=meta['run_id'],trace_id=meta['trace_id'],status=meta['status'],phase=meta['phase'],
                planned_count=meta['planned_count'],recorded_count=len(rows),finish_from_start_ms=span,
                original_planned_count=meta.get('original_planned_count',meta['planned_count']),
                skipped_prepare_failed=len(skipped),
                success_requests_per_s=sum(r['success'] for r in rows)/(span/1000) if span else None,
                legacy_reclassified=sum(bool(r.get('legacy_measurement_error_reclassified')) for r in rows),groups=summaries)
    write_json(out/'summary.json',result)
    lines=['# Trace运行摘要','',f"Trace：{meta['trace_id']}；Run：{meta['run_id']}；阶段：{meta['phase']}；状态：{meta['status']}。",
           f"原计划{result['original_planned_count']}条；prepare失败跳过{len(skipped)}条；本阶段计划发送{meta['planned_count']}条，已记录{len(rows)}条；开始至最后结束{span:.2f} ms。",
           'TTFT仅统计成功且有可见文本的请求。跳过请求不计为成功；跳过后保留其余请求的原始到达时间。',
           'TTFT从实际发出请求开始；SLO从计划到达时刻开始。正常结束但无可见文本时，完成状态为成功，TTFT缺失；相应SLO未知，不当作已知超时。',
           'TTFT以首个非空生成文本为准；不是纯计算时间。没有服务端证据时不报告实际H/U、batch或16层时间。','',
           '|类别|成功/尝试|TTFT缺失|平均TTFT ms|P95 TTFT ms|平均发送滞后 ms|SLO满足/有目标|SLO未知|无SLO目标|usage长度不符|',
           '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    def v(x,key):return '—' if x is None else f'{x[key]:.2f}'
    for k,r in summaries.items():lines.append(f"|{k}|{r['succeeded']}/{r['attempted']}|{r['ttft_unavailable']}|{v(r['ttft_ms'],'mean')}|{v(r['ttft_ms'],'p95')}|{v(r['send_lateness_ms'],'mean')}|{r['slo_pass']}/{r['slo_total']}|{r['slo_unknown']}|{r['slo_unassigned']}|{r['usage_length_mismatches']}|")
    if result['legacy_reclassified']:lines+=['',f"旧版空文本误判重分类：{result['legacy_reclassified']}条；未改写原requests.jsonl，原run状态仍保留。"]
    if skipped:lines+=['',f"本轮负载有缺失：{len(skipped)}条因prepare请求失败未发送。详见skipped_requests.jsonl；对比策略时同时报告跳过数量和类别。"]
    lines+=['','metrics文件保留服务原始输出，快照覆盖整个运行阶段；服务端并发其他业务时不能归因于本trace。',
            '缓存准备记录仅证明前缀请求完成。存储持久化、设备缓存清理和实际命中未自动验证。']
    (out/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if rows:
        with (out/'requests.csv').open('w',encoding='utf-8-sig',newline='') as f:
            keys=[k for k in rows[0] if k!='usage'];w=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore');w.writeheader();w.writerows(rows)
    return result

async def run_async(a):
    tm,rows=load_trace(a.trace);cfg=read_json(a.config)
    selected=[r for r in rows if not a.classes or r['request_class'] in a.classes]
    if not selected:raise ValueError('No requests selected')
    spec=tm.get('spec',{});single_use=spec.get('single_use_replay',False)
    endpoint=cfg['endpoint'];model=cfg.get('model','minimax');mode=cfg.get('mode','paced')
    limit=cfg.get('concurrency',32);timeout=cfg.get('timeout_s',600)
    scale=cfg.get('time_scale',1);order=cfg.get('order','original')
    if mode not in ('serial','concurrent','paced','burst') or type(limit) is not int or limit<1 or timeout<=0 or not math.isfinite(scale) or scale<=0:raise ValueError('Invalid run configuration')
    if order not in ('original','shuffle'):raise ValueError('Unknown order')
    if mode=='paced' and order!='original':raise ValueError('Shuffle is only supported outside paced mode')
    if order=='shuffle':random.Random(cfg.get('seed',20261110)).shuffle(selected)
    if a.phase=='prepare':mode='concurrent';selected=[r for r in selected if r['hit_tokens']>0]
    if not selected:raise ValueError('No prefixes require preparation')
    original_planned_count=len(selected)
    skipped=[]
    if a.prepared_run:
        prep=read_json(Path(a.prepared_run)/'run.json')
        prep_rows=[classify_record(r) for r in jsonl(Path(a.prepared_run)/'requests.jsonl')]
        good={r['request_id'] for r in prep_rows if r['success']}
        prep_by_id={r['request_id']:r for r in prep_rows}
        if (prep['phase']!='prepare' or prep['trace_id']!=tm['trace_id']
                or len(prep_by_id)!=len(prep_rows)
                or any(r['hit_tokens'] and r['request_id'] not in prep_by_id for r in selected)):
            raise ValueError('Preparation record does not cover this trace selection')
        if prep['config'].get('model','minimax')!=model:raise ValueError('Preparation used another served model')
        for r in selected:
            if r['hit_tokens'] and r['request_id'] not in good:
                failed=prep_by_id[r['request_id']]
                skipped.append(dict(request_id=r['request_id'],logical_id=r.get('logical_id'),
                    **{'class':r['request_class']},reason='prepare_request_failed',
                    preparation_error=failed.get('error'),preparation_http_status=failed.get('http_status'),
                    expected_hit_tokens=r['hit_tokens'],expected_miss_tokens=r['miss_tokens'],
                    original_arrival_offset_ms=r['arrival_offset_ms']))
        selected=[r for r in selected if not r['hit_tokens'] or r['request_id'] in good]
    if a.phase=='replay' and cfg.get('cache_condition')=='storage' and (not a.prepared_run or not a.cache_ready_note):
        raise ValueError('Storage replay needs --prepared-run and --cache-ready-note describing save/device-cache checks')
    headers=dict(cfg.get('headers',{}))
    if cfg.get('api_key_env'):
        headers['Authorization']='Bearer '+os.environ[cfg['api_key_env']]
    extra=cfg.get('body_extra',{})
    if set(extra)&{'model','prompt','max_tokens','temperature','stream'}:raise ValueError('Reserved body_extra key')
    run_id='run_'+uuid.uuid4().hex
    out=Path(a.output) if a.output else Path(__file__).resolve().parent/'results'/run_id
    out.mkdir(parents=True,exist_ok=False)
    if single_use and a.phase=='replay':
        claim=Path(a.trace)/'.replay_claim.json'
        try:
            with claim.open('x',encoding='utf-8') as f:
                f.write(canonical(dict(run_id=run_id,output=str(out.resolve()),started_utc=datetime.now(timezone.utc).isoformat())))
        except FileExistsError:
            raise ValueError('This trace has already been claimed for replay; generate fresh prefixes, do not delete the claim')
    safe_cfg=dict(cfg);safe_cfg['headers']={k:'<redacted>' for k in cfg.get('headers',{})}
    meta=dict(version=VERSION,run_id=run_id,trace_id=tm['trace_id'],phase=a.phase,status='running',
              planned_count=len(selected),started_utc=datetime.now(timezone.utc).isoformat(),config=safe_cfg,
              original_planned_count=original_planned_count,skipped_prepare_requests=skipped,
              preparation_run=a.prepared_run,
              preparation_legacy_reclassified_ids=[r['request_id'] for r in prep_rows if r.get('legacy_measurement_error_reclassified')] if a.prepared_run else [],
              cache_condition=cfg.get('cache_condition','unspecified'),
              cache_state_verified=False,cache_ready_note=a.cache_ready_note,
              script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    meta.update(workload_id=spec.get('workload_id'), label=getattr(a,'label',''))
    write_json(out/'run.json',meta);write_json(out/'trace_manifest.json',tm)
    (out/'skipped_requests.jsonl').write_text(''.join(canonical(r)+'\n' for r in skipped),encoding='utf-8')
    save_sse=cfg.get('save_sse',False)
    if save_sse:(out/'sse').mkdir()
    print('OUTPUT='+str(out),flush=True)
    metrics=cfg.get('metrics_urls',[])
    loop=asyncio.get_running_loop();pool=ThreadPoolExecutor(max_workers=1 if mode=='serial' else limit)
    log=(out/'requests.jsonl').open('x',encoding='utf-8');sem=asyncio.Semaphore(1 if mode=='serial' else limit)
    tasks=[];outcomes=[]
    try:
        await loop.run_in_executor(pool,capture_metrics,metrics,out,'before',headers)
        warmup_count=cfg.get('warmup_count',0) if selected else 0
        if type(warmup_count) is not int or warmup_count<0:raise ValueError('warmup_count must be a nonnegative integer')
        warmups=[]
        for i in range(warmup_count):
            row=await loop.run_in_executor(pool,send_http,endpoint,model,f'Warmup query {i}: Hello',1,
                    timeout,extra,headers,time.perf_counter(),f'{run_id}:warmup{i}',None)
            warmups.append(row);write_json(out/'warmup.json',warmups)
            if not row['success']:raise RuntimeError('Warmup HTTP request failed; inspect warmup.json')
        base=time.perf_counter()
        async def one(r,planned):
            if mode=='paced':await asyncio.sleep(max(0,base+planned/1000-time.perf_counter()))
            async with sem:
                prompt=r['preload_prompt'] if a.phase=='prepare' else r['prompt']
                expected=r['preload_tokens'] if a.phase=='prepare' else r['prompt_tokens']
                key=f'{run_id}:{r["request_id"]}'
                result=await loop.run_in_executor(pool,send_http,endpoint,model,prompt,
                    1 if a.phase=='prepare' else r['max_tokens'],timeout,extra,headers,base,key,expected,
                    out/'sse'/(hashlib.sha256(key.encode()).hexdigest()+'.jsonl') if save_sse else None)
                arrival_first=(result['first_content_ms']-planned) if result['first_content_ms'] is not None else None
                result.update(request_id=r['request_id'],class_=r['request_class'])
                result['class']=result.pop('class_')
                result.update(expected_hit_tokens=r['hit_tokens'],expected_miss_tokens=r['miss_tokens'],
                    planned_arrival_ms=planned,send_lateness_ms=max(0,result['actual_send_ms']-planned),
                    arrival_to_first_content_ms=arrival_first,slo_ms=r.get('slo_ms') if a.phase=='replay' else None)
                result.update(logical_id=r.get('logical_id'),first_block_sha256=r.get('first_block_sha256'))
                outcomes.append(result['success'])
                log.write(canonical(result)+'\n');log.flush()
                print(f"{r['request_class']} {r['request_id']} success={result['success']} TTFT={result['ttft_ms']}",flush=True)
        for r in selected:
            planned=r['arrival_offset_ms']*scale if mode=='paced' else 0
            if mode=='serial':await one(r,planned)
            else:tasks.append(asyncio.create_task(one(r,planned)))
        if tasks:await asyncio.gather(*tasks)
        meta['status']='complete' if all(outcomes) else 'complete_with_request_errors'
        if skipped:
            meta['status']=('complete_with_skipped_requests' if all(outcomes)
                            else 'complete_with_request_errors_and_skips') if selected else 'no_requests_sent'
    except BaseException as exc:
        meta.update(status='failed',error=repr(exc));raise
    finally:
        for t in tasks:
            if not t.done():t.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        # Drain only this client's in-flight HTTP work before finalizing records.
        pool.shutdown(wait=True,cancel_futures=True);log.close()
        capture_metrics(metrics,out,'after',headers)
        write_json(out/'run.json',meta)
        result=summarize(out)
        print('SUMMARY='+str(out/'summary.md'),flush=True)
    if result['recorded_count']!=result['planned_count']:
        raise RuntimeError('Requests failed or missing; inspect requests.jsonl and summary.md')
    if result['groups']['ALL']['failed']:
        print(f"WARNING: {result['groups']['ALL']['failed']} {a.phase} requests failed; results saved, no automatic retry",flush=True)
