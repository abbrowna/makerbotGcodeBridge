# MakerBot .makerbot File — Complete Command Reference

> Derived from reverse-engineering the printer's Linux filesystem (`mbot ripped files`):
> `libmachine/pymachine.py`, `tinything/tinything.py`, `kaiten/machine_manager.py`,
> `mbcoreutils/machine_definitions.py`, `start.json`, `home.json`, and the existing `converter.py`.

---

## 1. File Structure

A `.makerbot` file is a **ZIP archive** containing:

| File | Purpose |
|------|---------|
| `meta.json` | Print metadata (temperatures, bounding box, material, duration, etc.) |
| `print.jsontoolpath` | Line-delimited JSON toolpath commands (the main print body) |
| `start.json` | Start sequence run before printing begins |
| `home.json` | Home sub-sequence (called from `start.json`) |

---

## 2. meta.json Fields

```json
{
  "bot_type": "replicator_b",
  "bounding_box": {
    "x_min": -120.0, "x_max": 96.0,
    "y_min": -65.0,  "y_max": 86.0,
    "z_min": 0.0,    "z_max": 126.0
  },
  "platform_temperature": 0,
  "build_plane_temperature": 0,
  "commanded_duration_s": 55542,
  "duration_s": 55542,
  "commanded_durations_s": [55542],
  "extrusion_distance_mm": 180470.0,
  "extrusion_distances_mm": [180470.0],
  "extrusion_mass_g": 538.26,
  "extrusion_masses_g": [538.26],
  "extruder_temperature": 220,
  "extruder_temperatures": [220],
  "material": "pla",
  "materials": ["pla"],
  "tool_type": "mk13",
  "tool_types": ["mk13"],
  "uuid": "00000000-0000-0000-0000-000000000000",
  "version": "3.0.0"
}
```

### Supported `bot_type` values
- `replicator_b` — MakerBot Replicator+ / 5th Gen family ("horseshoe" / "birdwing")

### Supported `tool_type` / `tool_types` values
| ID | Name | Tool |
|----|------|------|
| 1-4 | `mark_11` / `mark_11_mod_1..3` | Smart Extruder 11.x |
| 5-7,9-13 | `mark_12` / `mark_12_x` | Smart Extruder 12.x |
| 8,15,17,19,21 | `mark_13` | Smart Extruder+ |
| 14,16,18,20,22 | `mark_13_impla` | Tough Smart Extruder+ |
| 99 | `mark_13_experimental` | Experimental Extruder |
| 100 | `mk14_0` | Shiny Octo Parakeet (mk14) |

### Supported `material` values
`pla`, `abs`, `im-pla`, `tough` (and others accepted by the slicer profile)

---

## 3. Axes Reference

| Index | Name | Description |
|-------|------|-------------|
| 0 | `x` | X axis (gantry left/right) |
| 1 | `y` | Y axis (build plate front/back) |
| 2 | `z` | Z axis (build plate up/down) |
| 3 | `a` | A axis (primary extruder motor) |
| 4 | `b` | B axis (secondary extruder motor, dual-head only) |

---

## 4. print.jsontoolpath Command Format

Each line in `print.jsontoolpath` is a self-contained JSON object:

```json
{
  "command": {
    "function": "<function_name>",
    "metadata": { ... },
    "parameters": { ... },
    "tags": ["TagName"]
  }
}
```

`tags` is an informational list (e.g. `"Move"`, `"Retract"`, `"Extrude"`) used for analytics. It
does not affect execution.

---

## 5. Complete Command List — print.jsontoolpath

### 5.1 Motion Commands

#### `move`
Move all axes simultaneously to absolute or relative positions.

