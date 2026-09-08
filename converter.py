import json
import math
import zipfile
import os
import sys
import logging
import re
import socket
import ssl
import time
import zlib
import shutil
import subprocess
import threading
from pathlib import Path
from pync import Notifier
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

# Initialize logging
downloads_folder = os.path.join(os.path.expanduser("~"), "Downloads")
log_file = os.path.join(downloads_folder, "makerbot_conversion.log")
logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Log script start
logging.info("Script started")
# Log passed arguments
logging.info(f"Arguments: {sys.argv}")


def notify(title, text):
    """Send macOS notification with custom logo"""
    Notifier.notify(text, title=title, appIcon=logo_path)


# Formatting helpers used by the --mesh-print terminal window
_STATUS_ICONS = {
    'stage': '\n▶',
    'info':  '  ',
    'ok':    ' ✓',
    'warn':  ' ⚠',
    'error': ' ✗',
    'probe': '  ·',
    'data':  '   ',
    'xfer':  '  ↑',
}

def status(msg: str, level: str = 'info') -> None:
    """
    Print a timestamped, formatted line to stdout.
    Used exclusively by the --mesh-print Terminal subprocess for live feedback.
    The main process (which exits immediately) never calls this.
    """
    ts = time.strftime('%H:%M:%S')
    icon = _STATUS_ICONS.get(level, '  ')
    print(f'[{ts}]{icon} {msg}', flush=True)


# Print transfer constants
PRINTER_PORT = 9999
SSL_PORT = 12309
BLOCK_SIZE = 32768
WAIT_FOR_PUT_RAW_RESPONSE = True  # Set to False to send put_raw without waiting for RPC responses

# =============================================================================
# Mesh Bed Leveling Constants
# =============================================================================
MESH_GRID_X = 13            # probe columns (X direction)
MESH_GRID_Y = 9            # probe rows    (Y direction)
FADE_HEIGHT_MM = 10.0      # compensation fades to zero at this layer height (mm)
MESH_SAFE_Z = 5.0          # Z height for XY travel between probe points (mm above Z=0)
MESH_REPROBE_Z = 2       # Z height before re-probe at each point (mm above Z=0)
MESH_PROBE_SPEED = 1.0     # probe descent speed (mm/s)
MESH_PROBE_LIMIT = -8.0    # how far below MESH_SAFE_Z to search for bed (mm)
MESH_XY_SPEED = 100.0      # XY travel speed during probing (mm/s)
MESH_LIFT_SPEED = 5.0      # Z lift speed after each probe (mm/s)
PROBE_SAMPLES_PER_POINT = 1  # probes taken at each grid point; the sample farthest
                              # from the other two is discarded and the remaining
                              # two are averaged (rejects single-sample noise —
                              # debris on the nozzle/bed, HES trigger jitter, etc.)
PROBE_SPREAD_WARN_MM = 0.15  # if the full 3-sample spread at a point exceeds this,
                              # log a warning — even after outlier rejection, a wide
                              # spread across all 3 attempts suggests something
                              # physical (debris, loose HES wiring) rather than
                              # ordinary noise
NO_TRIGGER_EPSILON_MM = 0.05      # a reading this close to the hard search limit
                                   # means the HES never fired at all — the descent
                                   # ran to the end of its travel, not a real contact
EARLY_TRIGGER_DEPTH_WARN_MM = 0.15  # a reading that stopped this close to the start
                                     # height (MESH_SAFE_Z) almost certainly isn't a
                                     # real bed contact either — more likely the HES
                                     # was still latched from the previous probe and
                                     # fired the instant the descent began
MAX_MESH_AGE_HOURS = 8760.0  # reuse saved mesh if younger than this

# Probe grid — points are evenly spaced across (bed size - MESH_PROBE_MARGIN_MM)
# on each axis, centered on the bed origin, using however many columns/rows
# MESH_GRID_X / MESH_GRID_Y specify. Changing the grid-count constants above
# is now sufficient on its own — these no longer need separate hand edits.
BED_SIZE_X_MM = 295.0        # physical build plate width  (X axis, mm)
BED_SIZE_Y_MM = 195.0        # physical build plate depth  (Y axis, mm)
MESH_PROBE_MARGIN_MM = 10.0   # total margin subtracted from each axis' full span
                              # (e.g. 295 x 195 bed -> 290 x 190 probed span)

def _evenly_spaced_points(count: int, span: float) -> list:
    """`count` points evenly spaced across `span`, centered on 0."""
    if count < 2:
        return [0.0]
    step = span / (count - 1)
    half = span / 2.0
    return [round(-half + i * step, 2) for i in range(count)]

MESH_PROBE_X = _evenly_spaced_points(MESH_GRID_X, BED_SIZE_X_MM - MESH_PROBE_MARGIN_MM)
MESH_PROBE_Y = _evenly_spaced_points(MESH_GRID_Y, BED_SIZE_Y_MM - MESH_PROBE_MARGIN_MM)
MESH_MIN_SEGMENT_MM        = 2.0    # never subdivide finer than this (mm of XY travel)
MESH_MAX_SEGMENT_MM        = 60.0   # always break at least this often, even where the bed is flat
MESH_CHORD_TOLERANCE_MM    = 0.015  # max allowed Z deviation between a straight chord and the
                                     # true mesh-compensated surface before we bisect further
MESH_MAX_SUBDIVISION_DEPTH = 8      # safety cap: min possible segment = move_length / 2**depth

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
logo_path = os.path.join(script_dir, 'makerbot_bridge_logo.png')


# =============================================================================
# Mesh Bed Leveling: File I/O and Interpolation
# =============================================================================

def _mesh_file_path(printer_name: str) -> str:
    """Return the path to the saved mesh file for a given printer."""
    safe_name = re.sub(r'[^A-Za-z0-9_\-.]', '_', printer_name)
    return os.path.join(script_dir, f'mesh_{safe_name}.json')


def load_mesh_from_file(printer_name: str) -> dict:
    """
    Load a saved mesh from disk.  Returns the mesh dict if it exists and is
    younger than MAX_MESH_AGE_HOURS, otherwise returns None.
    """
    path = _mesh_file_path(printer_name)
    if not os.path.exists(path):
        logging.info(f"No saved mesh found for {printer_name}")
        return None
    try:
        with open(path, 'r') as f:
            mesh = json.load(f)
        age_hours = (time.time() - mesh.get('timestamp', 0)) / 3600.0
        if age_hours > MAX_MESH_AGE_HOURS:
            logging.info(f"Saved mesh for {printer_name} is {age_hours:.1f}h old — will re-probe")
            return None
        logging.info(f"Loaded mesh for {printer_name} ({age_hours:.1f}h old)")
        return mesh
    except Exception as e:
        logging.warning(f"Could not load mesh file {path}: {e}")
        return None


def save_mesh_to_file(printer_name: str, mesh_data: dict) -> None:
    """Save a mesh dict to disk."""
    path = _mesh_file_path(printer_name)
    try:
        with open(path, 'w') as f:
            json.dump(mesh_data, f, indent=2)
        logging.info(f"Mesh saved to {path}")
    except Exception as e:
        logging.warning(f"Could not save mesh to {path}: {e}")


def display_mesh(mesh_data: dict) -> None:
    """
    Print the mesh offset grid to stdout (shown in the Terminal subprocess window).
    Rows are ordered with the highest Y at the top so the grid matches a
    top-down view of the build plate.
    """
    offsets = mesh_data['offsets']
    probe_x = mesh_data['probe_x']
    probe_y = mesh_data['probe_y']
    fade_h  = mesh_data.get('fade_height', FADE_HEIGHT_MM)
    age_s   = time.time() - mesh_data.get('timestamp', time.time())
    max_dev = max(abs(v) for row in offsets for v in row)

    col_w   = 8   # characters per X column
    label_w = 10  # width of the "  Y=+xx: " prefix
    bar_w   = label_w + col_w * len(probe_x)

    # Column header
    hdr = ' ' * label_w + ''.join(f'{"X="+str(int(x)):>{col_w}}' for x in probe_x)
    sep = '  ' + '─' * (bar_w - 2)

    print('', flush=True)
    print('  Bed Mesh Map  (mm  │  + = high, − = low)', flush=True)
    print(sep, flush=True)
    print(hdr, flush=True)
    print(sep, flush=True)

    # Rows — highest Y first so the grid looks like a top-down view
    for yi in range(len(probe_y) - 1, -1, -1):
        row   = offsets[yi]
        label = f'  Y={probe_y[yi]:+.0f}:'
        vals  = ''.join(f'{v:>+{col_w}.3f}' for v in row)
        print(f'{label:<{label_w}}{vals}', flush=True)

    print(sep, flush=True)
    age_str = f'{age_s/3600:.1f} h' if age_s > 60 else f'{age_s:.0f} s'
    print(
        f'  Max deviation: {max_dev:.3f} mm   '
        f'Fade height: {fade_h:.1f} mm   '
        f'Age: {age_str}',
        flush=True
    )
    print('', flush=True)


def reject_outlier_average(values: list) -> tuple:
    """
    Given probe-Z samples taken at the same physical point (nominally 3),
    discard whichever single sample is farthest — in total distance — from
    all the others, and return the average of the rest.

    This targets single-sample noise: debris on the nozzle or bed at the
    moment of contact, HES trigger jitter, a slightly early/late fire —
    things that produce one bad reading rather than a systematically
    skewed pair. With exactly 3 samples this is equivalent to "drop the
    one farthest from the median, average the remaining two."

    Returns (kept_average, outlier_value_or_None, spread) where spread is
    max(values) - min(values) of the ORIGINAL sample set (useful for
    logging/QA — a large spread even after rejection suggests a probing
    problem worth investigating rather than trusting blindly).
    """
    n = len(values)
    if n < 3:
        avg = sum(values) / n
        spread = (max(values) - min(values)) if n > 1 else 0.0
        return avg, None, spread

    total_dist = [
        sum(abs(values[i] - values[j]) for j in range(n) if j != i)
        for i in range(n)
    ]
    outlier_idx = max(range(n), key=lambda i: total_dist[i])
    kept = [values[i] for i in range(n) if i != outlier_idx]
    spread = max(values) - min(values)
    return sum(kept) / len(kept), values[outlier_idx], spread


def bilinear_interpolate(mesh_offsets: list, probe_x: list, probe_y: list,
                         query_x: float, query_y: float) -> float:
    """
    Bilinear interpolation of Z offsets at (query_x, query_y).

    mesh_offsets[row][col]  — row 0 = lowest Y,  col 0 = lowest X
    probe_x / probe_y       — sorted position lists for columns / rows
    """
    # Clamp to grid bounds
    x = max(probe_x[0], min(probe_x[-1], query_x))
    y = max(probe_y[0], min(probe_y[-1], query_y))

    # Find bounding column indices
    xi = len(probe_x) - 2
    for i in range(len(probe_x) - 1):
        if probe_x[i] <= x <= probe_x[i + 1]:
            xi = i
            break

    # Find bounding row indices
    yi = len(probe_y) - 2
    for i in range(len(probe_y) - 1):
        if probe_y[i] <= y <= probe_y[i + 1]:
            yi = i
            break

    x0, x1 = probe_x[xi], probe_x[xi + 1]
    y0, y1 = probe_y[yi], probe_y[yi + 1]

    tx = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
    ty = (y - y0) / (y1 - y0) if y1 != y0 else 0.0

    z00 = mesh_offsets[yi][xi]
    z10 = mesh_offsets[yi][xi + 1]
    z01 = mesh_offsets[yi + 1][xi]
    z11 = mesh_offsets[yi + 1][xi + 1]

    return (z00 * (1 - tx) * (1 - ty) +
            z10 * tx       * (1 - ty) +
            z01 * (1 - tx) * ty +
            z11 * tx       * ty)


def apply_mesh_compensation(z_commanded: float, x: float, y: float,
                            mesh_data: dict) -> float:
    """
    Apply mesh bed leveling with fade-height.

    The offset is full-strength at z=0 and linearly fades to zero at
    FADE_HEIGHT_MM.  Above the fade height the G-code Z is returned unchanged.

    Sign convention (matches MakerBot Z axis):
      positive offset → bed is HIGH at this XY → Z must increase to maintain gap
      negative offset → bed is LOW  at this XY → Z must decrease
    """
    if not mesh_data:
        return z_commanded

    fade_height = mesh_data.get('fade_height', FADE_HEIGHT_MM)
    fade = max(0.0, 1.0 - z_commanded / fade_height)
    if fade <= 0.0:
        return z_commanded

    offset = bilinear_interpolate(
        mesh_data['offsets'],
        mesh_data['probe_x'],
        mesh_data['probe_y'],
        x, y
    )
    return round(z_commanded + offset * fade, 4)


