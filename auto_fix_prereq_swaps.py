#!/usr/bin/env python3
"""Topologically sort blocks within each (grades, duration) lane and
reassign them to slots in dependency order.

For each unique combination of (grades-set, duration):
  - Gather all non-locked blocks with that grades-set and duration.
  - Build a prereq DAG using only edges where both endpoints are blocks
    in this lane (cross-lane prereqs are unfixable by within-lane sort).
  - Kahn's algorithm topologically sorts the block ids.
  - Sort the existing slots chronologically.
  - Assign sorted blocks to sorted slots in topological order.

This handles cascades that pairwise swaps oscillate on (e.g., a chain
of 5 blocks that needs full reorder, or one block that has multiple
prereqs).

Tie-breaking in the topo queue is by current chronological position —
keeps blocks roughly in their existing places when there's no
dependency reason to move them.

Doesn't address:
  - Cross-lane prereqs (e.g., a G5+G6 block depending on a G5+G6+G7+G8
    block — those edges are honored only if they happen to already be
    correct)
  - Cross-week violations (kitchen visit / cost model finalization)
  - Locked blocks (anchors, PINs)
  - Missing-block warnings
  - Proximity warnings
"""

import json, re, datetime
from collections import defaultdict

REPO = '/Users/ktusiime/Desktop/DLA/Forge/Code/CohortCalendar'

PINS = {"PIN-restaurant-meeting","PIN-grocery-visit","PIN-wholesale-visit",
        "PIN-kitchen-visit","PIN-panel-1","PIN-panel-2"}
G56_ONLY = {
    "frac-mult-instruction","frac-mult-practice","frac-mult-recipe-ido","frac-mult-full-recipe",
    "unit-conv-weight","unit-conv-volume","fluency-frac-conv",
    "ratios-concept","ratios-practice","ratios-unitrate","ratios-recipe-ido","ratios-unitrate-recipe",
    "mmm-data","mmm-concept",
    "labor-calc-hourly","labor-calc-unitrate",
}
HAS_GRADE_SUFFIX = lambda t: bool(re.search(r'-(?:7|8|78)$', t))


def build_prereqs():
    src = open(f'{REPO}/index.html').read()
    m = re.search(r'const PREREQS = (\{.*?\});', src, re.DOTALL)
    pre = json.loads(m.group(1))
    suf = lambda t: t if t in PINS else t + '-78'
    for k in list(pre.keys()):
        if k in PINS or k in G56_ONLY or HAS_GRADE_SUFFIX(k): continue
        pre[k + '-78'] = [suf(p) for p in pre[k]
                          if p not in G56_ONLY and not HAS_GRADE_SUFFIX(p)]
    return pre


def find_violations(blocks, pre):
    by_tag = {}
    for b in blocks:
        if b.get('tag'): by_tag.setdefault(b['tag'], []).append(b)
    sk = lambda b: (b['w'], b['d'], b['s'])
    out = []
    for b in blocks:
        tag = b.get('tag')
        if not tag or tag not in pre: continue
        for p_tag in pre[tag]:
            ps = by_tag.get(p_tag, [])
            if not ps: continue
            ep = min(ps, key=sk)
            if (ep['w'], ep['d'], ep['s'] + ep['dur']) > (b['w'], b['d'], b['s']):
                out.append((b, ep))
    return out


