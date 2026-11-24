// Chart creation, history loading and live updates.

import { element, setText } from "./dom.js";
import { convertTemp, formatMoment, formatNumber, formatTick, formatTime, tempUnit, zoneOffset } from "./units.js";

/* global Chart */

export const METRICS = [
  {
    key: "temp_c", title: "Temperature", color: "--temp", digits: 1, minSpan: 2, unit: tempUnit, convert: convertTemp,
    band: true, annotate: true,
    extra: [
      { key: "dew_point_c", title: "Dew point (calculated)", color: "--humidity", dash: [5, 4], width: 1.5 },
      { key: "heat_index_c", title: "Feels like (calculated)", color: "--text-2", dash: [2, 3], width: 1.5 },
    ],
  },
  { key: "rh_pct", title: "Humidity", color: "--humidity", digits: 0, minSpan: 10, unit: () => "%", band: true },
  {
    key: "light_raw", title: "Light", color: "--light", digits: 0, minSpan: 20, zero: true,
    unit: () => "raw sensor value",
    // Used once the light sensor is calibrated.
    calibrated: { key: "light_norm", unit: () => "% of full sun, relative", convert: (v) => v * 100 },
    extra: [{ key: "clear_sky_norm", title: "Expected under a clear sky (calculated)", color: "--text-2", dash: [5, 4], width: 1.5 }],
  },
];
export const SIGNAL = { key: "rssi_dbm", title: "Signal strength", color: "--signal", digits: 1, minSpan: 10, unit: () => "dBm" };

const seriesOf = (metric) => [metric, ...(metric.extra ?? [])];
export const HISTORY_KEYS = [...[...METRICS, SIGNAL].flatMap((m) => seriesOf(m).map((s) => s.key)), "light_norm"];

const MAX_MARKERS = 6;
const AXIS_WIDTH = 48;
const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 7200, 10080].map((m) => m * 60000);
const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Shades the hours between sunset and sunrise. */
const background = (set) => ({
  id: "background",
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea, scales: { x } } = chart;
    ctx.save();
    ctx.fillStyle = set.colors.night;
    for (const [from, to] of set.nights) {
      const left = Math.max(chartArea.left, x.getPixelForValue(from));
      const right = Math.min(chartArea.right, x.getPixelForValue(to));
      if (right > left) ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
    }
    ctx.restore();
  },
});

/** Marks where the few most important events began; labels never overlap. */
const annotations = (set, metric) => ({
  id: "annotations",
  afterDatasetsDraw(chart) {
    if (!metric.annotate) return;
    const { ctx, chartArea, scales: { x } } = chart;
    ctx.save();
    ctx.font = "12px system-ui, sans-serif";
    ctx.strokeStyle = ctx.fillStyle = set.colors.muted;
    let free = chartArea.left;
    for (const event of set.markers()) {
      const at = x.getPixelForValue(event.start_ms);
      if (at < chartArea.left || at > chartArea.right) continue;
      ctx.setLineDash([2, 4]);
      ctx.beginPath();
      ctx.moveTo(at, chartArea.top);
      ctx.lineTo(at, chartArea.bottom);
      ctx.stroke();
      const width = ctx.measureText(event.title).width;
      if (at + 5 >= free && at + 5 + width <= chartArea.right) {
        ctx.fillText(event.title, at + 5, chartArea.top + 11);
        free = at + width + 16;
      }
    }
    ctx.restore();
  },
});

/** A vertical line at the same time in every chart, with dots on this one. */
const crosshair = (set) => ({
  id: "crosshair",
  afterEvent(chart, { event }) {
    if (event.type === "mousemove" || event.type === "touchstart" || event.type === "touchmove") set.keyboard = null;
    const active = event.type === "mouseout" ? [] : chart.tooltip.getActiveElements();
    const time = active.length ? chart.data.datasets[active[0].datasetIndex].data[active[0].index]?.x ?? null : null;
    set.point(time, chart);
  },
  afterDatasetsDraw(chart) {
    if (set.time === null) return;
    const { ctx, chartArea, scales: { x } } = chart;
    const at = x.getPixelForValue(set.time);
    if (at < chartArea.left || at > chartArea.right) return;
    ctx.save();
    ctx.strokeStyle = set.colors.muted;
    ctx.beginPath();
    ctx.moveTo(at, chartArea.top);
    ctx.lineTo(at, chartArea.bottom);
    ctx.stroke();
    for (const { element: dot, datasetIndex } of chart.tooltip.getActiveElements()) {
      ctx.fillStyle = chart.data.datasets[datasetIndex].borderColor;
      ctx.beginPath();
      ctx.arc(dot.x, dot.y, 4, 0, 2 * Math.PI);
      ctx.fill();
    }
    ctx.restore();
  },
});

