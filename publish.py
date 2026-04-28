#!/usr/bin/env python3
"""
publish.py — one-shot publish for the cohort calendar.

What it does (in order):
  1. Pull current Supabase row into memory ('live').
  2. Read disk state.json ('disk') and the previous-publish baseline
     ('.last_published.json'). 3-way-merge live + disk against baseline:
       - User UI deletes propagate (block in baseline + disk, missing
         from live → drop).
       - Code-side deletes propagate (block in baseline + live, missing
         from disk → drop).
       - User UI adds propagate (block in live only → keep).
       - Code-side adds propagate (block in disk only → keep).
       - Per-field merges: if I didn't touch a field, use live's value;
         if user didn't touch a field, use my disk value; if both
         changed, position fields fall back to live, content fields
         to disk.
  3. Bump __schema_version on the merged result.
  4. Write merged result to state.json.
  5. Regenerate PUBLISHED_STATE in index.html.
  6. git add + commit + push.
  7. Upsert merged result into Supabase.
  8. Snapshot the merged result as the new baseline.

This means: John can edit blocks in the browser (drag, edit description,
delete) freely; Claude can edit state.json freely; both sets of changes
survive a publish, and only true contradictions need disambiguation.

Usage:
    python3 publish.py "your commit message"

Pull-only mode (refresh disk from Supabase without publishing):
    python3 publish.py --pull

Run from the repo root.
"""

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATE_PATH = REPO / "state.json"
HTML_PATH = REPO / "index.html"
BASELINE_PATH = REPO / ".last_published.json"

SUPABASE_URL = "https://vaqdoeckaobmsalikmpx.supabase.co"
SUPABASE_KEY = "sb_publishable_UlWZDjS5Yx07Cl-reOlLAg_qOsp7DLn"
DOC_ID = "main"

# Block fields where user UI drags are authoritative on conflicts.
POSITION_FIELDS = {"w", "d", "s", "dur"}

# Top-level UI-state fields that should follow Supabase when there's
# divergence (these reflect "what someone is currently looking at",
# not document structure).
UI_FIELDS = {
    "currentWeek", "viewMode", "guideFilter",
    "selectedBlockIds", "selectedPinId", "fullChallenge",
    "visibleGradeIds",
}


def fail(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, **kw)
    if r.returncode != 0:
        fail(f"command failed ({' '.join(cmd)}):\n{r.stderr.strip()}")
    return r.stdout.strip()


# ---------- Supabase I/O ----------

