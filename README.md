# NeoKey Pi Web Controller

A responsive LAN controller for one or more
[Adafruit NeoKey Trinkey M0](https://www.adafruit.com/product/5020) boards connected to a
Raspberry Pi.

The browser sends color, brightness, flash, and flash-rate changes over a WebSocket at up
to 10 Hz. The Pi mirrors every update to all compatible Trinkeys over dedicated USB CDC
data ports. Flash timing runs on each Trinkey, so it remains accurate without continuous
network traffic.

## Features

- HSV color wheel and live color preview
- Brightness control from 0–100%
- Flash toggle and 0.25–10 Hz flash-rate control
- WebSocket transport with HTTP fallback
- Automatic discovery, mirroring, and reconnection of multiple Trinkeys
- State synchronization between browser tabs
- Persistent state with coalesced writes to reduce SD-card wear
- Responsive interface for phones and desktop browsers
- Runs as a user-level systemd service on port 8888

## Architecture

```text
Browser(s) ── WebSocket /ws ──> Raspberry Pi service ── USB CDC ──> NeoKey 1
                       10 Hz              │              USB CDC ──> NeoKey 2
                                          └───────────── USB CDC ──> ...
```

The repository contains two parts:

- `boot.py` and `code.py`: CircuitPython firmware copied to every Trinkey.
- `app.py` and `static/index.html`: the Raspberry Pi web service and interface.

## Requirements

- Raspberry Pi or another Linux host with Python 3.10+
- One or more Adafruit NeoKey Trinkey M0 boards
- CircuitPython 10.x for the NeoKey Trinkey M0
- A local account with access to the `dialout` group

## Install the Raspberry Pi service

Clone the repository to `~/neokey-control`:

```bash
git clone https://github.com/petedaws/neokey-pi-web-controller.git ~/neokey-control
cd ~/neokey-control
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Allow your account to access USB serial devices, then log out and back in if the group was
newly added:

```bash
sudo usermod -aG dialout "$USER"
```

Install and start the user service:

```bash
mkdir -p ~/.config/systemd/user
cp neokey-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now neokey-web.service
sudo loginctl enable-linger "$USER"
```

Open `http://PI_IP_ADDRESS:8888/` from another device on the same LAN.

Useful checks:

```bash
systemctl --user status neokey-web.service
journalctl --user -u neokey-web.service -f
curl http://127.0.0.1:8888/api/state
```

The service listens on `0.0.0.0:8888` and has no authentication. Do not expose it directly
to the public internet.

## Update a NeoKey for USB control

Each NeoKey must run the included CircuitPython firmware. Repeat this section for every
new board.

### 1. Identify the target board

Before changing anything, list the USB serial devices:

```bash
lsusb | grep -i 239a
ls -l /dev/serial/by-id/
```

Record the target board's serial number and `/dev/ttyACM*` path. If multiple NeoKeys are
connected, verify the physical USB path as well:

```bash
udevadm info --query=property --name=/dev/ttyACM2 \
  | grep -E 'ID_SERIAL_SHORT|ID_PATH|ID_USB_INTERFACE_NUM'
```

Do not rely only on `/dev/ttyACM2`; the number can change after a reboot or reflash.

### 2. Download CircuitPython

Download the current stable UF2 for `adafruit_neokey_trinkey_m0` from the
[official CircuitPython board page](https://circuitpython.org/board/adafruit_neokey_trinkey_m0/).
The deployment documented here used CircuitPython 10.3.0.

Example:

```bash
curl -fL -o ~/neokey-circuitpython.uf2 \
  https://downloads.circuitpython.org/bin/adafruit_neokey_trinkey_m0/en_US/adafruit-circuitpython-adafruit_neokey_trinkey_m0-en_US-10.3.0.uf2
sha256sum ~/neokey-circuitpython.uf2
```

The expected SHA-256 for that exact 10.3.0 English build is:

```text
70b5085b3431fc46dd88289f45c5ddc81a527b8df6dca2b6ed09e50726dc4c25
```

### 3. Enter the UF2 bootloader

Install pyserial in a temporary virtual environment if it is not already available:

```bash
python3 -m venv ~/neokey-flash-venv
~/neokey-flash-venv/bin/pip install pyserial
```

Perform a 1200-baud touch on the **target board's** serial port:

```bash
TARGET_TTY=/dev/ttyACM2
~/neokey-flash-venv/bin/python -c \
  "import serial,time; s=serial.Serial('$TARGET_TTY',1200); time.sleep(.4); s.close()"
sleep 5
```

The target should reappear as USB product `239a:00ff` with a volume named
`TRINKEYBOOT`:

```bash
lsusb | grep -i 239a
ls -l /dev/disk/by-label/TRINKEYBOOT
```

### 4. Back up the existing firmware

Back up `CURRENT.UF2` before flashing. This file can restore the exact previous firmware.

```bash
BACKUP_DIR="$HOME/neokey-backups/$(date +%Y%m%d-%H%M%S)-factory"
sudo mkdir -p /mnt/neokey-boot
mkdir -p "$BACKUP_DIR"
sudo mount -o uid="$(id -u)",gid="$(id -g)",umask=022 \
  /dev/disk/by-label/TRINKEYBOOT /mnt/neokey-boot
cp -a /mnt/neokey-boot/. "$BACKUP_DIR/"
sha256sum "$BACKUP_DIR/CURRENT.UF2"
```

### 5. Flash CircuitPython

```bash
cp ~/neokey-circuitpython.uf2 /mnt/neokey-boot/NEOKEY-CIRCUITPYTHON.UF2
sync
sleep 8
sudo umount /mnt/neokey-boot 2>/dev/null || true
```

The Trinkey should now identify as USB product `239a:8100` and expose a small
`CIRCUITPY` filesystem.

### 6. Copy the controller firmware

When more than one `CIRCUITPY` volume exists, identify the new board's block device by
serial number before mounting it:

```bash
lsblk -o NAME,PATH,LABEL,FSTYPE,SIZE,MOUNTPOINTS
udevadm info --query=property --name=/dev/sdc1 | grep ID_SERIAL_SHORT
```

After verifying the exact target (replace `/dev/sdc1` below):

```bash
TARGET_BLOCK=/dev/sdc1
sudo mkdir -p /mnt/neokey-circuitpy
sudo mount -o uid="$(id -u)",gid="$(id -g)",umask=022 \
  "$TARGET_BLOCK" /mnt/neokey-circuitpy
cp boot.py code.py /mnt/neokey-circuitpy/
sync
sudo umount /mnt/neokey-circuitpy
```

`boot.py` disables unused HID/MIDI interfaces and enables a CircuitPython console plus a
dedicated USB CDC data interface. After a hard reset, Linux should expose two serial ports
for the board: interface `if00` is the console and `if02` is the controller data channel.

The safest final step is to unplug and reconnect the board. Alternatively, from its
CircuitPython console, enter the REPL with Ctrl-C and run:

```python
import microcontroller
microcontroller.reset()
```

Verify the two interfaces:

```bash
ls -l /dev/serial/by-id/*NeoKey*
```

The Pi service scans once per second. A configured board automatically receives the current
state when connected, so no service restart is required.

## Restore a backed-up firmware image

Enter `TRINKEYBOOT` again, mount it, and copy the saved image back:

```bash
cp "$BACKUP_DIR/CURRENT.UF2" /mnt/neokey-boot/RESTORE.UF2
sync
```

Restoring a factory image removes the CircuitPython USB data interface, so that board will
no longer be controlled by this service until the controller firmware is reinstalled.

## WebSocket protocol

Connect to `ws://PI_IP_ADDRESS:8888/ws`.

Client updates are partial patches:

```json
{"type":"set","patch":{"brightness":50}}
```

Color updates contain `r`, `g`, and `b`; flash updates use `flash` and `flashRate`.

The server broadcasts the complete synchronized state to every browser:

```json
{
  "type": "state",
  "state": {
    "r": 255,
    "g": 255,
    "b": 255,
    "brightness": 50,
    "flash": false,
    "flashRate": 2.0,
    "connected": true,
    "deviceCount": 2,
    "devices": ["/dev/ttyACM1", "/dev/ttyACM3"],
    "lastError": null
  }
}
```

`GET /api/state` and partial `POST /api/state` requests remain available as an HTTP
fallback.

## Troubleshooting

- **The page says the NeoKey is offline:** confirm the board has both `if00` and `if02`
  entries under `/dev/serial/by-id/`, and confirm the service account belongs to `dialout`.
- **Only one of several boards responds:** verify every board contains this repository's
  `boot.py` and `code.py`. A factory board exposes only one serial interface.
- **The switch appears stuck:** reload the page. Responses include `Cache-Control: no-store`,
  and WebSocket state updates synchronize current tabs.
- **The color changes slowly:** confirm the status reads `live 10 Hz`; inspect `/ws` in the
  browser's Network developer tools.
- **CircuitPython reports an error:** open the board's `if00` console at 115200 baud and
  inspect the traceback. The `if02` port is reserved for control messages.
