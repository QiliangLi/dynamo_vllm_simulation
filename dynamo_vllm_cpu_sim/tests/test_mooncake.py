"""首现规则与缓存池隔离，不变量覆盖 D1/D2/D3/D7。"""
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from sim.mooncake import derive_rows, derive_file, export_stack_rows, engine_key, validate_hit_row


def row(ts,hs,n=None):
    return dict(timestamp=ts,input_length=512*len(hs) if n is None else n,
                output_length=1,hash_ids=hs)


def hits(rows):
    return [r['hit_tokens'] for _,r in derive_rows(rows)]


class FirstSeenTests(unittest.TestCase):
    def test_first_and_second_seen_continuous_prefix(self):
        self.assertEqual(hits([row(0,[1,2]),row(1,[1,3])]),[0,512])

    def test_suffix_after_first_miss_not_counted_but_enters_history(self):
        self.assertEqual(hits([row(0,[1,2]),row(1,[3,1]),row(2,[3,1])]),[0,0,1023])

    def test_same_request_duplicates_are_invisible(self):
        self.assertEqual(hits([row(0,[1,1])]),[0])

    def test_timestamp_group_is_atomic_and_stably_sorted(self):
        result = derive_rows([row(1,[1,4]),row(0,[1,2]),row(0,[1,3])])
        self.assertEqual([line for line,_ in result],[2,3,1])
        self.assertEqual([r['hit_tokens'] for _,r in result],[0,0,512])

    def test_tail_participates_and_full_hit_retains_one_token(self):
        result = derive_rows([row(0,[1,2],700),row(1,[1,2],700)])
        self.assertEqual(result[1][1]['hit_tokens'],699)
        self.assertEqual(result[1][1]['u_tokens'],1)
        self.assertTrue(result[1][1]['full_hit_adjusted'])
        self.assertEqual(hits([row(0,[1]),row(1,[1,2],700)]),[0,512])
        self.assertEqual(hits([row(0,[1,2]),row(1,[1,2])]),[0,1023])

    def test_each_file_has_independent_history(self):
        self.assertEqual(hits([row(0,[1])]),[0])
        self.assertEqual(hits([row(0,[1])]),[0])

    def test_derivation_preserves_fields_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,output = Path(tmp)/'source',Path(tmp)/'derived'
            source.write_text(json.dumps(dict(row(0,[1]),extra='keep'))+'\n')
            original = source.read_bytes()
            derive_file(source,output)
            derive_file(source,output,check_only=True)
            self.assertEqual(json.loads(output.read_text())['extra'],'keep')
            self.assertEqual(source.read_bytes(),original)
            output.write_text('{}\n')
            with self.assertRaises(ValueError):
                derive_file(source,output,check_only=True)
            with self.assertRaises(ValueError):
                derive_file(source,source)

    def test_selection_after_full_scan_and_arrival_group_preserved(self):
        full = derive_rows([row(0,[1]),row(0,[2]),row(1,[1]),row(1,[2])])
        exported = export_stack_rows(full,'f',[1,2,3],lines=[3,4],time_scale=.1)
        self.assertEqual([r['hit_tokens'] for r in exported],[511,511])
        self.assertEqual([r['arrival_s'] for r in exported],[0,0])

    def test_invalid_rows_rejected(self):
        for rows in ([],[row(0,[],0)],[row(-1,[1])],[row(0,[1],700)]):
            with self.assertRaises(ValueError):
                derive_rows(rows)

    def test_blank_lines_cannot_change_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'
            source.write_text(json.dumps(row(0,[1]))+'\n\n')
            with self.assertRaises(ValueError):
                derive_file(source,Path(tmp)/'derived')

    def test_nonfinite_time_scale_rejected(self):
        for scale in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                export_stack_rows(derive_rows([row(0,[1])]),'f',[1],time_scale=scale)

    def test_engine_suffix_forms_use_same_uuid(self):
        identity = uuid.uuid4()
        for suffix in ('','_0','-99626d23'):
            self.assertEqual(engine_key(str(identity)+suffix),identity.hex)


class FirstSeenStorageTests(unittest.TestCase):
    def setUp(self):
        from sim.stack.storage_service import Service
        self.cold = dict(id='cold',arrival_s=0,prompt_token_ids=[1,2,3,4,5],output_tokens=1,
                         hit_tokens=0,u_tokens=5,full_hit_adjusted=False,
                         remote_prefix_tokens=0,cache_model='first_seen')
        self.hot = dict(self.cold,id='hot',hit_tokens=4,u_tokens=1,
                        full_hit_adjusted=True,remote_prefix_tokens=4)
        self.service = Service(dict(block_size=1,cache_model='first_seen',
            enable_prefix_caching=False,storage=dict(disks=3,paths_per_disk=4,
                disk_Bps=40e9,shared_link_Bps=None,path_Bps=1e9,
                kv_bytes_per_token=100,read_latency_s=0)),[self.cold,self.hot])
        self.cold_id,self.hot_id = str(uuid.uuid4()),str(uuid.uuid4())
        for identity,r in ((self.cold_id,self.cold),(self.hot_id,self.hot)):
            self.service.op_bind(identity,r['id'],r['prompt_token_ids'],r['hit_tokens'])
        self.blocks = [b.hex() for b in self.service._trace_hashes['hot'][:4]]

    def test_future_preloaded_shared_blocks_cannot_create_cold_hit(self):
        self.assertTrue(all(bytes.fromhex(h) in self.service.store.present for h in self.blocks))
        cold = self.service.op_present_for_request(self.cold_id+'-abc',0,self.blocks)
        hot = self.service.op_present_for_request(self.hot_id+'_0',0,self.blocks)
        self.assertEqual(cold['present'],[False]*4)
        self.assertEqual(hot['present'],[True]*4)
        with self.assertRaises(ValueError):
            self.service.op_present(self.blocks)
        with self.assertRaises(ValueError):
            self.service.op_submit(0,self.cold_id+'-abc',self.blocks,None)

    def test_contract_binding_and_query_validation(self):
        with self.assertRaises(ValueError):
            self.service.op_bind(str(uuid.uuid4()),'cold',[9],0)
        with self.assertRaises(ValueError):
            self.service.op_bind(self.cold_id,'cold',self.cold['prompt_token_ids'],0)
        with self.assertRaises(KeyError):
            self.service.op_present_for_request(str(uuid.uuid4()),0,self.blocks)
        with self.assertRaises(ValueError):
            self.service.op_present_for_request(self.hot_id,0,list(reversed(self.blocks)))
        self.assertEqual(self.service.op_present_for_request(self.hot_id,2,self.blocks[2:])['present'],[True,True])

    def test_alignment_and_local_cache_cannot_silently_change_hit_count(self):
        validate_hit_row(self.hot,1)
        with self.assertRaises(ValueError):
            validate_hit_row(self.hot,16)
        from sim.request_profile import normalize_config
        cfg = json.loads((Path(__file__).resolve().parents[1]/'configs/tfs_stack_prefill.json').read_text())
        cfg.update(cache_model='first_seen',enable_prefix_caching=True)
        with self.assertRaises(ValueError):
            normalize_config(cfg)


if __name__ == '__main__':
    unittest.main()
