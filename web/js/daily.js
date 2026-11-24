// Tables of daily summaries and the sensor health panel.

import { signalLabel } from "./copy.js";
import { element } from "./dom.js";
import {
  MISSING, formatAge, formatClock, formatDay, formatNumber, formatTemp, formatTime, isMissing, tempUnit,
} from "./units.js";

const temp = formatTemp;

/** Minutes between detected and calculated sunrise or sunset. */
function offset(detected, astronomical) {
  if (detected === null || astronomical === null) return "";
  const minutes = Math.round((detected - astronomical) / 60000);
  return ` (${minutes >= 0 ? "+" : "−"}${Math.abs(minutes)} min)`;
}

// [heading, cell text, explanation]
const COLUMNS = [
  ["Date", (d) => formatDay(d.local_date) + (d.in_progress ? " (so far)" : "")],
  [() => `High (${tempUnit()})`, (d, z) => `${temp(d.temp_max_c)} at ${formatClock(d.temp_max_time_ms, z)}`],
  [() => `Low (${tempUnit()})`, (d, z) => `${temp(d.temp_min_c)} at ${formatClock(d.temp_min_time_ms, z)}`],
  [() => `Mean (${tempUnit()})`, (d) => temp(d.temp_avg_c)],
  ["Humidity", (d) => `${formatNumber(d.rh_min, 0)}–${formatNumber(d.rh_max, 0)}%`],
  ["Sky", (d) => d.sky_summary ?? MISSING, "Estimated from daytime light"],
  ["Sun hours", (d) => formatNumber(d.bright_sun_hours, 1), "Hours with at least 70% of clear-sky light"],
  ["Daylight (h)", (d) => `${formatNumber(d.daylight_hours_measured, 1)} of ${formatNumber(d.daylight_hours_astronomical, 1)}`,
    "Measured at the sensor, of the time between sunrise and sunset"],
  ["Light from", (d, z) => formatClock(d.sunrise_detected_ms, z) + offset(d.sunrise_detected_ms, d.sunrise_astro_ms),
    "When light first passed 5% of full sun. Around 40 min after sunrise is normal in winter; much later suggests shade to the east."],
  ["Light until", (d, z) => formatClock(d.sunset_detected_ms, z) + offset(d.sunset_detected_ms, d.sunset_astro_ms),
    "When light left the sensor, compared with sunset"],
  ["HDD", (d) => formatNumber(d.hdd, 1), "Heating degree days, proportional to heating energy demand"],
  ["CDD", (d) => formatNumber(d.cdd, 1), "Cooling degree days"],
  ["GDD", (d) => formatNumber(d.gdd, 1), "Growing degree days"],
  ["Packets", (d) => `${formatNumber(d.packet_success_pct, 1)}%`, "Share of expected packets received"],
  ["Coverage", (d) => `${formatNumber(d.data_coverage_pct, 0)}%`, "Share of the day with data"],
  ["Notes", (d) => d.notes || ""],
];

export function renderDays(table, days, timeZone) {
  const head = element("tr", "", ...COLUMNS.map(([heading]) => element("th", "", typeof heading === "function" ? heading() : heading)));
  const rows = [...days].reverse().map((day) => element("tr", "",
    ...COLUMNS.map(([heading, text]) => element("td", heading === "Notes" ? "notes" : "", text(day, timeZone)))));
  if (!rows.length) {
    const cell = element("td", "", "No data in this period yet.");
    cell.colSpan = COLUMNS.length;
    rows.push(element("tr", "", cell));
  }
  table.replaceChildren(head, ...rows);
}

/** Explanations of the table's columns, as visible text. */
export function renderGlossary(list) {
  list.replaceChildren(...COLUMNS.filter((c) => c[2]).map(([heading, , text]) => element("div", "",
    element("dt", "", typeof heading === "function" ? heading() : heading), element("dd", "", text))));
}

