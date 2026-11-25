// Start-up, live stream and page state.

import { Alerts } from "./alerts.js";
import { ChartSet, HISTORY_KEYS } from "./charts.js";
import { renderHealth, renderWeek } from "./daily.js";
import { $, getJson, segmented, setText, showSource } from "./dom.js";
import { icon } from "./icons.js";
import { Now } from "./now.js";
import { chooseUnit, formatAge, formatPlace, formatTime, setDefaultUnit, tempUnit } from "./units.js";

const MINUTE = 60000, HOUR = 60 * MINUTE, DAY = 24 * HOUR;
const WINDOWS = [
  ["15 min", 15 * MINUTE], ["1 h", HOUR], ["6 h", 6 * HOUR],
  ["24 h", DAY], ["7 d", 7 * DAY], ["30 d", 30 * DAY],
];

function saved(key, allowed, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key));
    return allowed.includes(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage may be blocked */ }
}

const charts = new ChartSet($("charts"), $("signal-chart"), {
  announce: (text) => setText($("chart-announce"), text),
  onSummary: (text) => setText($("chart-summary"), text),
});
const now = new Now();
const alerts = new Alerts({
  main: $("alert"), others: $("other-alerts"), quiet: $("no-alerts"), timeline: $("timeline"), more: $("more-events"),
});

let span = saved("span", WINDOWS.map((w) => w[1]), 6 * HOUR);
let current = null;     // last /api/current response
let clockOffset = 0;    // server clock minus browser clock
let lastSeen = null;    // server-clock time of the newest packet
let lastTs = null;      // newest reading timestamp
let lastId = 0;
let shadedUntil = Infinity; // night shading is known up to here
let days = null;
let eventsTimer = null;
let loads = 0;
let loading = false;
let reloadTimer = null;
let buffered = [];
let connected = null;   // null until the first attempt has an outcome

const serverNow = () => Date.now() + clockOffset;
const isHardware = () => (current?.mode ?? "hardware") === "hardware";
// Simulated and replayed time is unrelated to the clock: follow the data.
const windowEnd = () => (isHardware() || lastTs === null ? Math.max(serverNow(), lastTs ?? 0) : lastTs);

function applyCurrent(data) {
  current = data;
  clockOffset = data.server_ms - Date.now();
  if (data.age_s !== null) lastSeen = data.server_ms - data.age_s * 1000;
  now.conditions = data;
  charts.alpha = data.smoothing_alpha;
  charts.mode = data.mode;
  charts.timeZone = now.timeZone = alerts.timeZone = data.station.timezone;
  if (setDefaultUnit(data.display_temperature)) showUnit();
  setText($("station"), data.station.name);
  document.title = `${data.station.name} — PIP Weather`;
  setText($("place"), data.station.latitude === null ? "" : formatPlace(data.station.latitude, data.station.longitude));
  document.body.classList.toggle("view-only", !data.can_edit);
  $("enable-notifications").hidden = !data.browser_notifications
    || !("Notification" in window) || Notification.permission !== "default";
  showSource(data.mode);
}

async function loadEvents() {
  if (lastTs === null) {
    alerts.setEvents([]);
    charts.events = now.events = [];
    return;
  }
  const from = Math.max(0, Math.floor(windowEnd() - Math.max(span, 7 * DAY)));
  try {
    const events = await getJson(`api/events?from=${from}`);
    alerts.setEvents(events);
    charts.events = now.events = events;
    charts.draw(windowEnd());
  } catch (error) {
    console.warn(error);
  }
}

async function loadPanels() {
  try {
    const [health, recent] = await Promise.all([getJson("api/health"), getJson("api/daily")]);
    renderHealth($("health"), $("health-summary"), health, charts.timeZone);
    days = recent.slice(-7);
    renderWeek($("days"), days);
  } catch (error) {
    console.warn(error);
  }
}

/** Events often come in groups: fetch the list once for all of them. */
function scheduleEvents() {
  eventsTimer ??= setTimeout(() => { eventsTimer = null; loadEvents(); }, 500);
}