export class ChartSet {
  /** `announce` receives the text of a point chosen with the keyboard. */
  constructor(main, secondary, { announce, onSummary }) {
    this.timeZone = "UTC";
    this.mode = null;
    this.nights = [];
    this.events = [];
    this.calibrated = false;
    this.smooth = false;
    this.alpha = 0.2;
    this.showSuspect = true;
    this.spanMs = 0;
    this.end = 0;
    this.step = 0; // bucket size, 0 for raw points
    this.gapMs = Infinity;
    this.lastTime = null;
    this.bucketCount = 0;
    this.redraw = null;
    this.time = null; // the time pointed at, in any chart
    this.keyboard = null; // {entry, time} while a chart is read with the keyboard
    this.announce = announce;
    this.onSummary = onSummary;
    this.#readColors();
    this.charts = [...METRICS.map((m) => this.#create(main, m)), this.#create(secondary, SIGNAL)];
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      this.#readColors();
      for (const entry of this.charts) {
        this.#style(entry);
        entry.chart.update("none");
      }
    });
  }

  #readColors() {
    this.colors = {
      night: token("--night"), muted: token("--text-2"), text: token("--text"),
      surface: token("--surface"), line: token("--line"),
    };
  }

  /** How a metric is charted at the moment. */
  #view(metric) {
    return this.calibrated ? { ...metric, ...metric.calibrated } : metric;
  }

  /** The most severe alerts in view, in order of time. */
  markers() {
    const start = this.end - this.spanMs;
    const rank = { critical: 3, warning: 2, watch: 1, info: 0 };
    return this.events
      .filter((e) => (e.severity !== "info" || e.type === "front_passage") && e.start_ms >= start && e.start_ms <= this.end)
      .sort((a, b) => rank[b.severity] - rank[a.severity] || b.start_ms - a.start_ms)
      .slice(0, MAX_MARKERS)
      .sort((a, b) => a.start_ms - b.start_ms);
  }

  #create(container, metric) {
    const title = element("h3", "", `${metric.title} `);
    const unit = element("span", "unit");
    const origin = element("span", "origin"); // keeps screenshots of test data honest
    title.append(unit, " ", origin);
    const legend = element("ul", "legend");
    const canvas = element("canvas");
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${metric.title} over time. Use the arrow keys to read single values.`);
    container.append(element("figure", "chart", element("figcaption", "", title, legend), element("div", "plot", canvas)));

    const series = seriesOf(metric);
    const entry = { metric, unit, origin, legend, series, points: series.map(() => []), band: [[], []] };
    const suspect = (ctx) => ctx.raw?.suspect === true;
    // An isolated reading has no line to either side, so it needs a dot.
    const isolated = (ctx) => {
      const data = ctx.dataset.data;
      const i = ctx.dataIndex;
      return ctx.raw && ctx.raw.y !== null && (data[i - 1]?.y ?? null) === null && (data[i + 1]?.y ?? null) === null;
    };
    const lines = series.map((s, i) => ({
      label: s.title,
      data: [],
      order: i,
      borderDash: s.dash ?? [],
      borderWidth: s.width ?? 2,
      pointRadius: (ctx) => (suspect(ctx) ? 4 : isolated(ctx) ? 3 : 0),
      // The crosshair draws the hover dot: built-in hover state goes stale
      // while points scroll.
      pointHoverRadius: 0,
      pointBackgroundColor: (ctx) => (suspect(ctx) ? this.colors.surface : ctx.dataset.borderColor),
    }));
    const range = [false, "-1"].map((fill) => ({
      data: [], order: 10, isBand: true, fill, borderWidth: 0, pointRadius: 0, pointHoverRadius: 0,
    }));
    entry.chart = new Chart(canvas, {
      type: "line",
      plugins: [background(this), annotations(this, metric), crosshair(this)],
      data: { datasets: [...lines, ...(metric.band ? range : [])] },
      options: {
        animation: false,
        parsing: false,
        normalized: true,
        spanGaps: false,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            filter: (item) => !item.dataset.isBand,
            displayColors: false,
            padding: 10,
            cornerRadius: 4,
            borderWidth: 1,
            caretSize: 0,
            caretPadding: 12,
            titleFont: { size: 13 },
            bodyFont: { size: 13 },
            footerFont: { size: 12, weight: "normal" },
            callbacks: {
              title: (items) => (items.length ? formatTime(items[0].raw.x, this.timeZone) : ""),
              label: (item) => this.#valueText(entry, item.datasetIndex, item.raw),
              footer: (items) => (items.length ? this.#notes(entry, items[0].raw) : []),
            },
          },
        },
        scales: {
          x: {
            type: "time",
            time: { minUnit: "minute" }, // labels show minutes; avoid repeats
            grid: { display: false },
            ticks: {
              maxRotation: 0, autoSkip: false, font: { size: 12 },
              callback: (value) => formatTick(value, this.timeZone, this.spanMs),
            },
            afterBuildTicks: (axis) => { axis.ticks = this.#roundTicks(axis); },
            // Fixed, so that a label near the edge does not move the plot.
            afterFit: (axis) => { axis.paddingLeft = 0; axis.paddingRight = 22; },
          },
          y: {
            beginAtZero: Boolean(metric.zero),
            // Keep sensor noise from filling the chart; fix the axis width so
            // that the charts line up.
            afterDataLimits: (axis) => {
              const missing = metric.minSpan - (axis.max - axis.min);
              if (missing > 0) {
                axis.max += metric.zero ? missing : missing / 2;
                axis.min -= metric.zero ? 0 : missing / 2;
              }
            },
            afterFit: (axis) => { axis.width = AXIS_WIDTH; },
            ticks: {
              maxTicksLimit: 5, font: { size: 12 },
              // No scale without data: its numbers would mean nothing.
              callback(value, index, ticks) {
                const any = this.chart.data.datasets.some((set) => set.data.some((p) => p.y !== null));
                return any ? Chart.Ticks.formatters.numeric.apply(this, [value, index, ticks]) : "";
              },
            },
            border: { display: false },
          },
        },
      },
    });
    canvas.addEventListener("keydown", (event) => this.#onKey(entry, event));
    canvas.addEventListener("blur", () => this.#stopKeyboard(entry));
    this.#style(entry);
    return entry;
  }

  /** Ticks on round local times, about one per 80 pixels. */
  #roundTicks({ min, max, width }) {
    if (!this.spanMs) return [];
    const wanted = this.spanMs / Math.max(2, width / 80);
    const step = TICK_STEPS.find((s) => s >= wanted) ?? TICK_STEPS.at(-1);
    const shift = zoneOffset(max, this.timeZone);
    const ticks = [];
    for (let t = Math.ceil((min + shift) / step) * step - shift; t <= max; t += step) ticks.push({ value: t });
    return ticks;
  }

  #valueText(entry, index, point) {
    const metric = this.#view(entry.metric);
    const unit = metric.unit().replace(", relative", "").replace("raw sensor value", "raw");
    const value = formatNumber(point.y, metric.digits);
    return `${value} ${unit}  ${entry.series[index].title}`.replace(" %", "%");
  }

  #notes(entry, point) {
    const notes = [];
    if (point.suspect) notes.push("Suspect reading");
    if (this.smooth) notes.push("Smoothed for display");
    if (this.step) notes.push(`Average of ${this.step >= 60000 ? `${this.step / 60000} min` : `${this.step / 1000} s`}`);
    for (const e of this.markers()) if (Math.abs(e.start_ms - point.x) <= this.spanMs / 100) notes.push(e.title);
    return notes;
  }

  #style(entry) {
    const { chart, series, metric } = entry;
    chart.data.datasets.forEach((set, i) => {
      if (set.isBand) set.backgroundColor = `${token(metric.color)}26`;
      else set.borderColor = set.pointBorderColor = token(series[i].color);
    });
    const tip = chart.options.plugins.tooltip;
    tip.backgroundColor = this.colors.surface;
    tip.titleColor = tip.bodyColor = this.colors.text;
    tip.footerColor = this.colors.muted;
    tip.borderColor = this.colors.line;
    for (const axis of ["x", "y"]) {
      chart.options.scales[axis].ticks.color = this.colors.muted;
      chart.options.scales[axis].grid.color = this.colors.line;
    }
  }

  // Reading with the pointer or the keyboard

  /** Show the same moment in every chart. */
  point(time, source) {
    if (time === this.time) return;
    this.time = time;
    for (const { chart } of this.charts) if (chart !== source) chart.draw();
  }

  #onKey(entry, event) {
    const data = entry.chart.data.datasets[0].data.filter((p) => p.y !== null);
    if (!data.length) return;
    const time = this.keyboard?.time ?? null;
    let index = time === null ? data.length : data.findIndex((p) => p.x >= time);
    if (index < 0) index = data.length - 1;
    const jump = event.shiftKey ? 10 : 1;
    if (event.key === "ArrowLeft") index -= jump;
    else if (event.key === "ArrowRight") index += jump;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = data.length - 1;
    else if (event.key === "Escape") {
      this.#stopKeyboard(entry);
      return;
    } else return;
    event.preventDefault();
    const point = data[Math.min(data.length - 1, Math.max(0, index))];
    this.keyboard = { entry, time: point.x };
    this.#showKeyboard();
    entry.chart.draw();
    const values = entry.series.flatMap((_, i) => {
      const at = entry.chart.data.datasets[i].data.find((p) => p.x === point.x);
      return at && at.y !== null ? [this.#valueText(entry, i, at)] : [];
    });
    this.announce([formatTime(point.x, this.timeZone), ...values, ...this.#notes(entry, point)].join(", "));
  }

  #showKeyboard() {
    const { entry, time } = this.keyboard;
    const active = [];
    entry.series.forEach((_, datasetIndex) => {
      const index = entry.chart.data.datasets[datasetIndex].data.findIndex((p) => p.x === time && p.y !== null);
      if (index >= 0) active.push({ datasetIndex, index });
    });
    entry.chart.tooltip.setActiveElements(active, { x: 0, y: 0 });
    this.time = time;
  }

  #stopKeyboard(entry) {
    if (!this.keyboard) return;
    this.keyboard = null;
    entry.chart.tooltip.setActiveElements([], { x: 0, y: 0 });
    this.time = null;
    for (const { chart } of this.charts) chart.draw();
  }

  // Data

  /** Replace everything with a /api/history response. */
  load(history, spanMs) {
    this.spanMs = spanMs;
    this.step = history.resolution_ms;
    this.gapMs = history.gap_threshold_ms;
    this.nights = history.night_intervals;
    this.calibrated = history.calibrated;
    const fill = (t, values) => {
      const points = [];
      t.forEach((x, i) => {
        if (i && x - t[i - 1] > this.gapMs) points.push({ x: t[i - 1] + 1, base: null });
        points.push({ x, base: values?.[i] ?? null, suspect: history.suspect[i] });
      });
      return points;
    };
    for (const entry of this.charts) {
      const metric = this.#view(entry.metric);
      entry.series.forEach((s, i) => {
        const { t, avg } = history.series[i ? s.key : metric.key];
        entry.points[i] = fill(t, avg);
      });
      const { t, min, max } = history.series[metric.key];
      entry.band = metric.band && min ? [fill(t, min), fill(t, max)] : [[], []];
    }
    const times = history.series[METRICS[0].key].t;
    this.lastTime = times.at(-1) ?? null;
    this.bucketCount = history.n.at(-1) ?? 0;
  }

  /** Add a live reading. Returns false if it is older than the newest point. */
  add(reading) {
    if (this.step && reading.quality !== "good") return true;
    const time = this.step ? Math.floor(reading.ts_ms / this.step) * this.step : reading.ts_ms;
    if (this.lastTime !== null && time < this.lastTime) return false;

    const sameBucket = this.step > 0 && time === this.lastTime;
    this.bucketCount = sameBucket ? this.bucketCount + 1 : 1;
    const gap = this.lastTime !== null && time - this.lastTime > this.gapMs;
    const push = (points, value, combine) => {
      if (sameBucket && points.length) {
        const old = points.at(-1).base;
        const base = old === null || value === null ? value ?? old : combine(old, value);
        points[points.length - 1] = { x: time, base, suspect: false };
      } else {
        if (gap) points.push({ x: this.lastTime + 1, base: null });
        points.push({ x: time, base: value, suspect: reading.quality === "suspect" });
      }
    };
    for (const entry of this.charts) {
      const metric = this.#view(entry.metric);
      entry.series.forEach((s, i) => {
        push(entry.points[i], reading[i ? s.key : metric.key] ?? null, (old, v) => old + (v - old) / this.bucketCount);
      });
      if (metric.band && this.step) {
        const value = reading[metric.key] ?? null;
        push(entry.band[0], value, Math.min);
        push(entry.band[1], value, Math.max);
      }
    }
    this.lastTime = time;
    return true;
  }

  /** Points as drawn: converted, and smoothed or filtered if asked for. */
  #display(points, convert, smooth) {
    const shown = [];
    let average = null;
    for (const p of points) {
      if (p.base === null) {
        average = null;
        shown.push({ x: p.x, y: null });
        continue;
      }
      if (p.suspect && !this.showSuspect) continue;
      let base = p.base;
      if (smooth && !p.suspect) base = average = average === null ? base : average + this.alpha * (base - average);
      shown.push({ x: p.x, y: convert ? convert(base) : base, suspect: p.suspect });
    }
    return shown;
  }

  /** Redraw with the window ending at `end`, at most four times a second. */
  draw(end) {
    this.end = end;
    this.redraw ??= setTimeout(() => this.drawNow(), 250);
  }

  drawNow() {
    clearTimeout(this.redraw);
    this.redraw = null;
    const start = this.end - this.spanMs;
    const trim = (points) => {
      const first = points.findIndex((p) => p.x >= start - Math.max(this.step, 1));
      if (first > 0) points.splice(0, first);
      else if (first < 0) points.length = 0;
    };
    for (const entry of this.charts) {
      const { chart, points, band, series } = entry;
      const metric = this.#view(entry.metric);
      points.forEach((p, i) => {
        trim(p);
        chart.data.datasets[i].data = this.#display(p, metric.convert, this.smooth);
      });
      if (metric.band) {
        band.forEach((p, i) => {
          trim(p);
          chart.data.datasets[series.length + i].data = this.#display(p, metric.convert, false);
        });
      }
      setText(entry.unit, metric.unit());
      setText(entry.origin, { simulator: "Simulated", replay: "Replayed" }[this.mode] ?? "");
      this.#legend(entry);
      chart.options.scales.x.min = start;
      chart.options.scales.x.max = this.end;
      if (this.keyboard?.entry === entry) this.#showKeyboard();
      chart.update("none");
    }
    this.#summarise(start);
  }

  #legend({ legend, series, chart, metric }) {
    const shown = series.map((s, i) => [s, chart.data.datasets[i].data.some((p) => p.y !== null)]);
    const band = Boolean(metric.band && this.step && shown[0][1]);
    const items = shown.filter(([, any]) => any).map(([s]) => s);
    const key = items.map((s) => s.key).join() + band;
    if (legend.dataset.key === key) return;
    legend.dataset.key = key;
    const entries = items.length > 1 || band ? items.map((s) => {
      const line = element("span", `key ${s.dash ? (s.dash[0] > 3 ? "dashed" : "dotted") : ""}`);
      line.style.setProperty("--key", `var(${s.color})`);
      return element("li", "", line, s.title);
    }) : [];
    if (band) {
      const block = element("span", "key range");
      block.style.setProperty("--key", `var(${metric.color})`);
      entries.push(element("li", "", block, "Lowest to highest"));
    }
    legend.replaceChildren(...entries);
  }

  /** One sentence about what the charts do and do not cover. */
  #summarise(start) {
    const points = this.charts[0].points[0];
    const real = points.filter((p) => p.base !== null);
    let text = "";
    if (!real.length) {
      text = "No readings in this period. Choose a longer range, or wait for the next reading.";
    } else {
      const gaps = points.filter((p) => p.base === null).length;
      const notes = [];
      if (real[0].x > start + this.spanMs * 0.1) notes.push(`Data starts ${formatMoment(real[0].x, this.timeZone)}; the earlier part is empty.`);
      if (gaps) notes.push(`${gaps === 1 ? "One gap" : `${gaps} gaps`} without data, left blank.`);
      if (this.smooth) notes.push("Lines are smoothed.");
      if (!this.showSuspect) notes.push("Suspect readings are hidden.");
      text = notes.join(" ");
    }
    this.onSummary(text);
  }
}
