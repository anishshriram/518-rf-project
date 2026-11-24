// One family of line icons, drawn on a 24 px grid.

const PATHS = {
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  cloudSun: "M12 3v1.5M4.9 5.9 6 7M3 13h1.5M19.1 5.9 18 7M8.2 12.2A4 4 0 0 1 15.6 10M10 20h7.5a3.5 3.5 0 0 0 .4-7A5 5 0 0 0 8.6 14.2 3 3 0 0 0 10 20Z",
  cloud: "M7 19h10.5a4 4 0 0 0 .5-8A6 6 0 0 0 6.5 10.5 4.3 4.3 0 0 0 7 19Z",
  moon: "M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z",
  horizon: "M3 18h18M7 14a5 5 0 0 1 10 0M12 5v2M5 9l1.4 1.4M19 9l-1.4 1.4M8 22h8",
  up: "M7 17 17 7M9 7h8v8",
  down: "M7 7l10 10M17 9v8H9",
  steady: "M4 12h16M15 7l5 5-5 5",
  warning: "M12 3.5 2.5 20h19L12 3.5ZM12 10v4.5M12 17.5v.1",
  critical: "M8 2.5h8L21.5 8v8L16 21.5H8L2.5 16V8L8 2.5ZM12 7.5v5.5M12 16.5v.1",
  watch: "M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12ZM12 9.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6Z",
  info: "M12 2.8a9.2 9.2 0 1 0 0 18.4 9.2 9.2 0 0 0 0-18.4ZM12 11v5.5M12 7.5v.1",
  check: "M4.5 12.5 10 18 19.5 6.5",
  window: "M4 3h16v18H4ZM12 3v18M4 12h16",
  laundry: "M8.5 3 3 6.5l2 4 2-1V21h10V9.5l2 1 2-4L15.5 3a3.5 3.5 0 0 1-7 0Z",
  drop: "M12 3s6.5 6.6 6.5 11.2a6.5 6.5 0 0 1-13 0C5.5 9.6 12 3 12 3Z",
  link: "M5 12.5a10 10 0 0 1 14 0M8 15.8a5.6 5.6 0 0 1 8 0M12 19.5v.1M2 9.2a14.5 14.5 0 0 1 20 0",
  unlink: "M3 3l18 18M8 15.8a5.6 5.6 0 0 1 6.5-1M5 12.5a10 10 0 0 1 4.5-2.6M14 9.7a10 10 0 0 1 5 2.8M12 19.5v.1",
};

/** A decorative icon; the text beside it carries the meaning. */
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", PATHS[name]);
  svg.append(path);
  return svg;
}
