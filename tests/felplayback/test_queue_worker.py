import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/felplayback"))
from felworker.worker import Worker, source_path, output_path
from felworker.spool import write_json, read_json

spec = importlib.util.spec_from_file_location("queue_module", ROOT / "plugins.v3/felplayback/queue.py")
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


class QueueWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media, self.playback, self.scratch = (self.root / s for s in ("media", "playback", "scratch"))
        for p in (self.media, self.playback, self.scratch): p.mkdir()
        self.movie = self.media / "Film" / "电影.mkv"
        self.movie.parent.mkdir()
        self.movie.write_bytes(b"unchanged original")
        self.map = [{"id": "sata", "mp_root": str(self.media)}]
        self.spool = q.Spool(self.root / "queue")
        self.config = {"queue": str(self.spool.root), "scratch": str(self.scratch), "reserve_gib": 0,
                       "roots": {"sata": {"source": str(self.media), "output": str(self.playback)}}}
        write_json(self.spool.root / "control.json", {"enabled": True, "dry_run": False})

    def tearDown(self):
        self.temp.cleanup()

    def ready_job(self):
        self.movie.with_suffix(".nfo").write_text("<movie><title>电影</title></movie>")
        jid = self.spool.offer(self.movie, self.map)
        self.spool.ready(self.map, 0)
        self.spool.ready(self.map, 0)
        return jid

    def test_metadata_and_stability_gate_and_idempotency(self):
        jid = self.spool.offer(self.movie, self.map)
        self.spool.ready(self.map, 0)
        self.assertFalse((self.spool.root / "requests" / (jid + ".json")).exists())
        self.movie.with_suffix(".nfo").write_text("<movie><title>电影</title></movie>")
        with patch.object(q.time, "time", return_value=100): self.spool.ready(self.map, 120)
        with patch.object(q.time, "time", return_value=219): self.spool.ready(self.map, 120)
        self.assertEqual(list((self.spool.root / "requests").iterdir()), [])
        with patch.object(q.time, "time", return_value=221): self.spool.ready(self.map, 120)
        self.assertEqual(self.spool.offer(self.movie, self.map), jid)
        self.assertEqual(len(list((self.spool.root / "requests").iterdir())), 1)

    def test_changed_source_cannot_run_old_request(self):
        jid = self.ready_job()
        self.movie.write_bytes(b"replacement")
        Worker(self.config).tick()
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "failed")
        self.assertEqual(list(self.playback.iterdir()), [])

    def test_traversal_and_symlink_escape(self):
        with self.assertRaises(ValueError): source_path(self.media, "../outside.mkv")
        (self.media / "bad.mkv").symlink_to(self.root / "outside.mkv")
        (self.root / "outside.mkv").write_text("outside")
        with self.assertRaises(ValueError): source_path(self.media, "bad.mkv")
        (self.playback / "escape").symlink_to(self.media, target_is_directory=True)
        with self.assertRaises(ValueError): output_path(self.playback, "escape/new.mkv")

    def test_copy_publication_preserves_original_and_metadata(self):
        jid = self.ready_job()
        with patch.object(Worker, "supervise", return_value={"status": "copy"}):
            Worker(self.config).tick()
        out = self.playback / "Film" / "电影.mkv"
        self.assertEqual(out.read_bytes(), self.movie.read_bytes())
        self.assertEqual(out.with_suffix(".nfo").read_bytes(), self.movie.with_suffix(".nfo").read_bytes())
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "passed")

    def test_existing_output_never_replaced(self):
        jid = self.ready_job()
        out = self.playback / "Film" / "电影.mkv"
        out.parent.mkdir(); out.write_text("existing output")
        Worker(self.config).tick()
        self.assertEqual(out.read_text(), "existing output")
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "failed")

    def test_cancelled_request_does_not_publish(self):
        jid = self.ready_job()
        write_json(self.spool.root / "cancel" / (jid + ".json"), {"requested": True})
        Worker(self.config).tick()
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "cancelled")
        self.assertFalse((self.playback / "Film/电影.mkv").exists())

    def test_dry_run_never_publishes_even_for_non_fel(self):
        jid = self.ready_job()
        write_json(self.spool.root / "control.json", {"enabled": True, "dry_run": True})
        with patch.object(Worker, "supervise", return_value={"status": "copy"}): Worker(self.config).tick()
        self.assertFalse((self.playback / "Film/电影.mkv").exists())
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "detected")

    def test_interrupted_job_cleans_orphans_and_one_retry_succeeds(self):
        jid = self.ready_job()
        work = self.scratch / jid
        work.mkdir()
        (work / "encode.log").write_text("interrupted encoder")
        (work / "BL.hevc").write_bytes(b"unfinished base layer")
        stage = self.playback / ".gb1-staging" / jid
        stage.mkdir(parents=True)
        (stage / "电影.mkv.partial").write_bytes(b"incomplete output")
        preserved = self.playback / "other.mkv"
        preserved.write_bytes(b"existing published film")
        original = self.movie.read_bytes()
        write_json(self.spool.root / "state" / (jid + ".json"), {"status": "running"})
        worker = Worker(self.config)
        worker.tick()
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "failed")
        self.assertFalse(work.exists())
        self.assertFalse(stage.exists())
        self.assertEqual((self.spool.root / "reports" / jid / "encode.log").read_text(), "interrupted encoder")
        write_json(self.spool.root / "retry" / (jid + ".json"), {"requested": True})
        with patch.object(Worker, "supervise", return_value={"status": "copy"}):
            worker.tick()
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "passed")
        self.assertEqual((self.playback / "Film/电影.mkv").read_bytes(), original)
        self.assertEqual(self.movie.read_bytes(), original)
        self.assertEqual(preserved.read_bytes(), b"existing published film")

    def test_retry_cleans_old_failed_job_artifacts_without_following_symlink(self):
        jid = self.ready_job()
        stage_root = self.playback / ".gb1-staging"
        outside = self.root / "unrelated"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        stage_root.symlink_to(outside, target_is_directory=True)
        write_json(self.spool.root / "state" / (jid + ".json"), {"status": "failed"})
        write_json(self.spool.root / "retry" / (jid + ".json"), {"requested": True})
        Worker(self.config).tick()
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertTrue((self.spool.root / "retry" / (jid + ".json")).exists())
        self.assertEqual(read_json(self.spool.root / "state" / (jid + ".json"))["status"], "failed")

    def test_unsafe_staging_directory_fails_job_without_crashing_worker(self):
        jid = self.ready_job()
        outside = self.root / "unrelated"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (self.playback / ".gb1-staging").symlink_to(outside, target_is_directory=True)
        Worker(self.config).tick()
        state = read_json(self.spool.root / "state" / (jid + ".json"))
        self.assertEqual(state["status"], "failed")
        self.assertIn("cleanup_warning", state)
        self.assertEqual((outside / "keep.txt").read_text(), "keep")


if __name__ == "__main__": unittest.main()
