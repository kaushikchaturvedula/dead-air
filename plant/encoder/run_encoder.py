"""L1 source + encoder -- ffmpeg ABR ladder with a Prometheus exporter.

Runs ffmpeg in realtime (-re) generating a 4-rung ABR HLS ladder with burned-in
timecode, and exports encoder health on :9103 for Alloy to scrape.

Metric names come from brief §5 verbatim -- `encoder_fps`, `dropped_frames`,
`packager_segment_lag` -- deliberately unprefixed, since that is how the brief
and the eventual alert rules name them.

    encoder_fps                          gauge    current encode rate
    dropped_frames                       counter  cumulative frames dropped
    packager_segment_lag{rendition=...}  gauge    seconds since that rung last
                                                  produced a segment
    encoder_up                           gauge    1 while ffmpeg is running

`packager_segment_lag` is measured as the age of the newest segment file for a
rung. In steady state it sawtooths between 0 and the segment duration (4s); if
the packager stalls it climbs without bound, which is the signal that matters.

The video source is deliberately an environment variable rather than a
hardcoded input. Brief §5's `black_source` chaos mode swaps the input to
`color=black` -- the demo the whole project is built around -- so that swap must
be a restart with a different env var, not a code change.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ENCODER_METRICS_PORT", "9103"))
HLS_ROOT = os.environ.get("HLS_ROOT", "/data/hls")
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "4"))
FPS = int(os.environ.get("ENCODER_FPS_TARGET", "30"))

# Chaos hook: `black_source` swaps this for "color=black:size=1920x1080:rate=30".
SOURCE = os.environ.get(
    "ENCODER_SOURCE", f"testsrc2=size=1920x1080:rate={FPS}"
)

# x264 speed/quality. Four 1080p-sourced rungs in realtime on a laptop needs a
# fast preset; quality is irrelevant for a test pattern.
PRESET = os.environ.get("ENCODER_PRESET", "ultrafast")

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# name, width, height, video bitrate, maxrate, bufsize
LADDER = [
    ("1080p", 1920, 1080, "5000k", "5350k", "7500k"),
    ("720p", 1280, 720, "3000k", "3210k", "4500k"),
    ("480p", 854, 480, "1500k", "1600k", "2250k"),
    ("360p", 640, 360, "800k", "856k", "1200k"),
]

_lock = threading.Lock()
_fps = 0.0
_dropped = 0
_up = 0
_started = time.time()


def build_command():
    """Assemble the ffmpeg invocation as an argv list (no shell quoting)."""
    # Burn in a running timecode, then fan out to the ladder. The timecode is
    # what makes a frozen source visually obvious -- and it is what Gemini
    # vision reads later to tell "frozen" from "black".
    drawtext = (
        f"drawtext=fontfile={FONT}:text='%{{pts\\:hms}}'"
        ":x=(w-tw)/2:y=h-th-40:fontsize=64:fontcolor=white"
        ":box=1:boxcolor=black@0.6:boxborderw=10"
    )
    splits = "".join(f"[s{i}]" for i in range(len(LADDER)))
    chains = [f"[0:v]{drawtext},split={len(LADDER)}{splits}"]
    for i, (name, w, h, *_rest) in enumerate(LADDER):
        chains.append(f"[s{i}]scale={w}:{h}[v{i}]")
    filter_complex = ";".join(chains)

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-re",
        "-f", "lavfi", "-i", SOURCE,
        "-filter_complex", filter_complex,
    ]

    gop = SEGMENT_SECONDS * FPS
    for i, (name, w, h, bv, maxrate, bufsize) in enumerate(LADDER):
        cmd += [
            "-map", f"[v{i}]",
            f"-c:v:{i}", "libx264",
            f"-b:v:{i}", bv,
            f"-maxrate:v:{i}", maxrate,
            f"-bufsize:v:{i}", bufsize,
        ]

    cmd += [
        "-preset", PRESET,
        "-pix_fmt", "yuv420p",
        # Keyframe-aligned across rungs so a player can switch cleanly.
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments",
        "-hls_segment_type", "mpegts",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", " ".join(
            f"v:{i},name:{name}" for i, (name, *_r) in enumerate(LADDER)
        ),
        "-hls_segment_filename", f"{HLS_ROOT}/%v/seg_%05d.ts",
        f"{HLS_ROOT}/%v/index.m3u8",
        # Machine-readable progress on stdout; suppress the human stats line.
        "-progress", "pipe:1", "-nostats",
    ]
    return cmd


def segment_lag():
    """Seconds since each rung last produced a segment."""
    now = time.time()
    out = {}
    for name, *_rest in LADDER:
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
        # No segments yet -> report lag since process start, not a bogus 0.
        out[name] = (now - newest) if newest else (now - _started)
    return out


def render_metrics():
    with _lock:
        fps, dropped, up = _fps, _dropped, _up
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
        "# HELP packager_segment_lag Seconds since this rendition last "
        "produced an HLS segment.\n",
        "# TYPE packager_segment_lag gauge\n",
    ]
    for name, lag in segment_lag().items():
        lines.append(f'packager_segment_lag{{rendition="{name}"}} {lag:.3f}\n')
    return "".join(lines)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code, body):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/metrics"):
            self._reply(200, render_metrics())
        elif self.path.startswith("/healthz"):
            with _lock:
                self._reply(200 if _up else 503, "ok\n" if _up else "encoder down\n")
        else:
            self._reply(404, "not found\n")

    def log_message(self, *args):
        pass


def serve_metrics():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def main():
    global _fps, _dropped, _up

    for name, *_rest in LADDER:
        os.makedirs(os.path.join(HLS_ROOT, name), exist_ok=True)
    # Clear stale segments so lag is not computed against a previous run.
    for name, *_rest in LADDER:
        d = os.path.join(HLS_ROOT, name)
        for entry in os.scandir(d):
            if entry.name.endswith((".ts", ".m3u8")):
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass

    threading.Thread(target=serve_metrics, daemon=True).start()
    cmd = build_command()
    print("[encoder] source:", SOURCE, flush=True)
    print("[encoder] ladder:", ", ".join(n for n, *_ in LADDER), flush=True)
    print("[encoder] metrics on :%d/metrics" % PORT, flush=True)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )

    def drain_stderr():
        # ffmpeg logs warnings/errors here; surface them but do not spam.
        for line in proc.stderr:
            line = line.strip()
            if line and ("rror" in line or "arning" in line or "Invalid" in line):
                print("[ffmpeg]", line, flush=True)

    threading.Thread(target=drain_stderr, daemon=True).start()

    def shutdown(*_a):
        proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    with _lock:
        _up = 1

    # -progress emits repeating key=value blocks terminated by progress=...
    for raw in proc.stdout:
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

    code = proc.wait()
    with _lock:
        _up = 0
    print(f"[encoder] ffmpeg exited with {code}", flush=True)
    # Stay alive briefly so the final scrape sees encoder_up 0 rather than a
    # connection refused, which reads as "scrape broken" instead of "encoder died".
    time.sleep(30)
    sys.exit(code)


if __name__ == "__main__":
    main()
