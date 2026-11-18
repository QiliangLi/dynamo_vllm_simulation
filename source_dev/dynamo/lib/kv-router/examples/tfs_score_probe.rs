//! CPU protocol fixture using the actual TFS ledger. No model executor is created.
#[cfg(feature = "tfs-admission")]
fn main() {
    use dynamo_kv_router::{pending_score, protocols::WorkerWithDpRank};
    use std::io::{BufRead, Write};
    let id = std::env::var("TFS12_PROBE_REQUEST_ID").expect("probe request ID");
    let worker = WorkerWithDpRank::new(7, 1);
    pending_score::initialize();
    pending_score::register(&id, 160).unwrap();
    // Exercise the real projection, selector, full booking and bind hooks.
    // Only the event publisher/discovery data are local CPU fixtures.
    use dynamo_kv_router::protocols::WorkerConfigLike;
    use dynamo_kv_router::protocols::{ActiveLoad, ActiveSequenceEvent};
    use dynamo_kv_router::sequences::{ActiveSequencesMultiWorker, SequencePublisher};
    use dynamo_kv_router::{DefaultWorkerSelector, SchedulerQueue, SchedulingRequest};
    use std::collections::HashMap;
    use std::sync::Arc;
    #[derive(Clone)]
    struct Config;
    impl WorkerConfigLike for Config {
        fn data_parallel_start_rank(&self) -> u32 {
            1
        }
        fn data_parallel_size(&self) -> u32 {
            1
        }
        fn max_num_batched_tokens(&self) -> Option<u64> {
            Some(4096)
        }
        fn total_kv_blocks(&self) -> Option<u64> {
            Some(4096)
        }
    }
    struct Publisher;
    impl SequencePublisher for Publisher {
        async fn publish_event(&self, _: &ActiveSequenceEvent) -> anyhow::Result<()> {
            Ok(())
        }
        fn publish_load(&self, _: ActiveLoad) {}
        fn observe_load(&self, _: &WorkerWithDpRank, _: &str, _: usize, _: usize) {}
    }
    let runtime = tokio::runtime::Runtime::new().unwrap();
    let slots = Arc::new(ActiveSequencesMultiWorker::new(
        Publisher,
        16,
        HashMap::from([(7, (1, 1))]),
        false,
        0,
        "test",
    ));
    let (_tx, rx) = tokio::sync::watch::channel(HashMap::from([(7, Config)]));
    let queue = SchedulerQueue::new(
        slots.clone(),
        rx,
        None,
        16,
        DefaultWorkerSelector::new(None, "test"),
        dynamo_kv_router::scheduling::policy::FcfsPolicy,
        None,
    );
    let (tx, rx) = tokio::sync::oneshot::channel();
    let request = SchedulingRequest {
        maybe_request_id: Some(id.clone()),
        isl_tokens: 160,
        token_seq: Some((0..10).collect()),
        track_prefill_tokens: true,
        update_states: true,
        resp_tx: Some(tx),
        ..Default::default()
    };
    let booking = runtime.block_on(async {
        queue.enqueue(request).await;
        rx.await.unwrap().unwrap()
    });
    assert_eq!(booking.best_worker, worker);
    assert_eq!(
        slots.active_tokens(tokio::time::Instant::now())[&worker],
        160
    );
    println!("ready");
    std::io::stdout().flush().unwrap();
    for line in std::io::stdin().lock().lines() {
        match line.unwrap().as_str() {
            "finish" => {
                slots.free(&id, tokio::time::Instant::now()).unwrap();
                assert!(slots.active_blocks().values().all(|count| *count == 0));
                println!("finished");
                std::io::stdout().flush().unwrap();
            }
            "quit" => break,
            _ => panic!("unsupported probe command"),
        }
    }
    drop(booking); // Receiver/dispatch cleanup also works without an explicit finish.
}

#[cfg(not(feature = "tfs-admission"))]
fn main() {
    panic!("build this probe with --features tfs-admission");
}
