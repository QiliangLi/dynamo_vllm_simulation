"""Detect runtime drift on either side, independent of the current private workspace."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('runtime_sync_check',Path(__file__).resolve().parents[1]/'scripts/check_runtime_sync.py')
check=importlib.util.module_from_spec(spec);spec.loader.exec_module(check)


class RuntimeSyncTests(unittest.TestCase):
    def test_different_checkout_line_endings_preserve_code_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            a=Path(folder)/'a';b=Path(folder)/'b';a.mkdir();b.mkdir()
            (a/'model.py').write_bytes(b'x=1\n');(b/'model.py').write_bytes(b'x=1\r\n')
            manifest=dict(research_commit='fixture',files_sha256={'model.py':check.digest(a/'model.py')})
            self.assertEqual(check.verify(manifest,a,b)['files_checked'],1)
            (b/'model.py').write_bytes(b'x=2\n')
            with self.assertRaises(ValueError):check.verify(manifest,a,b)
            (b/'model.py').unlink()
            with self.assertRaises(ValueError):check.verify(manifest,a,b)

    def test_local_drift_is_detected_without_the_source_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'model.py';p.write_bytes(b'x=1\n')
            manifest=dict(research_commit='fixture',files_sha256={'model.py':check.digest(p)})
            p.write_bytes(b'x=2\n')
            with self.assertRaises(ValueError):check.verify(manifest,root)

    def test_escaping_paths_are_rejected(self):
        with self.assertRaises(ValueError):check.verify(dict(research_commit='fixture',files_sha256={'../outside':'bad'}))
