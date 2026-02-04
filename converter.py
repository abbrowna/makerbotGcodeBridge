import json
import zipfile
import os
import sys
import logging
import re
import socket
import ssl
import time
import zlib
import subprocess
import threading
from pathlib import Path
from pync import Notifier

# Initialize logging
downloads_folder = os.path.join(os.path.expanduser("~"), "Downloads")
log_file = os.path.join(downloads_folder, "makerbot_conversion.log")
logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Log script start
logging.info("Script started")
# Log passed arguments
logging.info(f"Arguments: {sys.argv}")


def notify(title, text):
    """Send macOS notification"""
    Notifier.notify(text, title=title)


# Print transfer constants
PRINTER_PORT = 9999
SSL_PORT = 12309
BLOCK_SIZE = 32768

# Initialize last known values and variables
last_known_values = {
    "x": 0.0,
    "y": 0.0,
    "z": 0.2,
    "a": 0.0,
    "feedrate": 40.0  # Default feedrate in mm/s
}
first_m109_temperature = None
extrusion_distance = None
printing_time = None

# Get the directory where the script is located
script_dir = os.path.dirname(os.path.abspath(__file__))
meta_file_path = os.path.join(script_dir, 'meta.json')


# =============================================================================
# G-code Parsing Functions
# =============================================================================

def parse_g1_command(line):
    global last_known_values
    logging.debug(f"Parsing G1 command: {line}")

    # Parse the parameters from the G1 line
    params = {}
    for part in line.split():
        if part.startswith('X'):
            params['x'] = float(part[1:])
            last_known_values['x'] = params['x']
        elif part.startswith('Y'):
            params['y'] = float(part[1:])
            last_known_values['y'] = params['y']
        elif part.startswith('Z'):
            params['z'] = float(part[1:])
            last_known_values['z'] = params['z']
        elif part.startswith('A'):  # Map A to a
            params['a'] = float(part[1:])
            last_known_values['a'] = params['a']
        elif part.startswith('F'):
            # Convert feedrate from mm/min to mm/s
            params['feedrate'] = float(part[1:]) / 60
            last_known_values['feedrate'] = params['feedrate']

    # Fill in missing axes and feedrate with last known values
    for axis in ['x', 'y', 'z', 'a', 'feedrate']:
        if axis not in params:
            params[axis] = last_known_values[axis]

    return {
        "command": {
            "function": "move",
            "metadata": {
                "relative": {
                    "x": False,
                    "y": False,
                    "z": False,
                    "a": False
                }
            },
            "parameters": params,
            "tags": ["Move"]
        }
    }


def parse_m109_command(line):
    """
    Parses the M109 command to set the extruder temperature and wait.
    Generates a JSON command for print.jsontoolpath and updates meta.json.
    """
    global first_m109_temperature
    logging.debug(f"Parsing M109 command: {line}")

    # Extract the temperature from the M109 line
    for part in line.split():
        if part.startswith('P'):  # P indicates the temperature
            try:
                temp = int(float(part[1:]))  # Truncate to integer
                logging.info(f"Set first_m109_temperature to {temp} from M109")
                
                # Update the first M109 temperature for meta.json
                if first_m109_temperature is None:
                    first_m109_temperature = temp
                
                # Return the JSON command for print.jsontoolpath
                return {
                    "command": {
                        "function": "set_toolhead_temperature",
                        "metadata": {},
                        "parameters": {
                            "index": 0,
                            "temperature": temp
                        },
                        "tags": []
                    }
                }
            except ValueError as e:
                logging.error(f"Failed to parse M109 temperature: {e}")
    return None


def parse_m104_command(line):
    logging.debug(f"Parsing M104 command: {line}")

    # Extract the temperature from the M104 line
    for part in line.split():
        if part.startswith('P'):  # P indicates the temperature
            try:
                temp = int(float(part[1:]))  # Truncate to integer
                logging.info(f"Adding set_toolhead_temperature command with temperature {temp} from M104")
                return {
                    "command": {
                        "function": "set_toolhead_temperature",
                        "metadata": {},
                        "parameters": {
                            "index": 0,
                            "temperature": temp
                        },
                        "tags": []
                    }
                }
            except ValueError as e:
                logging.error(f"Failed to parse M104 temperature: {e}")
    return None


