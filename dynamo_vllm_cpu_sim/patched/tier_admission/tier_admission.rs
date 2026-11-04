//! Opt-in bridge after native tier lookup/selection and before worker dispatch.
//! A profile-service failure aborts the native guard; it never defaults to light.
use std::sync::OnceLock;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use dynamo_kv_router::protocols::WorkerWithDpRank;
use dynamo_kv_router::scheduling::{AdmissionAttempt, overlap::SelectedWorkerTierSnapshot};

pub fn snapshot(tiers: &SelectedWorkerTierSnapshot, worker: WorkerWithDpRank,
                block_size: u32, prompt_tokens: usize) -> Option<Value> {
    std::env::var_os("DYN_TIER_PROFILE_URL")?;
    Some(json!({"version":1, "worker_id":worker.worker_id, "dp_rank":worker.dp_rank,
        "block_size":block_size, "prompt_tokens":prompt_tokens,
        "dp_device_blocks":tiers.dp_device_blocks, "gpu_blocks":tiers.gpu_blocks,
        "host_pinned_blocks":tiers.host_pinned_blocks, "disk_blocks":tiers.disk_blocks}))
}

pub async fn annotations(request_id: &str, snapshot: Option<&Value>,
        worker: WorkerWithDpRank, attempt: AdmissionAttempt) -> Result<Option<Vec<String>>> {
    let Ok(url) = std::env::var("DYN_TIER_PROFILE_URL") else { return Ok(None); };
    let mut data = snapshot.context("tier snapshot missing: unsupported pinned/advisory path")?.clone();
    data["request_id"] = json!(request_id);
    // Experimental control plane is loopback-only and authenticated per run.
    let parsed = reqwest::Url::parse(&url)?;
    if parsed.scheme() != "http" || parsed.host_str() != Some("127.0.0.1") {
        bail!("tier profile service must use loopback HTTP");
    }
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    let client = CLIENT.get_or_init(|| reqwest::Client::builder()
        .no_proxy().redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(5)).build().expect("tier HTTP client"));
    let response = client.post(parsed)
        .header("x-tier-profile-token", std::env::var("DYN_TIER_PROFILE_TOKEN")?)
        .json(&data).send().await?.error_for_status()?;
    let bytes = response.bytes().await?;
    if bytes.len() > 16384 { bail!("oversized tier profile response"); }
    let reply: Value = serde_json::from_slice(&bytes)?;
    let annotations: Vec<String> = serde_json::from_value(reply["annotations"].clone())?;
    let heavy = reply["is_heavy_read"].as_bool().context("missing heavy classification")?;
    if heavy {
        let AdmissionAttempt::Tracked(attempt) = attempt else { bail!("tier profile requires tracked booking"); };
        dynamo_kv_router::pending_score::register_selected(request_id, worker, attempt,
            data["prompt_tokens"].as_u64().context("invalid prompt tokens")? as usize)
            .map_err(anyhow::Error::msg)?;
    }
    Ok(Some(annotations))
}
