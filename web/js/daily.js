// Tables of daily summaries and the sensor health panel.

import { convertTemp, formatAge, formatClock, formatNumber, formatTime, tempUnit } from "./units.js";

export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export const temp = (c) => formatNumber(convertTemp(c ?? null), 1);

/** Minutes between detected and calculated sunrise or sunset. */
function offset(detected, astronomical) {
  if (detected === null || astronomical === null) return "";
  const minutes = Math.round((detected - astronomical) / 60000);
  return ` (${minutes >= 0 ? "+" : "−"}${Math.abs(minutes)} min)`;
}

// [heading, cell text, explanation, only on the history page]
const COLUMNS = [
  ["Date", (d) => d.local_date + (d.in_progress ? " (so far)" : "")],
  [() => `High (${tempUnit()})`, (d, z) => `${temp(d.temp_max_c)} at ${formatClock(d.temp_max_time_ms, z)}`],
  [() => `Low (${tempUnit()})`, (d, z) => `${temp(d.temp_min_c)} at ${formatClock(d.temp_min_time_ms, z)}`],
  [() => `Mean (${tempUnit()})`, (d) => temp(d.temp_avg_c), "", true],
  ["Humidity", (d) => `${formatNumber(d.rh_min, 0)}–${formatNumber(d.rh_max, 0)}%`, "", true],
  ["Sky", (d) => d.sky_summary ?? "–", "Estimated from daytime light"],
  ["Sun hours", (d) => formatNumber(d.bright_sun_hours, 1), "Hours with at least 70% of clear-sky light"],
  ["Daylight (h)", (d) => `${formatNumber(d.daylight_hours_measured, 1)} of ${formatNumber(d.daylight_hours_astronomical, 1)}`,
    "Measured at the sensor, of the time between sunrise and sunset", true],
  ["Light from", (d, z) => formatClock(d.sunrise_detected_ms, z) + offset(d.sunrise_detected_ms, d.sunrise_astro_ms),
    "When light first passed 5% of full sun. Around 40 min after sunrise is normal in winter; much later suggests shade to the east.", true],
  ["Light until", (d, z) => formatClock(d.sunset_detected_ms, z) + offset(d.sunset_detected_ms, d.sunset_astro_ms),
    "When light left the sensor, compared with sunset", true],
  ["HDD", (d) => formatNumber(d.hdd, 1), "Heating degree days, proportional to heating energy demand"],
  ["CDD", (d) => formatNumber(d.cdd, 1), "Cooling degree days", true],
  ["GDD", (d) => formatNumber(d.gdd, 1), "Growing degree days"],
  ["Packets", (d) => `${formatNumber(d.packet_success_pct, 1)}%`, "Share of expected packets received", true],
  ["Coverage", (d) => `${formatNumber(d.data_coverage_pct, 0)}%`, "Share of the day with data"],
  ["Notes", (d) => d.notes || "", "", true],
];

export function renderDays(table, days, timeZone, full) {
  const columns = COLUMNS.filter((c) => full || !c[3]);
  const head = element("tr");
  for (const [heading, , title] of columns) {
    const cell = element("th", "", typeof heading === "function" ? heading() : heading);
    cell.title = title ?? "";
    head.append(cell);
  }
  const rows = [...days].reverse().map((day) => {
    const row = element("tr");
    row.append(...columns.map(([heading, text]) => element("td", heading === "Notes" ? "notes" : "", text(day, timeZone))));
    return row;
  });
  if (!rows.length) {
    const cell = element("td", "", "No data yet.");
    cell.colSpan = columns.length;
    rows.push(element("tr"));
    rows[0].append(cell);
  }
  table.replaceChildren(head, ...rows);
}

const STATUS = {
  online: "● Online", offline_warning: "⚠ Offline", offline_critical: "✕ Offline", never_seen: "○ Never seen",
};

function signal(dbm) {
  if (dbm === null) return "–";
  const label = dbm >= -70 ? "good" : dbm >= -85 ? "fair" : "weak";
  return `${formatNumber(dbm, 1)} dBm (${label})`;
}

function batteryAge(installed) {
  const days = Math.floor((Date.now() - Date.parse(`${installed}T00:00:00`)) / 86400000);
  return days < 0 ? `to be installed ${installed}` : `${days} days`;
}

export function renderHealth(list, { tag, receiver, collector, server }, timeZone) {
  const summary = receiver.last_summary;
  const rows = [
    ["Tag", `${tag.tx_id ?? "any"} — ${STATUS[tag.status]}`],
    ["Last reading", tag.last_seen_ms === null ? "–" : formatTime(tag.last_seen_ms, timeZone)],
    ["Signal now", signal(tag.rssi_dbm_now)],
    ["Signal, 1 h median", signal(tag.rssi_dbm_1h_median)],
    ["Packets received (1 h)", tag.packet_success_pct_1h === null ? "–" : `${formatNumber(tag.packet_success_pct_1h, 1)}%`],
    ["Beacon every", tag.beacon_interval_s_measured === null ? "–" : `${formatNumber(tag.beacon_interval_s_measured, 2)} s`],
    ["Bad CRC / noise", `${tag.bad_crc_per_h} per h`],
    ["Drop field (1 h max)", tag.drop_max_1h ?? "–"],
    ["Other tags heard (1 h)", tag.foreign_tags_1h.join(", ") || "none"],
    ...(tag.battery_installed ? [["Battery age", batteryAge(tag.battery_installed)]] : []),
    ["Receiver", `${receiver.rx_id ?? "–"}${summary ? `, last report ${summary.packets} packets, ${formatNumber(summary.ok_pct, 1)}% OK` : ""}`],
    ["Collector", collector.source === null ? "no data yet" : `${collector.source}, last sent ${formatTime(collector.last_ingest_ms, timeZone)}`],
    ["Server", `up ${formatAge(server.uptime_s)}, database ${formatNumber(server.db_size_mb, 1)} MB, version ${server.version}`],
  ];
  list.replaceChildren(...rows.flatMap(([name, value]) => [element("dt", "", name), element("dd", "", String(value))]));
}