/** Fetch state and history. Readings that arrive meanwhile are buffered. */
async function load() {
  const token = ++loads;
  loading = true;
  try {
    const data = await getJson("api/current");
    if (token !== loads) return;
    const modeChanged = current && current.mode !== data.mode;
    applyCurrent(data);
    lastTs = data.last_reading_ms;
    const from = Math.max(0, Math.floor(windowEnd() - span));
    const history = await getJson(`api/history?from=${from}&metrics=${HISTORY_KEYS.join(",")}`);
    if (token !== loads) return;
    charts.load(history, span);
    lastId = history.last_id;
    shadedUntil = lastTs === null ? Infinity : windowEnd() + DAY;
    if (modeChanged || lastTs === null) now.clear();
    if (lastTs !== null) now.show({ ...data, ts_ms: lastTs });
    loading = false;
    const pending = buffered;
    buffered = [];
    pending.forEach(onReading);
    charts.end = windowEnd();
    charts.drawNow();
    $("charts").classList.remove("updating");
    renderState();
    loadEvents();
    loadPanels();
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
  lastSeen = Math.max(lastSeen ?? 0, isHardware() ? reading.ts_ms : serverNow());
  if (newest) {
    lastTs = reading.ts_ms;
    now.show(reading);
  }
  charts.draw(windowEnd());
  if (windowEnd() > shadedUntil) load();
}

function renderConnection() {
  const state = connected === null ? "waiting" : connected ? "up" : "down";
  if ($("connection").dataset.state !== state) {
    $("connection").dataset.state = state;
    $("connection-icon").replaceChildren(icon(state === "down" ? "unlink" : "link"));
  }
  setText($("connection-text"), { waiting: "Connecting…", up: "Connected", down: "Server unreachable, retrying" }[state]);
}

function renderState() {
  renderConnection();
  if (!current) return;
  const never = lastSeen === null;
  const ids = current.tag.allowed_tx_ids;
  $("waiting").hidden = !never;
  for (const id of ["now", "trends", "lower"]) $(id).hidden = never;
  $("attention").hidden = never;
  if (never) {
    setText($("waiting-title"), `Waiting for the first packet from ${ids.length ? `tag ${ids.join(" or ")}` : "any tag"}`);
    const noise = current.bad_crc_1h;
    setText($("waiting-text"), noise
      ? `The receiver is working: it heard ${noise} noise packet${noise === 1 ? "" : "s"} in the last hour, but none from the tag. If it stays quiet:`
      : "Nothing has been received in the last hour. Check that the receiver is plugged in and the collector is running, then:");
    setText($("freshness"), "No readings yet");
    $("freshness").classList.remove("old");
    alerts.setOffline(null);
    return;
  }
  const age = Math.max(0, (serverNow() - lastSeen) / 1000);
  const offline = age >= current.tag.offline_warning_s;
  const critical = age >= current.tag.offline_critical_s;
  setText($("freshness"), age < 10 && !offline ? "Last reading just now" : `Last reading ${formatAge(age)} ago`);
  $("freshness").classList.toggle("old", offline);
  alerts.setOffline(offline && connected ? {
    severity: critical ? "critical" : "warning",
    start_ms: lastTs,
    // No running seconds here: this text is announced by screen readers.
    explanation: `The last reading is from ${formatTime(lastTs, charts.timeZone)}.`,
  } : null);
  now.render({ staleAge: offline ? age : null, connected: connected !== false });
  if ($("health-details").open) now.renderTable();
}

function connect() {
  const source = new EventSource("api/stream");
  source.addEventListener("open", () => {
    connected = true;
    renderState();
    load(); // also fills in whatever was missed while disconnected
  });
  source.addEventListener("reading", (event) => onReading(JSON.parse(event.data)));
  source.addEventListener("alert", (event) => {
    alerts.onAlert(JSON.parse(event.data), { notifications: current?.browser_notifications, sound: current?.sound });
    scheduleEvents();
  });
  for (const name of ["alert_cleared", "inference"]) source.addEventListener(name, scheduleEvents);
  source.addEventListener("settings", () => load());
  source.addEventListener("calibration", () => load()); // the light scale changed
  source.addEventListener("error", () => {
    connected = false;
    renderState();
    // The browser retries by itself unless the connection was refused outright.
    if (source.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
  });
}

function showUnit() {
  for (const button of $("units").children) button.setAttribute("aria-pressed", String(button.textContent === tempUnit()));
  if (days) renderWeek($("days"), days);
  if (!current) return;
  if (charts.end) charts.drawNow();
  renderState();
}

segmented($("units"), [["°C", "C"], ["°F", "F"]], tempUnit().slice(1), (unit) => {
  chooseUnit(unit);
  showUnit();
});
segmented($("windows"), WINDOWS, span, (ms) => {
  span = ms;
  save("span", ms);
  $("charts").classList.add("updating");
  load();
});
$("smooth").checked = charts.smooth = saved("smooth", [true, false], false);
$("smooth").addEventListener("change", () => {
  charts.smooth = $("smooth").checked;
  save("smooth", charts.smooth);
  charts.drawNow();
});
$("suspect").addEventListener("change", () => {
  charts.showSuspect = $("suspect").checked;
  charts.drawNow();
});
for (const id of ["details", "health-details"]) $(id).addEventListener("toggle", renderState);
$("no-alerts-icon").append(icon("check"));
$("enable-notifications").addEventListener("click", async () => {
  await Notification.requestPermission();
  $("enable-notifications").hidden = true;
});

renderConnection();
setInterval(() => {
  renderState();
  if (current && lastSeen !== null && isHardware()) charts.draw(windowEnd());
}, 1000);
setInterval(() => { if (span >= DAY && connected) load(); }, MINUTE);
setInterval(async () => {
  if (!connected || loading) return;
  loadPanels();
  try {
    const data = await getJson("api/current");
    // A changed source is handled by load(), which also replaces the charts.
    if (!loading && data.mode === current?.mode) applyCurrent(data);
  } catch { /* shown by the stream state */ }
}, 10000);
connect();
