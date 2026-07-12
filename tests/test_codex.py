import json
import os
import tempfile
import unittest
from pathlib import Path

from tokenmaxx import codex
from tokenmaxx.transcript import tail_records


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_rollout(self, session_id="codex-1", cwd="/tmp/repo", records=(), mtime=1000):
        path = self.root / "2026" / "07" / "11" / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"type": "session_meta", "timestamp": "1970-01-01T00:16:40Z", "payload": {"id": session_id, "cwd": cwd}}
        path.write_text("\n".join(json.dumps(row) for row in (meta, *records)) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    def test_tail_records_returns_only_requested_valid_dicts(self):
        path = self.root / "tail.jsonl"
        path.write_text('{"n":1}\nnot-json\n{"n":2}\n{"n":3}\n')
        self.assertEqual(tail_records(path, max_lines=2), [{"n": 2}, {"n": 3}])

    def test_load_codex_sessions_reads_recent_metadata(self):
        recent = self.write_rollout()
        self.write_rollout(session_id="old", mtime=900)
        sessions = codex.load_codex_sessions(self.root, now=1000, max_session_age_hours=0.02)
        self.assertEqual([row["sessionId"] for row in sessions], ["codex-1"])
        self.assertEqual(sessions[0]["cwd"], "/tmp/repo")
        self.assertEqual(sessions[0]["updatedAt"], 1_000_000)
        self.assertEqual(sessions[0]["_path"], str(recent))
