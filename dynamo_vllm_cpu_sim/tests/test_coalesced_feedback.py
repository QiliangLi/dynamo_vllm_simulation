"""Legacy completion telemetry cannot silently re-enable the removed RPCs."""
import unittest
import test_batch_feedback as fixtures


class RemovedCoalescedFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_flush_frame_is_rejected_without_releasing_a_permit(self):
        await fixtures.RemovedBatchFeedbackTests.rejected(self,'layer_feedback_flush')
