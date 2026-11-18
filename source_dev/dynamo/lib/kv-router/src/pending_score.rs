//! Opt-in experiment: discount pending heavy load in the default score only.
//! Native sequences and hard admission projections always retain full load.
use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::protocols::WorkerWithDpRank;
use serde_json::{Value, json};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    Registered,
    Waiting,
    Active,
    Closed,
}

#[derive(Debug)]
struct Entry {
    tokens: usize,
    worker: Option<WorkerWithDpRank>,
    phase: Phase,
    permit: Option<String>,
    retired: HashSet<String>,
    degraded: bool,
    updated: Instant,
}

struct Ledger {
    factor: f64,
    epoch: String,
    entries: HashMap<String, Entry>,
    waiting: HashMap<WorkerWithDpRank, usize>,
    events: Vec<Value>,
    origin: Instant,
    heartbeat: Option<Instant>,
}

impl Ledger {
    fn new(factor: f64) -> Self {
        assert!(factor.is_finite() && (0.0..=1.0).contains(&factor));
        Self {
            factor,
            epoch: Uuid::new_v4().to_string(),
            entries: HashMap::new(),
            waiting: HashMap::new(),
            events: Vec::new(),
            origin: Instant::now(),
            heartbeat: None,
        }
    }
    fn event(&mut self, kind: &str, id: &str) {
        if self.events.len() >= 10000 { self.events.drain(..1000); }
        let e = &self.entries[id];
        self.events.push(
            json!({"event":kind,"id":id,"t_wall_s":self.origin.elapsed().as_secs_f64(),
            "phase":format!("{:?}",e.phase),"worker_id":e.worker.map(|w|w.worker_id),
            "dp_rank":e.worker.map(|w|w.dp_rank),
            "tokens":e.tokens,"permit_id":e.permit,"factor":self.factor}),
        );
    }
    fn register(&mut self, id: &str, tokens: usize) -> Result<(), String> {
        // Keep terminal identities for ten minutes. Expired identities cannot
        // be revived by control RPCs, which never create registrations.
        self.entries.retain(|_, e| e.phase != Phase::Closed || e.updated.elapsed() < Duration::from_secs(600));
        if tokens == 0 {
            return Err("prompt tokens must be positive".into());
        }
        if let Some(e) = self.entries.get(id) {
            return if e.tokens == tokens && e.phase != Phase::Closed {
                Ok(())
            } else {
                Err("registration conflicts or request is closed".into())
            };
        }
        // One experiment has a bounded lifetime. Fail explicitly instead of
        // dropping tombstones and allowing an old permit to revive a request.
        if self.entries.len() >= 100_000 {
            return Err("score ledger run limit exceeded".into());
        }
        self.entries.insert(
            id.into(),
            Entry {
                tokens,
                worker: None,
                phase: Phase::Registered,
                permit: None,
                retired: HashSet::new(),
                degraded: false,
                updated: Instant::now(),
            },
        );
        self.event("registered", id);
        Ok(())
    }
    fn bind(&mut self, id: &str, worker: WorkerWithDpRank, tokens: usize) -> Result<(), String> {
        let Some(e) = self.entries.get_mut(id) else {
            return Ok(());
        };
        if e.phase != Phase::Registered || e.tokens != tokens {
            return Err("registered score request cannot bind to this booking".into());
        }
        e.worker = Some(worker);
        e.phase = Phase::Waiting;
        if !e.degraded { *self.waiting.entry(worker).or_default() += tokens; }
        self.event("bound_waiting", id);
        Ok(())
    }
    fn activate(&mut self, id: &str, permit: &str) -> Result<(), String> {
        if permit.is_empty() || permit.len() > 256 {
            return Err("invalid permit identity".into());
        }
        let e = self.entries.get_mut(id).ok_or("unknown request")?;
        if e.phase == Phase::Active && e.permit.as_deref() == Some(permit) {
            return Ok(());
        }
        if e.phase != Phase::Waiting || e.retired.contains(permit) {
            return Err("stale activation or request is not waiting".into());
        }
        if !e.degraded { *self.waiting.get_mut(&e.worker.unwrap()).unwrap() -= e.tokens; }
        e.phase = Phase::Active;
        e.permit = Some(permit.into());
        self.event("activated", id);
        Ok(())
    }
    fn rollback(&mut self, id: &str, permit: &str) -> Result<(), String> {
        let e = self.entries.get_mut(id).ok_or("unknown request")?;
        if e.retired.contains(permit) {
            return Ok(());
        }
        if e.phase != Phase::Active || e.permit.as_deref() != Some(permit) {
            return Err("rollback does not own current activation".into());
        }
        e.retired.insert(permit.into());
        e.permit = None;
        e.phase = Phase::Waiting;
        if !e.degraded { *self.waiting.entry(e.worker.unwrap()).or_default() += e.tokens; }
        self.event("rolled_back", id);
        Ok(())
    }
    fn finish(&mut self, id: &str, worker: WorkerWithDpRank) {
        let Some(e) = self.entries.get_mut(id) else {
            return;
        };
        if e.worker != Some(worker) || e.phase == Phase::Closed {
            return;
        }
        if e.phase == Phase::Waiting && !e.degraded {
            *self.waiting.get_mut(&worker).unwrap() -= e.tokens;
        }
        e.phase = Phase::Closed;
        e.updated = Instant::now();
        self.event("closed", id);
    }
    fn snapshot(&self) -> Value {
        json!({"factor":self.factor,"epoch":self.epoch,"entries":self.entries.len(),
            "registered":self.entries.values().filter(|e|e.phase==Phase::Registered).count(),
            "waiting":self.entries.values().filter(|e|e.phase==Phase::Waiting).count(),
            "active":self.entries.values().filter(|e|e.phase==Phase::Active).count(),
            "closed":self.entries.values().filter(|e|e.phase==Phase::Closed).count(),
            "waiting_workers":self.waiting.iter().map(|(w,n)|json!({"worker_id":w.worker_id,"dp_rank":w.dp_rank,"tokens":n})).collect::<Vec<_>>(),
            "events":self.events})
    }
    fn score_view(&self, raw: Option<&str>, require_heartbeat: bool) -> ScoreSnapshot {
        if require_heartbeat && self.heartbeat.is_none_or(|t| t.elapsed() > Duration::from_secs(3)) {
            return ScoreSnapshot { factor: 1., ..Default::default() };
        }
        let heavy = raw.and_then(canonical).and_then(|id| self.entries.get(&id))
            .is_some_and(|entry| entry.phase != Phase::Closed && !entry.degraded);
        ScoreSnapshot { factor: self.factor, heavy, waiting: self.waiting.clone(), ..Default::default() }
    }
    fn degrade(&mut self, id: &str) {
        if let Some(e) = self.entries.get_mut(id) {
            if e.phase == Phase::Waiting && !e.degraded {
                *self.waiting.get_mut(&e.worker.unwrap()).unwrap() -= e.tokens;
            }
            e.degraded = true;
        }
    }
    fn control(&mut self, v: &Value) -> Result<Value, String> {
        let op = v["op"].as_str().ok_or("missing op")?;
        if v["epoch"].as_str() != Some(&self.epoch) {
            return Err("score ledger epoch mismatch".into());
        }
        if op == "snapshot" {
            return Ok(self.snapshot());
        }
        if op == "heartbeat" || op == "reset_degraded" {
            if op == "reset_degraded" || self.heartbeat.is_none_or(|t| t.elapsed() > Duration::from_secs(3)) {
                for e in self.entries.values_mut() { e.degraded = true; }
                self.waiting.clear();
            }
            self.heartbeat = Some(Instant::now());
            return Ok(json!({"heartbeat":true}));
        }
        let id = canonical(v["request_id"].as_str().ok_or("missing request ID")?)
            .ok_or("request ID must contain UUID")?;
        match op {
            "degrade" => {
                self.degrade(&id);
                return Ok(json!({"degraded":true}));
            }
            "register" => self.register(
                &id,
                v["prompt_tokens"].as_u64().ok_or("missing prompt tokens")? as usize,
            )?,
            "activate" => {
                if v["allow_closed"].as_bool() == Some(true)
                    && self
                        .entries
                        .get(&id)
                        .is_some_and(|e| e.phase == Phase::Closed)
                {
                    return Ok(json!({"phase":"Closed","already_closed":true}));
                }
                self.activate(&id, v["permit_id"].as_str().ok_or("missing permit ID")?)?
            }
            "rollback" => {
                if self.entries.get(&id).is_some_and(|e| e.phase == Phase::Closed) {
                    return Ok(json!({"phase":"Closed","already_closed":true}));
                }
                self.rollback(&id, v["permit_id"].as_str().ok_or("missing permit ID")?)?
            }
            _ => return Err("unsupported score operation".into()),
        }
        Ok(json!({"phase":format!("{:?}",self.entries[&id].phase)}))
    }
}