def parse_m106_command(line):
    """
    Parses the M106 command to control the fan.
    Generates JSON commands for turning the fan on and setting the duty cycle.
    """
    logging.debug(f"Parsing M106 command: {line}")

    commands = []

    # Add the "toggle_fan" command
    commands.append({
        "command": {
            "function": "toggle_fan",
            "metadata": {},
            "parameters": {
                "index": 0,
                "value": True
            },
            "tags": []
        }
    })

    # Extract and set the fan duty cycle
    for part in line.split():
        if part.startswith('P'):  # P indicates the duty cycle
            try:
                duty_cycle = float(part[1:]) / 255  # Convert 0-255 to 0-1
                commands.append({
                    "command": {
                        "function": "fan_duty",
                        "metadata": {},
                        "parameters": {
                            "index": 0,
                            "value": duty_cycle
                        },
                        "tags": []
                    }
                })
                logging.info(f"Added fan_duty command with duty cycle {duty_cycle}")
                break
            except ValueError as e:
                logging.error(f"Failed to parse M106 duty cycle: {e}")

    return commands


def extract_gcode_comments(line):
    """
    Extracts specific information from G-code comments, including filament used and printing time.
    """
    global extrusion_distance, printing_time
    logging.debug(f"Processing comment: {line}")

    if line.startswith("; filament used [mm]"):
        try:
            extrusion_distance = float(line.split("=")[1].strip())
            logging.info(f"Set extrusion_distance to {extrusion_distance}")
        except (IndexError, ValueError) as e:
            logging.error(f"Failed to parse filament used: {e}")

    elif line.startswith("; estimated printing time (normal mode)"):
        try:
            # Regex to match "1d 23h 52m 50s", "23h 52m 50s", "52m 50s", or "50s"
            match = re.match(r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", line.split("=")[1].strip())
            if match:
                d = int(match.group(1)) if match.group(1) else 0
                h = int(match.group(2)) if match.group(2) else 0
                m = int(match.group(3)) if match.group(3) else 0
                s = int(match.group(4)) if match.group(4) else 0
                printing_time = d * 86400 + h * 3600 + m * 60 + s
                logging.info(f"Set printing_time to {printing_time} seconds")
            else:
                logging.error(f"Failed to match printing time format: {line}")
        except (IndexError, ValueError) as e:
            logging.error(f"Failed to parse estimated printing time: {e}")
    else:
        logging.debug(f"Comment did not match: {line}")


def parse_line(line):
    """
    Parses a single line of G-code.
    Delegates to the appropriate handler based on the command type.
    """
    line = line.strip()
    if line.startswith(";"):
        # Handle comments
        extract_gcode_comments(line)
    elif line.startswith("G1"):
        # Handle G1 (Move) commands
        return parse_g1_command(line)
    elif line.startswith("M109"):
        # Handle M109 (Set Temperature and Wait) commands
        return parse_m109_command(line)
    elif line.startswith("M104"):
        # Handle M104 (Set Temperature Without Waiting) commands
        return parse_m104_command(line)
    elif line.startswith("M106"):
        # Handle M106 (Fan Control) commands
        return parse_m106_command(line)
    return None


def process_gcode_file(input_path):
    """
    Processes the G-code file and extracts JSON commands.
    """
    json_commands = []
    try:
        with open(input_path, 'r') as gcode_file:
            for line in gcode_file:
                # Parse each line of the G-code file
                parsed_command = parse_line(line)
                if parsed_command:
                    if isinstance(parsed_command, list):
                        json_commands.extend(parsed_command)
                    else:
                        json_commands.append(parsed_command)
        logging.info(f"Processed G-code file: {input_path}")
    except Exception as e:
        logging.error(f"Error processing G-code file {input_path}: {e}")
    return json_commands


def modify_meta_json():
    """
    Modifies the meta.json file with extracted G-code information.
    """
    global meta_file_path
    logging.info("Modifying meta.json")

    if not os.path.exists(meta_file_path):
        logging.error(f"meta.json not found at {meta_file_path}")
        return None

    # Check if file is empty
    if os.path.getsize(meta_file_path) == 0:
        logging.error(f"meta.json is empty at {meta_file_path}")
        return None

    try:
        # Log current values of global variables
        logging.info(f"Current first_m109_temperature: {first_m109_temperature}")
        logging.info(f"Current extrusion_distance: {extrusion_distance}")
        logging.info(f"Current printing_time: {printing_time}")

        # Load the existing meta.json data
        with open(meta_file_path, 'r') as meta_file:
            meta_data = json.load(meta_file)
            logging.info(f"Loaded meta.json content: {meta_data}")

        # Modify the relevant fields
        if first_m109_temperature is not None:
            meta_data["extruder_temperature"] = first_m109_temperature
            meta_data["extruder_temperatures"] = [first_m109_temperature]
            logging.info(f"Set extruder_temperature to {first_m109_temperature}")

        if extrusion_distance is not None:
            meta_data["extrusion_distance_mm"] = extrusion_distance
            meta_data["extrusion_distances_mm"] = [extrusion_distance]
            logging.info(f"Set extrusion_distance_mm to {extrusion_distance}")

        if printing_time is not None:
            # Update printing time fields
            meta_data["commanded_duration_s"] = printing_time
            meta_data["commanded_durations_s"] = [printing_time]
            meta_data["duration_s"] = printing_time
            logging.info(f"Set printing_time fields: commanded_duration_s, commanded_durations_s, and duration_s to {printing_time}")

        # Log the updated meta_data before saving
        logging.info(f"Updated meta.json content: {meta_data}")

        # Save the modified meta.json
        with open(meta_file_path, 'w') as meta_file:
            json.dump(meta_data, meta_file, indent=3)
            logging.info("meta.json saved successfully")

    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode meta.json: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred while modifying meta.json: {e}")

    return meta_file_path


# =============================================================================
# IP Address Extraction
# =============================================================================

def extract_ip_from_filename(filename: str) -> str:
    """
    Extracts IP address from filename.
    Expected format: "Shape-Box T3_2 (172.16.11.3).gcode"
    
    Returns:
        IP address string or None if not found
    """
    # Match IP address pattern in parentheses
    match = re.search(r'\((\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\)', filename)
    if match:
        ip = match.group(1)
        logging.info(f"Extracted IP address: {ip} from filename: {filename}")
        return ip
    
    logging.warning(f"No IP address found in filename: {filename}")
    return None


def extract_printer_name_from_filename(filename: str) -> str:
    """
    Extracts printer name from filename.
    Expected format: "Shape-Box T3_2 (172.16.11.3).gcode"
    Returns the part before the IP, e.g., "T3_2"
    """
    # Match pattern: anything followed by printer name and IP in parentheses
    match = re.search(r'[\s_]([A-Za-z0-9_-]+)\s*\(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\)', filename)
    if match:
        name = match.group(1)
        logging.info(f"Extracted printer name: {name} from filename: {filename}")
        return name
    
    logging.warning(f"No printer name found in filename: {filename}")
    return "printer"


# =============================================================================
# MakerBot Printer Communication
# =============================================================================

class MakerbotPrinter:
    """MakerBot printer client for sending prints"""
    
    def __init__(self, ip: str, port: int = PRINTER_PORT, printer_name: str = "printer"):
        self.ip = ip
        self.port = port
        self.ssl_port = SSL_PORT
        self.printer_name = printer_name
        self._request_id = 0
        self._socket = None
        logging.info(f"MakerbotPrinter initialized for {ip}:{port} ({printer_name})")
    
    def _next_id(self) -> int:
        current = self._request_id
        self._request_id += 1
        return current
    
    def _send_rpc(self, method: str, params: dict = None) -> dict:
        """Send JSON-RPC request and receive response"""
        request = {
            "id": self._next_id(),
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        message = json.dumps(request).encode('utf-8')
        logging.info(f"Sending RPC: {method}")
        self._socket.sendall(message)
        
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
                                        logging.error(f"RPC error: {obj['error']}")
                                    else:
                                        logging.info(f"RPC response received for id {expected_id}")
                                    return obj
                                elif 'method' in obj:
                                    # Notification - skip
                                    pass
                                
                                buffer = decoded[i+1:].encode('utf-8')
                                decoded = buffer.decode('utf-8', errors='replace')
                                i = -1
                                
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
        logging.info("Starting TLS authorization...")
        
        # Notify user to press button
        notify("MakerBot Print", f"Press button on {self.printer_name} to authorize")
        
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
            
            logging.info("Waiting for printer authorization (button press required)...")
            
            data = sock.recv(8192)
            response = json.loads(data.decode('utf-8'))
            
            token = response.get('result', {}).get('one_time_token', '')
            if token:
                logging.info(f"Got authorization token: {token}")
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
        logging.info("Connecting and authenticating...")
        
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(30)
        self._socket.connect((self.ip, self.port))
        
        # Handshake
        response = self._send_rpc("handshake")
        self.ssl_port = int(response.get('result', {}).get('ssl_port', SSL_PORT))
        
        # Update printer name from handshake if available
        machine_name = response.get('result', {}).get('machine_name')
        if machine_name:
            self.printer_name = machine_name
        
        # Authenticate
        response = self._send_rpc("authenticate", {"access_token": token})
        if 'error' in response:
            raise RuntimeError(f"Authentication failed: {response['error']}")
        
        logging.info("Authentication successful")
    
    def _print_init(self, filename: str):
        """Initialize print job with transfer_wait"""
        logging.info(f"Initializing print job for: {filename}")
        
        response = self._send_rpc("print", {
            "filepath": filename,
            "transfer_wait": True
        })
        
        if 'error' in response:
            raise RuntimeError(f"print init failed: {response['error']}")
    
    def _put_init(self, file_path: Path) -> tuple:
        """Initialize file transfer"""
        file_size = file_path.stat().st_size
        file_id = "1"
        remote_path = f"/current_thing/{file_path.name}"
        
        logging.info(f"Initializing transfer: {file_path.name} ({file_size} bytes)")
        
        response = self._send_rpc("put_init", {
            "block_size": BLOCK_SIZE,
            "file_id": file_id,
            "file_path": remote_path,
            "length": file_size
        })
        
        if 'error' in response:
            raise RuntimeError(f"put_init failed: {response['error']}")
        
        return file_id, file_size
    
    def _put_raw_chunks(self, file_path: Path, file_id: str) -> int:
        """Send file in chunks using put_raw"""
        file_size = file_path.stat().st_size
        bytes_sent = 0
        crc = 0
        
        logging.info(f"Transferring file data...")
        
        with open(file_path, 'rb') as f:
            while bytes_sent < file_size:
                chunk = f.read(BLOCK_SIZE)
                chunk_len = len(chunk)
                
                crc = zlib.crc32(chunk, crc)
                
                response = self._send_rpc("put_raw", {
                    "file_id": file_id,
                    "length": chunk_len
                })
                
                if 'error' in response:
                    raise RuntimeError(f"put_raw failed: {response['error']}")
                
                self._socket.sendall(chunk)
                
                bytes_sent += chunk_len
                progress = (bytes_sent / file_size) * 100
                logging.debug(f"Transfer progress: {progress:.1f}%")
        
        logging.info(f"File transfer complete: {bytes_sent} bytes")
        return crc & 0xffffffff
    
    def _put_term(self, file_id: str, file_size: int, crc: int):
        """Terminate file transfer"""
        logging.info(f"Finalizing transfer with CRC: {crc}")
        
        response = self._send_rpc("put_term", {
            "crc": crc,
            "file_id": file_id,
            "length": file_size
        })
        
        if 'error' in response:
            raise RuntimeError(f"put_term failed: {response['error']}")
    
    def _process_method(self):
        """Signal that build plate is cleared and start print"""
        logging.info("Starting print...")
        
        response = self._send_rpc("process_method", {
            "method": "build_plate_cleared"
        })
        
        if 'error' in response:
            raise RuntimeError(f"process_method failed: {response['error']}")
        
        logging.info("Print started successfully!")
    
    def send_print(self, file_path: Path):
        """Complete workflow to send a print"""
        logging.info(f"Starting print job for: {file_path}")
        
        try:
            # Step 1: Fresh authorization
            token = self._fresh_authorize()
            
            # Notify that we're sending
            notify("MakerBot Print", f"Sending print to {self.printer_name}...")
            
            # Step 2: Connect and authenticate
            self._connect_and_auth(token)
            
            # Step 3: Initialize print job
            self._print_init(file_path.name)

            # Step 4: Start print
            self._process_method()
            
            # Step 5: Initialize file transfer
            file_id, file_size = self._put_init(file_path)
            
            # Step 6: Send file chunks
            crc = self._put_raw_chunks(file_path, file_id)
            
            # Step 7: Finalize transfer
            self._put_term(file_id, file_size, crc)
            
            logging.info("Print job sent successfully!")
            
            # Final notification
            notify("MakerBot Print", f"Print sent to {self.printer_name} successfully!")
            
            return True
            
        except Exception as e:
            logging.error(f"Failed to send print: {e}")
            notify("MakerBot Print", f"Failed to send print: {e}")
            raise
            
        finally:
            if self._socket:
                self._socket.close()
                self._socket = None


# =============================================================================
# Main File Creation and Sending
# =============================================================================

def create_makerbot_file(gcode_path):
    logging.info(f"Creating Makerbot file for G-code: {gcode_path}")
    
    # Retrieve true file name from environment variable
    true_output_name = os.getenv("SLIC3R_PP_OUTPUT_NAME")
    if not true_output_name:
        logging.error("SLIC3R_PP_OUTPUT_NAME environment variable not set. Using temporary file name.")
        true_output_name = gcode_path

    try:
        # Process G-code
        json_commands = process_gcode_file(gcode_path)

        # Create print.jsontoolpath
        json_file_name = "print.jsontoolpath"
        with open(json_file_name, 'w') as json_file:
            json.dump(json_commands, json_file, separators=(',', ':'))

        # Modify meta.json
        modify_meta_json()

        # Create .zip file and rename it to .makerbot
        zip_name = os.path.splitext(os.path.basename(true_output_name))[0] + ".zip"
        makerbot_name = os.path.join(downloads_folder, os.path.splitext(os.path.basename(true_output_name))[0] + ".makerbot")

        with zipfile.ZipFile(zip_name, 'w') as zipf:
            zipf.write(json_file_name, arcname="print.jsontoolpath")
            zipf.write(meta_file_path, arcname="meta.json")

        # Move the .makerbot file to Downloads
        os.rename(zip_name, makerbot_name)
        logging.info(f"Makerbot file saved to: {makerbot_name}")
        return makerbot_name
    except Exception as e:
        logging.error(f"Error while creating Makerbot file: {e}")
        return None


def send_to_printer(makerbot_file: str, printer_ip: str, printer_name: str):
    """
    Send the .makerbot file to the printer.
    
    Args:
        makerbot_file: Path to the .makerbot file
        printer_ip: IP address of the printer
        printer_name: Name of the printer for notifications
    """
    logging.info(f"Sending {makerbot_file} to printer at {printer_ip} ({printer_name})")
    
    try:
        printer = MakerbotPrinter(printer_ip, printer_name=printer_name)
        printer.send_print(Path(makerbot_file))
        logging.info("Print sent successfully!")
        return True
    except Exception as e:
        logging.error(f"Failed to send print to {printer_ip}: {e}")
        return False


def run_send_subprocess(makerbot_file: str, printer_ip: str, printer_name: str):
    """
    Spawn a subprocess to send the print, allowing the main script to exit.
    """
    script_path = os.path.abspath(__file__)
    
    # Launch subprocess with special argument to indicate send mode
    subprocess.Popen(
        [sys.executable, script_path, "--send", makerbot_file, printer_ip, printer_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True  # Detach from parent process
    )
    
    logging.info(f"Spawned send subprocess for {makerbot_file} to {printer_ip}")


if __name__ == "__main__":
    # Check if we're in send mode (called as subprocess)
    if len(sys.argv) >= 5 and sys.argv[1] == "--send":
        # Send mode: send the file to printer
        makerbot_file = sys.argv[2]
        printer_ip = sys.argv[3]
        printer_name = sys.argv[4]
        
        logging.info(f"Send subprocess started for {makerbot_file} to {printer_ip} ({printer_name})")
        send_to_printer(makerbot_file, printer_ip, printer_name)
        sys.exit(0)
    
    # Normal mode: convert G-code and spawn send subprocess
    if len(sys.argv) < 2:
        logging.error("No G-code file provided. Usage: python script.py <path_to_gcode>")
        sys.exit(1)

    gcode_file_path = sys.argv[1]
    logging.info(f"Received G-code file: {gcode_file_path}")
    
    # Extract IP address and printer name from filename
    true_output_name = os.getenv("SLIC3R_PP_OUTPUT_NAME", gcode_file_path)
    printer_ip = extract_ip_from_filename(true_output_name)
    printer_name = extract_printer_name_from_filename(true_output_name)
    
    # Create .makerbot file
    makerbot_file = create_makerbot_file(gcode_file_path)
    
    if makerbot_file and printer_ip:
        # Spawn subprocess to send to printer (non-blocking)
        logging.info(f"Spawning send subprocess for {printer_ip} ({printer_name})")
        notify("MakerBot Print", f"Preparing to send to {printer_name}...")
        run_send_subprocess(makerbot_file, printer_ip, printer_name)
        
        # Exit immediately so PrusaSlicer can continue
        logging.info("Main script exiting, send subprocess running in background")
        sys.exit(0)
        
    elif makerbot_file:
        logging.warning("No printer IP found in filename. File created but not sent.")
        notify("MakerBot Print", f"File created: {os.path.basename(makerbot_file)}")
        logging.info(f"Makerbot file created: {makerbot_file}")
    else:
        logging.error("Failed to create .makerbot file")
        notify("MakerBot Print", "Failed to create .makerbot file")
        sys.exit(1)
