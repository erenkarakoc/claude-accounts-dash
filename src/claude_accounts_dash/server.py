"""Claude Accounts Dashboard - local, read-only view of Claude desktop app data.

Run:  claude-accounts-dash                  (opens http://127.0.0.1:8765)
      claude-accounts-dash --port 9000 --no-browser

Reads only local files:
  <Claude app data>/plan-usage-history.json     -> 5h / weekly limit % per account (org)
  <Claude app data>/claude-code-sessions/**     -> desktop Code sessions per account
  ~/.claude/projects/**/*.jsonl                 -> transcripts (tokens, activity, handoff briefs)
Nothing is sent anywhere; the server listens on 127.0.0.1 only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter, defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__

HOME = Path.home()


def _windows_app_data_dirs():
    """Candidate Claude data folders on Windows.

    The packaged (MSIX / Microsoft Store) app writes %APPDATA% into a private
    per-package folder. Programs started from inside the app see it as
    %APPDATA%\\Claude, but a normal terminal only finds it under
    %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming\\Claude.
    """
    roaming = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    dirs = [roaming / "Claude"]
    try:
        dirs += sorted(p / "LocalCache" / "Roaming" / "Claude" for p in (local / "Packages").glob("Claude_*"))
    except OSError:
        pass
    return dirs


def _last_written(folder):
    """How recently the desktop app wrote usage data here (0 if it never did)."""
    times = []
    for name in ("plan-usage-history.json", "claude-code-sessions"):
        try:
            times.append((folder / name).stat().st_mtime)
        except OSError:
            pass
    return max(times, default=0)


def _app_data_dir():
    """Per-OS folder where the Claude desktop app keeps its data."""
    if sys.platform == "win32":
        dirs = _windows_app_data_dirs()
        used = [d for d in dirs if _last_written(d)]
        return max(used, key=_last_written) if used else dirs[0]
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support" / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "Claude"


def _config_dir():
    """Where this tool keeps its own settings (account nicknames)."""
    if os.environ.get("CLAUDE_ACCOUNTS_DASH_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_ACCOUNTS_DASH_CONFIG_DIR"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming")) / "claude-accounts-dash"
    return Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "claude-accounts-dash"


DESKTOP_DIR = Path(os.environ.get("CLAUDE_ACCOUNTS_DASH_APP_DIR") or _app_data_dir())
USAGE_FILE = DESKTOP_DIR / "plan-usage-history.json"
DESKTOP_SESSIONS = DESKTOP_DIR / "claude-code-sessions"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
PROJECTS_DIR = CLAUDE_HOME / "projects"
CLI_CONFIG = HOME / ".claude.json"
NICK_FILE = _config_dir() / "nicknames.json"

FIVE_H = 5 * 3600 * 1000
SEVEN_D = 7 * 24 * 3600 * 1000
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def iso_ms(ts):
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def local_dt(ms):
    return datetime.fromtimestamp(ms / 1000)


# ---------------------------------------------------------------- limits

def window_estimate(samples, key, span):
    """Estimate when the current rolling window started, from sampled percentages.

    A window is assumed to start at the first sample where usage rises from 0,
    drops (a reset happened), or after a gap longer than the window itself.
    """
    start, prev = None, None
    for s in samples:
        v = s["u"].get(key)
        if v is None:
            continue
        if v > 0 and (prev is None or prev[1] == 0 or v < prev[1] or s["t"] - prev[0] > span):
            start = s["t"]
        elif v == 0:
            start = None
        prev = (s["t"], v)
    return start + span if start else None


def build_limits(now_ms):
    data = read_json(USAGE_FILE, {}) or {}
    by_org = defaultdict(list)
    for s in data.get("samples", []):
        if s.get("org") and isinstance(s.get("u"), dict):
            by_org[s["org"]].append(s)
    out = {}
    for org, samples in by_org.items():
        samples.sort(key=lambda s: s["t"])
        last = samples[-1]
        fh, sd = last["u"].get("fh"), last["u"].get("sd")
        fh_reset = window_estimate(samples, "fh", FIVE_H)
        sd_reset = window_estimate(samples, "sd", SEVEN_D)
        # If the estimated reset already passed, the stored % is stale -> probably 0 now.
        fh_now = 0 if (fh_reset and fh_reset < now_ms) else fh
        sd_now = 0 if (sd_reset and sd_reset < now_ms) else sd
        cutoff = now_ms - SEVEN_D
        hist = [[s["t"], s["u"].get("fh"), s["u"].get("sd")] for s in samples if s["t"] >= cutoff]
        out[org] = {
            "fh": fh, "sd": sd, "xu": last["u"].get("xu"),
            "fh_now": fh_now, "sd_now": sd_now,
            "fh_reset": fh_reset if (fh_reset and fh_reset > now_ms) else None,
            "sd_reset": sd_reset if (sd_reset and sd_reset > now_ms) else None,
            "last_seen": last["t"], "samples": len(samples), "history": hist,
        }
    return out


# ---------------------------------------------------------------- desktop sessions

def build_desktop_sessions():
    """Map cliSessionId -> desktop app metadata (which account/org it lives under)."""
    out = {}
    if not DESKTOP_SESSIONS.is_dir():
        return out
    archived = set()
    for f in DESKTOP_SESSIONS.glob("**/archived-sessions.idx"):
        archived.update((read_json(f, {}) or {}).get("archived") or [])
    for f in DESKTOP_SESSIONS.glob("*/*/local_*.json"):
        d = read_json(f)
        if not d or not d.get("cliSessionId"):
            continue
        out[d["cliSessionId"]] = {
            "account": f.parent.parent.name, "org": f.parent.name,
            "title": d.get("title"), "cwd": d.get("cwd"),
            "created": d.get("createdAt"), "last_activity": d.get("lastActivityAt"),
            "model": d.get("model"), "archived": bool(d.get("isArchived")) or d.get("sessionId") in archived,
            "turns": d.get("completedTurns"),
        }
    return out


def build_desktop_deleted():
    """Sessions deleted in the desktop app: cliSessionId -> {org, account, at}.

    The app leaves a `deleted_<cliSessionId>` marker (containing the deletion time)
    while the transcript itself may stay in ~/.claude/projects.
    """
    out = {}
    if DESKTOP_SESSIONS.is_dir():
        for f in DESKTOP_SESSIONS.glob("*/*/deleted_*"):
            sid = f.name[len("deleted_"):]
            try:
                at = int(f.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                at = 0
            out[sid] = {"org": f.parent.name, "account": f.parent.parent.name, "at": at}
    return out


def build_scheduled_tasks():
    """Scheduled tasks per org from the desktop app (Code and Cowork)."""
    out = defaultdict(list)
    for base in (DESKTOP_SESSIONS, DESKTOP_DIR / "local-agent-mode-sessions"):
        if not base.is_dir():
            continue
        for f in base.glob("*/*/scheduled-tasks.json"):
            for t in (read_json(f, {}) or {}).get("scheduledTasks") or []:
                if isinstance(t, dict):
                    name = t.get("name") or t.get("title") or (t.get("prompt") or "")[:60] or "Scheduled task"
                    out[f.parent.name].append(str(name))
    return out


def pid_alive(pid):
    """True if a process with this pid is running on this machine."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def build_live_sessions(now_ms):
    """Sessions that are open right now (CLI and desktop), from ~/.claude/sessions/*.json."""
    out = {}
    folder = CLAUDE_HOME / "sessions"
    if not folder.is_dir():
        return out
    for f in folder.glob("*.json"):
        d = read_json(f)
        if not isinstance(d, dict) or not d.get("sessionId"):
            continue
        updated = d.get("updatedAt") or d.get("statusUpdatedAt") or d.get("startedAt") or 0
        alive = pid_alive(d.get("pid")) if d.get("pid") else now_ms - updated < 10 * 60_000
        if not alive:
            continue
        out[d["sessionId"]] = {"status": d.get("status") or "open", "entry": d.get("entrypoint"),
                               "name": d.get("name"), "updated": updated}
    return out


