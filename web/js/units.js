// Unit conversion and formatting. The API always uses °C.

let fahrenheit = false;
try {
  fahrenheit = localStorage.getItem("unit") === "F";
} catch { /* storage may be blocked */ }

export const tempUnit = () => (fahrenheit ? "°F" : "°C");

export function toggleUnit() {
  fahrenheit = !fahrenheit;
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
