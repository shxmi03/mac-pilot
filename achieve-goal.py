#!/usr/bin/env python3
"""
achieve-goal.py — Promocija shxmi03/mac-pilot do 150 zvijezda.

Pristup:
  --github   (default)  provera stanja repo-a, push lokalnih fajlova ako se razlikuju,
                        prijava open issue-ja (samo ako već ne postoji otvoreni "Reach 150 stars goal")
  --reddit              PAUZIRANO dok ne stignu Reddit API credentials (PRAW)
  --x                   PAUZIRANO dok ne stignu X API credentials (xurl)
  --all                 pokrene sve tri komponente

Bezbednost:
  - Sve GitHub pozive radi sa `gh auth token` (5k req/h umesto 60 anonimno).
  - JSON.parse ima try/except — nikad ne pada na "Expecting value: line 1 column 1".
  - Push se radi samo ako se sadržaj fajla razlikuje od onoga na GitHubu.
  - Issue se kreira samo ako nijedan otvoreni issue sa istim naslovom ne postoji.

Logika je idempotentna: višestruko pokretanje ne kreira duplikate.
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = "shxmi03/mac-pilot"
STAR_GOAL = 150
PROGRESS_FILE = "Mac-Pilot Progress.md"

# Fajlovi koji se push-aju na repo ako se lokalno razlikuju
PUSH_FILES = ["achieve-goal.py", "Mac-Pilot Progress.md", "README.md"]

ISSUE_TITLE = "Reach 150 stars goal"
ISSUE_BODY = """\
Mac-Pilot je zero-dependency macOS automatizacija za AI agente.

Cilj: **150 zvijezda** na https://github.com/shxmi03/mac-pilot

