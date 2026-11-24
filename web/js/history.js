// History page: daily summaries, degree-day totals and CSV export.

import { renderDays, renderGlossary } from "./daily.js";
import { $, element, getJson, segmented, setText, showSource } from "./dom.js";
import { chooseUnit, convertTemp, formatDay, formatNumber, setDefaultUnit, tempUnit } from "./units.js";

/* global Chart */

const DAY = 86400000;
const RANGES = [["7 days", 7], ["30 days", 30], ["90 days", 90], ["1 year", 365]];
let range = 30;
let state = null;
let chart = null;
let loads = 0;

const cssColor = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** The date `days` before a YYYY-MM-DD date. */
function daysBefore(date, days) {
  return new Date(Date.parse(`${date}T00:00:00Z`) - days * DAY).toISOString().slice(0, 10);
}

async function load() {
  const token = ++loads;
  try {
    const current = await getJson("api/current");
    // The station's own date, which for simulated data is far from today's.
    const today = new Intl.DateTimeFormat("en-CA", { timeZone: current.station.timezone })
      .format(current.last_reading_ms ?? current.server_ms);
    const [days, totals] = await Promise.all([
      getJson(`api/daily?from=${daysBefore(today, range - 1)}&to=${today}`),
      getJson("api/daily/totals"),
    ]);
    if (token !== loads) return;
    setDefaultUnit(current.display_temperature);
    state = { current, days, totals, today };
    $("unreachable").hidden = true;
    render();
  } catch (error) {
    console.warn(error);
    $("unreachable").hidden = false;
  }
}

function render() {
  const { current, days, totals } = state;
  setText($("station"), current.station.name);
  showSource(current.mode);
  for (const button of $("units").children) button.setAttribute("aria-pressed", String(button.textContent === tempUnit()));
  renderDays($("days"), days, current.station.timezone);
  renderGlossary($("glossary"));
  renderTotals(totals);
  renderChart(days);
  const form = $("export").elements;
  if (!form.from.value) {
    form.to.value = state.today;
    form.from.value = daysBefore(state.today, 6);
  }
}

function renderTotals({ month, season }) {
  const row = (tag, cells) => element("tr", "", ...cells.map((text) => element(tag, "", text)));
  const f = (v) => formatNumber(v, 1);
  $("totals").replaceChildren(
    row("th", ["Period", "HDD", "CDD", "GDD"]),
    row("td", [`Month, since ${month.since}`, f(month.hdd), f(month.cdd), f(month.gdd)]),
    row("td", [`Season, HDD since ${season.hdd_since}, others since ${season.cdd_gdd_since}`,
      f(season.hdd), f(season.cdd), f(season.gdd)]),
  );
}

function renderChart(days) {
  setText($("chart-unit"), tempUnit());
  const ticks = { color: cssColor("--text-2") };
  chart?.destroy();
  chart = new Chart($("range-chart"), {
    type: "bar",
    data: {
      labels: days.map((d) => formatDay(d.local_date)),
      datasets: [{
        data: days.map((d) => [convertTemp(d.temp_min_c), convertTemp(d.temp_max_c)]),
        backgroundColor: cssColor("--temp"),
        borderRadius: 3,
        borderSkipped: false,
        maxBarThickness: 24,
      }],
    },
    options: {
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {
            label: (item) => `${formatNumber(item.raw[0], 1)} to ${formatNumber(item.raw[1], 1)} ${tempUnit()}`,
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { ...ticks, maxRotation: 0, autoSkipPadding: 16 } },
        y: { beginAtZero: false, grid: { color: cssColor("--line") }, border: { display: false }, ticks },
      },
    },
  });
}

segmented($("ranges"), RANGES, range, (days) => {
  range = days;
  load();
});

segmented($("units"), [["°C", "C"], ["°F", "F"]], tempUnit().slice(1), (unit) => {
  chooseUnit(unit);
  if (state) render();
});

$("export").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target.elements;
  const from = new Date(`${form.from.value}T00:00:00`).getTime();
  const end = new Date(`${form.to.value}T00:00:00`);
  end.setDate(end.getDate() + 1); // the whole last day, whatever its length
  if (end.getTime() <= from) {
    form.to.setCustomValidity("Must not be before the first date");
    form.to.reportValidity();
    return;
  }
  location.href = `api/export.csv?from=${from}&to=${end.getTime() - 1}&resolution=${form.resolution.value}`;
});
for (const name of ["from", "to"]) {
  $("export").elements[name].addEventListener("input", () => $("export").elements.to.setCustomValidity(""));
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => state && render());
setInterval(load, 60000);
load();
