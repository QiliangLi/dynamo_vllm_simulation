"""Removed layer telemetry must fail closed instead of mutating the ledger."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from sim.stack.admission_service import create_app
from sim.stack.feedback import FeedbackClient
from test_stack_admission import config


class RemovedBatchFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def rejected(self, op):
        with tempfile.TemporaryDirectory() as temporary:
            ready=Path(temporary)/'ready.json'
            cfg=config();cfg['experiment']=dict(batch_feedback=True,coalesced_feedback=True)
            app=create_app(cfg,'http://127.0.0.1:1',feedback_endpoint='tcp://127.0.0.1:0',feedback_ready_file=ready)
            async with app.router.lifespan_context(app):
                state=app.state.admission
                state.arrive(dict(id='r',prompt_token_ids=list(range(100)),remote_prefix_tokens=32))
                state.admit()
                client=FeedbackClient(json.loads(ready.read_text())['endpoint'],0)
                try:
                    before=state.controller.records['r'].submitted_read_bytes
                    with self.assertRaises(RuntimeError):
                        await asyncio.to_thread(client._exchange,dict(op=op,rows=[]))
                    self.assertIsNotNone(state.fatal)
                    self.assertTrue(client.closed)
                    self.assertEqual(state.controller.records['r'].submitted_read_bytes,before)
                    self.assertTrue(state.controller.records['r'].lease_open)
                finally:client.close()

    async def test_old_batch_frame_is_rejected_without_releasing_a_permit(self):
        await self.rejected('layer_feedback_batch')


if __name__=='__main__':unittest.main()
