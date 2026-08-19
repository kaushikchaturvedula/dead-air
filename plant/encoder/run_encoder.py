"""L1 source + encoder -- ffmpeg ABR ladder, exporter, and L1 fault injection.

Runs ffmpeg in realtime (-re) producing a 4-rung ABR HLS ladder with burned-in
timecode, exports encoder health on :9103, and accepts chaos commands that
reshape what the encoder produces.

Metric names come from brief §5 verbatim -- `encoder_fps`, `dropped_frames`,
`packager_segment_lag` -- deliberately unprefixed.

L1 chaos modes (brief §5's fault menu). Each must produce a DISTINCT telemetry
signature, because the agent's job is to tell them apart:

    none             healthy
    black_source     input -> color=black. Every delivery metric stays green;
                     only the pixels are wrong.
    ladder_collapse  the 1080p rung stops being produced. Delivered bitrate
                     drops with no rebuffering.
    ladder_mismatch  720p content is muxed into the 1080p rung. NOTHING moves
                     in telemetry -- the manifest, bitrate, cadence and status
                     codes are all identical to healthy.
    segment_gap      every Nth segment is deleted after the packager writes it,
                     producing a 404 storm in every region at once.

Modes that change the encode restart ffmpeg in place; segment_gap does not.
Switching is a POST, not a container restart, so the agent-week demo can move
between faults in seconds.

    GET  /metrics    Prometheus exposition
    GET  /chaos      current mode
    POST /chaos      {"mode": "...", "severity": N}
    GET  /healthz    liveness
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("ENCODER_METRICS_PORT", "9103"))
HLS_ROOT = os.environ.get("HLS_ROOT", "/data/hls")
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "4"))
FPS = int(os.environ.get("ENCODER_FPS_TARGET", "30"))
PRESET = os.environ.get("ENCODER_PRESET", "ultrafast")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

HEALTHY_SOURCE = os.environ.get(
    "ENCODER_SOURCE", f"testsrc2=size=1920x1080:rate={FPS}"
)
BLACK_SOURCE = f"color=black:size=1920x1080:rate={FPS}"

# name, width, height, bitrate, maxrate, bufsize
LADDER = [
    ("1080p", 1920, 1080, "5000k", "5350k", "7500k"),
    ("720p", 1280, 720, "3000k", "3210k", "4500k"),
    ("480p", 854, 480, "1500k", "1600k", "2250k"),
    ("360p", 640, 360, "800k", "856k", "1200k"),
]
# packager_segment_lag is reported for the CANONICAL ladder at all times, not
# just the rungs currently being produced. A killed rung must show its lag
# climbing -- if the series simply disappeared, `ladder_collapse` would look
# like a scrape failure instead of an encoder fault.
CANONICAL_RUNGS = [r[0] for r in LADDER]

L1_MODES = ("none", "black_source", "ladder_collapse", "ladder_mismatch",
            "segment_gap")
RESTART_MODES = ("none", "black_source", "ladder_collapse", "ladder_mismatch")

_lock = threading.Lock()
_fps = 0.0
_dropped = 0
_up = 0
_started = time.time()
_chaos = {"mode": "none", "severity": 0}
_restart = threading.Event()
_proc = None
_gap_deleted = 0


# --- ffmpeg command --------------------------------------------------------

def active_ladder(mode):
    """ladder_collapse kills the top rung; everything else keeps all four."""
    if mode == "ladder_collapse":
        return LADDER[1:]
    return LADDER


def build_command(mode):
    source = BLACK_SOURCE if mode == "black_source" else HEALTHY_SOURCE
    rungs = active_ladder(mode)

    drawtext = (
        f"drawtext=fontfile={FONT}:text='%{{pts\\:hms}}'"
        ":x=(w-tw)/2:y=h-th-40:fontsize=64:fontcolor=white"
        ":box=1:boxcolor=black@0.6:boxborderw=10"
    )
    splits = "".join(f"[s{i}]" for i in range(len(rungs)))
    chains = [f"[0:v]{drawtext},split={len(rungs)}{splits}"]
    for i, (name, w, h, *_rest) in enumerate(rungs):
        if mode == "ladder_mismatch" and name == "1080p":
            # Brief §5: "mux 720p content into the 1080p rung". Downscale to
            # 720p and blow it back up to 1920x1080, so the rung carries
            # genuinely 720p-worth of detail at its full advertised resolution
            # and bitrate. The manifest, segment cadence and byte sizes are all
            # unchanged -- which is exactly why telemetry cannot see it.
            chains.append(f"[s{i}]scale=1280:720,scale={w}:{h}[v{i}]")
        else:
            chains.append(f"[s{i}]scale={w}:{h}[v{i}]")
    filter_complex = ";".join(chains)

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-re",
           "-f", "lavfi", "-i", source,
           "-filter_complex", filter_complex]

    for i, (name, w, h, bv, maxrate, bufsize) in enumerate(rungs):
        cmd += ["-map", f"[v{i}]", f"-c:v:{i}", "libx264",
                f"-b:v:{i}", bv, f"-maxrate:v:{i}", maxrate,
                f"-bufsize:v:{i}", bufsize]

    gop = SEGMENT_SECONDS * FPS
    cmd += [
        "-preset", PRESET, "-pix_fmt", "yuv420p",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments",
        "-hls_segment_type", "mpegts",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", " ".join(
            f"v:{i},name:{name}" for i, (name, *_r) in enumerate(rungs)
        ),
        "-hls_segment_filename", f"{HLS_ROOT}/%v/seg_%05d.ts",
        f"{HLS_ROOT}/%v/index.m3u8",
        "-progress", "pipe:1", "-nostats",
    ]
    return cmd


# --- segment_gap -----------------------------------------------------------

def gap_watcher():
    """Delete every Nth segment after the packager writes it.

    Brief §5 puts this fault at the origin, not the edge, and that placement is
    the whole diagnosis: a gap here 404s in EVERY region simultaneously, which
    is what separates 'packager fault' from 'one edge is sick'.
    """
    global _gap_deleted
    seen = set()
    counter = 0
    while True:
        with _lock:
            active = _chaos["mode"] == "segment_gap"
            every = max(2, _chaos["severity"] or 7)
        if not active:
            seen.clear()
            counter = 0
            time.sleep(1.0)
            continue
        for rung in CANONICAL_RUNGS:
            d = os.path.join(HLS_ROOT, rung)
            try:
                entries = sorted(e.name for e in os.scandir(d)
                                 if e.name.endswith(".ts"))
            except FileNotFoundError:
                continue
            for name in entries:
                key = f"{rung}/{name}"
                if key in seen:
                    continue
                seen.add(key)
                counter += 1
                if counter % every == 0:
                    try:
                        os.unlink(os.path.join(d, name))
                        with _lock:
                            _gap_deleted += 1
                    except OSError:
                        pass
        if len(seen) > 3000:
            seen = set(sorted(seen)[-1000:])
        time.sleep(0.5)


# --- metrics ---------------------------------------------------------------

def segment_lag():
    now = time.time()
    out = {}
    for name in CANONICAL_RUNGS:
        d = os.path.join(HLS_ROOT, name)
        newest = 0.0
        try:
            for entry in os.scandir(d):
                if entry.name.endswith(".ts"):
                    m = entry.stat().st_mtime
                    if m > newest:
                        newest = m
        except FileNotFoundError:
            pass
        out[name] = (now - newest) if newest else (now - _started)
    return out


def render_metrics():
    with _lock:
        fps, dropped, up = _fps, _dropped, _up
        mode = _chaos["mode"]
        gaps = _gap_deleted
    lines = [
        "# HELP encoder_fps Frames per second currently produced by the encoder.\n",
        "# TYPE encoder_fps gauge\n",
        f"encoder_fps {fps}\n",
        "# HELP dropped_frames Cumulative frames dropped by the encoder.\n",
        "# TYPE dropped_frames counter\n",
        f"dropped_frames {dropped}\n",
        "# HELP encoder_up 1 while the ffmpeg process is running.\n",
        "# TYPE encoder_up gauge\n",
        f"encoder_up {up}\n",
        "# HELP encoder_rungs_active Ladder rungs currently being produced.\n",
        "# TYPE encoder_rungs_active gauge\n",
        f"encoder_rungs_active {len(active_ladder(mode))}\n",
        "# HELP encoder_chaos_active 1 when an L1 fault is injected.\n",
        "# TYPE encoder_chaos_active gauge\n",
        f"encoder_chaos_active {0 if mode == 'none' else 1}\n",
        "# HELP packager_segments_deleted_total Segments removed by the "
        "segment_gap fault.\n",
        "# TYPE packager_segments_deleted_total counter\n",
        f"packager_segments_deleted_total {gaps}\n",
        "# HELP packager_segment_lag Seconds since this rendition last "
        "produced an HLS segment.\n",
        "# TYPE packager_segment_lag gauge\n",
    ]
    for name, lag in segment_lag().items():
        lines.append(f'packager_segment_lag{{rendition="{name}"}} {lag:.3f}\n')
    return "".join(lines)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/metrics":
            self._reply(200, render_metrics())
        elif path == "/chaos":
            with _lock:
                self._reply(200, json.dumps(_chaos) + "\n", "application/json")
        elif path == "/healthz":
            with _lock:
                self._reply(200 if _up else 503, "ok\n" if _up else "down\n")
        else:
            self._reply(404, "not found\n")

    def do_POST(self):
        if urlparse(self.path).path != "/chaos":
            self._reply(404, "not found\n")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._reply(400, '{"error":"invalid json"}\n', "application/json")
            return
        mode = str(payload.get("mode", "none"))
        severity = int(payload.get("severity", 0) or 0)
        if mode not in L1_MODES:
            self._reply(400, json.dumps(
                {"error": f"unknown mode {mode!r}", "modes": list(L1_MODES)}
            ) + "\n", "application/json")
            return

        with _lock:
            previous = _chaos["mode"]
            _chaos["mode"] = mode
            _chaos["severity"] = severity
        print(f"[encoder] chaos -> mode={mode} severity={severity}", flush=True)

        # Only restart the encode when the produced output actually changes.
        if (mode in RESTART_MODES or previous in RESTART_MODES) and mode != previous:
            _restart.set()
            if _proc and _proc.poll() is None:
                _proc.terminate()

        self._reply(200, json.dumps(
            {"mode": mode, "severity": severity,
             "restart": mode in RESTART_MODES or previous in RESTART_MODES}
        ) + "\n", "application/json")

    def log_message(self, *args):
        pass


# --- supervisor ------------------------------------------------------------

def prepare_dirs():
    for name in CANONICAL_RUNGS:
        os.makedirs(os.path.join(HLS_ROOT, name), exist_ok=True)


def clear_segments(rungs=None):
    for name in (rungs if rungs is not None else CANONICAL_RUNGS):
        d = os.path.join(HLS_ROOT, name)
        try:
            for entry in os.scandir(d):
                if entry.name.endswith((".ts", ".m3u8")):
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
        except FileNotFoundError:
            pass


def run_once():
    """Launch ffmpeg for the current mode and stream its progress until it
    exits or a chaos change requests a restart."""
    global _proc, _fps, _dropped, _up
    with _lock:
        mode = _chaos["mode"]
    cmd = build_command(mode)
    rungs = [r[0] for r in active_ladder(mode)]
    print(f"[encoder] starting mode={mode} rungs={','.join(rungs)}", flush=True)

    _proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)

    def drain_stderr(p):
        for line in p.stderr:
            line = line.strip()
            if line and ("rror" in line or "Invalid" in line):
                print("[ffmpeg]", line, flush=True)

    threading.Thread(target=drain_stderr, args=(_proc,), daemon=True).start()
    with _lock:
        _up = 1

    for raw in _proc.stdout:
        line = raw.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            if key == "fps":
                with _lock:
                    _fps = float(value)
            elif key == "drop_frames":
                with _lock:
                    _dropped = int(value)
        except ValueError:
            pass

    code = _proc.wait()
    with _lock:
        _up = 0
        _fps = 0.0
    return code


def main():
    prepare_dirs()
    clear_segments()

    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever(),
        daemon=True,
    ).start()
    threading.Thread(target=gap_watcher, daemon=True).start()

    print(f"[encoder] metrics on :{PORT}/metrics", flush=True)
    print(f"[encoder] chaos modes: {', '.join(L1_MODES)}", flush=True)

    def shutdown(*_a):
        if _proc and _proc.poll() is None:
            _proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        code = run_once()
        if _restart.is_set():
            _restart.clear()
            # Clear ONLY the rungs that are no longer produced. Wiping every
            # rung on each mode switch 404s the segments viewers are actively
            # fetching, and that disruption is loud enough to drown out the
            # signature of the fault being injected -- every mode ends up
            # looking like "404 storm everywhere".
            with _lock:
                mode = _chaos["mode"]
            still_active = {r[0] for r in active_ladder(mode)}
            dropped = [r for r in CANONICAL_RUNGS if r not in still_active]
            if dropped:
                clear_segments(dropped)
                print(f"[encoder] cleared dropped rungs: {','.join(dropped)}",
                      flush=True)
            continue
        print(f"[encoder] ffmpeg exited with {code}; restarting in 3s", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
