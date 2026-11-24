// Latest-value cards and the table of recent readings.

import { METRICS } from "./charts.js";
import { formatNumber, formatTime } from "./units.js";

const ROWS = 10;
const display = (metric, value) =>
  formatNumber(metric.convert ? metric.convert(value ?? null) : value, metric.digits);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export class Cards {
  constructor(container, head, body) {
    this.container = container;
    this.body = body;
    this.head = head;
    this.latest = null;
    this.recent = [];
    this.timeZone = "UTC";
    this.values = METRICS.map((metric) => {
      const card = element("div", "card");
      card.style.setProperty("--accent", `var(${metric.color})`);
      const value = element("span", "value", "–");
      const unit = element("span", "unit");
      const line = element("div");
      line.append(value, " ", unit);
      card.append(element("div", "label", metric.title), line);
      container.append(card);
      return { metric, value, unit };
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
    for (const { metric, value, unit } of this.values) {
      value.textContent = display(metric, this.latest?.[metric.key]);
      unit.textContent = metric.unit();
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
