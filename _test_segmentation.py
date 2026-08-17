"""Functional tests for the mesh segmentation implementation."""
import os, types, math
os.chdir('/Users/abrown/Documents/makerbotGcodeBridge')

mod = types.ModuleType('converter')
mod.__file__ = os.path.abspath('converter.py')
src = open('converter.py').read().split('if __name__')[0]
exec(compile(src, 'converter.py', 'exec'), mod.__dict__)

parse = mod.parse_g1_command
lkv   = mod.last_known_values

# Flat mesh (+0.1 everywhere, fade_height=20 so fade~0.99 at z=0.2)
mesh = {
    'probe_x': [-200.0, 200.0],
    'probe_y': [-200.0, 200.0],
    'offsets': [[0.1, 0.1], [0.1, 0.1]],
    'fade_height': 20.0,
}

# ── Test 1: short move (<= 10 mm) → single dict ─────────────────────────────
lkv.update({'x': 0, 'y': 0, 'z': 0.2, 'a': 0, 'feedrate': 40})
r = parse('G1 X5 Y5 A0.5 F2400', mesh=mesh)
assert isinstance(r, dict), f'Expected dict, got {type(r)}'
print(f'PASS  short 7mm move   → single dict   z={r["command"]["parameters"]["z"]}')

# ── Test 2: long move (50 mm) → 5 × 10 mm segments ─────────────────────────
lkv.update({'x': 0, 'y': 0, 'z': 0.2, 'a': 0, 'feedrate': 40})
r = parse('G1 X50 Y0 A2.5 F2400', mesh=mesh)
assert isinstance(r, list), f'Expected list, got {type(r)}'
assert len(r) == 5, f'Expected 5 segments, got {len(r)}'
xs = [c['command']['parameters']['x'] for c in r]
zs = [c['command']['parameters']['z'] for c in r]
print(f'PASS  long  50mm move  → {len(r)} segments  x={xs}')
print(f'       z values (mesh-compensated): {zs}')

# ── Test 3: pure Z move → single dict ───────────────────────────────────────
lkv.update({'x': 10, 'y': 10, 'z': 0.2, 'a': 0, 'feedrate': 40})
r = parse('G1 Z0.4', mesh=mesh)
assert isinstance(r, dict), f'Expected dict, got {type(r)}'
print(f'PASS  pure Z move      → single dict   z={r["command"]["parameters"]["z"]}')

# ── Test 4: no mesh → single dict, z unchanged ──────────────────────────────
lkv.update({'x': 0, 'y': 0, 'z': 0.2, 'a': 0, 'feedrate': 40})
r = parse('G1 X100 Y0 A5', mesh=None)
assert isinstance(r, dict)
assert r['command']['parameters']['z'] == 0.2
print('PASS  no mesh (100mm)  → single dict   z=0.2 (no compensation)')

# ── Test 5: pure retraction → single dict ───────────────────────────────────
lkv.update({'x': 50, 'y': 50, 'z': 0.2, 'a': 5.0, 'feedrate': 40})
r = parse('G1 A4.5 F2400', mesh=mesh)   # retract 0.5mm
assert isinstance(r, dict)
print(f'PASS  retraction only  → single dict   a={r["command"]["parameters"]["a"]}')

# ── Test 6: exact segment count for various lengths ──────────────────────────
cases = [(10, 1), (10.001, 2), (20, 2), (25, 3), (100, 10), (105, 11)]
for length, expected in cases:
    lkv.update({'x': 0, 'y': 0, 'z': 0.2, 'a': 0, 'feedrate': 40})
    r = parse(f'G1 X{length} Y0', mesh=mesh)
    actual = 1 if isinstance(r, dict) else len(r)
    assert actual == expected, f'length={length} → {actual} segs, expected {expected}'
print(f'PASS  segment counts correct for lengths: {[c[0] for c in cases]}')

# ── Test 7: A axis interpolated correctly along segments ─────────────────────
lkv.update({'x': 0, 'y': 0, 'z': 0.2, 'a': 0, 'feedrate': 40})
r = parse('G1 X50 Y0 A5.0', mesh=mesh)   # 5 segments, A should go 1,2,3,4,5
a_vals = [round(c['command']['parameters']['a'], 4) for c in r]
assert a_vals == [1.0, 2.0, 3.0, 4.0, 5.0], f'A interpolation wrong: {a_vals}'
print(f'PASS  A-axis interpolation along 5 segments: {a_vals}')

print('\nAll tests passed.')
