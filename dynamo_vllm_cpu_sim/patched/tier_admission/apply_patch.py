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
    def edit(name, old, new, count=1, *, within=None):
        path = source/name
        text = path.read_text()
        before = ''
        if within:
            i = text.index(within)
            j = text.index(old, i)
            before, text = text[:j], text[j:]
            count = 1
            path.write_text(before + text.replace(old, new, count))
        else:
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
         '        router_hint: Option<RouterHint>,\n        tier_annotations: Option<Vec<String>>,')
    edit(router, '        let router_hint_candidates = retain_router_hint_chain',
         '''        let tier_profile = if is_admitted_routing && update_states && tier_admission::enabled() {
            let snapshot=tier_admission::snapshot(&overlap,self.block_size,isl_tokens);
            Some(tier_admission::resolve(context_id.expect("tracked request ID"),snapshot).await?)
        } else { None };
        let router_hint_candidates = retain_router_hint_chain''')
    edit(router, '        let router_hint = if is_admitted_routing {',
         '''        let tier_annotations = tier_profile.as_ref().map(|p| p.annotations.clone());
        let router_hint = if is_admitted_routing {''')
    edit(router, '                        router_hint,\n                    },',
         '                        router_hint,\n                        tier_annotations,\n                    },')

    selection = 'lib/llm/src/kv_router/routing_host/kv_selection.rs'
    edit(selection, '    pub(super) router_hint: Option<RouterHint>,',
         '    pub(super) router_hint: Option<RouterHint>,\n    pub(super) tier_annotations: Option<Vec<String>>,')
    edit(selection, '                    router_hint,', '                    router_hint,\n                    tier_annotations,', count=2)
    edit(selection, '                    router_hint: None,',
         '                    router_hint: None,\n                    tier_annotations: None,')

    dispatch = 'lib/llm/src/kv_router/routing_host/kv.rs'
    anchor = '        let updated_request = context.map(|_| backend_input);'
    edit(dispatch, anchor, '''        if let Some(annotations) = selection.tier_annotations.as_ref() {
            if backend_input.annotations.iter().any(|a|
                a.starts_with("storage_hint_v1:") || a.starts_with("edf_priority_v1:")) {
                guard.abort().await;
                return Err(anyhow::anyhow!("caller supplied reserved admission metadata"));
            }
            backend_input.annotations.extend(annotations.iter().cloned());
        }
'''+anchor)

    pending = 'lib/kv-router/src/pending_score.rs'
    shutil.copy2(HERE/'pending_score.rs', source/pending)
    http = 'lib/llm/src/http/service/openai.rs'
    ingress = 'lib/llm/src/http/service/tfs_ingress.rs'
    edit('lib/llm/src/http/service.rs', 'mod openai;', 'mod openai;\npub(crate) mod tfs_ingress;')
    shutil.copy2(HERE/'tfs_ingress.rs', source/ingress)
    edit(http, '    // Detect batch prompts\n    let batch_size', """    if super::tfs_ingress::enabled() {
        super::tfs_ingress::validate(&request)
            .map_err(|e| ErrorMessage::from_anyhow(e, "TFS completion contract"))?;
    }
    // Detect batch prompts
    let batch_size""")
    edit(http, '    // issue the generate call on the engine\n    let stream = engine.generate(request).await.map_err(|e| {',
         """    let session = if super::tfs_ingress::enabled() {
        let arrival = super::tfs_ingress::monotonic()
            .map_err(|e| ErrorMessage::from_anyhow(e, "TFS monotonic clock"))?;
        Some(super::tfs_ingress::start(&request_id, &model, arrival).await
            .map_err(|e| ErrorMessage::from_anyhow(e, "TFS request registration"))?)
    } else { None };

    // Report even failures that happen before a response stream is returned.
    let generated = engine.generate(request).await;
    if generated.is_err() {
        if let Some(session) = session.as_ref() {
            session.uncertain().await;
        }
    }
    let stream = generated.map_err(|e| {""", within='async fn completions_single(')
    edit(http, '    let ctx = stream.context();', """    let ctx = stream.context();
    let stream = match session {
        Some(session) => super::tfs_ingress::observe(stream, session, ctx.clone()).boxed(),
        None => stream.boxed(),
    };""", within='async fn completions_single(')
    selector = 'lib/kv-router/src/scheduling/selector/default.rs'
    edit(selector, '        let decode_cost_blocks = (load.decode_cost_blocks - waiting_discount).max(0.0);', '''        let mut own_prefill_credit=0.;
        let mut own_decode_credit=0.;
        #[cfg(feature = "standalone-selection")]
        let mut request_heavy=None;
        #[cfg(feature = "standalone-selection")]
        if std::env::var_os("DYN_TIER_PROFILE_URL").is_some() && context.request.mode.is_tracked() {
            let heavy=crate::pending_score::is_heavy(context.request_id);
            if heavy {
                let factor=crate::pending_score::waiting_factor();
                let own_prefill=(load.raw_prefill_blocks-load.active_prefill_tokens as f64/context.block_size as f64).max(0.);
                let own_decode=context.request.worker_load_for(worker).additional_active_blocks as f64;
                own_prefill_credit=(1.-factor)*own_prefill.min((load.raw_prefill_blocks-overlap_credit_blocks-waiting_discount).max(0.));
                own_decode_credit=(1.-factor)*own_decode.min((load.decode_cost_blocks-waiting_discount).max(0.));
            }
            request_heavy=Some(heavy);
        }
        let decode_cost_blocks = (load.decode_cost_blocks - waiting_discount - own_decode_credit).max(0.0);''')
    edit(selector, 'let adjusted_prefill_blocks = (load.raw_prefill_blocks - overlap_credit_blocks - waiting_discount).max(0.0);',
         'let adjusted_prefill_blocks = (load.raw_prefill_blocks - overlap_credit_blocks - waiting_discount - own_prefill_credit).max(0.0);')
    edit(selector, '        let logit = prefill_cost_blocks + decode_cost_blocks + active_request_cost_blocks;', '''        let logit = prefill_cost_blocks + decode_cost_blocks + active_request_cost_blocks;
        #[cfg(feature = "standalone-selection")]
        if let Some(heavy)=request_heavy {
            let baseline=weights.prefill_load_scale*(load.raw_prefill_blocks-overlap_credit_blocks-waiting_discount).max(0.)
                +(load.decode_cost_blocks-waiting_discount).max(0.)+active_request_cost_blocks;
            tracing::debug!(request_id=context.request_id,
                "TIER_REQUEST_SCORE worker_id={} dp_rank={} heavy={} own_prefill_credit={} own_decode_credit={} baseline={} weighted={}",
                worker.worker_id,worker.dp_rank,heavy,own_prefill_credit,own_decode_credit,baseline,logit);
        }''')
    edit(selector, 'discount={} scale={} overlap={} weighted={}',
         'discount={} scale={} overlap={} weighted={} own_prefill_credit={} own_decode_credit={}')
    edit(selector, 'waiting_discount, weights.prefill_load_scale, overlap_credit_blocks, logit);',
         'waiting_discount, weights.prefill_load_scale, overlap_credit_blocks, logit, own_prefill_credit, own_decode_credit);')
    shutil.copy2(HERE/'tier_admission.rs', source/'lib/llm/src/kv_router/tier_admission.rs')
    paths = [*pins, pending, selector, ingress, 'lib/llm/src/kv_router/tier_admission.rs']
    return {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in paths}
