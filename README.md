# Spoolkitty

Automatic Spoolman QR labels on a Bluetooth "Kitty" cat printer (MX05 / MX06 / MX08 / MX10).

Tick **Print label** on a spool in Spoolman and a sticker comes out. When it prints, the checkbox unticks itself.

```
 Spoolman ──poll every 5 s──▶  spoolman-kitty (Docker, :8080)
    ▲                          ├─ renders the label and builds the printer byte stream
    └──── untick on success ───┤  job queue with leases and retries
                               │
          GET /api/agent/next  │  (long-poll; whichever agent is free takes the job)
              ┌────────────────┴─────────────────┐
     Browser tab (Web Bluetooth)          ESP32 next to the printer
     Chrome/Edge on a PC or Android       (Wi-Fi + BLE, no screen)
              └──────── Bluetooth LE ──▶ MX printer ◀──┘
```

The container never touches Bluetooth, so it can run on the Ubuntu server with no dongle. The Bluetooth link comes from a **print agent**:

- **Browser tab:** open the web UI in Chrome or Edge, click **Pair printer** and leave the tab open. It works in a background tab, and on an Android phone parked by the printer.
- **ESP32:** a small board next to the printer. It works with no browser open.

Run either one or both. If both are up, whichever can see the printer takes the job, and a failed job goes back to the queue after 15 s for any agent to pick up.

## 1. Run the container

On the server:

```bash
# edit SPOOLMAN_URL / PUBLIC_SPOOLMAN_URL in .env first
docker compose up -d --build
docker compose logs -f
```

On first start the container adds a **Print label** checkbox to every spool in Spoolman. Open `http://<server>:8080`.

| env | default | |
|---|---|---|
| `SPOOLMAN_URL` | `http://spoolman:8000` | How the container reaches Spoolman. If both run in the same compose project, use the service name. |
| `PUBLIC_SPOOLMAN_URL` | = `SPOOLMAN_URL` | The address encoded in the QR code. Must be one your phone can open. |
| `QR_MODE` | `url` | `url` makes a phone camera open the spool page. `spoolman` uses Spoolman's native `web+spoolman:s-<id>` format. |
| `ENERGY` | `65535` | Print darkness. Lower it (e.g. `40000`) if stickers smear. |
| `LABEL_HEIGHT_PX` | `240` | 8 px = 1 mm, so 240 px gives a 30 mm sticker on the 57 mm roll. |
| `FEED_PX` | `90` | Blank rows fed after each label so it clears the tear bar. |
| `POLL_SECONDS` | `5` | How often Spoolman is checked. |

## 2a. Browser agent (Web Bluetooth over plain http)

Chrome only allows Bluetooth on secure pages. For a LAN http address you whitelist it once per browser:

1. Open `chrome://flags/#unsafely-treat-insecure-origin-as-secure` (Edge: `edge://flags/#unsafely-treat-insecure-origin-as-secure`).
2. Enter `http://<server>:8080`, set it to **Enabled** and relaunch.
3. Open `http://<server>:8080` and click **Pair printer**. The browser asks you to pick the printer once.
4. Leave the tab open. With **Print queued labels from this tab** ticked, it prints whatever is queued.

If the page shows **Web Bluetooth needs a secure page**, the flag isn't set for that exact address (check the port).

- **Android:** the same flag works in Chrome for Android. Turn on **Keep screen awake** so the phone doesn't sleep.
- **Reconnecting after a reload:** Chrome remembers the paired printer and reconnects to it after a reload. If it doesn't, also enable `chrome://flags/#enable-web-bluetooth-new-permissions-backend`.
- **Other browsers:** Firefox and iOS Safari have no Web Bluetooth.

## 2b. ESP32 agent

It's a [PlatformIO](https://platformio.org/) project in `esp32/`. Board: any ESP32 with BLE (ESP32, C3 or S3; **not** the S2).

1. Install the [PlatformIO IDE extension](https://platformio.org/install/ide?install=vscode) for VS Code, or the [PlatformIO Core CLI](https://docs.platformio.org/en/latest/core/installation/index.html) (`pip install platformio`).
2. Fill in `WIFI_SSID`, `WIFI_PASS` and `BRIDGE_URL` (`http://<server>:8080`) near the top of `esp32/src/main.cpp`. Optionally set `PRINTER_MAC` to the address the web UI shows.
3. Build and flash with the environment matching your board — `esp32dev` (plain ESP32), `esp32-c3`, or `esp32-s3`:
   ```bash
   cd esp32
   pio run -e esp32dev -t upload -t monitor
   ```
   (In the VS Code extension: pick the environment in the status bar, then use the Upload and Monitor icons.)

`esp32/platformio.ini` pulls in NimBLE-Arduino 2.x automatically and sets the partition table to Huge APP. It builds against the [pioarduino](https://github.com/pioarduino/platform-espressif32) community platform rather than the official `platformio.org` one, since the official one is still pinned to an older Arduino-ESP32 core that NimBLE-Arduino 2.x doesn't support.

The ESP32 only claims a job when it can see the printer advertising, and it disconnects after each label. That leaves the printer free for a browser tab or the phone app. The web UI's **Print agents** panel shows it as `esp32-printer`.

## Using it

- **From Spoolman:** tick **Print label** on any spool and save. Several spools can be ticked at once.
- **From the web UI:** use **Preview** or **Print** on any spool (manual prints never touch Spoolman), and **Reprint** or **Cancel** in the queue.
- **Printer off or asleep:** the job waits in the queue and retries. Nothing is lost, and the checkbox stays ticked until the label actually prints.
- **Changed your mind:** unticking the box in Spoolman before the label prints cancels the job.

## Notes

- There's no login. Keep port 8080 on your LAN and don't forward it to the internet.
- Only one device can connect to the printer at a time. Close the Kitty phone app while agents are running.
- The protocol is the GB/MX cat-printer protocol, byte-matched to [rbaron/catprinter](https://github.com/rbaron/catprinter). MX models ignore the paper-feed command, so the app feeds paper by sending blank rows, the approach [NaitLee/Cat-Printer](https://github.com/NaitLee/Cat-Printer) uses.

## License

[GPL-3.0](LICENSE).
