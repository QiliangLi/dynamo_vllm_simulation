//! Opt-in experiment: discount pending heavy load in the default score only.
//! Native sequences and hard admission projections always retain full load.
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use uuid::Uuid;
use crate::protocols::WorkerWithDpRank;
use crate::scheduling::AttemptId;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase { Registered, Waiting, Active, Closed }

#[derive(Debug)]
struct Entry {
    tokens: usize,
    worker: Option<WorkerWithDpRank>,
    attempt: Option<AttemptId>,
    phase: Phase,
    permit: Option<String>,
    retired: HashSet<String>,
}

struct Ledger {
    factor: f64,
    epoch: String,
    entries: HashMap<String, Entry>,
    waiting: HashMap<WorkerWithDpRank, usize>,
    // 双信号路由的囤积信号：等带宽重读"条数"（区别于 token 量的 waiting）。
    waiting_counts: HashMap<WorkerWithDpRank, usize>,
    loading_limit: Option<usize>,
    events: Vec<Value>,
    origin: Instant,
}

impl Ledger {
    fn new(factor: f64) -> Self {
        assert!(factor.is_finite() && (0.0..=1.0).contains(&factor));
        Self { factor, epoch:Uuid::new_v4().to_string(), entries:HashMap::new(),
              waiting:HashMap::new(), waiting_counts:HashMap::new(),
              loading_limit:None, events:Vec::new(), origin:Instant::now() }
    }
    fn event(&mut self, kind:&str, id:&str) {
        let e = &self.entries[id];
        self.events.push(json!({"event":kind,"id":id,"t_wall_s":self.origin.elapsed().as_secs_f64(),
            "phase":format!("{:?}",e.phase),"worker_id":e.worker.map(|w|w.worker_id),
            "dp_rank":e.worker.map(|w|w.dp_rank),"attempt":e.attempt.map(|a|a.to_string()),
            "tokens":e.tokens,"permit_id":e.permit,"factor":self.factor}));
    }
    fn register(&mut self, id:&str, tokens:usize) -> Result<(),String> {
        if tokens==0 {return Err("prompt tokens must be positive".into());}
        if let Some(e)=self.entries.get(id) {
            return if e.tokens==tokens && e.phase!=Phase::Closed {Ok(())}
                else {Err("registration conflicts or request is closed".into())};
        }
        // One experiment has a bounded lifetime. Fail explicitly instead of
        // dropping tombstones and allowing an old permit to revive a request.
        if self.entries.len()>=100_000 {return Err("score ledger run limit exceeded".into());}
        self.entries.insert(id.into(),Entry {tokens,worker:None,attempt:None,
            phase:Phase::Registered,permit:None,retired:HashSet::new()});
        self.event("registered",id);Ok(())
    }
    fn bind(&mut self,id:&str,worker:WorkerWithDpRank,attempt:AttemptId,tokens:usize) -> Result<(),String> {
        let Some(e)=self.entries.get_mut(id) else {return Ok(());};
        if e.phase!=Phase::Registered || e.tokens!=tokens {
            return Err("registered score request cannot bind to this booking".into());
        }
        e.worker=Some(worker);e.attempt=Some(attempt);e.phase=Phase::Waiting;
        *self.waiting.entry(worker).or_default()+=tokens;
        *self.waiting_counts.entry(worker).or_default()+=1;
        self.event("bound_waiting",id);Ok(())
    }
    fn activate(&mut self,id:&str,permit:&str) -> Result<(),String> {
        if permit.is_empty() || permit.len()>256 {return Err("invalid permit identity".into());}
        let e=self.entries.get_mut(id).ok_or("unknown request")?;
        if e.phase==Phase::Active && e.permit.as_deref()==Some(permit) {return Ok(());}
        if e.phase!=Phase::Waiting || e.retired.contains(permit) {
            return Err("stale activation or request is not waiting".into());
        }
        *self.waiting.get_mut(&e.worker.unwrap()).unwrap()-=e.tokens;
        let worker=e.worker.unwrap();
        if let Some(count)=self.waiting_counts.get_mut(&worker) {*count-=1;}
        e.phase=Phase::Active;e.permit=Some(permit.into());
        self.event("activated",id);Ok(())
    }
    fn rollback(&mut self,id:&str,permit:&str) -> Result<(),String> {
        let e=self.entries.get_mut(id).ok_or("unknown request")?;
        if e.retired.contains(permit) {return Ok(());}
        if e.phase!=Phase::Active || e.permit.as_deref()!=Some(permit) {
            return Err("rollback does not own current activation".into());
        }
        e.retired.insert(permit.into());e.permit=None;e.phase=Phase::Waiting;
        let worker=e.worker.unwrap();
        *self.waiting.entry(worker).or_default()+=e.tokens;
        *self.waiting_counts.entry(worker).or_default()+=1;
        self.event("rolled_back",id);Ok(())
    }
    fn finish(&mut self,id:&str,worker:WorkerWithDpRank,attempt:AttemptId) {
        let Some(e)=self.entries.get_mut(id) else {return;};
        if e.worker!=Some(worker) || e.attempt!=Some(attempt) || e.phase==Phase::Closed {return;}
        if e.phase==Phase::Waiting {
            *self.waiting.get_mut(&worker).unwrap()-=e.tokens;
            if let Some(count)=self.waiting_counts.get_mut(&worker) {*count-=1;}
        }
        e.phase=Phase::Closed;self.event("closed",id);
    }
    fn snapshot(&self) -> Value {
        json!({"factor":self.factor,"epoch":self.epoch,"entries":self.entries.len(),
            "registered":self.entries.values().filter(|e|e.phase==Phase::Registered).count(),
            "waiting":self.entries.values().filter(|e|e.phase==Phase::Waiting).count(),
            "active":self.entries.values().filter(|e|e.phase==Phase::Active).count(),
            "closed":self.entries.values().filter(|e|e.phase==Phase::Closed).count(),
            "loading_limit":self.loading_limit,
            "waiting_workers":self.waiting.iter().map(|(w,n)|json!({"worker_id":w.worker_id,"dp_rank":w.dp_rank,"tokens":n})).collect::<Vec<_>>(),
            "waiting_counts":self.waiting_counts.iter().map(|(w,n)|json!({"worker_id":w.worker_id,"dp_rank":w.dp_rank,"requests":n})).collect::<Vec<_>>(),
            "events":self.events})
    }
    fn control(&mut self,v:&Value) -> Result<Value,String> {
        let op=v["op"].as_str().ok_or("missing op")?;
        if v["epoch"].as_str()!=Some(&self.epoch) {return Err("score ledger epoch mismatch".into());}
        if op=="snapshot" {return Ok(self.snapshot());}
        let id=canonical(v["request_id"].as_str().ok_or("missing request ID")?).ok_or("request ID must contain UUID")?;
        match op {
            "register"=>self.register(&id,v["prompt_tokens"].as_u64().ok_or("missing prompt tokens")? as usize)?,
            "activate"=>self.activate(&id,v["permit_id"].as_str().ok_or("missing permit ID")?)?,
            "rollback"=>self.rollback(&id,v["permit_id"].as_str().ok_or("missing permit ID")?)?,
            _=>return Err("unsupported score operation".into()),
        }
        Ok(json!({"phase":format!("{:?}",self.entries[&id].phase)}))
    }
}

