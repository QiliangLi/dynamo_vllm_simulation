"""WSL mirror safety: exercise real rsync without importing the simulator."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('wsl_launcher', Path(__file__).resolve().parents[1] / 'scripts/wsl.py')
wsl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wsl)


@unittest.skipUnless(shutil.which('rsync'), 'rsync is provided by the WSL environment')
class WSLMirrorTest(unittest.TestCase):
    def test_sync_removes_stale_code_and_preserves_environment_and_results(self):
        with tempfile.TemporaryDirectory() as temp:
            source, dest = Path(temp)/'Windows 源代码 with spaces', Path(temp)/'Linux 工作目录'
            (source/'scripts').mkdir(parents=True)
            (source/'scripts/entry.sh').write_bytes(b'echo hello\r\n')
            (source/'old.py').write_text('old')
            wsl.mirror(source,dest)
            for name in ('.venv','upstream','results'):
                (dest/name).mkdir()
                (dest/name/'keep').write_text('preserve')
                (source/name).mkdir()
                (source/name/'do-not-copy').write_text('source runtime')
            (source/'old.py').unlink()
            (source/'new.py').write_text('new')
            wsl.mirror(source,dest)
            self.assertFalse((dest/'old.py').exists())
            self.assertEqual((dest/'new.py').read_text(),'new')
            self.assertEqual((dest/'scripts/entry.sh').read_bytes(),b'echo hello\n')
            for name in ('.venv','upstream','results'):
                self.assertEqual((dest/name/'keep').read_text(),'preserve')
                self.assertFalse((dest/name/'do-not-copy').exists())

    def test_unknown_destination_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            source,dest=Path(temp)/'source',Path(temp)/'other-work'
            source.mkdir();dest.mkdir();(dest/'important').write_text('keep')
            with self.assertRaises(RuntimeError):wsl.mirror(source,dest)
            self.assertEqual((dest/'important').read_text(),'keep')

    def test_nested_source_and_destination_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source';source.mkdir()
            with self.assertRaises(RuntimeError):wsl.mirror(source,source/'runtime')

    def test_runtime_rejects_relative_and_windows_mounted_paths(self):
        for path in ('relative','/mnt/c/runtime'):
            with self.assertRaises(RuntimeError):wsl.state_path(path)

    def test_source_runtime_symlinks_do_not_replace_linux_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            source,dest,other=Path(temp)/'source',Path(temp)/'runtime',Path(temp)/'elsewhere'
            source.mkdir();other.mkdir()
            wsl.mirror(source,dest)
            for name in ('.venv','upstream','results'):
                (dest/name).mkdir();(dest/name/'keep').write_text('preserve')
                (source/name).symlink_to(other,target_is_directory=True)
            wsl.mirror(source,dest)
            for name in ('.venv','upstream','results'):
                self.assertFalse((dest/name).is_symlink())
                self.assertEqual((dest/name/'keep').read_text(),'preserve')

    def test_stack_logs_are_preserved_without_runtime_socket_or_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            work,logs=Path(temp)/'work',Path(temp)/'tmp-logs'
            case=logs/'sample';case.mkdir(parents=True)
            (case/'worker0.log').write_text('real log')
            (case/'worker0.pid').write_text('123')
            (case/'storage.sock').write_text('placeholder')
            wsl.preserve_stack_logs(work,['--name','sample'],logs)
            destination=work/'results/sample/logs'
            self.assertEqual((destination/'worker0.log').read_text(),'real log')
            self.assertFalse((destination/'worker0.pid').exists())
            self.assertFalse((destination/'storage.sock').exists())


if __name__=='__main__':unittest.main()
