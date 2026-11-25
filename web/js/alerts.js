// The most important alert, other alerts, the event timeline and notifications.

import { element, setText } from "./dom.js";
import { GUIDANCE, RANK, SEVERITY } from "./copy.js";
import { icon } from "./icons.js";
import { formatMoment } from "./units.js";

const FIRST_ROWS = 6;
const TEST_SHOWN_MS = 10000;

/** Two short tones. Browsers allow sound only after a click on the page. */
function chime() {
  try {
    const audio = new AudioContext();
    [880, 660].forEach((pitch, i) => {
      const tone = audio.createOscillator();
      const volume = audio.createGain();
      volume.gain.value = 0.15;
      tone.frequency.value = pitch;
      tone.connect(volume).connect(audio.destination);
      tone.start(audio.currentTime + i * 0.25);
      tone.stop(audio.currentTime + i * 0.25 + 0.2);
    });
    setTimeout(() => audio.close(), 1000);
  } catch (error) {
    console.warn(error);
  }
}

const isAlert = (event) => event.severity !== "info";
const mostImportantFirst = (a, b) => RANK[b.severity] - RANK[a.severity] || b.start_ms - a.start_ms;

export class Alerts {
  constructor({ main, others, quiet, timeline, more }) {
    this.main = main;
    this.others = others;
    this.quiet = quiet;
    this.timeline = timeline;
    this.more = more;
    this.events = [];
    this.offline = null; // stands in until the server's own event arrives
    this.test = null;
    this.timeZone = "UTC";
    this.ready = false;
    this.expanded = false;
    this.alertKey = this.timelineKey = "";
    more.addEventListener("click", () => {
      this.expanded = !this.expanded;
      this.render();
    });
  }

  setEvents(events) {
    this.events = events;
    this.ready = true;
    this.render();
  }

  /** Called every second; `offline` is {severity, start_ms, explanation} or null. */
  setOffline(offline) {
    this.offline = offline && { ...offline, id: null, type: "tag_offline", title: "Sensor offline", confidence: "high", end_ms: null };
    this.#renderAlerts();
  }

  render() {
    this.#renderAlerts();
    this.#renderTimeline();
  }

  #renderAlerts() {
    const active = this.events.filter((e) => isAlert(e) && e.banner !== false && e.end_ms === null);
    const serverOffline = active.find((e) => e.type === "tag_offline");
    const open = active.filter((e) => !e.acknowledged);
    if (this.offline && !serverOffline) open.push(this.offline);
    if (this.test) open.push(this.test);
    open.sort(mostImportantFirst);
    const acknowledged = active.filter((e) => e.acknowledged);

    // Rebuild only on change: this region is announced by screen readers.
    const key = JSON.stringify([open, acknowledged, this.timeZone, this.ready]);
    if (key === this.alertKey) return;
    this.alertKey = key;
    const [first, ...rest] = open;
    this.main.replaceChildren(...(first ? [this.#banner(first)] : []));
    const list = element("ul");
    list.append(...rest.map((e) => this.#row(e, false)), ...acknowledged.map((e) => this.#row(e, true)));
    this.others.replaceChildren(...(list.children.length
      ? [element("h3", "", first ? "Also active" : "Acknowledged and still ongoing"), list] : []));
    this.quiet.hidden = !this.ready || Boolean(first) || acknowledged.length > 0;
  }

  #banner(event) {
    const [word, name] = SEVERITY[event.severity];
    const [why, next] = GUIDANCE[event.type] ?? ["", ""];
    const text = element("div", "alert-text",
      element("p", "alert-kind", `${word} · since ${formatMoment(event.start_ms, this.timeZone)}${event.confidence ? ` · ${event.confidence} confidence` : ""}`),
      element("h2", "", event.title),
      element("p", "", event.explanation));
    if (why) text.append(element("p", "", why, next ? " " : null, next ? element("strong", "", next) : null));
    const box = element("div", `alert ${event.severity}`, icon(name), text);
    if (event.id !== null || event.type === "test") box.append(this.#ackButton(event));
    return box;
  }

  #row(event, acknowledged) {
    const [word, name] = SEVERITY[event.severity];
    const row = element("li", event.severity, icon(name), element("span", "", element("strong", "", event.title),
      ` · ${word.toLowerCase()} · ${acknowledged ? "acknowledged, still ongoing" : "ongoing"} since ${formatMoment(event.start_ms, this.timeZone)}`));
    if (!acknowledged && event.id !== null) row.append(this.#ackButton(event));
    return row;
  }

  #ackButton(event) {
    const button = element("button", event.type === "test" ? "quiet" : "quiet editing", event.type === "test" ? "Close" : "Acknowledge");
    button.type = "button";
    button.setAttribute("aria-label", `${button.textContent}: ${event.title}`);
    button.addEventListener("click", () => this.#acknowledge(event, button));
    return button;
  }

  async #acknowledge(event, button) {
    if (event.type === "test") {
      this.test = null;
    } else {
      button.disabled = true;
      const response = await fetch(`api/events/${event.id}/ack`, { method: "POST" }).catch(() => null);
      if (!response?.ok) {
        button.disabled = false;
        setText(button, "Try again");
        return;
      }
      event.acknowledged = true;
    }
    this.render();
  }

  #renderTimeline() {
    const shown = this.expanded ? this.events : this.events.slice(0, FIRST_ROWS);
    const key = JSON.stringify([shown, this.events.length, this.timeZone]);
    if (key === this.timelineKey) return;
    this.timelineKey = key;
    this.more.hidden = this.events.length <= FIRST_ROWS;
    setText(this.more, this.expanded ? "Show fewer" : `Show all ${this.events.length}`);
    if (!shown.length) {
      this.timeline.replaceChildren(element("li", "empty", "Nothing yet. Frost, fog, sudden darkening, sensor problems and similar events will appear here."));
      return;
    }
    this.timeline.replaceChildren(...shown.map((e) => this.#entry(e)));
  }

  #entry(event) {
    const [word, name] = SEVERITY[event.severity];
    const start = formatMoment(event.start_ms, this.timeZone);
    let state = `${start} to ${event.end_ms === null ? "" : formatMoment(event.end_ms, this.timeZone)}`;
    if (event.end_ms === null) state = `Ongoing since ${start}${event.acknowledged ? ", acknowledged" : ""}`;
    return element("li", event.severity, icon(name), element("div", "",
      element("p", "", element("strong", "", event.title)),
      element("p", "meta", `${word} · ${state} · inferred, ${event.confidence} confidence`),
      element("p", "", event.explanation)));
  }

  /** A live alert from the stream; the event list is refreshed separately. */
  onAlert(event, { notifications, sound }) {
    if (sound && (event.severity === "critical" || event.type === "test")) chime();
    if (event.type === "test") {
      this.test = { ...event, confidence: "", end_ms: null };
      const shown = this.test;
      this.render();
      setTimeout(() => {
        if (this.test === shown) {
          this.test = null;
          this.render();
        }
      }, TEST_SHOWN_MS);
    }
    if (!event.notify || !notifications) return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const label = { simulator: "[SIMULATED] ", replay: "[REPLAYED] " }[event.source] ?? "";
    try {
      new Notification(`${label}${event.title}`, { body: event.explanation, tag: event.type });
    } catch (error) {
      console.warn(error); // some mobile browsers do not allow this
    }
  }
}
