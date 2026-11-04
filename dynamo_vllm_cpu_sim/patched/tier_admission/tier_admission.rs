//! Prepare per-candidate marks before scoring; commit only the selected profile.
use std::sync::OnceLock;
use std::time::Duration;
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use dynamo_kv_router::protocols::WorkerWithDpRank;
use dynamo_kv_router::scheduling::{AdmissionAttempt, overlap::SelectedWorkerTierSnapshot};
pub fn enabled()->bool {std::env::var_os("DYN_TIER_PROFILE_URL").is_some()}
pub fn snapshot(tiers:&SelectedWorkerTierSnapshot,worker:WorkerWithDpRank,
                block_size:u32,prompt_tokens:usize)->Option<Value> {
    if !enabled() {return None;}
    Some(json!({"version":1,"worker_id":worker.worker_id,"dp_rank":worker.dp_rank,
        "block_size":block_size,"prompt_tokens":prompt_tokens,
        "dp_device_blocks":tiers.dp_device_blocks,"gpu_blocks":tiers.gpu_blocks,
        "host_pinned_blocks":tiers.host_pinned_blocks,"disk_blocks":tiers.disk_blocks}))
}
async fn exchange(data:&Value)->Result<Value> {
    let parsed=reqwest::Url::parse(&std::env::var("DYN_TIER_PROFILE_URL")?)?;
    if parsed.scheme()!="http" || parsed.host_str()!=Some("127.0.0.1") {
        bail!("tier profile service must use loopback HTTP");
    }
    static CLIENT:OnceLock<reqwest::Client>=OnceLock::new();
    let client=CLIENT.get_or_init(||reqwest::Client::builder().no_proxy()
        .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(5))
        .build().expect("tier HTTP client"));
    let bytes=client.post(parsed)
        .header("x-tier-profile-token",std::env::var("DYN_TIER_PROFILE_TOKEN")?)
        .json(data).send().await?.error_for_status()?.bytes().await?;
    if bytes.len()>1024*1024 {bail!("oversized tier profile response");}
    Ok(serde_json::from_slice(&bytes)?)
}
pub struct PreparedProfiles {
    preview_id:String,
    _registration:dynamo_kv_router::candidate_profiles::Registration,
}
impl PreparedProfiles {
    pub fn selection(&self,worker:WorkerWithDpRank)->Value {
        json!({"preview_id":self.preview_id,"worker_id":worker.worker_id,"dp_rank":worker.dp_rank})
    }
}
pub async fn prepare(request_id:&str,mut snapshots:Vec<Value>)->Result<PreparedProfiles> {
    for s in &mut snapshots {s["request_id"]=json!(request_id);}
    let reply=exchange(&json!({"phase":"prepare","request_id":request_id,"candidates":snapshots})).await?;
    let preview_id=reply["preview_id"].as_str().context("missing preview ID")?.to_string();
    let rows=reply["candidates"].as_array().context("missing candidate marks")?;
    let registration=dynamo_kv_router::candidate_profiles::register(request_id,&preview_id,rows)
        .map_err(anyhow::Error::msg)?;
    tracing::debug!(request_id,preview_id,candidates=rows.len(),"TIER_PROFILES_PREPARED");
    Ok(PreparedProfiles {preview_id,_registration:registration})
}
pub async fn annotations(request_id:&str,selection:Option<&Value>,
        worker:WorkerWithDpRank,_attempt:AdmissionAttempt)->Result<Option<Vec<String>>> {
    if !enabled() {return Ok(None);}
    let mut data=selection.context("pre-score preparation missing for selected request")?.clone();
    if data["worker_id"].as_u64()!=Some(worker.worker_id)
        || data["dp_rank"].as_u64()!=Some(worker.dp_rank as u64) {
        bail!("selected worker differs from prepared profile selection");
    }
    data["phase"]=json!("commit"); data["request_id"]=json!(request_id);
    let reply=exchange(&data).await?;
    Ok(Some(serde_json::from_value(reply["annotations"].clone())?))
}
