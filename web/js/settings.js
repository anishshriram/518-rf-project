// Settings page: builds a form from /api/settings and saves what changed.

import { element } from "./daily.js";
import { modeBadge } from "./units.js";

const $ = (id) => document.getElementById(id);
const CHANNELS = ["dashboard", "email", "browser"];
const WINDOWS = [["Single glazing", 5.8], ["Double glazing", 2.8], ["Triple glazing", 1.0]];
const TITLES = {
  station: "Station", tag: "Tag", units: "Units and calibration", home: "Home", alerts: "Alerts",
  thresholds: "Thresholds", light: "Light sensor", validation: "Validation", smoothing: "Smoothing",
};
const NOTES = {
  tag: "Use the number on the tag's sticker. An empty list accepts any tag.",
  units: "These apply to new readings. Run “python -m server.rebuild” to apply them to stored ones.",
  home: "There is no indoor sensor, so these values are used for window advice.",
  alerts: "Email also needs SMTP settings in the server's environment.",
};
const ADVANCED = ["thresholds", "light", "validation", "smoothing"];
const ORDER = ["station", "tag", "units", "home", "alerts", ...ADVANCED];
const LABELS = {
  "station.elevation_m": "Elevation (m)",
  "tag.allowed_tx_ids": "Tag IDs (separated by commas)",
  "tag.expected_beacon_interval_s": "Expected beacon interval (s)",
  "tag.offline_warning_s": "Offline warning after (s)",
  "tag.offline_critical_s": "Offline critical after (s)",
  "tag.battery_installed": "Battery installed on",
  "units.display_temperature": "Default temperature unit",
  "units.temp_input_unit": "Unit the tag reports",
  "units.humidity_scale": "Humidity scale factor",
  "units.temp_offset_c": "Temperature offset (°C)",
  "units.humidity_offset_pct": "Humidity offset (points)",
  "home.indoor_setpoint_c": "Indoor temperature (°C)",
  "home.indoor_rh_assumed": "Indoor humidity (%)",
  "home.window_u_value": "Window type",
  "alerts.cooldown_minutes": "Minutes before notifying again",
  "alerts.email_enabled": "Send alert emails",
  "alerts.email_to": "Email address",
  "alerts.browser_notifications": "Browser notifications",
  "alerts.sound": "Sound on critical alerts",
};
const CHOICES = { "units.display_temperature": ["C", "F"], "units.temp_input_unit": ["C", "F"] };
const UNITS = [
  [/_c_per_s$/, " (°C per s)"], [/_per_s$/, " (points per s)"], [/_c$/, " (°C)"], [/_s$/, " (s)"], [/_days$/, " (days)"],
];

let data = null;
let inputs = new Map(); // "section.key" -> { read, write }

function label(path, key) {
  if (LABELS[path]) return LABELS[path];
  const unit = UNITS.find(([pattern]) => pattern.test(key));
  const words = (unit ? key.replace(unit[0], "") : key).replaceAll("_", " ");
  return words[0].toUpperCase() + words.slice(1) + (unit ? unit[1] : "");
}

const typeName = (type) => type[0].toUpperCase() + type.slice(1).replaceAll("_", " ");
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function show(text, kind) {
  $("message").textContent = text;
  $("message").className = `banner ${kind}`;
  $("message").hidden = false;
  $("message").scrollIntoView({ block: "nearest" });
}

/** An input for one setting, with functions to read and write its value. */
function control(path, initial) {
  if (path === "home.window_u_value") return windowControl();
  if (CHOICES[path]) {
    const node = element("select");
    for (const value of CHOICES[path]) node.append(new Option(`°${value}`, value));
    return { node, read: () => node.value, write: (v) => { node.value = v; } };
  }
  const node = element("input");
  if (typeof initial === "boolean") {
    node.type = "checkbox";
    return { node, read: () => node.checked, write: (v) => { node.checked = v; } };
  }
  if (typeof initial === "number") {
    node.type = "number";
    node.step = "any";
    return {
      node,
      read: () => {
        if (node.value === "" || !Number.isFinite(node.valueAsNumber)) throw new Error("enter a number");
        return node.valueAsNumber;
      },
      write: (v) => { node.value = v; },
    };
  }
  if (Array.isArray(initial)) {
    return {
      node,
      read: () => node.value.split(",").map((s) => s.trim()).filter(Boolean).map((s) => {
        if (!/^\d+$/.test(s)) throw new Error("enter whole numbers separated by commas");
        return Number(s);
      }),
      write: (v) => { node.value = v.join(", "); },
    };
  }
  node.type = path === "tag.battery_installed" ? "date" : path === "alerts.email_to" ? "email" : "text";
  if (path === "station.timezone" && Intl.supportedValuesOf) {
    const list = element("datalist");
    list.id = "timezones";
    list.append(...Intl.supportedValuesOf("timeZone").map((zone) => new Option(zone)));
    document.body.append(list);
    node.setAttribute("list", "timezones");
  }
  return { node, read: () => node.value.trim(), write: (v) => { node.value = v; } };
}

