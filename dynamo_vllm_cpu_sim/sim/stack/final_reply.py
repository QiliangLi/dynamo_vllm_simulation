"""A validated final reply releases the caller; bounded tasks drain stream tails.

All mutations of task state run on the owning asyncio loop. Other threads may
read the first failure through check(). Acknowledged operations are never
replayed or rolled back because their response stream later fails.
"""
import asyncio
import time


class FinalReplyStreams:
    def __init__(self, timeout_s, *, max_pending=64, observe=None):
        if timeout_s <= 0 or max_pending < 1:
            raise ValueError('positive drain timeout and capacity required')
        self.timeout_s = timeout_s
        self.max_pending = max_pending
        self.observe = observe
        self.failure = None
        self.pending = set()
        self.lock = asyncio.Lock()
        self.replies = self.drained = self.failed = self.peak_pending = 0

    def check(self):
        if self.failure is not None:
            raise RuntimeError('admission response stream failed after final reply') from self.failure

    def snapshot(self):
        return dict(status='failed' if self.failure else 'passed', final_replies=self.replies,
                    drained_streams=self.drained, failed_streams=self.failed,
                    pending_streams=len(self.pending), peak_pending_streams=self.peak_pending,
                    max_pending_streams=self.max_pending,
                    failure=repr(self.failure) if self.failure else None)

    def emit(self, phase, **fields):
        if self.observe:
            self.observe(phase, **fields)

    def fail(self, error):
        if self.failure is None:
            self.failure = error
            self.emit('failed', error=repr(error))

    async def close_iterator(self, iterator):
        close = getattr(iterator, 'aclose', None)
        if close is not None:
            async with asyncio.timeout(self.timeout_s):
                await close()

    async def drain(self, iterator, call_id):
        started = time.monotonic_ns()
        try:
            async with asyncio.timeout(self.timeout_s):
                try:
                    await anext(iterator)
                except StopAsyncIteration:
                    self.emit('stream_ended', call_id=call_id, start_ns=started)
                    return
                raise RuntimeError('admission RPC returned data after its final reply')
        finally:
            await self.close_iterator(iterator)

    def done(self, task):
        if task not in self.pending:
            return
        self.pending.remove(task)
        try:
            task.result()
        except BaseException as error:
            self.failed += 1
            self.fail(error)
        else:
            self.drained += 1

    async def request(self, client, instance_id, request, validate, phase):
        # Serial through the final reply, not through EOF. This retains the
        # mutation order of acquire, force_acquire and rollback on one client.
        async with self.lock:
            for task in tuple(self.pending):
                if task.done():
                    self.done(task)
            self.check()
            if len(self.pending) >= self.max_pending:
                error = RuntimeError('admission response drain capacity exceeded')
                self.fail(error)
                raise error  # Do not silently move EOF waiting into this queue.
            phase['stage'] = 'await_direct'
            stream = await client.direct(request, instance_id)
            iterator = stream.__aiter__()
            phase['stage'] = 'await_first_response'
            try:
                try:
                    item = await anext(iterator)
                except StopAsyncIteration as error:
                    raise ConnectionError('admission RPC ended without a final reply') from error
                response = item.data()
                validate(response)
                self.check()
            except BaseException:
                await self.close_iterator(iterator)
                raise
            call_id = request['call_id']
            task = asyncio.create_task(self.drain(iterator, call_id), name='admission-drain-'+call_id)
            self.pending.add(task)
            self.replies += 1
            self.peak_pending = max(self.peak_pending, len(self.pending))
            task.add_done_callback(self.done)
            phase['stage'] = 'final_reply_validated'
            self.emit('final_reply', call_id=call_id, op=request['body'].get('op'),
                      request_id=request['body'].get('request_id'), pending_streams=len(self.pending))
            return response

    async def settle(self):
        while self.pending:
            tasks = tuple(self.pending)
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in tasks:
                self.done(task)
        self.check()
        return self.snapshot()