fn canonical(raw:&str)->Option<String> {
    for width in [36,32] {
        for bytes in raw.as_bytes().windows(width) {
            if let Ok(s)=std::str::from_utf8(bytes) {
                if let Ok(id)=Uuid::parse_str(s) {return Some(id.simple().to_string());}
            }
        }
    }
    None
}

struct Service { ledger:Arc<Mutex<Ledger>> }
static SERVICE:OnceLock<Option<Service>>=OnceLock::new();

fn service()->Option<&'static Service> {
    SERVICE.get_or_init(|| {
        let factor=match std::env::var("DYN_WAITING_SCORE_FACTOR") {Ok(v)=>v,Err(_)=>return None};
        let factor:f64=factor.parse().expect("invalid waiting score factor");
        let mut ledger=Ledger::new(factor);
        // 硬反压阈值（条数）：仅 lease 臂的 frontend 环境注入；未设置则不钳位。
        if let Ok(v)=std::env::var("DYN_LEASE_LOADING_SLOTS") {
            let limit:usize=v.parse().expect("invalid loading slots");
            ledger.loading_limit=Some(limit);
        }
        let ledger=Arc::new(Mutex::new(ledger));
        let listener=TcpListener::bind("127.0.0.1:0").expect("bind score ledger");
        let ready=std::env::var("DYN_WAITING_SCORE_READY").expect("score ready path required");
        let epoch=ledger.lock().unwrap().epoch.clone();
        let info=json!({"protocol":"waiting-score-v1","endpoint":format!("tcp://{}",listener.local_addr().unwrap()),"epoch":epoch,"factor":factor,"pid":std::process::id()});
        let temporary=format!("{ready}.tmp");
        std::fs::write(&temporary,info.to_string()).expect("write score identity");
        std::fs::rename(temporary,ready).expect("publish score identity");
        let shared=ledger.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream)=stream else {continue;};let state=shared.clone();
                std::thread::spawn(move || serve(stream,state));
            }
        });
        Some(Service {ledger})
    }).as_ref()
}

fn serve(mut stream:TcpStream,state:Arc<Mutex<Ledger>>) {
    use std::io::Read;
    let _=stream.set_nodelay(true);
    let _=stream.set_read_timeout(Some(Duration::from_secs(3600)));
    let Ok(copy)=stream.try_clone() else {return;};let mut reader=BufReader::new(copy);
    loop {
        let mut line=String::new();
        let Ok(n)=reader.by_ref().take(65537).read_line(&mut line) else {return;};
        if n==0 {return;}
        if n>65536 || !line.ends_with('\n') {return;}
        let response=match serde_json::from_str::<Value>(&line) {
            Ok(v)=>match state.lock().unwrap().control(&v) {
                Ok(data)=>json!({"ok":true,"data":data}),Err(e)=>json!({"ok":false,"error":e}),
            },
            Err(e)=>json!({"ok":false,"error":e.to_string()}),
        };
        if writeln!(stream,"{response}").is_err() {return;}
    }
}