fn canonical(raw: &str) -> Option<String> {
    for width in [36, 32] {
        for bytes in raw.as_bytes().windows(width) {
            if let Ok(s) = std::str::from_utf8(bytes) {
                if let Ok(id) = Uuid::parse_str(s) {
                    return Some(id.simple().to_string());
                }
            }
        }
    }
    None
}

struct Service {
    ledger: Arc<Mutex<Ledger>>,
}
static SERVICE: OnceLock<Option<Service>> = OnceLock::new();

fn service() -> Option<&'static Service> {
    SERVICE.get_or_init(|| {
        if !enabled() { return None; }
        let factor=std::env::var("DYN_WAITING_SCORE_FACTOR").expect("validated waiting factor");
        let factor:f64=factor.parse().expect("invalid waiting score factor");
        let ledger=Arc::new(Mutex::new(Ledger::new(factor)));
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

fn serve(mut stream: TcpStream, state: Arc<Mutex<Ledger>>) {
    use std::io::Read;
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3600)));
    let Ok(copy) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(copy);
    loop {
        let mut line = String::new();
        let Ok(n) = reader.by_ref().take(65537).read_line(&mut line) else {
            return;
        };
        if n == 0 {
            return;
        }
        if n > 65536 || !line.ends_with('\n') {
            return;
        }
        let response = match serde_json::from_str::<Value>(&line) {
            Ok(v) => match state.lock().unwrap().control(&v) {
                Ok(data) => json!({"ok":true,"data":data}),
                Err(e) => json!({"ok":false,"error":e}),
            },
            Err(e) => json!({"ok":false,"error":e.to_string()}),
        };
        if writeln!(stream, "{response}").is_err() {
            return;
        }
    }
}

