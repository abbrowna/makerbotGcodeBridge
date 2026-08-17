"""
Test: park → wait → attempt print(transfer_wait=True) → cancel
Reproduces the ProcessAlreadyRunningException scenario.
"""
import json, socket, ssl, time

IP = "172.16.11.6"; PORT = 9999; SSL_PORT = 12309

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
        print("Press button...",flush=True); d=s.recv(8192); s.close(); r.close()
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
    def wait_idle(self, label="", timeout=30):
        print(f"  Waiting for idle ({label})...", flush=True)
        start=time.monotonic()
        while time.monotonic()-start<timeout:
            r=self.rpc("get_system_information",t=5)
            cp=r.get("result",{}).get("current_process")
            print(f"  [{time.strftime('%H:%M:%S')}] current_process={json.dumps(cp) if cp else 'null'}",flush=True)
            if cp is None: print(f"  -> IDLE after {time.monotonic()-start:.1f}s",flush=True); return True
            if cp.get("complete"): print(f"  -> COMPLETE",flush=True); return True
            time.sleep(0.5)
        print(f"  -> TIMEOUT",flush=True); return False
    def close(self): self._s and self._s.close()

p=P()
tok=p.auth(); p.connect(tok)

# ── 1. Park ──────────────────────────────────────────────────────────────────
print("\n[1] Parking...", flush=True)
r=p.rpc("park")
print(f"  park result: id={r.get('result',{}).get('id')}  complete={r.get('result',{}).get('complete')}  error={r.get('error')}", flush=True)

# ── 2. Wait for park to complete ─────────────────────────────────────────────
p.wait_idle("park")

# ── 3. Simulate .makerbot creation delay (1s) ────────────────────────────────
print("\n[3] Simulating create_makerbot_file (1s sleep)...", flush=True)
time.sleep(1)

# ── 4. set_config (like _set_acceleration) ───────────────────────────────────
print("\n[4] set_config (acceleration)...", flush=True)
r=p.rpc("set_config",{"config":{"acceleration":{"rate_mm_per_s_sq":{"x":400,"y":400,"z":150}}}})
print(f"  result: {r.get('result')}  error: {r.get('error')}", flush=True)

# ── 5. print(transfer_wait=True) ─────────────────────────────────────────────
print("\n[5] print(transfer_wait=True)...", flush=True)
r=p.rpc("print",{"filepath":"test_probe.makerbot","transfer_wait":True},t=10)
print(f"  result: {json.dumps(r.get('result'))}",flush=True)
print(f"  error:  {json.dumps(r.get('error'))}",flush=True)

if r.get('result'):
    # ── 6. Cancel immediately ─────────────────────────────────────────────────
    print("\n[6] Cancelling print process...", flush=True)
    r2=p.rpc("cancel",t=10)
    print(f"  cancel result: {r2}",flush=True)
    p.wait_idle("cancel")
    print("Print successfully started and cancelled - no ProcessAlreadyRunningException!",flush=True)
else:
    print(f"\n!!! print() failed: {r.get('error')} !!!",flush=True)

p.close()
