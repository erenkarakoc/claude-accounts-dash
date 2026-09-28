"""Generate a realistic, entirely fake Claude data folder for demos and screenshots.

Usage:
    python scripts/demo_data.py DEST
    CLAUDE_ACCOUNTS_DASH_APP_DIR=DEST/app CLAUDE_CONFIG_DIR=DEST/claude \
    CLAUDE_ACCOUNTS_DASH_CONFIG_DIR=DEST/config claude-accounts-dash

Nothing here comes from a real account.
"""
import json
import random
import sys
import time
import uuid
from pathlib import Path

rnd = random.Random(7)
NOW = int(time.time() * 1000)
MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000

ACCOUNT_ID = "5f0c2a8e-1b7d-4c3e-9a61-2d84f0b7c913"
ACCOUNTS = [  # org id, nickname, current 5h %, current weekly %
    ("7a1e4c90-3f2b-4d8a-b6c5-91e0d2f4a837", "Personal", 100, 64),
    ("c3d9b812-6e4f-4a17-8c2d-5b7f0e9a1d46", "Work", 38, 47),
    ("e81f5a26-9c3d-4b70-a4e8-0f2c6d9b3a75", "Side projects", 6, 14),
]
MODELS = ["claude-opus-5-5"] * 6 + ["claude-sonnet-5"] * 3 + ["claude-haiku-4-5"]
ROOT = "/Users/alex/code"

PROJECTS = {
    "acme-web": ["src/app/checkout/page.tsx", "src/components/CartSummary.tsx", "src/lib/stripe.ts", "src/app/settings/theme.ts", "tests/checkout.spec.ts"],
    "billing-api": ["app/webhooks/stripe.py", "app/invoices/render.py", "app/models/subscription.py", "tests/test_webhooks.py", "migrations/0042_retry_queue.sql"],
    "mobile-app": ["android/app/src/main/PushService.kt", "src/screens/Inbox.tsx", "src/hooks/useNotifications.ts", "ios/App/AppDelegate.swift"],
    "docs-site": ["docs/getting-started.md", "docs/api/webhooks.md", "astro.config.mjs", "src/components/Search.astro"],
    "infra": ["terraform/staging/main.tf", "terraform/modules/db/variables.tf", ".github/workflows/deploy.yml", "k8s/api/deployment.yaml"],
}

PLAN = """Webhook retries:

1. Store failed deliveries in a `webhook_retry` table (event id, attempt, next_run_at).
2. Worker picks due rows every 30s; exponential backoff 1m, 5m, 30m, 2h, 12h.
3. Give up after 5 attempts and alert in #billing.
4. Tests: backoff schedule, idempotency on duplicate events, give-up path."""

# Extra details that show the dashboard's tags: source, open now, jobs, worktrees, plans, archived.
EXTRAS = {
    "Postgres full-text search for docs": {"entry": "cli", "live": "busy"},
    "Dark mode for the settings page": {"live": "idle", "worktree": "dark-mode"},
    "Retry failed Stripe webhooks with backoff": {"plan": PLAN},
    "Migrate auth to OAuth 2.1 + PKCE": {"plan": "## Plan\n1. Add PKCE to the login flow\n2. Rotate refresh tokens\n3. Remove the implicit grant"},
    "Terraform: add a staging environment": {"entry": "cli"},
    "CI: cache dependencies and split test jobs": {"entry": "sdk-cli", "job": ("ci-cache-audit", "working")},
    "Landing page A/B test": {"archived": True},
}
DELETED = [("Scratch: try the Bun runtime", "acme-web", 2, 2.2, 3)]
SCHEDULED = {1: ["Nightly dependency audit", "Weekly release notes draft"]}   # account index -> task names