pub fn initialize() {
    let _ = service();
}
pub fn bind(raw: &str, worker: WorkerWithDpRank, tokens: usize) -> Result<(), String> {
    let Some(s) = service() else {
        return Ok(());
    };
    let id = canonical(raw).ok_or("request ID must contain UUID")?;
    s.ledger.lock().unwrap().bind(&id, worker, tokens)
}
pub fn finish(raw: &str, worker: WorkerWithDpRank) {
    if let (Some(s), Some(id)) = (service(), canonical(raw)) {
        s.ledger.lock().unwrap().finish(&id, worker);
    }
}
pub fn discount(worker: WorkerWithDpRank, block_size: u32) -> f64 {
    let Some(s) = service() else {
        return 0.;
    };
    let ledger = s.ledger.lock().unwrap();
    if std::env::var("DYN_TFS_NATIVE_INGRESS").as_deref() == Ok("1")
        && ledger.heartbeat.is_none_or(|t| t.elapsed() > Duration::from_secs(3)) {
        return 0.; // Full original load while score control is unavailable.
    }
    token_discount_blocks(
        ledger.waiting.get(&worker).copied().unwrap_or(0),
        ledger.factor,
        block_size,
    )
}

pub fn degrade_request(raw: &str) {
    if let (Some(s), Some(id)) = (service(), canonical(raw)) {
        s.ledger.lock().unwrap().degrade(&id);
    }
}

fn token_discount_blocks(tokens: usize, factor: f64, block_size: u32) -> f64 {
    assert!(block_size > 0, "block size must be positive");
    // Native scorer inputs are blocks; the ledger deliberately retains tokens.
    // Preserve fractions, just like raw_prefill_tokens / context.block_size.
    (1. - factor) * tokens as f64 / block_size as f64
}

