"""Exercise routing against the pinned native Dynamo reservation service."""
import unittest

from sim.router import Router


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_robin_ignores_unequal_load_and_releases_native_reservations(self):
        router = Router("round-robin")
        try:
            await router.start(dict(workers=3, block_size=1,
                                    max_num_batched_tokens=8192, num_blocks=196114))
            # Worker 0 deliberately gets the largest prompts; it must still
            # receive turns 0, 3, 6 while all earlier requests remain active.
            rows = [dict(id=f"r{i}", prompt_token_ids=[1]*(1000 if i % 3 == 0 else 8),
                         output_tokens=1) for i in range(7)]
            assigned = [await router.route(row) for row in rows]
            self.assertEqual(assigned, [0, 1, 2, 0, 1, 2, 0])
            loads = {w["worker_id"]: w for m in router.service.loads() for w in m["loads"]}
            self.assertEqual([loads[i]["active_requests"] for i in range(3)], [3, 2, 2])
            self.assertEqual([loads[i]["potential_prefill_tokens"] for i in range(3)], [3000, 16, 16])
            await router.first_token("r0")
            loads = {w["worker_id"]: w for m in router.service.loads() for w in m["loads"]}
            self.assertEqual(loads[0]["active_requests"], 3)
            self.assertEqual(loads[0]["potential_prefill_tokens"], 2000)
            for row in rows:
                if row["id"] != "r0":
                    await router.first_token(row["id"])
                await router.finish(row["id"])
            self.assertFalse(router.active)
            self.assertTrue(all(w[k] == 0 for m in router.service.loads() for w in m["loads"]
                                for k in ("active_requests", "potential_prefill_tokens", "potential_decode_blocks")))
        finally:
            await router.close()

    def test_unknown_mode_fails_before_creating_service(self):
        with self.assertRaises(ValueError):
            Router("unknown")


if __name__ == "__main__":
    unittest.main()
