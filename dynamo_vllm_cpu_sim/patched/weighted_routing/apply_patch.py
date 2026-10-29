"""Apply a fingerprinted experiment overlay to a disposable Dynamo source copy."""
import argparse,hashlib,json,shutil
from pathlib import Path
HERE=Path(__file__).resolve().parent

def apply(source):
 pins=json.loads((HERE/'source_hashes.json').read_text())
 for name,expected in pins.items():
  assert hashlib.sha256((source/name).read_bytes()).hexdigest()==expected, f'unsupported Dynamo source: {name}'
 def edit(name,old,new,count=1):
  p=source/name;s=p.read_text();assert s.count(old)==count,(name,s.count(old),old)
  p.write_text(s.replace(old,new))
 edit('lib/kv-router/src/lib.rs','mod active_set;','mod active_set;\n#[cfg(feature = "standalone-selection")]\npub mod pending_score;')
 selector='lib/kv-router/src/scheduling/selector/default.rs'
 edit(selector,'pub fn new(kv_router_config: Option<KvRouterConfig>, worker_type: &\'static str) -> Self {',
  'pub fn new(kv_router_config: Option<KvRouterConfig>, worker_type: &\'static str) -> Self {\n        #[cfg(feature = "standalone-selection")]\n        crate::pending_score::initialize();')
 edit(selector,'let decode_cost_blocks = load.decode_cost_blocks;',
  '''#[cfg(feature = "standalone-selection")]
        let waiting_discount = crate::pending_score::discount(worker, context.block_size);
        #[cfg(not(feature = "standalone-selection"))]
        let waiting_discount = 0.0;
        let decode_cost_blocks = (load.decode_cost_blocks - waiting_discount).max(0.0);''')
 edit(selector,'let adjusted_prefill_blocks = (load.raw_prefill_blocks - overlap_credit_blocks).max(0.0);',
  'let adjusted_prefill_blocks = (load.raw_prefill_blocks - overlap_credit_blocks - waiting_discount).max(0.0);')
 edit(selector,'let logit = prefill_cost_blocks + decode_cost_blocks + active_request_cost_blocks;',
  '''let logit = prefill_cost_blocks + decode_cost_blocks + active_request_cost_blocks;
        #[cfg(feature = "standalone-selection")]
        if waiting_discount > 0.0 {
            tracing::debug!(request_id = context.request_id,
                "WAITING_SCORE worker_id={} raw_prefill={} raw_decode={} discount={} scale={} overlap={} weighted={}",
                worker.worker_id, load.raw_prefill_blocks, load.decode_cost_blocks,
                waiting_discount, weights.prefill_load_scale, overlap_credit_blocks, logit);
        }''')
 queue='lib/kv-router/src/scheduling/queue.rs'
 anchor='''        if let Some(transfer) = lifecycle_transfer {
            transfer.arm_booking(SchedulerBookingDescriptor {'''
 edit(queue,anchor,'''        #[cfg(feature = "standalone-selection")]
        if let Err(error) = crate::pending_score::bind(&request_id, worker, attempt_id, request.isl_tokens) {
            let _ = self.slots.free_if_booking(&request_id, worker, attempt_id, Instant::now());
            request.respond(Err(KvSchedulerError::BookingFailed(error)));
            return false;
        }
'''+anchor)
 maps='lib/kv-router/src/sequences/request_maps.rs'
 edit(maps,'.map(|(_request_id, booking)| booking.worker);', '''.map(|(_request_id, booking)| {
                #[cfg(feature = "standalone-selection")]
                crate::pending_score::finish(request_id, booking.worker, booking.attempt_id);
                booking.worker
            });''')
 edit(maps,'''.remove_if(request_id, |_, booking| booking.worker == worker)
            .is_some();''','''.remove_if(request_id, |_, booking| booking.worker == worker)
            .map(|(_, booking)| {
                #[cfg(feature = "standalone-selection")]
                crate::pending_score::finish(request_id, booking.worker, booking.attempt_id);
            })
            .is_some();''')
 edit(maps,'''booking.worker == worker && booking.attempt_id == attempt_id
            })
            .is_some();''','''booking.worker == worker && booking.attempt_id == attempt_id
            })
            .map(|(_, booking)| {
                #[cfg(feature = "standalone-selection")]
                crate::pending_score::finish(request_id, booking.worker, booking.attempt_id);
            })
            .is_some();''')
 shutil.copy2(HERE/'pending_score.rs',source/'lib/kv-router/src/pending_score.rs')
 return {name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in [*pins,'lib/kv-router/src/pending_score.rs']}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--source',required=True,type=Path);p.add_argument('--manifest',required=True,type=Path);a=p.parse_args()
 a.manifest.write_text(json.dumps(dict(base_commit='b83b1d9304ebfc624709ac46db32b1b6f1ff1615',patched=apply(a.source)),indent=2)+'\n')
