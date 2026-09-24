from vllm.distributed.kv_transfer.kv_connector.v1.base import (
    KVConnectorBase_V1,
    KVConnectorMetadata,
)


class Metadata(KVConnectorMetadata):
    pass


class SimConnector(KVConnectorBase_V1):
    def __init__(self, vllm_config, role, kv_cache_config=None):
        super().__init__(vllm_config, role, kv_cache_config)
        self.store = None
        self.pending_blocks = {}
        self.submissions = []
        self.external_tokens = {}

    def get_num_new_matched_tokens(self, request, num_computed_tokens):
        size = self._vllm_config.cache_config.block_size
        end = min(len(request.block_hashes), (request.num_prompt_tokens - 1) // size)
        blocks = []
        for b in request.block_hashes[num_computed_tokens // size : end]:
            if b not in self.store.present:
                break
            blocks.append(b)
        self.pending_blocks[request.request_id] = blocks
        n = len(blocks) * size
        return n, n > 0

    def update_state_after_alloc(self, request, blocks, num_external_tokens):
        if num_external_tokens:
            wanted = self.pending_blocks.pop(request.request_id)
            assert (
                len(wanted) * self._vllm_config.cache_config.block_size
                == num_external_tokens
            )
            self.submissions.append((request.request_id, wanted))
            self.external_tokens[request.request_id] = num_external_tokens

    def build_connector_meta(self, scheduler_output):
        return Metadata()

    def start_load_kv(self, forward_context, **kwargs):
        raise RuntimeError("use event harness, not device worker")

    def wait_for_layer_load(self, layer_name):
        raise RuntimeError("layerwise mode not implemented")

    def save_kv_layer(self, layer_name, kv_layer, attn_metadata, **kwargs):
        raise RuntimeError("read-only pool")

    def wait_for_save(self):
        raise RuntimeError("read-only pool")