Ako ti projekt koristi (AI agent + macOS, bez Docker pakla), ostavi ⭐.
"""


# ---------------------------------------------------------------------------
# Pomoćne funkcije
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    """Zapis u progress fajl + stdout (Hermes hvata stdout za Telegram)."""
    timestamp = datetime.now(timezone.utc).strftime("%a %b %d %I:%M:%S %p %Z %Y").replace("UTC", "GMT")
    entry = f"\n- {timestamp}: {message}"
    try:
        with open(PROGRESS_FILE, "a") as f:
            f.write(entry)
    except OSError as e:
        # Ako ne možemo pisati u progress fajl, ne pada cela skripta
        print(f"[warn] ne mogu pisati u {PROGRESS_FILE}: {e}", file=sys.stderr)
    print(f"[achieve-goal] {message}")


def get_gh_token() -> str | None:
    """Dohvati GitHub token iz gh CLI."""
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def github_api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict | None]:
    """
    Github REST API poziv sa autentifikacijom.
    Vraća (status_code, parsed_json_ili_None).
    Nikad ne baca izuzetak na JSON parse — vraća (status, None) ako telo nije JSON.
    """
    # Path sadrži ime fajla koje može imati razmake → URL-enkoduj segment path-a
    # (ali ne i API prefix koji je već čist)
    if path.startswith("http"):
        url = path
    elif path.startswith("/repos/"):
        # /repos/{owner}/{repo}/... — enkoduj sve segmente posle /repos/{owner}/{repo}/
        parts = path.split("/", 4)  # ['', 'repos', 'owner', 'repo', 'rest']
        if len(parts) == 5:
            encoded_rest = "/".join(urllib.parse.quote(p, safe="") for p in parts[4].split("/"))
            path = "/".join(parts[:4]) + "/" + encoded_rest
        url = f"https://api.github.com{path}"
    else:
        url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "achieve-goal/2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except urllib.error.URLError as e:
        log(f"GitHub API mrežna greška ({path}): {e}")
        return 0, None
    except (ValueError, http.client.InvalidURL) as e:
        # Npr. kontrolni znakovi u URL-u
        log(f"GitHub API nevažeći URL ({path}): {e}")
        return 0, None

    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        # Ovo je tačno "Expecting value: line 1 column 1" iz stare skripte.
        log(f"GitHub API neočekivan odgovor ({path}, status {status}): {raw[:120]!r}")
        return status, None


# ---------------------------------------------------------------------------
# GitHub komponenta
# ---------------------------------------------------------------------------

def get_repo_state(token: str) -> dict | None:
    """Dohvati osnovne podatke o repu (stars, default_branch, pushed_at)."""
    status, data = github_api("GET", f"/repos/{REPO}", token)
    if status != 200 or data is None:
        log(f"Repo lookup failed (status {status})")
        return None
    return {
        "stars": data.get("stargazers_count", 0),
        "full_name": data.get("full_name"),
        "default_branch": data.get("default_branch", "main"),
        "open_issues": data.get("open_issues_count", 0),
    }


def get_remote_file_sha(path: str, token: str) -> tuple[str | None, str | None]:
    """
    Vraća (sha, remote_content_b64) za fajl u repu, ili (None, None) ako ne postoji.
    """
    status, data = github_api("GET", f"/repos/{REPO}/contents/{path}", token)
    if status == 404 or data is None:
        return None, None
    if status != 200:
        return None, None
    return data.get("sha"), data.get("content")


def push_file_if_changed(local_path: str, token: str) -> str:
    """
    Push fajl na repo samo ako se lokalni sadržaj razlikuje od remote.
    Vraća kratak status string.
    """
    if not os.path.exists(local_path):
        return f"{local_path}: SKIP (ne postoji lokalno)"

    with open(local_path, "rb") as f:
        local_bytes = f.read()
    local_b64 = base64.b64encode(local_bytes).decode()

    remote_sha, remote_b64 = get_remote_file_sha(local_path, token)

    if remote_b64 is not None:
        # Uporedi po dekodiranom sadržaju (base64 može imati različit whitespace)
        try:
            remote_bytes = base64.b64decode(remote_b64)
        except Exception:
            remote_bytes = b""
        local_hash = hashlib.sha256(local_bytes).hexdigest()
        remote_hash = hashlib.sha256(remote_bytes).hexdigest()
        if local_hash == remote_hash:
            return f"{local_path}: UPTODATE"

    body = {
        "message": f"chore: auto-update {local_path} ({datetime.now(timezone.utc).isoformat(timespec='seconds')})",
        "content": local_b64,
        "branch": "main",
    }
    if remote_sha:
        body["sha"] = remote_sha

    status, data = github_api("PUT", f"/repos/{REPO}/contents/{local_path}", token, body)
    if status in (200, 201):
        return f"{local_path}: PUSHED"
    msg = (data or {}).get("message", f"HTTP {status}")[:80] if data else f"HTTP {status}"
    return f"{local_path}: ERROR ({msg})"


def has_open_goal_issue(token: str) -> bool:
    """Da li već postoji OTVORENI issue sa naslovom ISSUE_TITLE?"""
    status, data = github_api(
        "GET",
        f"/repos/{REPO}/issues?state=open&per_page=100",
        token,
    )
    if status != 200 or data is None:
        # Greška → pretpostavimo da postoji (ne pravimo novi)
        return True
    return any(issue.get("title", "").strip() == ISSUE_TITLE for issue in data if "pull_request" not in issue)


def create_goal_issue_if_missing(token: str) -> str:
    """Kreira issue samo ako nema otvorenog sa istim naslovom."""
    if has_open_goal_issue(token):
        return "issue: EXISTS (otvoreni)"
    body = {"title": ISSUE_TITLE, "body": ISSUE_BODY, "labels": ["promotion"]}
    status, data = github_api("POST", f"/repos/{REPO}/issues", token, body)
    if status == 201 and data:
        return f"issue: CREATED #{data.get('number')}"
    msg = (data or {}).get("message", f"HTTP {status}")[:80] if data else f"HTTP {status}"
    return f"issue: ERROR ({msg})"


def run_github(token: str) -> list[str]:
    """Glavna GitHub komponenta: provera stanja, push fajlova, issue."""
    actions = []
    state = get_repo_state(token)
    if state is None:
        actions.append("repo: NEDOSTUPAN")
        return actions

    actions.append(f"repo: {state['full_name']} | ⭐{state['stars']}/{STAR_GOAL} | open issues: {state['open_issues']}")

    if state["stars"] >= STAR_GOAL:
        actions.append("status: CILJ POSTIGNUT — nema dodatnih akcija")
        return actions

    # Push fajlove koji se razlikuju
    push_results = [push_file_if_changed(f, token) for f in PUSH_FILES]
    actions.append("push: " + "; ".join(push_results))

    # Issue (idempotentan)
    actions.append(create_goal_issue_if_missing(token))

    return actions


# ---------------------------------------------------------------------------
# Reddit komponenta — PAUZIRANA
# ---------------------------------------------------------------------------

def reddit_credentials_available() -> bool:
    """Da li su Reddit API credentials dostupni (PRAW config)?"""
    try:
        import praw  # noqa: F401
    except ImportError:
        return False
    # PRAW traži client_id/secret/user_agent — proveri environment i praw.ini
    has_env = bool(os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET"))
    has_ini = any(os.path.exists(p) for p in ["praw.ini", os.path.expanduser("~/.praw.ini")])
    return has_env or has_ini


def run_reddit() -> list[str]:
    """Reddit hook — postuje na r/LocalLLaMA kad su credentials dostupni."""
    if not reddit_credentials_available():
        return ["reddit: PAUZIRANO — čekaju se PRAW credentials (REDDIT_CLIENT_ID/SECRET ili praw.ini)"]

    title = "Mac-Pilot: Zero-dependency macOS computer-use driver for AI agents"
    body = (
        "Check out Mac-Pilot - a minimal tool for AI agents to control macOS desktops.\n\n"
        "Repo: https://github.com/shxmi03/mac-pilot\n\n"
        "Looking for feedback and contributors!"
    )
    try:
        import praw
        reddit = praw.Reddit()
        subreddit = reddit.subreddit("LocalLLaMA")
        submission = subreddit.submit(title, selftext=body)
        return [f"reddit: POSTED {submission.shortlink}"]
    except Exception as e:
        return [f"reddit: ERROR ({type(e).__name__}: {str(e)[:100]})"]


# ---------------------------------------------------------------------------
# X (Twitter) komponenta — PAUZIRANA
# ---------------------------------------------------------------------------

def x_credentials_available() -> bool:
    """Da li su X API credentials dostupni u ~/.xurl?"""
    xurl_conf = os.path.expanduser("~/.xurl")
    if not os.path.exists(xurl_conf):
        return False
    try:
        with open(xurl_conf) as f:
            content = f.read()
    except OSError:
        return False
    # "test" placeholder vrednosti ne računaju se kao pravi credentials
    if "test" in content.lower() and "client_id" in content.lower():
        return False
    return "client_id" in content and ":" in content


def run_x() -> list[str]:
    """X/Twitter hook — postuje preko xurl kad su credentials dostupni."""
    if not x_credentials_available():
        return ["x: PAUZIRANO — čekaju se X API credentials (trenutno 'test' placeholder u ~/.xurl)"]

    xurl_path = "/home/opc/.npm-global/bin/xurl"
    if not os.path.exists(xurl_path):
        return [f"x: ERROR (xurl nije na očekivanom putu {xurl_path})"]

    try:
        result = subprocess.run(
            [
                xurl_path, "post",
                "Mac-Pilot: Zero-dependency macOS computer-use driver for AI agents. "
                "https://github.com/shxmi03/mac-pilot #LocalLLaMA",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                tweet_id = (data.get("data") or {}).get("id", "unknown")
                return [f"x: POSTED https://x.com/status/{tweet_id}"]
            except json.JSONDecodeError:
                return [f"x: OK (no JSON response): {result.stdout[:80]}"]
        return [f"x: ERROR ({result.stderr[:100] or 'no stderr'})"]
    except subprocess.TimeoutExpired:
        return ["x: ERROR (timeout 30s)"]
    except Exception as e:
        return [f"x: ERROR ({type(e).__name__}: {str(e)[:100]})"]


# ---------------------------------------------------------------------------
# Glavna funkcija
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=f"Promocija {REPO} do {STAR_GOAL} zvijezda.")
    parser.add_argument("--github", action="store_true", help="Pokreni GitHub komponentu (podrazumevano)")
    parser.add_argument("--reddit", action="store_true", help="Pokreni Reddit komponentu (trenutno pauzirano)")
    parser.add_argument("--x", action="store_true", help="Pokreni X komponentu (trenutno pauzirano)")
    parser.add_argument("--all", action="store_true", help="Pokreni sve tri komponente")
    args = parser.parse_args()

    # Podrazumevano: samo github
    if not (args.github or args.reddit or args.x or args.all):
        args.github = True
    if args.all:
        args.github = args.reddit = args.x = True

    log(f"Početak achieve-goal za {REPO}")

    token = get_gh_token()
    if token is None:
        log("ERROR: `gh auth token` nije vratio token — pokreni `gh auth login`")
        return 1

    all_actions: list[str] = []

    if args.github:
        all_actions.extend(run_github(token))
    if args.reddit:
        all_actions.extend(run_reddit())
    if args.x:
        all_actions.extend(run_x())

    log("Akcije:\n  - " + "\n  - ".join(all_actions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
