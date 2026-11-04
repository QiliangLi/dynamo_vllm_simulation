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
    edit(router, '        let router_hint = if is_admitted_routing {',
         '''        let tier_snapshot = tier_admission::snapshot(&response.selected_worker_tiers,
            response.best_worker, self.block_size, isl_tokens);
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
    # Retain booking identity until classification returns. Closing the native
    # booking during the await prevents a late registration from reviving it.
    edit(pending, '    entries: HashMap<String, Entry>,',
         '    entries: HashMap<String, Entry>,\n    bookings: HashMap<String, (WorkerWithDpRank, AttemptId, usize)>,')
    edit(pending, 'entries:HashMap::new(),', 'entries:HashMap::new(), bookings:HashMap::new(),')
    edit(pending, '        let Some(e)=self.entries.get_mut(id) else {return Ok(());};',
         '''        if std::env::var_os("DYN_TIER_PROFILE_URL").is_some() {
            if self.bookings.len() >= 100_000 {return Err("tier booking limit exceeded".into());}
            self.bookings.insert(id.into(), (worker,attempt,tokens));
        }
        let Some(e)=self.entries.get_mut(id) else {return Ok(());};''')
    edit(pending, '        let Some(e)=self.entries.get_mut(id) else {return;};',
         '''        if self.bookings.get(id).is_some_and(|(w,a,_)| *w==worker && *a==attempt) {
            self.bookings.remove(id);
        }
        let Some(e)=self.entries.get_mut(id) else {return;};''')
    edit(pending, 'pub fn initialize()', '''pub fn register_selected(raw:&str, worker:WorkerWithDpRank, attempt:AttemptId, tokens:usize)->Result<(),String> {
    let Some(s)=service() else {return Ok(());};
    let id=canonical(raw).ok_or("tier request must contain UUID")?;
    let mut ledger=s.ledger.lock().unwrap();
    if ledger.bookings.get(&id)!=Some(&(worker,attempt,tokens)) {
        return Err("tier classification no longer owns a live native booking".into());
    }
    ledger.register(&id,tokens)?;
    ledger.bind(&id,worker,attempt,tokens)
}
pub fn initialize()''')
    shutil.copy2(HERE/'tier_admission.rs', source/'lib/llm/src/kv_router/tier_admission.rs')
    paths = [*pins, pending, 'lib/llm/src/kv_router/tier_admission.rs']
    return {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in paths}