def topo_sort_lane(lane_blocks, pre):
    """Kahn's. Tie-break by current chronological position so blocks stay
    near their original places when not constrained by deps."""
    tag_to_blk = {b['tag']: b for b in lane_blocks if b.get('tag')}

    # deps[bid] = set of bids in same lane that must come before bid
    deps = {b['id']: set() for b in lane_blocks}
    for b in lane_blocks:
        tag = b.get('tag')
        if not tag or tag not in pre: continue
        for ptag in pre[tag]:
            if ptag in tag_to_blk and tag_to_blk[ptag]['id'] != b['id']:
                deps[b['id']].add(tag_to_blk[ptag]['id'])

    # rdeps[bid] = blocks whose prereq is bid
    rdeps = defaultdict(set)
    for bid, ds in deps.items():
        for d in ds:
            rdeps[d].add(bid)

    indeg = {bid: len(ds) for bid, ds in deps.items()}
    sk = lambda b: (b['w'], b['d'], b['s'])
    id_to_blk = {b['id']: b for b in lane_blocks}

    ready = sorted([bid for bid, n in indeg.items() if n == 0],
                   key=lambda bid: sk(id_to_blk[bid]))
    out = []
    while ready:
        # Pop the chronologically-earliest among ready (stable tie-break)
        bid = ready.pop(0)
        out.append(bid)
        for cid in sorted(rdeps[bid], key=lambda c: sk(id_to_blk[c])):
            indeg[cid] -= 1
            if indeg[cid] == 0:
                # Insert maintaining chronological order
                ck = sk(id_to_blk[cid])
                inserted = False
                for i, ex in enumerate(ready):
                    if sk(id_to_blk[ex]) > ck:
                        ready.insert(i, cid); inserted = True; break
                if not inserted: ready.append(cid)
    return out if len(out) == len(lane_blocks) else None


def main():
    state = json.load(open(f'{REPO}/state.json'))
    blocks = state['blocks']
    pre = build_prereqs()

    initial_v = len(find_violations(blocks, pre))
    print(f'Initial violations: {initial_v}\n')

    # Group by (grades_set, dur) — only blocks that share both can swap slots
    # cleanly without changing duration.
    groups = defaultdict(list)
    for b in blocks:
        if b.get('locked'): continue
        key = (tuple(sorted(b.get('grades', []))), b.get('dur', 35))
        groups[key].append(b)

    n_reordered_groups = 0
    n_blocks_moved = 0
    for key, lane_blocks in groups.items():
        if len(lane_blocks) < 2: continue
        sorted_ids = topo_sort_lane(lane_blocks, pre)
        if sorted_ids is None:
            print(f'  cycle detected in lane {key}, skipping')
            continue
        # Check if any change is needed
        cur_order = sorted(lane_blocks, key=lambda b: (b['w'], b['d'], b['s']))
        cur_id_order = [b['id'] for b in cur_order]
        if cur_id_order == sorted_ids: continue

        # Reassign positions
        slots = [(b['w'], b['d'], b['s']) for b in cur_order]
        id_to_blk = {b['id']: b for b in lane_blocks}
        for i, bid in enumerate(sorted_ids):
            w, d, s = slots[i]
            blk = id_to_blk[bid]
            if (blk['w'], blk['d'], blk['s']) != (w, d, s):
                blk['w'] = w; blk['d'] = d; blk['s'] = s
                n_blocks_moved += 1
        n_reordered_groups += 1

    print(f'Reordered {n_reordered_groups} lane-groups, moved {n_blocks_moved} blocks')

    remaining = find_violations(blocks, pre)
    print(f'\nViolations: {initial_v} → {len(remaining)}')
    print(f'\nRemaining {len(remaining)} (need manual / curriculum decision):')
    for b, p in remaining:
        cw = '(same week)' if b['w'] == p['w'] else '(CROSS-WEEK)'
        lk = ' [LOCKED]' if (b.get('locked') or p.get('locked')) else ''
        sl = ' [CROSS-LANE]' if sorted(b.get('grades', [])) != sorted(p.get('grades', [])) else ''
        print(f'  {cw}{lk}{sl} "{b["ttl"][:36]}" Wk{b["w"]} d{b["d"]} '
              f'← needs "{p["ttl"][:36]}" Wk{p["w"]} d{p["d"]}')

    # Save
    state['__schema_version'] = datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    json.dump(state, open(f'{REPO}/state.json', 'w'), indent=2)

    src = open(f'{REPO}/index.html').read()
    new_state = json.dumps(state, separators=(', ', ': '))
    s = src.find('const PUBLISHED_STATE = ')
    e = src.find('};', s) + 2
    src = src[:s] + f'const PUBLISHED_STATE = {new_state};' + src[e:]
    open(f'{REPO}/index.html', 'w').write(src)
    print(f'\n__schema_version = {state["__schema_version"]}')


if __name__ == '__main__':
    main()
