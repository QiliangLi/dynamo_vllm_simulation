"""Fingerprint-checked optional bridge on top of the existing scorer overlay."""
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def apply(source):
    pins = json.loads((HERE/'source_hashes.json').read_text())
    for name, expected in pins.items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest() != expected:
            raise ValueError('unsupported Dynamo tier source: ' + name)
    def edit(name, old, new, count=1):
        path = source/name
        text = path.read_text()
        assert text.count(old) == count, (name, text.count(old), old)
        path.write_text(text.replace(old, new))

    router = 'lib/llm/src/kv_router.rs'
    edit('lib/kv-router/src/scheduling/config.rs',
         '        self.use_kv_events && self.overlap_score_credit > 0.0',
         '''        self.use_kv_events && (self.overlap_score_credit > 0.0
            || std::env::var_os("DYN_TIER_PROFILE_URL").is_some())''')
    edit('lib/llm/src/kv_router/indexer/mod.rs',
         '    if config.overlap_score_credit <= 0.0 {',
         '''    if config.overlap_score_credit <= 0.0
        && std::env::var_os("DYN_TIER_PROFILE_URL").is_none() {''')
    edit(router, 'pub enum FindBestMatchOutcome {', 'mod tier_admission;\n\npub enum FindBestMatchOutcome {')
    edit(router, '        router_hint: Option<RouterHint>,',
         '        router_hint: Option<RouterHint>,\n        tier_snapshot: Option<serde_json::Value>,')
    edit(router, '        let router_hint_candidates = retain_router_hint_chain',
         '''        let tier_prepared = if is_admitted_routing && update_states && tier_admission::enabled() {
            let snapshots = {
                let workers = self.workers_with_configs.borrow();
                workers.iter().map(|(id, config)| {
                    let worker = WorkerWithDpRank::new(*id,
                        dynamo_kv_router::protocols::WorkerConfigLike::data_parallel_start_rank(config));
                    let tiers = overlap.selected_worker_tiers(worker, config);
                    tier_admission::snapshot(&tiers, worker, self.block_size, isl_tokens).expect("enabled tier profile")
                }).collect()
            };
            Some(tier_admission::prepare(context_id.expect("tracked request ID"), snapshots).await?)
        } else { None };
        let router_hint_candidates = retain_router_hint_chain''')
    edit(router, '        let router_hint = if is_admitted_routing {',
         '''        let tier_snapshot = tier_prepared.as_ref().map(|p| p.selection(response.best_worker));
        let router_hint = if is_admitted_routing {''')
    edit(router, '                        router_hint,\n                    },',
         '                        router_hint,\n                        tier_snapshot,\n                    },')

    selection = 'lib/llm/src/kv_router/routing_host/kv_selection.rs'
    edit(selection, '    pub(super) router_hint: Option<RouterHint>,',
         '    pub(super) router_hint: Option<RouterHint>,\n    pub(super) tier_snapshot: Option<serde_json::Value>,')
    edit(selection, '                    router_hint,', '                    router_hint,\n                    tier_snapshot,', count=2)
    edit(selection, '                    router_hint: None,',
         '                    router_hint: None,\n                    tier_snapshot: None,')

    dispatch = 'lib/llm/src/kv_router/routing_host/kv.rs'
    anchor = '        let updated_request = context.map(|_| backend_input);'
    edit(dispatch, anchor, '''        let tier_result = crate::kv_router::tier_admission::annotations(
            &context_id, selection.tier_snapshot.as_ref(), selection.worker, selection.attempt).await;
        match tier_result {
            Ok(Some(annotations)) => {
                if backend_input.annotations.iter().any(|a|
                    a.starts_with("storage_hint_v1:") || a.starts_with("edf_priority_v1:")) {
                    guard.abort().await;
                    return Err(anyhow::anyhow!("caller supplied reserved admission metadata"));
                }
                backend_input.annotations.extend(annotations);
            }
            Ok(None) => {}
            Err(error) => { guard.abort().await; return Err(error); }
        }
'''+anchor)

    pending = 'lib/kv-router/src/pending_score.rs'
    edit('lib/kv-router/src/lib.rs', 'pub mod pending_score;',
         'pub mod pending_score;\n#[cfg(feature = "standalone-selection")]\npub mod candidate_profiles;')
    edit(pending, 'fn canonical(raw:', 'pub(crate) fn canonical(raw:')
    edit(pending, 'pub fn initialize()', '''pub fn waiting_factor()->f64 {
    service().map(|s|s.ledger.lock().unwrap().factor).unwrap_or(1.)
}
pub fn initialize()''')
    edit(pending, '''    s.ledger.lock().unwrap().bind(&id,worker,attempt,tokens)''', '''    if std::env::var_os("DYN_TIER_PROFILE_URL").is_some() {
        let mark=crate::candidate_profiles::lookup(raw,worker).ok_or("selected worker lacks pre-score classification")?;
        let mut ledger=s.ledger.lock().unwrap();
        if mark.heavy {ledger.register(&id,tokens)?;}
        ledger.bind(&id,worker,attempt,tokens)?;
        tracing::debug!(request_id=raw, worker_id=worker.worker_id, dp_rank=worker.dp_rank,
            heavy=mark.heavy, "TIER_PROFILE_BOOKED");
        return Ok(());
    }
    s.ledger.lock().unwrap().bind(&id,worker,attempt,tokens)''')
    selector = 'lib/kv-router/src/scheduling/selector/default.rs'
    edit(selector, '        let decode_cost_blocks = (load.decode_cost_blocks - waiting_discount).max(0.0);', '''        let mut own_prefill_credit=0.;
        let mut own_decode_credit=0.;
        #[cfg(feature = "standalone-selection")]
        let mut own_mark=None;
        #[cfg(feature = "standalone-selection")]
        if std::env::var_os("DYN_TIER_PROFILE_URL").is_some() && context.request.mode.is_tracked() {
            let Some(mark)=crate::candidate_profiles::lookup(context.request_id, worker) else {
                return f64::MAX;
            };
            if mark.heavy {
                let factor=crate::pending_score::waiting_factor();
                let own_prefill=(load.raw_prefill_blocks-load.active_prefill_tokens as f64/context.block_size as f64).max(0.);
                let own_decode=context.request.worker_load_for(worker).additional_active_blocks as f64;
                own_prefill_credit=(1.-factor)*own_prefill.min((load.raw_prefill_blocks-overlap_credit_blocks-waiting_discount).max(0.));
                own_decode_credit=(1.-factor)*own_decode.min((load.decode_cost_blocks-waiting_discount).max(0.));
            }
            own_mark=Some(mark);
        }
        let decode_cost_blocks = (load.decode_cost_blocks - waiting_discount - own_decode_credit).max(0.0);''')
    edit(selector, 'let adjusted_prefill_blocks = (load.raw_prefill_blocks - overlap_credit_blocks - waiting_discount).max(0.0);',
         'let adjusted_prefill_blocks = (load.raw_prefill_blocks - overlap_credit_blocks - waiting_discount - own_prefill_credit).max(0.0);')
    edit(selector, '        let logit = prefill_cost_blocks + decode_cost_blocks + active_request_cost_blocks;', '''        let logit = prefill_cost_blocks + decode_cost_blocks + active_request_cost_blocks;
        #[cfg(feature = "standalone-selection")]
        if let Some(mark)=own_mark {
            let baseline=weights.prefill_load_scale*(load.raw_prefill_blocks-overlap_credit_blocks-waiting_discount).max(0.)
                +(load.decode_cost_blocks-waiting_discount).max(0.)+active_request_cost_blocks;
            tracing::debug!(request_id=context.request_id, preview_id=mark.preview_id,
                "TIER_CANDIDATE_SCORE worker_id={} dp_rank={} heavy={} own_prefill_credit={} own_decode_credit={} baseline={} weighted={}",
                worker.worker_id,worker.dp_rank,mark.heavy,own_prefill_credit,own_decode_credit,baseline,logit);
        }''')
    edit(selector, 'discount={} scale={} overlap={} weighted={}',
         'discount={} scale={} overlap={} weighted={} own_prefill_credit={} own_decode_credit={}')
    edit(selector, 'waiting_discount, weights.prefill_load_scale, overlap_credit_blocks, logit);',
         'waiting_discount, weights.prefill_load_scale, overlap_credit_blocks, logit, own_prefill_credit, own_decode_credit);')
    shutil.copy2(HERE/'tier_admission.rs', source/'lib/llm/src/kv_router/tier_admission.rs')
    shutil.copy2(HERE/'candidate_profiles.rs', source/'lib/kv-router/src/candidate_profiles.rs')
    paths = [*pins, pending, selector, 'lib/kv-router/src/lib.rs',
             'lib/kv-router/src/candidate_profiles.rs', 'lib/llm/src/kv_router/tier_admission.rs']
    return {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in paths}
