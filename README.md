# BTT SFS v2.0 Jam Monitor for Prusa Core One (Stock Firmware)

A Raspberry Pi–based filament jam monitor using the **BTT SFS v2.0** optical wheel sensor.

This project is designed to work **strictly with stock Marlin-based firmware** and **PrusaConnect streaming**, with **no firmware modification**, **no Klipper**, and **no host-side G-code parsing**.

When a filament jam is detected, the script triggers a clean **`M600` filament change**, allowing the user to clear the jam and resume the print safely.

---

## Key Features

- ✅ Works with **stock firmware only**
- ✅ Compatible with **PrusaConnect streaming**
- ✅ No G-code parsing required
- ✅ Uses real filament motion (encoder pulses), not heuristics
- ✅ Clean recovery using **`M600`**
- ✅ Script is fully external and non-invasive
- ✅ Optional **self-test mode**
- ✅ Optional **automatic re-arm after resume**

---

## System Architecture (Important)

- PrusaConnect streams G-code over the network
- The Raspberry Pi **does NOT see outgoing G-code**
- The Pi **only sees printer → host serial output**
- Therefore:
  - Plain G-code comments (`// ...`) are invisible
  - **`M118` messages must be used** as control markers

---

## Hardware Requirements

### Required
- **Prusa Core One** (or compatible Prusa machine)
- **Stock Prusa / Marlin-based firmware**
- **BTT SFS v2.0 filament sensor**
- **Raspberry Pi** (any model with GPIO)
- USB cable from Pi → printer (USB-C on Core One)

### Electrical Assumptions (Critical)
- SFS v2.0 pulse output is **open-collector / open-drain**
- Raspberry Pi **internal pull-up is required**
- Signal idles **HIGH** and pulses **LOW** (active-low)
- GPIO numbering uses **BCM mode**

---

## Wiring

| SFS v2.0 Pin | Raspberry Pi |
|-------------|--------------|
| VCC         | 5V or 3.3V (per SFS spec) |
| GND         | GND |
| PULSE       | GPIO **26** (BCM) |

⚠️ Ground **must** be shared between SFS and Pi.

---

## Software Installation (Raspberry Pi)

### 1. Install dependencies
```bash
sudo apt update
sudo apt install -y python3 python3-pip
python3 -m pip install pyserial gpiozero
```

### 2. Copy the script
Save the script as:
```text
sfs_jam_monitor.py
```

Make it executable:
```bash
chmod +x sfs_jam_monitor.py
```

---

## PrusaSlicer Configuration (REQUIRED)

⚠️ **This step is mandatory.**  
The monitor relies on `M118` markers echoed back by firmware.

### Printer Settings → Custom G-code

#### Start G-code
```gcode
M118 A1 sensor:reset
M118 A1 sensor:enable
```

(Optional comments for readability)
```gcode
// sensor:reset
// sensor:enable
```

#### End G-code
```gcode
M118 A1 sensor:disable
```

(Optional)
```gcode
// sensor:disable
```

### ⚠️ Do NOT put `sensor:reset` here:
- Pause Print G-code
- Filament Change G-code
- Toolchange G-code

Resetting during pause is unsafe and can cause false re-arming.

---

## Self-Test Mode (Highly Recommended)

Run this **before trusting the system**.

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --self-test
```

What it tests:
- ✔ Serial communication
- ✔ Firmware echo of `M118`
- ✔ GPIO wiring
- ✔ SFS pulse generation

---

## Normal Operation

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --auto-reset --reset-pulses 1.5 --quiet-temps
```

---

## License / Use

Personal / experimental use.

---

---

## Calibration & Tuning Guide (Strongly Recommended)

Jam detection based on filament motion **must be calibrated** for your printer, sensor mounting, slicer profile, and filament.
Do **not** start by letting the monitor send `M600` immediately.

### Phase 0 — Verify pulses (hardware sanity check)

Run the built-in self-test and confirm you see a non-zero pulse rate when the SFS wheel turns:

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --self-test
```

If this is flaky, fix wiring, grounding, or cable routing before tuning any parameters.

### Phase 1 — Tune safely with dry-run

Run during a real print using `--dry-run` (no pause commands will be sent):

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --dry-run --quiet-temps --heartbeat-seconds 10
```

Watch the heartbeat output (or `journalctl -f` if running under systemd). You care about:
- **last_pulse_age** during normal printing
- whether it ever approaches/exceeds your current `--timeout` while armed

### Phase 2 — Choose `--timeout` and `--arm-hold`

**`--timeout`** is the maximum allowed time without pulses while the detector is armed.
Pick it based on observed behavior:

