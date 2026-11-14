"""Regression tests for the source-retirement / analysis-handoff chain.

Runnable without third-party test runners:

    .venv/bin/python -m unittest discover -s tests -v

The covered incident:
  * retirement must block every new use of a source;
  * tickets accepted and frozen before retirement still render their cut;
  * analyses unfinished when retirement lands cannot hand off results;
  * results that leaked into the store under the old behavior are quarantined
    at startup and never served as effective measurements;
  * legal historical analyses and delivered masters stay available;
  * restoring a source allows new work without schema recreation.
"""
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from reel_loom.config import Settings
from reel_loom.application import Application
from reel_loom.errors import LoomError
from reel_loom.values import digest


def make_media(app):
    with app.blobs.temporary(".mp4") as sample:
        app.processes.run([
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=659:sample_rate=48000:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(sample)])
        return sample.read_bytes()


class RetirementChainTest(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix="reel-loom-test-")
        self.settings = replace(Settings.from_env(),
                                root=Path(self._folder.name), render_timeout=30)
        self.app = Application(self.settings, start_workers=False)
        self.asset = self.app.assets.ingest(make_media(self.app), "harbor-tone")
        self.asset_id = self.asset["id"]
        self.slices = [{"asset": self.asset_id, "in": 0.2, "out": 1.2, "label": "s1"},
                       {"asset": self.asset_id, "in": 1.6, "out": 2.8, "label": "s2"}]
        self.reel = self.app.reels.create({"title": "样片", "slices": self.slices}, "reel-1")
        self.app.looks.put("preview", {"kind": "video", "width": 320, "height": 180})

    def tearDown(self):
        self.app.close()
        self._folder.cleanup()

    def assert_rejected(self, code, fn):
        with self.assertRaises(LoomError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_retirement_blocks_new_uses_immediately(self):
        # Warm the availability cache first so a stale entry cannot mask the
        # state change (the original cross-role defect).
        self.app.assets.admit(self.asset_id)
        self.app.assets.retire(self.asset_id)
        self.assert_rejected("asset_retired",
                             lambda: self.app.reels.create(
                                 {"title": "退役来源", "slices": self.slices}, "reel-2"))
        self.assert_rejected("asset_retired",
                             lambda: self.app.tickets.enqueue(
                                 {"reel": self.reel["id"], "look": "preview"}, "render-2"))
        self.assert_rejected("asset_retired",
                             lambda: self.app.analysis.run(self.asset_id, "loudness", {}))
        self.assert_rejected("asset_retired",
                             lambda: self.app.frames.contact_sheet(
                                 self.asset_id, {"count": 4, "columns": 2}))

    def test_frozen_ticket_renders_after_retirement_and_packages_an_imprint(self):
        ticket = self.app.tickets.enqueue(
            {"reel": self.reel["id"], "look": "preview"}, "render-1")
        self.app.assets.retire(self.asset_id)
        finished = self.app.renderer.run_once()
        self.assertEqual(finished["state"], "succeeded")
        imprint = self.app.imprints.create(ticket["id"], {"cue_tracks": []})
        verification = self.app.imprints.verify(imprint["id"])
        self.assertTrue(verification["valid"])

    def test_interrupted_frozen_ticket_retries_against_its_snapshot(self):
        ticket = self.app.tickets.enqueue(
            {"reel": self.reel["id"], "look": "preview"}, "render-retry")
        self.app.tickets.claim()
        self.app.assets.retire(self.asset_id)
        self.app.close()
        self.app = Application(self.settings, start_workers=False)
        recovered = self.app.tickets.get(ticket["id"])
        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(recovered["error"]["error"], "worker_interrupted")
        self.app.tickets.retry(ticket["id"])
        finished = self.app.renderer.run_once()
        self.assertEqual(finished["state"], "succeeded")

    def test_analysis_handoff_blocked_when_source_retires_mid_flight(self):
        for kind, settings in (("signal", {"threshold_db": -40.0, "minimum_silence": 0.2}),
                               ("waveform", {"buckets": 40}),
                               ("loudness", {})):
            self.app.assets.restore(self.asset_id)
            admission = self.app.analysis._begin(self.asset_id, kind, settings)
            self.app.assets.retire(self.asset_id)
            self.assert_rejected(
                "asset_retired",
                lambda: self.app.analysis.persist(
                    self.asset_id, kind, settings, {"dummy": True},
                    f"fp-{kind}-{time.time()}", admission))
        self.assertEqual(self.app.analysis.list(self.asset_id), [])

    def test_leaked_analyses_are_quarantined_on_restart(self):
        good = self.app.analysis.run(self.asset_id, "signal", {})
        loudness_fp = digest({"blob": self.asset["blob"], "kind": "loudness",
                              "settings": {}, "algorithm": 1})
        self.app.assets.retire(self.asset_id)
        # Simulate a row the old build handed off while the source was retired.
        with self.app.db.write_unit() as db:
            db.execute(
                "INSERT INTO analyses(id,asset,kind,settings,result,fingerprint,created,state) "
                "VALUES(?,?,?,?,?,?,?,'valid')",
                ("analysis_leaked_1", self.asset_id, "loudness", "{}",
                 '{"standard":"EBU R128"}', loudness_fp, time.time()))
        self.app.close()
        self.app = Application(self.settings, start_workers=False)
        listed = [item["id"] for item in self.app.analysis.list(self.asset_id)]
        self.assertEqual(listed, [good["id"]])
        self.assert_rejected("not_found", lambda: self.app.analysis.get("analysis_leaked_1"))
        # Quarantine preserves the row for audit instead of deleting it.
        with self.app.db.read() as db:
            row = db.execute(
                "SELECT state FROM analyses WHERE id='analysis_leaked_1'").fetchone()
            self.assertEqual(row["state"], "invalidated")
        self.assertIn("analysis_leaked_1", self.app.analysis_recovery["invalidated"])

    def test_revalidation_after_restore_survives_later_retirement_and_restart(self):
        loudness_fp = digest({"blob": self.asset["blob"], "kind": "loudness",
                              "settings": {}, "algorithm": 1})
        self.app.assets.retire(self.asset_id)
        with self.app.db.write_unit() as db:
            db.execute(
                "INSERT INTO analyses(id,asset,kind,settings,result,fingerprint,created,state) "
                "VALUES(?,?,?,?,?,?,?,'valid')",
                ("analysis_leaked_2", self.asset_id, "loudness", "{}",
                 '{"standard":"EBU R128"}', loudness_fp, time.time()))
        self.app.close()
        self.app = Application(self.settings, start_workers=False)
        self.app.assets.restore(self.asset_id)
        revalidated = self.app.analysis.run(self.asset_id, "loudness", {})
        self.assertEqual(revalidated["id"], "analysis_leaked_2")
        self.app.assets.retire(self.asset_id)
        self.app.close()
        self.app = Application(self.settings, start_workers=False)
        listed = [item["id"] for item in self.app.analysis.list(self.asset_id)]
        self.assertEqual(listed, ["analysis_leaked_2"])

    def test_legal_history_and_delivered_masters_survive_retirement(self):
        legal = self.app.analysis.run(self.asset_id, "signal", {})
        ticket = self.app.tickets.enqueue(
            {"reel": self.reel["id"], "look": "preview"}, "render-history")
        self.app.renderer.run_once()
        artifact = self.app.tickets.get(ticket["id"])["artifacts"][0]
        self.app.assets.retire(self.asset_id)
        self.assertEqual(
            [item["id"] for item in self.app.analysis.list(self.asset_id)],
            [legal["id"]])
        self.assertTrue(self.app.blobs.resolve(artifact["blob"], verify=True).is_file())

    def test_restore_allows_new_work_without_recreating_data(self):
        self.app.assets.retire(self.asset_id)
        self.app.assets.restore(self.asset_id)
        new_ticket = self.app.tickets.enqueue(
            {"reel": self.reel["id"], "look": "preview"}, "render-restored")
        finished = self.app.renderer.run_once()
        self.assertEqual(finished["state"], "succeeded")
        again = self.app.analysis.run(self.asset_id, "waveform", {"buckets": 25})
        self.assertEqual(again["kind"], "waveform")


if __name__ == "__main__":
    unittest.main()
