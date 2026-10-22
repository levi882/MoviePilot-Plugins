import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "plugins.v3/felplayback"
pkg = types.ModuleType("fel_delivery_test")
pkg.__path__ = [str(PACKAGE)]
sys.modules[pkg.__name__] = pkg
for name in ("queue", "publish"):
    spec = importlib.util.spec_from_file_location(pkg.__name__ + "." + name, PACKAGE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
from fel_delivery_test.publish import Publisher
from fel_delivery_test.queue import Spool, write_json, read_json


class OriginalLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.downloads, self.library, self.staging = [self.root / x for x in ("Downloads", "Movie", "stage")]
        for p in (self.downloads, self.library, self.staging): p.mkdir()
        self.seed = self.downloads / "Original.mkv"
        self.seed.write_bytes(b"P7 original torrent bytes")
        self.movie = self.library / "Film.mkv"
        os.link(self.seed, self.movie)
        self.movie.with_suffix(".nfo").write_text("<movie><title>Film</title></movie>")
        self.mapping = [{"id": "sata", "mp_root": str(self.library), "download_root": str(self.downloads), "staging_root": str(self.staging)}]
        self.spool = Spool(self.root / "queue")
        self.job = self.spool.offer(self.movie, self.mapping)
        self.spool.ready(self.mapping, 0); self.spool.ready(self.mapping, 0)
        self.request = read_json(self.spool.root / "requests" / (self.job + ".json"))
        self.incoming = self.staging / self.movie.name
        self.incoming.write_bytes(b"validated rebuilt P8.1 output")
        self.report = {"status": "passed", "kind": "P7 FEL", "rpu_roundtrip_exact": True, "audio_subtitle_payloads_unchanged": True, "timing_checked": True,
                       "output_size": self.incoming.stat().st_size, "output_sha256": hashlib.sha256(self.incoming.read_bytes()).hexdigest()}
        self.publisher = Publisher(self.spool.root, self.mapping)
        self.scraped = []

    def tearDown(self):
        self.tmp.cleanup()

    def scrape(self, file):
        self.scraped.append(file.read_bytes())
        file.with_suffix(".nfo").write_text("<movie><title>Film</title><profile>P8.1</profile></movie>")
        return True, "ok"

    def test_published_movie_links_download_output_and_scrapes_new_bytes(self):
        record = self.publisher.publish(self.request, self.report, self.scrape)
        rebuilt = Path(record["rebuilt"])
        self.assertTrue(os.path.samefile(self.movie, rebuilt))
        self.assertFalse(os.path.samefile(self.movie, self.seed))
        self.assertTrue(os.path.samefile(record["backup"], self.seed))
        self.assertEqual(self.seed.read_bytes(), b"P7 original torrent bytes")
        self.assertEqual(self.scraped, [b"validated rebuilt P8.1 output"])
        self.assertFalse(self.incoming.exists())
        self.assertEqual(record["status"], "published")
        self.publisher.publish(self.request, self.report, self.scrape)
        self.assertEqual(len(self.scraped), 1)

    def test_corrupt_output_never_replaces_library_or_seed(self):
        self.incoming.write_bytes(b"corrupt")
        with self.assertRaises(ValueError): self.publisher.publish(self.request, self.report, self.scrape)
        self.assertTrue(os.path.samefile(self.seed, self.movie))
        self.assertEqual(self.scraped, [])

    def test_mp_directory_rule_change_blocks_delivery(self):
        self.request['directory'] = {'mp_root': str(self.library), 'download_root': '/changed/downloads'}
        with self.assertRaises(ValueError): self.publisher.publish(self.request, self.report, self.scrape)
        self.assertTrue(os.path.samefile(self.seed, self.movie))

    def test_changed_movie_never_replaced(self):
        self.movie.unlink(); self.movie.write_bytes(b"new edition")
        with self.assertRaises(ValueError): self.publisher.publish(self.request, self.report, self.scrape)
        self.assertEqual(self.movie.read_bytes(), b"new edition")
        self.assertEqual(self.seed.read_bytes(), b"P7 original torrent bytes")

    def test_scrape_failure_resumes_without_reencoding_or_losing_original(self):
        first = self.publisher.publish(self.request, self.report, lambda _: (False, "network unavailable"))
        self.assertEqual(first["status"], "linked")
        backup_inode = Path(first["backup"]).stat().st_ino
        second = self.publisher.publish(self.request, self.report, self.scrape)
        self.assertEqual(second["status"], "published")
        self.assertEqual(Path(second["backup"]).stat().st_ino, backup_inode)
        self.assertEqual(self.seed.read_bytes(), b"P7 original torrent bytes")

    def test_crash_after_link_recovers_safely(self):
        save = self.publisher._save
        def crash(jobid, record):
            if record["status"] == "linked": raise RuntimeError("simulated process exit")
            return save(jobid, record)
        with patch.object(self.publisher, "_save", side_effect=crash):
            with self.assertRaises(RuntimeError): self.publisher.publish(self.request, self.report, self.scrape)
        final = self.publisher.publish(self.request, self.report, self.scrape)
        self.assertEqual(final["status"], "published")
        self.assertTrue(os.path.samefile(final["backup"], self.seed))

    def test_symlink_output_path_cannot_escape(self):
        outside = self.root / "outside"; outside.mkdir()
        (self.downloads / ".FEL-Rebuilt").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): self.publisher.publish(self.request, self.report, self.scrape)
        self.assertTrue(os.path.samefile(self.seed, self.movie))
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_validation_report_never_publishes(self):
        self.report["timing_checked"] = False
        with self.assertRaises(ValueError): self.publisher.publish(self.request, self.report, self.scrape)
        self.assertTrue(os.path.samefile(self.seed, self.movie))

    def test_ordinary_media_skips_rebuild_and_queue_message_is_accurate(self):
        request = self.spool.root / "requests" / (self.job + ".json")
        request.unlink()
        self.spool.ready(self.mapping, 0, predicate=lambda _: False)
        self.assertFalse(request.exists())
        self.assertEqual(self.spool.history()[0]["status"], "skipped")
        (self.spool.root / "state" / request.name).unlink()
        self.spool.ready(self.mapping, 0, predicate=lambda _: True)
        self.assertEqual(self.spool.history()[0]["message"], "已就绪，等待处理服务")


if __name__ == "__main__": unittest.main()
