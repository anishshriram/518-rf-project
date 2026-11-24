// Unit conversion and formatting. The API always uses °C.

let fahrenheit = false;
let chosen = false; // the viewer's own choice beats the station's default
try {
  const unit = localStorage.getItem("unit");
  chosen = unit !== null;
  fahrenheit = unit === "F";
} catch { /* storage may be blocked */ }

/** Use the station's unit unless the viewer chose one. True if it changed. */
export function setDefaultUnit(unit) {
  const before = fahrenheit;
  if (!chosen) fahrenheit = unit === "F";
  return fahrenheit !== before;
}

export const tempUnit = () => (fahrenheit ? "°F" : "°C");

export function toggleUnit() {
  fahrenheit = !fahrenheit;
  chosen = true;
  try {
    localStorage.setItem("unit", fahrenheit ? "F" : "C");
  } catch { /* storage may be blocked */ }
}

export const convertTemp = (c) => (c === null || !fahrenheit ? c : (c * 9) / 5 + 32);

export const formatNumber = (value, digits) =>
  value === null || value === undefined ? "–" : value.toFixed(digits).replace("-", "−");

export function formatAge(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

// Times are always shown in the station's time zone.
const cache = new Map();
function formatter(timeZone, options) {
  const key = timeZone + JSON.stringify(options);
  if (!cache.has(key)) cache.set(key, new Intl.DateTimeFormat("en-US", { timeZone, ...options }));
  return cache.get(key);
}

const CLOCK = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
const DATE = { month: "short", day: "numeric" };

export function formatTick(ms, timeZone, spanMs) {
  if (spanMs > 2 * 86400000) return formatter(timeZone, DATE).format(ms);
  if (spanMs > 12 * 3600000) return formatter(timeZone, { ...DATE, ...CLOCK }).format(ms);
  return formatter(timeZone, CLOCK).format(ms);
}

export const formatTime = (ms, timeZone) =>
  formatter(timeZone, { ...DATE, ...CLOCK, second: "2-digit", timeZoneName: "short" }).format(ms);

export const formatClock = (ms, timeZone) =>
  ms === null || ms === undefined ? "–" : formatter(timeZone, CLOCK).format(ms);

/** Shows clearly where the data comes from. */
export function modeBadge(badge, mode) {
  badge.hidden = mode === null;
  badge.className = `badge ${mode ?? ""}`;
  badge.textContent = { simulator: "⚠ SIMULATED DATA", replay: "↻ REPLAYED DATA" }[mode] ?? "● LIVE (hardware)";
}
