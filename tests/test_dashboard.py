"""Smoke tests against a fake Claude data folder. Run with: python -m unittest -v"""
import importlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "33333333-3333-4333-8333-333333333333"
SID = "44444444-4444-4444-8444-444444444444"


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))


def make_fixture(root: Path):
    now = int(time.time() * 1000)
    app, claude, cfg = root / "app", root / "claude", root / "cfg"
    proj_dir = root / "work" / "my-project"
    proj_dir.mkdir(parents=True)

    # usage history: account A maxed out 5h, account B idle
    samples = [{"t": now - 3_600_000 + i * 900_000, "org": ORG_A, "u": {"fh": min(100, 30 * (i + 1)), "sd": 40}} for i in range(4)]
    samples += [{"t": now - 600_000, "org": ORG_B, "u": {"fh": 0, "sd": 5}}]
    app.mkdir(parents=True)
    (app / "plan-usage-history.json").write_text(json.dumps({"version": 2, "samples": samples}), encoding="utf-8")

    # desktop session owned by account A
    sess_dir = app / "claude-code-sessions" / ACCOUNT / ORG_A
    sess_dir.mkdir(parents=True)
    (sess_dir / "local_x.json").write_text(json.dumps({
        "sessionId": "local_x", "cliSessionId": SID, "cwd": str(proj_dir), "title": "Build the thing",
        "createdAt": now - 3_000_000, "lastActivityAt": now - 60_000, "model": "claude-test", "isArchived": False,
    }), encoding="utf-8")

    # transcript
    tdir = claude / "projects" / "my-project"
    tdir.mkdir(parents=True)
    base = {"sessionId": SID, "cwd": str(proj_dir), "gitBranch": "main", "entrypoint": "claude-desktop"}
    lines = [
        {**base, "type": "custom-title", "customTitle": "Build the thing"},
        {**base, "type": "user", "turnOrigin": "human", "timestamp": iso(now - 3_000_000),
         "message": {"role": "user", "content": "Please build the thing"}},
        {**base, "type": "assistant", "timestamp": iso(now - 2_990_000),
         "message": {"id": "m1", "model": "claude-test", "content": [
             {"type": "text", "text": "Starting."},
             {"type": "tool_use", "name": "Edit", "input": {"file_path": str(proj_dir / "app.py")}}],
             "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7}}},
        {**base, "type": "file-history-delta", "trackingPath": "app.py"},
        {**base, "type": "user", "isCompactSummary": True, "turnOrigin": "human", "timestamp": iso(now - 2_000_000),
         "message": {"role": "user", "content": "This session is being continued...\n\nSummary:\nWe built half the thing."}},
        {**base, "type": "user", "turnOrigin": "human", "timestamp": iso(now - 1_000_000),
         "message": {"role": "user", "content": "Continue please"}},
        {**base, "type": "assistant", "timestamp": iso(now - 990_000),
         "message": {"id": "m2", "model": "claude-test", "content": [{"type": "text", "text": "Almost done."}],
                     "usage": {"input_tokens": 1, "output_tokens": 2}}},
        {**base, "type": "assistant", "error": "rate_limit", "isApiErrorMessage": True, "timestamp": iso(now - 900_000),
         "message": {"id": "m3", "model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your session limit · resets 8:20pm (UTC)"}]}},
    ]
    with open(tdir / f"{SID}.jsonl", "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return app, claude, cfg, proj_dir


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="claude-dash-test-"))
        app, claude, cfg, cls.proj = make_fixture(cls.tmp)
        cls._env = {k: os.environ.get(k) for k in ("CLAUDE_DASH_APP_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_DASH_CONFIG_DIR")}
        os.environ.update(CLAUDE_DASH_APP_DIR=str(app), CLAUDE_CONFIG_DIR=str(claude), CLAUDE_DASH_CONFIG_DIR=str(cfg))
        import claude_dash.server as server
        cls.S = importlib.reload(server)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_accounts_and_limits(self):
        d = self.S.build_data()
        orgs = {a["org"]: a for a in d["accounts"]}
        self.assertEqual(set(orgs), {ORG_A, ORG_B})
        self.assertEqual(orgs[ORG_A]["limits"]["fh"], 100)
        self.assertEqual(d["best"], ORG_B)
        self.assertEqual(orgs[ORG_A]["sessions"], 1)
        self.assertIsNotNone(orgs[ORG_A]["last_limit_hit"])

    def test_session_tokens_and_limit_hit(self):
        s = next(r for r in self.S.build_data()["sessions"] if r["id"] == SID)
        self.assertEqual(s["title"], "Build the thing")
        self.assertEqual(s["prompts"], 2)  # compaction summary is not a prompt
        self.assertEqual(s["tokens"]["output"], 22)
        self.assertEqual(s["limit_hit"][2], "session")

    def test_export_compact_and_last_turns(self):
        full = self.S.export_chat(SID, 0, True, True)
        self.assertEqual(full["total"], 2)
        self.assertIn("Please build the thing", full["text"])
        self.assertIn("Earlier context", full["text"])
        self.assertIn("edited `", full["text"])
        self.assertIn("hit your session limit", full["text"])
        last = self.S.export_chat(SID, 1, True, True)
        self.assertEqual(last["turns"], 1)
        self.assertIn("We built half the thing", last["text"])  # summary covers skipped turns
        self.assertNotIn("Please build the thing", last["text"])

    def test_brief(self):
        b = self.S.handoff_brief(SID)
        self.assertIn("Continue please", b)
        self.assertIn("We built half the thing", b)
        self.assertNotIn("This session is being continued", b.split("My last requests were:")[1].split("Context summary")[0])

    def test_http_server(self):
        srv = self.S.ThreadingHTTPServer(("127.0.0.1", 0), self.S.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
            self.assertIn("<title>Claude Accounts</title>", html)
            data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/data").read())
            self.assertEqual(len(data["accounts"]), 2)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/nick", method="POST",
                                         data=json.dumps({"org": ORG_A, "nick": "Work"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req).read()
            self.assertEqual(json.loads(self.S.NICK_FILE.read_text(encoding="utf-8"))[ORG_A], "Work")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_cli_paths(self):
        self.assertEqual(self.S.main(["--paths"]), 0)


if __name__ == "__main__":
    unittest.main()