function windowControl() {
  const node = element("span", "inline");
  const select = element("select");
  const number = element("input");
  number.type = "number";
  number.step = "any";
  number.setAttribute("aria-label", "U value");
  for (const [name, u] of WINDOWS) select.append(new Option(`${name} (U ${u})`, u));
  select.append(new Option("Other…", "other"));
  select.addEventListener("change", () => {
    number.hidden = select.value !== "other";
    if (select.value !== "other") number.value = select.value;
  });
  node.append(select, number);
  return {
    node,
    read: () => {
      if (!Number.isFinite(number.valueAsNumber)) throw new Error("enter a number");
      return number.valueAsNumber;
    },
    write: (v) => {
      const known = WINDOWS.some(([, u]) => u === v);
      select.value = known ? String(v) : "other";
      number.value = v;
      number.hidden = known;
    },
  };
}

function alertTable() {
  const table = element("table");
  const head = element("tr");
  head.append(element("th", "", "Alert type"), ...CHANNELS.map((c) => element("th", "", typeName(c))));
  table.append(head);
  const boxes = [];
  for (const type of data.alert_types) {
    const row = element("tr");
    row.append(element("td", "", typeName(type)));
    for (const channel of CHANNELS) {
      const box = element("input");
      box.type = "checkbox";
      box.setAttribute("aria-label", `${typeName(type)}: ${channel}`);
      const cell = element("td");
      cell.append(box);
      row.append(cell);
      boxes.push({ type, channel, box });
    }
    table.append(row);
  }
  return {
    node: table,
    read: () => {
      const off = {};
      for (const { type, channel, box } of boxes) if (!box.checked) (off[type] ??= {})[channel] = false;
      return off;
    },
    write: (types) => {
      for (const { type, channel, box } of boxes) box.checked = types[type]?.[channel] !== false;
    },
  };
}

function locationButton() {
  const button = element("button", "", "Use my location");
  button.type = "button";
  button.addEventListener("click", () => {
    if (!navigator.geolocation) return show("✕ This browser cannot give a location.", "critical");
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        inputs.get("station.latitude").write(Number(coords.latitude.toFixed(4)));
        inputs.get("station.longitude").write(Number(coords.longitude.toFixed(4)));
        show("Location filled in. Save to apply it.", "info");
      },
      (error) => show(`✕ No location: ${error.message}`, "critical"),
    );
    return undefined;
  });
  return button;
}

function resetButton(section) {
  const button = element("button", "", "Reset to defaults");
  button.type = "button";
  button.addEventListener("click", async () => {
    const keys = Object.keys(data.values[section]);
    await put({ [section]: Object.fromEntries(keys.map((key) => [key, null])) }, `${TITLES[section]} reset.`);
  });
  return button;
}

function build() {
  inputs = new Map();
  $("sections").replaceChildren(...ORDER.map((section) => {
    const advanced = ADVANCED.includes(section);
    const box = element(advanced ? "details" : "section", "chart settings");
    box.append(element(advanced ? "summary" : "h2", "", TITLES[section]));
    if (NOTES[section]) box.append(element("p", "note", NOTES[section]));
    const grid = element("div", "fields");
    let table = null;
    for (const [key, value] of Object.entries(data.values[section])) {
      const path = `${section}.${key}`;
      if (path === "alerts.types") {
        table = alertTable();
        inputs.set(path, table);
      } else {
        const item = control(path, value);
        inputs.set(path, item);
        const row = element("label");
        row.append(element("span", "", label(path, key)), item.node);
        grid.append(row);
      }
      inputs.get(path).write(value);
    }
    box.append(grid);
    if (table) {
      const scroll = element("div", "scroll");
      scroll.append(table.node);
      box.append(scroll);
    }
    if (section === "station") box.append(locationButton(), " ");
    box.append(resetButton(section));
    return box;
  }));
}

/** Send changes; shows the server's explanation if it refuses them. */
async function put(changes, done) {
  try {
    const response = await fetch("api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    const reply = await response.json();
    if (!response.ok) throw new Error(typeof reply.detail === "string" ? reply.detail : "the values were not accepted");
    data = reply;
    build();
    show(`✓ ${done}`, "info");
  } catch (error) {
    show(`✕ Not saved: ${error.message}`, "critical");
  }
}

$("form").addEventListener("submit", (event) => {
  event.preventDefault();
  const changes = {};
  for (const [path, item] of inputs) {
    const [section, key] = path.split(".");
    let value;
    try {
      value = item.read();
    } catch (error) {
      show(`✕ ${TITLES[section]}, ${label(path, key)}: ${error.message}`, "critical");
      item.node.focus?.();
      return;
    }
    if (!same(value, data.values[section][key])) {
      // Back at the default means no override.
      (changes[section] ??= {})[key] = same(value, data.defaults[section][key]) ? null : value;
    }
  }
  if (Object.keys(changes).length) put(changes, "Saved.");
  else show("Nothing has changed.", "info");
});

$("test-alert").addEventListener("click", async () => {
  try {
    const reply = await (await fetch("api/alerts/test", { method: "POST" })).json();
    show(`Test alert — dashboard: ${reply.dashboard}, browser: ${reply.browser}, email: ${reply.email}`, "info");
  } catch (error) {
    show(`✕ ${error.message}`, "critical");
  }
});

try {
  const [settings, current] = await Promise.all(
    ["api/settings", "api/current"].map(async (url) => {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }),
  );
  data = settings;
  modeBadge($("mode"), current.mode);
  $("simulated").hidden = current.mode !== "simulator";
  build();
} catch (error) {
  show(`✕ Could not load the settings: ${error.message}`, "critical");
}
