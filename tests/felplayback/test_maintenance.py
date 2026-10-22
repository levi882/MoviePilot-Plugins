import fcntl
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
import zipfile
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[2] / 'plugins.v3/felplayback'
pkg = types.ModuleType('fel_maintenance_test'); pkg.__path__ = [str(PACKAGE)]
sys.modules[pkg.__name__] = pkg
for name in ('queue', 'maintenance'):
    spec = importlib.util.spec_from_file_location(pkg.__name__ + '.' + name, PACKAGE / (name + '.py'))
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
from fel_maintenance_test.queue import Spool, read_json, write_json
from fel_maintenance_test.maintenance import Maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.spool = Spool(self.root / 'queue'); self.service = Maintenance(self.spool)
        self.media = self.root / 'Movie'; self.media.mkdir()
        self.original = self.media / 'Original.mkv'; self.original.write_bytes(b'original')
        self.output = self.media / 'Rebuilt.mkv'; self.output.write_bytes(b'rebuilt')
        self.mapping = [{'id': 'main', 'mp_root': str(self.media)}]
        self.job = self.spool.offer(self.original, self.mapping)
        self.state('passed', result={'kind': 'P7 FEL'})
        write_json(self.spool.root / 'delivery' / (self.job + '.json'), {'status': 'published', 'message': 'done'})
        self.logs = self.spool.root / 'reports' / self.job; self.logs.mkdir(parents=True)
        (self.logs / 'encode.log').write_text('safe test log\n' * 100)
        write_json(self.spool.root / 'reports' / (self.job + '.json'), {'status': 'passed'})
        write_json(self.spool.root / 'requests' / (self.job + '.json'), {'id': self.job})

    def tearDown(self): self.tmp.cleanup()

    def state(self, status, **kw):
        write_json(self.spool.root / 'state' / (self.job + '.json'), {'status': status, 'updated_at': time.time() - 120, **kw})

    def test_cleanup_restore_preserves_media_and_idempotency(self):
        before = {name: (self.spool.root / name / (self.job + '.json')).read_bytes() for name in ('requests','state','delivery','reports')}
        result = self.service.clean()
        self.assertEqual((result['history'], result['log_files']), (1, 1))
        self.assertEqual(self.spool.history(), [])
        self.assertEqual(self.spool.offer(self.original, self.mapping), self.job)
        self.assertEqual(self.spool.history(), [])
        for name, data in before.items(): self.assertEqual((self.spool.root / name / (self.job + '.json')).read_bytes(), data)
        self.assertEqual(self.original.read_bytes(), b'original'); self.assertEqual(self.output.read_bytes(), b'rebuilt')
        self.assertFalse(self.logs.exists())
        archive = next((self.spool.root / 'archive').rglob('*.zip'))
        with zipfile.ZipFile(archive) as z: self.assertEqual(z.read('encode.log'), b'safe test log\n' * 100)
        self.assertTrue(self.service.restore()['success'])
        self.assertEqual(len(self.spool.history()), 1)
        self.assertEqual((self.logs / 'encode.log').read_bytes(), b'safe test log\n' * 100)
        self.assertTrue(archive.exists())

    def test_retry_state_change_reappears(self):
        self.service.clean(); self.state('running')
        self.assertEqual(self.spool.history()[0]['status'], 'passed')  # Published receipt remains authoritative.
        write_json(self.spool.root / 'delivery' / (self.job + '.json'), {'status': 'failed'})
        self.assertEqual(self.spool.history()[0]['status'], 'delivery_failed')

    def test_failures_and_pending_publication_keep_history_and_logs(self):
        for status in ('failed', 'running', 'queued', 'passed'):
            (self.spool.root / 'delivery' / (self.job + '.json')).unlink(missing_ok=True)
            self.state(status, result={'kind': 'P7 FEL'})
            result = self.service.clean()
            self.assertEqual(result['history'], 0); self.assertEqual(result['log_files'], 0)
            self.assertTrue((self.logs / 'encode.log').exists())

    def test_pending_retry_and_recent_completion_skipped(self):
        retry = self.spool.root / 'retry' / (self.job + '.json'); write_json(retry, {'requested': True})
        self.assertEqual(self.service.clean()['history'], 0)
        retry.unlink(); self.state('skipped', updated='unused')
        write_json(self.spool.root / 'state' / (self.job + '.json'), {'status': 'skipped', 'updated_at': time.time()})
        self.assertEqual(self.service.clean()['history'], 0)

    def test_live_worker_must_be_paused_idle_and_fresh(self):
        with (self.spool.root / 'worker.lock').open('a') as worker:
            fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write_json(self.spool.root / 'control.json', {'enabled': True, 'paused': False})
            write_json(self.spool.root / 'heartbeat.json', {'at': time.time(), 'active': None})
            self.assertFalse(self.service.clean()['success'])
            write_json(self.spool.root / 'control.json', {'enabled': True, 'paused': True})
            write_json(self.spool.root / 'heartbeat.json', {'at': time.time(), 'active': self.job})
            self.assertFalse(self.service.clean()['success'])
            write_json(self.spool.root / 'heartbeat.json', {'at': time.time() - 60, 'active': None})
            self.assertFalse(self.service.clean()['success'])
            write_json(self.spool.root / 'heartbeat.json', {'at': time.time(), 'active': None})
            self.assertTrue(self.service.clean()['success'])

    def test_busy_publisher_is_not_interrupted(self):
        with (self.spool.root / 'delivery.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(self.service.clean()['success'])
            self.assertTrue((self.logs / 'encode.log').exists())

    def test_symlink_or_unknown_log_entries_not_archived(self):
        (self.logs / 'external.log').symlink_to(self.original)
        self.assertEqual(self.service.clean(history=False)['log_files'], 0)
        self.assertEqual(self.original.read_bytes(), b'original')

    def test_archive_failure_leaves_original_logs_and_history(self):
        with patch.object(zipfile.ZipFile, 'testzip', return_value='encode.log'):
            with self.assertRaises(ValueError): self.service.clean()
        self.assertTrue((self.logs / 'encode.log').exists()); self.assertEqual(len(self.spool.history()), 1)

if __name__ == '__main__': unittest.main()
