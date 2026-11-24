// Start-up, live stream and page state.

import { Cards } from "./cards.js";
import { ChartSet, METRICS } from "./charts.js";
import { formatAge, formatTime, tempUnit, toggleUnit } from "./units.js";

const $ = (id) => document.getElementById(id);
const MINUTE = 60000, HOUR = 60 * MINUTE, DAY = 24 * HOUR;
const WINDOWS = [
  ["15 min", 15 * MINUTE], ["1 h", HOUR], ["6 h", 6 * HOUR],
  ["24 h", DAY], ["7 d", 7 * DAY], ["30 d", 30 * DAY],
];

const charts = new ChartSet($("charts"));
const cards = new Cards($("cards"), $("table-head"), $("table-body"));

let span = WINDOWS[0][1];
let current = null;     // last /api/current response
let clockOffset = 0;    // server clock minus browser clock
let lastSeen = null;    // server-clock time of the newest packet
let lastTs = null;      // newest reading timestamp
let lastId = 0;
let loads = 0;
let loading = false;
let reloadTimer = null;
let buffered = [];
let connected = false;

const serverNow = () => Date.now() + clockOffset;
const isHardware = () => (current?.mode ?? "hardware") === "hardware";
// Simulated and replayed time is unrelated to the clock: follow the data.
const windowEnd = () => (isHardware() || lastTs === null ? Math.max(serverNow(), lastTs ?? 0) : lastTs);

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

function applyCurrent(data) {
  current = data;
  clockOffset = data.server_ms - Date.now();
  if (data.age_s !== null) lastSeen = data.server_ms - data.age_s * 1000;
  charts.timeZone = cards.timeZone = data.station.timezone;
  $("station").textContent = `— ${data.station.name}`;
  const badge = $("mode");
  badge.hidden = data.mode === null;
  badge.className = `badge ${data.mode ?? ""}`;
  badge.textContent = { simulator: "⚠ SIMULATED DATA", replay: "↻ REPLAYED DATA" }[data.mode] ?? "● LIVE (hardware)";
}

/** Fetch state and history. Readings that arrive meanwhile are buffered. */
async function load() {
  const token = ++loads;
  loading = true;
  try {
    const now = await getJson("api/current");
    if (token !== loads) return;
    const modeChanged = current && current.mode !== now.mode;
    applyCurrent(now);
    lastTs = now.last_reading_ms;
    const from = Math.max(0, Math.floor(windowEnd() - span));
    const metrics = METRICS.map((m) => m.key).join(",");
    const history = await getJson(`api/history?from=${from}&metrics=${metrics}`);
    if (token !== loads) return;
    charts.load(history, span);
    lastId = history.last_id;
    if (modeChanged) cards.clear();
    if (now.last_reading_ms !== null) cards.show({ ...now, ts_ms: now.last_reading_ms });
    else cards.clear();
    loading = false;
    const pending = buffered;
    buffered = [];
    pending.forEach(onReading);
    charts.end = windowEnd();
    charts.drawNow();
    cards.render();
    renderState();
  } catch (error) {
    console.warn(error);
    if (token === loads) scheduleLoad(3000);
  }
}

function scheduleLoad(delay) {
  reloadTimer ??= setTimeout(() => { reloadTimer = null; load(); }, delay);
}

function onReading(reading) {
  if (loading) {
    buffered.push(reading);
    if (buffered.length > 5000) buffered.shift();
    return;
  }
  if (reading.id <= lastId) return; // already in the history
  if (reading.source !== current.mode) {
    load(); // the data source changed
    return;
  }
  lastId = reading.id;
  if (!charts.add(reading)) scheduleLoad(1000); // late data: fetch it in order
  const newest = lastTs === null || reading.ts_ms >= lastTs;
  const seen = isHardware() ? reading.ts_ms : serverNow();
  lastSeen = Math.max(lastSeen ?? 0, seen);
  if (newest) {
    lastTs = reading.ts_ms;
    cards.show(reading);
  }
  charts.draw(windowEnd());
}

function renderState() {
  $("unreachable").hidden = connected;
  if (!current) return;
  const never = lastSeen === null;
  const ids = current.tag.allowed_tx_ids;
  $("waiting").hidden = !never;
  $("waiting-title").textContent =
    `Waiting for first packet from ${ids.length ? `tag ${ids.join(", ")}` : "any tag"}… ` +
    `(receiver heard ${current.bad_crc_1h} noise packets in the last hour)`;
  if (never) {
    $("offline").hidden = true;
    $("status").textContent = connected ? "○ Waiting for data" : "✕ Disconnected";
    return;
  }
  const age = Math.max(0, (serverNow() - lastSeen) / 1000);
  const offline = age >= current.tag.offline_warning_s;
  const critical = age >= current.tag.offline_critical_s;
  cards.setStale(offline || !connected);
  $("offline").hidden = !offline;
  $("offline").className = `banner ${critical ? "critical" : "warning"}`;
  $("offline").textContent =
    `${critical ? "✕" : "⚠"} Tag offline — last packet ${formatAge(age)} ago` +
    (lastTs === null ? "" : ` (reading from ${formatTime(lastTs, charts.timeZone)})`);
  $("status").textContent = connected ? `Last packet ${formatAge(age)} ago` : "✕ Disconnected";
}

function connect() {
  const source = new EventSource("api/stream");
  source.addEventListener("open", () => {
    connected = true;
    renderState();
    load(); // also fills in whatever was missed while disconnected
  });
  source.addEventListener("reading", (event) => onReading(JSON.parse(event.data)));
  source.addEventListener("error", () => {
    connected = false;
    renderState();
    // The browser retries by itself unless the connection was refused outright.
    if (source.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
  });
}

for (const [label, ms] of WINDOWS) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("aria-pressed", String(ms === span));
  button.addEventListener("click", () => {
    span = ms;
    for (const other of $("windows").children) other.setAttribute("aria-pressed", String(other === button));
    load();
  });
  $("windows").append(button);
}

const showUnit = () => { $("unit").textContent = tempUnit(); };
$("unit").addEventListener("click", () => {
  toggleUnit();
  showUnit();
  charts.changeUnit();
  charts.drawNow();
  cards.render();
});
showUnit();

setInterval(() => {
  renderState();
  if (current && lastSeen !== null) {
    cards.render();
    if (isHardware()) charts.draw(windowEnd());
  }
}, 1000);
setInterval(() => { if (span >= DAY && connected) load(); }, MINUTE);
setInterval(async () => {
  if (!connected || loading || lastSeen !== null) return;
  try { applyCurrent(await getJson("api/current")); } catch { /* shown by the stream state */ }
}, 10000);
connect();
