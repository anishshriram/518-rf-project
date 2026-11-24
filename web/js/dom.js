// Small helpers shared by the pages.

export const $ = (id) => document.getElementById(id);

export function element(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

/** Set text only when it changes, so that nothing is announced or redrawn twice. */
export function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

export async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

const SOURCES = {
  hardware: ["Live sensor", ""],
  simulator: ["Simulated data", "Simulated data, not real measurements"],
  replay: ["Replayed data", "Replayed recording, not live measurements"],
};

/** Shows where the data comes from, in the header and as a strip above it. */
export function showSource(mode) {
  const [label, notice] = SOURCES[mode] ?? ["No data yet", ""];
  setText($("source"), label);
  $("source").dataset.source = mode ?? "none";
  $("source-strip").hidden = !notice;
  setText($("source-strip"), notice);
}

export function segmented(container, options, selected, onSelect) {
  for (const [label, value] of options) {
    const button = element("button", "", label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(value === selected));
    button.addEventListener("click", () => {
      for (const other of container.children) other.setAttribute("aria-pressed", String(other === button));
      onSelect(value);
    });
    container.append(button);
  }
}