- Find the worst (largest) **last_pulse_age** seen during normal printing.
- Set:
  - `timeout = worst_seen + 0.2s` (margin)

Typical starting ranges:
- **0.8–1.0s** for most direct-drive setups
- **1.0–1.3s** if you have lots of short segments, retractions, or very intermittent extrusion

**`--arm-hold`** keeps jam checking active briefly after the last pulse.
- If you see false triggers near travel moves / retractions:
  - increase `--timeout` first
  - optionally reduce `--arm-hold` slightly

Rule of thumb:
- keep `arm-hold` a bit longer than `timeout` (the defaults do this).

### Phase 3 — Validate the “active print” gating

By default, `--require-active` is ON to prevent arming from manual filament moves while idle.
Active status is inferred from *recent evidence* (temps / `echo:busy:` lines).

Tune the recency window:
- default: `--active-recent-seconds 120`
- if it fails to arm early in a print: increase (e.g. 180–300)
- if it arms during idle heating or manual feed: decrease (e.g. 60–120)

### Phase 4 — Auto-reset tuning (optional)

Enable only after jam detection is stable. Test with dry-run first:

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --dry-run --auto-reset --quiet-temps \
  --reset-pulses 1.5 --reset-min-pulses 25 --post-reset-grace 0.6
```

Guidelines:
- If it resets too easily during load/unload:
  - increase `--reset-min-pulses` (e.g. 40–80)
- If it never resets after you clear a jam and resume:
  - decrease `--reset-min-pulses` or `--reset-pulses`
- If you see immediate re-trigger after reset:
  - increase `--post-reset-grace` (e.g. 1.0–2.0)

### Phase 5 — Controlled jam test (still dry-run)

Do one deliberate jam test:
- print something extrusion-heavy
- gently pinch filament enough to stop motion
- confirm detection timing and no false triggers elsewhere

### Phase 6 — Go live (send `M600`)

Once you can complete a full print in `--dry-run` with **zero false jams**, remove `--dry-run`.

Recommended first-live command:

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --quiet-temps \
  --auto-reset --reset-pulses 1.5 --reset-min-pulses 25 --post-reset-grace 0.6
```

### Starter presets

**Conservative (fewer false positives):**
```text
--timeout 1.2 --arm-hold 1.6
```

**Faster detection:**
```text
--timeout 0.75 --arm-hold 1.1
```

---


## Running as a systemd Service (Recommended)

For long prints and unattended operation, running the monitor as a **systemd service**
is strongly recommended. This ensures the monitor:

- Starts automatically at boot
- Restarts if it crashes or the USB connection resets
- Logs cleanly to `journalctl`

The package includes:
- `sfs-jam-monitor.service` – systemd unit file
- `install_systemd.sh` – helper script to install and enable the service

### Installation

1. Copy the project to:
```bash
/home/pi/sfs-jam-monitor
```

2. Install Python dependencies (if not already installed):
```bash
python3 -m pip install pyserial gpiozero
```

3. Ensure the user is allowed to access serial devices:
```bash
sudo usermod -a -G dialout pi
```
Log out and back in for group changes to take effect.

4. Install and enable the service:
```bash
cd /home/pi/sfs-jam-monitor
chmod +x install_systemd.sh
./install_systemd.sh
```

### Serial Port Configuration

The service defaults to using:
```text
/dev/ttyACM0
```

If your printer appears as a different device (for example `/dev/ttyACM1` or
`/dev/ttyUSB0`), edit the service file:

```bash
sudo nano /etc/systemd/system/sfs-jam-monitor.service
```

Update the `ExecStart` line accordingly, then reload:
```bash
sudo systemctl daemon-reload
sudo systemctl restart sfs-jam-monitor.service
```

### Monitoring and Logs

Check service status:
```bash
systemctl status sfs-jam-monitor.service --no-pager
```

View live logs:
```bash
journalctl -u sfs-jam-monitor.service -f
```

Restart or stop:
```bash
sudo systemctl restart sfs-jam-monitor.service
sudo systemctl stop sfs-jam-monitor.service
```

---

---

## Additional Recommendations & Best Practices

### Reliability
- **USB cable quality matters**: Use a short, shielded USB cable between the Pi and the printer to minimize disconnects.
- **Dedicated USB port**: Avoid USB hubs if possible.
- **systemd restart policy**: Keep `Restart=on-failure` enabled (default in the provided service file).
- **Dry-run first**: Always validate thresholds with `--dry-run` before enabling live `M600` triggers.

### Jam Detection Tuning
- Start with defaults:
  - `--timeout 0.85`
  - `--arm-hold 1.25`
