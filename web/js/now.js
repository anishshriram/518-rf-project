// "Right now": current conditions, details and suggestions.

import { $, element, setText } from "./dom.js";
import { SKY } from "./copy.js";
import { icon } from "./icons.js";
import {
  MISSING, convertDelta, formatAge, formatClock, formatNumber, formatTemp, formatTime, isMissing, tempUnit, withUnit,
} from "./units.js";

const RECENT = 10;

/** "Cooling · down 1.2 °C over the past hour", with an icon. */
function showTrend(node, stale, change, { limit, up, down, text }) {
  let name = "steady";
  let sentence = "Steady over the past hour";
  if (stale) {
    name = null;
    sentence = "No trend while readings are not arriving";
  } else if (isMissing(change)) {
    name = null;
    sentence = "Trend appears after an hour of data";
  } else if (Math.abs(change) >= limit) {
    name = change > 0 ? "up" : "down";
    sentence = `${change > 0 ? up : down} · ${change > 0 ? "up" : "down"} ${text(Math.abs(change))} over the past hour`;
  }
  if (node.textContent === sentence) return;
  node.replaceChildren(...(name ? [icon(name)] : []), element("span", "", sentence));
}

// Today's figures refresh every few seconds; the live value may pass them.
function extreme(pick, today, reading) {
  return isMissing(today) || reading.quality !== "good" ? today : pick(today, reading.temp_c);
}

export class Now {
  constructor() {
    this.reading = null;
    this.conditions = null; // latest /api/current response
    this.recent = [];
    this.events = [];
    this.timeZone = "UTC";
    this.detailsKey = "";
    this.suggestionsKey = "";
    $("sky-icon").append(icon("sun"));
  }

  clear() {
    this.reading = null;
    this.recent = [];
  }

  show(reading) {
    this.reading = reading;
    if (reading.ts_ms !== this.recent[0]?.ts_ms) this.recent = [reading, ...this.recent].slice(0, RECENT);
  }

  /** `age` is the seconds since the last packet if it is old, else null. */
  render({ staleAge, connected }) {
    const r = this.reading;
    const c = this.conditions;
    if (!c) return;
    const stale = staleAge !== null || !connected;
    $("now").classList.toggle("stale", stale && r !== null);
    $("now").setAttribute("aria-busy", "false");
    let asOf = "";
    if (r) {
      asOf = `Measured ${formatTime(r.ts_ms, this.timeZone)}`;
      if (staleAge !== null) asOf = `Last known values, ${formatAge(staleAge)} old · measured ${formatTime(r.ts_ms, this.timeZone)}`;
      else if (!connected) asOf = `Last known values while the server is unreachable · measured ${formatTime(r.ts_ms, this.timeZone)}`;
    }
    setText($("as-of"), asOf);
    setText($("now-title"), stale && r ? "Last known conditions" : "Right now");

    setText($("temp"), formatTemp(r?.temp_c));
    for (const node of document.querySelectorAll("[data-temp-unit]")) setText(node, tempUnit());
    showTrend($("temp-trend"), stale, c.change_1h.temp_c, {
      limit: 0.3, up: "Warming", down: "Cooling", text: (v) => `${formatNumber(convertDelta(v), 1)} ${tempUnit()}`,
    });
    setText($("feels"), withUnit(r?.feels_like_c));
    setText($("high"), r ? withUnit(extreme(Math.max, c.today.temp_max_c, r)) : MISSING);
    setText($("low"), r ? withUnit(extreme(Math.min, c.today.temp_min_c, r)) : MISSING);
    const sunny = $("sun-affected");
    sunny.hidden = !c.flags.solar_heating;

    setText($("rh"), formatNumber(r?.rh_pct, 0));
    showTrend($("rh-trend"), stale, c.change_1h.rh_pct, {
      limit: 2, up: "Rising", down: "Falling", text: (v) => `${formatNumber(v, 0)} points`,
    });
    setText($("dew"), withUnit(r?.dew_point_c));

    this.#renderLight(r, c);
    this.#renderDetails(r, c);
    this.#renderSuggestions(r, c, stale);
  }

