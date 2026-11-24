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

export function chooseUnit(unit) {
  fahrenheit = unit === "F";
  chosen = true;
  try {
    localStorage.setItem("unit", unit);
  } catch { /* storage may be blocked */ }
}

export const MISSING = "—";
export const isMissing = (value) => value === null || value === undefined;
export const convertTemp = (c) => (isMissing(c) ? null : fahrenheit ? (c * 9) / 5 + 32 : c);
/** A difference in temperature: no 32 degree offset. */
export const convertDelta = (c) => (isMissing(c) ? null : fahrenheit ? (c * 9) / 5 : c);

export function formatNumber(value, digits) {
  if (isMissing(value)) return MISSING;
  const text = value.toFixed(digits);
  return Number(text) === 0 ? text.replace("-", "") : text.replace("-", "−");
}

export const formatTemp = (c) => formatNumber(convertTemp(c), 1);
export const withUnit = (c) => (isMissing(c) ? MISSING : `${formatTemp(c)} ${tempUnit()}`);

/** "4 minutes", for sentences. */
export function formatAge(seconds) {
  const say = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (seconds < 90) return say(Math.round(seconds), "second");
  if (seconds < 5400) return say(Math.round(seconds / 60), "minute");
  if (seconds < 172800) return say(Math.round(seconds / 3600), "hour");
  return say(Math.round(seconds / 86400), "day");
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
  return formatter(timeZone, CLOCK).format(ms);
}

export const formatTime = (ms, timeZone) =>
  formatter(timeZone, { ...DATE, ...CLOCK, second: "2-digit", timeZoneName: "short" }).format(ms);

export const formatMoment = (ms, timeZone) => formatter(timeZone, { ...DATE, ...CLOCK }).format(ms);

export const formatClock = (ms, timeZone) =>
  isMissing(ms) ? MISSING : formatter(timeZone, CLOCK).format(ms);

/** "Fri, Nov 20" for a YYYY-MM-DD date. */
export const formatDay = (date) =>
  formatter("UTC", { weekday: "short", ...DATE }).format(Date.parse(`${date}T00:00:00Z`));

export function formatPlace(latitude, longitude) {
  const part = (v, plus, minus) => `${Math.abs(v).toFixed(2)}° ${v >= 0 ? plus : minus}`;
  return `${part(latitude, "N", "S")}, ${part(longitude, "E", "W")}`;
}

/** Milliseconds that local time is ahead of UTC. */
export function zoneOffset(ms, timeZone) {
  const part = Object.fromEntries(formatter(timeZone, {
    year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric", hourCycle: "h23",
  }).formatToParts(ms).map((p) => [p.type, Number(p.value)]));
  return Date.UTC(part.year, part.month - 1, part.day, part.hour, part.minute, part.second) - Math.floor(ms / 1000) * 1000;
}
