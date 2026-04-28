#!/usr/bin/env python3
"""Wire each G7+G8 shared math block (the ones with tag ending in -78
that I added in rebuild_g78_math.py) to depend on the most recent prior
math session in BOTH the G7-only and G8-only chains.

Inference: a G7+G8 shared math block depends on whatever each cohort
was doing immediately beforehand. For G7 students, that's the latest
G7-only math tag in time before this slot. For G8, the latest G8-only.
The block requires both prereqs to be satisfied.

This is mechanical — easy to revise if John has a more nuanced view.
"""

import json, re, datetime

REPO = '/Users/ktusiime/Desktop/DLA/Forge/Code/CohortCalendar'

def main():
    state = json.load(open(f'{REPO}/state.json'))
    blocks = state['blocks']

    def slot_key(b): return (b['w'], b['d'], b['s'])

    g7_only = sorted([b for b in blocks
                      if b.get('tp') == 'math' and b.get('grades') == ['G7']
                      and b.get('tag')], key=slot_key)
    g8_only = sorted([b for b in blocks
                      if b.get('tp') == 'math' and b.get('grades') == ['G8']
                      and b.get('tag')], key=slot_key)

    # G7+G8 shared math blocks I added (tag ends in -78). Skip the auto-
    # mirrored ones — those come from the JS mirror block at runtime, not
    # from state. We're targeting only the new-style shared tags I minted
    # like "cost-model-build-full-assembly-78" that aren't auto-mirrored.
    src = open(f'{REPO}/index.html').read()
    pre_match = re.search(r'const PREREQS = (\{.*?\});', src, re.DOTALL)
    pre = json.loads(pre_match.group(1))

    g78_shared = [b for b in blocks
                  if b.get('tp') == 'math'
                  and sorted(b.get('grades', [])) == ['G7', 'G8']
                  and b.get('tag') and b['tag'].endswith('-78')
                  and b['tag'] not in pre]   # skip ones that already have prereqs

    print(f'G7-only math blocks: {len(g7_only)}')
    print(f'G8-only math blocks: {len(g8_only)}')
    print(f'G7+G8 shared math blocks needing prereqs: {len(g78_shared)}\n')

    new_entries = {}
    for sb in sorted(g78_shared, key=slot_key):
        sk = slot_key(sb)
        prior_g7 = max((b for b in g7_only if slot_key(b) < sk),
                       key=slot_key, default=None)
        prior_g8 = max((b for b in g8_only if slot_key(b) < sk),
                       key=slot_key, default=None)
        prereqs = []
        if prior_g7: prereqs.append(prior_g7['tag'])
        if prior_g8 and prior_g8['tag'] not in prereqs:
            prereqs.append(prior_g8['tag'])
        if not prereqs:
            prereqs = ['math-diagnostic']
        new_entries[sb['tag']] = prereqs
        print(f'  Wk{sb["w"]} d{sb["d"]} s={sb["s"]:>3}  {sb["tag"]}')
        print(f'    ← {prereqs}')

    if not new_entries:
        print('Nothing to wire. Exiting.')
        return

    pre.update(new_entries)
    pre_compact = json.dumps(pre, separators=(', ', ': '))

    # Use string replacement instead of re.sub to avoid \u escape issues.
    pre_start = pre_match.start()
    pre_end = pre_match.end()
    src = src[:pre_start] + f'const PREREQS = {pre_compact};' + src[pre_end:]

    # Bump schema version + sync PUBLISHED_STATE
    state['__schema_version'] = datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    json.dump(state, open(f'{REPO}/state.json', 'w'), indent=2)

    new_state_json = json.dumps(state, separators=(', ', ': '))
    start = src.find('const PUBLISHED_STATE = ')
    end = src.find('};', start) + 2
    src = src[:start] + f'const PUBLISHED_STATE = {new_state_json};' + src[end:]

    open(f'{REPO}/index.html', 'w').write(src)
    print(f'\nWired {len(new_entries)} entries.')
    print(f'__schema_version = {state["__schema_version"]}')

if __name__ == '__main__':
    main()