/** Recent days as rows with a bar from the low to the high. */
export function renderWeek(list, days) {
  if (!days.length) {
    list.replaceChildren(element("li", "empty", "The first day appears once readings arrive."));
    return;
  }
  const bottom = Math.min(...days.map((d) => d.temp_min_c));
  const range = Math.max(...days.map((d) => d.temp_max_c)) - bottom || 1;
  list.replaceChildren(...[...days].reverse().map((d) => {
    const fill = element("i");
    fill.style.left = `${(100 * (d.temp_min_c - bottom)) / range}%`;
    fill.style.right = `${100 - (100 * (d.temp_max_c - bottom)) / range}%`;
    const bar = element("span", "bar", fill);
    bar.setAttribute("aria-hidden", "true");
    const facts = [];
    if (d.sky_summary) facts.push(`${d.sky_summary[0].toUpperCase()}${d.sky_summary.slice(1)} (estimated)`);
    if (!isMissing(d.bright_sun_hours)) facts.push(`${formatNumber(d.bright_sun_hours, 1)} h bright sun`);
    facts.push(`humidity ${formatNumber(d.rh_min, 0)}–${formatNumber(d.rh_max, 0)}%`);
    if (d.data_coverage_pct < 90) facts.push(`only ${formatNumber(d.data_coverage_pct, 0)}% of the day recorded`);
    const label = (word, value, name) => element("span", `num ${name}`, element("span", "visually-hidden", `${word} `), `${temp(value)}°`);
    return element("li", "", element("span", "", d.in_progress ? "Today so far" : formatDay(d.local_date)),
      label("low", d.temp_min_c, "low"), bar, label("high", d.temp_max_c, "high"), element("span", "meta", facts.join(" · ")));
  }));
}

const STATUS = {
  online: "Online", offline_warning: "Offline", offline_critical: "Offline", never_seen: "Not heard yet",
};

const signal = (dbm) => (isMissing(dbm) ? MISSING : `${formatNumber(dbm, 1)} dBm (${signalLabel(dbm)})`);
const percent = (value) => (isMissing(value) ? MISSING : `${formatNumber(value, 1)}%`);

function batteryAge(installed) {
  const days = Math.floor((Date.now() - Date.parse(`${installed}T00:00:00`)) / 86400000);
  return days < 0 ? `to be installed ${installed}` : `${days} days`;
}

export function renderHealth(list, summary, { tag, receiver, collector, server }, timeZone) {
  const report = receiver.last_summary;
  summary.textContent = [
    `Tag ${STATUS[tag.status].toLowerCase()}`,
    isMissing(tag.rssi_dbm_1h_median) ? null : `${signalLabel(tag.rssi_dbm_1h_median)} signal`,
    isMissing(tag.packet_success_pct_1h) ? null : `${percent(tag.packet_success_pct_1h)} of packets in the last hour`,
  ].filter(Boolean).join(" · ");
  const rows = [
    ["Tag", `${tag.tx_id ?? "any"}, ${STATUS[tag.status].toLowerCase()}`],
    ["Last reading", tag.last_seen_ms === null ? MISSING : formatTime(tag.last_seen_ms, timeZone)],
    ["Signal now", signal(tag.rssi_dbm_now)],
    ["Signal, median of 1 h", signal(tag.rssi_dbm_1h_median)],
    ["Packets received, 1 h", percent(tag.packet_success_pct_1h)],
    ["Time between packets", tag.beacon_interval_s_measured === null ? MISSING : `${formatNumber(tag.beacon_interval_s_measured, 2)} s`],
    ["Noise packets, 1 h", tag.bad_crc_per_h],
    ["Drop field, highest in 1 h", tag.drop_max_1h ?? MISSING],
    ["Other tags heard, 1 h", tag.foreign_tags_1h.join(", ") || "none"],
    ...(tag.battery_installed ? [["Battery age", batteryAge(tag.battery_installed)]] : []),
    ["Receiver", `${receiver.rx_id ?? MISSING}${report ? `, last report ${report.packets} packets, ${formatNumber(report.ok_pct, 1)}% OK` : ""}`],
    ["Collector", collector.source === null ? "no data yet" : `${collector.source}, last sent ${formatTime(collector.last_ingest_ms, timeZone)}`],
    ["Server", `running ${formatAge(server.uptime_s)}, database ${formatNumber(server.db_size_mb, 1)} MB, version ${server.version}`],
  ];
  list.replaceChildren(...rows.map(([name, value]) => element("div", "", element("dt", "", name), element("dd", "", String(value)))));
}
