#!/usr/bin/env python3
"""
BTT SFS v2.0 Filament Jam Monitor (Stock Marlin + PrusaConnect)
==============================================================

What this does
--------------
- Reads filament motion pulses from a BTT SFS v2.0 sensor on Raspberry Pi GPIO (default BCM 26).
- Arms jam detection ONLY when filament motion pulses occur during an "active print" (heuristic).
- Declares a jam if pulses stop for longer than --timeout while armed.
- On jam, sends a pause action (default: M600) over the printer's USB serial port.
- Supports control markers echoed by firmware:
    M118 A1 sensor:enable   -> echoed as "// sensor:enable"
    M118 A1 sensor:disable  -> echoed as "// sensor:disable"
    M118 A1 sensor:reset    -> echoed as "// sensor:reset"
- Designed for stock Marlin-based firmware (no firmware mods) and PrusaConnect streaming.

This version includes:
- USB disconnect/reconnect loop (auto recovers from /dev/ttyACM* dropouts)
- Smarter auto-reset after jam (time + minimum pulses + post-reset grace)
- Dry-run mode (detect & log only, no M600)
- Idle-arming suppression heuristic with recency window (reduces arming from manual moves)
- Rotating file logging (optional)
- Status heartbeat (local + optional M118 heartbeat to printer)
- Post-jam grace window
- Precise pulse counting for auto-reset thresholds
- Optional interactive console (stdin) for enable/disable/reset/status
- Optional Prometheus textfile metrics export

License: GPL-3.0-or-later (see README.md)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, List, Tuple

import serial
from gpiozero import DigitalInputDevice


def ts() -> str:
    t = time.time()
    lt = time.localtime(t)
    ms = int((t - int(t)) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", lt) + f".{ms:03d}"


# Firmware echoes: "// sensor:reset"
RE_SENSOR = re.compile(r'^\s*//\s*sensor:(enable|disable|reset)\b', re.IGNORECASE)

# Typical Marlin temp report lines begin with "T:".
# Example:  T:275.94/275.00 B:110.10/110.00 ...
RE_TEMP = re.compile(
    r'^\s*T:(?P<tcur>-?\d+(?:\.\d+)?)/(?P<ttgt>-?\d+(?:\.\d+)?)\s+'
    r'B:(?P<bcur>-?\d+(?:\.\d+)?)/(?P<btgt>-?\d+(?:\.\d+)?)\b'
)

RE_BUSY = re.compile(r'^\s*echo:busy:', re.IGNORECASE)


@dataclass
class State:
    enabled: bool = True
    jam_latched: bool = False

    last_pulse_time: float = 0.0
    armed_until: float = 0.0
    ever_pulsed: bool = False

    # Total pulses observed since start (precise)
    pulse_total: int = 0

    # Auto-reset tracking (precise)
    reset_candidate_since: Optional[float] = None
    reset_start_pulse_total: int = 0  # pulse_total at candidate start
    grace_until: float = 0.0

    # Temps / busy for "active print" heuristic
    hotend_cur: Optional[float] = None
    hotend_tgt: Optional[float] = None
    bed_cur: Optional[float] = None
    bed_tgt: Optional[float] = None
    last_busy_time: float = 0.0
    last_active_evidence_time: float = 0.0

    # Jam counters + deferrals
    jam_count: int = 0
    pending_jam_action: bool = False
    post_jam_grace_until: float = 0.0


class SerialLink:
    """
    Owns the pyserial object and handles auto-reconnect.
    A single reader thread runs continuously and reads whenever connected.
    """
    def __init__(self, port: str, baud: int, dtr: bool, rts: bool, quiet_temps: bool, log: logging.Logger):
        self.port = port
        self.baud = baud
        self.dtr = dtr
        self.rts = rts
        self.quiet_temps = quiet_temps
        self.log = log

        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()

        self.connected = threading.Event()
        self.stop = threading.Event()

        # RX buffer for self-test / debug
        self.rx_lines: List[Tuple[float, str]] = []
        self.rx_lock = threading.Lock()

    def _close_serial(self):
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None
        self.connected.clear()

    def _open_serial(self) -> bool:
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.2)
            ser.dtr = self.dtr
            ser.rts = self.rts
            with self._lock:
                self._ser = ser
            self.connected.set()
            return True
        except Exception as e:
            self.log.warning("Serial open failed (%s): %s", self.port, e)
            self._close_serial()
            return False

    def send(self, line: str) -> bool:
        """Best-effort send. Returns True on success, False if disconnected/failure."""
        if not self.connected.is_set():
            return False
        if not line.endswith("\n"):
            line += "\n"
        data = line.encode("ascii", "ignore")
        try:
            with self._lock:
                ser = self._ser
            if ser is None:
                return False
            ser.write(data)
            ser.flush()
            return True
        except Exception as e:
            self.log.warning("Serial write failed (disconnect?): %s", e)
            self._close_serial()
            return False

    def _reader_loop(self, on_line: Callable[[str, bool], None]):
        while not self.stop.is_set():
            if not self.connected.is_set():
                time.sleep(0.1)
                continue
            try:
                with self._lock:
                    ser = self._ser
                if ser is None:
                    time.sleep(0.1)
                    continue
                raw = ser.readline()
                if not raw:
                    continue
                text = raw.decode(errors="replace").rstrip()

                with self.rx_lock:
                    self.rx_lines.append((time.time(), text))
                    if len(self.rx_lines) > 5000:
                        del self.rx_lines[:1000]

                if self.quiet_temps and text.lstrip().startswith("T:"):
                    on_line(text, suppressed=True)
                else:
                    on_line(text, suppressed=False)

            except Exception as e:
                self.log.warning("Serial read failed (disconnect?): %s", e)
                self._close_serial()
                time.sleep(0.2)

    def _manager_loop(self):
        backoff = 0.25
        while not self.stop.is_set():
            if not self.connected.is_set():
                ok = self._open_serial()
                if ok:
                    self.log.info("Serial connected: %s @ %d", self.port, self.baud)
                    backoff = 0.25
                else:
                    time.sleep(backoff)
                    backoff = min(backoff * 1.8, 5.0)
            else:
                time.sleep(0.2)

    def start(self, on_line: Callable[[str, bool], None]):
        threading.Thread(target=self._reader_loop, args=(on_line,), daemon=True).start()
        threading.Thread(target=self._manager_loop, daemon=True).start()

    def shutdown(self):
        self.stop.set()
        self._close_serial()


def setup_logging(args) -> logging.Logger:
    log = logging.getLogger("sfs")
    log.setLevel(logging.INFO)

    # Stream handler (stdout)
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(sh)

    # Optional rotating file handler
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=args.log_max_bytes,
            backupCount=args.log_backups,
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)

    return log


def atomic_write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="BTT SFS v2.0 jam monitor (stock Marlin / PrusaConnect-safe)")
    ap.add_argument("-p", "--port", required=True, help="Printer serial port (recommend /dev/serial/by-id/...)")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="Serial baudrate (default 115200)")
    ap.add_argument("--gpio", type=int, default=26, help="BCM GPIO for SFS pulse (default 26)")

    ap.add_argument("--timeout", type=float, default=0.85,
                    help="Jam if no pulses for this long (s) while armed (default 0.85)")
    ap.add_argument("--arm-hold", type=float, default=1.25,
                    help="Keep detection armed after last pulse (s, default 1.25)")

    ap.add_argument("--jam-gcode", default="M600",
                    help="G-code to send on jam (default M600)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not send jam action G-code; only log and announce via M118")

    # Smarter auto-reset
    ap.add_argument("--auto-reset", action="store_true",
                    help="After a jam, auto-reset latch after sustained pulses resume")
    ap.add_argument("--reset-pulses", type=float, default=1.5,
                    help="Seconds of sustained motion required to auto-reset (default 1.5)")
    ap.add_argument("--reset-min-pulses", type=int, default=25,
                    help="Minimum pulses required during auto-reset window (default 25)")
    ap.add_argument("--post-reset-grace", type=float, default=0.6,
                    help="Seconds to suppress jam detection after auto-reset (default 0.6)")

    # Post-jam grace (after triggering jam action)
    ap.add_argument("--post-jam-grace", type=float, default=2.0,
                    help="Seconds to suppress additional logic immediately after triggering jam (default 2.0)")

    ap.add_argument("--quiet-temps", action="store_true",
                    help="Hide temperature spam lines (still parses // sensor:*)")

    # Active-print heuristic
    ap.add_argument("--require-active", action="store_true", default=True,
                    help="Require evidence of an active print to arm on pulses (default ON)")
    ap.add_argument("--no-require-active", dest="require_active", action="store_false",
                    help="Disable active-print heuristic (arm on any pulses)")
    ap.add_argument("--arm-temp-threshold", type=float, default=170.0,
                    help="Hotend target threshold (°C) to infer active printing (default 170)")
    ap.add_argument("--busy-window", type=float, default=10.0,
                    help="Seconds since last 'echo:busy:' to treat as active evidence (default 10)")
    ap.add_argument("--active-recent-seconds", type=float, default=120.0,
                    help="How recent active evidence must be to allow arming (default 120s)")

    # Heartbeats
    ap.add_argument("--heartbeat-seconds", type=float, default=30.0,
                    help="Local status heartbeat interval (default 30s)")
    ap.add_argument("--printer-heartbeat-seconds", type=float, default=0.0,
                    help="If >0, send M118 heartbeat to printer at this interval (default 0 = off)")

    # Logging
    ap.add_argument("--log-file", default="",
                    help="Optional log file path (rotating). Example: /var/log/sfs-jam-monitor.log")
    ap.add_argument("--log-max-bytes", type=int, default=5_000_000, help="Rotate log after this many bytes (default 5MB)")
    ap.add_argument("--log-backups", type=int, default=5, help="Number of rotated logs to keep (default 5)")

    # Metrics (Prometheus textfile format)
    ap.add_argument("--metrics-file", default="",
                    help="Optional Prometheus textfile path. Example: /var/lib/node_exporter/textfile_collector/sfs.prom")

    # Optional interactive console
    ap.add_argument("--console", action="store_true",
                    help="Read stdin commands (enable/disable/reset/status). Not for systemd.")

    # Self-test
    ap.add_argument("--self-test", action="store_true",
                    help="Run serial + GPIO self-test and exit (no jam logic)")
    ap.add_argument("--self-test-seconds", type=float, default=8.0,
                    help="How long to watch for pulses during self-test (default 8s)")

    # Serial control lines (avoid reset on connect)
    ap.add_argument("--dtr", choices=["on", "off"], default="off",
                    help="Set DTR on open (OFF avoids reset on many boards)")
    ap.add_argument("--rts", choices=["on", "off"], default="off",
                    help="Set RTS on open")

    args = ap.parse_args()
    log = setup_logging(args)

    st = State()
    st.last_pulse_time = time.time()

    pulse_pin = DigitalInputDevice(
        args.gpio,
        pull_up=True,        # SFS requires pull-up
        active_state=False   # active-low pulses
    )

    def active_evidence(now: float):
        st.last_active_evidence_time = now

    def is_active_print(now: float) -> bool:
        if args.self_test:
            return True
        if not args.require_active:
            return True
        # Require that some evidence happened recently
        return (now - st.last_active_evidence_time) <= args.active_recent_seconds

    def on_pulse():
        now = time.time()
        st.pulse_total += 1
        st.last_pulse_time = now

        if is_active_print(now):
            st.armed_until = now + args.arm_hold
            st.ever_pulsed = True

            if st.jam_latched and args.auto_reset:
                if st.reset_candidate_since is None:
                    st.reset_candidate_since = now
                    st.reset_start_pulse_total = st.pulse_total
        else:
            # Idle pulses: do not arm; cancel auto-reset candidate
            st.reset_candidate_since = None
            st.reset_start_pulse_total = st.pulse_total

    pulse_pin.when_activated = on_pulse

    link = SerialLink(
        port=args.port,
        baud=args.baud,
        dtr=(args.dtr == "on"),
        rts=(args.rts == "on"),
        quiet_temps=args.quiet_temps,
        log=log
    )

    def handle_sensor(cmd: str):
        cmd = cmd.lower()
        if cmd == "reset":
            st.jam_latched = False
            st.pending_jam_action = False
            st.reset_candidate_since = None
            st.grace_until = 0.0
            st.post_jam_grace_until = 0.0
            st.last_pulse_time = time.time()
            log.info("SFS: reset")
        elif cmd == "enable":
            st.enabled = True
            st.jam_latched = False
            st.pending_jam_action = False
            st.reset_candidate_since = None
            st.grace_until = 0.0
            st.post_jam_grace_until = 0.0
            st.last_pulse_time = time.time()
            log.info("SFS: enabled")
        elif cmd == "disable":
            st.enabled = False
            log.info("SFS: disabled")

    def on_serial_line(text: str, suppressed: bool):
        # Control markers
        m = RE_SENSOR.match(text)
        if m:
            handle_sensor(m.group(1))

        # Busy marker
        if RE_BUSY.match(text):
            st.last_busy_time = time.time()
            active_evidence(st.last_busy_time)

        # Temps
        tm = RE_TEMP.match(text)
        if tm:
            try:
                st.hotend_cur = float(tm.group("tcur"))
                st.hotend_tgt = float(tm.group("ttgt"))
                st.bed_cur = float(tm.group("bcur"))
                st.bed_tgt = float(tm.group("btgt"))
                # Active evidence: hotend target suggests printing/heating for printing
                if st.hotend_tgt is not None and st.hotend_tgt >= args.arm_temp_threshold:
                    active_evidence(time.time())
                # Or: hotend is hot and bed target nonzero
                if (st.hotend_cur is not None and st.hotend_cur >= args.arm_temp_threshold) and (st.bed_tgt is not None and st.bed_tgt > 0.0):
                    active_evidence(time.time())
            except Exception:
                pass

        if not suppressed:
            # Mirror printer output to stdout/log for debugging
            log.info("%s", text)

    link.start(on_serial_line)

    def announce(s: str) -> bool:
        return link.send(f"M118 A1 {s}")

    def emit_metrics(now: float):
        if not args.metrics_file:
            return
        p = Path(args.metrics_file)
        connected = 1 if link.connected.is_set() else 0
        armed = 1 if (now <= st.armed_until and st.ever_pulsed and (not st.jam_latched)) else 0
        last_pulse_age = max(0.0, now - st.last_pulse_time) if st.last_pulse_time else 0.0
        content = "\n".join([
            "# HELP sfs_connected Serial connected (1/0)",
            "# TYPE sfs_connected gauge",
            f"sfs_connected {connected}",
            "# HELP sfs_enabled Monitor enabled (1/0)",
            "# TYPE sfs_enabled gauge",
            f"sfs_enabled {1 if st.enabled else 0}",
            "# HELP sfs_jam_latched Jam latched (1/0)",
            "# TYPE sfs_jam_latched gauge",
            f"sfs_jam_latched {1 if st.jam_latched else 0}",
            "# HELP sfs_armed Jam detection armed (1/0)",
            "# TYPE sfs_armed gauge",
            f"sfs_armed {armed}",
            "# HELP sfs_pulse_total Total pulses observed",
            "# TYPE sfs_pulse_total counter",
            f"sfs_pulse_total {st.pulse_total}",
            "# HELP sfs_jam_count Total jam events observed",
            "# TYPE sfs_jam_count counter",
            f"sfs_jam_count {st.jam_count}",
            "# HELP sfs_last_pulse_age_seconds Seconds since last pulse",
            "# TYPE sfs_last_pulse_age_seconds gauge",
            f"sfs_last_pulse_age_seconds {last_pulse_age:.3f}",
            ""
        ])
        try:
            atomic_write_text(p, content)
        except Exception as e:
            log.warning("Metrics write failed: %s", e)

    def console_loop():
        log.info("Console enabled. Commands: enable | disable | reset | status | quit")
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                cmd = line.strip().lower()
                if cmd in ("q", "quit", "exit"):
                    log.info("Console quit requested.")
                    os._exit(0)
                elif cmd in ("enable", "disable", "reset"):
                    handle_sensor(cmd)
                    announce(f"sensor:{cmd}")
                elif cmd == "status":
                    now = time.time()
                    connected = link.connected.is_set()
                    log.info("STATUS enabled=%s jam=%s connected=%s armed=%s pulses=%d last_pulse_age=%.2fs jams=%d",
                             st.enabled, st.jam_latched, connected,
                             (now <= st.armed_until), st.pulse_total, now - st.last_pulse_time, st.jam_count)
                elif cmd:
                    log.info("Unknown command: %s", cmd)
            except Exception as e:
                log.warning("Console error: %s", e)
                time.sleep(0.2)

    if args.console and not args.self_test:
        threading.Thread(target=console_loop, daemon=True).start()

    # --- SELF TEST ---
    if args.self_test:
        log.info("SELF-TEST MODE")
        log.info("Waiting for serial connection (up to 5s)...")
        t0 = time.time()
        while not link.connected.is_set() and (time.time() - t0) < 5.0:
            time.sleep(0.05)

        if link.connected.is_set():
            marker = f"SFS_SELFTEST_{int(time.time())}"
            log.info("Step 1) Serial echo test: sending marker via M118...")
            link.send(f"M118 A1 {marker}")
            t1 = time.time()
            echoed = False
            while time.time() - t1 < 3.0:
                time.sleep(0.05)
                with link.rx_lock:
                    recent = [s for (t, s) in link.rx_lines if t >= t1]
                if any(marker in s for s in recent):
                    echoed = True
                    break
            log.info("Serial echo: %s", "OK" if echoed else "NOT SEEN (sensor:* echo is what matters)")
        else:
            log.info("Serial not connected; continuing GPIO test anyway.")

        log.info("Step 2) Pulse test: move filament so the SFS wheel turns.")
        n0 = st.pulse_total
        p0 = time.time()
        time.sleep(args.self_test_seconds)
        p1 = time.time()
        dn = st.pulse_total - n0
        dt = max(1e-6, p1 - p0)
        if dn > 0:
            log.info("GPIO pulse OK: %d pulses in %.2fs (~%.1f Hz)", dn, dt, dn/dt)
            rc = 0
        else:
            log.info("NO pulses detected. Check wiring, shared GND, pull-up, polarity.")
            rc = 1

        link.shutdown()
        pulse_pin.close()
        return rc

    # --- NORMAL MODE LOOP ---
    log.info("Jam monitor running (USB reconnect ON).")
    log.info("dry_run=%s auto_reset=%s require_active=%s port=%s gpio=%d",
             args.dry_run, args.auto_reset, args.require_active, args.port, args.gpio)

    last_connected = link.connected.is_set()
    last_heartbeat = 0.0
    last_printer_heartbeat = 0.0
    last_metrics = 0.0

    try:
        while True:
            time.sleep(0.05)
            now = time.time()

            # Heartbeat
            if args.heartbeat_seconds > 0 and (now - last_heartbeat) >= args.heartbeat_seconds:
                last_heartbeat = now
                connected = link.connected.is_set()
                armed = (now <= st.armed_until) and st.ever_pulsed and (not st.jam_latched)
                last_pulse_age = max(0.0, now - st.last_pulse_time)
                log.info("HEARTBEAT enabled=%s jam=%s connected=%s armed=%s pulses=%d last_pulse_age=%.2fs jams=%d",
                         st.enabled, st.jam_latched, connected, armed, st.pulse_total, last_pulse_age, st.jam_count)

            # Optional printer heartbeat
            if args.printer_heartbeat_seconds and args.printer_heartbeat_seconds > 0:
                if (now - last_printer_heartbeat) >= args.printer_heartbeat_seconds:
                    last_printer_heartbeat = now
                    if link.connected.is_set():
                        armed = (now <= st.armed_until) and st.ever_pulsed and (not st.jam_latched)
                        announce(f"SFS: OK enabled={int(st.enabled)} armed={int(armed)} jam={int(st.jam_latched)} pulses={st.pulse_total}")

            # Metrics
            if args.metrics_file and (now - last_metrics) >= 5.0:
                last_metrics = now
                emit_metrics(now)

            # Reconnect transition handling
            connected_now = link.connected.is_set()
            if connected_now and not last_connected:
                log.info("Reconnected to serial")
                if st.pending_jam_action and st.jam_latched:
                    log.info("Pending jam action after reconnect")
                    if args.dry_run:
                        announce("SFS: DRY-RUN (not sending jam_gcode)")
                    else:
                        link.send(args.jam_gcode)
                    st.pending_jam_action = False
            last_connected = connected_now

            # Auto-reset after jam once sustained motion resumes
            if st.jam_latched and args.auto_reset and st.reset_candidate_since is not None:
                sustained_time = now - st.reset_candidate_since
                pulses_since = st.pulse_total - st.reset_start_pulse_total
                if (now <= st.armed_until and
                    sustained_time >= args.reset_pulses and
                    pulses_since >= args.reset_min_pulses):
                    st.jam_latched = False
                    st.pending_jam_action = False
                    st.reset_candidate_since = None
                    st.grace_until = now + args.post_reset_grace
                    log.info("AUTO-RESET after sustained motion (%.2fs, pulses=%d)", sustained_time, pulses_since)
                    announce("SFS: auto-reset")
                    continue
                if now > st.armed_until:
                    st.reset_candidate_since = None
                    st.reset_start_pulse_total = st.pulse_total

            # Skip if disabled or jam latched or never armed
            if (not st.enabled) or st.jam_latched or (not st.ever_pulsed):
                continue

            # Suppress checks immediately after auto-reset or after jam trigger
            if now < st.grace_until or now < st.post_jam_grace_until:
                continue

            # Jam detection: only while armed
            if now <= st.armed_until and (now - st.last_pulse_time) > args.timeout:
                st.jam_latched = True
                st.jam_count += 1
                st.reset_candidate_since = None
                st.reset_start_pulse_total = st.pulse_total
                st.post_jam_grace_until = now + args.post_jam_grace

                log.info("JAM: no pulses for %.2fs (sending action=%s)", now - st.last_pulse_time, "NO" if args.dry_run else "YES")

                # Announce (best effort)
                announce("SFS: Jam detected")

                # Jam action: immediate if connected, else defer until reconnect
                if args.dry_run:
                    announce("SFS: DRY-RUN (not sending jam_gcode)")
                    st.pending_jam_action = False
                else:
                    if link.connected.is_set():
                        if not link.send(args.jam_gcode):
                            st.pending_jam_action = True
                    else:
                        st.pending_jam_action = True

    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        link.shutdown()
        pulse_pin.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
