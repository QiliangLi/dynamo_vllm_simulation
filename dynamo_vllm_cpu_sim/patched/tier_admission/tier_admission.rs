//! One shared-storage profile, resolved once after lookup and before scoring.
use std::sync::OnceLock;
use std::time::Duration;
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use dynamo_kv_router::scheduling::overlap::OverlapSignals;

pub fn enabled()->bool {std::env::var_os("DYN_TIER_PROFILE_URL").is_some()}

pub fn snapshot(overlap:&OverlapSignals, block_size:u32, prompt_tokens:usize)->Value {
    // Equal instances expose replicas of the same shared prefix. Take one
    // prefix length, never sum replica hits or build per-worker profiles.
    let disk_blocks=overlap.tier_overlap_blocks.disk.values().copied().max().unwrap_or(0);
    json!({"version":1,"block_size":block_size,"prompt_tokens":prompt_tokens,
           "disk_blocks":disk_blocks})
}

pub struct Profile { request_id:String, pub annotations:Vec<String> }
impl Drop for Profile {
    fn drop(&mut self) {
        // Selection can fail before a resource booking is created.
        dynamo_kv_router::pending_score::discard_unbound(&self.request_id);
    }
}

pub async fn resolve(request_id:&str,mut snapshot:Value)->Result<Profile> {
    snapshot["request_id"]=json!(request_id);
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
        .json(&snapshot).send().await?.error_for_status()?.bytes().await?;
    if bytes.len()>1024*1024 {bail!("oversized tier profile response");}
    let reply:Value=serde_json::from_slice(&bytes)?;
    let annotations=serde_json::from_value(reply["annotations"].clone())?;
    let heavy=reply["is_heavy_read"].as_bool().context("missing shared heavy mark")?;
    if heavy {
        dynamo_kv_router::pending_score::register(request_id,
            snapshot["prompt_tokens"].as_u64().context("missing prompt tokens")? as usize)
            .map_err(anyhow::Error::msg)?;
    }
    tracing::debug!(request_id,heavy,"TIER_PROFILE_RESOLVED");
    Ok(Profile {request_id:request_id.into(),annotations})
}