def build_jobs():
    """Background jobs started from Claude Code: sessionId -> {name, state}."""
    out = {}
    folder = CLAUDE_HOME / "jobs"
    if not folder.is_dir():
        return out
    for f in folder.glob("*/state.json"):
        d = read_json(f)
        if not isinstance(d, dict):
            continue
        info = {"name": d.get("name") or f.parent.name, "state": d.get("state") or "unknown"}
        for key in ("sessionId", "resumeSessionId"):
            if d.get(key):
                out[d[key]] = info
    return out


WORKTREE_RE = re.compile(r"^(.*?)[/\\]\.claude(?:[/\\]|-)worktrees[/\\]([^/\\]+)")


def build_worktrees():
    """Worktree folder -> base repo, from the desktop app's git-worktrees.json."""
    out = {}
    d = read_json(DESKTOP_DIR / "git-worktrees.json", {}) or {}
    for name, w in (d.get("worktrees") or {}).items():
        if isinstance(w, dict) and w.get("path") and w.get("baseRepo"):
            out[os.path.normcase(os.path.normpath(w["path"]))] = (w["baseRepo"], w.get("name") or name)
    return out


def project_of(cwd, worktrees):
    """(project folder, worktree name or None) for a session's working directory."""
    if not cwd:
        return None, None
    hit = worktrees.get(os.path.normcase(os.path.normpath(cwd)))
    if hit:
        return hit
    m = WORKTREE_RE.match(cwd)
    if m:
        return m.group(1), m.group(2)
    return cwd, None