- If you see false positives on retractions or coast moves:
  - Increase `--timeout` slightly (e.g. 1.0–1.2s)
- If real jams are detected too late:
  - Reduce `--timeout` or `--arm-hold`

### Auto-Reset Tuning
- Use auto-reset only if your printer reliably resumes extrusion after clearing jams.
- Recommended baseline:
  - `--reset-pulses 1.5`
  - `--reset-min-pulses 25`
  - `--post-reset-grace 0.6`
- If you see re-triggering after resume, increase `--post-reset-grace`.

### Idle-Arming Prevention
- Leave `--require-active` **enabled** (default).
- This prevents accidental arming from:
  - Manual filament loading
  - Spinning the SFS wheel by hand
- Disable only if your firmware does not emit temperature or `echo:busy:` lines.

### PrusaSlicer Integration
- Always use **`M118 A1 sensor:*` markers**.
- Plain comments (`// sensor:*`) are for human readability only.
- Required locations:
  - Start G-code: `sensor:reset`, then `sensor:enable`
  - End G-code: `sensor:disable`
- Do **not** reset during pause or filament change.

### Monitoring & Debugging
- Use live logs:
```bash
journalctl -u sfs-jam-monitor.service -f
```
- Look for:
  - `JAM:` messages
  - `AUTO-RESET:` confirmations
  - `Reconnected to serial`

### Safety Notes
- The monitor **never parses G-code**.
- Jam detection is based solely on **physical filament motion**.
- This makes the system robust across slicers, profiles, and firmware versions.

### Backup & Recovery
- If the monitor is stopped or crashes:
  - Prints continue normally
  - No firmware state is modified
- On restart, the monitor waits for new pulses before arming.

---


## Final Hardening & Advanced Features

This release includes **all recommended robustness upgrades**:

- **Persistent logging** with rotation (`--log-file`)
- **Heartbeat/status** to printer logs (`--heartbeat`)
- **Post-jam cooldown** (`--post-jam-grace`)
- **Exact pulse counting** for auto-reset
- **Stronger active-print heuristic** with recency window
- **Stable serial paths** (recommend `/dev/serial/by-id/*`)
- **Optional console commands** (`--console`)
- **Prometheus textfile metrics** (`--metrics-file`)
- **systemd hardening** (see service file)

### Example hardened run (systemd ExecStart)
```bash
/usr/bin/python3 sfs_jam_monitor.py   -p /dev/serial/by-id/usb-Prusa_Research_*   --auto-reset   --post-jam-grace 2.0   --heartbeat 45   --log-file /var/log/sfs-jam-monitor.log   --metrics-file /var/lib/node_exporter/textfile_collector/sfs.prom   --quiet-temps
```

## Logging (Recommended)

By default, logs go to stdout / `journalctl` (systemd). For long prints, enable rotating file logs:

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --auto-reset --quiet-temps \
  --log-file /var/log/sfs-jam-monitor.log
```

Log rotation defaults:
- 5 MB per file (`--log-max-bytes 5000000`)
- 5 backups (`--log-backups 5`)

## Stable Serial Device Path

Linux device names like `/dev/ttyACM0` can change across boots. Prefer a stable path:

```bash
ls -l /dev/serial/by-id/
```

Then run the monitor using the by-id path, for example:

```bash
python3 sfs_jam_monitor.py -p /dev/serial/by-id/usb-... --auto-reset --quiet-temps
```

## Heartbeats

The monitor prints a periodic **local heartbeat** (default every 30s) with state:

- enabled / jam latched / connected / armed
- pulse count and time since last pulse
- jam count

You can also send a **printer heartbeat** via `M118` (rate-limited):

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --auto-reset --quiet-temps \
  --printer-heartbeat-seconds 60
```

This is helpful if you want to confirm the monitor is alive from PrusaConnect logs.

## Metrics Export (Optional)

The monitor can write **Prometheus textfile metrics** (compatible with node_exporter textfile collector):

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --auto-reset --quiet-temps \
  --metrics-file /var/lib/node_exporter/textfile_collector/sfs.prom
```

Metrics include:
- `sfs_connected`
- `sfs_enabled`
- `sfs_jam_latched`
- `sfs_armed`
- `sfs_pulse_total`
- `sfs_jam_count`
- `sfs_last_pulse_age_seconds`

## Console Mode (Optional)

For tuning at a terminal (not systemd), you can enable an interactive console:

```bash
python3 sfs_jam_monitor.py -p /dev/ttyACM0 --auto-reset --quiet-temps --console
```

Commands:
- `enable`
- `disable`
- `reset`
- `status`
- `quit`
