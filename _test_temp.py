"""
Test: probe which temperature command names actually work in the jsontoolpath.

Strategy:
 1. Idle RPC test: machine_action_command("set_temperature_target", ...) — confirm
    the raw pymachine call heats the extruder.
 2. Toolpath test A: create a tiny .makerbot with "set_toolhead_temperature" (old/wrong)
    and "set_temperature_target" (correct pymachine name).  Monitor temp to see
    which one causes the extruder to change temperature during the print.
"""
import json, socket, ssl, time, zipfile, os, sys

IP = "172.16.11.6"; PORT = 9999; SSL_PORT = 12309
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── helpers ──────────────────────────────────────────────────────────────────
def find_end(s):
    d=0; ins=False; esc=False
    for i,ch in enumerate(s):
        if esc: esc=False; continue
        if ch=='\\' and ins: esc=True; continue
        if ch=='"': ins=not ins; continue
        if ins: continue
        if ch=='{': d+=1
        elif ch=='}':
            d-=1
            if d==0: return i
    return None

class P:
    def __init__(self): self._s=None; self._buf=b""; self._n=0
    def auth(self):
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
        r=socket.socket(); r.settimeout(60); r.connect((IP,SSL_PORT))
        s=ctx.wrap_socket(r,server_hostname=IP)
        s.sendall(json.dumps({"id":0,"jsonrpc":"2.0","method":"authorize","params":{"makerbot_token":None,"username":"ANON","local_secret":"undefined"}}).encode())
        print("Press button on printer…",flush=True); d=s.recv(8192); s.close(); r.close()
        return json.loads(d)["result"]["one_time_token"]
    def connect(self,tok):
        self._s=socket.socket(); self._s.settimeout(15); self._s.connect((IP,PORT)); self._buf=b""
        self.rpc("handshake"); self.rpc("authenticate",{"access_token":tok}); print("Connected",flush=True)
    def rpc(self,m,p=None,t=15):
        self._n+=1; rid=self._n
        self._s.sendall(json.dumps({"id":rid,"jsonrpc":"2.0","method":m,"params":p or {}}).encode())
        self._s.settimeout(t); dl=time.monotonic()+t
        while time.monotonic()<dl:
            try: c=self._s.recv(8192); self._buf+=c
            except: pass
            dec=self._buf.decode("utf-8","replace"); pos=0
            while pos<len(dec):
                st=dec.find('{',pos)
                if st<0: break
                en=find_end(dec[st:])
                if en is None: break
                seg=dec[st:st+en+1]; self._buf=dec[st+en+1:].encode(); dec=self._buf.decode("utf-8","replace"); pos=0
                try:
                    obj=json.loads(seg)
                    if obj.get('id')==rid: return obj
                except: pass
        raise TimeoutError(f"{m} timed out")
    def get_temp(self):
        r=self.rpc("get_system_information",t=5)
        th=r.get("result",{}).get("toolheads",{}).get("extruder",[{}])
        return th[0].get("current_temperature","?") if th else "?"
    def close(self): self._s and self._s.close()

def make_makerbot(toolpath_commands, filename):
    """Create a tiny .makerbot archive with the given toolpath commands."""
    meta = {
        "bot_type": "replicator_b",
        "bounding_box": {"x_min": -10.0,"x_max": 10.0,"y_min": -10.0,"y_max": 10.0,"z_min": 0.0,"z_max": 5.0},
        "commanded_duration_s": 120, "duration_s": 120,
        "extruder_temperature": 230, "extruder_temperatures": [230],
        "extrusion_distance_mm": 100.0, "extrusion_distances_mm": [100.0],
        "extrusion_mass_g": 0.3, "extrusion_masses_g": [0.3],
        "material": "pla", "materials": ["pla"],
        "tool_type": "mk13_impla", "tool_types": ["mk13_impla"],
        "uuid": "00000000-0000-0000-0000-000000000001",
        "version": "3.0.0"
    }
    toolpath = json.dumps(toolpath_commands, separators=(',',':'))
    path = os.path.join(SCRIPT_DIR, filename)
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr("meta.json", json.dumps(meta, indent=2))
        z.writestr("print.jsontoolpath", toolpath)
    print(f"Created {filename} ({os.path.getsize(path)} bytes)")
    return path

