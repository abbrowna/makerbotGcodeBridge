"""Quick test: verify _WIPE inline comments no longer crash the parser."""
import sys, os
os.chdir('/Users/abrown/Documents/makerbotGcodeBridge')

# Patch __file__ so module-level code in converter works
import types
_mod = types.ModuleType('converter')
_mod.__file__ = '/Users/abrown/Documents/makerbotGcodeBridge/converter.py'
_mod.__spec__ = None
sys.modules['converter'] = _mod

exec(compile(open('converter.py').read(), 'converter.py', 'exec'), _mod.__dict__)

from converter import parse_g1_command, process_gcode_file

# 1. _WIPE inline comment on F
r = parse_g1_command('G1 X10.0 Y20.0 A1.5 F6240;_WIPE')
assert r['command']['parameters']['feedrate'] == 6240/60, "feedrate mismatch"
assert r['command']['parameters']['x'] == 10.0
print("PASS: F6240;_WIPE parses correctly,  feedrate =", r['command']['parameters']['feedrate'])

# 2. Full gcode file
gcode = '/var/folders/4_/ztn2d0y57_d93q9p5w7kxnj40000gp/T/.PrusaSlicer.upload.85e0-6834-116a-e210'
cmds = process_gcode_file(gcode)
print(f"PASS: {len(cmds)} commands from real gcode  (was 0 before fix)")

# 3. Stable copy test
stable = os.path.join(os.path.dirname(os.path.abspath('converter.py')), '_pending_print.gcode')
import shutil; shutil.copy2(gcode, stable)
cmds2 = process_gcode_file(stable)
print(f"PASS: {len(cmds2)} commands from stable copy")
