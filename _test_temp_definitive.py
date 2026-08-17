"""
Definitive mid-print temperature test.

Toolpath (start temp = 215°C from meta.json):
  [1] slow move 40s  — should stay at 215°C
  [2] set_temperature_target(230)          — Approach A: direct pymachine name
  [3] slow move 40s  — should RISE to 230°C if Approach A works
  [4] load_temperature_settings([200]) + heat()  — Approach B: two-step
  [5] slow move 40s  — should DROP to 200°C if Approach B works
  [6] set_temperature_target(0)            — cool down at end

Monitor: timestamp + temp every 2s with annotations showing expected changes.
"""
import json, socket, ssl, time, zlib, zipfile, os

IP = "172.16.11.6"; PORT = 9999; SSL_PORT = 12309
SCRIPT_DIR = "/Users/abrown/Documents/makerbotGcodeBridge"

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
        self.rpc("handshake"); self.rpc("authenticate",{"access_token":tok}); print("Connected\n",flush=True)
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
        return th[0].get("current_temperature",0) if th else 0
    def close(self): self._s and self._s.close()

# ─────────────────────────────────────────────────────────────────────────────
# Build test .makerbot
# ─────────────────────────────────────────────────────────────────────────────
START_TEMP   = 215   # start.json heats to this via meta.json
TARGET_UP    = 230   # try to RAISE temperature mid-print (faster to detect)
TARGET_DOWN  = 200   # try to LOWER temperature mid-print
MOVE_SPEED   = 3.0   # mm/s — slow moves so timing is predictable

def m(x, y, z=0.2, a=0.0, f=MOVE_SPEED):
    return {"command":{"function":"move","metadata":{"relative":{"x":False,"y":False,"z":False,"a":False}},
                       "parameters":{"x":x,"y":y,"z":z,"a":a,"feedrate":f},"tags":["Move"]}}

def set_temp(t):
    """Approach A: set_temperature_target (correct pymachine name)"""
    return {"command":{"function":"set_temperature_target","metadata":{},
                       "parameters":{"index":0,"temperature":t},"tags":[]}}

def load_temp(t):
    """Approach B step 1: load_temperature_settings"""
    return {"command":{"function":"load_temperature_settings","metadata":{},
                       "parameters":{"active_temperatures":[t]},"tags":[]}}

def heat():
    """Approach B step 2: heat"""
    return {"command":{"function":"heat","metadata":{},"parameters":{},"tags":[]}}

# Distance of each slow segment (at MOVE_SPEED mm/s this gives ~40s per leg)
D = 120.0   # mm per leg → 40 s at 3 mm/s

toolpath = [
    # ── Segment 1: no command, should stay at 215°C ─────────────────────────
    m(-D/2, 0), m(D/2, 0, a=2.0),     # 80s total, stays at 215
    # ── Approach A: set_temperature_target → try to raise to 230°C ──────────
    set_temp(TARGET_UP),
    m(-D/2, 0, a=4.0), m(D/2, 0, a=6.0),   # 80s — watch for rise to 230
    # ── Approach B: load_temperature_settings + heat → try 200°C ─────────────
    load_temp(TARGET_DOWN), heat(),
    m(-D/2, 0, a=8.0), m(D/2, 0, a=10.0),  # 80s — watch for drop to 200
    # ── Cool down ────────────────────────────────────────────────────────────
    set_temp(0),
    m(0, 0, a=10.0),
]

meta = {
    "bot_type": "replicator_b",
    "bounding_box": {"x_min": -70.0,"x_max": 70.0,"y_min": -5.0,"y_max": 5.0,"z_min": 0.0,"z_max": 1.0},
    "commanded_duration_s": 300, "duration_s": 300,
    "extruder_temperature": START_TEMP, "extruder_temperatures": [START_TEMP],
    "extrusion_distance_mm": 200.0, "extrusion_distances_mm": [200.0],
    "extrusion_mass_g": 0.6, "extrusion_masses_g": [0.6],
    "material": "pla", "materials": ["pla"],
    "tool_type": "mk13_impla", "tool_types": ["mk13_impla"],
    "uuid": "00000000-0000-0000-0000-000000000002",
    "version": "3.0.0"
}

