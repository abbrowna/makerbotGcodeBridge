import socket
import ssl
import json
import time
import zlib
from pathlib import Path

# Hardcoded configuration
PRINTER_IP = "172.16.11.3"
PRINTER_PORT = 9999
SSL_PORT = 12309
PRINT_FILE = Path("/Users/abrown/Downloads/smallprint.makerbot")
BLOCK_SIZE = 32768  # From capture


class MakerbotPrinter:
    """MakerBot printer client for sending prints"""
    
    def __init__(self, ip: str = PRINTER_IP, port: int = PRINTER_PORT):
        self.ip = ip
        self.port = port
        self.ssl_port = SSL_PORT
        self._request_id = 0
        self._socket = None
    
    def _next_id(self) -> int:
        current = self._request_id
        self._request_id += 1
        return current
    
    def _send_rpc(self, method: str, params: dict = None, expect_response: bool = True) -> dict:
        """Send JSON-RPC request and optionally receive response"""
        request = {
            "id": self._next_id(),
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        message = json.dumps(request).encode('utf-8')
        print(f"  → {method}: {json.dumps(params or {})[:60]}...")
        self._socket.sendall(message)
        
        if not expect_response:
            return {}
        
        return self._recv_response(request['id'])
    
    def _recv_response(self, expected_id: int, timeout: float = 30) -> dict:
        """Receive response for a specific request ID, skipping notifications"""
        self._socket.settimeout(timeout)
        buffer = b""
        
        while True:
            try:
                chunk = self._socket.recv(8192)
                if not chunk:
                    raise ConnectionError("Connection closed")
                buffer += chunk
                
                # Try to parse JSON objects from buffer
                decoded = buffer.decode('utf-8', errors='replace')
                
                # Find complete JSON objects
                depth = 0
                start = 0
                i = 0
                while i < len(decoded):
                    char = decoded[i]
                    if char == '{':
                        if depth == 0:
                            start = i
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0:
                            json_str = decoded[start:i+1]
                            try:
                                obj = json.loads(json_str)
                                
                                if 'id' in obj and obj['id'] == expected_id:
                                    if 'error' in obj:
                                        print(f"  ← ERROR: {obj['error']}")
                                    else:
                                        result_str = json.dumps(obj.get('result'))
                                        print(f"  ← OK: {result_str[:60]}...")
                                    return obj
                                elif 'method' in obj:
                                    # Notification - skip
                                    pass
                                
                                # Remove parsed portion
                                buffer = decoded[i+1:].encode('utf-8')
                                decoded = buffer.decode('utf-8', errors='replace')
                                i = -1  # Reset index
                                
                            except json.JSONDecodeError:
                                pass
                    i += 1
                    
            except socket.timeout:
                raise TimeoutError(f"Timeout waiting for response {expected_id}")
    
    def _create_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    
    def _fresh_authorize(self) -> str:
        """Perform fresh TLS authorization and return token"""
        print("\n[1/7] Authorizing (press button on printer)...")
        
        ctx = self._create_ssl_context()
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(60)
        
        try:
            raw_sock.connect((self.ip, self.ssl_port))
            sock = ctx.wrap_socket(raw_sock, server_hostname=self.ip)
            
            request = {
                "id": 0,
                "jsonrpc": "2.0",
                "method": "authorize",
                "params": {
                    "makerbot_token": None,
                    "username": "ANON",
                    "local_secret": "undefined"
                }
            }
            sock.sendall(json.dumps(request).encode('utf-8'))
            
            print("  ⚠️  Press the button on the printer to authorize...")
            
            data = sock.recv(8192)
            response = json.loads(data.decode('utf-8'))
            
            token = response.get('result', {}).get('one_time_token', '')
            if token:
                print(f"  ✓ Got token: {token}")
            else:
                raise RuntimeError(f"No token in response: {response}")
            
            return token
            
        finally:
            try:
                sock.close()
            except:
                pass
            raw_sock.close()
    
    def _connect_and_auth(self, token: str):
        """Connect TCP and authenticate"""
        print("\n[2/7] Connecting and authenticating...")
        
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(30)
        self._socket.connect((self.ip, self.port))
        
        # Handshake
        response = self._send_rpc("handshake")
        self.ssl_port = int(response.get('result', {}).get('ssl_port', SSL_PORT))
        
        # Authenticate
        response = self._send_rpc("authenticate", {"access_token": token})
        if 'error' in response:
            raise RuntimeError(f"Authentication failed: {response['error']}")
        
        print("  ✓ Authenticated")
    
    def _print_init(self, filename: str):
        """Initialize print job with transfer_wait"""
        print("\n[3/7] Initializing print job...")
        
        response = self._send_rpc("print", {
            "filepath": filename,
            "transfer_wait": True
        })
        
        if 'error' in response:
            raise RuntimeError(f"print init failed: {response['error']}")
        
        print(f"  ✓ Print job initialized for: {filename}")
    
    def _put_init(self, file_path: Path) -> tuple[str, int]:
        """Initialize file transfer"""
        print("\n[4/7] Initializing file transfer...")
        
        file_size = file_path.stat().st_size
        file_id = "1"  # Simple file ID as seen in capture
        remote_path = f"/current_thing/{file_path.name}"
        
        print(f"  File: {file_path.name}")
        print(f"  Size: {file_size:,} bytes")
        print(f"  Block size: {BLOCK_SIZE:,} bytes")
        print(f"  Remote: {remote_path}")
        
        response = self._send_rpc("put_init", {
            "block_size": BLOCK_SIZE,
            "file_id": file_id,
            "file_path": remote_path,
            "length": file_size
        })
        
        if 'error' in response:
            raise RuntimeError(f"put_init failed: {response['error']}")
        
        print(f"  ✓ Transfer initialized")
        return file_id, file_size
    
    def _put_raw_chunks(self, file_path: Path, file_id: str) -> int:
        """Send file in chunks using put_raw"""
        print("\n[5/7] Transferring file data...")
        
        file_size = file_path.stat().st_size
        bytes_sent = 0
        crc = 0
        
        with open(file_path, 'rb') as f:
            while bytes_sent < file_size:
                # Read chunk
                chunk = f.read(BLOCK_SIZE)
                chunk_len = len(chunk)
                
                # Update CRC
                crc = zlib.crc32(chunk, crc)
                
                # Send put_raw command
                response = self._send_rpc("put_raw", {
                    "file_id": file_id,
                    "length": chunk_len
                })
                
                if 'error' in response:
                    raise RuntimeError(f"put_raw failed: {response['error']}")
                
                # Send raw binary data immediately after
                self._socket.sendall(chunk)
                
                bytes_sent += chunk_len
                progress = (bytes_sent / file_size) * 100
                print(f"\r  Progress: {progress:.1f}% ({bytes_sent:,}/{file_size:,} bytes)", end="", flush=True)
        
        print()  # New line
        print(f"  ✓ File data sent")
        
        # Return unsigned CRC32
        return crc & 0xffffffff
    
    def _put_term(self, file_id: str, file_size: int, crc: int):
        """Terminate file transfer"""
        print("\n[6/7] Finalizing transfer...")
        
        print(f"  CRC32: {crc}")
        
        response = self._send_rpc("put_term", {
            "crc": crc,
            "file_id": file_id,
            "length": file_size
        })
        
        if 'error' in response:
            raise RuntimeError(f"put_term failed: {response['error']}")
        
        print(f"  ✓ Transfer finalized")
    
    def _process_method(self):
        """Signal that build plate is cleared and start print"""
        print("\n[7/7] Starting print...")
        
        response = self._send_rpc("process_method", {
            "method": "build_plate_cleared"
        })
        
        if 'error' in response:
            raise RuntimeError(f"process_method failed: {response['error']}")
        
        print(f"  ✓ Print started!")
    
    def send_print(self, file_path: Path = PRINT_FILE):
        """Complete workflow to send a print"""
        print("=" * 60)
        print("MakerBot Print Transfer")
        print("=" * 60)
        print(f"Printer: {self.ip}:{self.port}")
        print(f"File: {file_path}")
        print(f"Size: {file_path.stat().st_size:,} bytes")
        print("=" * 60)
        
        try:
            # Step 1: Fresh authorization
            token = self._fresh_authorize()
            
            # Step 2: Connect and authenticate
            self._connect_and_auth(token)
            
            # Step 3: Initialize print job
            self._print_init(file_path.name)
            
            # Step 4: Initialize file transfer
            file_id, file_size = self._put_init(file_path)
            
            # Step 5: Send file chunks
            crc = self._put_raw_chunks(file_path, file_id)
            
            # Step 6: Finalize transfer
            self._put_term(file_id, file_size, crc)
            
            # Step 7: Start print
            self._process_method()
            
            print("\n" + "=" * 60)
            print("✓ SUCCESS! Print job sent and started.")
            print("=" * 60)
            
        except Exception as e:
            print(f"\n*** Error: {e} ***")
            import traceback
            traceback.print_exc()
            raise
            
        finally:
            if self._socket:
                self._socket.close()
                self._socket = None


def main():
    # Verify file exists
    if not PRINT_FILE.exists():
        print(f"ERROR: Print file not found: {PRINT_FILE}")
        return
    
    printer = MakerbotPrinter()
    printer.send_print(PRINT_FILE)


if __name__ == "__main__":
    main()