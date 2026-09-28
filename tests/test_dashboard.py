"""Smoke tests against a fake Claude data folder. Run with: python -m unittest -v"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "33333333-3333-4333-8333-333333333333"
SID = "44444444-4444-4444-8444-444444444444"
SID_CLI = "55555555-5555-4555-8555-555555555555"   # CLI session in a worktree, open right now, a background job
SID_DEL = "66666666-6666-4666-8666-666666666666"   # deleted in the desktop app


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
             {"type": "tool_use", "name": "Edit", "input": {"file_path": str(proj_dir / "app.py")}},
             {"type": "tool_use", "name": "Write", "input": {"file_path": str(claude / "plans" / "brave-plan.md")}}],
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
    (claude / "plans").mkdir(parents=True)
    (claude / "plans" / "brave-plan.md").write_text("# Plan\n1. Build the API\n2. Ship it", encoding="utf-8")

    # a CLI session inside a git worktree, open right now and registered as a background job
    wt = proj_dir / ".claude" / "worktrees" / "feat-x"
    wt.mkdir(parents=True)
    cli = {"sessionId": SID_CLI, "cwd": str(wt), "entrypoint": "cli"}
    with open(tdir / f"{SID_CLI}.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({**cli, "type": "user", "turnOrigin": "human", "timestamp": iso(now - 60_000),
                            "message": {"role": "user", "content": "Fix the flaky test"}}) + "\n")
    (claude / "sessions").mkdir(parents=True)
    (claude / "sessions" / "123.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": SID_CLI, "cwd": str(wt), "status": "busy", "entrypoint": "cli",
        "updatedAt": now}), encoding="utf-8")
    (claude / "sessions" / "999.json").write_text(json.dumps({     # stale: that process is gone
        "pid": 2_000_000_000, "sessionId": SID, "status": "idle", "updatedAt": now - 86_400_000}), encoding="utf-8")
    (claude / "jobs" / "j1").mkdir(parents=True)
    (claude / "jobs" / "j1" / "state.json").write_text(
        json.dumps({"state": "working", "name": "fix-flaky", "sessionId": SID_CLI}), encoding="utf-8")

    # a session deleted in the desktop app: the transcript stays on disk, a marker is left behind
    with open(tdir / f"{SID_DEL}.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"sessionId": SID_DEL, "cwd": str(proj_dir), "entrypoint": "claude-desktop", "type": "user",
                            "turnOrigin": "human", "timestamp": iso(now - 500_000),
                            "message": {"role": "user", "content": "Old experiment"}}) + "\n")
    (sess_dir / f"deleted_{SID_DEL}").write_text(str(now - 100_000), encoding="utf-8")
    (sess_dir / "scheduled-tasks.json").write_text(
        json.dumps({"scheduledTasks": [{"name": "Nightly tests"}]}), encoding="utf-8")
    return app, claude, cfg, proj_dir


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="claude-accounts-dash-test-"))
        app, claude, cfg, cls.proj = make_fixture(cls.tmp)
        cls._env = {k: os.environ.get(k) for k in ("CLAUDE_ACCOUNTS_DASH_APP_DIR", "CLAUDE_CONFIG_DIR",
                                                   "CLAUDE_ACCOUNTS_DASH_CONFIG_DIR", "CLAUDE_ACCOUNTS_DASH_CACHE_DIR")}
        os.environ.update(CLAUDE_ACCOUNTS_DASH_APP_DIR=str(app), CLAUDE_CONFIG_DIR=str(claude),
                          CLAUDE_ACCOUNTS_DASH_CONFIG_DIR=str(cfg), CLAUDE_ACCOUNTS_DASH_CACHE_DIR=str(cls.tmp / "cache"))
        import claude_accounts_dash.server as server
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

    def test_desktop_and_cli_sources(self):
        d = self.S.build_data()
        rows = {r["id"]: r for r in d["sessions"]}
        cli, gone, main = rows[SID_CLI], rows[SID_DEL], rows[SID]
        self.assertEqual(cli["entry"], "cli")
        self.assertEqual(cli["live"]["status"], "busy")          # its process is alive
        self.assertIsNone(main["live"])                          # stale session file is ignored
        self.assertEqual(cli["job"], {"name": "fix-flaky", "state": "working"})
        self.assertEqual(cli["worktree"], "feat-x")
        self.assertEqual(Path(cli["project"]), self.proj)        # grouped under the main project
        self.assertTrue(gone["deleted"] and gone["hidden"])
        self.assertEqual(gone["org"], ORG_A)                     # attributed from the marker's folder
        self.assertTrue(main["plan"])
        self.assertEqual(d["counts"]["open"], 1)
        a = next(x for x in d["accounts"] if x["org"] == ORG_A)
        self.assertEqual(a["scheduled"], ["Nightly tests"])
        self.assertEqual(a["sessions"], 1)                       # the deleted session is not counted
        proj = next(x for x in d["projects"] if Path(x["cwd"]) == self.proj)
        self.assertEqual(proj["worktrees"], ["feat-x"])

    def test_plan_in_brief_and_export(self):
        self.S.build_data()
        self.assertIn("Build the API", self.S.handoff_brief(SID))
        self.assertIn("## Plan", self.S.export_chat(SID, 0, True, True)["text"])

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

    @unittest.skipUnless(sys.platform == "win32", "Windows-only folder layout")
    def test_finds_packaged_windows_app_data(self):
        # MSIX/Store installs keep the data under %LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude
        root = self.tmp / "win"
        packaged = root / "Local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
        packaged.mkdir(parents=True)
        (packaged / "plan-usage-history.json").write_text("{}", encoding="utf-8")
        (root / "Roaming").mkdir()
        old = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA")}
        os.environ.update(APPDATA=str(root / "Roaming"), LOCALAPPDATA=str(root / "Local"))
        try:
            self.assertEqual(self.S._app_data_dir(), packaged)
            (root / "Roaming" / "Claude").mkdir()      # an empty unpackaged folder must not win
            self.assertEqual(self.S._app_data_dir(), packaged)
        finally:
            for k, v in old.items():
                os.environ[k] = v

    def test_client_disconnect_is_silent(self):
        # The browser may drop the connection mid-reply (reload, closed tab, a newer refresh).
        import contextlib
        import io

        class Gone:
            def write(self, b):
                raise ConnectionAbortedError(10053, "connection aborted by the client")

        h = self.S.Handler.__new__(self.S.Handler)
        h.send_response = h.send_header = lambda *a: None
        h.end_headers = lambda: None
        h.wfile = Gone()
        h._send(200, {"ok": True})                       # must not raise
        self.assertTrue(h.close_connection)

        srv = self.S.Server(("127.0.0.1", 0), self.S.Handler)
        try:
            for exc, noisy in ((ConnectionResetError(), False), (ValueError("real bug"), True)):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    try:
                        raise exc
                    except Exception:
                        srv.handle_error(None, ("127.0.0.1", 1))
                self.assertEqual("Traceback" in err.getvalue(), noisy)   # real errors are still reported
        finally:
            srv.server_close()

    def test_parse_reset(self):
        from datetime import datetime, timezone
        ms = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)
        P = self.S.parse_reset
        # later the same day, and past midnight
        self.assertEqual(P("You've hit your session limit · resets 8:20pm (UTC)", ms(2026, 9, 28, 17, 0)), ms(2026, 9, 28, 20, 20))
        self.assertEqual(P("You've hit your session limit · resets 1am (UTC)", ms(2026, 9, 28, 22, 0)), ms(2026, 9, 29, 1, 0))
        self.assertEqual(P("resets 12:40am (UTC)", ms(2026, 9, 28, 21, 0)), ms(2026, 9, 29, 0, 40))
        self.assertEqual(P("resets 12pm (UTC)", ms(2026, 9, 28, 9, 0)), ms(2026, 9, 28, 12, 0))
        # weekly with a date, including a year change
        self.assertEqual(P("You've hit your weekly limit · resets Aug 3, 1am (UTC)", ms(2026, 7, 30, 16, 0)), ms(2026, 8, 3, 1, 0))
        self.assertEqual(P("resets Jan 2, 9am (UTC)", ms(2026, 12, 30, 10, 0)), ms(2027, 1, 2, 9, 0))
        self.assertIsNone(P("You've hit your limit", ms(2026, 9, 28, 17, 0)))

    def test_exact_reset_overrides_estimate(self):
        now = int(time.time() * 1000)
        est = {"fh": 60, "sd": 20, "fh_now": 60, "sd_now": 20, "fh_reset": now + 9_000_000, "sd_reset": None,
               "last_seen": now - 600_000, "history": []}
        hit = (now - 60_000, "You've hit your session limit", "session", now + 3_600_000)
        out = self.S.apply_exact_resets(est, [hit], now)
        self.assertEqual((out["fh_now"], out["fh_reset"], out.get("fh_exact")), (100, now + 3_600_000, True))
        self.assertEqual(out["sd_now"], 20)
        # the reset already passed and no newer sample: usage is back to 0
        old = (now - 7_200_000, "", "session", now - 60_000)
        self.assertEqual(self.S.apply_exact_resets({**est, "fh_now": 100, "last_seen": now - 3_600_000}, [old], now)["fh_now"], 0)
        # an account with no usage samples still gets a card from its limit message
        bare = self.S.apply_exact_resets(None, [(now - 60_000, "", "weekly", now + 86_400_000)], now)
        self.assertEqual((bare["sd_now"], bare["fh_now"], bare["no_samples"]), (100, None, True))
        self.assertIsNone(self.S.apply_exact_resets(None, [old], now))

    def test_host_header_is_checked(self):
        ok = self.S.host_allowed
        for h in ("127.0.0.1:8765", "localhost:8765", "localhost", "[::1]:8765", "LOCALHOST."):
            self.assertTrue(ok(h), h)
        for h in ("evil.example:8765", "127.0.0.1.evil.example", "", None, "localhost.evil.com:8765"):
            self.assertFalse(ok(h), h)
        srv = self.S.Server(("127.0.0.1", 0), self.S.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/data", headers={"Host": f"rebind.example:{port}"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_index_cache_round_trip(self):
        before = self.S.build_data()
        self.S.INDEX.save_cache(force=True)
        self.assertTrue(self.S.CACHE_FILE.exists())
        fresh = self.S.TranscriptIndex()
        self.assertEqual(fresh.load_cache(), len(self.S.INDEX.files))
        fresh.refresh()
        self.assertEqual(fresh.parsed, 0)                       # nothing re-read
        old, self.S.INDEX = self.S.INDEX, fresh
        try:
            after = self.S.build_data()
        finally:
            self.S.INDEX = old
        strip = lambda d: [{k: v for k, v in r.items() if k != "live"} for r in d["sessions"]]
        self.assertEqual(json.loads(json.dumps(strip(after))), json.loads(json.dumps(strip(before))))
        self.assertEqual(after["activity"], before["activity"])
        self.assertIn("Build the API", self.S.handoff_brief(SID))

    def test_cli_paths(self):
        self.assertEqual(self.S.main(["--paths"]), 0)


if __name__ == "__main__":
    unittest.main()
