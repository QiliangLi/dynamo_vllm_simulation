from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import benchmark as b
import engine as e
import install_tfs_switch as launch


class Tokens:
    all_special_ids = [0]
    def get_vocab(self): return {' word' + str(i): i for i in range(1, 101)}
    def decode(self, ids, **kwargs): return ''.join(' word' + str(i) for i in ids)
    def encode(self, s, add_special_tokens=False):
        ids = [int(x) for x in re.findall(r'word(\d+)', s)]
        return ([0] if add_special_tokens else []) + ids


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.server.lock:
            self.server.bodies.append(body)
            count = len(self.server.bodies)
        if count in getattr(self.server, 'fail_at', set()):
            self.send_error(500, 'injected request failure')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        chunk = dict(id=f'cmpl-test-{count}', choices=[dict(text='' if count % 3 == 0 else 'OK', finish_reason='length')])
        self.wfile.write(('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())


def fixture(root, name, label='anything', nonce=None, include_c=True):
    tok = root / 'tok'
    tok.mkdir(exist_ok=True)
    (tok / 'tokenizer.json').write_text('{}')
    out = root / name
    out.mkdir()
    catalog = dict(max_model_len=98000, requests=[
        dict(hit_tokens=h, miss_tokens=u, request_class=c, arrival_offset_ms=i * 10,
             block_size=128, max_tokens=1, add_special_tokens=True)
        for i, (h, u, c) in enumerate([(128, 16, 'A'), (256, 32, 'B'), (0, 64, 'C')])])
    if not include_c:
        catalog['requests']=catalog['requests'][:2]
    args = NS(benchmark='pilot24', tokenizer=tok, seed=4, label=label)
    original_read = e.read_json
    def read(path):
        return catalog if Path(path) == b.ROOT / 'catalogs/pilot24.json' else original_read(path)
    with patch.object(e, 'read_json', side_effect=read), redirect_stdout(io.StringIO()):
        if nonce:
            with patch.object(b.uuid, 'uuid4', return_value=NS(hex=nonce)):
                trace = b.build(args, out, Tokens(), list(range(1, 101)))
        else:
            trace = b.build(args, out, Tokens(), list(range(1, 101)))
    return trace


class WorkloadTests(unittest.TestCase):
    def test_labels_do_not_select_strategy_or_change_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = fixture(root, 'a', 'default', nonce='same')
            c = fixture(root, 'c', 'tfs', nonce='same')
            d = fixture(root, 'd', 'tfs')
            am, ar = e.load_trace(a)
            cm, cr = e.load_trace(c)
            dm, dr = e.load_trace(d)
            self.assertEqual(ar, cr)
            self.assertEqual(am['spec']['workload_id'], dm['spec']['workload_id'])
            self.assertNotEqual(ar[0]['first_block_sha256'], dr[0]['first_block_sha256'])
            for r in ar:
                self.assertEqual(len(e.encode(Tokens(), r['prompt'], True)), r['hit_tokens'] + r['miss_tokens'])

    def test_all_catalogs_and_public_modes(self):
        for case in b.CASES:
            rows = e.read_json(b.ROOT / 'catalogs' / (case + '.json'))['requests']
            self.assertEqual(len(rows), {'pilot24': 24, 'synthetic_abc_3w': 120}.get(case, 360))
            self.assertTrue(all(r['max_tokens'] == 1 for r in rows))
        a = b.parser().parse_args(['run', '--benchmark', 'mooncake_abc', '--label', 'arbitrary-name'])
        self.assertEqual(a.mode, 'paced')
        self.assertEqual(a.endpoint, 'http://127.0.0.1:8000/v1/completions')

    def test_three_worker_pressure_catalog(self):
        rows = e.read_json(b.ROOT / 'catalogs/synthetic_abc_3w.json')['requests']
        shapes = {'A': (30720, 256), 'B': (9344, 8192), 'C': (0, 8192)}
        for cls, shape in shapes.items():
            group = [r for r in rows if r['request_class'] == cls]
            self.assertEqual(len(group), 40)
            self.assertEqual({(r['hit_tokens'], r['miss_tokens']) for r in group}, {shape})
        self.assertEqual([r['arrival_offset_ms'] for r in rows], list(range(0, 12000, 100)))
        self.assertEqual(sum(r['hit_tokens'] > 0 for r in rows), 80)
        self.assertEqual(sum(r['hit_tokens'] * 65536 for r in rows), 105025372160)
        self.assertTrue(all(r['hit_tokens'] % 128 == 0 and r['max_tokens'] == 1 for r in rows))

    def test_real_cli_http_prepare_replay_and_duplicate_guard(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.lock = threading.Lock()
        server.bodies = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                trace = fixture(root, 'data')
                out = root / 'sent'
                cmd = [sys.executable, '-u', str(b.ROOT / 'benchmark.py'), 'send', '--trace', str(trace),
                       '--output', str(out), '--endpoint', f'http://127.0.0.1:{server.server_port}/v1/completions',
                       '--label', 'not-a-strategy', '--prepare-concurrency', '2', '--concurrency', '3', '--settle-s', '0']
                result = subprocess.run(cmd, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
                self.assertEqual(len(server.bodies), 5)
                self.assertTrue(all(set(body) == {'model', 'prompt', 'max_tokens', 'temperature', 'stream'} for body in server.bodies))
                summary = e.read_json(out / 'replay/summary.json')['groups']['ALL']
                self.assertEqual(summary['succeeded'], 3)
                self.assertEqual(summary['ttft_unavailable'], 1)
                self.assertEqual(e.read_json(out / 'replay/run.json')['label'], 'not-a-strategy')
                again = subprocess.run(cmd, capture_output=True)
                self.assertNotEqual(again.returncode, 0)
                self.assertEqual(len(server.bodies), 5)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


    def test_phase2_dump_http_payloads_and_saved_config(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.lock = threading.Lock()
        server.bodies = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for option in ('off', 'on'):
                    with self.subTest(option=option):
                        server.bodies.clear()
                        trace = fixture(root, 'data_' + option)
                        out = root / ('sent_' + option)
                        cmd = [sys.executable, str(b.ROOT / 'benchmark.py'), 'send',
                               '--trace', str(trace), '--output', str(out),
                               '--endpoint', f'http://127.0.0.1:{server.server_port}/v1/completions',
                               '--settle-s', '0', '--replay-phase2-dump', option]
                        result = subprocess.run(cmd, capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(len(server.bodies), 5)
                        expected = [True, True] + [option == 'on'] * 3
                        self.assertEqual([body['kv_transfer_params']['enable_phase2_dump']
                                          for body in server.bodies], expected)
                        self.assertTrue(all(type(body['kv_transfer_params']['enable_phase2_dump'])
                                            is bool for body in server.bodies))
                        for phase, value in [('prepare', True), ('replay', option == 'on')]:
                            cfg = e.read_json(out / phase / 'run.json')['config']
                            self.assertIs(cfg['body_extra']['kv_transfer_params']['enable_phase2_dump'], value)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_request_failures_skip_only_failed_preparation(self):
        for failures, include_c, expected_requests, skipped, replay_failed in [
                ({4}, True, 5, 0, 1), ({1}, True, 4, 1, 0),
                ({1,2}, True, 3, 2, 0), ({1,2}, False, 2, 2, 0),
                ({1,3}, True, 4, 1, 1)]:
            with self.subTest(failures=failures, include_c=include_c):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                server.lock = threading.Lock()
                server.bodies = []
                server.fail_at = failures
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        trace = fixture(root, 'data', include_c=include_c)
                        out = root / 'sent'
                        cmd = [sys.executable, str(b.ROOT / 'benchmark.py'), 'send',
                               '--trace', str(trace), '--output', str(out),
                               '--endpoint', f'http://127.0.0.1:{server.server_port}/v1/completions',
                               '--prepare-concurrency', '1', '--concurrency', '1', '--settle-s', '0']
                        result = subprocess.run(cmd, capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(len(server.bodies), expected_requests)
                        summary = e.read_json(out / 'replay/summary.json')
                        self.assertEqual(summary['groups']['ALL']['failed'], replay_failed)
                        self.assertEqual(summary['skipped_prepare_failed'], skipped)
                        self.assertEqual(summary['original_planned_count'], 3 if include_c else 2)
                        self.assertEqual(summary['recorded_count'], expected_requests-2)
                        self.assertEqual(summary['groups']['ALL']['succeeded'], expected_requests-2-replay_failed)
                        prep=list(e.jsonl(out/'prepare/requests.jsonl'))
                        replay=list(e.jsonl(out/'replay/requests.jsonl'))
                        skip_rows=list(e.jsonl(out/'replay/skipped_requests.jsonl'))
                        failed_ids={r['request_id'] for r in prep if not r['success']}
                        self.assertEqual({r['request_id'] for r in skip_rows},failed_ids)
                        self.assertFalse(failed_ids & {r['request_id'] for r in replay})
                        _,requests=e.load_trace(trace)
                        offsets={r['request_id']:r['arrival_offset_ms'] for r in requests}
                        for row in replay:
                            self.assertEqual(row['planned_arrival_ms'],offsets[row['request_id']])
                        self.assertEqual(summary['groups']['ALL']['skipped_prepare_failed'],skipped)
                        if not include_c:
                            self.assertEqual(summary['status'],'no_requests_sent')
                            self.assertIsNone(summary['groups']['ALL']['ttft_ms'])
                        elif skipped:
                            self.assertIn('skipped' if not replay_failed else 'skips',summary['status'])
                finally:
                    server.shutdown()
                    thread.join()
                    server.server_close()


class LaunchTests(unittest.TestCase):
    def original(self, name):
        text = '''#!/usr/bin/env bash
# native-ingress-20261116
export DYN_NAMESPACE=existing_namespace
export DYN_TFS_CONFIG=/etc/hosts
export DYN_TFS_VLLM=1
export VLLM_PLUGINS=tfs12,ascend,custom
export DYN_TFS_NATIVE_INGRESS=1
export DYN_TFS_INGRESS_URL=http://127.0.0.1:28766/internal/dynamo-ingress
export DYN_TIER_PROFILE_URL=http://127.0.0.1:28766/internal/dynamo-ingress/profile
export DYN_TIER_PROFILE_TOKEN=fixture-only
export DYN_WAITING_SCORE_FACTOR=0.1
export DYN_WAITING_SCORE_READY=/tmp/fixture-score
export SIM_STACK_ROUTING_SCORE_READY=/tmp/fixture-score
export RDMA_KV_CONFIG=original_config
export OD_LAYER_SUBMIT_BATCH_SIZE=1
export ASCEND_RT_VISIBLE_DEVICES=2,3
'''
        command = 'exec python -m dynamo.vllm --kv-transfer-config OdKvStorageConnector' if int(name[0]) < 4 else 'exec python -m dynamo.frontend'
        return text + "setsid bash -c '\n" + command + "\n' &\n"

    def shell(self, text, args=(), syntax=False):
        cmd = (['wsl', '-e', 'bash'] if os.name == 'nt' else ['bash'])
        cmd += ['-n'] if syntax else ['-s', '--', *args]
        return subprocess.run(cmd, input=text.encode(), capture_output=True)

    def test_all_six_preserve_launch_body_and_valid_bash(self):
        for name in launch.NAMES:
            original = self.original(name)
            new = launch.patch_script(original, name)
            self.assertEqual(original[original.index('setsid'):], new[new.index('setsid'):])
            result = self.shell(new, syntax=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            with self.assertRaisesRegex(ValueError, 'Already patched'):
                launch.patch_script(new, name)

    def test_off_clears_inherited_flags_but_preserves_compute_and_storage(self):
        for name in launch.NAMES[:5]:
            new = launch.patch_script(self.original(name), name).split('setsid bash')[0]
            test = '''
[[ -z "${DYN_TFS_CONFIG+x}" && -z "${DYN_TFS_VLLM+x}" && -z "${DYN_TFS_NATIVE_INGRESS+x}" && -z "${SIM_STACK_ROUTING_SCORE_READY+x}" ]] || exit 7
[[ "$RDMA_KV_CONFIG" == original_config && "$OD_LAYER_SUBMIT_BATCH_SIZE" == 1 && "$ASCEND_RT_VISIBLE_DEVICES" == 2,3 && "$DYN_NAMESPACE" == existing_namespace ]] || exit 8
'''
            if int(name[0]) < 4:
                test += '[[ "$VLLM_PLUGINS" == ascend,custom ]] || exit 9\n'
            result = self.shell(new + test, ['--tfs', 'off'])
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_on_default_and_explicit_and_bad_arguments(self):
        for name in launch.NAMES[:5]:
            new = launch.patch_script(self.original(name), name).split('setsid bash')[0]
            for args in ([], ['--tfs', 'on'], ['--tfs=on']):
                result = self.shell(new + '\n[[ "$DYN_TFS_CONFIG" == /etc/hosts && "$DYN_WAITING_SCORE_FACTOR" == 0.1 ]]\n', args)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            for args in (['--tfs'], ['--tfs', 'typo'], ['--unknown']):
                self.assertEqual(self.shell(new, args).returncode, 2)

    def test_central_off_does_not_execute_existing_body(self):
        text = launch.patch_script(self.original(launch.NAMES[-1]), launch.NAMES[-1])
        text = text.split('setsid bash')[0] + '\nexit 97\n'
        result = self.shell(text, ['--tfs', 'off'])
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'central not started', result.stdout)

    def test_install_and_hash_guarded_rollback(self):
        def syntax(cmd, input, check):
            result = self.shell(input.decode(), syntax=True)
            if result.returncode:
                raise ValueError(result.stderr.decode())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in launch.NAMES:
                (root / name).write_bytes(self.original(name).encode())
            # Avoid recursive mocking of subprocess.run used by shell().
            original_run = launch.subprocess.run
            def call(cmd, **kwargs):
                bash = ['wsl', '-e', 'bash', '-n'] if os.name == 'nt' else ['bash', '-n']
                return original_run(bash, **kwargs)
            with patch.object(launch.subprocess, 'run', side_effect=call), redirect_stdout(io.StringIO()):
                launch.install(root, apply=True)
            backup = next((root / 'temp').iterdir())
            name = launch.NAMES[0]
            changed = (root / name).read_bytes()
            (root / name).write_bytes(changed + b'\n# later change\n')
            with self.assertRaisesRegex(ValueError, 'changed after'):
                launch.rollback(root, backup)
            (root / name).write_bytes(changed)
            with redirect_stdout(io.StringIO()):
                launch.rollback(root, backup)
            for name in launch.NAMES:
                self.assertEqual((root / name).read_bytes(), self.original(name).encode())


if __name__ == '__main__':
    unittest.main()