SESSIONS = [  # title, project, account index, days ago, prompts, hit limit
    ("Retry failed Stripe webhooks with backoff", "billing-api", 0, 0.08, 14, "session"),
    ("Checkout page: fix flaky payment test", "acme-web", 0, 0.3, 9, "session"),
    ("Invoice PDF renderer refactor", "billing-api", 0, 1.2, 22, "session"),
    ("Dark mode for the settings page", "acme-web", 1, 0.05, 11, None),
    ("Push notifications on Android 15", "mobile-app", 1, 0.6, 17, None),
    ("Terraform: add a staging environment", "infra", 1, 1.6, 12, "session"),
    ("Postgres full-text search for docs", "docs-site", 2, 0.02, 6, None),
    ("Migrate auth to OAuth 2.1 + PKCE", "acme-web", 0, 2.4, 31, "weekly"),
    ("Onboarding guide rewrite", "docs-site", 2, 3.1, 8, None),
    ("Subscription proration edge cases", "billing-api", 1, 3.8, 19, None),
    ("Inbox screen performance", "mobile-app", 1, 4.5, 10, None),
    ("CI: cache dependencies and split test jobs", "infra", 2, 5.2, 7, None),
    ("Cart summary redesign", "acme-web", 0, 6.1, 13, None),
    ("Webhook docs with runnable examples", "docs-site", 2, 7.4, 5, None),
    ("Database connection pool tuning", "infra", 1, 8.3, 9, None),
    ("iOS deep links for password reset", "mobile-app", 0, 9.6, 15, None),
    ("Usage-based pricing prototype", "billing-api", 2, 11.2, 20, None),
    ("Accessibility pass on forms", "acme-web", 1, 12.5, 12, None),
    ("Release notes generator", "docs-site", 2, 14.1, 6, None),
    ("Kubernetes autoscaling for the API", "infra", 1, 16.3, 11, None),
    ("Offline mode for the mobile app", "mobile-app", 0, 18.7, 24, None),
    ("Tax calculation service", "billing-api", 1, 21.0, 16, None),
    ("Landing page A/B test", "acme-web", 2, 23.4, 8, None),
    ("Search results ranking", "docs-site", 0, 25.8, 10, None),
    ("Blue/green deploys", "infra", 1, 27.9, 13, None),
]

PROMPTS = [
    "Can you look at why this fails intermittently and fix it?",
    "Add tests for the edge cases we discussed.",
    "Now wire it into the settings page and keep the existing API.",
    "Looks good. Can you also handle the error state?",
    "Refactor this so it's easier to read, no behavior changes.",
    "Update the docs to match.",
    "Run the test suite and fix anything that breaks.",
    "What's left before we can ship this?",
    "continue",
    "Make it work on mobile too.",
]
REPLIES = [
    "Found it: the retry job reads the queue before the transaction commits. I moved the enqueue after commit and added a test that reproduces the race.",
    "Done. The new tests cover empty carts, expired cards and partial refunds. All 148 tests pass.",
    "I split the renderer into layout, totals and tax sections. The output is byte-identical to before on all fixtures.",
    "The settings page now follows the system theme, with an override saved per user.",
    "Updated the guide and added a runnable example for each webhook event.",
    "Everything passes. Remaining: the migration needs a backfill for old rows; I've written it but not run it.",
]


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))


def usage_history():
    samples = []
    for org, _, fh_now, sd_now in ACCOUNTS:
        t = NOW - 7 * DAY
        fh = 0
        while t <= NOW:
            hour = time.localtime(t / 1000).tm_hour
            active = 8 <= hour <= 23
            if active:
                fh = min(100, fh + rnd.randint(0, 9))
            if rnd.random() < 0.06 or not active:
                fh = 0
            frac = (t - (NOW - 7 * DAY)) / (7 * DAY)
            samples.append({"t": t, "org": org, "u": {"fh": fh, "sd": round(sd_now * frac)}})
            t += 15 * MIN
        # make the last few hours end at the current values
        tail = [s for s in samples if s["org"] == org][-16:]
        for i, s in enumerate(tail):
            s["u"]["fh"] = round(fh_now * (i + 1) / len(tail))
            s["u"]["sd"] = sd_now
    samples.sort(key=lambda s: s["t"])
    return {"version": 2, "samples": samples}


