#!/usr/bin/env python3
"""BTT SFS v2.0 Jam + Runout Monitor (Stock Marlin + PrusaConnect)

Adds a second GPIO input for the SFS runout on/off signal (default GPIO27).
Runout triggers an immediate pause (M600 by default), independent of jam arming.

See README.md for wiring and calibration.
License: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse, logging, logging.handlers, os, re, sys, threading, time
import textwrap
import json
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, List, Tuple

import serial
from gpiozero import DigitalInputDevice

__version__ = "0.10.0"
__build__ = "2025-12-29"

RE_SENSOR = re.compile(r'^\s*//\s*sensor:(enable|disable|reset)\b', re.IGNORECASE)
RE_TEMP = re.compile(r'^\s*T:(?P<tcur>-?\d+(?:\.\d+)?)/(?P<ttgt>-?\d+(?:\.\d+)?)\s+'
                     r'B:(?P<bcur>-?\d+(?:\.\d+)?)/(?P<btgt>-?\d+(?:\.\d+)?)\b')
RE_BUSY = re.compile(r'^\s*echo:busy:', re.IGNORECASE)

@dataclass
class State:
    enabled: bool = True
    latched: bool = False
    reason: str = ""  # "jam" or "runout"

    last_pulse_time: float = 0.0
    armed_until: float = 0.0
    ever_pulsed: bool = False
    pulse_total: int = 0

    reset_candidate_since: Optional[float] = None
    reset_start_pulse_total: int = 0
    grace_until: float = 0.0

    last_active_evidence_time: float = 0.0

    runout_asserted: bool = False
    last_runout_edge_time: float = 0.0

    jam_count: int = 0
    runout_count: int = 0
    pending_action: bool = False
    post_trigger_grace_until: float = 0.0

class SerialLink:
    def __init__(self, port: str, baud: int, dtr: bool, rts: bool, quiet_temps: bool, log: logging.Logger):
        self.port, self.baud, self.dtr, self.rts = port, baud, dtr, rts
        self.quiet_temps, self.log = quiet_temps, log
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self.connected = threading.Event()
        self.stop = threading.Event()
        self.rx_lines: List[Tuple[float, str]] = []
        self.rx_lock = threading.Lock()

    def _close(self):
        with self._lock:
            if self._ser:
                try: self._ser.close()
                except Exception: pass
            self._ser = None
        self.connected.clear()

    def _open(self) -> bool:
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.2)
            ser.dtr = self.dtr
            ser.rts = self.rts
            with self._lock: self._ser = ser
            self.connected.set()
            return True
        except Exception as e:
            self.log.warning("Serial open failed (%s): %s", self.port, e)
            self._close()
            return False

    def send(self, line: str) -> bool:
        if not self.connected.is_set(): return False
        if not line.endswith("\n"): line += "\n"
        try:
            with self._lock: ser = self._ser
            if ser is None: return False
            ser.write(line.encode("ascii", "ignore"))
            ser.flush()
            return True
        except Exception as e:
            self.log.warning("Serial write failed: %s", e)
            self._close()
            return False

    def start(self, on_line: Callable[[str, bool], None]):
        def reader():
            while not self.stop.is_set():
                if not self.connected.is_set():
                    time.sleep(0.1); continue
                try:
                    with self._lock: ser = self._ser
                    if ser is None: time.sleep(0.1); continue
                    raw = ser.readline()
                    if not raw: continue
                    text = raw.decode(errors="replace").rstrip()
                    with self.rx_lock:
                        self.rx_lines.append((time.time(), text))
                        if len(self.rx_lines) > 5000: del self.rx_lines[:1000]
                    if self.quiet_temps and text.lstrip().startswith("T:"):
                        on_line(text, True)
                    else:
                        on_line(text, False)
                except Exception as e:
                    self.log.warning("Serial read failed: %s", e)
                    self._close()
                    time.sleep(0.2)

        def manager():
            backoff = 0.25
            while not self.stop.is_set():
                if not self.connected.is_set():
                    if self._open():
                        self.log.info("Serial connected: %s @ %d", self.port, self.baud)
                        backoff = 0.25
                    else:
                        time.sleep(backoff)
                        backoff = min(backoff * 1.8, 5.0)
                else:
                    time.sleep(0.2)

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=manager, daemon=True).start()

    def shutdown(self):
        self.stop.set()
        self._close()


class JsonLogFormatter(logging.Formatter):
    """JSON Lines formatter (one JSON object per log line)."""
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            obj.update(fields)
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

def setup_logging(args) -> logging.Logger:
    log = logging.getLogger("sfs")
    log.setLevel(logging.INFO)

    formatter = JsonLogFormatter() if args.log_json else logging.Formatter(
        "[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    log.addHandler(sh)

    if args.log_file:
        p = Path(args.log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            p, maxBytes=args.log_max_bytes, backupCount=args.log_backups
        )
        fh.setFormatter(formatter)
        log.addHandler(fh)

    return log

def atomic_write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)

def main() -> int:
    ap = argparse.ArgumentParser(
    description="BTT SFS v2.0 filament jam + runout monitor (stock Marlin / PrusaConnect)",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=textwrap.dedent("""
    Examples:

      # Normal operation (jam + runout)
      python3 sfs_jam_monitor.py -p /dev/ttyACM0 --auto-reset --quiet-temps

      # Dry-run (detect only, no M600 sent)
      python3 sfs_jam_monitor.py -p /dev/ttyACM0 --dry-run

      # Runout wiring test (no serial required)
      python3 sfs_jam_monitor.py --runout-test --runout-gpio 27

      # Active-high runout signal
      python3 sfs_jam_monitor.py --runout-test --runout-gpio 27 --runout-active-high

      # One-shot status
      python3 sfs_jam_monitor.py -p /dev/ttyACM0 --status

      # Version
      python3 sfs_jam_monitor.py --version
    """)
    )
    ap.add_argument("-p", "--port", default="", help="Printer serial port (required unless --runout-test)")
    ap.add_argument("-b","--baud", type=int, default=115200)
    ap.add_argument("--motion-gpio", type=int, default=26, help="SFS motion pulse GPIO (BCM)")
    ap.add_argument("--runout-gpio", type=int, default=27, help="SFS runout switch GPIO (BCM)")
    ap.add_argument("--runout-enabled", action="store_true", default=True)
    ap.add_argument("--no-runout", dest="runout_enabled", action="store_false")
    ap.add_argument("--runout-active-low", action="store_true", default=True)
    ap.add_argument("--runout-active-high", dest="runout_active_low", action="store_false")
    ap.add_argument("--runout-debounce", type=float, default=0.10)

    ap.add_argument("--timeout", type=float, default=0.85)
    ap.add_argument("--arm-hold", type=float, default=1.25)
    ap.add_argument("--jam-gcode", default="M600")
    ap.add_argument("--dry-run", action="store_true")

    ap.add_argument("--auto-reset", action="store_true")
    ap.add_argument("--reset-pulses", type=float, default=1.5)
    ap.add_argument("--reset-min-pulses", type=int, default=25)
    ap.add_argument("--post-reset-grace", type=float, default=0.6)
    ap.add_argument("--post-jam-grace", type=float, default=2.0)

    ap.add_argument("--quiet-temps", action="store_true")
    ap.add_argument("--require-active", action="store_true", default=True)
    ap.add_argument("--no-require-active", dest="require_active", action="store_false")
    ap.add_argument("--arm-temp-threshold", type=float, default=170.0)
    ap.add_argument("--active-recent-seconds", type=float, default=120.0)

    ap.add_argument("--heartbeat-seconds", type=float, default=30.0)
    ap.add_argument("--printer-heartbeat-seconds", type=float, default=0.0)

    ap.add_argument("--log-json", action="store_true",
                    help="Emit logs as JSON lines (one JSON object per line)")
    ap.add_argument("--log-file", default="")
    ap.add_argument("--log-max-bytes", type=int, default=5_000_000)
    ap.add_argument("--log-backups", type=int, default=5)

    ap.add_argument("--metrics-file", default="")
    ap.add_argument("--dtr", choices=["on","off"], default="off")
    ap.add_argument("--rts", choices=["on","off"], default="off")
    ap.add_argument("--doctor", action="store_true", help="Run hardware self-test (no pause, no serial required)")
    args = ap.parse_args()

    if args.doctor:
        print("Doctor mode: basic wiring sanity check")
        print(" - Move filament to observe MOTION pulses")
        print(" - Remove filament to observe RUNOUT change")
        return 0

    if args.version:
        print(f"sfs-jam-monitor {__version__} ({__build__})")
        print("License: GPL-3.0-or-later")
        return 0
    if (not args.port) and (not args.runout_test) and (not args.status):
        ap.error("--port is required unless --runout-test is used")

    log = setup_logging(args)
    st = State(last_pulse_time=time.time())

    def _fields(now: float | None = None) -> dict:
        now = time.time() if now is None else now
        armed = (now <= st.armed_until) and st.ever_pulsed and (not st.latched)
        return {
            "enabled": st.enabled,
            "latched": st.latched,
            "reason": st.reason or "none",
            "motion_gpio": args.motion_gpio,
            "runout_gpio": args.runout_gpio,
            "pulse_total": st.pulse_total,
            "last_pulse_age_s": round(max(0.0, now - st.last_pulse_time), 3),
            "armed": armed,
            "serial_connected": link.connected.is_set() if 'link' in locals() else False,
            "runout_asserted": st.runout_asserted,
            "jam_count": st.jam_count,
            "runout_count": st.runout_count,
        }

    def log_event(event: str, level: int = logging.INFO, **extra_fields):
        f = _fields()
        f.update({"event": event})
        f.update(extra_fields)
        log.log(level, event.upper(), extra={"fields": f})

    pulse_pin = DigitalInputDevice(args.motion_gpio, pull_up=True, active_state=False)

    runout_pin = None
    if args.runout_enabled:
        pull_up = True if args.runout_active_low else False
        active_state = False if args.runout_active_low else True
        runout_pin = DigitalInputDevice(args.runout_gpio, pull_up=pull_up, active_state=active_state)
    # RUNOUT TEST MODE (no serial required)
    if args.runout_test:
        if not args.runout_enabled:
            log.info("Runout test requested but runout monitoring is disabled (--no-runout).")
            return 2
        if runout_pin is None:
            log.info("Runout GPIO not initialized.")
            return 2

        log.info("RUNOUT TEST: GPIO %d (active-%s). Toggle filament present/out to observe changes.",
                 args.runout_gpio, "low" if args.runout_active_low else "high")
        log.info("Press Ctrl-C to exit.")

        # Print initial state
        last = runout_pin.is_active
        log.info("RUNOUT asserted=%s", last)

        ev = threading.Event()

        def report():
            nonlocal last
            cur = runout_pin.is_active
            if cur != last:
                last = cur
                log.info("RUNOUT asserted=%s", cur)

        runout_pin.when_activated = report
        runout_pin.when_deactivated = report

        try:
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            log.info("Runout test stopped.")
        finally:
            runout_pin.close()
            pulse_pin.close()
        return 0



        def is_active(now: float) -> bool:
            if not args.require_active: return True
            return (now - st.last_active_evidence_time) <= args.active_recent_seconds

        def on_pulse():
            now = time.time()
            st.pulse_total += 1
            st.last_pulse_time = now
            if is_active(now):
                st.armed_until = now + args.arm_hold
                st.ever_pulsed = True
                if st.latched and args.auto_reset and st.reason == "jam" and st.reset_candidate_since is None:
                    st.reset_candidate_since = now
                    st.reset_start_pulse_total = st.pulse_total
            else:
                st.reset_candidate_since = None
                st.reset_start_pulse_total = st.pulse_total

        pulse_pin.when_activated = on_pulse

        link = SerialLink(args.port, args.baud, dtr=(args.dtr=="on"), rts=(args.rts=="on"),
                          quiet_temps=args.quiet_temps, log=log)

        def announce(msg: str):
            link.send(f"M118 A1 {msg}")

        def trigger(reason: str):
            if st.latched: return
            st.latched = True
            st.reason = reason
            st.post_trigger_grace_until = time.time() + args.post_jam_grace
            st.pending_action = False
            if reason == "runout":
                st.runout_count += 1
                log_event("runout", action_sent=(not args.dry_run))
                announce("SFS: Runout detected")
            else:
                st.jam_count += 1
                log.info("JAM: no pulses for %.2fs (sending action=%s)", time.time()-st.last_pulse_time, "NO" if args.dry_run else "YES")
                announce("SFS: Jam detected")

            if args.dry_run:
                announce("SFS: DRY-RUN (not sending jam_gcode)")
                return

            if link.connected.is_set():
                if not link.send(args.jam_gcode):
                    st.pending_action = True
            else:
                st.pending_action = True

        def handle_sensor(cmd: str):
            cmd = cmd.lower()
            if cmd in ("reset","enable"):
                st.enabled = True if cmd=="enable" else st.enabled
                st.latched = False
                st.reason = ""
                st.pending_action = False
                st.reset_candidate_since = None
                st.grace_until = 0.0
                st.post_trigger_grace_until = 0.0
                st.last_pulse_time = time.time()
                log.info("SFS: %s", cmd)
            elif cmd == "disable":
                st.enabled = False
                log.info("SFS: disabled")

        def on_line(text: str, suppressed: bool):
            m = RE_SENSOR.match(text)
            if m: handle_sensor(m.group(1))
            if RE_BUSY.match(text):
                st.last_active_evidence_time = time.time()
            tm = RE_TEMP.match(text)
            if tm:
                try:
                    ttgt = float(tm.group("ttgt"))
                    if ttgt >= args.arm_temp_threshold:
                        st.last_active_evidence_time = time.time()
                except Exception:
                    pass
            if not suppressed:
                log.info("%s", text)

        link.start(on_line)
    def print_status():
        now = time.time()
        armed = (now <= st.armed_until) and st.ever_pulsed and (not st.latched)

        print("SFS Jam Monitor Status")
        print("----------------------")
        print(f"Version           : {__version__} ({__build__})")
        print(f"Enabled           : {st.enabled}")
        print(f"Latched           : {st.latched}")
        print(f"Trigger reason    : {st.reason or 'none'}")
        print(f"Serial connected  : {link.connected.is_set()}")
        print(f"Armed             : {armed}")
        print(f"Pulse total       : {st.pulse_total}")
        print(f"Last pulse age    : {max(0.0, now - st.last_pulse_time):.2f}s")
        print(f"Runout asserted   : {st.runout_asserted}")
        print(f"Jam count         : {st.jam_count}")
        print(f"Runout count      : {st.runout_count}")

        if args.json:
            print(json.dumps(_fields(now), indent=2, ensure_ascii=False))

    # Runout debounce

        if runout_pin is not None:
            lock = threading.Lock()
            def on_runout_change():
                now = time.time()
                with lock:
                    st.last_runout_edge_time = now
                def deb(edge: float):
                    time.sleep(args.runout_debounce)
                    with lock:
                        if st.last_runout_edge_time != edge: return
                    st.runout_asserted = runout_pin.is_active
                    if st.runout_asserted and st.enabled:
                        trigger("runout")
                threading.Thread(target=deb, args=(now,), daemon=True).start()
            runout_pin.when_activated = on_runout_change
            runout_pin.when_deactivated = on_runout_change

        # One-shot status (attempt quick serial connect, then exit)
    if args.status:
        t0 = time.time()
        while (args.port and (not link.connected.is_set())) and (time.time() - t0) < 1.5:
            time.sleep(0.05)
        print_status()
        link.shutdown()
        pulse_pin.close()
        if runout_pin is not None:
            runout_pin.close()
        return 0



        last_hb = 0.0
        last_phb = 0.0
        last_metrics = 0.0
        last_connected = link.connected.is_set()

        def emit_metrics(now: float):
            if not args.metrics_file: return
            p = Path(args.metrics_file)
            content = "\n".join([
                f"sfs_connected {1 if link.connected.is_set() else 0}",
                f"sfs_enabled {1 if st.enabled else 0}",
                f"sfs_latched {1 if st.latched else 0}",
                f"sfs_trigger_reason {2 if st.reason=='runout' else (1 if st.reason=='jam' else 0)}",
                f"sfs_armed {1 if (now<=st.armed_until and st.ever_pulsed and not st.latched) else 0}",
                f"sfs_pulse_total {st.pulse_total}",
                f"sfs_jam_count {st.jam_count}",
                f"sfs_runout_asserted {1 if st.runout_asserted else 0}",
                f"sfs_runout_count {st.runout_count}",
                f"sfs_last_pulse_age_seconds {max(0.0, now-st.last_pulse_time):.3f}",
                ""
            ])
            try: atomic_write_text(p, content)
            except Exception as e: log.warning("Metrics write failed: %s", e)

        try:
            while True:
                time.sleep(0.05)
                now = time.time()

                if args.heartbeat_seconds > 0 and (now - last_hb) >= args.heartbeat_seconds:
                    last_hb = now
                    armed = (now <= st.armed_until) and st.ever_pulsed and (not st.latched)
                    log.info(
                        "HEARTBEAT enabled=%s latched=%s reason=%s connected=%s armed=%s pulses=%d last_pulse_age=%.2fs jams=%d runouts=%d runout_asserted=%s",
                        st.enabled,
                        st.latched,
                        st.reason,
                        link.connected.is_set(),
                        armed,
                        st.pulse_total,
                        max(0.0, now - st.last_pulse_time),
                        st.jam_count,
                        st.runout_count,
                        st.runout_asserted,
                    )
                    log_event("heartbeat")

                if (
                    args.printer_heartbeat_seconds > 0
                    and link.connected.is_set()
                    and (now - last_phb) >= args.printer_heartbeat_seconds
                ):
                    last_phb = now
                    armed = (now <= st.armed_until) and st.ever_pulsed and (not st.latched)
                    announce(
                        f"SFS: OK enabled={int(st.enabled)} latched={int(st.latched)} armed={int(armed)} "
                        f"reason={st.reason or 'none'} pulses={st.pulse_total} runout={int(st.runout_asserted)}"
                    )

                if args.metrics_file and (now-last_metrics)>=5.0:
                    last_metrics = now
                    emit_metrics(now)

                connected_now = link.connected.is_set()
                if connected_now and not last_connected:
                    log_event("serial_reconnected")
                    if st.pending_action and st.latched and not args.dry_run:
                        link.send(args.jam_gcode)
                        st.pending_action = False
                last_connected = connected_now

                if not st.enabled: continue

                # Auto-reset for jam only
                if st.latched and args.auto_reset and st.reason=="jam" and st.reset_candidate_since is not None:
                    sustained = now - st.reset_candidate_since
                    pulses_since = st.pulse_total - st.reset_start_pulse_total
                    if (now<=st.armed_until and sustained>=args.reset_pulses and pulses_since>=args.reset_min_pulses):
                        st.latched = False
                        st.reason = ""
                        st.pending_action = False
                        st.reset_candidate_since = None
                        st.grace_until = now + args.post_reset_grace
                        announce("SFS: auto-reset")
                        log_event("auto_reset", sustained_s=round(sustained,3), pulses_since=int(pulses_since))
                    elif now>st.armed_until:
                        st.reset_candidate_since = None
                        st.reset_start_pulse_total = st.pulse_total

                if st.latched: continue
                if now < st.grace_until or now < st.post_trigger_grace_until: continue

                if st.ever_pulsed and now<=st.armed_until and (now-st.last_pulse_time)>args.timeout:
                    trigger("jam")

        except KeyboardInterrupt:
            log.info("Stopped by user")
        finally:
            link.shutdown()
            pulse_pin.close()
            if runout_pin is not None: runout_pin.close()

        return 0

if __name__ == "__main__":
    raise SystemExit(main())
