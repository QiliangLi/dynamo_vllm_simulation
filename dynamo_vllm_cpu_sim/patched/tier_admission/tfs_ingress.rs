//! Opt-in metadata-only TFS ingress. Linux 64-bit, single frontend prototype.
//! Lifecycle reporting never blocks response delivery. Uncertainty releases accounting.
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context as _, Result, bail, ensure};
use dynamo_runtime::engine::AsyncEngineContext;
use futures::{Stream, StreamExt};
use serde_json::{Value, json};
use tokio::sync::mpsc;
use base64::Engine as _;
use tokio_stream::wrappers::ReceiverStream;

use crate::protocols::openai::completions::{NvCreateCompletionRequest, NvCreateCompletionResponse};
use crate::types::Annotated;

const MAX_SESSIONS: usize = 4096;
const MAX_REPLY_BYTES: usize = 16384;
const EVENT_ATTEMPTS: usize = 3;
const STREAM_TIMEOUT: Duration = Duration::from_secs(120);
const UNCERTAIN_POLICY: &str = "release-permit-best-effort-v1";
static SESSIONS: OnceLock<Mutex<HashMap<String, Weak<Session>>>> = OnceLock::new();

pub fn enabled() -> bool {
    std::env::var("DYN_TFS_NATIVE_INGRESS").as_deref() == Ok("1")
}

pub fn validate(request: &NvCreateCompletionRequest) -> Result<()> {
    ensure!(request.inner.stream == Some(true), "TFS ingress requires streaming");
    ensure!(request.inner.max_tokens == Some(1), "TFS ingress requires max_tokens=1");
    ensure!(request.inner.n.unwrap_or(1) == 1, "TFS ingress requires n=1");
    ensure!(matches!(&request.inner.prompt, dynamo_protocols::types::Prompt::String(_)),
            "TFS ingress requires one text prompt");
    Ok(())
}

#[cfg(all(target_os = "linux", target_pointer_width = "64"))]
pub fn monotonic() -> Result<f64> {
    #[repr(C)]
    struct Timespec { sec: std::ffi::c_long, nsec: std::ffi::c_long }
    unsafe extern "C" { fn clock_gettime(clock: i32, out: *mut Timespec) -> i32; }
    let mut ts = Timespec { sec: 0, nsec: 0 };
    // Linux CLOCK_MONOTONIC = 1; same clock used by Python time.monotonic().
    ensure!(unsafe { clock_gettime(1, &mut ts) } == 0, "CLOCK_MONOTONIC failed");
    Ok(ts.sec as f64 + ts.nsec as f64 / 1e9)
}

#[cfg(not(all(target_os = "linux", target_pointer_width = "64")))]
pub fn monotonic() -> Result<f64> { bail!("native TFS ingress requires 64-bit Linux") }

fn clock_id() -> Result<String> {
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let offsets = match std::fs::read_to_string("/proc/self/timens_offsets") {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(error) => return Err(error.into()),
    };
    Ok(format!("linux:{}:{}", boot.trim(), offsets.trim()))
}

fn sessions() -> &'static Mutex<HashMap<String, Weak<Session>>> {
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

#[derive(Clone)]
struct Client { http: reqwest::Client, url: reqwest::Url, token: String }
impl Client {
    fn new() -> Result<Self> {
        let raw = std::env::var("DYN_TFS_INGRESS_URL")?;
        let url = reqwest::Url::parse(&(raw.trim_end_matches('/').to_owned() + "/"))?;
        ensure!(url.scheme() == "http" && url.host_str() == Some("127.0.0.1")
            && url.username().is_empty() && url.password().is_none()
            && url.query().is_none() && url.fragment().is_none(),
            "TFS ingress URL must use authenticated loopback HTTP");
        let token = std::env::var("DYN_TIER_PROFILE_TOKEN")?;
        ensure!(!token.is_empty(), "empty TFS token");
        let http = reqwest::Client::builder().no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(5)).build()?;
        Ok(Self { http, url, token })
    }
    async fn exchange(&self, operation: &str, body: Option<&Value>) -> Result<Value> {
        let url = self.url.join(operation)?;
        let req = match body {
            Some(value) => self.http.post(url).json(value),
            None => self.http.get(url),
        };
        let mut response = req.header("x-tier-profile-token", &self.token)
            .send().await?.error_for_status()?;
        let mut data = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            ensure!(data.len() + chunk.len() <= MAX_REPLY_BYTES, "oversized TFS reply");
            data.extend_from_slice(&chunk);
        }
        Ok(serde_json::from_slice(&data)?)
    }
}