def transcript(sid, title, cwd, proj, org, start, prompts, hit, entry="claude-desktop", plan=None):
    files = PROJECTS[proj]
    base = {"sessionId": sid, "cwd": cwd, "gitBranch": "main", "entrypoint": entry, "version": "2.1.281"}
    out = [{**base, "type": "custom-title", "customTitle": title},
           {**base, "type": "bridge-session", "ownerAccountUuid": ACCOUNT_ID, "ownerOrganizationUuid": org}]
    t = start
    model = rnd.choice(MODELS)
    for i in range(prompts):
        if i == prompts // 2 and prompts > 12:
            out.append({**base, "type": "user", "isCompactSummary": True, "turnOrigin": "human", "timestamp": iso(t),
                        "message": {"role": "user", "content": "This session is being continued from a previous conversation.\n\nSummary:\n"
                                    f"1. Goal: {title.lower()}.\n2. Done so far: the core change is implemented and covered by tests.\n"
                                    "3. Open items: error states, docs, and one flaky test on CI."}})
        prompt = (f"Let's work on this: {title[0].lower() + title[1:]}. Read the relevant code first, "
                  "propose a plan, then implement it with tests.") if i == 0 else rnd.choice(PROMPTS)
        out.append({**base, "type": "user", "turnOrigin": "human", "origin": {"kind": "human"}, "timestamp": iso(t),
                    "message": {"role": "user", "content": prompt}})
        for step in range(rnd.randint(2, 6)):
            t += rnd.randint(20, 90) * 1000
            f = rnd.choice(files)
            tool = rnd.choice([("Edit", {"file_path": f"{cwd}/{f}"}), ("Bash", {"command": "npm test -- --run"}),
                               ("Read", {"file_path": f"{cwd}/{f}"}), ("Grep", {"pattern": "retry|backoff"})])
            out.append({**base, "type": "assistant", "timestamp": iso(t), "message": {
                "id": f"msg_{uuid.uuid4().hex[:20]}", "model": model,
                "content": [{"type": "text", "text": "Checking the relevant code first."}, {"type": "tool_use", "name": tool[0], "input": tool[1]}],
                "usage": {"input_tokens": rnd.randint(4, 40), "output_tokens": rnd.randint(250, 2600),
                          "cache_read_input_tokens": rnd.randint(30_000, 180_000), "cache_creation_input_tokens": rnd.randint(2_000, 24_000)}}})
            out.append({**base, "type": "user", "timestamp": iso(t + 2000), "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "ok\n" + "  src line of output\n" * rnd.randint(40, 200)}]}})
            if tool[0] == "Edit":
                out.append({**base, "type": "file-history-delta", "trackingPath": f, "timestamp": iso(t)})
        t += rnd.randint(30, 120) * 1000
        out.append({**base, "type": "assistant", "timestamp": iso(t), "message": {
            "id": f"msg_{uuid.uuid4().hex[:20]}", "model": model, "content": [{"type": "text", "text": rnd.choice(REPLIES)}],
            "usage": {"input_tokens": 12, "output_tokens": rnd.randint(300, 1200),
                      "cache_read_input_tokens": rnd.randint(40_000, 150_000), "cache_creation_input_tokens": rnd.randint(1_000, 8_000)}}})
        if i == 0 and plan:
            out.append({**base, "type": "assistant", "timestamp": iso(t + 1000), "message": {
                "id": f"msg_{uuid.uuid4().hex[:20]}", "model": model,
                "content": [{"type": "tool_use", "name": "ExitPlanMode", "input": {"plan": plan}}],
                "usage": {"input_tokens": 8, "output_tokens": 420}}})
        t += rnd.randint(3, 25) * MIN
    if hit:
        reset = time.strftime("%I:%M%p", time.localtime((t + 3 * HOUR) / 1000)).lstrip("0").lower()
        text = f"You've hit your {hit} limit · resets {reset} (Europe/Berlin)" if hit == "session" \
            else "You've hit your weekly limit · resets Oct 2, 9am (Europe/Berlin)"
        out.append({**base, "type": "assistant", "error": "rate_limit", "isApiErrorMessage": True, "timestamp": iso(t),
                    "message": {"id": f"msg_{uuid.uuid4().hex[:20]}", "model": "<synthetic>", "content": [{"type": "text", "text": text}]}})
    return out, t


def main(dest):
    dest = Path(dest)
    app, claude, config = dest / "app", dest / "claude", dest / "config"
    for p in (app, claude / "projects", config):
        p.mkdir(parents=True, exist_ok=True)
    (app / "plan-usage-history.json").write_text(json.dumps(usage_history()), encoding="utf-8")
    (config / "nicknames.json").write_text(json.dumps({org: nick for org, nick, _, _ in ACCOUNTS}, indent=2), encoding="utf-8")
    all_sessions = [(*x, False) for x in SESSIONS] + [(*x, None, True) for x in DELETED]
    for n, (title, proj, acct, days_ago, prompts, hit, deleted) in enumerate(all_sessions):
        extra = EXTRAS.get(title, {})
        org = ACCOUNTS[acct][0]
        sid = str(uuid.UUID(int=rnd.getrandbits(128), version=4))
        cwd = f"{ROOT}/{proj}" + (f"/.claude/worktrees/{extra['worktree']}" if extra.get("worktree") else "")
        duration = prompts * 9 * MIN
        start = NOW - int(days_ago * DAY) - duration
        start -= start % DAY % HOUR  # keep it tidy
        entry = extra.get("entry", "claude-desktop")
        lines, end = transcript(sid, title, cwd, proj, org, start, prompts, hit, entry, extra.get("plan"))
        tdir = claude / "projects" / ("-Users-alex-code-" + proj)
        tdir.mkdir(parents=True, exist_ok=True)
        with open(tdir / f"{sid}.jsonl", "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        sdir = app / "claude-code-sessions" / ACCOUNT_ID / org
        sdir.mkdir(parents=True, exist_ok=True)
        if deleted:   # the desktop app leaves a marker; the transcript stays on disk
            (sdir / f"deleted_{sid}").write_text(str(NOW - HOUR), encoding="utf-8")
        elif entry == "claude-desktop":
            (sdir / f"local_{n:04d}.json").write_text(json.dumps({
                "sessionId": f"local_{n:04d}", "cliSessionId": sid, "cwd": cwd, "title": title,
                "createdAt": start, "lastActivityAt": end, "model": "claude-opus-5-5",
                "isArchived": bool(extra.get("archived")), "completedTurns": prompts}), encoding="utf-8")
        if extra.get("live"):   # no pid: counted as open while updatedAt is recent (10 minutes)
            (claude / "sessions").mkdir(exist_ok=True)
            (claude / "sessions" / f"{n}.json").write_text(json.dumps({
                "sessionId": sid, "cwd": cwd, "status": extra["live"], "entrypoint": entry,
                "kind": "interactive", "updatedAt": NOW, "startedAt": start}), encoding="utf-8")
        if extra.get("job"):
            jdir = claude / "jobs" / sid[:8]
            jdir.mkdir(parents=True, exist_ok=True)
            (jdir / "state.json").write_text(json.dumps({
                "name": extra["job"][0], "state": extra["job"][1], "sessionId": sid}), encoding="utf-8")
    for acct, names in SCHEDULED.items():
        sdir = app / "claude-code-sessions" / ACCOUNT_ID / ACCOUNTS[acct][0]
        (sdir / "scheduled-tasks.json").write_text(json.dumps({"scheduledTasks": [{"name": x} for x in names]}), encoding="utf-8")
    print(f"Demo data written to {dest}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-data")
