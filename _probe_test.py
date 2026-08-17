"""
Quick diagnostic: authenticate to the printer and dump what
get_system_information actually returns (especially current_process).
"""
import json, socket, ssl, time

IP       = "172.16.11.6"
PORT     = 9999
SSL_PORT = 12309

# ── helpers ──────────────────────────────────────────────────────────────────
def find_json_end(s):
    depth = 0; in_str = False; esc = False
    for i, ch in enumerate(s):
        if esc:        esc = False; continue
        if ch == '\\' and in_str: esc = True; continue
        if ch == '"':  in_str = not in_str; continue
        if in_str:     continue
        if ch == '{':  depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0: return i
    return None

class Printer:
    def __init__(self, ip, port, ssl_port):
        self.ip = ip; self.port = port; self.ssl_port = ssl_port
        self._sock = None; self._buf = b""; self._id = 0

    def _next_id(self):
        self._id += 1; return self._id

    def authorize(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        raw = socket.socket(); raw.settimeout(60)
        raw.connect((self.ip, self.ssl_port))
        s = ctx.wrap_socket(raw, server_hostname=self.ip)
        req = json.dumps({"id":0,"jsonrpc":"2.0","method":"authorize",
            "params":{"makerbot_token":None,"username":"ANON","local_secret":"undefined"}})
        s.sendall(req.encode())
        print("Waiting for button press…", flush=True)
        data = s.recv(8192)
        resp = json.loads(data.decode())
        token = resp["result"]["one_time_token"]
        s.close(); raw.close()
        print(f"Token: {token}", flush=True)
        return token

    def connect(self, token):
        self._sock = socket.socket(); self._sock.settimeout(10)
        self._sock.connect((self.ip, self.port))
        self._buf = b""
        self.rpc("handshake")
        self.rpc("authenticate", {"access_token": token})
        print("Connected + authenticated", flush=True)

    def rpc(self, method, params=None, timeout=10):
        req_id = self._next_id()
        self._sock.sendall(json.dumps(
            {"id": req_id, "jsonrpc":"2.0", "method": method, "params": params or {}}
        ).encode())
        self._sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(8192)
                if chunk: self._buf += chunk
            except socket.timeout:
                pass
            decoded = self._buf.decode("utf-8", errors="replace")
            pos = 0
            while pos < len(decoded):
                start = decoded.find('{', pos)
                if start == -1: break
                end = find_json_end(decoded[start:])
                if end is None: break
                obj_str = decoded[start : start+end+1]
                self._buf = decoded[start+end+1:].encode("utf-8")
                decoded = self._buf.decode("utf-8", errors="replace")
                pos = 0
                try:
                    obj = json.loads(obj_str)
                    if 'method' in obj:
                        print(f"  [notif] {obj['method']}", flush=True)
                        continue
                    if obj.get('id') == req_id:
                        return obj
                except json.JSONDecodeError:
                    pass
        raise TimeoutError(f"No response for {method} within {timeout}s")

    def close(self):
        if self._sock: self._sock.close(); self._sock = None


# ── main ──────────────────────────────────────────────────────────────────────
p = Printer(IP, PORT, SSL_PORT)
token = p.authorize()
p.connect(token)

print("\n─── get_system_information ───")
info = p.rpc("get_system_information", timeout=10)
result = info.get("result", {})
print(f"Top-level keys: {list(result.keys())}")
print(f"\ncurrent_process = {json.dumps(result.get('current_process'), indent=2)}")
print(f"\nFull result:\n{json.dumps(result, indent=2)}")

p.close()