def fetch_supabase_state():
    """Return the data dict in the documents row, or None if missing."""
    cmd = [
        "curl", "-sSf",
        f"{SUPABASE_URL}/rest/v1/documents?id=eq.{DOC_ID}&select=data",
        "-H", f"apikey: {SUPABASE_KEY}",
        "-H", f"Authorization: Bearer {SUPABASE_KEY}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        print(f"  WARNING: Supabase fetch failed: {r.stderr.strip()[:200]}")
        return None
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        print("  WARNING: Supabase returned non-JSON")
        return None
    if not rows:
        return None
    return rows[0].get("data")


def upsert_supabase(state):
    payload = json.dumps([{
        "id": DOC_ID,
        "data": state,
        "client_id": "publish-script",
    }])
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
    if not out or not out[-1].startswith("2"):
        fail(f"Supabase HTTP {out[-1] if out else '?'}: {chr(10).join(out[:-1])[:500]}")
    print(f"  Supabase upsert OK (HTTP {out[-1]})")


# ---------- 3-way merge ----------

def merge_block(disk_b, live_b, base_b):
    """Field-level 3-way merge for a single block present in all three sources."""
    fields = set(disk_b) | set(live_b) | set(base_b or {})
    result = {}
    for f in fields:
        d = disk_b.get(f)
        l = live_b.get(f)
        b = (base_b or {}).get(f)
        if d == b:
            # I didn't touch this field; take live's value (could be user's edit, or unchanged).
            result[f] = l
        elif l == b:
            # User didn't touch; take my disk value.
            result[f] = d
        else:
            # Both changed since baseline: real conflict.
            if f in POSITION_FIELDS:
                result[f] = l  # drag wins for positions
            else:
                result[f] = d  # disk wins for content
    return result


def merge(disk, live, baseline):
    """3-way merge of disk + live against baseline. Returns merged state."""
    if baseline is None:
        baseline = {"blocks": []}

    disk_blocks = {b["id"]: b for b in disk.get("blocks", []) if "id" in b}
    live_blocks = {b["id"]: b for b in live.get("blocks", []) if "id" in b}
    base_blocks = {b["id"]: b for b in baseline.get("blocks", []) if "id" in b}

    all_ids = set(disk_blocks) | set(live_blocks) | set(base_blocks)
    merged_blocks = []
    stats = {
        "kept": 0, "user_deleted": 0, "code_deleted": 0,
        "user_added": 0, "code_added": 0, "merged": 0, "both_deleted": 0,
        "position_drifts": 0, "content_drifts": 0,
    }

    for bid in all_ids:
        in_d = bid in disk_blocks
        in_l = bid in live_blocks
        in_b = bid in base_blocks

        if in_d and in_l and in_b:
            merged_b = merge_block(disk_blocks[bid], live_blocks[bid], base_blocks[bid])
            merged_blocks.append(merged_b)
            stats["merged"] += 1
            for f in POSITION_FIELDS:
                if disk_blocks[bid].get(f) != live_blocks[bid].get(f):
                    stats["position_drifts"] += 1
                    break
            for f in (set(disk_blocks[bid]) | set(live_blocks[bid])) - POSITION_FIELDS - {"id"}:
                if disk_blocks[bid].get(f) != live_blocks[bid].get(f):
                    stats["content_drifts"] += 1
                    break
        elif in_d and in_b and not in_l:
            stats["user_deleted"] += 1                  # user deleted via UI; honor it
        elif in_l and in_b and not in_d:
            stats["code_deleted"] += 1                  # I deleted via state.json; honor it
        elif in_d and in_l and not in_b:
            # Both added the same id (rare) — treat as merged with empty baseline.
            merged_b = merge_block(disk_blocks[bid], live_blocks[bid], None)
            merged_blocks.append(merged_b)
            stats["merged"] += 1
        elif in_d and not in_l and not in_b:
            merged_blocks.append(disk_blocks[bid])
            stats["code_added"] += 1
        elif in_l and not in_d and not in_b:
            merged_blocks.append(live_blocks[bid])
            stats["user_added"] += 1
        elif in_b and not in_d and not in_l:
            stats["both_deleted"] += 1                  # gone everywhere; drop
        else:
            stats["kept"] += 1

    # Top-level merge: same logic, scalar fields
    merged = {}
    top_keys = (set(disk.keys()) | set(live.keys()) | set(baseline.keys())) - {"blocks"}
    for k in top_keys:
        d = disk.get(k)
        l = live.get(k)
        b = baseline.get(k)
        if d == b:
            merged[k] = l
        elif l == b:
            merged[k] = d
        else:
            # Both changed: UI fields → live; structural → disk
            merged[k] = l if k in UI_FIELDS else d
    merged["blocks"] = merged_blocks

    # Print summary
    print(f"  Merge summary:")
    print(f"    blocks merged (3-way):       {stats['merged']}")
    print(f"      with position drift:        {stats['position_drifts']}")
    print(f"      with content drift:         {stats['content_drifts']}")
    print(f"    user UI adds kept:           {stats['user_added']}")
    print(f"    code adds kept:              {stats['code_added']}")
    print(f"    user UI deletes honored:     {stats['user_deleted']}")
    print(f"    code deletes honored:        {stats['code_deleted']}")
    print(f"    both-deleted (dropped):      {stats['both_deleted']}")
    return merged


# ---------- Other helpers ----------

def bump_schema_version(state):
    state["__schema_version"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return state["__schema_version"]


def regenerate_published_state():
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
        fail("Could not find PUBLISHED_STATE in index.html")
    if html_new != html:
        HTML_PATH.write_text(html_new)
        return True
    return False


def load_baseline():
    if BASELINE_PATH.exists():
        try:
            return json.loads(BASELINE_PATH.read_text())
        except json.JSONDecodeError:
            print("  WARNING: .last_published.json malformed; treating baseline as empty")
    return None


def save_baseline(state):
    BASELINE_PATH.write_text(json.dumps(state, indent=2))


# ---------- Subcommands ----------

def cmd_pull():
    """Refresh state.json + baseline from Supabase. No publish, no git."""
    print("Pulling Supabase live state into state.json + baseline…")
    live = fetch_supabase_state()
    if live is None:
        fail("Supabase row missing or fetch failed; aborting.")
    STATE_PATH.write_text(json.dumps(live, indent=2))
    save_baseline(live)
    print(f"  Done. state.json + .last_published.json now reflect Supabase.")
    print(f"  Block count: {len(live.get('blocks', []))}")
    print(f"  Schema version: {live.get('__schema_version')}")


def cmd_publish(msg):
    if not STATE_PATH.exists() or not HTML_PATH.exists():
        fail("Run from repo root (state.json and index.html missing).")

    print("1) Pulling Supabase live state…")
    live = fetch_supabase_state()
    if live is None:
        print("  Supabase empty/unreachable — using disk as live.")
        live = json.loads(STATE_PATH.read_text())

    disk = json.loads(STATE_PATH.read_text())
    baseline = load_baseline()
    if baseline is None:
        print("  No baseline found — first publish, treating disk as baseline.")
        baseline = disk

    print("2) 3-way merging disk + live against baseline…")
    merged = merge(disk, live, baseline)

    print("3) Bumping __schema_version + regenerating PUBLISHED_STATE…")
    new_version = bump_schema_version(merged)
    print(f"   __schema_version -> {new_version}")
    STATE_PATH.write_text(json.dumps(merged, indent=2))
    changed = regenerate_published_state()
    print(f"   index.html {'updated' if changed else 'unchanged'}")

    print("4) Snapshotting baseline for next publish (before commit so it lands in git)…")
    save_baseline(merged)
    print(f"   .last_published.json updated.")

    print("5) Staging + committing…")
    # Stage state.json, index.html, .last_published.json — and publish.py
    # if the user has edited the script itself. We pass --update to limit
    # to already-tracked files (won't pick up screenshots, .claude/, etc.).
    run(["git", "add", "state.json", "index.html", ".last_published.json", "publish.py"])
    diff = run(["git", "diff", "--cached", "--name-only"])
    if not diff:
        print("   nothing to commit")
    else:
        run(["git", "commit", "-m", msg])
        print(f"   committed: {msg}")

    print("6) Pushing to GitHub…")
    out = run(["git", "push"])
    print(f"   {out or 'up to date'}")

    print("7) Pushing merged state to Supabase…")
    upsert_supabase(merged)

    print("\nDone. Browsers see the new schema version on next load (or sooner via realtime).")


def main():
    args = sys.argv[1:]
    if not args:
        fail('Usage: publish.py "commit message"   OR   publish.py --pull')
    if args[0] == "--pull":
        cmd_pull()
    else:
        msg = args[0].strip()
        if not msg:
            fail('Need a commit message.')
        cmd_publish(msg)


if __name__ == "__main__":
    main()
