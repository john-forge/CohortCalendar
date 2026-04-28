#!/usr/bin/env python3
"""
publish.py — one-shot publish for the cohort calendar.

What it does:
  1. Reads state.json (the canonical source of truth on disk).
  2. Regenerates the PUBLISHED_STATE constant inside index.html so
     anyone loading the page sees the new state as the default.
  3. git add + commit + push (so GitHub Pages serves the new build).
  4. Upserts state.json into the Supabase `documents` row so the
     real-time live state matches — otherwise Supabase's stale row
     would overwrite the fresh PUBLISHED_STATE on every page load
     and your code-level changes (descriptions, splits, etc.) get
     silently dropped.

Usage:
    python3 publish.py "your commit message"

If you skip the message, the script aborts (we want a meaningful
commit message, not a generic one).

Requires:
  - state.json and index.html in the same directory as this script.
  - git remote configured.
  - curl in $PATH (we use it to talk to Supabase since it has the
    system trust store; Python's stdlib SSL config is iffy on macOS).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATE_PATH = REPO / "state.json"
HTML_PATH = REPO / "index.html"

# Supabase write target. URL + anon key are public (they're already in
# index.html). The anon key has only the privileges Supabase RLS allows.
SUPABASE_URL = "https://vaqdoeckaobmsalikmpx.supabase.co"
SUPABASE_KEY = "sb_publishable_UlWZDjS5Yx07Cl-reOlLAg_qOsp7DLn"
DOC_ID = "main"


def fail(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd, **kw):
    """Run a shell command, raise on failure, return stdout."""
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, **kw)
    if r.returncode != 0:
        fail(f"command failed ({' '.join(cmd)}):\n{r.stderr.strip()}")
    return r.stdout.strip()


def regenerate_published_state():
    """Replace the PUBLISHED_STATE constant in index.html with state.json."""
    state = json.loads(STATE_PATH.read_text())
    html = HTML_PATH.read_text()
    new_publish = "const PUBLISHED_STATE = " + json.dumps(state) + ";"
    html_new, n = re.subn(
        r"const PUBLISHED_STATE = \{.*?\};",
        lambda m: new_publish,
        html,
        count=1,
        flags=re.DOTALL,
    )
    if n != 1:
        fail("Could not find PUBLISHED_STATE in index.html — did the format change?")
    if html_new == html:
        return False  # no-op
    HTML_PATH.write_text(html_new)
    return True


def push_to_supabase():
    """Upsert state.json into Supabase documents row 'main' via curl."""
    state = json.loads(STATE_PATH.read_text())
    payload = json.dumps([{
        "id": DOC_ID,
        "data": state,
        "client_id": "publish-script",
    }])
    # Hand the payload via stdin so we don't blow out the arg list.
    cmd = [
        "curl", "-sS",
        "-X", "POST",
        f"{SUPABASE_URL}/rest/v1/documents",
        "-H", f"apikey: {SUPABASE_KEY}",
        "-H", f"Authorization: Bearer {SUPABASE_KEY}",
        "-H", "Content-Type: application/json",
        "-H", "Prefer: resolution=merge-duplicates,return=representation",
        "-w", "\n%{http_code}",
        "--data-binary", "@-",
    ]
    r = subprocess.run(cmd, input=payload, capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        fail(f"curl failed: {r.stderr.strip()}")
    out = r.stdout.strip().splitlines()
    if not out:
        fail("empty response from Supabase")
    code = out[-1]
    if not code.startswith("2"):
        body = "\n".join(out[:-1])
        fail(f"Supabase HTTP {code}: {body[:500]}")
    print(f"  Supabase upsert OK (HTTP {code})")


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        fail('Need a commit message. Usage: python3 publish.py "what changed"')
    msg = sys.argv[1].strip()

    if not STATE_PATH.exists() or not HTML_PATH.exists():
        fail("Run this from the repo root (state.json and index.html missing).")

    print("1) Regenerating PUBLISHED_STATE in index.html…")
    changed = regenerate_published_state()
    print(f"   {'updated' if changed else 'already in sync'}")

    print("2) Staging + committing…")
    # Only stage the two canonical files. Avoids accidentally committing
    # screenshots, .claude/, etc.
    run(["git", "add", "state.json", "index.html"])
    diff = run(["git", "diff", "--cached", "--name-only"])
    if not diff:
        print("   nothing to commit (working tree clean against HEAD)")
    else:
        run(["git", "commit", "-m", msg])
        print(f"   committed: {msg}")

    print("3) Pushing to GitHub…")
    out = run(["git", "push"])
    print(f"   {out or 'up to date'}")

    print("4) Pushing to Supabase…")
    push_to_supabase()

    print("\nDone. GitHub Pages will rebuild in ~1-2 min; Supabase is live now.")


if __name__ == "__main__":
    main()