# =============================================================================
# G-code Parsing Functions
# =============================================================================

def _g1_move(params: dict) -> dict:
    """Build a single move command dict from an axis-params dict."""
    return {
        "command": {
            "function": "move",
            "metadata": {
                "relative": {"x": False, "y": False, "z": False, "a": False}
            },
            "parameters": params,
            "tags": ["Move"]
        }
    }


def parse_g1_command(line, mesh=None):
    """
    Parse a G1 line and return one move command (dict) or a list of move
    commands when mesh compensation is active and the move needs
    subdivision to follow the bed contour.

    Segmentation ensures the Z offset applied at each point correctly
    reflects the mesh height at that XY location, not just the endpoint.
    Without segmentation, a 200 mm infill line would get the mesh offset
    of the final position applied for the ENTIRE line.

    Segment breakpoints are chosen adaptively (see `subdivide` below):
    flat regions of the bed get long segments, curved regions get short
    ones, keeping the slope change at every junction small so the
    firmware's motion planner doesn't need to decelerate through it.
    """
    global last_known_values

    # Capture start position BEFORE parsing this line
    # (last_known_values is updated token-by-token below)
    start_x = last_known_values['x']
    start_y = last_known_values['y']
    start_z = last_known_values['z']   # raw commanded Z, NOT compensated
    start_a = last_known_values['a']

    # Parse tokens — PrusaSlicer 2.9+ may append inline comments: F6240;_WIPE
    params = {}
    for part in line.split():
        try:
            if part.startswith('X'):
                params['x'] = float(part[1:].split(';')[0])
                last_known_values['x'] = params['x']
            elif part.startswith('Y'):
                params['y'] = float(part[1:].split(';')[0])
                last_known_values['y'] = params['y']
            elif part.startswith('Z'):
                params['z'] = float(part[1:].split(';')[0])
                last_known_values['z'] = params['z']
            elif part.startswith('A'):
                params['a'] = float(part[1:].split(';')[0])
                last_known_values['a'] = params['a']
            elif part.startswith('F'):
                params['feedrate'] = float(part[1:].split(';')[0]) / 60
                last_known_values['feedrate'] = params['feedrate']
        except (ValueError, IndexError) as e:
            logging.debug(f'Skipping G1 token {part!r}: {e}')

    # Fill in missing axes with last known values
    for axis in ['x', 'y', 'z', 'a', 'feedrate']:
        if axis not in params:
            params[axis] = last_known_values[axis]

    # ── No mesh: single command, no compensation ──────────────────────────────
    if not mesh:
        return _g1_move(params)

    # ── With mesh: check XY travel distance ────────────────────────────────
    dx = params['x'] - start_x
    dy = params['y'] - start_y
    dist_xy = math.sqrt(dx * dx + dy * dy)

    if dist_xy <= MESH_MIN_SEGMENT_MM:
        # Short move — single point compensation at destination
        params['z'] = apply_mesh_compensation(params['z'], params['x'], params['y'], mesh)
        return _g1_move(params)

    # ── Fade-aware segmentation guard ───────────────────────────────────────
    # If the fade factor at the current Z is so small that the maximum possible
    # Z correction across the ENTIRE move would be less than the chord
    # tolerance, skip segmentation entirely.  This avoids generating collinear
    # (but separately commanded) segments above the fade height, which causes
    # visible deceleration artefacts without providing any meaningful
    # correction.
    fade_height = mesh.get('fade_height', FADE_HEIGHT_MM)
    fade_at_z   = max(0.0, 1.0 - params['z'] / fade_height)
    if fade_at_z <= 0.0:
        # Above fade height — compensation is exactly zero, no point segmenting
        return _g1_move(params)

    max_mesh_dev = max(abs(v) for row in mesh['offsets'] for v in row)
    if max_mesh_dev * fade_at_z < MESH_CHORD_TOLERANCE_MM:
        # Correction so tiny it's sub-threshold — apply at endpoint, no segments
        params['z'] = apply_mesh_compensation(params['z'], params['x'], params['y'], mesh)
        return _g1_move(params)

    # ── Long move: subdivide adaptively based on local mesh curvature ───────
    # Instead of a fixed segment length, walk the move and only insert a
    # breakpoint where the mesh-compensated Z actually deviates from a
    # straight chord by more than MESH_CHORD_TOLERANCE_MM (checked via
    # recursive bisection, same idea as adaptive curve flattening).
    #
    # Flat regions of the bed collapse to one long segment (up to
    # MESH_MAX_SEGMENT_MM). Regions where the interpolated surface bends —
    # e.g. crossing a probe-grid cell boundary, or a local high/low spot —
    # get subdivided further, down to MESH_MIN_SEGMENT_MM. This keeps
    # segment-to-segment slope changes small everywhere, rather than forcing
    # the same fixed segmentation onto flat and curved regions alike, which
    # is what was producing junction-deviation slowdowns at essentially
    # random points along a move.
    dz   = params['z'] - start_z   # commanded Z change (uncompensated)
    da   = params['a'] - start_a
    feed = params['feedrate']

    def z_at(t):
        """Mesh-compensated Z at fraction t along this move."""
        x = start_x + dx * t
        y = start_y + dy * t
        return apply_mesh_compensation(start_z + dz * t, x, y, mesh)

    def subdivide(t0, t1, z0, z1, depth, out):
        # Valid ONLY when [t0, t1] lies within a single bilinear cell — in that
        # case Z(t) is at most quadratic, so a single midpoint sample is a
        # mathematically exact test for curvature (a quadratic's maximum
        # deviation from its chord always occurs at the midpoint). Callers
        # must not hand this a span that crosses a grid line.
        tm = (t0 + t1) / 2.0
        z_true  = z_at(tm)
        z_chord = (z0 + z1) / 2.0
        seg_len = dist_xy * (t1 - t0)

        needs_split = (
            (abs(z_true - z_chord) > MESH_CHORD_TOLERANCE_MM
             or seg_len > MESH_MAX_SEGMENT_MM)
            and seg_len > MESH_MIN_SEGMENT_MM
            and depth < MESH_MAX_SUBDIVISION_DEPTH
        )
        if needs_split:
            subdivide(t0, tm, z0, z_true, depth + 1, out)
            subdivide(tm, t1, z_true, z1, depth + 1, out)
        else:
            out.append(t1)

    def grid_crossings(probe_x, probe_y):
        """
        Fractions t in (0, 1) where this move crosses a probe grid line —
        the only points where the bilinear interpolation gradient can
        discontinuously change. These MUST be explicit breakpoints: no
        amount of curvature sampling can substitute for them, because the
        surface isn't even continuous in slope there.
        """
        ts = set()
        if dx != 0:
            for gx in probe_x:
                t = (gx - start_x) / dx
                if 0.0 < t < 1.0:
                    ts.add(t)
        if dy != 0:
            for gy in probe_y:
                t = (gy - start_y) / dy
                if 0.0 < t < 1.0:
                    ts.add(t)
        # Merge near-duplicate crossings (e.g. passing near a grid corner)
        # so we don't create degenerate near-zero-length cells.
        merged = []
        min_gap_t = (MESH_MIN_SEGMENT_MM / dist_xy) * 0.1
        for t in sorted(ts):
            if not merged or t - merged[-1] > min_gap_t:
                merged.append(t)
        return merged

    # Cell-confined spans: [0, crossing_1, crossing_2, ..., 1]. Each span is
    # guaranteed to lie within a single bilinear cell, so the curvature check
    # inside subdivide() is exact rather than a spot-sample that can miss
    # curvature between cell boundaries.
    cell_bounds = [0.0] + grid_crossings(mesh['probe_x'], mesh['probe_y']) + [1.0]

    breakpoints = []
    z_prev = z_at(0.0)
    for t0, t1 in zip(cell_bounds[:-1], cell_bounds[1:]):
        z1 = z_at(t1)
        subdivide(t0, t1, z_prev, z1, 0, breakpoints)
        z_prev = z1

    commands = []
    for t in breakpoints:
        seg_x = start_x + dx * t
        seg_y = start_y + dy * t
        seg_z = start_z + dz * t          # commanded Z at this fraction
        commands.append(_g1_move({
            'x':        round(seg_x, 4),
            'y':        round(seg_y, 4),
            'z':        apply_mesh_compensation(seg_z, seg_x, seg_y, mesh),
            'a':        round(start_a + da * t, 4),
            'feedrate': feed,
        }))
    return commands


def parse_m109_command(line):
    """
    Parses the M109 command to set the extruder temperature and wait.
    NOTE: The MakerBot Smart Extruder+ firmware locks the heater temperature
    once a print starts. Temperature commands in the toolpath are ignored by
    the toolhead hardware. The print temperature is set once via start.json
    using meta.json's extruder_temperature field.
    """
    global first_m109_temperature
    logging.debug(f"Parsing M109 command: {line}")

    for part in line.split():
        if part.startswith('P'):
            try:
                temp = int(float(part[1:].split(';')[0]))
                if first_m109_temperature is None:
                    first_m109_temperature = temp
                    logging.info(f"Set first_m109_temperature to {temp} from M109")
                return {
                    "command": {
                        "function": "set_temperature_target",
                        "metadata": {},
                        "parameters": {"index": 0, "temperature": temp},
                        "tags": []
                    }
                }
            except ValueError as e:
                logging.error(f"Failed to parse M109 temperature: {e}")
    return None


def parse_m104_command(line):
    logging.debug(f"Parsing M104 command: {line}")
    for part in line.split():
        if part.startswith('P'):
            try:
                temp = int(float(part[1:].split(';')[0]))
                logging.info(f"M104 set_temperature_target {temp}")
                return {
                    "command": {
                        "function": "set_temperature_target",
                        "metadata": {},
                        "parameters": {"index": 0, "temperature": temp},
                        "tags": []
                    }
                }
            except ValueError as e:
                logging.error(f"Failed to parse M104 temperature: {e}")
    return None


def parse_m106_command(line):
    """
    Parses M106 (fan on + set duty cycle).
    Uses the C-library command names that the printer's jsontoolpath parser
    actually recognises: 'toggle_fan' with 'value' key, and 'fan_duty' with
    'value' key.  These differ from the Python pymachine wrapper names but match
    what the C library expects in the toolpath.
    """
    logging.debug(f"Parsing M106 command: {line}")
    commands = []
    commands.append({
        "command": {
            "function": "toggle_fan",
            "metadata": {},
            "parameters": {"index": 0, "value": True},
            "tags": []
        }
    })
    for part in line.split():
        if part.startswith('S') or part.startswith('P'):
            try:
                duty = float(part[1:].split(';')[0]) / 255.0
                commands.append({
                    "command": {
                        "function": "fan_duty",
                        "metadata": {},
                        "parameters": {"index": 0, "value": round(duty, 4)},
                        "tags": []
                    }
                })
                logging.info(f"M106 fan_duty {duty:.3f}")
                break
            except ValueError as e:
                logging.error(f"Failed to parse M106 duty: {e}")
    return commands


def parse_m107_command():
    """M107 — fan off."""
    return {
        "command": {
            "function": "toggle_fan",
            "metadata": {},
            "parameters": {"index": 0, "value": False},
            "tags": []
        }
    }


def extract_gcode_comments(line):
    """
    Extracts specific information from G-code comments, including filament used and printing time.
    """
    global extrusion_distance, printing_time
    #logging.debug(f"Processing comment: {line}")

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
    #else:
        #logging.debug(f"Comment did not match: {line}")


def parse_line(line, mesh=None):
    """
    Parses a single line of G-code.
    Delegates to the appropriate handler based on the command type.
    Bad individual lines are logged and skipped — they never abort the whole job.
    """
    line = line.strip()
    if line.startswith(";"):
        extract_gcode_comments(line)
    elif line.startswith("G1"):
        try:
            return parse_g1_command(line, mesh=mesh)
        except Exception as e:
            logging.debug(f'Skipping G1 line ({e}): {line[:80]}')
    elif line.startswith("M109"):
        try:
            return parse_m109_command(line)
        except Exception as e:
            logging.debug(f'Skipping M109 line ({e}): {line[:80]}')
    elif line.startswith("M104"):
        try:
            return parse_m104_command(line)
        except Exception as e:
            logging.debug(f'Skipping M104 line ({e}): {line[:80]}')
    elif line.startswith("M106"):
        try:
            return parse_m106_command(line)
        except Exception as e:
            logging.debug(f'Skipping M106 line ({e}): {line[:80]}')
    elif line.startswith("M107"):
        return parse_m107_command()
    return None