# ---------------------------------------------------------------- transcripts (incremental)

class FileState:
    __slots__ = ("offset", "sid", "title", "cwd", "branch", "first", "last", "prompts",
                 "asst", "tok", "models", "daily", "hours", "owner_org", "msg_ids",
                 "last_prompts", "last_text", "files", "cost", "is_sub", "entry", "limit_hit", "summary",
                 "plan_path", "plan_text")

    def __init__(self, is_sub):
        self.offset = 0
        self.sid = self.title = self.cwd = self.branch = self.owner_org = None
        self.first = self.last = None
        self.prompts = self.asst = 0
        self.tok = Counter()
        self.models = Counter()
        self.daily = defaultdict(Counter)   # date -> {msgs, tokens:<model>}
        self.hours = Counter()
        self.msg_ids = set()
        self.last_prompts = deque(maxlen=5)
        self.last_text = ""
        self.files = []
        self.cost = None
        self.is_sub = is_sub
        self.entry = None
        self.limit_hit = None       # (ms, text, kind)
        self.summary = None
        self.plan_path = None       # plan file Claude wrote (~/.claude/plans/*.md)
        self.plan_text = None       # plan submitted with ExitPlanMode


class TranscriptIndex:
    def __init__(self):
        self.files = {}
        self.lock = threading.Lock()
        self.scanning = False
        self.progress = [0, 0]

    def refresh(self):
        with self.lock:
            if not PROJECTS_DIR.is_dir():
                return
            paths = list(PROJECTS_DIR.rglob("*.jsonl"))
            self.scanning, self.progress = True, [0, len(paths)]
            seen = set()
            for p in paths:
                key = str(p)
                seen.add(key)
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                st = self.files.get(key)
                if st is None or size < st.offset:          # new or truncated -> reparse
                    st = self.files[key] = FileState("subagents" in p.parts)
                if size > st.offset:
                    self._parse(p, st)
                self.progress[0] += 1
            for k in list(self.files):
                if k not in seen:
                    del self.files[k]
            self.scanning = False

    def _parse(self, path, st):
        with open(path, "rb") as f:
            f.seek(st.offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        st.offset += end + 1
        for raw in chunk[: end + 1].splitlines():
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            self._line(d, st)

    @staticmethod
    def _line(d, st):
        t = d.get("type")
        if st.sid is None and d.get("sessionId"):
            st.sid = d["sessionId"]
        if d.get("cwd") and not st.cwd:
            st.cwd = d["cwd"]
        if d.get("gitBranch"):
            st.branch = d["gitBranch"]
        if d.get("entrypoint"):
            st.entry = d["entrypoint"]
        ms = iso_ms(d["timestamp"]) if d.get("timestamp") else None
        if ms:
            st.first = ms if st.first is None else min(st.first, ms)
            st.last = ms if st.last is None else max(st.last, ms)
        if t == "custom-title":
            st.title = d.get("customTitle") or st.title
        elif t == "agent-name" and not st.title:
            st.title = d.get("agentName")
        elif t == "summary" and not st.title:
            st.title = d.get("summary")
        elif t == "bridge-session":
            st.owner_org = d.get("ownerOrganizationUuid") or st.owner_org
        elif t == "file-history-delta":
            fp = d.get("trackingPath")
            if fp and fp not in st.files and not is_temp_path(fp):
                st.files.append(fp)
        elif t == "cost-state":
            st.cost = d.get("totalCostUSD")
        elif t == "user":
            msg = d.get("message") or {}
            c = msg.get("content")
            text = c if isinstance(c, str) else None
            if isinstance(c, list):
                parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(parts) if parts else None
            human = (d.get("origin") or {}).get("kind") == "human" or d.get("turnOrigin") == "human"
            if text and d.get("isCompactSummary"):
                st.summary = text
            elif text and human and not d.get("isSidechain") and not text.startswith("<"):
                st.prompts += 1
                st.last_prompts.append(text[:600])
                if ms:
                    st.daily[local_dt(ms).strftime("%Y-%m-%d")]["prompts"] += 1
                    st.hours[local_dt(ms).hour] += 1
        elif t == "assistant":
            msg = d.get("message") or {}
            if d.get("error") == "rate_limit":
                txt = "".join(b.get("text", "") for b in (msg.get("content") or []) if isinstance(b, dict))
                kind = "weekly" if "weekly" in txt else "session"
                if ms and (not st.limit_hit or ms >= st.limit_hit[0]):
                    st.limit_hit = (ms, txt.strip()[:160], kind)
                return
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    fp = str(inp.get("file_path") or "")
                    if is_plan_path(fp):
                        st.plan_path = fp
                    if b.get("name") == "ExitPlanMode" and isinstance(inp.get("plan"), str):
                        st.plan_text = inp["plan"][:20000]
            mid = msg.get("id")
            texts = [b.get("text", "") for b in (msg.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            if texts and not d.get("isSidechain"):
                st.last_text = texts[-1][:1500]
            if mid in st.msg_ids:               # streamed blocks repeat the same usage
                return
            if mid:
                st.msg_ids.add(mid)
            model = msg.get("model") or "unknown"
            if model == "<synthetic>":
                return
            u = msg.get("usage") or {}
            inp, out = u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0
            cr, cc = u.get("cache_read_input_tokens", 0) or 0, u.get("cache_creation_input_tokens", 0) or 0
            st.asst += 1
            st.tok.update({"input": inp, "output": out, "cache_read": cr, "cache_write": cc})
            st.models[model] += inp + out + cc
            if ms:
                day = st.daily[local_dt(ms).strftime("%Y-%m-%d")]
                day["msgs"] += 1
                day["tok:" + model] += inp + out + cc


INDEX = TranscriptIndex()


# ---------------------------------------------------------------- aggregate

def is_temp_path(fp):
    f = fp.replace(chr(92), "/").lower()
    return any(x in f for x in ("/scratchpad/", "/appdata/local/temp/", "/tmp/", "/var/folders/"))


def is_plan_path(fp):
    """Plan files live in ~/.claude/plans (or $CLAUDE_CONFIG_DIR/plans)."""
    f = fp.replace(chr(92), "/").lower()
    plans_dir = str(CLAUDE_HOME / "plans").replace(chr(92), "/").lower() + "/"
    return f.endswith(".md") and ("/.claude/plans/" in f or f.startswith(plans_dir))


def session_plan(st):
    """(path or None, text or None) of the latest plan in a session."""
    if st.plan_path:
        try:
            return st.plan_path, Path(st.plan_path).read_text(encoding="utf-8")
        except OSError:
            pass
    return st.plan_path, st.plan_text


def is_background(path):
    """Sessions produced by plugins/background workers (e.g. claude-mem observers)."""
    return bool(path) and ("observer-sessions" in path or "claude-mem" in path)


def short(x):
    return (x or "")[:8]


def build_data():
    now = int(time.time() * 1000)
    INDEX.refresh()
    limits = build_limits(now)
    desk = build_desktop_sessions()
    deleted = build_desktop_deleted()
    live = build_live_sessions(now)
    jobs = build_jobs()
    scheduled = build_scheduled_tasks()
    worktrees = build_worktrees()
    nicks = read_json(NICK_FILE, {}) or {}
    cli = (read_json(CLI_CONFIG, {}) or {}).get("oauthAccount") or {}

    # merge transcript files into sessions (subagent files fold into their parent)
    sessions = {}
    with INDEX.lock:
        states = list(INDEX.files.items())
    for path, st in states:
        sid = st.sid
        if st.is_sub:
            parts = Path(path).parts
            sid = parts[-3] if len(parts) >= 3 and UUID_RE.match(parts[-3]) else sid
        if not sid:
            continue
        s = sessions.setdefault(sid, {
            "id": sid, "title": None, "cwd": None, "branch": None, "first": None, "last": None,
            "prompts": 0, "msgs": 0, "tok": Counter(), "models": Counter(), "size": 0,
            "path": None, "owner_org": None, "files": [], "subagents": 0, "cost": None,
            "entry": None, "limit_hit": None, "plan": False,
        })
        try:
            s["size"] += os.path.getsize(path)
        except OSError:
            pass
        s["tok"].update(st.tok)
        s["models"].update(st.models)
        s["msgs"] += st.asst
        if st.is_sub:
            s["subagents"] += 1
        else:
            s["path"] = path
            s["title"] = st.title or s["title"]
            s["cwd"] = st.cwd or s["cwd"]
            s["branch"] = st.branch
            s["prompts"] += st.prompts
            s["owner_org"] = st.owner_org
            s["files"] = st.files[-15:]
            s["cost"] = st.cost
            s["entry"] = st.entry
            s["plan"] = bool(st.plan_path or st.plan_text)
        if st.limit_hit and (not s["limit_hit"] or st.limit_hit[0] > s["limit_hit"][0]):
            s["limit_hit"] = st.limit_hit
        for k in ("first", "last"):
            v = getattr(st, k)
            if v:
                cur = s[k]
                s[k] = v if cur is None else (min(cur, v) if k == "first" else max(cur, v))

    # per-day / per-hour activity & per-org tokens
    daily = defaultdict(Counter)
    hours = Counter()
    for path, st in states:
        for day, c in st.daily.items():
            daily[day].update(c)
        hours.update(st.hours)

    orgs = set(limits)
    rows = []
    def extras(sid, cwd, background):
        gone = sid in deleted and sid not in desk
        project, worktree = project_of(cwd, worktrees)
        return {"deleted": gone, "live": live.get(sid), "job": jobs.get(sid), "project": project,
                "worktree": worktree, "background": background, "hidden": background or gone}

    for sid, s in sessions.items():
        dm = desk.get(sid) or {}
        org = dm.get("org") or (deleted.get(sid) or {}).get("org") or s["owner_org"]
        if org:
            orgs.add(org)
        title = dm.get("title") or s["title"] or (live.get(sid) or {}).get("name") or "(untitled)"
        cwd = dm.get("cwd") or s["cwd"]
        rows.append({
            "id": sid, "title": title, "cwd": cwd, "branch": s["branch"],
            "org": org, "account": dm.get("account"), "desktop": bool(dm), "archived": dm.get("archived", False),
            "first": s["first"], "last": max(filter(None, [s["last"], dm.get("last_activity")]), default=None),
            "prompts": s["prompts"], "msgs": s["msgs"], "size": s["size"], "subagents": s["subagents"],
            "tokens": dict(s["tok"]), "model": dm.get("model") or (s["models"].most_common(1)[0][0] if s["models"] else None),
            "cost": s["cost"], "path": s["path"], "entry": s["entry"] or (live.get(sid) or {}).get("entry"),
            "limit_hit": s["limit_hit"], "plan": s["plan"],
            **extras(sid, cwd, is_background(s["path"])),
        })
    # desktop sessions that have no transcript yet
    for sid, dm in desk.items():
        if sid not in sessions:
            rows.append({"id": sid, "title": dm.get("title") or "(untitled)", "cwd": dm.get("cwd"), "branch": None,
                         "org": dm["org"], "account": dm["account"], "desktop": True, "archived": dm["archived"],
                         "first": dm.get("created"), "last": dm.get("last_activity"), "prompts": 0, "msgs": 0,
                         "size": 0, "subagents": 0, "tokens": {}, "model": dm.get("model"), "cost": None,
                         "path": None, "entry": "claude-desktop", "limit_hit": None, "plan": False,
                         **extras(sid, dm.get("cwd"), False)})
            orgs.add(dm["org"])
    rows.sort(key=lambda r: r["last"] or 0, reverse=True)

    # accounts
    accounts = []
    for org in orgs:
        mine = [r for r in rows if r["org"] == org and not r["deleted"]]
        tok = Counter()
        for r in mine:
            tok.update(r["tokens"])
        lim = limits.get(org)
        hits = [r["limit_hit"] for r in mine if r["limit_hit"] and not r["hidden"]]
        accounts.append({
            "org": org, "short": short(org), "nick": nicks.get(org) or "",
            "email": cli.get("emailAddress") if cli.get("organizationUuid") == org else None,
            "cli_active": cli.get("organizationUuid") == org,
            "limits": lim, "sessions": len(mine),
            "last_active": max((r["last"] or 0 for r in mine), default=0) or None,
            "tokens": dict(tok),
            "last_limit_hit": max(hits) if hits else None,
            "open": sum(1 for r in mine if r["live"]),
            "scheduled": scheduled.get(org, []),
        })
    # best account to use now: lowest current 5h, then lowest weekly
    def score(a):
        l = a["limits"]
        return (l["fh_now"] if l else 101, l["sd_now"] if l else 101)
    accounts.sort(key=lambda a: (a["nick"] or "~") + a["org"])
    candidates = [a for a in accounts if a["limits"]]
    best = min(candidates, key=score)["org"] if candidates else None

    # projects
    projects = defaultdict(lambda: {"sessions": 0, "size": 0, "last": 0, "orgs": set(), "tokens": 0, "prompts": 0,
                                    "worktrees": set(), "open": 0})
    for r in rows:
        if r["deleted"]:
            continue
        key = r["project"] or "(unknown)"
        p = projects[key]
        p["sessions"] += 1
        p["size"] += r["size"]
        p["last"] = max(p["last"], r["last"] or 0)
        p["prompts"] += r["prompts"]
        p["tokens"] += sum(v for k, v in r["tokens"].items() if k != "cache_read")
        if r["org"]:
            p["orgs"].add(r["org"])
        if r["worktree"]:
            p["worktrees"].add(r["worktree"])
        if r["live"]:
            p["open"] += 1
    proj_rows = [{"cwd": k, **{**v, "orgs": sorted(v["orgs"]), "worktrees": sorted(v["worktrees"])},
                  "exists": os.path.isdir(k) if k != "(unknown)" else False} for k, v in projects.items()]
    proj_rows.sort(key=lambda p: p["last"], reverse=True)

    # activity: last 30 days
    days = sorted(daily)[-30:]
    models = sorted({k[4:] for d in days for k in daily[d] if k.startswith("tok:")})
    activity = {
        "days": days,
        "msgs": [daily[d]["msgs"] for d in days],
        "prompts": [daily[d]["prompts"] for d in days],
        "tokens": {m: [daily[d]["tok:" + m] for d in days] for m in models},
        "hours": [hours.get(h, 0) for h in range(24)],
    }
    totals = Counter()
    for r in rows:
        totals.update(r["tokens"])

    return {
        "now": now, "best": best, "accounts": accounts, "sessions": rows[:400] + [r for r in rows[400:] if r["live"]],
        "projects": proj_rows, "activity": activity, "totals": dict(totals),
        "counts": {"sessions": len(rows), "projects": len(proj_rows),
                   "size": sum(r["size"] for r in rows),
                   "visible": sum(1 for r in rows if not r["hidden"]),
                   "hidden": sum(1 for r in rows if r["hidden"]),
                   "open": sum(1 for r in rows if r["live"])},
        "sources": {"usage": USAGE_FILE.exists(), "desktop": DESKTOP_SESSIONS.exists(),
                    "projects": PROJECTS_DIR.exists(), "live": (CLAUDE_HOME / "sessions").is_dir()},
    }


def handoff_brief(sid):
    with INDEX.lock:
        main = next((st for p, st in INDEX.files.items() if st.sid == sid and not st.is_sub), None)
        path = next((p for p, st in INDEX.files.items() if st.sid == sid and not st.is_sub), None)
    if not main:
        return None
    desk = build_desktop_sessions().get(sid) or {}
    title = desk.get("title") or main.title or "(untitled)"
    lines = [
        f"Continue the work from my earlier Claude session \"{title}\".",
        "",
        f"- Project folder: {desk.get('cwd') or main.cwd}",
    ]
    if main.branch:
        lines.append(f"- Git branch: {main.branch}")
    lines.append(f"- Full transcript (JSONL, only read the end if needed): {path}")
    if main.files:
        lines.append("- Files touched: " + ", ".join(main.files[-12:]))
    if main.last_prompts:
        lines += ["", "My last requests were:"]
        lines += [f"{i}. {p.strip()}" for i, p in enumerate(main.last_prompts, 1)]
    plan_path, plan_text = session_plan(main)
    if plan_path:
        lines.append(f"- Plan file: {plan_path}")
    if plan_text:
        lines += ["", "The plan we agreed on (trimmed):", _trim(plan_text.strip(), 3000)]
    if main.summary:
        body = main.summary.split("Summary:", 1)[-1].strip()
        lines += ["", "Context summary Claude wrote earlier in that session (trimmed):", _trim(body, 2500)]
    if main.last_text:
        lines += ["", "Your last reply was:", main.last_text.strip()]
    lines += ["", "First check the current state of the project (git status / the files above), "
              "tell me briefly where we left off, then continue."]
    return "\n".join(lines)


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _tool_arg(b):
    inp = b.get("input") or {}
    arg = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or inp.get("command") \
        or inp.get("pattern") or inp.get("url") or inp.get("description") or ""
    return str(arg).replace("\n", " ")


def _trim(text, limit):
    if len(text) <= limit:
        return text
    head, tail = text[: int(limit * 0.7)], text[-int(limit * 0.25):]
    return f"{head}\n\n[... {len(text) - len(head) - len(tail):,} characters trimmed ...]\n\n{tail}"


def _actions_summary(tools):
    edited, counts = [], Counter()
    for name, arg in tools:
        counts[name] += 1
        if name in EDIT_TOOLS and arg:
            p = "/".join(arg.replace("\\", "/").split("/")[-3:])
            if p not in edited:
                edited.append(p)
    bits = []
    if edited:
        more = f" +{len(edited) - 15} more" if len(edited) > 15 else ""
        bits.append("edited " + ", ".join(f"`{e}`" for e in edited[:15]) + more)
    other = [f"{n}x {k}" for k, n in counts.most_common() if k not in EDIT_TOOLS]
    if other:
        bits.append("used " + ", ".join(other[:6]))
    return "_Actions: " + "; ".join(bits) + "_" if bits else ""


def export_chat(sid, turns=0, tools=True, compact=True):
    """Markdown of a session for pasting into a new session on another account.

    turns=0 exports everything, otherwise only the last N human turns.
    compact: trims very long prompts, keeps Claude's final reply per turn and
    summarizes tool calls instead of listing every one.
    """
    with INDEX.lock:
        path, main = next(((p, st) for p, st in INDEX.files.items() if st.sid == sid and not st.is_sub), (None, None))
    if not path:
        return None
    desk = build_desktop_sessions().get(sid) or {}
    plan_path, plan_text = session_plan(main)
    turns_out, cur, meta, seen_text = [], None, {}, set()
    with open(path, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            if d.get("cwd"):
                meta.setdefault("cwd", d["cwd"])
            if d.get("gitBranch"):
                meta["branch"] = d["gitBranch"]
            if t == "custom-title":
                meta["title"] = d.get("customTitle")
            if d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            c = msg.get("content")
            if t == "user":
                human = (d.get("origin") or {}).get("kind") == "human" or d.get("turnOrigin") == "human"
                text = c if isinstance(c, str) else "\n".join(
                    b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                if text and d.get("isCompactSummary"):
                    cur = {"summary": text.split("Summary:", 1)[-1].strip(), "parts": [], "tools": []}
                    turns_out.append(cur)
                elif human and text and not text.lstrip().startswith("<"):
                    cur = {"user": text.strip(), "parts": [], "tools": []}
                    turns_out.append(cur)
            elif t == "assistant" and cur is not None:
                if d.get("error") == "rate_limit":
                    txt = "".join(b.get("text", "") for b in (c or []) if isinstance(b, dict))
                    cur["parts"].append(("limit", txt.strip()))
                    continue
                if msg.get("model") == "<synthetic>":
                    continue
                for b in c or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        key = (msg.get("id"), b["text"][:200])
                        if key not in seen_text:
                            seen_text.add(key)
                            cur["parts"].append(("text", b["text"].strip()))
                    elif b.get("type") == "tool_use":
                        item = (b.get("name", "tool"), _tool_arg(b))
                        cur["tools"].append(item)
                        cur["parts"].append(("tool", item))
    n = 0
    for e in turns_out:
        if "user" in e:
            n += 1
            e["n"] = n
    total = n
    if turns and 0 < turns < total:
        cut = next(i for i, e in enumerate(turns_out) if e.get("n") == total - turns + 1)
        earlier = [e for e in turns_out[:cut] if "summary" in e]
        turns_out = earlier[-1:] + turns_out[cut:]
    included = sum(1 for e in turns_out if "user" in e)
    title = desk.get("title") or meta.get("title") or "(untitled)"
    out = [
        f"# Handoff: {title}",
        "",
        "I'm continuing this conversation from another Claude account because the previous one hit its usage limit. "
        "Below is the conversation so far. Read it, check the current state of the project, "
        "briefly summarize where we left off, then continue.",
        "",
        f"- Project folder: `{desk.get('cwd') or meta.get('cwd')}`",
    ]
    if meta.get("branch"):
        out.append(f"- Git branch: `{meta['branch']}`")
    if plan_path:
        out.append(f"- Plan file: `{plan_path}`")
    out.append(f"- Turns included: {included} of {total}" + (" (compact)" if compact else ""))
    out += ["", "---", ""]
    if plan_text:
        out += ["## Plan", "", _trim(plan_text.strip(), 6000) if compact else plan_text.strip(), "", "---", ""]
    for tr in turns_out:
        if "summary" in tr:
            out += ["## Earlier context (summary Claude wrote when the chat was compacted)", "",
                    _trim(tr["summary"], 8000) if compact else tr["summary"], ""]
            if not tr["parts"]:
                continue
            out += ["## Claude", ""]
        else:
            out += [f"## Turn {tr['n']} - Me", "", _trim(tr["user"], 3000) if compact else tr["user"], "", "## Claude", ""]
        if compact:
            texts = [v for k, v in tr["parts"] if k == "text"]
            limits = [v for k, v in tr["parts"] if k == "limit"]
            if texts:
                if len(texts) > 1:
                    out += [f"_({len(texts) - 1} earlier progress messages omitted)_", ""]
                out += [_trim(texts[-1], 6000), ""]
            if tools and tr["tools"]:
                out += [_actions_summary(tr["tools"]), ""]
            if limits:
                out += [f"> **{limits[-1]}**", ""]
            continue
        buf = []
        for kind, val in tr["parts"]:
            if kind == "tool":
                if tools:
                    buf.append(f"- `{val[0]}` {val[1][:140]}".rstrip())
                continue
            if buf:
                out += ["_Actions:_", *buf, ""]
                buf = []
            out += [f"> **{val}**" if kind == "limit" else val, ""]
        if buf:
            out += ["_Actions:_", *buf, ""]
    return {"text": "\n".join(out).rstrip() + "\n", "turns": included, "total": total, "title": title}


def open_folder(path):
    """Open a folder in the OS file manager (Explorer, Finder, or xdg-open on Linux)."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - path is validated by the caller
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- http

# The browser closed the connection before the reply was sent (page reload, tab closed,
# a newer refresh replacing an older one). Nothing to do: the next request will work.
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, default=list).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except CLIENT_GONE:
            self.close_connection = True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            page = resources.files("claude_accounts_dash").joinpath("static/index.html").read_bytes()
            return self._send(200, page, "text/html")
        if u.path == "/api/data":
            try:
                data = build_data()
            except Exception as e:  # a real error while reading the local files
                return self._send(500, {"error": str(e)})
            return self._send(200, data)
        if u.path == "/api/brief":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            text = handoff_brief(sid)
            return self._send(200 if text else 404, {"text": text})
        if u.path == "/api/export":
            q = parse_qs(u.query)
            sid = (q.get("id") or [""])[0]
            try:
                turns = int((q.get("turns") or ["0"])[0])
            except ValueError:
                turns = 0
            res = export_chat(sid, turns, (q.get("tools") or ["1"])[0] == "1", (q.get("compact") or ["1"])[0] == "1")
            return self._send(200 if res else 404, res or {"error": "no transcript"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # Only accept same-origin requests (blocks other websites from poking the local server)
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        u = urlparse(self.path)
        if u.path == "/api/nick":
            nicks = read_json(NICK_FILE, {}) or {}
            org, name = str(body.get("org", "")), str(body.get("nick", "")).strip()[:40]
            if not UUID_RE.match(org):
                return self._send(400, {"error": "bad org"})
            if name:
                nicks[org] = name
            else:
                nicks.pop(org, None)
            NICK_FILE.parent.mkdir(parents=True, exist_ok=True)
            NICK_FILE.write_text(json.dumps(nicks, indent=2, ensure_ascii=False), encoding="utf-8")
            return self._send(200, {"ok": True})
        if u.path == "/api/open":
            # open a project folder in Explorer - only folders that belong to known sessions
            target = str(body.get("path", ""))
            known = {r["cwd"] for r in build_data()["sessions"] if r["cwd"]}
            if target in known and os.path.isdir(target) and open_folder(target):
                return self._send(200, {"ok": True})
            return self._send(400, {"error": "unknown folder"})
        self._send(404, {"error": "not found"})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-accounts-dash", description="Local dashboard for your Claude accounts, limits and sessions.")
    ap.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--paths", action="store_true", help="print the local folders it reads and exit")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = ap.parse_args(argv)
    if a.paths:
        for label, p in [("Desktop app data", DESKTOP_DIR), ("Usage history", USAGE_FILE),
                         ("Transcripts", PROJECTS_DIR), ("Nicknames", NICK_FILE)]:
            print(f"{label:17} {p}  {'(found)' if p.exists() else '(missing)'}")
        return 0
    t0 = time.time()
    print("Indexing local transcripts...", flush=True)
    INDEX.refresh()
    print(f"  {len(INDEX.files)} files indexed in {time.time() - t0:.1f}s", flush=True)
    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        print(f"Port {a.port} is in use. Is the dashboard already running? Try --port {a.port + 1}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{a.port}"
    print(f"Claude dashboard running at {url}  (Ctrl+C to stop)", flush=True)
    if not a.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
