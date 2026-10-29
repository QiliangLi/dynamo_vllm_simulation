"""Exercise fragmented/coalesced JSON lines and failures with a real socket."""
import json
import socket
import threading
import unittest
from sim.stack.storage_client import StorageClient


class FramingTest(unittest.TestCase):
    def client(self):
        a,b=socket.socketpair()
        self.addCleanup(a.close);self.addCleanup(b.close)
        c=StorageClient.__new__(StorageClient);c.sock=a;c._buf=b''
        return c,b

    def test_large_fragmented_history_and_coalesced_next_reply(self):
        client,server=self.client()
        response=dict(history=['ab'*32]*20000,ok=True)
        raw=(json.dumps(response)+'\n'+json.dumps(dict(ok=True,tail=7))+'\n').encode()
        def produce():
            server.recv(65536)
            for i in range(0,len(raw),127):server.sendall(raw[i:i+127])
        t=threading.Thread(target=produce);t.start()
        self.assertEqual(client.stats(),response)
        self.assertEqual(client.stats(),dict(ok=True,tail=7));t.join(2)
        self.assertFalse(t.is_alive())

    def test_error_and_truncated_reply(self):
        client,server=self.client();server.sendall(b'{"ok":false,"error":"invalid"}\n')
        with self.assertRaisesRegex(RuntimeError,'invalid'):client.stats()
        server.sendall(b'{"unfinished":');server.shutdown(socket.SHUT_WR)
        with self.assertRaises(ConnectionError):client.stats()
