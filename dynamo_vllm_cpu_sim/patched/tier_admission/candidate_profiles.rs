//! Request-scoped classification installed before scorer/picker execution.
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use serde_json::Value;
use crate::protocols::WorkerWithDpRank;
#[derive(Clone)]
pub struct Mark { pub heavy: bool, pub preview_id: String }
struct Prepared { generation: String, workers: HashMap<WorkerWithDpRank, Mark> }
static PREPARED: OnceLock<Mutex<HashMap<String, Prepared>>> = OnceLock::new();
fn registry() -> &'static Mutex<HashMap<String, Prepared>> {
    PREPARED.get_or_init(|| Mutex::new(HashMap::new()))
}
pub struct Registration { id: String, generation: String }
impl Drop for Registration {
    fn drop(&mut self) {
        let mut entries=registry().lock().unwrap();
        if entries.get(&self.id).is_some_and(|p| p.generation==self.generation) {
            entries.remove(&self.id);
        }
    }
}
pub fn register(raw:&str,generation:&str,rows:&[Value])->Result<Registration,String> {
    let id=crate::pending_score::canonical(raw).ok_or("candidate request needs UUID")?;
    let mut workers=HashMap::new();
    for row in rows {
        let worker=WorkerWithDpRank::new(row["worker_id"].as_u64().ok_or("missing worker")?,
            u32::try_from(row["dp_rank"].as_u64().ok_or("missing DP rank")?).map_err(|_| "invalid DP rank")?);
        let heavy=row["hint"]["is_heavy_read"].as_bool().ok_or("missing heavy mark")?;
        if workers.insert(worker,Mark {heavy,preview_id:generation.into()}).is_some() {
            return Err("duplicate candidate classification".into());
        }
    }
    if workers.is_empty() {return Err("no candidate classifications".into());}
    let mut entries=registry().lock().unwrap();
    if entries.len()>=100_000 || entries.contains_key(&id) {
        return Err("candidate registry full or request already prepared".into());
    }
    entries.insert(id.clone(),Prepared {generation:generation.into(),workers});
    Ok(Registration {id,generation:generation.into()})
}
pub fn lookup(raw:&str,worker:WorkerWithDpRank)->Option<Mark> {
    let id=crate::pending_score::canonical(raw)?;
    registry().lock().unwrap().get(&id)?.workers.get(&worker).cloned()
}