def process_gcode_file(input_path, mesh=None):
    """
    Processes the G-code file and extracts JSON commands.
    If mesh is provided, Z coordinates are mesh-compensated with fade height.
    """
    if mesh:
        logging.info(f"Processing G-code with mesh compensation (fade height={mesh.get('fade_height', FADE_HEIGHT_MM)}mm)")
    json_commands = []
    try:
        with open(input_path, 'r') as gcode_file:
            for line in gcode_file:
                # Parse each line of the G-code file
                parsed_command = parse_line(line, mesh=mesh)
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
    Expected formats:
        - "Shape-Box T3_2.gcode"
        - "cable clip T3_2.gcode"
        - "Shape-Box T3_2 (172.16.11.3).gcode"
        - "my model name T3_2 (172.16.11.3).gcode"
    Returns the printer identifier, e.g., "T3_2"
    """
    # Remove .gcode extension first
    base_name = re.sub(r'\.gcode$', '', filename, flags=re.IGNORECASE)
    
    # Remove IP address in parentheses if present
    base_name = re.sub(r'\s*\(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\)\s*$', '', base_name)
    
    # The printer name should be the last whitespace-separated token
    # Split on whitespace and get the last part
    parts = base_name.split()
    if parts:
        name = parts[-1]
        logging.info(f"Extracted printer name: {name} from filename: {filename}")
        return name
    
    logging.warning(f"No printer name found in filename: {filename}")
    return None


def get_printer_ip(filename: str, timeout: float = 5.0) -> tuple:
    """
    Get printer IP address - first check filename for explicit IP, then try mDNS discovery.
    
    Args:
        filename: The output filename from PrusaSlicer
        timeout: mDNS discovery timeout
    
    Returns:
        Tuple of (ip_address, printer_name) or (None, None) if not found
    """
    # First, try to extract IP directly from filename
    ip = extract_ip_from_filename(filename)
    printer_name = extract_printer_name_from_filename(filename)
    
    if ip:
        logging.info(f"Using IP from filename: {ip}")
        return ip, printer_name or "printer"
    
    # If no IP in filename but we have a printer name, try mDNS discovery
    if printer_name:
        logging.info(f"No IP in filename, attempting mDNS discovery for '{printer_name}'")
        notify("MakerBot Print", f"Searching for {printer_name} on network...")
        
        ip = discover_printer_ip(printer_name, timeout=timeout)
        if ip:
            return ip, printer_name
        else:
            notify("MakerBot Print", f"Could not find {printer_name} on network")
    
    return None, printer_name


# =============================================================================
# Printer Discovery via mDNS
# =============================================================================

class MakerbotDiscoveryListener(ServiceListener):
    """Listener for mDNS printer discovery"""
    
    def __init__(self):
        self.printers = {}  # machine_name -> ip mapping
        self.printer_details = {}  # machine_name -> full details
        self._lock = threading.Lock()
    
    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            service_name = name.split('.')[0]
            
            # Get IP address
            ip = None
            if info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
            
            # Extract TXT record properties
            properties = {}
            if info.properties:
                for key, value in info.properties.items():
                    if isinstance(key, bytes):
                        key = key.decode('utf-8', errors='replace')
                    if isinstance(value, bytes):
                        value = value.decode('utf-8', errors='replace')
                    properties[key] = value
            
            # Get machine_name from properties (this is the friendly name like "T5_2")
            machine_name = properties.get('machine_name', service_name)
            
            logging.info(f"Discovered printer: {machine_name} at {ip} (service: {service_name})")
            
            with self._lock:
                self.printers[machine_name] = ip
                self.printer_details[machine_name] = {
                    'ip': ip,
                    'port': info.port,
                    'ssl_port': properties.get('ssl_port', '12309'),
                    'server': info.server,
                    'service_name': service_name,
                    'properties': properties
                }
    
    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        # Find and remove by matching service name
        with self._lock:
            to_remove = None
            for machine_name, details in self.printer_details.items():
                if details.get('service_name') == name.split('.')[0]:
                    to_remove = machine_name
                    break
            if to_remove:
                self.printers.pop(to_remove, None)
                self.printer_details.pop(to_remove, None)
                logging.info(f"Printer removed: {to_remove}")
    
    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)


def discover_printer_ip(printer_name: str, timeout: float = 5.0) -> str:
    """
    Discover a MakerBot printer's IP address by its machine_name using mDNS.
    
    Args:
        printer_name: The printer name to search for (e.g., "T5_2")
        timeout: How long to wait for discovery (seconds)
    
    Returns:
        IP address string or None if not found
    """
    logging.info(f"Searching for printer '{printer_name}' via mDNS...")
    
    zeroconf = Zeroconf()
    listener = MakerbotDiscoveryListener()
    browser = ServiceBrowser(zeroconf, "_makerbot-jsonrpc._tcp.local.", listener)
    
    try:
        start_time = time.time()
        while time.time() - start_time < timeout:
            with listener._lock:
                # Exact match (case-insensitive)
                for name, ip in listener.printers.items():
                    if name.lower() == printer_name.lower():
                        logging.info(f"Found printer '{name}' at {ip}")
                        return ip
            
            time.sleep(0.2)
        
        # Log available printers for debugging
        with listener._lock:
            if listener.printers:
                available = ", ".join(listener.printers.keys())
                logging.warning(f"Printer '{printer_name}' not found. Available: {available}")
            else:
                logging.warning("No MakerBot printers discovered on network")
        
        return None
        
    finally:
        zeroconf.close()


def discover_all_printers(timeout: float = 5.0) -> dict:
    """
    Discover all MakerBot printers on the network.
    
    Returns:
        Dictionary of machine_name -> ip_address
    """
    logging.info("Discovering all MakerBot printers on network...")
    
    zeroconf = Zeroconf()
    listener = MakerbotDiscoveryListener()
    browser = ServiceBrowser(zeroconf, "_makerbot-jsonrpc._tcp.local.", listener)
    
    try:
        time.sleep(timeout)
        with listener._lock:
            return dict(listener.printers)
    finally:
        zeroconf.close()


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
        self._auth_token = None
        self._recv_buffer = b""  # Persistent receive buffer across calls
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
        logging.info(f">>> SEND RPC [{request['id']}]: {method}")
        logging.debug(f">>> SEND FULL: {message.decode('utf-8')}")
        self._socket.sendall(message)
        
        return self._recv_response(request['id'])
    
    def _recv_response(self, expected_id: int, timeout: float = 30) -> dict:
        """Receive response for a specific request ID, skipping notifications.
        Uses a persistent buffer to avoid losing data between calls."""
        self._socket.settimeout(timeout)
        wait_start = time.monotonic()
        recv_count = 0
        
        while True:
            # First, try to extract a complete JSON object from existing buffer
            result = self._try_extract_json(expected_id)
            if result is not None:
                logging.debug(f"Response {expected_id} extracted after {recv_count} recv calls, {time.monotonic() - wait_start:.3f}s")
                return result
            
            # Need more data
            try:
                recv_count += 1
                logging.debug(f"Waiting for data (recv call #{recv_count}, elapsed {time.monotonic() - wait_start:.3f}s, buffer={len(self._recv_buffer)} bytes)...")
                chunk = self._socket.recv(8192)
                if not chunk:
                    logging.error("<<< RECV: Connection closed (empty chunk)")
                    raise ConnectionError("Connection closed")
                self._recv_buffer += chunk
                logging.debug(f"<<< RECV RAW ({len(chunk)} bytes, total buffer={len(self._recv_buffer)} bytes)")
                    
            except socket.timeout:
                logging.error(
                    f"<<< RECV TIMEOUT waiting for response {expected_id} after {time.monotonic() - wait_start:.3f}s. "
                    f"Buffer has {len(self._recv_buffer)} bytes. "
                    f"Buffer preview: {self._recv_buffer[:200]}"
                )
                raise TimeoutError(f"Timeout waiting for response {expected_id}")
    
    def _try_extract_json(self, expected_id: int):
        """Try to extract complete JSON objects from the persistent buffer.
        Returns the matching response dict, or None if not yet available.
        Uses a proper string-aware parser to handle braces inside strings."""
        
        while True:
            decoded = self._recv_buffer.decode('utf-8', errors='replace')
            
            # Find the start of a JSON object
            obj_start = decoded.find('{')
            if obj_start == -1:
                # No JSON object starting, clear non-JSON prefix
                self._recv_buffer = b""
                return None
            
            # Trim anything before the first '{'
            if obj_start > 0:
                self._recv_buffer = self._recv_buffer[obj_start:]
                decoded = self._recv_buffer.decode('utf-8', errors='replace')
            
            # Find the end of the JSON object using a string-aware depth counter
            end_idx = self._find_json_end(decoded)
            if end_idx is None:
                # Incomplete JSON object, need more data
                return None
            
            json_str = decoded[:end_idx + 1]
            # Remove this object from the buffer
            self._recv_buffer = self._recv_buffer[len(json_str.encode('utf-8')):]
            
            try:
                obj = json.loads(json_str)
            except json.JSONDecodeError as e:
                logging.warning(f"Failed to parse extracted JSON ({len(json_str)} chars): {e}")
                # Skip this malformed object and try again
                continue
            
            if 'id' in obj and obj['id'] == expected_id:
                if 'error' in obj:
                    logging.error(f"<<< RECV RESPONSE [{expected_id}] ERROR: {json.dumps(obj)}")
                else:
                    logging.info(f"<<< RECV RESPONSE [{expected_id}]: OK")
                return obj
            elif 'method' in obj:
                logging.info(f"<<< RECV NOTIFICATION: {obj.get('method')}")
                # Continue loop to try extracting the next object
            else:
                logging.info(f"<<< RECV RESPONSE [id={obj.get('id')}] (not {expected_id}), skipping")
                # Continue loop
    
    @staticmethod
    def _find_json_end(s: str) -> int:
        """Find the index of the closing '}' of the first top-level JSON object,
        correctly handling strings (with escaped quotes) so braces inside
        strings are not miscounted. Returns None if the object is incomplete."""
        depth = 0
        in_string = False
        escape = False
        
        for i, ch in enumerate(s):
            if escape:
                escape = False
                continue
            
            if ch == '\\' and in_string:
                escape = True
                continue
            
            if ch == '"':
                in_string = not in_string
                continue
            
            if in_string:
                continue
            
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
        
        return None  # Incomplete
    
    def _drain_pending_responses(self):
        """Non-blocking check for any pending responses from the printer"""
        import select
        try:
            # Check if there's data available without blocking
            readable, _, _ = select.select([self._socket], [], [], 0)
            if readable:
                # There's data waiting - read it
                self._socket.setblocking(False)
                try:
                    data = self._socket.recv(8192)
                    if data:
                        logging.info(f"<<< PENDING DATA ({len(data)} bytes): {data.decode('utf-8', errors='replace')}")
                except BlockingIOError:
                    pass
                finally:
                    self._socket.setblocking(True)
        except Exception as e:
            logging.debug(f"_drain_pending_responses error: {e}")
    
    def _create_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    
    def _fresh_authorize(self) -> str:
        """Perform fresh TLS authorization and return token"""
        logging.info('Starting TLS authorization...')

        status(f'>>> Press the button on  {self.printer_name}  to authorise <<<', 'stage')
        status('Waiting for button press (60 s timeout)...', 'info')

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
            
            logging.info('Waiting for printer authorization (button press required)...')

            data = sock.recv(8192)
            response = json.loads(data.decode('utf-8'))

            token = response.get('result', {}).get('one_time_token', '')
            if token:
                logging.info(f'Got authorization token: {token}')
                status('Authorised', 'ok')
                self._auth_token = token
            else:
                raise RuntimeError(f'No token in response: {response}')
            
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
        
        # Enable TCP keepalive to detect dead connections
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # macOS-specific keepalive tuning (start after 30s, interval 10s)
        try:
            TCP_KEEPALIVE = 0x10  # macOS TCP_KEEPALIVE
            self._socket.setsockopt(socket.IPPROTO_TCP, TCP_KEEPALIVE, 30)
        except (AttributeError, OSError) as e:
            logging.debug(f"Could not set TCP keepalive interval: {e}")
        
        self._socket.connect((self.ip, self.port))
        self._recv_buffer = b""  # Reset buffer on new connection
        
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
    
    def _set_acceleration(self):
        """
        Configure acceleration and jerk (max speed change at junctions) before printing.

        Z jerk is set to non-zero values so the motion planner can carry speed
        through the tiny Z transitions created by mesh-segmented moves.
        Without this, max_speed_change_mm_per_s.z = 0 forces a decelerate-to-zero
        at every segment junction where Z changes, causing visible stepping.

        Values chosen conservatively:
          max_speed_change_mm_per_s.z  = 5  mm/s  — junction Z speed change budget
          impulse_speed_limit_mm_per_s.z = 10 mm/s — instant Z jerk budget
        Both are well above the ~0.01-0.2 mm/s Z component in mesh segments,
        and harmless for normal layer-change moves (pure Z, no XY).
        """
        response = self._send_rpc("set_config", {
            "config": {
                "acceleration": {
                    "rate_mm_per_s_sq": {
                        "x": 400,
                        "y": 400,
                        "z": 100
                    },
                    "max_speed_change_mm_per_s": {
                        "x": 25,
                        "y": 25,
                        "z": 25
                    },
                    "impulse_speed_limit_mm_per_s": {
                        "x": 100,
                        "y": 100,
                        "z": 2
                    }
                },
                "max_speed_mm_per_second": {
                    "z": 30
                }
            }
        })
        if 'error' in response:
            logging.warning(f"set_config acceleration returned error: {response['error']}")
        else:
            logging.info(f"Acceleration set successfully: {response}")
    
    def _reconnect_and_reauth(self):
        """Close current socket, re-authorize via TLS, and reconnect."""
        logging.info("_reconnect_and_reauth: closing old socket...")
        if self._socket:
            try:
                self._socket.close()
            except Exception as e:
                logging.warning(f"Error closing old socket: {e}")
            self._socket = None
        self._recv_buffer = b""  # Clear stale buffer
        
        logging.info("_reconnect_and_reauth: re-authorizing...")
        token = self._fresh_authorize()
        logging.info("_reconnect_and_reauth: reconnecting...")
        self._connect_and_auth(token)
        logging.info("_reconnect_and_reauth: done")
    
    def _print_init(self, filename: str, retries: int = 3):
        """Initialize print job with transfer_wait.  Retries on ProcessAlreadyRunning."""
        for attempt in range(1, retries + 1):
            response = self._send_rpc("print", {
                "filepath": filename,
                "transfer_wait": True
            })
            if 'error' not in response:
                return  # success
            err = response['error']
            name = err.get('data', {}).get('name', '') if isinstance(err, dict) else ''
            if name == 'ProcessAlreadyRunningException' and attempt < retries:
                logging.warning(f"print_init attempt {attempt}: ProcessAlreadyRunning — waiting 3 s before retry")
                status(f'  Printer still busy (attempt {attempt}/{retries}) — retrying in 3 s…', 'warn')
                time.sleep(3)
                continue
            raise RuntimeError(f"print init failed: {err}")
    
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
        """Send file in chunks using put_raw, with retry and reconnect logic"""
        file_size = file_path.stat().st_size
        bytes_sent = 0
        crc = 0
        
        MAX_CHUNK_RETRIES = 3
        MAX_RECONNECTS = 2
        reconnect_count = 0
        
        logging.info(f"Transferring file data ({file_size} bytes, wait_for_response={WAIT_FOR_PUT_RAW_RESPONSE})...")
        
        # Log socket state before starting transfer
        try:
            peer = self._socket.getpeername()
            logging.info(f"Socket connected to {peer}, timeout={self._socket.gettimeout()}")
        except Exception as e:
            logging.error(f"Socket not connected at start of _put_raw_chunks: {e}")
            raise
        
        chunk_number = 0
        
        with open(file_path, 'rb') as f:
            while bytes_sent < file_size:
                chunk = f.read(BLOCK_SIZE)
                chunk_len = len(chunk)
                chunk_crc = zlib.crc32(chunk, crc)
                chunk_number += 1
                
                chunk_sent = False
                
                if WAIT_FOR_PUT_RAW_RESPONSE:
                    chunk_start_time = time.monotonic()
                    logging.info(f"Chunk #{chunk_number} at offset {bytes_sent}/{file_size} ({chunk_len} bytes) — starting put_raw RPC...")
                    
                    for retry in range(MAX_CHUNK_RETRIES):
                        try:
                            if retry > 0:
                                logging.warning(f"Retry {retry}/{MAX_CHUNK_RETRIES} for chunk #{chunk_number} at offset {bytes_sent}")
                                time.sleep(0.5 * retry)
                            
                            rpc_start = time.monotonic()
                            response = self._send_rpc("put_raw", {
                                "file_id": file_id,
                                "length": chunk_len
                            })
                            rpc_elapsed = time.monotonic() - rpc_start
                            logging.info(f"Chunk #{chunk_number} put_raw RPC response in {rpc_elapsed:.3f}s")
                            
                            if 'error' in response:
                                err_msg = response['error'].get('message', str(response['error'])) if isinstance(response['error'], dict) else str(response['error'])
                                logging.error(f"put_raw error response: {err_msg}")
                                
                                if retry < MAX_CHUNK_RETRIES - 1:
                                    logging.warning(f"put_raw returned error, retrying...")
                                    continue
                                else:
                                    raise RuntimeError(f"put_raw failed after {MAX_CHUNK_RETRIES} retries: {err_msg}")
                            
                            # RPC acknowledged — send the raw chunk data
                            send_start = time.monotonic()
                            logging.debug(f">>> SEND CHUNK #{chunk_number}: {chunk_len} bytes at offset {bytes_sent}")
                            self._socket.sendall(chunk)
                            send_elapsed = time.monotonic() - send_start
                            
                            chunk_elapsed = time.monotonic() - chunk_start_time
                            logging.info(f"Chunk #{chunk_number} complete: RPC={rpc_elapsed:.3f}s, sendall={send_elapsed:.3f}s, total={chunk_elapsed:.3f}s")
                            
                            # Small delay to let printer process the chunk
                            # This prevents overwhelming the printer's receive buffer
                            time.sleep(0.01)
                            
                            chunk_sent = True
                            break
                            
                        except (TimeoutError, socket.timeout) as e:
                            logging.warning(
                                f"Timeout on put_raw at chunk #{chunk_number}, offset {bytes_sent} "
                                f"(retry {retry + 1}/{MAX_CHUNK_RETRIES}), "
                                f"elapsed {time.monotonic() - chunk_start_time:.3f}s: {e}"
                            )
                            logging.warning(f"  recv_buffer has {len(self._recv_buffer)} bytes pending")
                            logging.warning(f"  recv_buffer preview: {self._recv_buffer[:200]}")
                            
                            if retry < MAX_CHUNK_RETRIES - 1:
                                continue
                            else:
                                logging.warning(f"All {MAX_CHUNK_RETRIES} retries failed for chunk at offset {bytes_sent}")
                                
                        except (ConnectionError, BrokenPipeError, OSError) as e:
                            logging.warning(f"Connection error on put_raw at chunk #{chunk_number}, offset {bytes_sent}: {e}")
                            break
                    
                    # If chunk wasn't sent, try reconnecting
                    if not chunk_sent:
                        if reconnect_count >= MAX_RECONNECTS:
                            raise RuntimeError(
                                f"Transfer failed at offset {bytes_sent}/{file_size} after "
                                f"{MAX_RECONNECTS} reconnection attempts"
                            )
                        
                        reconnect_count += 1
                        logging.warning(
                            f"Reconnecting (attempt {reconnect_count}/{MAX_RECONNECTS}) "
                            f"to resume transfer at offset {bytes_sent}..."
                        )
                        notify("MakerBot Print", f"Transfer stalled, reconnecting ({reconnect_count}/{MAX_RECONNECTS})...")
                        
                        try:
                            self._reconnect_and_reauth()
                            self._print_init(file_path.name)
                            self._process_method()
                            new_file_id, _ = self._put_init(file_path)
                            file_id = new_file_id
                            
                            f.seek(0)
                            bytes_sent = 0
                            crc = 0
                            chunk_number = 0
                            
                            logging.info("Reconnected successfully, restarting transfer from beginning")
                            notify("MakerBot Print", f"Reconnected to {self.printer_name}, resending...")
                            continue
                            
                        except Exception as reconnect_err:
                            logging.error(f"Reconnection failed: {reconnect_err}")
                            raise RuntimeError(
                                f"Transfer failed at offset {bytes_sent}/{file_size}. "
                                f"Reconnection failed: {reconnect_err}"
                            )
                
                else:
                    # Non-wait mode
                    request = {
                        "id": self._next_id(),
                        "jsonrpc": "2.0",
                        "method": "put_raw",
                        "params": {
                            "file_id": file_id,
                            "length": chunk_len
                        }
                    }
                    message = json.dumps(request).encode('utf-8')
                    logging.info(f">>> SEND RPC [{request['id']}]: put_raw (no-wait mode)")
                    self._socket.sendall(message)
                    
                    # Brief pause to avoid overwhelming printer
                    time.sleep(0.005)
                    
                    logging.debug(f">>> SEND CHUNK: {chunk_len} bytes")
                    self._socket.sendall(chunk)
                    chunk_sent = True
                
                if chunk_sent:
                    crc = chunk_crc
                    bytes_sent += chunk_len
                    reconnect_count = 0
                    progress = (bytes_sent / file_size) * 100
                    if progress % 10 < (BLOCK_SIZE / file_size * 100):
                        logging.info(f'Transfer progress: {progress:.1f}% ({bytes_sent}/{file_size})')
                        status(f'  {progress:5.1f}%  ({bytes_sent:,} / {file_size:,} bytes)', 'xfer')
        
        logging.info(f"File transfer complete: {bytes_sent} bytes, CRC: {crc & 0xffffffff}")
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
    
    def _tls_send_rpc(self, sock, method: str, params: dict = None, request_id: int = 0) -> dict:
        """Send JSON-RPC request over a TLS socket and return the parsed response."""
        request = {
            "id": request_id,
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        message = json.dumps(request).encode('utf-8')
        print(f"\n{'='*60}")
        print(f">>> TLS REQUEST  [{request['id']}] method='{method}'")
        print(f"{'='*60}")
        print(json.dumps(request, indent=2))
        sock.sendall(message)

        # Receive response
        data = b""
        sock.settimeout(60)
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            # Try to parse complete JSON objects from buffer
            try:
                decoded = data.decode('utf-8', errors='replace')
                # Check if we have a complete JSON object
                depth = 0
                for i, ch in enumerate(decoded):
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            json_str = decoded[:i+1]
                            response = json.loads(json_str)
                            print(f"\n{'='*60}")
                            print(f"<<< TLS RESPONSE [{response.get('id', '?')}]")
                            print(f"{'='*60}")
                            print(json.dumps(response, indent=2))
                            return response
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

        # If we exit the loop, try to parse whatever we have
        if data:
            try:
                response = json.loads(data.decode('utf-8', errors='replace'))
                print(f"\n{'='*60}")
                print(f"<<< TLS RESPONSE [{response.get('id', '?')}]")
                print(f"{'='*60}")
                print(json.dumps(response, indent=2))
                return response
            except json.JSONDecodeError:
                print(f"\n<<< TLS RAW DATA: {data.decode('utf-8', errors='replace')}")
        return {}

    def _send_rpc_verbose(self, method: str, params: dict = None) -> dict:
        """Send JSON-RPC request and print both the request and response."""
        request = {
            "id": self._next_id(),
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        message = json.dumps(request).encode('utf-8')
        print(f"\n{'='*60}")
        print(f">>> TCP REQUEST  [{request['id']}] method='{method}'")
        print(f"{'='*60}")
        print(json.dumps(request, indent=2))
        logging.info(f">>> SEND RPC [{request['id']}]: {method}")
        self._socket.sendall(message)

        # Receive and print response(s)
        response = self._recv_response_verbose(request['id'])
        return response

    def _recv_response_verbose(self, expected_id: int, timeout: float = 30) -> dict:
        """Receive response, printing all messages (notifications + responses)."""
        self._socket.settimeout(timeout)
        buffer = b""

        while True:
            try:
                chunk = self._socket.recv(8192)
                if not chunk:
                    print("<<< Connection closed")
                    raise ConnectionError("Connection closed")
                buffer += chunk

                decoded = buffer.decode('utf-8', errors='replace')

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

                                if 'method' in obj:
                                    print(f"\n{'-'*60}")
                                    print(f"<<< TCP NOTIFICATION: method='{obj['method']}'")
                                    print(f"{'-'*60}")
                                    print(json.dumps(obj, indent=2))
                                elif 'id' in obj:
                                    label = "RESPONSE" if obj['id'] == expected_id else f"RESPONSE (id={obj['id']})"
                                    print(f"\n{'='*60}")
                                    print(f"<<< TCP {label} [{obj['id']}]")
                                    print(f"{'='*60}")
                                    print(json.dumps(obj, indent=2))
                                    if obj['id'] == expected_id:
                                        return obj
                                else:
                                    print(f"\n<<< TCP UNKNOWN MESSAGE:")
                                    print(json.dumps(obj, indent=2))

                                buffer = decoded[i+1:].encode('utf-8')
                                decoded = buffer.decode('utf-8', errors='replace')
                                i = -1

                            except json.JSONDecodeError:
                                pass
                    i += 1

            except socket.timeout:
                print(f"<<< TIMEOUT waiting for response {expected_id}")
                raise TimeoutError(f"Timeout waiting for response {expected_id}")

    def print_all_responses(self):
        """
        Go through the full authentication workflow and print every response
        from the printer, including decrypted TLS traffic.
        After authentication, queries additional introspection methods.
        """
        print(f"\n{'#'*60}")
        print(f"# MakerBot Response Dump")
        print(f"# Printer: {self.printer_name} ({self.ip})")
        print(f"# Port: {self.port}  |  SSL Port: {self.ssl_port}")
        print(f"{'#'*60}")

        # ── Step 1: TLS Authorization ──────────────────────────────
        print(f"\n\n{'*'*60}")
        print("* PHASE 1: TLS Authorization (port {0})".format(self.ssl_port))
        print(f"{'*'*60}")
        print("\n⏳ Press the button on the printer to authorize...\n")

        ctx = self._create_ssl_context()
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(120)

        try:
            raw_sock.connect((self.ip, self.ssl_port))
            tls_sock = ctx.wrap_socket(raw_sock, server_hostname=self.ip)

            # Print TLS connection info
            print(f"TLS version : {tls_sock.version()}")
            print(f"Cipher      : {tls_sock.cipher()}")
            peer_cert = tls_sock.getpeercert(binary_form=True)
            if peer_cert:
                print(f"Peer cert   : {len(peer_cert)} bytes")

            auth_response = self._tls_send_rpc(tls_sock, "authorize", {
                "makerbot_token": None,
                "username": "ANON",
                "local_secret": "undefined"
            }, request_id=0)

            token = auth_response.get('result', {}).get('one_time_token', '')
            if not token:
                print("\n❌ No token received. Aborting.")
                return

            print(f"\n✅ Got authorization token: {token}")

        finally:
            try:
                tls_sock.close()
            except Exception:
                pass
            raw_sock.close()

        # ── Step 2: TCP Handshake + Authenticate ──────────────────
        print(f"\n\n{'*'*60}")
        print("* PHASE 2: TCP Handshake & Authentication (port {0})".format(self.port))
        print(f"{'*'*60}")

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(30)
        self._socket.connect((self.ip, self.port))

        try:
            # Handshake
            hs_response = self._send_rpc_verbose("handshake")
            self.ssl_port = int(hs_response.get('result', {}).get('ssl_port', SSL_PORT))
            machine_name = hs_response.get('result', {}).get('machine_name')
            if machine_name:
                self.printer_name = machine_name

            # Authenticate
            auth_response = self._send_rpc_verbose("authenticate", {"access_token": token})
            if 'error' in auth_response:
                print(f"\n❌ Authentication failed: {auth_response['error']}")
                return
            print(f"\n✅ Authenticated successfully")

            # ── Step 3: Query introspection / info methods ────────
            print(f"\n\n{'*'*60}")
            print("* PHASE 3: Querying printer information")
            print(f"{'*'*60}")

            # List of methods to probe — these are common MakerBot JSON-RPC methods
            probe_methods = [
                ("get_system_information", {}),
                ("get_machine_config", {}),
                ("get_machine_status", {}),
                ("get_available_firmware", {}),
                ("get_queue_status", {}),
                ("get_statistics", {}),
                ("get_errors", {}),
            ]

            for method, params in probe_methods:
                try:
                    self._send_rpc_verbose(method, params)
                except Exception as e:
                    print(f"\n⚠️  {method} failed: {e}")

            print(f"\n\n{'#'*60}")
            print("# Done — all responses printed above")
            print(f"{'#'*60}\n")

        finally:
            if self._socket:
                self._socket.close()
                self._socket = None

    def _send_rpc_quiet(self, method: str, params: dict = None, timeout: float = 5) -> dict:
        """Send JSON-RPC request and return response without verbose printing.
        Uses a short timeout so brute-forcing doesn't hang on slow methods."""
        request = {
            "id": self._next_id(),
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        message = json.dumps(request).encode('utf-8')
        self._socket.sendall(message)

        self._socket.settimeout(timeout)
        buffer = b""
        while True:
            try:
                chunk = self._socket.recv(8192)
                if not chunk:
                    raise ConnectionError("Connection closed")
                buffer += chunk
                decoded = buffer.decode('utf-8', errors='replace')
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
                                if 'id' in obj and obj['id'] == request['id']:
                                    return obj
                                # Skip notifications / other responses
                                buffer = decoded[i+1:].encode('utf-8')
                                decoded = buffer.decode('utf-8', errors='replace')
                                i = -1
                            except json.JSONDecodeError:
                                pass
                    i += 1
            except socket.timeout:
                return {"error": {"message": "timeout"}}

    def _check_plate_variability(self):
        """Query get_machine_config and return the current plate_variability value."""
        resp = self._send_rpc_quiet("get_machine_config", {}, timeout=10)
        if resp and 'result' in resp:
            extra = resp['result'].get('extra_slicer_settings', {})
            return extra.get('plate_variability', None)
        return None

    def brute_force_methods(self):
        """
        Authenticate then try many different parameter structures with set_config
        to change extra_slicer_settings.plate_variability to a 5x5 array of
        alternating 0.0 and 5.0 values (mesh bed leveling test).
        After each attempt, verifies via get_machine_config whether the value changed.
        """
        # Build a 5x5 grid of alternating 0.0 and 5.0
        TARGET_GRID = []
        for row in range(5):
            row_data = []
            for col in range(5):
                row_data.append(0.0 if (row + col) % 2 == 0 else 5.0)
            TARGET_GRID.append(row_data)

        # Flat version (25 values)
        TARGET_FLAT = [val for row in TARGET_GRID for val in row]

        strategies = [
            # ── Using known winning pattern: config wrapper ──

            # Strategy 1: config wrapper, plate_variability as scalar 5.0
            ("set_config — config.extra_slicer_settings.plate_variability = 5.0", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": 0.0
                    }
                }
            }),

            # Strategy 2: config wrapper, plate_variability as flat 1D array
            ("set_config — config.extra_slicer_settings.plate_variability = flat 25-element array", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": TARGET_FLAT
                    }
                }
            }),

            
            # Strategy 3: config wrapper, plate_variability as 2D array
            ("set_config — config.extra_slicer_settings.plate_variability = 5x5 2D array", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": TARGET_GRID
                    }
                }
            }),



            # Strategy 3: config wrapper, plate_variability as dict with grid key
            ("set_config — config.extra_slicer_settings.plate_variability = {grid: 2D, size: 5}", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": {
                            "grid": TARGET_GRID,
                            "size": 5
                        }
                    }
                }
            }),

            # Strategy 4: config wrapper, plate_variability as dict with points + dimensions
            ("set_config — config.extra_slicer_settings.plate_variability = {points, rows, cols}", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": {
                            "points": TARGET_FLAT,
                            "rows": 5,
                            "cols": 5
                        }
                    }
                }
            }),

            # Strategy 5: config wrapper, entire extra_slicer_settings replaced
            ("set_config — config.extra_slicer_settings replaced entirely", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": TARGET_GRID
                    }
                }
            }),

            # ── Top-level param styles (in case config wrapper isn't needed for this) ──

            # Strategy 6: extra_slicer_settings at top level with 2D array
            ("set_config — extra_slicer_settings.plate_variability = 2D array (top-level)", {
                "extra_slicer_settings": {
                    "plate_variability": TARGET_GRID
                }
            }),

            # Strategy 7: extra_slicer_settings at top level with flat array
            ("set_config — extra_slicer_settings.plate_variability = flat array (top-level)", {
                "extra_slicer_settings": {
                    "plate_variability": TARGET_FLAT
                }
            }),

            # Strategy 8: plate_variability directly at top level as 2D
            ("set_config — plate_variability = 2D array (top-level)", {
                "plate_variability": TARGET_GRID
            }),

            # Strategy 9: plate_variability directly at top level as flat
            ("set_config — plate_variability = flat array (top-level)", {
                "plate_variability": TARGET_FLAT
            }),

            # ── key/value style ──

            # Strategy 10: key/value with dot path
            ("set_config — key='extra_slicer_settings.plate_variability' value=2D", {
                "key": "extra_slicer_settings.plate_variability",
                "value": TARGET_GRID
            }),

            # Strategy 11: key/value with dot path, flat
            ("set_config — key='extra_slicer_settings.plate_variability' value=flat", {
                "key": "extra_slicer_settings.plate_variability",
                "value": TARGET_FLAT
            }),

            # Strategy 12: key=extra_slicer_settings, value=dict
            ("set_config — key='extra_slicer_settings' value={plate_variability: 2D}", {
                "key": "extra_slicer_settings",
                "value": {
                    "plate_variability": TARGET_GRID
                }
            }),

            # ── name/value style ──

            # Strategy 13: name with dot path
            ("set_config — name='extra_slicer_settings.plate_variability' value=2D", {
                "name": "extra_slicer_settings.plate_variability",
                "value": TARGET_GRID
            }),

            # ── section/values style ──

            # Strategy 14: section + values
            ("set_config — section='extra_slicer_settings' values={plate_variability: 2D}", {
                "section": "extra_slicer_settings",
                "values": {
                    "plate_variability": TARGET_GRID
                }
            }),

            # ── set_machine_config variants ──

            # Strategy 15: set_machine_config config wrapper 2D
            ("set_machine_config — config.extra_slicer_settings.plate_variability = 2D", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": TARGET_GRID
                    }
                }
            }),

            # Strategy 16: set_machine_config top-level 2D
            ("set_machine_config — extra_slicer_settings.plate_variability = 2D (top-level)", {
                "extra_slicer_settings": {
                    "plate_variability": TARGET_GRID
                }
            }),

            # Strategy 17: set_machine_config config wrapper flat
            ("set_machine_config — config.extra_slicer_settings.plate_variability = flat", {
                "config": {
                    "extra_slicer_settings": {
                        "plate_variability": TARGET_FLAT
                    }
                }
            }),

            # Strategy 18: set_machine_config top-level flat
            ("set_machine_config — extra_slicer_settings.plate_variability = flat (top-level)", {
                "extra_slicer_settings": {
                    "plate_variability": TARGET_FLAT
                }
            }),
        ]

        print(f"\n{'#'*60}")
        print(f"# MakerBot plate_variability Brute-Force")
        print(f"# Printer: {self.printer_name} ({self.ip})")
        print(f"# Target: 5x5 alternating 0.0/5.0 grid")
        print(f"# Strategies to try: {len(strategies)}")
        print(f"{'#'*60}")
        print(f"\nTarget 5x5 grid:")
        for row in TARGET_GRID:
            print(f"  {row}")

        # ── Authenticate ──
        print(f"\n{'*'*60}")
        print("* PHASE 1: TLS Authorization")
        print(f"{'*'*60}")
        print("\n⏳ Press the button on the printer to authorize...\n")

        ctx = self._create_ssl_context()
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(120)

        try:
            raw_sock.connect((self.ip, self.ssl_port))
            tls_sock = ctx.wrap_socket(raw_sock, server_hostname=self.ip)

            auth_response = self._tls_send_rpc(tls_sock, "authorize", {
                "makerbot_token": None,
                "username": "ANON",
                "local_secret": "undefined"
            }, request_id=0)

            token = auth_response.get('result', {}).get('one_time_token', '')
            if not token:
                print("\n❌ No token received. Aborting.")
                return
            print(f"\n✅ Got authorization token: {token}")
        finally:
            try:
                tls_sock.close()
            except Exception:
                pass
            raw_sock.close()

        # ── Connect and authenticate over TCP ──
        print(f"\n{'*'*60}")
        print("* PHASE 2: TCP Handshake & Authentication")
        print(f"{'*'*60}")

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(30)
        self._socket.connect((self.ip, self.port))

        try:
            hs_response = self._send_rpc_quiet("handshake")
            self.ssl_port = int(hs_response.get('result', {}).get('ssl_port', SSL_PORT))
            machine_name = hs_response.get('result', {}).get('machine_name')
            if machine_name:
                self.printer_name = machine_name

            auth_resp = self._send_rpc_quiet("authenticate", {"access_token": token})
            if 'error' in auth_resp:
                print(f"\n❌ Authentication failed: {auth_resp['error']}")
                return
            print(f"✅ Authenticated as {self.printer_name}")

            # ── Read baseline ──
            print(f"\n{'*'*60}")
            print("* PHASE 3: Reading current plate_variability value")
            print(f"{'*'*60}")

            baseline = self._check_plate_variability()
            print(f"\n  Current plate_variability: {json.dumps(baseline)}")

            # ── Try each strategy ──
            print(f"\n{'*'*60}")
            print("* PHASE 4: Trying strategies to set plate_variability")
            print(f"{'*'*60}\n")

            success = False
            for idx, (description, params) in enumerate(strategies, 1):
                if description.startswith("set_machine_config"):
                    rpc_method = "set_machine_config"
                else:
                    rpc_method = "set_config"

                print(f"{'─'*60}")
                print(f"  Strategy {idx}/{len(strategies)}: {description}")
                print(f"  Method: {rpc_method}")
                params_preview = json.dumps(params, indent=4)
                if len(params_preview) > 400:
                    params_preview = params_preview[:400] + "\n    ... (truncated)"
                print(f"  Params: {params_preview}")

                try:
                    resp = self._send_rpc_quiet(rpc_method, params, timeout=10)
                except ConnectionError:
                    print(f"  ❌ Connection lost. Stopping.")
                    break
                except Exception as e:
                    print(f"  ❌ Exception: {e}")
                    continue

                # Print response
                if resp is None or resp == {}:
                    print(f"  ⏱  No response (timeout)")
                elif 'error' in resp:
                    err = resp['error']
                    err_msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
                    print(f"  ⚠️  Error: {err_msg}")
                else:
                    print(f"  📨 Response: {json.dumps(resp, indent=4)}")

                # Verify by reading config back
                print(f"  🔍 Verifying via get_machine_config...")
                try:
                    current = self._check_plate_variability()
                    print(f"     plate_variability = {json.dumps(current)}")

                    if current != baseline:
                        print(f"\n  🎉🎉🎉 SUCCESS! plate_variability value CHANGED!")
                        print(f"  ✅ Was: {json.dumps(baseline)}")
                        print(f"  ✅ Now: {json.dumps(current)}")
                        print(f"  ✅ Winning strategy: {description}")
                        print(f"  ✅ Method: {rpc_method}")
                        success = True
                        break
                    else:
                        print(f"     ↳ No change (still {json.dumps(current)})")
                except Exception as e:
                    print(f"     ↳ Verify failed: {e}")

                print()

            # ── Summary ──
            print(f"\n{'#'*60}")
            if success:
                print("# ✅ plate_variability SUCCESSFULLY CHANGED!")
            else:
                print("# ❌ None of the strategies changed plate_variability.")
                print("# The printer may not support this parameter as an array,")
                print("# or a different format is needed.")
            print(f"{'#'*60}\n")

        finally:
            if self._socket:
                self._socket.close()
                self._socket = None

    # =========================================================================
    # Mesh Bed Leveling — Probing
    # =========================================================================

    def _wait_for_process(self, process_id=None, timeout: float = 120) -> bool:
        """
        Poll get_system_information until the current process is complete (or absent).
        Returns True on success, False on timeout.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                resp = self._send_rpc("get_system_information")
                cp = resp.get('result', {}).get('current_process')
                if cp is None:
                    return True                          # no running process
                if cp.get('complete', False):
                    return True                          # completed normally
                if cp.get('error'):
                    logging.warning(f"Process error: {cp['error']}")
                    return False
                if process_id is not None and cp.get('id') != process_id:
                    return True                          # our process already finished
            except Exception as e:
                logging.debug(f"_wait_for_process poll error: {e}")
            time.sleep(0.5)
        logging.warning(f"_wait_for_process timed out after {timeout:.0f}s")
        return False

    def _run_machine_action(self, func: str, params: dict, timeout: float = 60) -> None:
        """
        Send machine_action_command, wait until the resulting process completes.
        Raises RuntimeError if the command is rejected by the printer.
        """
        resp = self._send_rpc("machine_action_command", {
            "machine_func": func,
            "params": params,
            "ignore_tool_errors": True
        })
        if 'error' in resp:
            raise RuntimeError(f"machine_action_command({func}) error: {resp['error']}")
        pid = resp.get('result', {}).get('id') if isinstance(resp.get('result'), dict) else None
        self._wait_for_process(pid, timeout=timeout)

    def _run_machine_query(self, func: str, params: dict = None):
        """
        Send machine_query_command and return the result value directly.
        Raises RuntimeError on error.
        """
        resp = self._send_rpc("machine_query_command", {
            "machine_func": func,
            "params": params or {}
        })
        if 'error' in resp:
            raise RuntimeError(f"machine_query_command({func}) error: {resp['error']}")
        return resp.get('result')

    def _wait_for_hes_acknowledge(self, index: int = 0, timeout: float = 10.0) -> None:
        """
        Poll toolhead_acknowledged_hes until the toolhead confirms the HES at
        `index` is armed and ready, or raise TimeoutError if it never does.

        Used before the very first probe of a mesh: every later point gets an
        incidental delay between configure_hes and find_knob_z from that
        point's retract move, which is enough time for the sensor to settle.
        The first point follows directly from the post-home travel move with
        nothing in between, so it needs an explicit readiness check instead.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            acknowledged = self._run_machine_query("toolhead_acknowledged_hes", {"index": index})
            if acknowledged:
                return
            time.sleep(0.05)
        raise TimeoutError(
            f"Toolhead did not acknowledge HES index {index} within {timeout}s "
            f"before the first probe — check the Smart Extruder+ connection"
        )

    def _wait_for_temperature(self, index: int, target: int,
                              timeout: float = 300) -> None:
        """
        Poll extruder temperature until it reaches `target` °C.
        Prints a live-updating line so the user can see heating progress.
        """
        status(f'Waiting for extruder → {target}°C…', 'info')
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            current = self._run_machine_query('get_temperature', {'index': index})
            if current is not None:
                print(f'\r  [{time.strftime("%H:%M:%S")}]   {current}°C / {target}°C   ',
                      end='', flush=True)
                if current >= target:
                    print(flush=True)
                    status(f'Extruder ready at {current}°C', 'ok')
                    return
            time.sleep(2)
        print(flush=True)
        raise RuntimeError(
            f'Extruder did not reach {target}°C within {timeout:.0f}s')

    def probe_bed_mesh(self, preheat_temp: int = 170,
                       print_temp: int = 0) -> dict:
        """
        Home the printer (XY then Z) and probe a MESH_GRID_X × MESH_GRID_Y grid
        using the HES probe on the Smart Extruder+.

        Each grid point is probed PROBE_SAMPLES_PER_POINT times (independent
        descents, not repeated reads of one contact event). The sample
        farthest from the other two is discarded and the remaining two are
        averaged — see reject_outlier_average() — to reduce the effect of a
        single bad trigger (debris, jitter) on that point's Z value.

        Returns a mesh_data dict suitable for apply_mesh_compensation(), or None
        if probing fails.  The dict contains mean-centred Z offsets so only the
        relative shape of the bed matters — absolute Z calibration is handled by
        the normal start sequence.

        preheat_temp — heat extruder to this °C before probing (reduces filament
                       interference with the HES sensor). Heating starts before
                       homing so the two happen concurrently.
        print_temp   — after probing, set extruder to this °C (0 = turn off).
        """
        status(f'Bed mesh probe  ({MESH_GRID_X}×{MESH_GRID_Y} = {MESH_GRID_X * MESH_GRID_Y} points'
               + (f',  preheat {preheat_temp}°C' if preheat_temp else '') + ')', 'stage')
        logging.info(f'Starting bed mesh probe ({MESH_GRID_X}×{MESH_GRID_Y} grid)…')

        try:
            # ── Start extruder heating (non-blocking — runs concurrently with homing) ──
            if preheat_temp > 0:
                status(f'Starting extruder heat to {preheat_temp}°C (will heat during homing)…', 'info')
                self._run_machine_action('set_temperature_target', {
                    'index': 0, 'temperature': preheat_temp
                }, timeout=5)

            # ── Home gantry (XY) ──────────────────────────────────────────────
            status('Homing XY gantry…', 'info')
            logging.info('Homing XY gantry…')
            resp = self._send_rpc('home', {'axes': ['gantry']})
            if 'error' in resp:
                raise RuntimeError(f'Gantry home failed: {resp["error"]}')
            pid = resp.get('result', {}).get('id') if isinstance(resp.get('result'), dict) else None
            if not self._wait_for_process(pid, timeout=60):
                raise RuntimeError('Gantry home timed out')
            status('XY homed', 'ok')

            # ── Home Z (full HES timeseries procedure) ────────────────────────
            status('Homing Z (HES timeseries — may take ~30 s)…', 'info')
            logging.info('Homing Z (HES)…')
            resp = self._send_rpc('home', {'axes': ['z']})
            if 'error' in resp:
                raise RuntimeError(f'Z home failed: {resp["error"]}')
            pid = resp.get('result', {}).get('id') if isinstance(resp.get('result'), dict) else None
            if not self._wait_for_process(pid, timeout=120):
                raise RuntimeError('Z home timed out')
            status('Z homed', 'ok')

            # ── Wait for extruder to reach preheat temp (concurrent with homing) ──
            if preheat_temp > 0:
                self._wait_for_temperature(0, preheat_temp, timeout=300)

                # Retract 10 mm to reduce ooze during probing
                status('Retracting filament 10 mm before probing…', 'info')
                self._run_machine_action('move_axis', {
                    'axis': 3, 'point_mm': -10.0,
                    'mm_per_second': 25.0, 'relative': True
                }, timeout=10)

            # Lift to safe Z before moving between probe points
            self._run_machine_action('move_axis', {
                'axis': 2, 'point_mm': MESH_SAFE_Z,
                'mm_per_second': MESH_LIFT_SPEED, 'relative': False
            }, timeout=15)

            # ── Probe grid ───────────────────────────────────────────────────
            z_contacts = []
            total_points = MESH_GRID_Y * MESH_GRID_X

            for yi, py in enumerate(MESH_PROBE_Y):
                for xi, px in enumerate(MESH_PROBE_X):
                    point_num = yi * MESH_GRID_X + xi + 1
                    logging.info(f"  [{point_num:2d}/{total_points}] probing ({px:+.0f}, {py:+.0f})…")

                    # Move to probe XY at safe Z (leave extruder relative)
                    self._run_machine_action("move", {
                        "point_mm": [px, py, MESH_SAFE_Z, 0.0],
                        "mm_per_second": MESH_XY_SPEED,
                        "relative": [False, False, False, True]
                    }, timeout=20)

                    # Take PROBE_SAMPLES_PER_POINT independent contact readings at
                    # this XY location, retracting fully clear of the bed between
                    # each so every sample is an independent descent-and-trigger,
                    # not just a repeated read of the same contact event.
                    samples = []
                    for sample_i in range(PROBE_SAMPLES_PER_POINT):
                        if sample_i > 0:
                            # Retract clear of the bed before repeating the probe
                            self._run_machine_action("move_axis", {
                                "axis": 2, "point_mm": MESH_REPROBE_Z,
                                "mm_per_second": MESH_LIFT_SPEED, "relative": False
                            }, timeout=15)

                        # Arm HES sensor, then wait for the toolhead to confirm it
                        # is READY (not latched from a previous contact).
                        #
                        # Two failure modes this prevents:
                        #
                        #  a) First-probe crash — Without the wait, the second
                        #     configure_hes (called here inside the loop) fires
                        #     find_knob_z before the HES has settled after the
                        #     XY travel move.  No trigger arrives and the descent
                        #     runs all the way into the bed.
                        #
                        #  b) Alternating +0.287 mm ghost readings — The lift from
                        #     Z≈0 back to MESH_SAFE_Z passes through the HES spring
                        #     hysteresis release point (~0.287 mm).  The Bronx
                        #     toolhead latches this release event.  configure_hes
                        #     clears the latch in hardware, but only once
                        #     toolhead_acknowledged_hes returns True is the sensor
                        #     guaranteed to be in a clean, un-triggered state.
                        #     Without the wait, find_knob_z fires immediately at
                        #     the latch position on every other probe.
                        self._run_machine_action("configure_hes", {
                            "index": 0, "exponent": 0, "threshold": 2000
                        }, timeout=5)
                        self._wait_for_hes_acknowledge(index=0, timeout=15.0)

                        #seems to help clear buffer or someother firmware quirk
                        pos = self._run_machine_query("get_axes_position")
                        status(f"pre-read {pos}")

                        # Descend until HES fires (or limit reached)
                        self._run_machine_action("find_knob_z", {
                            "limit": MESH_PROBE_LIMIT,
                            "speed": MESH_PROBE_SPEED
                        }, timeout=30)

                        # Read Z at contact point
                        pos = self._run_machine_query("get_axes_position")
                        #status(f"pre-read 2 {pos[2]:+.3f} mm")
                        #pos = self._run_machine_query("get_move_buffer_position")
                        if pos is None:
                            raise RuntimeError(
                                f"No position returned at probe point ({px}, {py}), "
                                f"sample {sample_i + 1}/{PROBE_SAMPLES_PER_POINT}"
                            )
                        samples.append(pos[2])
                        logging.info(f"     sample {sample_i + 1}/{PROBE_SAMPLES_PER_POINT}: "
                                     f"Z = {pos[2]:+.3f} mm")

                        # Live readout — printed as each probe completes, not just
                        # in the final mesh table, so a bad point is visible the
                        # moment it happens instead of after the whole grid finishes.
                        depth = pos[2] - MESH_SAFE_Z   # negative = mm descended below travel height
                        no_trigger_z = MESH_SAFE_Z + MESH_PROBE_LIMIT
                        loc = f"[{point_num:3d}/{total_points}] ({px:+.0f},{py:+.0f})"
                        if abs(pos[2] - no_trigger_z) < NO_TRIGGER_EPSILON_MM:
                            status(f"{loc} sample {sample_i + 1}: Z={pos[2]:+.3f}  "
                                   f"(descended {depth:+.3f} mm) — NEVER TRIGGERED, "
                                   f"hit search limit", 'warn')
                        elif abs(depth) < EARLY_TRIGGER_DEPTH_WARN_MM:
                            status(f"{loc} sample {sample_i + 1}: Z={pos[2]:+.3f}  "
                                   f"(descended {depth:+.3f} mm) — SUSPICIOUSLY SHALLOW, "
                                   f"likely a false/latched trigger, not real contact", 'warn')
                        else:
                            status(f"{loc} sample {sample_i + 1}: Z={pos[2]:+.3f}  "
                                   f"(descended {depth:+.3f} mm)", 'probe')

                    z_contact, outlier, spread = reject_outlier_average(samples)
                    if outlier is not None:
                        logging.info(f"     → rejected {outlier:+.3f} mm outlier "
                                     f"(spread {spread:.3f} mm) → Z = {z_contact:+.3f} mm")
                        status(f"   → recorded Z = {z_contact:+.3f} mm  "
                               f"(rejected {outlier:+.3f} outlier, spread {spread:.3f} mm)", 'data')
                    else:
                        logging.info(f"     → Z = {z_contact:+.3f} mm")
                        status(f"   → recorded Z = {z_contact:+.3f} mm", 'data')
                    if spread > PROBE_SPREAD_WARN_MM:
                        # Spread between samples is suspiciously large relative to
                        # the probe search range — flag it, don't silently trust it.
                        logging.warning(f"     ⚠ large sample spread ({spread:.3f} mm) "
                                         f"at ({px:+.0f}, {py:+.0f}) — check for debris "
                                         f"or a loose HES connection")
                        status(f"   ⚠ large sample spread ({spread:.3f} mm) at "
                               f"({px:+.0f}, {py:+.0f}) — check for debris or a loose "
                               f"HES connection", 'warn')
                    z_contacts.append(z_contact)

                    # Lift clear before next XY move
                    self._run_machine_action("move_axis", {
                        "axis": 2, "point_mm": MESH_SAFE_Z,
                        "mm_per_second": MESH_LIFT_SPEED, "relative": False
                    }, timeout=15)

            # No park — the print's own start.json re-homes everything.
            # Just wait for the last move_axis to fully settle before returning.
            status('Waiting for printer to become fully idle…', 'info')
            self._wait_for_process(timeout=30)

            # Unretract 8 mm (leaves 2 mm net retraction; the purge line handles the rest)
            if preheat_temp > 0:
                status('Unretracting filament 8 mm…', 'info')
                self._run_machine_action('move_axis', {
                    'axis': 3, 'point_mm': 8.0,
                    'mm_per_second': 10.0, 'relative': True
                }, timeout=10)

            # ── Set post-probe extruder temperature ──────────────────────────────
            if print_temp > 0:
                status(f'Setting extruder to print temperature ({print_temp}°C)…', 'info')
            else:
                status('Turning extruder off (start.json will re-heat)…', 'info')
            self._run_machine_action('set_temperature_target', {
                'index': 0, 'temperature': print_temp
            }, timeout=5)

            # ── Compute mean-centred mesh offsets ────────────────────────────
            mean_z = sum(z_contacts) / len(z_contacts)
            offsets = []
            idx = 0
            for yi in range(MESH_GRID_Y):
                row = []
                for xi in range(MESH_GRID_X):
                    row.append(round(z_contacts[idx] - mean_z, 4))
                    idx += 1
                offsets.append(row)

            max_dev = max(abs(v) for row in offsets for v in row)
            logging.info(f"Mesh complete — mean Z={mean_z:+.3f}  max deviation={max_dev:.3f} mm")

            mesh_result = {
                'probe_x': MESH_PROBE_X,
                'probe_y': MESH_PROBE_Y,
                'offsets': offsets,
                'fade_height': FADE_HEIGHT_MM,
                'mean_z': round(mean_z, 4),
                'z_contacts': z_contacts,
                'timestamp': time.time(),
                'printer_name': self.printer_name
            }

            status('Mesh measured — offset grid:', 'ok')
            display_mesh(mesh_result)

            return mesh_result

        except Exception as e:
            logging.error(f"Bed mesh probe failed: {e}", exc_info=True)
            notify("MakerBot Print", f"Mesh probe failed — printing without compensation")
            return None

    # =========================================================================
    # Print Sending
    # =========================================================================

    def _do_send(self, file_path: Path) -> None:
        """
        Send a .makerbot file assuming the socket is already open and authenticated.
        Raises on error.
        """
        t0 = time.monotonic()
        file_size = file_path.stat().st_size

        status('Configuring printer settings…', 'info')
        self._set_acceleration()

        status('Initialising print job…', 'info')
        self._print_init(file_path.name)

        status('Starting print process on printer…', 'info')
        self._process_method()

        status(f'Uploading  {file_path.name}  ({file_size:,} bytes)…', 'stage')
        file_id, _ = self._put_init(file_path)
        crc = self._put_raw_chunks(file_path, file_id)

        status('Finalising transfer…', 'info')
        self._put_term(file_id, file_size, crc)

        status(f'File delivered in {time.monotonic() - t0:.1f}s', 'ok')
        logging.info(f'File sent in {time.monotonic() - t0:.1f}s')

    def send_print(self, file_path: Path):
        """Complete workflow: authenticate, connect, then send print."""
        logging.info(f'Starting print job for: {file_path}')

        try:
            t0 = time.monotonic()
            token = self._fresh_authorize()
            self._connect_and_auth(token)
            self._do_send(file_path)
            logging.info(f'Print job sent successfully in {time.monotonic() - t0:.1f}s')
            return True

        except Exception as e:
            logging.error(f'Failed to send print: {e}', exc_info=True)
            status(f'Failed to send print: {e}', 'error')
            raise

        finally:
            if self._socket:
                self._socket.close()
                self._socket = None


