"""实验专用AF_UNIX连接队列容量；不改变RPC操作、模拟带宽或锁顺序。"""
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket

original_socket=socket.socket


class ExperimentSocket(original_socket):
    def listen(self,backlog=128):
        requested=backlog
        if self.family==socket.AF_UNIX:
            backlog=max(1024,backlog)
            folder=Path(os.environ['SIM_STACK_CONFIG']).parent
            (folder/'storage.transport.json').write_text(json.dumps(dict(
                requested_backlog=requested,effective_backlog=backlog,
                adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2)+'\n')
        return super().listen(backlog)


socket.socket=ExperimentSocket
import sim.stack.storage_service as service
original_bind=service.Service.op_bind


def bind_reference(self,engine_id,source_id,prompt_token_ids,hit_tokens):
    if prompt_token_ids is None:
        prompt_token_ids=self.trace_by_id[source_id]['prompt_token_ids']
    return original_bind(self,engine_id,source_id,prompt_token_ids,hit_tokens)


service.Service.op_bind=bind_reference
service.main()