```json
{
  "command": {
    "function": "move",
    "metadata": {
      "relative": {
        "x": false,
        "y": false,
        "z": false,
        "a": false
      }
    },
    "parameters": {
      "x": 10.0,
      "y": 20.0,
      "z": 0.2,
      "a": 5.0,
      "feedrate": 40.0
    },
    "tags": ["Move"]
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `x` | float | X position (mm) |
| `y` | float | Y position (mm) |
| `z` | float | Z position (mm) |
| `a` | float | Extruder position (mm of filament) |
| `feedrate` | float | Speed in **mm/s** (not mm/min) |
| `metadata.relative.x/y/z/a` | bool | If `true`, value is relative to current position |

> **Note:** `feedrate` is in **mm/s**. PrusaSlicer exports mm/min — divide by 60.

---

#### `move_axis`
Move a single axis.

```json
{
  "command": {
    "function": "move_axis",
    "metadata": {},
    "parameters": {
      "axis": 2,
      "point_mm": 5.0,
      "mm_per_second": 10.0,
      "relative": false
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `axis` | int | Axis index (0=X, 1=Y, 2=Z, 3=A, 4=B) |
| `point_mm` | float | Target position (mm) |
| `mm_per_second` | float | Speed in mm/s |
| `relative` | bool | Move relative to current position |

---

#### `set_position`
Declare the current position of an axis without moving it (like `G92` in G-code).

```json
{
  "command": {
    "function": "set_position",
    "metadata": {},
    "parameters": {
      "axis": 3,
      "position_mm": 0.0
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `axis` | int | Axis index |
| `position_mm` | float | Value to assign to current position |

---

#### `home_axis`
Home a single axis against its endstop.

```json
{
  "command": {
    "function": "home_axis",
    "metadata": {},
    "parameters": {
      "axis": 2,
      "speed": 10.0,
      "flip_direction": false,
      "set_position": true,
      "blocking_home": false
    },
    "tags": []
  }
}
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `axis` | int | — | Axis to home |
| `speed` | float | — | Homing speed (mm/s) |
| `flip_direction` | bool | `false` | Reverse homing direction |
| `set_position` | bool | `true` | Set position reference after homing |
| `blocking_home` | bool | `false` | Block until homing is complete |

---

#### `motor_enable`
Enable or disable a stepper motor.

```json
{
  "command": {
    "function": "motor_enable",
    "metadata": {},
    "parameters": {
      "axis": 0,
      "enable": true
    },
    "tags": []
  }
}
```

---

### 5.2 Temperature Commands

#### `set_toolhead_temperature` (alias: `set_temperature_target`)
Set the target temperature for a toolhead. Does **not** wait for the temperature to be reached.

```json
{
  "command": {
    "function": "set_toolhead_temperature",
    "metadata": {},
    "parameters": {
      "index": 0,
      "temperature": 215
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `index` | int | Toolhead index (0 = primary extruder) |
| `temperature` | int | Target temperature in °C (0 = turn off) |

---

#### `wait_for_heaters_at_target`
Block until all checked toolheads reach their target temperature, or timeout.

```json
{
  "command": {
    "function": "wait_for_heaters_at_target",
    "metadata": {},
    "parameters": {
      "timeout_minutes": 5,
      "check": [true]
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `timeout_minutes` | int | Max minutes to wait |
| `check` | list[bool] | One boolean per toolhead — `true` means check this toolhead |

---

#### `wait_for_heaters_at_least_target`
Block until all checked toolheads are **at or above** their target temperature.
Same parameters as `wait_for_heaters_at_target`.

---

#### `load_temperature_settings`
Load a set of target temperatures for all toolheads at once.

```json
{
  "command": {
    "function": "load_temperature_settings",
    "metadata": {},
    "parameters": {
      "active_temperatures": [215]
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `active_temperatures` | list[int] | Target °C per toolhead, e.g. `[215]` for single extruder |

---

#### `heat`
Start heating all toolheads toward their loaded temperature targets.
No parameters required.

```json
{ "command": { "function": "heat", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

#### `cool`
Turn off all heaters immediately.

```json
{ "command": { "function": "cool", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

### 5.3 Fan Commands

#### `toggle_fan`
Turn the part-cooling fan on or off.

```json
{
  "command": {
    "function": "toggle_fan",
    "metadata": {},
    "parameters": {
      "index": 0,
      "value": true
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `index` | int | Fan index (0 = part fan) |
| `value` | bool | `true` = on, `false` = off |

---

#### `fan_duty` (alias: `set_fan_duty`)
Set the fan duty cycle (speed).

```json
{
  "command": {
    "function": "fan_duty",
    "metadata": {},
    "parameters": {
      "index": 0,
      "value": 0.5
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `index` | int | Fan index |
| `value` | float | Duty cycle `0.0` (off) to `1.0` (full speed) |

> **Slicer conversion:** M106 S<0-255> → divide by 255 to get 0.0–1.0.

---

#### `set_heater_duty`
Directly set the heater PWM duty cycle (for diagnostics / advanced use).

```json
{
  "command": {
    "function": "set_heater_duty",
    "metadata": {},
    "parameters": {
      "index": 0,
      "duty": 0.5
    },
    "tags": []
  }
}
```

---

### 5.4 Extrusion / Filament Commands

#### `toggle_filament_jam`
Enable or disable filament jam detection.

```json
{
  "command": {
    "function": "toggle_filament_jam",
    "metadata": {},
    "parameters": { "on": true },
    "tags": []
  }
}
```

---

#### `load_filament_jam_settings`
Configure filament jam detection sensitivity.

```json
{
  "command": {
    "function": "load_filament_jam_settings",
    "metadata": {},
    "parameters": {
      "index": 0,
      "steps_per_mm": 108.55
    },
    "tags": []
  }
}
```

---

#### `reset_extrusion_distance`
Reset the extrusion distance counter (like `G92 E0` in G-code).

```json
{ "command": { "function": "reset_extrusion_distance", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

#### `toggle_extrusion_percent_update`
Enable/disable live extrusion progress tracking.

```json
{
  "command": {
    "function": "toggle_extrusion_percent_update",
    "metadata": {},
    "parameters": { "state": true },
    "tags": []
  }
}
```

---

#### `set_extruder_delay`
Set a delay (in ms) applied before/after extruder movement to reduce ooze.

```json
{
  "command": {
    "function": "set_extruder_delay",
    "metadata": {},
    "parameters": { "delay": 0 },
    "tags": []
  }
}
```

---

### 5.5 Acceleration / Motion Control

#### `toggle_acceleration`
Enable or disable acceleration (junction deviation / trapezoidal motion).

```json
{
  "command": {
    "function": "toggle_acceleration",
    "metadata": {},
    "parameters": { "state": true },
    "tags": []
  }
}
```

---

#### `toggle_acceleration_lookahead`
Enable or disable acceleration lookahead (look-ahead buffer for smoother corners).

```json
{
  "command": {
    "function": "toggle_acceleration_lookahead",
    "metadata": {},
    "parameters": { "state": true },
    "tags": []
  }
}
```

---

#### `toggle_expend_buffer`
Enable or disable buffer expenditure mode (controls how the move buffer is flushed).

```json
{
  "command": {
    "function": "toggle_expend_buffer",
    "metadata": {},
    "parameters": { "state": true },
    "tags": []
  }
}
```

---

#### `toggle_toolhead_syncing`
Enable or disable synchronized toolhead moves.

```json
{
  "command": {
    "function": "toggle_toolhead_syncing",
    "metadata": {},
    "parameters": { "state": true },
    "tags": []
  }
}
```

---

#### `enable_toolhead_idle`
Allow the toolhead to enter idle mode between moves (reduces heat creep).

```json
{ "command": { "function": "enable_toolhead_idle", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

### 5.6 Z-Pause (Colour Change / Layer Pause)

These commands support automatic pausing at specific Z heights (e.g. for filament colour changes).

#### `set_z_pause_mm`
Register a Z height at which the printer will automatically pause.

```json
{
  "command": {
    "function": "set_z_pause_mm",
    "metadata": {},
    "parameters": { "z_pause_mm": 10.0 },
    "tags": []
  }
}
```

---

#### `enable_z_pause` / `disable_z_pause`
Enable or disable the Z-pause feature globally.

```json
{ "command": { "function": "enable_z_pause", "metadata": {}, "parameters": {}, "tags": [] } }
{ "command": { "function": "disable_z_pause", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

#### `clear_z_pause_mm`
Remove a specific Z-pause height.

```json
{
  "command": {
    "function": "clear_z_pause_mm",
    "metadata": {},
    "parameters": { "mm": 10.0 },
    "tags": []
  }
}
```

---

#### `clear_all_z_pause`
Remove all registered Z-pause heights.

```json
{ "command": { "function": "clear_all_z_pause", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

#### `reset_z_pause_index`
Reset the internal Z-pause queue index (used after a resume).

```json
{ "command": { "function": "reset_z_pause_index", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

### 5.7 LED / Visual Feedback

#### `set_led_brightness`
Set the brightness of an LED strip.

```json
{
  "command": {
    "function": "set_led_brightness",
    "metadata": {},
    "parameters": {
      "index": 0,
      "brightness": 1.0
    },
    "tags": []
  }
}
```

---

#### `set_led_blink`
Set an LED to blink at a given rate.

```json
{
  "command": {
    "function": "set_led_blink",
    "metadata": {},
    "parameters": {
      "index": 0,
      "brightness": 255,
      "period": 1.0
    },
    "tags": []
  }
}
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `index` | int | LED index |
| `brightness` | int | 0–255 |
| `period` | float | Blink period in seconds |

---

#### `set_knob_color`
Set the colour of the front knob LED (RGB model printers).

```json
{
  "command": {
    "function": "set_knob_color",
    "metadata": {},
    "parameters": {
      "red": 1.0,
      "green": 0.0,
      "blue": 0.0,
      "brightness": 1.0
    },
    "tags": []
  }
}
```

All colour values are `0.0`–`1.0` floats.

---

#### `set_chamber_color`
Set the colour of the chamber LED (same parameter format as `set_knob_color`).

---

#### `set_knob_blink`
Set the knob LED to blink.

```json
{
  "command": {
    "function": "set_knob_blink",
    "metadata": {},
    "parameters": { "period": 0.5 },
    "tags": []
  }
}
```

---

#### `set_chamber_blink`
Set the chamber LED to blink (same parameter format as `set_knob_blink`).

---

#### `set_moose_chamber_brightness`
Set chamber brightness on MOOSE-variant printers.

```json
{
  "command": {
    "function": "set_moose_chamber_brightness",
    "metadata": {},
    "parameters": { "brightness": 0.8 },
    "tags": []
  }
}
```

---

#### `led_sleep` / `led_wake`
Put LEDs into sleep mode or wake them.

```json
{ "command": { "function": "led_sleep", "metadata": {}, "parameters": {}, "tags": [] } }
{ "command": { "function": "led_wake",  "metadata": {}, "parameters": {}, "tags": [] } }
```

---

### 5.8 Tool Management

#### `change_tool`
Switch to a different toolhead.

```json
{
  "command": {
    "function": "change_tool",
    "metadata": {},
    "parameters": { "index": 0 },
    "tags": []
  }
}
```

---

#### `connect_tool`
Initialize/connect a toolhead (usually called during startup).

```json
{
  "command": {
    "function": "connect_tool",
    "metadata": {},
    "parameters": {
      "index": 0,
      "abort_toolhead_only": false
    },
    "tags": []
  }
}
```

---

#### `toggle_toolhead_power`
Enable or disable power to a toolhead.

```json
{
  "command": {
    "function": "toggle_toolhead_power",
    "metadata": {},
    "parameters": {
      "index": 0,
      "power_state": true
    },
    "tags": []
  }
}
```

---

### 5.9 System / Print Context

#### `abort`
Abort the current machine operation.

```json
{ "command": { "function": "abort", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

#### `quick_pause`
Pause or resume motion immediately.

```json
{
  "command": {
    "function": "quick_pause",
    "metadata": {},
    "parameters": { "state": true },
    "tags": []
  }
}
```

---

#### `clear_all_buffers`
Flush all move and acceleration buffers.

```json
{ "command": { "function": "clear_all_buffers", "metadata": {}, "parameters": {}, "tags": [] } }
```

---

#### `set_print_context`
Set or clear the print context (used internally when starting/ending a print).

```json
{
  "command": {
    "function": "set_print_context",
    "metadata": {},
    "parameters": {
      "state": true,
      "filename": "print.jsontoolpath"
    },
    "tags": []
  }
}
```

---

#### `load_print_meta_settings`
Feed raw metadata JSON string to the machine driver.

```json
{
  "command": {
    "function": "load_print_meta_settings",
    "metadata": {},
    "parameters": {
      "meta_file_string": "{\"extruder_temperature\": 215}"
    },
    "tags": []
  }
}
```

---

#### `initialize_from_settings`
Fully re-initialize the machine driver from a metadata string.
Same parameters as `load_print_meta_settings`.

---

## 6. start.json / home.json — Sequence Command Format

These JSON files use a **different array-based command format** interpreted at a higher level
than `print.jsontoolpath`.

```json
{
  "environment": {},
  "sequence": [
    ["command_name", arg1, arg2, ...],
    ...
  ]
}
```

Environment variables (e.g. `$EXTRUDER_0_TEMPERATURE`) are substituted from the machine config
and print metadata.

### Available sequence commands

| Command | Arguments | Description |
|---------|-----------|-------------|
| `load_temperature_settings` | `[temps_array]` | Load target temps (e.g. `["$EXTRUDER_0_PREHEAT_TEMP"]`) |
| `heat` | — | Begin heating |
| `home_gantry` | — | Home X and Y axes |
| `home_z` | — | Home Z axis |
| `home` | `[axis_list]` | Home specified axes (e.g. `["y"]`, `["x"]`) |
| `move` | `[x,y,z,a], speed, [rel_x,rel_y,rel_z,rel_a]` | Move all axes |
| `move_axis` | `axis_idx, position, speed, relative` | Move a single axis |
| `set_position` | `axis_idx, position_mm` | Set position reference |
| `wait_for_file` | — | Block until toolpath file is ready (streaming prints) |
| `wait_for_heaters_at_target` | `timeout_min, [check_array]` | Wait for heaters |

### Environment variable substitutions
| Variable | Value |
|----------|-------|
| `$EXTRUDER_0_TEMPERATURE` | Print temperature for extruder 0 |
| `$EXTRUDER_0_PREHEAT_TEMP` | Preheat temperature for extruder 0 |
| `$X_SAFE_ZHOME` | Safe X position for Z homing |
| `$Y_SAFE_ZHOME` | Safe Y position for Z homing |
| `$X_PURGE_START` | X start of purge line |
| `$Y_PURGE_START` | Y start of purge line |
| `$Z_PURGE_START` | Z start of purge line |
| `$A_PURGE_START` | A start of purge line |
| `$X_PURGE_END` | X end of purge line |
| `$Y_PURGE_END` | Y end of purge line |
| `$Z_PURGE_END` | Z end of purge line |
| `$A_PURGE_END` | A end of purge line |

---

## 7. Machine Config Reference (from getmachineconfig.json)

Key values from the actual printer used in development:

```
Build volume:        300 x 200 x 165 mm
Steps/mm (X,Y):      88.573186 steps/mm
Steps/mm (Z):        400 steps/mm
Steps/mm (A):        108.55 steps/mm (mk13 extruder)
Max speed X/Y:       270 mm/s
Max speed Z:         30 mm/s
Max accel X/Y:       800 mm/s²
Max accel Z:         150 mm/s²
Travel speed XY:     150 mm/s (slicer default)
Max outer shell:     40 mm/s
Max inner shell:     90 mm/s
Max fill speed:      90 mm/s
bot_type:            replicator_b
Firmware:            2.6.3 build 736
```

---

## 8. Typical print.jsontoolpath Structure (ordered)

```
1.  set_toolhead_temperature      ← set target temp
2.  toggle_extrusion_percent_update (state: true)
3.  toggle_filament_jam           ← enable jam detection
4.  toggle_fan + fan_duty         ← configure fans
5.  toggle_acceleration           ← enable accel
6.  toggle_acceleration_lookahead ← enable lookahead
7.  toggle_expend_buffer          ← enable buffer
8.  [move commands…]              ← the actual toolpath
9.  set_toolhead_temperature (0)  ← cool down extruder
10. cool                          ← turn off all heaters
```

---

## 9. G-code to MakerBot Command Mapping

| G-code | MakerBot command | Notes |
|--------|-----------------|-------|
| `G1 X Y Z E F` | `move` | F in mm/min → divide by 60 for mm/s; E maps to `a` |
| `G28` | `home_axis` (each axis) or via `home_gantry`/`home_z` in start.json | |
| `G92 E0` | `set_position` axis=3, position_mm=0 | |
| `M104 S<T>` | `set_toolhead_temperature` (no wait) | |
| `M109 S<T>` | `set_toolhead_temperature` + `wait_for_heaters_at_target` | |
| `M106 S<V>` | `toggle_fan` (true) + `fan_duty` (V/255) | |
| `M107` | `toggle_fan` (false) | |
| `M84` | `motor_enable` (false) each axis | |

---

## 10. Notes & Quirks

- The `a` axis is **absolute filament distance** (like RepRap's absolute extrusion mode). There is
  no concept of relative extrusion in the toolpath — always track absolute position.
- `feedrate` in `print.jsontoolpath` is in **mm/s**. G-code `F` values are in **mm/min**.
- The printer expects `meta.json` to have `bot_type: "replicator_b"` — mismatches cause a
  `bot_type_mismatch` error from `libtinything` and the print is refused.
- `tool_type` in `meta.json` must match the physically connected extruder ID or the firmware
  returns a `tool_mismatch` error (error code 2 from TinyThing).
- `version` in `meta.json` must be `"3.0.0"` for modern firmware (≥ 2.6.x).
- The `print.jsontoolpath` is parsed by the closed-source `libtinything.so` C library which
  bridges into `libmachine.so`. The Python layer (`pymachine.py`) is a ctypes wrapper around
  `libmachine.so`.
