import json
from converter import MakerbotPrinter

ACCEL_CONFIG = {
    "config": {
        "acceleration": {
            "rate_mm_per_s_sq": {"x": 400, "y": 400, "z": 100},
            "max_speed_change_mm_per_s": {"x": 25, "y": 25, "z": 10},
            "impulse_speed_limit_mm_per_s": {"x": 100, "y": 100, "z": 10}
        },
        "max_speed_mm_per_second": {"z": 10},
        "build_volume": {"y": 200, "x": 300, "z": 160},
        "gantry_configuration": {"travel_speed_xy": 100, "travel_speed_z": 10}
    }
}

def run_set_accel(ip, name):
    printer = MakerbotPrinter(ip, printer_name=name)
    token = printer._fresh_authorize()
    printer._connect_and_auth(token)
    resp = printer._send_rpc("set_config", ACCEL_CONFIG)
    printer._socket.close()
    return resp

if __name__ == "__main__":
    results = {}
    for ip, name in [("172.16.11.3", "T3_1"), ("172.16.11.4", "T3_2")]:
        print(f"\n=== Connecting to {name} ({ip}) — press the button when prompted ===")
        results[name] = run_set_accel(ip, name)
        print(f"{name} response:\n{json.dumps(results[name], indent=2)}")

    print("\n=== Diff ===")
    if results["T3_1"] == results["T3_2"]:
        print("Responses are IDENTICAL.")
    else:
        print("Responses DIFFER:")
        print(f"T3_1: {json.dumps(results['T3_1'], indent=2)}")
        print(f"T3_2: {json.dumps(results['T3_2'], indent=2)}")
