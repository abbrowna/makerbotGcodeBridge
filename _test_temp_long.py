"""Long-running test: does set_temperature_target work in the jsontoolpath?"""
import json, socket, ssl, time, zlib, os

IP="172.16.11.6"; PORT=9999; SSL_PORT=12309
SCRIPT_DIR="/Users/abrown/Documents/makerbotGcodeBridge"

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
        print("Press button…",flush=True); d=s.recv(8192); s.close(); r.close()
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
        return th[0].get("current_temperature",0) if th else 0
    def close(self): self._s and self._s.close()

p=P()
tok=p.auth(); p.connect(tok)

makerbot_path=os.path.join(SCRIPT_DIR,"temp_test_B.makerbot")
fname="temp_test_B.makerbot"
fsize=os.path.getsize(makerbot_path)
BLOCK=32768

p.rpc("print",{"filepath":fname,"transfer_wait":True},t=10)
p.rpc("process_method",{"method":"build_plate_cleared"},t=10)
p.rpc("put_init",{"block_size":BLOCK,"file_id":"1","file_path":f"/current_thing/{fname}","length":fsize},t=10)
crc=0; sent=0
with open(makerbot_path,'rb') as f:
    while sent < fsize:
        chunk=f.read(BLOCK)
        p.rpc("put_raw",{"file_id":"1","length":len(chunk)},t=30)
        p._s.sendall(chunk); crc=zlib.crc32(chunk,crc); sent+=len(chunk)
p.rpc("put_term",{"crc":crc&0xffffffff,"file_id":"1","length":fsize},t=10)
print("File sent. Monitoring (watching for drop to 100 C after reaching 225 C)…\n")

reached_print_temp=False
post_count=0
prev=None
for i in range(180):  # 6 minutes
    t=p.get_temp()
    marker=""
    if (not reached_print_temp) and t >= 225:
        reached_print_temp=True
        marker=" <-- PRINT STARTED at print temp"
    if reached_print_temp:
        post_count+=1
        if t <= 120:
            marker=" <-- TEMPERATURE DROPPED! set_temperature_target WORKS!"
        elif t >= 225 and post_count > 5:
            marker=" <-- still at print temp"
    if t != prev or marker:
        print(f"  [{time.strftime('%H:%M:%S')}]  {t}C{marker}",flush=True)
        prev=t
    if reached_print_temp and post_count >= 40:
        print("\nWatched 80s after print started")
        break
    time.sleep(2)

try: p.rpc("cancel",t=5)
except: pass
p.close()
print("Done.")