  #renderLight(r, c) {
    const [label, name] = SKY[c.sky] ?? [null, null];
    const estimated = !["night", "dawn", "dusk"].includes(c.sky);
    setText($("sky"), label ?? (r ? "Daylight" : MISSING));
    if ($("sky-icon").dataset.name !== (name ?? "sun")) {
      $("sky-icon").dataset.name = name ?? "sun";
      $("sky-icon").replaceChildren(icon(name ?? "sun"));
    }
    let note = "From the time and the station's position";
    if (c.sky === "very_bright") note = "The light sensor is at its maximum, so clouds cannot be judged";
    else if (label && estimated) note = "Sky estimated from light";
    else if (!label) note = c.flags.calibrating_light ? "No sky estimate while calibrating" : "Not enough recent light data for a sky estimate";
    setText($("sky-note"), r ? note : "");
    const calibrated = !c.flags.calibrating_light;
    setText($("light"), !r ? MISSING : calibrated
      ? `${formatNumber(isMissing(r.light_norm) ? null : r.light_norm * 100, 0)}% of full sun`
      : `${formatNumber(r.light_raw, 0)} raw`);
    setText($("sunrise"), formatClock(c.sun.sunrise_ms, this.timeZone));
    setText($("sunset"), formatClock(c.sun.sunset_ms, this.timeZone));
    $("calibrating").hidden = calibrated || !r;
  }

  #renderDetails(r, c) {
    if (!$("details").open) return;
    const spread = isMissing(r?.dew_point_c) ? null : r.temp_c - r.dew_point_c;
    const unit = (value, digits, name) => (isMissing(value) ? MISSING : `${formatNumber(value, digits)} ${name}`);
    const rows = [
      ["Measured", "Raw light", unit(r?.light_raw, 0, ""), "The sensor's own number; its units are unknown."],
      ["Measured", "Signal strength", unit(r?.rssi_dbm, 1, "dBm"), "How strongly the receiver hears the tag."],
      ["Calculated", "Absolute humidity", unit(r?.abs_humidity_gm3, 1, "g/m³"), "Water vapor in each cubic meter of air."],
      ["Calculated", "Vapor pressure deficit", unit(r?.vpd_kpa, 2, "kPa"), "How strongly the air dries things. Higher dries faster."],
      ["Calculated", "Gap to dew point", spread === null ? MISSING : `${formatNumber(convertDelta(spread), 1)} ${tempUnit()}`, "A small gap means fog, dew or frost is more likely."],
      ["Calculated", "Frost point", withUnit(r?.frost_point_c), "Shown below freezing: where frost starts to form."],
      ["Calculated", "Feels like", withUnit(r?.feels_like_c), "Adds humidity in hot weather. Wind is not measured, so there is no wind chill."],
      ["Calculated", "Sun elevation", isMissing(c.sun.elevation_deg) ? MISSING : `${formatNumber(c.sun.elevation_deg, 1)}°`, "From the date, time and station position."],
      ["Estimated", "Sky clearness", isMissing(c.clearness) ? MISSING : `${formatNumber(c.clearness * 100, 0)}%`, "Measured light compared with a clear sky. Shade, direction, dirt and window glass affect it."],
    ];
    const key = JSON.stringify(rows);
    if (key === this.detailsKey) return;
    this.detailsKey = key;
    const groups = ["Measured", "Calculated", "Estimated"].map((kind) => {
      const list = element("dl", "facts");
      for (const [, name, value, text] of rows.filter((row) => row[0] === kind)) {
        list.append(element("div", "", element("dt", "", name), element("dd", "num", value), element("dd", "help", text)));
      }
      return element("section", "", element("h3", "", kind), list);
    });
    $("details-body").replaceChildren(...groups);
  }

  #renderSuggestions(r, c, stale) {
    const items = [];
    // Advice needs current, trustworthy readings.
    if (r && !stale && r.quality === "good") {
      const rec = c.recommendations;
      const open = this.events.find((e) => e.type === "open_windows" && e.end_ms === null);
      if (rec.open_windows && open) items.push(["window", "Good time to open windows", `It is ${withUnit(r.temp_c)} outside with a dew point of ${withUnit(r.dew_point_c)}, cooler than the ${withUnit(c.home.indoor_setpoint_c)} you set for indoors.`]);
      if (!isMissing(rec.laundry_score) && c.sun.is_daylight) {
        const word = rec.laundry_score >= 70 ? "great" : rec.laundry_score >= 40 ? "fair" : "poor";
        items.push(["laundry", `Laundry drying: ${word}`, `A rough score of ${Math.round(rec.laundry_score)} out of 100 from ${withUnit(r.temp_c)}, ${formatNumber(r.rh_pct, 0)}% humidity and the light. Wind is not measured.`]);
      }
      if (["high", "medium"].includes(rec.window_condensation_risk)) {
        items.push(["drop", `Window condensation: ${rec.window_condensation_risk} risk`, `Estimate for ${withUnit(c.home.indoor_setpoint_c)} and ${formatNumber(c.home.indoor_rh_assumed, 0)}% humidity indoors, from your settings, with ${withUnit(r.temp_c)} outside.`]);
      }
    }
    const key = JSON.stringify(items);
    if (key === this.suggestionsKey) return;
    this.suggestionsKey = key;
    $("suggestions").hidden = !items.length;
    $("suggestion-list").replaceChildren(...items.map(([name, title, text]) =>
      element("li", "", icon(name), element("div", "", element("strong", "", title), element("p", "", text)))));
  }

  renderTable() {
    const cells = (tag, values) => element("tr", "", ...values.map((v) => element(tag, "", v)));
    $("recent-head").replaceChildren(cells("th", ["Time", `Temp (${tempUnit()})`, "Humidity (%)", "Light (raw)", "Signal (dBm)", "Quality"]));
    $("recent-body").replaceChildren(...this.recent.map((r) => cells("td", [
      formatTime(r.ts_ms, this.timeZone), formatTemp(r.temp_c), formatNumber(r.rh_pct, 0),
      formatNumber(r.light_raw, 0), formatNumber(r.rssi_dbm, 1), r.quality ?? "",
    ])));
  }
}
