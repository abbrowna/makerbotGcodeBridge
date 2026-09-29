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

def collect(ip, name):
    printer = MakerbotPrinter(ip, printer_name=name)
    token = printer._fresh_authorize()
    printer._connect_and_auth(token)

    sysinfo = printer._send_rpc("get_system_information")
    stats = printer._send_rpc("get_statistics")
    config_before = printer._send_rpc("get_machine_config")
    set_resp = printer._send_rpc("set_config", ACCEL_CONFIG)
    config_after = printer._send_rpc("get_machine_config")

    printer._socket.close()
    return {
        "sysinfo": sysinfo.get("result"),
        "stats": stats.get("result"),
        "config_before": config_before.get("result"),
        "set_config_response": set_resp,
        "config_after": config_after.get("result"),
    }

def flatten(d, prefix=''):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f'{prefix}.{k}' if prefix else k))
    elif isinstance(d, list):
        out[prefix] = json.dumps(d)
    else:
        out[prefix] = d
    return out

def diff(a, b, label_a, label_b):
    fa, fb = flatten(a), flatten(b)
    keys = sorted(set(fa) | set(fb))
    diffs = [(k, fa.get(k, '<MISSING>'), fb.get(k, '<MISSING>'))
             for k in keys if fa.get(k, '<MISSING>') != fb.get(k, '<MISSING>')]
    if not diffs:
        print(f"  No differences between {label_a} and {label_b}.")
    else:
        for k, va, vb in diffs:
            print(f"  {k}:\n    {label_a} = {va}\n    {label_b} = {vb}")

if __name__ == "__main__":
    results = {}
    for ip, name in [("172.16.11.3", "T3_2"), ("172.16.11.4", "T3_1")]:
        print(f"\n=== Connecting to {name} ({ip}) — press the button when prompted ===")
        results[name] = collect(ip, name)
        with open(f"_full_probe_{name}.json", "w") as f:
            json.dump(results[name], f, indent=2)

    print("\n\n========== set_config response (each printer) ==========")
    for name in results:
        print(f"{name}: {json.dumps(results[name]['set_config_response'])}")

    print("\n========== get_machine_config: before vs after set_config (per printer) ==========")
    for name in results:
        print(f"\n-- {name} --")
        diff(results[name]['config_before'], results[name]['config_after'], 'before', 'after')

    print("\n========== get_system_information: T3_2 vs T3_1 ==========")
    diff(results['T3_2']['sysinfo'], results['T3_1']['sysinfo'], 'T3_2', 'T3_1')

    print("\n========== get_statistics: T3_2 vs T3_1 ==========")
    print(f"  T3_2 = {json.dumps(results['T3_2']['stats'])}")
    print(f"  T3_1 = {json.dumps(results['T3_1']['stats'])}")

    print("\n========== get_machine_config (after set_config): T3_2 vs T3_1 ==========")
    diff(results['T3_2']['config_after'], results['T3_1']['config_after'], 'T3_2', 'T3_1')