# =============================================================================
# Main File Creation and Sending
# =============================================================================

def create_makerbot_file(gcode_path, mesh=None, output_name: str = ''):
    """
    Convert a G-code file to a .makerbot archive.

    output_name — preferred base for naming the .makerbot (SLIC3R_PP_OUTPUT_NAME).
                  Falls back to the env var then gcode_path.
    mesh        — if provided, Z offsets are applied with fade-height compensation.
    """
    true_output_name = (output_name
                        or os.getenv('SLIC3R_PP_OUTPUT_NAME')
                        or gcode_path)

    logging.info(f"Creating Makerbot file for G-code: {gcode_path}"
                 + (" (with mesh compensation)" if mesh else " (no mesh)"))

    try:
        # Process G-code (applies mesh compensation if provided)
        json_commands = process_gcode_file(gcode_path, mesh=mesh)
        logging.info(f"Toolpath: {len(json_commands)} commands")

        # Prepend motion-quality setup commands.
        # toggle_acceleration_lookahead enables the printer's look-ahead buffer
        # so it can plan smooth junctions between consecutive short segments
        # (including the mesh-segmented moves).  Uses 'value' key to match the
        # C-library parser convention (same pattern as toggle_fan).
        setup_cmds = [
            {"command": {"function": "toggle_acceleration",
                         "metadata": {}, "parameters": {"value": True}, "tags": []}},
            {"command": {"function": "toggle_acceleration_lookahead",
                         "metadata": {}, "parameters": {"value": True}, "tags": []}},
        ]
        json_commands = setup_cmds + json_commands

        # Write print.jsontoolpath to a known absolute path (never relative/CWD)
        json_file_path = os.path.join(script_dir, 'print.jsontoolpath')
        with open(json_file_path, 'w') as json_file:
            json.dump(json_commands, json_file, separators=(',', ':'))

        # Modify meta.json with extracted print statistics
        modify_meta_json()

        # Package into .makerbot (a renamed ZIP)
        base_name  = os.path.splitext(os.path.basename(true_output_name))[0]
        zip_path   = os.path.join(script_dir, base_name + '.zip')
        makerbot_name = os.path.join(downloads_folder, base_name + '.makerbot')

        with zipfile.ZipFile(zip_path, 'w',
                             compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zipf:
            zipf.write(json_file_path, arcname='print.jsontoolpath')
            zipf.write(meta_file_path, arcname='meta.json')

        os.replace(zip_path, makerbot_name)  # atomic even across same FS
        logging.info(f"Makerbot file saved to: {makerbot_name}")
        return makerbot_name
    except Exception as e:
        logging.error(f"Error while creating Makerbot file: {e}")
        return None


def get_gcode_print_temperature(gcode_path: str) -> int:
    """
    Quickly scan a G-code file for the first M109 P<temp> (heated wait command)
    and return the temperature as an integer.  Returns 0 if not found.
    """
    try:
        with open(gcode_path, 'r') as f:
            for line in f:
                ls = line.strip()
                if ls.startswith('M109') or ls.startswith('M104'):
                    for part in ls.split():
                        if part.startswith('P'):
                            try:
                                return int(float(part[1:].split(';')[0]))
                            except ValueError:
                                pass
    except Exception:
        pass
    return 0


def send_to_printer(makerbot_file: str, printer_ip: str, printer_name: str):
    """Send the .makerbot file to the printer (legacy helper)."""
    logging.info(f"Sending {makerbot_file} to printer at {printer_ip} ({printer_name})")
    try:
        printer = MakerbotPrinter(printer_ip, printer_name=printer_name)
        printer.send_print(Path(makerbot_file))
        logging.info("Print sent successfully!")
        return True
    except Exception as e:
        logging.error(f"Failed to send print to {printer_ip}: {e}")
        return False


def run_send_subprocess(gcode_path: str, output_name: str = ''):
    """
    Open a macOS Terminal window and bring it to front.
    The subprocess handles mDNS discovery, printer selection, probing, and sending.

    A lock file prevents multiple rapid calls (e.g. PrusaSlicer calling the
    script once per object) from opening more than one Terminal window.
    """
    # ── Spawn-once lock ───────────────────────────────────────────────────────
    # If this function was called within the last 30 s (same output file or any
    # file), skip spawning another window.  This stops the infinite-loop that
    # occurs when the --mesh-print condition is accidentally not matched and the
    # script falls back into normal mode inside the Terminal subprocess.
    lock_path = os.path.join(script_dir, '_spawn.lock')
    COOLDOWN = 30  # seconds
    now = time.time()
    try:
        if os.path.exists(lock_path):
            age = now - os.path.getmtime(lock_path)
            if age < COOLDOWN:
                logging.info(
                    f'Spawn suppressed — lock is only {age:.1f}s old '
                    f'(cooldown {COOLDOWN}s).  Duplicate PrusaSlicer call?')
                return
        with open(lock_path, 'w') as fh:
            fh.write(f'{os.getpid()}\n{output_name}\n{now}')
    except Exception as e:
        logging.warning(f'Spawn lock error (continuing anyway): {e}')
    # ─────────────────────────────────────────────────────────────────────────

    script_path = os.path.abspath(__file__)

    sh_path = os.path.join(script_dir, '_makerbot_run.sh')
    sh_lines = [
        '#!/bin/bash',
        f'PYTHON="{sys.executable}"',
        '',
        f'"$PYTHON" "{script_path}" --mesh-print "{gcode_path}" "{output_name}"',
        'EXIT_CODE=$?',
        'echo ""',
        'echo "--------------------------------------------"',
        'if [ $EXIT_CODE -eq 0 ]; then',
        '    echo "  All done."',
        'else',
        '    echo "  Exited with error code $EXIT_CODE."',
        'fi',
        'echo "--------------------------------------------"',
        'read -rp "Press Enter to close..." _',
    ]
    with open(sh_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(sh_lines) + '\n')
    os.chmod(sh_path, 0o755)

    # 'activate' brings the Terminal window to the foreground
    osa = (
        f'tell application "Terminal"\n'
        f'    activate\n'
        f'    do script "{sh_path}"\n'
        f'end tell'
    )
    subprocess.Popen(['osascript', '-e', osa])
    logging.info(f'Opened Terminal window  sh={sh_path}')


if __name__ == "__main__":
    # ── Mesh-print subprocess mode ────────────────────────────────────────────
    # Called by run_send_subprocess().  Receives the raw G-code path, probes (or
    # loads) the mesh, creates the .makerbot with compensation, and sends it —
    # all over a single authenticated connection (one button press).
    #
    # Invoked as:  python3 converter.py --mesh-print <gcode_path> <output_name>
    # Only 2 extra args — printer is chosen interactively via mDNS discovery.
    if len(sys.argv) >= 3 and sys.argv[1] == '--mesh-print':
        gcode_file  = sys.argv[2]
        output_name = sys.argv[3] if len(sys.argv) >= 4 else gcode_file

        # ── Banner ────────────────────────────────────────────────────────
        print('', flush=True)
        print('============================================', flush=True)
        print('  MakerBot Bridge  —  Printer Selection', flush=True)
        print('============================================', flush=True)
        logging.info(f'mesh-print subprocess: gcode={gcode_file}')

        # ── mDNS Discovery ────────────────────────────────────────────────
        status('Discovering printers on network (5 s)…', 'stage')
        printers_dict = discover_all_printers(timeout=5.0)

        if not printers_dict:
            status('No MakerBot printers found on network.', 'error')
            status('Creating .makerbot file for manual transfer.', 'warn')
            makerbot_file = create_makerbot_file(gcode_file, mesh=None,
                                                 output_name=output_name)
            if makerbot_file:
                status(f'Saved: {makerbot_file}', 'ok')
            sys.exit(0)

        # Numbered list of discovered printers + rescan option
        while True:
            printer_list = sorted(printers_dict.items())   # [(name, ip), …]
            print('', flush=True)
            for i, (name, ip) in enumerate(printer_list, 1):
                print(f'  {i}.  {name}  ({ip})', flush=True)
            print(f'  0.  Rescan network', flush=True)
            print('', flush=True)

            # User selects printer
            try:
                raw = input(f'Select printer [1–{len(printer_list)}] or 0 to rescan: ').strip()
                choice = int(raw)
            except (ValueError, EOFError):
                choice = 1

            if choice == 0:
                status('Rescanning network (5 s)…', 'info')
                printers_dict = discover_all_printers(timeout=5.0)
                if not printers_dict:
                    status('Still no printers found. Try again or press Ctrl-C to quit.', 'warn')
                continue

            idx = choice - 1
            if 0 <= idx < len(printer_list):
                break
            print(f'  Please enter a number between 0 and {len(printer_list)}.')

        printer_name, printer_ip = printer_list[idx]
        status(f'Selected:  {printer_name}  ({printer_ip})', 'ok')

        # ── Mesh selection ────────────────────────────────────────────────
        existing_mesh = load_mesh_from_file(printer_name)
        use_cached = False

        if existing_mesh:
            age_h = (time.time() - existing_mesh['timestamp']) / 3600
            print('\n─── Saved Mesh ─────────────────────────────────────────────', flush=True)
            print(f'  Age: {age_h:.1f} h', flush=True)
            display_mesh(existing_mesh)
            try:
                raw = input('  Run fresh probe? [Y/n]  (n = use saved mesh): ').strip().lower()
                use_cached = raw in ('n', 'no')
            except EOFError:
                use_cached = False
            if use_cached:
                status('Using saved mesh.', 'ok')
            else:
                status('Running fresh probe.', 'info')
        else:
            status('No saved mesh — fresh probe will run.', 'info')

        # ── Extract print temperature from gcode ──────────────────────────
        print_temp = get_gcode_print_temperature(gcode_file)
        if print_temp:
            status(f'Gcode print temperature: {print_temp}°C', 'info')

        # ── Connect, probe/load, create, send ─────────────────────────────
        printer = MakerbotPrinter(printer_ip, printer_name=printer_name)
        mesh = None
        connected = False

        try:
            status(f'Connecting to  {printer_name}  at  {printer_ip}', 'stage')
            token = printer._fresh_authorize()
            status('Establishing TCP connection…', 'info')
            printer._connect_and_auth(token)
            status('Connected and authenticated', 'ok')
            connected = True

            if use_cached:
                mesh = existing_mesh
            else:
                mesh = printer.probe_bed_mesh(preheat_temp=170,
                                              print_temp=print_temp)
                if mesh:
                    save_mesh_to_file(printer_name, mesh)

        except Exception as e:
            logging.error(f'Mesh probe phase failed: {e} — will print without compensation')
            status(f'Probe / auth failed: {e}', 'error')
            status('Continuing without mesh compensation.', 'warn')
            mesh = None

        # ── Create .makerbot (with or without mesh) ───────────────────────
        tag = 'with mesh compensation' if mesh else 'WITHOUT mesh compensation'
        status(f'Creating .makerbot file  ({tag})…', 'stage')
        makerbot_file = create_makerbot_file(gcode_file, mesh=mesh,
                                             output_name=output_name)
        if not makerbot_file:
            status('Failed to create .makerbot file — aborting.', 'error')
            sys.exit(1)
        status(f'Package ready:  {os.path.basename(makerbot_file)}', 'ok')

        # ── Send print (reuse existing connection if still alive) ─────────
        if printer_ip:
            status(f'Sending print to  {printer_name}…', 'stage')
            try:
                if connected and printer._socket:
                    printer._do_send(Path(makerbot_file))
                else:
                    status('Connection lost — re-authorising…', 'warn')
                    printer.send_print(Path(makerbot_file))
            except Exception as e:
                logging.error(f'Send failed: {e}', exc_info=True)
                status(f'Send failed: {e}', 'error')
                sys.exit(1)
            finally:
                if printer._socket:
                    try:
                        printer._socket.close()
                    except Exception:
                        pass
            status(f'Print job sent to {printer_name} ✔', 'ok')

        sys.exit(0)

    # ── Legacy --send mode (kept for backwards compatibility) ─────────────────
    if len(sys.argv) >= 5 and sys.argv[1] == "--send":
        makerbot_file = sys.argv[2]
        printer_ip   = sys.argv[3]
        printer_name = sys.argv[4]
        logging.info(f"Send subprocess started for {makerbot_file} to {printer_ip} ({printer_name})")
        send_to_printer(makerbot_file, printer_ip, printer_name)
        sys.exit(0)
    
    # Check for brute-force mode
    if len(sys.argv) >= 2 and sys.argv[1] == "--brute-force":
        if len(sys.argv) < 3:
            print("Usage: python converter.py --brute-force <printer_hostname_or_ip>")
            sys.exit(1)

        target = sys.argv[2]
        print(f"Resolving printer: {target}")

        ip_match = re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', target)
        if ip_match:
            printer_ip = target
            printer_name = target
        else:
            print(f"Searching for '{target}' via mDNS (10s timeout)...")
            printer_ip = discover_printer_ip(target, timeout=10.0)
            printer_name = target
            if not printer_ip:
                print(f"❌ Could not find printer '{target}' on the network.")
                all_printers = discover_all_printers(timeout=5.0)
                if all_printers:
                    print("Available printers:")
                    for name, ip in all_printers.items():
                        print(f"  {name} -> {ip}")
                else:
                    print("No MakerBot printers found on the network.")
                sys.exit(1)

        print(f"Connecting to {printer_name} at {printer_ip}...\n")
        printer = MakerbotPrinter(printer_ip, printer_name=printer_name)
        printer.brute_force_methods()
        sys.exit(0)

    # Check for printresponse mode
    if len(sys.argv) >= 2 and sys.argv[1] == "--printresponse":
        if len(sys.argv) < 3:
            print("Usage: python converter.py --printresponse <printer_hostname_or_ip>")
            sys.exit(1)

        target = sys.argv[2]
        print(f"Resolving printer: {target}")

        # Determine if target is an IP address or a hostname to discover
        ip_match = re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', target)
        if ip_match:
            printer_ip = target
            printer_name = target
        else:
            # Treat as a printer machine name — discover via mDNS
            print(f"Searching for '{target}' via mDNS (10s timeout)...")
            printer_ip = discover_printer_ip(target, timeout=10.0)
            printer_name = target
            if not printer_ip:
                print(f"❌ Could not find printer '{target}' on the network.")
                # Show what is available
                print("\nDiscovering all printers...")
                all_printers = discover_all_printers(timeout=5.0)
                if all_printers:
                    print("Available printers:")
                    for name, ip in all_printers.items():
                        print(f"  {name} -> {ip}")
                else:
                    print("No MakerBot printers found on the network.")
                sys.exit(1)

        print(f"Connecting to {printer_name} at {printer_ip}...\n")
        printer = MakerbotPrinter(printer_ip, printer_name=printer_name)
        printer.print_all_responses()
        sys.exit(0)

    # Check for discovery mode
    if len(sys.argv) >= 2 and sys.argv[1] == "--discover":
        zeroconf = Zeroconf()
        listener = MakerbotDiscoveryListener()
        browser = ServiceBrowser(zeroconf, "_makerbot-jsonrpc._tcp.local.", listener)
        
        print("Searching for MakerBot printers (10 seconds)...")
        time.sleep(10.0)
        
        print("\nDiscovered MakerBot printers:")
        print("-" * 60)
        with listener._lock:
            for machine_name, info in listener.printer_details.items():
                print(f"\nMachine Name: {machine_name}")
                print(f"  IP Address: {info['ip']}")
                print(f"  Port: {info['port']}")
                print(f"  SSL Port: {info['ssl_port']}")
                print(f"  Service Name: {info['service_name']}")
                print(f"  Server: {info['server']}")
                print(f"  Properties:")
                for key, value in info['properties'].items():
                    print(f"    {key}: {value}")
        
        zeroconf.close()
        sys.exit(0)
    
    # Normal mode: PrusaSlicer post-processing call
    if len(sys.argv) < 2:
        logging.error("No G-code file provided. Usage: python script.py <path_to_gcode>")
        sys.exit(1)

    gcode_file_path = sys.argv[1]
    logging.info(f"Received G-code file: {gcode_file_path}")

    true_output_name = os.getenv("SLIC3R_PP_OUTPUT_NAME", gcode_file_path)

    # Copy gcode to a stable path BEFORE the subprocess runs.
    # PrusaSlicer deletes its temp file once this main script exits, but the
    # subprocess runs minutes later (after bed probing). Without a stable copy
    # the subprocess finds the file gone and produces a 2 KB empty zip.
    stable_gcode = os.path.join(script_dir, '_pending_print.gcode')
    try:
        shutil.copy2(gcode_file_path, stable_gcode)
        logging.info(f"Copied gcode to stable path: {stable_gcode}")
    except Exception as e:
        logging.warning(f"Could not copy gcode ({e}) — passing original path")
        stable_gcode = gcode_file_path

    # Always open a Terminal subprocess — it handles mDNS discovery, printer
    # selection, mesh probing, and sending interactively.
    run_send_subprocess(stable_gcode, output_name=true_output_name)
    logging.info("Main script exiting — subprocess running in background")
    sys.exit(0)