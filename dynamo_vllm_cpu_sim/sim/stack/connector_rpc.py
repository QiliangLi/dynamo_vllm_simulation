"""SimConnectorRpc：进程栈侧的真实 KVConnectorBase_V1 实现（W5）。

语义镜像单进程 DES 的 sim/connector.py（读侧远端池、命中≠就绪、分配成功才
提交读、完成经 update_from_output 回传），仅两处不同：
- ``present`` 命中查询与 ``submit`` 读提交走 storage-sim RPC（不再进程内直连）；
- 需求（max_min_fair）用与 DES run.py 相同的推导值，不支持 trace 行级
  demand_Bps 覆盖（HTTP 请求不携带该字段）。

经 KVConnectorFactory 以名字 ``SimConnector`` 注册（stack_vllm_worker.py），
worker 以 ``--kv-transfer-config '{"kv_connector":"SimConnector","kv_role":"kv_both"}'``
启用。vLLM/Dynamo 源码零修改。
"""

from __future__ import annotations

import os

from vllm.distributed.kv_transfer.kv_connector.v1.base import (
    KVConnectorBase_V1,
    KVConnectorMetadata,
)

from sim.stack.storage_client import StorageClient, client_from_env, default_demand


class Metadata(KVConnectorMetadata):
    def __init__(self,loads=None):
        self.loads = loads or {}


class SimConnectorRpc(KVConnectorBase_V1):
    def __init__(self, vllm_config, role, kv_cache_config=None):
        super().__init__(vllm_config, role, kv_cache_config)
        client = client_from_env()
        if client is None:
            raise RuntimeError(
                "SimConnectorRpc requires SIM_STACK_STORAGE_SOCKET "
                "(start sim.stack.storage_service first, see launch_stack.sh)"
            )
        self.client: StorageClient = client
        self.worker_id = int(os.environ.get("SIM_STACK_WORKER_ID", "0"))
        self.demand = default_demand()
        from sim.stack.executor import load_stack_config
        self.cfg = load_stack_config()
        from sim.stack.layerwise import transfer_mode
        self.mode = transfer_mode(self.cfg)
        self._loads = {}
        self.external_tokens: dict[str, int] = {}
        self._pending_blocks: dict[str, list] = {}
        self._pending_demands: dict[str, float] = {}

    def get_num_new_matched_tokens(self, request, num_computed_tokens):
        size = self._vllm_config.cache_config.block_size
        end = min(len(request.block_hashes), (request.num_prompt_tokens - 1) // size)
        candidates = list(request.block_hashes[num_computed_tokens // size : end])
        if not candidates:
            self._pending_blocks[request.request_id] = []
            return 0, False
        if self.cfg.get("cache_model") == "first_seen":
            present = self.client.present_for_request(request.request_id,num_computed_tokens//size,candidates)
        else:
            present = self.client.present(candidates)
        blocks = []
        for block, hit in zip(candidates, present):
            if not hit:
                break
            blocks.append(block)
        self._pending_blocks[request.request_id] = blocks
        n = len(blocks) * size
        if n and self.cfg["compute"].get("mode") == "glm5_d950pr":
            from sim.compute_time import reference_compute_s
            budget = self.cfg.get("request_profile", {}).get(
                "profile_token_budget", self.cfg["max_num_batched_tokens"])
            seconds = reference_compute_s(self.cfg["compute"], request.num_prompt_tokens,
                                          num_computed_tokens + n, budget)
            self._pending_demands[request.request_id] = (
                n * self.cfg["storage"]["kv_bytes_per_token"] / seconds)
        return n, n > 0 and self.mode == "bulk"

    def update_state_after_alloc(self, request, blocks, num_external_tokens):
        if num_external_tokens:
            wanted = self._pending_blocks.pop(request.request_id)
            assert (
                len(wanted) * self._vllm_config.cache_config.block_size
                == num_external_tokens
            )
            demand = self._pending_demands.pop(request.request_id, self.demand)
            # GLM reference gives the whole request's V/T. Storage claimants are
            # blocks, so preserve sum(block demands) == request demand.
            if self.cfg["compute"].get("mode") == "glm5_d950pr" and demand is not None:
                demand /= len(wanted)
            if self.mode != "bulk":
                load = self.client.layer_register(self.worker_id,request.request_id,wanted,demand)
                self._loads[request.request_id] = load
                self.external_tokens[request.request_id] = num_external_tokens
                return
            self.client.submit(self.worker_id, request.request_id, wanted, demand)
            from sim.stack.load_observation import observer_from_env
            observer = observer_from_env()
            if observer is not None:
                observer.submitted(request.request_id,
                                   num_external_tokens*self.cfg["storage"]["kv_bytes_per_token"])
            self.external_tokens[request.request_id] = num_external_tokens

    def build_connector_meta(self, scheduler_output):
        loads,self._loads = self._loads,{}
        return Metadata(loads)

    def start_load_kv(self, forward_context, **kwargs):
        raise RuntimeError("use event harness, not device worker")

    def wait_for_layer_load(self, layer_name):
        raise RuntimeError("CPU layer barriers run in SimExecutor, not a device ModelRunner")

    def save_kv_layer(self, layer_name, kv_layer, attn_metadata, **kwargs):
        raise RuntimeError("read-only pool")

    def wait_for_save(self):
        raise RuntimeError("read-only pool")
