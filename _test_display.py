import time, sys
sys.path.insert(0, '/Users/abrown/Documents/makerbotGcodeBridge')

# Pull only the display_mesh function by running the module up to main
import importlib.util, types

# Build a minimal stub so converter.py can be imported without side effects
import unittest.mock as mock
with mock.patch('builtins.__import__', side_effect=lambda name, *a, **kw: mock.MagicMock() if name in ('pync','zeroconf') else __import__(name, *a, **kw)):
    spec = importlib.util.spec_from_file_location("conv", "/Users/abrown/Documents/makerbotGcodeBridge/converter.py")
    # Don't actually exec the full module — just extract display_mesh manually

# Just call it directly
exec(open("/Users/abrown/Documents/makerbotGcodeBridge/converter.py").read().split("def display_mesh")[0].split("\n")[-1])  # noop

import json

SRC = open("/Users/abrown/Documents/makerbotGcodeBridge/converter.py").read()
# Extract the function source
start = SRC.index("def display_mesh")
# Find the next top-level def/class after it
import re
tail = SRC[start:]
m = re.search(r'\ndef [a-z]|\nclass [a-z]', tail[1:])
func_src = tail[:m.start()+1] if m else tail

globs = {"time": time, "FADE_HEIGHT_MM": 10.0}
exec(func_src, globs)

mesh = {
    'probe_x': [-120.0, -60.0, 0.0, 60.0, 120.0],
    'probe_y': [-66.0, 0.0, 66.0],
    'offsets': [
        [+0.042, +0.018, -0.011, +0.003, +0.027],
        [+0.009, -0.004, -0.022, -0.008, +0.011],
        [-0.031, -0.019, -0.044, -0.028, -0.017],
    ],
    'fade_height': 10.0,
    'timestamp': time.time() - 1800,
}

globs['display_mesh'](mesh)
