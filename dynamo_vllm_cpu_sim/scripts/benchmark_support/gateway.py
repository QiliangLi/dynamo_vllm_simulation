"""实验专用网关启动适配；固定输入预分词，两组同样使用512连接/600秒超时。"""
import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import runpy
import sys
import time

import httpx
from transformers import AutoTokenizer

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from sim.request_profile import build_profile,normalize_config
folder=Path(os.environ['SIM_STACK_CONFIG']).parent
started=time.monotonic()
tokenizer=AutoTokenizer.from_pretrained(str(ROOT/'configs/model'))
cache={}
rows=[json.loads(s) for s in (folder/'trace.jsonl').read_text().splitlines()]
cfg=normalize_config(json.loads((folder/'config.json').read_text()))
profiles={}
source_rows={r['id']:r for r in rows}
for r in rows:
    text=tokenizer.decode(r['prompt_token_ids'])
    assert tokenizer.encode(text)==r['prompt_token_ids']
    if text in cache:
        assert cache[text]==r['prompt_token_ids']
    cache[text]=r['prompt_token_ids']
    row=dict(r,arrival_s=0,class_id=r.get('class_id','unclassified'))
    p=build_profile(row,cfg)
    # Validate the frozen fields and deadline shift before measured arrivals.
    expected=build_profile(dict(row,arrival_s=1),cfg)
    assert replace(p,arrival_s=1,deadline_s=1+cfg['request_profile']['slo_multiplier']*p.reference_service_s)==expected
    profiles[r['id']]=p


class FrozenTokenizer:
    def encode(self,text,*a,**kw):
        if not a and not kw and text in cache:
            return cache[text]
        return tokenizer.encode(text,*a,**kw)

    def __getattr__(self,name):
        return getattr(tokenizer,name)


proxy=FrozenTokenizer()
AutoTokenizer.from_pretrained=lambda *a,**kw:proxy
original_client=httpx.AsyncClient


class ExperimentClient(original_client):
    def __init__(self,*a,**kw):
        kw.update(timeout=600*cfg.get("wall_time_scale",1),limits=httpx.Limits(max_connections=512,max_keepalive_connections=64))
        super().__init__(*a,**kw)


httpx.AsyncClient=ExperimentClient
(folder/'gateway.preparation.json').write_text(json.dumps(dict(
    frozen_requests=len(rows),unique_prompts=len(cache),roundtrip_passed=True,
    frozen_profiles=len(profiles),profile_shift_validated=True,
    preparation_s=time.monotonic()-started,max_connections=512,http_timeout_s=600,
    adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2)+'\n')
print('experiment gateway: frozen tokenization validated; 512 connections; 600s timeout',flush=True)
import sim.stack.admission_service as service


def cached_profile(row,config):
    p=profiles.get(row['id'])
    if p is None:
        return build_profile(row,config)
    assert len(row['prompt_token_ids'])==p.prompt_tokens
    assert row['remote_prefix_tokens']==p.estimated_remote_tokens
    assert row['class_id']==p.class_id
    return replace(p,arrival_s=row['arrival_s'],
                   deadline_s=row['arrival_s']+config['request_profile']['slo_multiplier']*p.reference_service_s)


service.build_profile=cached_profile
from sim.stack.storage_client import StorageClient


def bind_reference(self,engine_id,source_id,prompt_token_ids,hit_tokens):
    r=source_rows[source_id]
    assert prompt_token_ids==r['prompt_token_ids'] and hit_tokens==r['hit_tokens']
    return self._call(dict(op='bind',engine_id=engine_id,source_id=source_id,
                           prompt_token_ids=None,hit_tokens=hit_tokens))


StorageClient.bind=bind_reference
service.main()
