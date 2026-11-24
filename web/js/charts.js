// Chart creation, history loading and live updates.

import { convertTemp, formatNumber, formatTick, formatTime, tempUnit } from "./units.js";

/* global Chart */

export const METRICS = [
  {
    key: "temp_c", title: "Temperature", color: "--temp", digits: 1, minSpan: 2, unit: tempUnit, convert: convertTemp,
    extra: [
      { key: "dew_point_c", title: "Dew point", color: "--rh", dash: [6, 4] },
      { key: "heat_index_c", title: "Feels like", color: "--text-2", dash: [2, 3] },
    ],
  },
  { key: "rh_pct", title: "Humidity", color: "--rh", digits: 0, minSpan: 10, unit: () => "%" },
  {
    key: "light_raw", title: "Light", color: "--light", digits: 0, minSpan: 20, unit: () => "raw", zero: true,
    // Used by the chart once the light sensor is calibrated.
    calibrated: {
      key: "light_norm", unit: () => "% of full sun", convert: (v) => (v === null ? null : v * 100),
    },
    extra: [{ key: "clear_sky_norm", title: "Clear sky", color: "--text-2", dash: [6, 4] }],
  },
  { key: "rssi_dbm", title: "Signal strength", color: "--rssi", digits: 1, minSpan: 10, unit: () => "dBm" },
];

const seriesOf = (metric) => [metric, ...(metric.extra ?? [])];
export const HISTORY_KEYS = [...METRICS.flatMap((metric) => seriesOf(metric).map((s) => s.key)), "light_norm"];

const cssColor = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Shades the hours between sunset and sunrise. */
const nightShading = (set) => ({
  id: "nightShading",
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea, scales: { x } } = chart;
    ctx.save();
    ctx.fillStyle = cssColor("--night");
    for (const [from, to] of set.nights) {
      const left = Math.max(chartArea.left, x.getPixelForValue(from));
      const right = Math.min(chartArea.right, x.getPixelForValue(to));
      if (right > left) ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
    }
    ctx.restore();
  },
});

const crosshair = {
  id: "crosshair",
  afterDatasetsDraw(chart) {
    const active = chart.tooltip?.getActiveElements() ?? [];
    if (!active.length) return;
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.strokeStyle = cssColor("--text-2");
    ctx.beginPath();
    ctx.moveTo(active[0].element.x, chartArea.top);
    ctx.lineTo(active[0].element.x, chartArea.bottom);
    ctx.stroke();
    for (const { element, datasetIndex } of active) {
      ctx.fillStyle = chart.data.datasets[datasetIndex].borderColor;
      ctx.beginPath();
      ctx.arc(element.x, element.y, 5, 0, 2 * Math.PI);
      ctx.fill();
    }
    ctx.restore();
  },
};

export class ChartSet {
  constructor(container) {
    this.timeZone = "UTC";
    this.nights = [];
    this.calibrated = false;
    this.spanMs = 0;
    this.step = 0; // bucket size, 0 for raw points
    this.gapMs = Infinity;
    this.lastTime = null;
    this.bucketCount = 0;
    this.redraw = null;
    this.charts = METRICS.map((metric) => this.#create(container, metric));
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.#recolor());
  }

  /** How a metric is charted at the moment. */
  #view(metric) {
    return this.calibrated ? { ...metric, ...metric.calibrated } : metric;
  }

  #create(container, metric) {
    const section = document.createElement("section");
    section.className = "chart";
    const heading = document.createElement("h2");
    const plot = document.createElement("div");
    plot.className = "plot";
    const canvas = document.createElement("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${metric.title} over time`);
    plot.append(canvas);
    section.append(heading, plot);
    container.append(section);

    const hollow = (ctx) => ctx.raw?.suspect === true;
    // An isolated reading has no line to either side, so it needs a dot.
    const isolated = (ctx) => {
      const data = ctx.dataset.data;
      const i = ctx.dataIndex;
      return ctx.raw && ctx.raw.y !== null && (data[i - 1]?.y ?? null) === null && (data[i + 1]?.y ?? null) === null;
    };
    const chart = new Chart(canvas, {
      type: "line",
      plugins: [nightShading(this), crosshair],
      data: {
        datasets: seriesOf(metric).map((series) => ({
          label: series.title,
          borderDash: series.dash ?? [],
          data: [],
          borderWidth: 2,
          pointRadius: (ctx) => (hollow(ctx) ? 4 : isolated(ctx) ? 3 : 0),
          // The crosshair draws the hover dot: built-in hover state goes stale
          // while points scroll.
          pointHoverRadius: 0,
          pointBackgroundColor: (ctx) => cssColor(hollow(ctx) ? "--surface" : series.color),
          spanGaps: false,
        })),
      },
      options: {
        animation: false,
        parsing: false,
        normalized: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: {
          legend: {
            display: Boolean(metric.extra),
            align: "end",
            labels: {
              usePointStyle: true,
              pointStyle: "line",
              pointStyleWidth: 36,
              boxHeight: 6,
              // Feels-like exists only in hot weather.
              generateLabels: (chart) =>
                Chart.defaults.plugins.legend.labels.generateLabels(chart).map((item) => ({
                  ...item,
                  lineDash: chart.data.datasets[item.datasetIndex].borderDash,
                })),
              filter: (item, data) => data.datasets[item.datasetIndex].data.some((p) => p.y !== null),
            },
          },
          tooltip: {
            displayColors: false,
            callbacks: {
              title: (items) => (items.length ? formatTime(items[0].raw.x, this.timeZone) : ""),
              label: (item) => {
                const text = `${item.dataset.label}: ${formatNumber(item.raw.y, metric.digits)} ${this.#view(metric).unit()}`;
                return item.raw.suspect ? `${text} (suspect)` : text;
              },
            },
          },
        },
        scales: {
          x: {
            type: "time",
            time: { minUnit: "minute" }, // labels show minutes; avoid repeats
            grid: { display: false },
            ticks: {
              maxRotation: 0,
              autoSkipPadding: 24,
              callback: (value) => formatTick(value, this.timeZone, this.spanMs),
            },
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
            afterFit: (axis) => { axis.width = 52; },
            ticks: { maxTicksLimit: 6 },
            border: { display: false },
          },
        },
      },
    });
    const entry = { metric, chart, heading };
    this.#style(entry);
    this.#title(entry);
    return entry;
  }