#[cfg(test)]
mod tests {
    use super::*;
    fn id() -> String {
        Uuid::new_v4().simple().to_string()
    }
    #[test]
    fn weight_activation_rollback_and_stale_fencing() {
        let mut l = Ledger::new(0.1);
        let id = id();
        let w = WorkerWithDpRank::new(7, 0);
        l.register(&id, 100).unwrap();
        l.bind(&id, w, 100).unwrap();
        assert_eq!(l.waiting[&w], 100);
        l.activate(&id, "p1").unwrap();
        l.activate(&id, "p1").unwrap();
        assert_eq!(l.waiting[&w], 0);
        l.rollback(&id, "p1").unwrap();
        l.rollback(&id, "p1").unwrap();
        assert_eq!(l.waiting[&w], 100);
        assert!(l.activate(&id, "p1").is_err());
        l.activate(&id, "p2").unwrap();
        assert!(l.rollback(&id, "p1").is_ok());
        assert_eq!(l.entries[&id].phase, Phase::Active);
        l.finish(&id, WorkerWithDpRank::new(9, 0));
        assert_eq!(l.entries[&id].phase, Phase::Active);
        l.finish(&id, w);
        assert!(l.activate(&id, "p2").is_err());
        assert_eq!(l.waiting[&w], 0);
    }
    #[test]
    fn pending_cancel_and_worker_failure_remove_discount() {
        let mut l = Ledger::new(0.);
        let id = id();
        let w = WorkerWithDpRank::new(2, 0);
        l.register(&id, 10).unwrap();
        l.bind(&id, w, 10).unwrap();
        l.finish(&id, w);
        l.finish(&id, w);
        assert_eq!(l.waiting[&w], 0);
        assert!(l.register(&id, 10).is_err());
    }
    #[test]
    fn epoch_and_registration_conflicts_are_rejected() {
        let mut l = Ledger::new(1.);
        let id = id();
        l.register(&id, 10).unwrap();
        assert!(l.register(&id, 11).is_err());
        assert!(
            l.control(&json!({"op":"snapshot","epoch":"stale"}))
                .is_err()
        );
        assert_eq!(canonical(&format!("cmpl-{id}_0")), Some(id));
    }
    #[test]
    fn discount_uses_scorer_block_units_without_rounding() {
        assert_eq!(token_discount_blocks(100, 0.1, 1), 90.);
        assert_eq!(token_discount_blocks(100, 0.1, 16), 5.625);
        assert_eq!(token_discount_blocks(43139, 0., 16), 2696.1875);
        assert_eq!(token_discount_blocks(43139, 1., 16), 0.);
        assert_eq!(token_discount_blocks(0, 0.1, 16), 0.);
    }
}

/// One frontend owns this ledger. Same-ID dispatch retries are not supported.
pub fn validate_config() -> Result<bool, String> {
    let fields = [
        "DYN_TIER_PROFILE_URL",
        "DYN_TIER_PROFILE_TOKEN",
        "DYN_WAITING_SCORE_FACTOR",
        "DYN_WAITING_SCORE_READY",
    ];
    let count = fields
        .iter()
        .filter(|key| std::env::var_os(key).is_some())
        .count();
    if count == 0 {
        return Ok(false);
    }
    if count != fields.len() {
        return Err("TFS requires profile URL/token and waiting factor/ready together".into());
    }
    for key in fields {
        if std::env::var(key)
            .map_err(|_| format!("invalid {key}"))?
            .is_empty()
        {
            return Err(format!("empty {key}"));
        }
    }
    let factor: f64 = std::env::var("DYN_WAITING_SCORE_FACTOR")
        .unwrap()
        .parse()
        .map_err(|_| "invalid waiting score factor")?;
    if !factor.is_finite() || !(0.0..=1.0).contains(&factor) {
        return Err("waiting factor must be in [0,1]".into());
    }
    Ok(true)
}

pub fn enabled() -> bool {
    validate_config().expect("invalid TFS configuration")
}

pub fn register(raw: &str, tokens: usize) -> Result<(), String> {
    let service = service().ok_or("TFS score service is disabled")?;
    let id = canonical(raw).ok_or("request ID must contain UUID")?;
    service.ledger.lock().unwrap().register(&id, tokens)
}

