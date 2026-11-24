// Alert banners, event timeline and browser notifications.

import { formatTime } from "./units.js";

const ICONS = { info: "ℹ", watch: "👁", warning: "⚠", critical: "✕" };
const TIMELINE_ROWS = 50;
const TEST_BANNER_MS = 10000;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const isAlert = (event) => event.severity !== "info";

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

export class Alerts {
  constructor(banners, timeline) {
    this.banners = banners;
    this.timeline = timeline;
    this.events = [];
    this.timeZone = "UTC";
    this.test = null;
    this.offlineShown = false; // the page already says that the tag is offline
  }

  setEvents(events) {
    this.events = events;
    this.render();
  }

  setOfflineShown(shown) {
    if (shown === this.offlineShown) return;
    this.offlineShown = shown;
    this.render();
  }

  render() {
    const active = this.events.filter((e) => isAlert(e) && e.banner !== false && e.end_ms === null && !e.acknowledged
      && !(e.type === "tag_offline" && this.offlineShown));
    this.banners.replaceChildren(...(this.test ? [this.test, ...active] : active).map((e) => this.#banner(e)));
    this.timeline.replaceChildren(...this.events.slice(0, TIMELINE_ROWS).map((e) => this.#entry(e)));
    if (!this.events.length) this.timeline.append(element("li", "note", "No events yet."));
  }

  #banner(event) {
    const node = element("div", `banner alert ${event.severity}`);
    const text = element("div");
    text.append(
      element("strong", "", `${ICONS[event.severity]} ${event.severity.toUpperCase()}: ${event.title}`),
      element("div", "", event.explanation),
    );
    const button = element("button", "", "Dismiss");
    button.type = "button";
    button.addEventListener("click", () => this.#dismiss(event));
    node.append(text, button);
    return node;
  }

  async #dismiss(event) {
    if (event.id === null) {
      this.test = null;
    } else {
      const response = await fetch(`api/events/${event.id}/ack`, { method: "POST" }).catch(() => null);
      if (!response?.ok) return;
      event.acknowledged = true;
    }
    this.render();
  }

  #entry(event) {
    const node = element("li", event.severity);
    const until = event.end_ms === null ? "now" : formatTime(event.end_ms, this.timeZone);
    const head = element("div");
    head.append(
      element("strong", "", `${ICONS[event.severity]} ${event.title}`),
      element("span", "note", ` ${event.severity}, ${event.confidence} confidence${event.end_ms === null ? " — active" : ""}`),
    );
    node.append(
      head,
      element("div", "", event.explanation),
      element("div", "note", `${formatTime(event.start_ms, this.timeZone)} → ${until}`),
    );
    return node;
  }

  /** A live alert from the stream; the event list is refreshed separately. */
  onAlert(event, browserNotifications, sound) {
    if (sound && (event.severity === "critical" || event.type === "test")) chime();
    if (event.type === "test") {
      this.test = event;
      this.render();
      setTimeout(() => {
        if (this.test === event) {
          this.test = null;
          this.render();
        }
      }, TEST_BANNER_MS);
    }
    if (!event.notify || !browserNotifications) return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const label = { simulator: "[SIMULATED] ", replay: "[REPLAYED] " }[event.source] ?? "";
    try {
      new Notification(`${label}${event.title}`, { body: event.explanation, tag: event.type });
    } catch (error) {
      console.warn(error); // some mobile browsers do not allow this
    }
  }
}