  #title({ metric, heading }) {
    heading.textContent = `${metric.title} (${this.#view(metric).unit()})`;
  }

  #style({ metric, chart }) {
    seriesOf(metric).forEach((series, i) => {
      const set = chart.data.datasets[i];
      set.borderColor = set.pointBorderColor = cssColor(series.color);
    });
    chart.options.plugins.legend.labels.color = cssColor("--text-2");
    for (const axis of ["x", "y"]) {
      const { ticks, grid } = chart.options.scales[axis];
      ticks.color = cssColor("--text-2");
      grid.color = cssColor("--grid");
    }
  }

  #recolor() {
    for (const entry of this.charts) {
      this.#style(entry);
      entry.chart.update("none");
    }
  }

  /** Replace everything with a /api/history response. */
  load(history, spanMs) {
    this.spanMs = spanMs;
    this.step = history.resolution_ms;
    this.gapMs = history.gap_threshold_ms;
    this.nights = history.night_intervals;
    this.calibrated = history.calibrated;
    for (const entry of this.charts) {
      const { chart } = entry;
      const metric = this.#view(entry.metric);
      this.#title(entry);
      seriesOf(metric).forEach((series, index) => {
        const { t, avg } = history.series[series.key];
        const points = [];
        t.forEach((x, i) => {
          if (i && x - t[i - 1] > this.gapMs) points.push(this.#gap(t[i - 1]));
          points.push(this.#point(metric, x, avg[i], history.suspect[i]));
        });
        chart.data.datasets[index].data = points;
      });
    }
    const times = history.series[METRICS[0].key].t;
    this.lastTime = times.at(-1) ?? null;
    this.bucketCount = history.n.at(-1) ?? 0;
  }

  #point(metric, x, base, suspect) {
    const y = metric.convert ? metric.convert(base) : base;
    return { x, y, base, suspect };
  }

  #gap(x) {
    return { x: x + 1, y: null, base: null };
  }

  /** Add a live reading. Returns false if it is older than the newest point. */
  add(reading) {
    if (this.step && reading.quality !== "good") return true;
    const time = this.step ? Math.floor(reading.ts_ms / this.step) * this.step : reading.ts_ms;
    if (this.lastTime !== null && time < this.lastTime) return false;

    const sameBucket = this.step > 0 && time === this.lastTime;
    this.bucketCount = sameBucket ? this.bucketCount + 1 : 1;
    const gap = this.lastTime !== null && time - this.lastTime > this.gapMs;
    for (const { metric: base, chart } of this.charts) {
      const metric = this.#view(base);
      seriesOf(metric).forEach((series, index) => {
        const { data } = chart.data.datasets[index];
        const value = reading[series.key] ?? null;
        if (sameBucket) {
          const old = data.at(-1).base;
          const mean = old === null || value === null ? value ?? old : old + (value - old) / this.bucketCount;
          data[data.length - 1] = this.#point(metric, time, mean, false);
        } else {
          if (gap) data.push(this.#gap(this.lastTime));
          data.push(this.#point(metric, time, value, reading.quality === "suspect"));
        }
      });
    }
    this.lastTime = time;
    return true;
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
    for (const { chart } of this.charts) {
      for (const { data } of chart.data.datasets) {
        const first = data.findIndex((p) => p.x >= start - Math.max(this.step, 1));
        if (first > 0) data.splice(0, first);
        else if (first < 0) data.length = 0;
      }
      chart.options.scales.x.min = start;
      chart.options.scales.x.max = this.end;
      chart.update("none");
    }
  }

  /** Re-express existing points after a unit change. */
  changeUnit() {
    for (const entry of this.charts) {
      const { convert } = this.#view(entry.metric);
      for (const set of entry.chart.data.datasets) {
        set.data = set.data.map((p) => ({ ...p, y: convert ? convert(p.base) : p.base }));
      }
      this.#title(entry);
    }
  }
}
