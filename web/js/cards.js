// Latest-value cards and the table of recent readings.

import { METRICS } from "./charts.js";
import { convertTemp, formatNumber, formatTime, tempUnit } from "./units.js";

const ROWS = 10;
const display = (metric, value) =>
  formatNumber(metric.convert ? metric.convert(value ?? null) : value, metric.digits);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const temp = (c) => `${formatNumber(convertTemp(c ?? null), 1)} ${tempUnit()}`;

// [metric key]: [trend name, steady below, double arrow above], per hour.
const TRENDS = {
  temp_c: ["temp_c_per_h", 0.3, 2],
  rh_pct: ["rh_per_h", 2, 10],
};

function trend(slope, steady, strong) {
  if (slope === null || slope === undefined) return ["", ""];
  if (Math.abs(slope) < steady) return ["→", "steady"];
  const word = slope > 0 ? "rising" : "falling";
  const arrow = slope > 0 ? "↑" : "↓";
  return Math.abs(slope) > strong ? [arrow + arrow, `${word} fast`] : [arrow, word];
}

// Today's figures are refreshed every few seconds; the live value may pass them.
const extreme = (pick, today, reading) =>
  today === null || today === undefined || reading.quality !== "good" ? today : pick(today, reading.temp_c);

/** Text lines under a card's main value. */
const DETAILS = {
  temp_c: (r, c) => [
    [`Feels like ${temp(r.feels_like_c)}`, "Uses humidity in hot weather. Wind is not measured, so there is no wind chill."],
    [`High ${temp(extreme(Math.max, c.today?.temp_max_c, r))} · Low ${temp(extreme(Math.min, c.today?.temp_min_c, r))}`,
      "Today, in station time"],
  ],
  rh_pct: (r, c) => [
    [`Dew point ${temp(r.dew_point_c)}`, "Temperature at which the air becomes saturated"],
    [`Absolute ${formatNumber(r.abs_humidity_gm3, 1)} g/m³ · VPD ${formatNumber(r.vpd_kpa, 2)} kPa`,
      "VPD (vapour pressure deficit): how strongly the air dries things"],
    [`Window condensation: ${c.recommendations?.window_condensation_risk ?? "–"}`, "Estimate based on your indoor settings"],
  ],
};

export class Cards {
  constructor(container, head, body) {
    this.container = container;
    this.body = body;
    this.head = head;
    this.latest = null;
    this.recent = [];
    this.timeZone = "UTC";
    this.conditions = {}; // latest /api/current response
    this.values = METRICS.map((metric) => {
      const card = element("div", "card");
      card.style.setProperty("--accent", `var(${metric.color})`);
      const value = element("span", "value", "–");
      const unit = element("span", "unit");
      const line = element("div");
      const arrow = element("span", "trend");
      const details = element("div", "details");
      line.append(value, " ", unit, " ", arrow);
      card.append(element("div", "label", metric.title), line, details);
      container.append(card);
      return { metric, value, unit, arrow, details };
    });
  }

  clear() {
    this.recent = [];
    this.latest = null;
  }

  show(reading) {
    this.latest = reading;
    if (reading && reading.ts_ms !== this.recent[0]?.ts_ms) {
      this.recent = [reading, ...this.recent].slice(0, ROWS);
    }
  }

  setStale(stale) {
    this.container.classList.toggle("stale", stale);
  }

  render() {
    for (const { metric, value, unit, arrow, details } of this.values) {
      value.textContent = display(metric, this.latest?.[metric.key]);
      unit.textContent = metric.unit();
      const [name, steady, strong] = TRENDS[metric.key] ?? [];
      const [symbol, word] = trend(this.conditions.trends?.[name], steady, strong);
      arrow.textContent = symbol;
      arrow.title = word && `${word} over the last 30 min`;
      arrow.setAttribute("aria-label", word);
      const lines = this.latest && DETAILS[metric.key]?.(this.latest, this.conditions);
      details.replaceChildren(...(lines || []).map(([text, title]) => {
        const node = element("div", "", text);
        node.title = title;
        return node;
      }));
    }
    this.head.replaceChildren(
      element("th", "", "Time"),
      ...METRICS.map((m) => element("th", "", `${m.title} (${m.unit()})`)),
      element("th", "", "Quality"),
    );
    this.body.replaceChildren(...this.recent.map((reading) => {
      const row = element("tr");
      row.append(
        element("td", "", formatTime(reading.ts_ms, this.timeZone)),
        ...METRICS.map((m) => element("td", "", display(m, reading[m.key]))),
        element("td", "", reading.quality ?? ""),
      );
      return row;
    }));
  }
}