pub fn initialize() {let _=service();}
pub fn bind(raw:&str,worker:WorkerWithDpRank,attempt:AttemptId,tokens:usize)->Result<(),String> {
    let Some(s)=service() else {return Ok(());};let Some(id)=canonical(raw) else {return Ok(());};
    s.ledger.lock().unwrap().bind(&id,worker,attempt,tokens)
}
pub fn finish(raw:&str,worker:WorkerWithDpRank,attempt:AttemptId) {
    if let (Some(s),Some(id))=(service(),canonical(raw)) {s.ledger.lock().unwrap().finish(&id,worker,attempt);}
}
pub fn discount(worker:WorkerWithDpRank,block_size:u32)->f64 {
    let Some(s)=service() else {return 0.;};
    let ledger=s.ledger.lock().unwrap();
    // 硬反压：等带宽重读条数 > 2×加载位上限 → 返回大额负折扣，
    // 经 scorer 的减法把该实例 logit 抬到饱和，argmin 不再选中。
    if let Some(limit)=ledger.loading_limit {
        let count=ledger.waiting_counts.get(&worker).copied().unwrap_or(0);
        if count>2*limit {return -1e12;}
    }
    token_discount_blocks(ledger.waiting.get(&worker).copied().unwrap_or(0),ledger.factor,block_size)
}

fn token_discount_blocks(tokens:usize,factor:f64,block_size:u32)->f64 {
    assert!(matches!(block_size,1|16),"waiting score experiment supports block_size=1/16");
    // Native scorer inputs are blocks; the ledger deliberately retains tokens.
    // Preserve fractions, just like raw_prefill_tokens / context.block_size.
    (1.-factor)*tokens as f64/block_size as f64
}

#[cfg(test)]
mod tests {
    use super::*;
    fn id()->String {Uuid::new_v4().simple().to_string()}
    #[test]
    fn weight_activation_rollback_and_stale_fencing() {
        let mut l=Ledger::new(0.1);let id=id();let w=WorkerWithDpRank::new(7,0);let a=AttemptId::new(1);
        l.register(&id,100).unwrap();l.bind(&id,w,a,100).unwrap();assert_eq!(l.waiting[&w],100);
        l.activate(&id,"p1").unwrap();l.activate(&id,"p1").unwrap();assert_eq!(l.waiting[&w],0);
        l.rollback(&id,"p1").unwrap();l.rollback(&id,"p1").unwrap();assert_eq!(l.waiting[&w],100);
        assert!(l.activate(&id,"p1").is_err());l.activate(&id,"p2").unwrap();
        assert!(l.rollback(&id,"p1").is_ok());assert_eq!(l.entries[&id].phase,Phase::Active);
        l.finish(&id,w,AttemptId::new(9));assert_eq!(l.entries[&id].phase,Phase::Active);
        l.finish(&id,w,a);assert!(l.activate(&id,"p2").is_err());assert_eq!(l.waiting[&w],0);
    }
    #[test]
    fn pending_cancel_and_worker_failure_remove_discount() {
        let mut l=Ledger::new(0.);let id=id();let w=WorkerWithDpRank::new(2,0);let a=AttemptId::new(1);
        l.register(&id,10).unwrap();l.bind(&id,w,a,10).unwrap();l.finish(&id,w,a);l.finish(&id,w,a);
        assert_eq!(l.waiting[&w],0);assert!(l.register(&id,10).is_err());
    }
    #[test]
    fn epoch_and_registration_conflicts_are_rejected() {
        let mut l=Ledger::new(1.);let id=id();l.register(&id,10).unwrap();
        assert!(l.register(&id,11).is_err());assert!(l.control(&json!({"op":"snapshot","epoch":"stale"})).is_err());
        assert_eq!(canonical(&format!("cmpl-{id}_0")),Some(id));
    }
    #[test]
    fn discount_uses_scorer_block_units_without_rounding() {
        assert_eq!(token_discount_blocks(100,0.1,1),90.);
        assert_eq!(token_discount_blocks(100,0.1,16),5.625);
        assert_eq!(token_discount_blocks(43139,0.,16),2696.1875);
        assert_eq!(token_discount_blocks(43139,1.,16),0.);
        assert_eq!(token_discount_blocks(0,0.1,16),0.);
    }
    #[test]
    fn waiting_counts_track_requests_not_tokens() {
        let mut l=Ledger::new(0.25);let id=id();let w=WorkerWithDpRank::new(7,0);let a=AttemptId::new(1);
        l.register(&id,100).unwrap();l.bind(&id,w,a,100).unwrap();
        assert_eq!(l.waiting_counts[&w],1);
        l.activate(&id,"p1").unwrap();assert_eq!(l.waiting_counts.get(&w),None);
        l.rollback(&id,"p1").unwrap();assert_eq!(l.waiting_counts[&w],1);
        l.finish(&id,w,a);assert_eq!(l.waiting_counts.get(&w),None);
    }
}
