"""Real Dynamo selector; explicit cache-blind baseline, not a mock router."""

import os
import asyncio
import time

os.environ["DYN_ROUTER_USE_KV_EVENTS"] = "false"
os.environ["DYN_ROUTER_ASSUME_KV_REUSE"] = "false"
os.environ["DYN_ROUTER_KV_OVERLAP_SCORE_CREDIT"] = "0"
os.environ["DYN_ROUTER_TEMPERATURE"] = "0"
os.environ.setdefault("DYN_LOG", "error")
from dynamo.llm import SelectionService


class Router:
    def __init__(self):
        self.service = SelectionService(indexer_threads=1)
        self.active = {}

    async def settle(self):
        # Drain async native accounting before the next virtual-time decision.
        deadline = time.monotonic() + 10
        while True:
            actual = {
                int(w["worker_id"]): w for m in self.service.loads() for w in m["loads"]
            }
            expected = {}
            for wid, n in self.active.values():
                count, tokens = expected.get(wid, (0, 0))
                expected[wid] = count + 1, tokens + n
            if all(
                (w["active_requests"], w["potential_prefill_tokens"])
                == expected.get(wid, (0, 0))
                for wid, w in actual.items()
            ) and all(wid in actual for wid in expected):
                return
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"accounting not settled: {actual}; expected {expected}"
                )
            await asyncio.sleep(0.0001)

    async def start(self, c):
        for wid in range(c["workers"]):
            await self.service.upsert_worker(
                {
                    "worker_id": wid,
                    "model_name": "sim-model",
                    "endpoint": f"http://sim-worker-{wid}:1",
                    "block_size": c["block_size"],
                    "max_num_batched_tokens": c["max_num_batched_tokens"],
                    "total_kv_blocks": c["num_blocks"],
                    "stable_routing_id": f"sim-worker-{wid}",
                }
            )

    async def route(self, row):
        result = await self.service.select_and_reserve(
            {
                "model_name": "sim-model",
                "selection_id": row["id"],
                "token_ids": row["prompt_token_ids"],
                "expected_output_tokens": row["output_tokens"],
                "router_config_override": {
                    "overlap_score_credit": 0.0,
                    "assume_kv_reuse": False,
                },
            }
        )
        self.active[row["id"]] = result["worker_id"], len(row["prompt_token_ids"])
        await self.settle()
        return result["worker_id"]

    async def first_token(self, rid):
        await self.service.prefill_complete(rid)
        self.active[rid] = (self.active[rid][0], 0)
        await self.settle()

    async def finish(self, rid):
        await self.service.free_reservation(rid)
        del self.active[rid]
        await self.settle()

    async def close(self):
        await self.service.shutdown_async()