#[derive(Clone)]
struct Reporter { id: String, epoch: String, client: Client }

pub struct Session {
    id: String,
    reporter: Option<Reporter>,
    events: mpsc::Sender<Value>,
    sequence: Mutex<u64>,
    degraded: AtomicBool,
    terminal: AtomicBool,
}

impl Drop for Session {
    fn drop(&mut self) {
        if let Ok(mut map) = sessions().lock() {
            if map.get(&self.id).is_some_and(|entry| std::ptr::eq(entry.as_ptr(), self)) {
                map.remove(&self.id);
            }
        }
    }
}

impl Reporter {
    async fn send(&self, body: &Value) -> Result<()> {
        for attempt in 0..EVENT_ATTEMPTS {
            match self.client.exchange("event", Some(body)).await {
                Ok(reply) if reply["ok"] == true && reply["epoch"] == self.epoch
                    && reply["sequence"] == body["sequence"] => return Ok(()),
                Err(error) => tracing::warn!(request_id = %self.id, attempt, %error,
                                           "TFS lifecycle delivery retry"),
                _ => tracing::warn!(request_id = %self.id, attempt, "TFS invalid lifecycle acknowledgement"),
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        bail!("TFS lifecycle delivery exhausted; permit expiry will release missing feedback")
    }
}

impl Session {
    fn new(id: &str, reporter: Option<Reporter>) -> Arc<Self> {
        let (events, mut rx) = mpsc::channel::<Value>(3);
        let delivery = reporter.clone();
        // One ordered sender per request; at most first_token + terminal.
        // Hold a global slot until the sender exits, even after HTTP completes.
        static REPORTERS: OnceLock<Arc<tokio::sync::Semaphore>> = OnceLock::new();
        let slot = REPORTERS.get_or_init(|| Arc::new(tokio::sync::Semaphore::new(MAX_SESSIONS)))
            .clone().try_acquire_owned();
        let reporter = if let Ok(slot) = slot {
            tokio::spawn(async move {
                let _slot = slot;
                if let Some(reporter) = delivery {
                    while let Some(body) = rx.recv().await {
                        if let Err(error) = reporter.send(&body).await {
                            // Do not skip the failed sequence and send later events.
                            tracing::error!(request_id = %reporter.id, %error, "TFS feedback lost");
                            break;
                        }
                    }
                }
            });
            reporter
        } else {
            tracing::warn!(request_id = id, "TFS reporter capacity reached; request bypasses TFS");
            None
        };
        Arc::new(Self { id: id.into(), degraded: AtomicBool::new(reporter.is_none()),
            terminal: AtomicBool::new(false), reporter, events, sequence: Mutex::new(0) })
    }

    fn event(&self, kind: &str, evidence: &str) {
        let Some(reporter) = &self.reporter else { return; };
        let Ok(mut sequence) = self.sequence.lock() else { return; };
        if self.terminal.load(Ordering::Relaxed) { return; }
        if kind != "first_token" { self.terminal.store(true, Ordering::Relaxed); }
        if *sequence >= 2 { return; }
        let Ok(occurred) = monotonic() else { return; };
        let body = json!({"epoch":reporter.epoch,"request_id":self.id,"sequence":*sequence+1,
                         "kind":kind,"evidence":evidence,"occurred_mono":occurred});
        match self.events.try_send(body) {
            Ok(()) => *sequence += 1,
            Err(error) => tracing::warn!(request_id = %self.id, %error,
                                        "TFS lifecycle queue unavailable; response continues"),
        }
    }

    pub async fn uncertain(&self) {
        self.event("uncertain", "execution_not_confirmed");
        dynamo_kv_router::pending_score::degrade_request(&self.id);
    }
}

fn control_timeout() -> Duration {
    let ms = std::env::var("DYN_TFS_CONTROL_TIMEOUT_MS").ok()
        .and_then(|v| v.parse::<u64>().ok()).filter(|v| *v > 0).unwrap_or(500);
    Duration::from_millis(ms)
}

pub async fn start(id: &str, model: &str, arrival: f64) -> Result<Arc<Session>> {
    ensure!(uuid::Uuid::parse_str(id)?.to_string() == id, "canonical request UUID required");
    let connect = async {
        let client = Client::new()?;
        let hello = client.exchange("hello", None).await?;
        ensure!(hello["protocol"] == "dynamo-ingress-v1", "ingress protocol mismatch");
        ensure!(hello["uncertain_policy"] == UNCERTAIN_POLICY, "central policy mismatch");
        let clock = clock_id()?;
        ensure!(hello["clock_id"] == clock, "central clock domain differs");
        let epoch = hello["epoch"].as_str().context("missing central epoch")?.to_owned();
        let body = json!({"epoch":epoch,"request_id":id,
            "attempt_id":uuid::Uuid::new_v4().to_string(),"clock_id":clock,
            "arrival_mono":arrival,"model":model});
        let reply = client.exchange("begin", Some(&body)).await?;
        ensure!(reply["ok"] == true && reply["epoch"] == epoch && reply["request_id"] == id,
                "invalid registration acknowledgement");
        Ok::<_, anyhow::Error>(Reporter { id: id.into(), epoch, client })
    };
    let reporter = match tokio::time::timeout(control_timeout(), connect).await {
        Ok(Ok(reporter)) => Some(reporter),
        result => {
            let error = match result { Ok(Err(e)) => e.to_string(), Err(e) => e.to_string(), _ => unreachable!() };
            tracing::warn!(request_id = id, %error, "TFS registration unavailable; request bypasses TFS");
            None
        }
    };
    let session = Session::new(id, reporter);
    let mut map = sessions().lock().map_err(|_| anyhow::anyhow!("session lock poisoned"))?;
    if map.len() < MAX_SESSIONS {
        ensure!(!map.contains_key(id), "duplicate active request UUID");
        map.insert(id.into(), Arc::downgrade(&session));
    } else {
        session.degraded.store(true, Ordering::Relaxed);
        tracing::warn!(request_id = id, "TFS session capacity reached; request bypasses TFS");
    }
    Ok(session)
}

pub fn fallback_profile() -> Value {
    let encode = |prefix: &str, value: Value| format!("{}{}", prefix,
        base64::engine::general_purpose::URL_SAFE.encode(value.to_string()));
    let now = monotonic().unwrap_or(1.0).max(1.0);
    json!({"is_heavy_read":false,"degraded":true,"annotations":[
        encode("storage_hint_v1:", json!({"version":1,"is_heavy_read":false,
            "estimated_remote_read_bytes":0,"threshold_version":0})),
        encode("edf_priority_v1:", json!({"version":1,"arrival_s":0.0,"deadline_s":120.0,
            "reference_service_s":120.0,"origin_monotonic_s":now,"wall_time_scale":1.0})),
        "tfs_degraded_v1:control_unavailable"]})
}

pub async fn resolve_profile(id: &str, snapshot: &Value) -> Result<Value> {
    let session = sessions().lock().ok().and_then(|map| map.get(id).and_then(Weak::upgrade));
    let Some(session) = session else { return Ok(fallback_profile()); };
    if session.degraded.load(Ordering::Relaxed) { return Ok(fallback_profile()); }
    let Some(reporter) = &session.reporter else { return Ok(fallback_profile()); };
    let body = json!({"epoch":reporter.epoch,"request_id":id,"snapshot":snapshot});
    let operation = reporter.client.exchange("profile", Some(&body));
    match tokio::time::timeout(control_timeout(), operation).await {
        Ok(Ok(reply)) if reply["epoch"] == reporter.epoch && reply["annotations"].is_array()
            && reply["is_heavy_read"].is_boolean() => Ok(reply),
        _ => {
            session.degraded.store(true, Ordering::Relaxed);
            session.uncertain().await;
            tracing::warn!(request_id = id, "TFS profile unavailable; request bypasses TFS");
            Ok(fallback_profile())
        }
    }
}

pub fn observe<S>(stream: S, session: Arc<Session>, context: Arc<dyn AsyncEngineContext>)
    -> impl Stream<Item = Annotated<NvCreateCompletionResponse>>
where S: Stream<Item = Annotated<NvCreateCompletionResponse>> + Send + 'static {
    let (tx, rx) = mpsc::channel(16);
    // Own the observation task independently of the HTTP response consumer.
    // Cancellation is observed explicitly; dropping a response is not completion.
    tokio::spawn(async move {
        tokio::pin!(stream);
        let mut first = false;
        let mut finished = false;
        let outcome: Result<()> = async {
            loop {
                let item = tokio::select! {
                    biased;
                    item = tokio::time::timeout(STREAM_TIMEOUT, stream.next()) => item?,
                    _ = context.stopped() => { bail!("request context stopped without confirmed completion"); }
                };
                let Some(item) = item else {
                    ensure!(finished, "stream EOF without finish_reason");
                    session.event("request_finished", "finish_reason");
                    return Ok(());
                };
                ensure!(item.error.is_none() && item.event.as_deref() != Some("error"),
                        "backend reported an error: {:?}", item.error);
                if let Some(data) = &item.data {
                    ensure!(data.inner.choices.len() <= 1, "unexpected multiple choices");
                    for choice in &data.inner.choices {
                        if !choice.text.is_empty() && !first {
                            session.event("first_token", "nonempty_text");
                            first = true;
                        }
                        finished |= choice.finish_reason.is_some();
                    }
                }
                // A slow client must not hold observation forever. If the client
                // is gone, keep draining until EOF or explicit cancellation.
                let _ = tokio::time::timeout(STREAM_TIMEOUT, tx.send(item)).await?;
            }
        }.await;
        if let Err(error) = outcome {
            tracing::error!(request_id = %session.id, error = %format!("{error:#}"),
                            "TFS request observation failed; reporting request-local uncertainty");
            session.uncertain().await;
            let _ = tokio::time::timeout(Duration::from_secs(1),
                tx.send(Annotated::from_error(format!("TFS ingress: {error}")))).await;
        }
    });
    ReceiverStream::new(rx)
}

#[cfg(all(test, target_os = "linux", target_pointer_width = "64"))]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    #[tokio::test]
    async fn lifecycle_enqueue_does_not_wait_for_central_and_preserves_order() {
        let bodies = Arc::new(Mutex::new(Vec::<Value>::new()));
        let observed = bodies.clone();
        let release = Arc::new(tokio::sync::Notify::new());
        let gate = release.clone();
        let app = axum::Router::new().route("/event", axum::routing::post(
            move |axum::Json(body): axum::Json<Value>| {
                let seen = observed.clone();
                let gate = gate.clone();
                async move {
                    seen.lock().unwrap().push(body.clone());
                    if body["sequence"] == 1 { gate.notified().await; }
                    axum::Json(json!({"ok":true,"epoch":body["epoch"],"sequence":body["sequence"]}))
                }
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = reqwest::Url::parse(&format!("http://{}/", listener.local_addr().unwrap())).unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap(); });
        let reporter = Reporter { id: uuid::Uuid::new_v4().to_string(), epoch: "e".into(),
            client: Client { http: reqwest::Client::builder().no_proxy().build().unwrap(), url, token:"t".into() } };
        let session = Session::new(&reporter.id.clone(), Some(reporter));
        session.event("first_token", "nonempty_text");
        session.event("request_finished", "finish_reason");
        tokio::time::timeout(Duration::from_secs(2), async {
            while bodies.lock().unwrap().is_empty() { tokio::task::yield_now().await; }
        }).await.unwrap();
        assert_eq!(bodies.lock().unwrap().len(), 1);
        release.notify_one();
        tokio::time::timeout(Duration::from_secs(2), async {
            while bodies.lock().unwrap().len() < 2 { tokio::task::yield_now().await; }
        }).await.unwrap();
        let seen = bodies.lock().unwrap();
        assert_eq!(seen[0]["sequence"], 1);
        assert_eq!(seen[1]["sequence"], 2);
        assert!(seen[1]["occurred_mono"].as_f64().unwrap() >= seen[0]["occurred_mono"].as_f64().unwrap());
        server.abort();
    }

    #[tokio::test]
    async fn retry_preserves_event_identity_and_timestamp() {
        let bodies = Arc::new(Mutex::new(Vec::<Value>::new()));
        let observed = bodies.clone();
        let calls = Arc::new(AtomicUsize::new(0));
        let app = axum::Router::new().route("/event", axum::routing::post(
            move |axum::Json(body): axum::Json<Value>| {
                let seen = observed.clone();
                let calls = calls.clone();
                async move {
                    seen.lock().unwrap().push(body.clone());
                    let ok = calls.fetch_add(1, Ordering::Relaxed) != 0;
                    axum::Json(json!({"ok":ok,"epoch":body["epoch"],"sequence":body["sequence"]}))
                }
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = reqwest::Url::parse(&format!("http://{}/", listener.local_addr().unwrap())).unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap(); });
        let reporter = Reporter { id: "id".into(), epoch:"e".into(),
            client: Client { http:reqwest::Client::builder().no_proxy().build().unwrap(), url, token:"t".into() } };
        reporter.send(&json!({"epoch":"e","sequence":1,"occurred_mono":123.0})).await.unwrap();
        let seen = bodies.lock().unwrap();
        assert_eq!(seen.len(), 2);
        assert_eq!(seen[0], seen[1]);
        server.abort();
    }
}
