#!/usr/bin/env python3
"""
GitHub Issue Watcher
--------------------
Track GitHub repos and get an email when:
  * a new issue is opened
  * an issue carries the "good first issue" label  (subject gets a 🚨 alert emoji)

Usage:
  python repo_watcher.py add owner/repo [https://github.com/o/r ...] [--notify-existing-gfi]
  python repo_watcher.py remove owner/repo
  python repo_watcher.py list
  python repo_watcher.py check [--dry-run]     # one polling pass (good for cron)
  python repo_watcher.py watch [--interval 300]# keep polling forever
  python repo_watcher.py test-email            # verify SMTP settings
  python repo_watcher.py cloud                 # repos.txt + state.json mode (GitHub Actions)

Config comes from environment variables or a .env file next to this script.
Pure standard library - no pip install needed.
"""
import argparse
import html
import json
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
API = "https://api.github.com"
GFI_LABELS = {"good first issue", "good-first-issue"}
PAGE_SIZE = 100
MAX_PAGES = 5            # safety cap per repo per pass (500 issues)
OVERLAP_SECONDS = 120    # re-fetch a little history so nothing slips between polls
ALERT_EMOJI = "🚨"


# --------------------------------------------------------------------------- config
def load_env():
    env_file = BASE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- storage
def db(path=None):
    path = path or Path(os.environ.get("WATCHER_DB", BASE / "watcher.db"))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repos (
            full_name    TEXT PRIMARY KEY,
            added_at     TEXT NOT NULL,
            last_checked TEXT
        );
        CREATE TABLE IF NOT EXISTS issues (
            repo         TEXT NOT NULL,
            number       INTEGER NOT NULL,
            gfi_notified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (repo, number)
        );
        """
    )
    return conn


# --------------------------------------------------------------------------- GitHub
class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    pass


def parse_repo(text):
    """Accept 'owner/name' or a github.com URL."""
    text = text.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$", text)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.match(r"^([\w.-]+)/([\w.-]+)$", text)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    raise ValueError(f"Not a valid repo: {text!r} (use owner/name or a GitHub URL)")


def gh_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gh-issue-watcher",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise GitHubError("repo not found (or private - set GITHUB_TOKEN)")
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = e.headers.get("X-RateLimit-Reset")
            when = datetime.fromtimestamp(int(reset)).strftime("%H:%M:%S") if reset else "later"
            raise RateLimited(f"GitHub rate limit hit, resets at {when}. Set GITHUB_TOKEN for a higher limit.")
        raise GitHubError(f"GitHub API error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise GitHubError(f"network error: {e.reason}")


def fetch_open_issues(repo, since=None):
    """Open issues (PRs excluded), most recently updated first."""
    issues = []
    for page in range(1, MAX_PAGES + 1):
        params = {"state": "open", "sort": "updated", "direction": "desc",
                  "per_page": PAGE_SIZE, "page": page}
        if since:
            params["since"] = since
        batch = gh_get(f"/repos/{repo}/issues", params)
        issues += [i for i in batch if "pull_request" not in i]
        if len(batch) < PAGE_SIZE:
            break
    return issues


def is_gfi(issue):
    return any(l["name"].strip().lower() in GFI_LABELS for l in issue.get("labels", []))


# --------------------------------------------------------------------------- email
def smtp_settings():
    s = {
        "host": os.environ.get("SMTP_HOST") or "smtp.gmail.com",
        "port": int(os.environ.get("SMTP_PORT") or "587"),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "to": os.environ.get("MAIL_TO", ""),
    }
    s["from"] = os.environ.get("MAIL_FROM") or s["user"]
    missing = [k for k in ("user", "password", "to") if not s[k]]
    if missing:
        raise RuntimeError(
            "Email not configured - missing: "
            + ", ".join({"user": "SMTP_USER", "password": "SMTP_PASSWORD", "to": "MAIL_TO"}[m] for m in missing)
        )
    return s


def send_email(subject, text, html_body=None):
    s = smtp_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["from"]
    msg["To"] = s["to"]
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if s["port"] == 465:
        with smtplib.SMTP_SSL(s["host"], s["port"], context=ctx, timeout=30) as smtp:
            smtp.login(s["user"], s["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s["host"], s["port"], timeout=30) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(s["user"], s["password"])
            smtp.send_message(msg)


def build_message(repo, issue, kind):
    n, title, url = issue["number"], issue["title"], issue["html_url"]
    labels = ", ".join(l["name"] for l in issue.get("labels", [])) or "none"
    author = issue.get("user", {}).get("login", "unknown")
    body = (issue.get("body") or "").strip()
    snippet = body[:600] + ("…" if len(body) > 600 else "")

    if kind == "gfi":
        subject = f"{ALERT_EMOJI} Good First Issue: {repo} #{n} - {title}"
        headline = f"{ALERT_EMOJI} GOOD FIRST ISSUE"
    else:
        subject = f"New issue: {repo} #{n} - {title}"
        headline = "New issue"

    text = (
        f"{headline}\n\n{repo} #{n}: {title}\nBy: {author}\nLabels: {labels}\n{url}\n\n"
        f"{snippet or '(no description)'}\n"
    )
    esc = html.escape
    html_body = f"""\
