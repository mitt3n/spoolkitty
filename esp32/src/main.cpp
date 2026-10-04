/*
  Spoolman Kitty - ESP32 print agent
  -----------------------------------
  Sits next to the MX-series cat printer. It asks the Spoolman Kitty container
  for queued labels and sends them to the printer over Bluetooth LE.

  Built with PlatformIO - see platformio.ini for board/platform/library setup.

  Loop:
    1. Scan ~3 s for the printer. If it isn't advertising (off, asleep, or
       connected to a phone), skip claiming, so a browser tab can take the job instead.
    2. Long-poll GET /api/agent/next. 204 = nothing to do, 200 = raw printer bytes.
    3. Connect, stream the bytes with flow control, disconnect.
    4. POST /api/agent/done?ok=1 (or ok=0&error=...) so the server can untick
       Spoolman or requeue the label.
*/
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <NimBLEDevice.h>

// ---------------- configure me ----------------
const char* WIFI_SSID   = "your-wifi";
const char* WIFI_PASS   = "your-password";
const char* BRIDGE_URL  = "http://192.168.1.10:8080";   // the Spoolman Kitty container, no trailing slash
const char* AGENT_NAME  = "esp32-printer";
const char* PRINTER_MAC = "";                            // optional, e.g. "aa:bb:cc:dd:ee:ff"; empty = match by name
const char* NAME_PREFIXES[] = {"MX05", "MX06", "MX08", "MX09", "MX10", "MX11", "GB0", "GT01", "YT01"};
// ----------------------------------------------

static NimBLEUUID SVC_UUID("ae30"), SVC_ALT_UUID("af30"), TX_UUID("ae01"), RX_UUID("ae02");
static volatile bool paused = false;
static NimBLEAddress printerAddr;
static String printerName;

static bool nameMatches(const std::string& n) {
  for (auto p : NAME_PREFIXES) {
    if (strncasecmp(n.c_str(), p, strlen(p)) == 0) return true;
  }
  return false;
}

static bool findPrinter() {
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setActiveScan(true);
  NimBLEScanResults res = scan->getResults(3000, false);
  bool found = false;
  for (int i = 0; i < res.getCount() && !found; i++) {
    const NimBLEAdvertisedDevice* d = res.getDevice(i);
    bool match = strlen(PRINTER_MAC) ? (d->getAddress().toString() == std::string(PRINTER_MAC))
                                     : (d->haveName() && nameMatches(d->getName()));
    if (match) {
      printerAddr = d->getAddress();
      printerName = d->getName().c_str();
      found = true;
    }
  }
  scan->clearResults();
  return found;
}

static void onNotify(NimBLERemoteCharacteristic*, uint8_t* data, size_t len, bool) {
  // 51 78 AE .. .. .. 10 = pause, 00 = resume
  if (len >= 7 && data[2] == 0xAE) paused = (data[6] == 0x10);
}

static bool printBytes(const uint8_t* buf, size_t len, String& err) {
  NimBLEClient* c = NimBLEDevice::createClient();
  c->setConnectTimeout(8000);
  bool ok = false;
  do {
    if (!c->connect(printerAddr)) { err = "connect failed"; break; }
    NimBLERemoteService* svc = c->getService(SVC_UUID);
    if (!svc) svc = c->getService(SVC_ALT_UUID);
    if (!svc) { err = "printer service not found"; break; }
    NimBLERemoteCharacteristic* tx = svc->getCharacteristic(TX_UUID);
    if (!tx) { err = "write characteristic not found"; break; }
    NimBLERemoteCharacteristic* rx = svc->getCharacteristic(RX_UUID);
    paused = false;
    if (rx && rx->canNotify()) rx->subscribe(true, onNotify);

    size_t chunk = c->getMTU() > 23 ? min<size_t>(c->getMTU() - 3, 180) : 20;
    uint32_t delayMs = chunk > 20 ? 15 : 6;
    size_t i = 0;
    ok = true;
    while (i < len) {
      uint32_t t0 = millis();
      while (paused) {
        if (millis() - t0 > 30000) { err = "printer stayed paused"; ok = false; break; }
        delay(20);
      }
      if (!ok) break;
      size_t n = min(chunk, len - i);
      if (!tx->writeValue(buf + i, n, false)) { err = "write failed"; ok = false; break; }
      i += n;
      delay(delayMs);
    }
    if (ok) delay(1500);  // let the printer drain its buffer before we drop the link
  } while (false);
  if (c->isConnected()) c->disconnect();
  NimBLEDevice::deleteClient(c);
  return ok;
}

static String urlEncode(const String& s) {
  String o;
  const char* hex = "0123456789ABCDEF";
  for (size_t i = 0; i < s.length(); i++) {
    char ch = s[i];
    if (isalnum((unsigned char)ch) || ch == '-' || ch == '_' || ch == '.') o += ch;
    else { o += '%'; o += hex[(ch >> 4) & 0xF]; o += hex[ch & 0xF]; }
  }
  return o;
}

static void reportDone(const String& jobId, bool ok, const String& err) {
  HTTPClient http;
  String url = String(BRIDGE_URL) + "/api/agent/done?kind=esp32&agent=" + AGENT_NAME +
               "&job=" + jobId + "&ok=" + (ok ? "1" : "0") + "&error=" + urlEncode(err);
  http.begin(url);
  http.POST("");
  http.end();
}

static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print('.'); }
  Serial.println(WiFi.status() == WL_CONNECTED ? " connected " + WiFi.localIP().toString() : " failed");
}

void setup() {
  Serial.begin(115200);
  NimBLEDevice::init("");
  NimBLEDevice::setMTU(247);
  ensureWifi();
}

void loop() {
  ensureWifi();
  if (WiFi.status() != WL_CONNECTED) { delay(5000); return; }

  if (!findPrinter()) {
    // Still check in so the web UI shows this agent as online, but don't claim anything.
    HTTPClient ping;
    ping.begin(String(BRIDGE_URL) + "/api/agent/next?kind=esp32&agent=" + AGENT_NAME + "&printer=&wait=0&peek=1");
    ping.GET();
    ping.end();
    delay(5000);
    return;
  }

  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(30000);
  const char* hdrs[] = {"X-Job-Id", "X-Spool-Id"};
  http.begin(String(BRIDGE_URL) + "/api/agent/next?kind=esp32&agent=" + AGENT_NAME +
             "&printer=" + urlEncode(printerName) + "&wait=20");
  http.collectHeaders(hdrs, 2);
  int code = http.GET();
  if (code != 200) {
    http.end();
    if (code < 0) delay(5000);
    return;
  }

  String jobId = http.header("X-Job-Id");
  String spoolId = http.header("X-Spool-Id");
  int len = http.getSize();
  if (len <= 0 || len > 200000) { http.end(); reportDone(jobId, false, "bad job size"); return; }

  uint8_t* buf = (uint8_t*)malloc(len);
  if (!buf) { http.end(); reportDone(jobId, false, "out of memory"); return; }
  WiFiClient* s = http.getStreamPtr();
  int got = 0;
  uint32_t t0 = millis();
  while (got < len && millis() - t0 < 15000) {
    int n = s->read(buf + got, len - got);
    if (n > 0) got += n; else delay(2);
  }
  http.end();

  String err;
  bool ok = false;
  if (got != len) err = "download incomplete";
  else ok = printBytes(buf, len, err);
  free(buf);

  Serial.printf("Spool #%s: %s %s\n", spoolId.c_str(), ok ? "printed" : "FAILED", err.c_str());
  reportDone(jobId, ok, err);
}
