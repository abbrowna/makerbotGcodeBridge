import json
import sys
from converter import MakerbotPrinter

def get_config(ip, name):
    printer = MakerbotPrinter(ip, printer_name=name)
    token = printer._fresh_authorize()
    printer._connect_and_auth(token)
    resp = printer._send_rpc("get_machine_config")
    printer._socket.close()
    if 'error' in resp:
        raise RuntimeError(f"get_machine_config failed for {name}: {resp['error']}")
    return resp.get('result', {})

if __name__ == "__main__":
    ips = [("172.16.11.3", "T3_1"), ("172.16.11.4", "T3_2")]
    configs = {}
    for ip, name in ips:
        print(f"\n=== Connecting to {name} ({ip}) — press the button on the printer when prompted ===")
        configs[name] = get_config(ip, name)
        with open(f"_config_{name}.json", "w") as f:
            json.dump(configs[name], f, indent=2)
        print(f"Saved config to _config_{name}.json")

    print("\n=== Done. Configs saved. ===")