<div style="font-family:system-ui,sans-serif;max-width:600px">
  <h2 style="margin:0 0 8px;color:{'#d1242f' if kind == 'gfi' else '#1f2328'}">{esc(headline)}</h2>
  <p style="margin:0 0 4px"><b>{esc(repo)}</b> #{n}</p>
  <h3 style="margin:0 0 8px"><a href="{esc(url)}">{esc(title)}</a></h3>
  <p style="margin:0 0 12px;color:#57606a">by {esc(author)} &middot; labels: {esc(labels)}</p>
  <pre style="white-space:pre-wrap;background:#f6f8fa;padding:12px;border-radius:6px">{esc(snippet or '(no description)')}</pre>
  <p><a href="{esc(url)}">Open on GitHub &rarr;</a></p>
</div>"""
    return subject, text, html_body


# --------------------------------------------------------------------------- core logic
def check_repo(conn, repo, dry_run=False):
    """Poll one repo. Returns number of notifications sent."""
    started = datetime.now(timezone.utc)
    row = conn.execute("SELECT last_checked FROM repos WHERE full_name=?", (repo,)).fetchone()
    since = None
    if row and row["last_checked"]:
        last = datetime.strptime(row["last_checked"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        since = now_iso(last - timedelta(seconds=OVERLAP_SECONDS))

    issues = fetch_open_issues(repo, since)
    sent, failed = 0, False

    for issue in sorted(issues, key=lambda i: i["number"]):
        n = issue["number"]
        gfi = is_gfi(issue)
        seen = conn.execute("SELECT gfi_notified FROM issues WHERE repo=? AND number=?", (repo, n)).fetchone()

        if seen is None:
            kind = "gfi" if gfi else "new"           # brand-new issue
        elif gfi and not seen["gfi_notified"]:
            kind = "gfi"                             # label added to an already-seen issue
        else:
            continue

        subject, text, html_body = build_message(repo, issue, kind)
        if dry_run:
            log(f"[dry-run] would send: {subject}")
            sent += 1
            continue
        try:
            send_email(subject, text, html_body)
        except Exception as e:                       # don't mark as seen -> retried next pass
            log(f"  email failed for {repo}#{n}: {e}")
            failed = True
            continue
        conn.execute(
            "INSERT INTO issues(repo, number, gfi_notified) VALUES(?,?,?) "
            "ON CONFLICT(repo, number) DO UPDATE SET gfi_notified=MAX(gfi_notified, excluded.gfi_notified)",
            (repo, n, 1 if gfi else 0),
        )
        conn.commit()
        sent += 1
        log(f"  sent {kind}: {repo}#{n} {issue['title'][:60]}")

    if not dry_run and not failed:
        conn.execute("UPDATE repos SET last_checked=? WHERE full_name=?", (now_iso(started), repo))
        conn.commit()
    return sent


def check_all(conn, dry_run=False):
    repos = [r["full_name"] for r in conn.execute("SELECT full_name FROM repos ORDER BY added_at")]
    if not repos:
        log("No repos tracked yet. Add one with: python repo_watcher.py add owner/repo")
        return
    total = 0
    for repo in repos:
        try:
            total += check_repo(conn, repo, dry_run)
        except RateLimited as e:
            log(f"{e} - stopping this pass early.")
            break
        except GitHubError as e:
            log(f"  {repo}: {e}")
    log(f"Pass complete: {len(repos)} repo(s), {total} notification(s).")


# --------------------------------------------------------------------------- commands
def add_repo(conn, raw, notify_existing_gfi=False):
    """Validate a repo, baseline its open issues, start tracking. Returns canonical name or None."""
    try:
        name = parse_repo(raw)
        info = gh_get(f"/repos/{name}")
        canonical = info["full_name"]
        if conn.execute("SELECT 1 FROM repos WHERE lower(full_name)=lower(?)", (canonical,)).fetchone():
            print(f"  already tracking {canonical}")
            return None
        # Baseline: mark everything currently open as seen so you aren't flooded.
        existing = fetch_open_issues(canonical)
        conn.execute("INSERT INTO repos(full_name, added_at, last_checked) VALUES(?,?,?)",
                     (canonical, now_iso(), None if notify_existing_gfi else now_iso()))
        for i in existing:
            # Existing GFI issues are "already notified" (unless asked otherwise);
            # non-GFI ones stay 0 so a label added later still triggers an alert.
            notified = 1 if (is_gfi(i) and not notify_existing_gfi) else 0
            conn.execute("INSERT OR IGNORE INTO issues(repo, number, gfi_notified) VALUES(?,?,?)",
                         (canonical, i["number"], notified))
        conn.commit()
        extra = " (existing good-first-issues will be emailed on next check)" if notify_existing_gfi else ""
        print(f"  + {canonical} - baseline of {len(existing)} open issue(s){extra}")
        return canonical
    except (ValueError, GitHubError) as e:
        print(f"  ! {raw}: {e}")
        return None


def cmd_add(args):
    conn = db()
    for raw in args.repos:
        add_repo(conn, raw, args.notify_existing_gfi)


def cmd_remove(args):
    conn = db()
    for raw in args.repos:
        try:
            name = parse_repo(raw)
        except ValueError as e:
            print(f"  ! {e}")
            continue
        cur = conn.execute("DELETE FROM repos WHERE lower(full_name)=lower(?)", (name,))
        conn.execute("DELETE FROM issues WHERE lower(repo)=lower(?)", (name,))
        conn.commit()
        print(f"  - removed {name}" if cur.rowcount else f"  ! {name} was not tracked")


def cmd_list(_args):
    rows = db().execute("SELECT * FROM repos ORDER BY added_at").fetchall()
    if not rows:
        print("No repos tracked yet.")
    for r in rows:
        print(f"  {r['full_name']:<40} last checked: {r['last_checked'] or 'never'}")


def cmd_check(args):
    check_all(db(), dry_run=args.dry_run)


def cmd_watch(args):
    conn = db()
    log(f"Watching every {args.interval}s. Ctrl+C to stop.")
    try:
        while True:
            check_all(conn)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("Stopped.")


def cmd_test_email(_args):
    send_email(f"{ALERT_EMOJI} GitHub watcher test", "If you can read this, email notifications work.")
    print("Test email sent.")


# --------------------------------------------------------------------------- cloud mode
# Used by the GitHub Actions workflow: repos come from repos.txt, memory lives in state.json
# (committed back to the repo after every run), so nothing depends on your PC being on.
STATE_FILE = BASE / "state.json"
REPOS_FILE = BASE / "repos.txt"


def read_repos_file():
    wanted = {}
    if not REPOS_FILE.exists():
        return wanted
    for line in REPOS_FILE.read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        try:
            name = parse_repo(parts[0])
        except ValueError as e:
            log(f"repos.txt: {e}")
            continue
        wanted[name.lower()] = (name, "notify-existing" in parts[1:])
    return wanted


def load_state(conn):
    if not STATE_FILE.exists():
        return
    state = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
    for name, last in state.get("repos", {}).items():
        conn.execute("INSERT INTO repos(full_name, added_at, last_checked) VALUES(?,?,?)", (name, now_iso(), last))
    for name, rows in state.get("issues", {}).items():
        conn.executemany("INSERT INTO issues(repo, number, gfi_notified) VALUES(?,?,?)",
                         [(name, n, flag) for n, flag in rows])
    conn.commit()


def save_state(conn):
    repos = {r["full_name"]: r["last_checked"]
             for r in conn.execute("SELECT full_name, last_checked FROM repos ORDER BY full_name")}
    issues = {}
    for r in conn.execute("SELECT repo, number, gfi_notified FROM issues ORDER BY repo, number"):
        issues.setdefault(r["repo"], []).append([r["number"], r["gfi_notified"]])
    body = ",\n".join(f" {json.dumps(k)}:{json.dumps(v, separators=(',', ':'))}" for k, v in issues.items())
    STATE_FILE.write_text(
        '{"repos":' + json.dumps(repos, sort_keys=True) + ',\n"issues":{\n' + body + "\n}}\n",
        encoding="utf-8",
    )


def cmd_cloud(_args):
    conn = db(":memory:")
    load_state(conn)
    wanted = read_repos_file()
    tracked = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos")}

    for name in sorted(tracked):                       # removed from repos.txt -> stop tracking
        if name.lower() not in wanted:
            conn.execute("DELETE FROM repos WHERE full_name=?", (name,))
            conn.execute("DELETE FROM issues WHERE repo=?", (name,))
            log(f"  - no longer tracking {name}")
    tracked_lower = {t.lower() for t in tracked}
    for lower, (name, notify_existing) in wanted.items():   # new in repos.txt -> baseline
        if lower not in tracked_lower:
            add_repo(conn, name, notify_existing)
    conn.commit()

    try:
        check_all(conn)
    finally:
        save_state(conn)


def main():
    load_env()
    p = argparse.ArgumentParser(description="Track GitHub repos and email new / good-first-issue issues.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="start tracking repos")
    a.add_argument("repos", nargs="+")
    a.add_argument("--notify-existing-gfi", action="store_true",
                   help="also email currently-open good first issues on the next check")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove", help="stop tracking repos")
    r.add_argument("repos", nargs="+")
    r.set_defaults(fn=cmd_remove)

    sub.add_parser("list", help="show tracked repos").set_defaults(fn=cmd_list)

    c = sub.add_parser("check", help="run one polling pass")
    c.add_argument("--dry-run", action="store_true", help="print what would be sent; change nothing")
    c.set_defaults(fn=cmd_check)

    w = sub.add_parser("watch", help="poll continuously")
    w.add_argument("--interval", type=int, default=int(os.environ.get("POLL_INTERVAL", "300")),
                   help="seconds between passes (default 300)")
    w.set_defaults(fn=cmd_watch)

    sub.add_parser("test-email", help="send a test email").set_defaults(fn=cmd_test_email)
    sub.add_parser("cloud", help="one pass driven by repos.txt + state.json (used by GitHub Actions)").set_defaults(fn=cmd_cloud)

    args = p.parse_args()
    try:
        args.fn(args)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