fname = "temp_test_definitive.makerbot"
fpath = os.path.join(SCRIPT_DIR, fname)
with zipfile.ZipFile(fpath, 'w') as z:
    z.writestr("meta.json", json.dumps(meta, indent=2))
    z.writestr("print.jsontoolpath", json.dumps(toolpath, separators=(',',':')))
print(f"Created {fname}  ({os.path.getsize(fpath)} bytes)")

# ─────────────────────────────────────────────────────────────────────────────
# Send and monitor
# ─────────────────────────────────────────────────────────────────────────────
p = P()
tok = p.auth(); p.connect(tok)

fsize = os.path.getsize(fpath)
BLOCK = 32768

p.rpc("print",{"filepath":fname,"transfer_wait":True},t=10)
p.rpc("process_method",{"method":"build_plate_cleared"},t=10)
p.rpc("put_init",{"block_size":BLOCK,"file_id":"1","file_path":f"/current_thing/{fname}","length":fsize},t=10)
crc=0; sent=0
with open(fpath,'rb') as f:
    while sent < fsize:
        chunk=f.read(BLOCK); p.rpc("put_raw",{"file_id":"1","length":len(chunk)},t=30)
        p._s.sendall(chunk); crc=zlib.crc32(chunk,crc); sent+=len(chunk)
p.rpc("put_term",{"crc":crc&0xffffffff,"file_id":"1","length":fsize},t=10)
print("File sent. Heating up and monitoring…\n")

print("Expected timeline after reaching 215°C:")
print(f"   0-80s  : STAY at {START_TEMP}°C  (no command)")
print(f"  ~80s    : set_temperature_target({TARGET_UP}) fired  → RISE to {TARGET_UP}°C if Approach A works")
print(f"  80-160s : watch for rise to {TARGET_UP}°C")
print(f" ~160s    : load_temperature_settings({TARGET_DOWN})+heat() fired → DROP to {TARGET_DOWN}°C if Approach B works")
print(f" 160-240s : watch for drop to {TARGET_DOWN}°C\n")

phase = 0
phase_names = ["preheating","seg1 (no cmd)","seg2 (A: +230)","seg3 (B: +200)","done"]
reached_print = False
post_count = 0
prev = None

for _ in range(250):  # ~8 minutes max
    t = p.get_temp()
    marker = ""

    if not reached_print and t >= START_TEMP - 3:
        reached_print = True; phase = 1
        marker = f"  <-- PRINT STARTED  (phase: {phase_names[phase]})"
    elif reached_print:
        post_count += 1
        # Annotate phase transitions
        if phase == 1 and post_count >= 42:   # ~84s
            phase = 2; marker = f"  <-- should have fired set_temperature_target({TARGET_UP})"
        elif phase == 2 and post_count >= 84:  # ~168s
            phase = 3; marker = f"  <-- should have fired load_temperature_settings+heat({TARGET_DOWN})"
        elif phase == 3 and post_count >= 126: # ~252s
            phase = 4; marker = "  <-- end of print"

        # Detect interesting temperature changes
        if phase == 2 and t >= TARGET_UP - 2:
            marker += f"  *** APPROACH A WORKS! rose to {t}°C ***"
        if phase == 3 and t <= TARGET_DOWN + 2:
            marker += f"  *** APPROACH B WORKS! dropped to {t}°C ***"

    if t != prev or marker:
        print(f"  [{time.strftime('%H:%M:%S')}]  {t:3d}°C  (phase: {phase_names[phase]}){marker}", flush=True)
        prev = t

    if phase == 4:
        print("\n--- Test complete ---"); break
    time.sleep(2)

try: p.rpc("cancel",t=5)
except: pass
p.close()

print("\nSummary:")
print("If neither approach showed temperature changes, the extruder firmware")
print("locks temperature once a print starts — only workaround is Z-pause + external control.")
