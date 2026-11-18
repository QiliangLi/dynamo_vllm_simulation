//! One shared-storage profile, resolved once after lookup and before scoring.
use anyhow::{Context, Result, bail};
use dynamo_kv_router::scheduling::TierOverlapBlocks;
use serde_json::{Value, json};
use std::sync::OnceLock;
use std::time::Duration;

pub fn enabled() -> bool {
    dynamo_kv_router::pending_score::enabled()
}

pub fn snapshot(overlap: &TierOverlapBlocks, block_size: u32, prompt_tokens: usize) -> Value {
    // Equal instances expose replicas of the same shared prefix. Take one
    // prefix length, never sum replica hits or build per-worker profiles.
    let disk_blocks = overlap.disk.values().copied().max().unwrap_or(0);
    json!({"version":1,"block_size":block_size,"prompt_tokens":prompt_tokens,
           "disk_blocks":disk_blocks})
}

#[derive(Debug)]
pub struct Profile {
    request_id: String,
    pub annotations: Vec<String>,
}
impl Drop for Profile {
    fn drop(&mut self) {
        // Selection can fail before a resource booking is created.
        dynamo_kv_router::pending_score::discard_unbound(&self.request_id);
    }
}

pub async fn resolve(request_id: &str, mut snapshot: Value) -> Result<Profile> {
    snapshot["request_id"] = json!(request_id);
    let reply: Value = if crate::http::service::tfs_ingress::enabled() {
        crate::http::service::tfs_ingress::resolve_profile(request_id, &snapshot).await?
    } else {
        let parsed = reqwest::Url::parse(&std::env::var("DYN_TIER_PROFILE_URL")?)?;
        if parsed.scheme() != "http" || parsed.host_str() != Some("127.0.0.1") {
            bail!("tier profile service must use loopback HTTP");
        }
        static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
        let client = CLIENT.get_or_init(|| {
            reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(5))
                .build()
                .expect("tier HTTP client")
        });
        let bytes = client
            .post(parsed)
            .header(
                "x-tier-profile-token",
                std::env::var("DYN_TIER_PROFILE_TOKEN")?,
            )
            .json(&snapshot)
            .send()
            .await?
            .error_for_status()?
            .bytes()
            .await?;
        if bytes.len() > 1024 * 1024 {
            bail!("oversized tier profile response");
        }
        serde_json::from_slice(&bytes)?
    };
    let annotations = serde_json::from_value(reply["annotations"].clone())?;
    let heavy = reply["is_heavy_read"]
        .as_bool()
        .context("missing shared heavy mark")?;
    if heavy {
        let registered = dynamo_kv_router::pending_score::register(
            request_id,
            snapshot["prompt_tokens"]
                .as_u64()
                .context("missing prompt tokens")? as usize,
        );
        if let Err(error) = registered {
            tracing::warn!(request_id, %error, "TFS score registration unavailable; using full load");
            dynamo_kv_router::pending_score::degrade_request(request_id);
        }
    }
    tracing::debug!(request_id, heavy, "TIER_PROFILE_RESOLVED");
    Ok(Profile {
        request_id: request_id.into(),
        annotations,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use dynamo_kv_router::protocols::WorkerWithDpRank;

    #[test]
    fn shared_prefix_uses_maximum_not_replica_sum() {
        let mut tiers = TierOverlapBlocks::default();
        tiers.disk.insert(WorkerWithDpRank::new(1, 0), 9);
        tiers.disk.insert(WorkerWithDpRank::new(2, 0), 9);
        tiers.disk.insert(WorkerWithDpRank::new(2, 1), 7);
        assert_eq!(snapshot(&tiers, 16, 160)["disk_blocks"], 9);
        assert_eq!(
            snapshot(&TierOverlapBlocks::default(), 16, 160)["disk_blocks"],
            0
        );
    }
}

/// Validate the transport at startup, before publishing the score ready file.
pub fn validate_endpoint() -> Result<()> {
    let url = reqwest::Url::parse(&std::env::var("DYN_TIER_PROFILE_URL")?)?;
    if url.scheme() != "http" || url.host_str() != Some("127.0.0.1") {
        bail!("tier profile service must use loopback HTTP");
    }
    Ok(())
}