def slow_move(x, y, z=0.2, a=0.0, feed=5.0):
    return {"command":{"function":"move","metadata":{"relative":{"x":False,"y":False,"z":False,"a":False}},"parameters":{"x":x,"y":y,"z":z,"a":a,"feedrate":feed},"tags":["Move"]}}

def temp_cmd(fname, temp):
    return {"command":{"function":fname,"metadata":{},"parameters":{"index":0,"temperature":temp},"tags":[]}}

def load_temp_cmd(temp):
    return {"command":{"function":"load_temperature_settings","metadata":{},"parameters":{"active_temperatures":[temp]},"tags":[]}}

def heat_cmd():
    return {"command":{"function":"heat","metadata":{},"parameters":{},"tags":[]}}

# ─────────────────────────────────────────────────────────────────────────────
# Build test toolpaths
# Start.json heats extruder to 230°C (from meta.json extruder_temperature).
# The toolpath then tries to change to 100°C using different command names.
# We monitor temperature during printing to see which approach works.
# ─────────────────────────────────────────────────────────────────────────────
TEST_TARGET = 100  # target temp — far enough from 230 to notice clearly

test_a = [
    slow_move(-50, 0, 0.2, 0.0, 5.0),            # slow move at start temp (230)
    temp_cmd("set_toolhead_temperature", TEST_TARGET),   # OLD name (likely ignored)
    slow_move(50, 0, 0.2, 1.0, 5.0),              # slow move — does temp change?
    temp_cmd("set_toolhead_temperature", 0),
]

test_b = [
    slow_move(-50, 0, 0.2, 0.0, 5.0),
    temp_cmd("set_temperature_target", TEST_TARGET),     # CORRECT pymachine name
    slow_move(50, 0, 0.2, 1.0, 5.0),
    temp_cmd("set_temperature_target", 0),
]

test_c = [
    slow_move(-50, 0, 0.2, 0.0, 5.0),
    load_temp_cmd(TEST_TARGET),                    # load_temperature_settings + heat
    heat_cmd(),
    slow_move(50, 0, 0.2, 1.0, 5.0),
    load_temp_cmd(0), heat_cmd(),
]

# ─────────────────────────────────────────────────────────────────────────────
p = P()
tok = p.auth()
p.connect(tok)

print(f"\nCurrent extruder temp: {p.get_temp()}°C\n")

# Choose which test to run from command line
test_name = sys.argv[1] if len(sys.argv) > 1 else 'b'
tests = {'a': (test_a, "temp_test_A.makerbot"),
         'b': (test_b, "temp_test_B.makerbot"),
         'c': (test_c, "temp_test_C.makerbot")}

if test_name not in tests:
    print("Usage: python _test_temp.py [a|b|c]"); sys.exit(1)

cmds, fname = tests[test_name]
makerbot_path = make_makerbot(cmds, fname)

print(f"\nTest {test_name.upper()}: using {'set_toolhead_temperature' if test_name=='a' else 'set_temperature_target' if test_name=='b' else 'load_temperature_settings+heat'}")
print(f"Sending print… temp should drop from 230 to {TEST_TARGET}°C mid-print if command works\n")

# Send the print
BLOCK = 32768
p.rpc("print",{"filepath":os.path.basename(makerbot_path),"transfer_wait":True},t=10)
p.rpc("process_method",{"method":"build_plate_cleared"},t=10)
fsize = os.path.getsize(makerbot_path)
resp = p.rpc("put_init",{"block_size":BLOCK,"file_id":"1","file_path":f"/current_thing/{fname}","length":fsize},t=10)
import zlib; crc=0; sent=0
with open(makerbot_path,'rb') as f:
    while sent < fsize:
        chunk = f.read(BLOCK)
        p.rpc("put_raw",{"file_id":"1","length":len(chunk)},t=30)
        p._s.sendall(chunk); crc=zlib.crc32(chunk,crc); sent+=len(chunk)
p.rpc("put_term",{"crc":crc&0xffffffff,"file_id":"1","length":fsize},t=10)
print("File sent. Monitoring temperature for 90 s…\n")

# Monitor temperature
prev = None
for _ in range(45):
    t = p.get_temp()
    if t != prev:
        print(f"  [{time.strftime('%H:%M:%S')}]  {t}°C", flush=True)
        prev = t
    time.sleep(2)

# Cancel print
try: p.rpc("cancel",t=5)
except: pass
p.close()
print("\nDone.")