pub fn discard_unbound(raw: &str) {
    if let (Some(service), Some(id)) = (service(), canonical(raw)) {
        let mut ledger = service.ledger.lock().unwrap();
        if let Some(entry) = ledger.entries.get_mut(&id) {
            if entry.phase == Phase::Registered {
                entry.phase = Phase::Closed;
                ledger.event("discarded_unbound", &id);
            }
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct ScoreSnapshot {
    pub factor: f64,
    pub heavy: bool,
    pub waiting: HashMap<WorkerWithDpRank, usize>,
    pub active_prefill: HashMap<WorkerWithDpRank, usize>,
    pub additional_blocks: HashMap<WorkerWithDpRank, usize>,
}

pub fn score_snapshot(raw: Option<&str>) -> ScoreSnapshot {
    let Some(service) = service() else {
        return ScoreSnapshot {
            factor: 1.,
            ..Default::default()
        };
    };
    let ledger = service.ledger.lock().unwrap();
    ledger.score_view(raw, std::env::var("DYN_TFS_NATIVE_INGRESS").as_deref() == Ok("1"))
}

/// A move-only cleanup owner, carried from the scheduler into the dispatch guard.
pub struct BookingGuard(Option<Box<dyn FnOnce() + Send + Sync>>);
impl std::fmt::Debug for BookingGuard {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("TfsBookingGuard")
    }
}
impl BookingGuard {
    pub fn new(cleanup: impl FnOnce() + Send + Sync + 'static) -> Self {
        Self(Some(Box::new(cleanup)))
    }
    pub fn disarm(&mut self) {
        self.0.take();
    }
}
impl Drop for BookingGuard {
    fn drop(&mut self) {
        if let Some(cleanup) = self.0.take() {
            cleanup();
        }
    }
}

/// The original full booking is never modified by these score credits.
pub fn score_credits(
    snapshot: &ScoreSnapshot,
    worker: WorkerWithDpRank,
    block_size: u32,
    raw_prefill: f64,
    overlap_credit: f64,
    decode: f64,
) -> (f64, f64, f64) {
    let waiting = token_discount_blocks(
        snapshot.waiting.get(&worker).copied().unwrap_or(0),
        snapshot.factor,
        block_size,
    );
    if !snapshot.heavy {
        return (waiting, 0., 0.);
    }
    let active =
        snapshot.active_prefill.get(&worker).copied().unwrap_or(0) as f64 / block_size as f64;
    let own_prefill = (raw_prefill - active).max(0.);
    let own_decode = snapshot
        .additional_blocks
        .get(&worker)
        .copied()
        .unwrap_or(0) as f64;
    (
        waiting,
        (1. - snapshot.factor) * own_prefill.min((raw_prefill - overlap_credit - waiting).max(0.)),
        (1. - snapshot.factor) * own_decode.min((decode - waiting).max(0.)),
    )
}

#[cfg(test)]
mod adaptation_tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn heartbeat_loss_and_recovery_restore_full_load_for_old_requests() {
        let mut l = Ledger::new(0.1);
        l.heartbeat = Some(Instant::now());
        let old = Uuid::new_v4().simple().to_string();
        let worker = WorkerWithDpRank::new(1, 0);
        l.register(&old, 128).unwrap();
        l.bind(&old, worker, 128).unwrap();
        assert!(l.score_view(Some(&old), true).heavy);
        l.heartbeat = Some(Instant::now() - Duration::from_secs(4));
        let unavailable = l.score_view(Some(&old), true);
        assert_eq!(unavailable.factor, 1.);
        assert!(unavailable.waiting.is_empty());
        assert!(!unavailable.heavy);
        l.control(&json!({"op":"heartbeat","epoch":l.epoch.clone()})).unwrap();
        assert!(!l.score_view(Some(&old), true).heavy);
        assert!(l.waiting.is_empty());
        // Old operations can still finish without underflow or stale discounts.
        l.activate(&old, "p").unwrap();
        l.rollback(&old, "p").unwrap();
        l.finish(&old, worker);
        let new = Uuid::new_v4().simple().to_string();
        l.register(&new, 256).unwrap();
        l.bind(&new, worker, 256).unwrap();
        assert_eq!(l.score_view(Some(&new), true).waiting[&worker], 256);
        l.degrade(&new);
        l.degrade(&new);
        assert_eq!(l.waiting[&worker], 0);
    }

    #[test]
    fn block16_credits_are_incremental_and_clamped() {
        let worker = WorkerWithDpRank::new(7, 1);
        let mut s = ScoreSnapshot {
            factor: 0.1,
            heavy: true,
            ..Default::default()
        };
        s.waiting.insert(worker, 160);
        s.active_prefill.insert(worker, 320);
        s.additional_blocks.insert(worker, 8);
        // P=30, H=2, W/B=10. Only this request's p=10 and d=8 are discounted.
        let (waiting, prefill, decode) = score_credits(&s, worker, 16, 30., 2., 35.);
        assert_eq!(waiting, 9.);
        assert_eq!(prefill, 9.);
        assert_eq!(decode, 7.2);
        assert_eq!(score_credits(&s, worker, 16, 1., 2., 3.), (9., 0., 0.));
        s.heavy = false;
        assert_eq!(score_credits(&s, worker, 16, 30., 2., 35.), (9., 0., 0.));
        s.heavy = true;
        s.factor = 1.;
        assert_eq!(score_credits(&s, worker, 16, 30., 2., 35.), (0., 0., 0.));
        let other_rank = WorkerWithDpRank::new(7, 0);
        assert_eq!(
            score_credits(&s, other_rank, 16, 30., 2., 35.),
            (0., 0., 0.)
        );
    }

    #[test]
    fn cleanup_owner_moves_and_disarms_exactly_once() {
        let count = Arc::new(AtomicUsize::new(0));
        let shared = count.clone();
        let original = BookingGuard::new(move || {
            shared.fetch_add(1, Ordering::SeqCst);
        });
        let moved = original;
        drop(moved);
        assert_eq!(count.load(Ordering::SeqCst), 1);
        let shared = count.clone();
        let mut guard = BookingGuard::new(move || {
            shared.fetch_add(1, Ordering::SeqCst);
        });
        guard.disarm();
        drop(guard);
        assert_eq!(count.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn closed_force_notice_and_old_epoch_are_distinct() {
        let mut ledger = Ledger::new(0.1);
        let id = Uuid::new_v4().simple().to_string();
        let worker = WorkerWithDpRank::new(1, 2);
        ledger.register(&id, 160).unwrap();
        ledger.bind(&id, worker, 160).unwrap();
        ledger.finish(&id, worker);
        let message = json!({"op":"activate", "epoch":ledger.epoch, "request_id":id,
            "permit_id":"late-force", "allow_closed":true});
        assert_eq!(ledger.control(&message).unwrap()["already_closed"], true);
        let mut stale = message.clone();
        stale["epoch"] = json!("old");
        assert!(ledger.control(&stale).is_err());
        let mut ordinary = message;
        ordinary["allow_closed"] = json!(false);
        assert!(ledger.control(&ordinary).is_err());
        assert_eq!(ledger.waiting[&worker], 0);
    }

    #[test]
    fn real_tcp_protocol_preserves_permit_ownership() {
        let mut ledger = Ledger::new(0.1);
        let id = Uuid::new_v4().simple().to_string();
        let epoch = ledger.epoch.clone();
        let worker = WorkerWithDpRank::new(2, 1);
        ledger.register(&id, 160).unwrap();
        ledger.bind(&id, worker, 160).unwrap();
        let shared = Arc::new(Mutex::new(ledger));
        let state = shared.clone();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || serve(listener.accept().unwrap().0, state));
        let mut stream = TcpStream::connect(endpoint).unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        for (op, permit, expected) in [
            ("activate", "p1", true),
            ("activate", "p1", true),
            ("rollback", "wrong", false),
            ("rollback", "p1", true),
            ("activate", "p1", false),
            ("activate", "p2", true),
        ] {
            writeln!(
                stream,
                "{}",
                json!({"op":op,"epoch":epoch,"request_id":id,"permit_id":permit})
            )
            .unwrap();
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let reply: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(reply["ok"], expected, "{op} {permit}: {reply}");
        }
        stream.shutdown(std::net::Shutdown::Both).unwrap();
        drop(reader);
        drop(stream);
        server.join().unwrap();
        let mut ledger = shared.lock().unwrap();
        ledger.finish(&id, worker);
        assert_eq!(ledger.waiting[&worker], 0);
        assert_eq!(ledger.snapshot()["active"], 0);
    }
}
