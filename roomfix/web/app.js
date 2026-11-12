"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k in e ? (e[k] = v) : e.setAttribute(k, v);
  e.append(...kids);
  return e;
};
let S, cfg, D, version = -1, saveTimer;

async function post(path, body = {}) {
  const r = await fetch(path, { method: "POST", headers: { "X-Roomfix": "1" }, body: JSON.stringify(body) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error);
  if (data.state) update(data.state);
  return data;
}

// ---------- setup form ----------
const SIMPLE = ["output_device", "input_device", "backend", "positions", "max_level_db"];

function options(select, items, value) {
  select.replaceChildren(...items.map(([v, t]) => el("option", { value: v, textContent: t })));
  if (value && !items.some(([v]) => v === value)) select.append(el("option", { value, textContent: value + " (not connected)" }));
  select.value = value;
}

function renderSetup() {
  const outs = S.devices.outputs, ins = S.devices.inputs;
  options($("#output_device"), [["", "Select…"], ...outs.map((d) => [d.name, `${d.name} (${d.channels} ch)`])], cfg.output_device);
  options($("#input_device"), [["", "Select…"], ...ins.map((d) => [d.name, d.name])], cfg.input_device);
  options($("#capture_device"), [["in:", "Select…"], ...ins.map((d) => ["in:" + d.name, d.name]),
    ...outs.filter((d) => d.name !== cfg.output_device).map((d) => ["loop:" + d.name, d.name + " (loopback)"])],
    (cfg.capture_loopback ? "loop:" : "in:") + cfg.capture_device);
  for (const id of ["backend", "positions", "max_level_db"]) $("#" + id).value = cfg[id];
  $("#input_channel").value = cfg.input_channel + 1;
  $("#autostart").checked = cfg.autostart;
  $("#n_ways").value = cfg.ways.length;
  $("#cal-name").textContent = S.cfg.mic_cal_name || "none (uncalibrated)";
  $("#capture-wrap").hidden = cfg.backend !== "camilladsp";

  const nOut = (outs.find((d) => d.name === cfg.output_device) || { channels: 8 }).channels;
  const chSelect = (spk, wi) => {
    const s = el("select", { onchange: () => { spk.channels[wi] = +s.value; save(); } });
    for (let c = 0; c < nOut; c++) s.append(el("option", { value: c, textContent: "DAC ch " + (c + 1) }));
    s.value = spk.channels[wi];
    return s;
  };
  const rows = cfg.speakers.map((spk, si) => {
    const name = el("input", { value: spk.name, oninput: () => { spk.name = name.value; save(); } });
    const input = el("select", { onchange: () => { spk.input = +input.value; save(); } },
      el("option", { value: 0, textContent: "Left" }), el("option", { value: 1, textContent: "Right" }));
    input.value = spk.input;
    const del = el("button", { type: "button", textContent: "Remove", onclick: () => { cfg.speakers.splice(si, 1); save(true); } });
    return el("tr", {}, el("td", {}, name), el("td", {}, input),
      ...cfg.ways.map((_, wi) => el("td", {}, chSelect(spk, wi))), el("td", {}, cfg.speakers.length > 1 ? del : ""));
  });
  $("#speakers").replaceChildren(
    el("tr", {}, el("th", { textContent: "Name" }), el("th", { textContent: "Plays" }),
      ...cfg.ways.map((w) => el("th", { textContent: w.name })), el("th")), ...rows);

  const xo = $("#xo-setup");
  xo.replaceChildren();
  if (cfg.ways.length > 1) {
    const grid = el("div", { className: "grid" });
    cfg.crossover.freqs.forEach((f, i) => {
      const inp = el("input", { type: "number", min: 20, max: 20000, value: f,
        oninput: () => { cfg.crossover.freqs[i] = +inp.value; save(); } });
      grid.append(el("label", {}, `Crossover ${cfg.ways[i].name} / ${cfg.ways[i + 1].name} (Hz)`, inp));
    });
    cfg.ways.slice(1).forEach((w) => {
      const inp = el("input", { type: "number", min: 20, max: 20000, value: w.min_freq || "",
        oninput: () => { w.min_freq = +inp.value; save(); } });
      grid.append(el("label", {}, `${w.name}: lowest safe crossover (Hz)`, inp));
    });
    const slope = el("select", { onchange: () => { cfg.crossover.slope = slope.value; save(); } },
      ...[["LR2", "12 dB/oct"], ["LR4", "24 dB/oct"], ["LR8", "48 dB/oct"]].map(([v, t]) =>
        el("option", { value: v, textContent: `Linkwitz-Riley ${t}` })));
    slope.value = cfg.crossover.slope;
    grid.append(el("label", {}, "Crossover slope", slope));
    xo.append(el("h3", { textContent: "Crossover" }),
      el("p", { className: "muted", textContent: "The lowest safe crossover is a hard limit from the driver's data sheet: the driver is never tested or crossed over below it." }), grid);
  }
}

function setWays(n) {
  const defaults = { 2: [2000], 3: [400, 3000] }[n] || [];
  const mins = { 2: [0, 1500], 3: [0, 250, 2000] }[n] || [0];
  cfg.ways = S.way_names[n].map((name, i) => ({ name, min_freq: mins[i] }));
  cfg.crossover.freqs = defaults;
  if (n > 1) cfg.backend = "camilladsp";
  let next = 0;
  for (const spk of cfg.speakers) spk.channels = cfg.ways.map(() => next++);
}

function save(rerender) {
  clearTimeout(saveTimer);
  if (rerender) renderSetup();
  saveTimer = setTimeout(async () => {
    try { await post("/api/config", cfg); } catch (e) { $("#errors").replaceChildren(el("li", { textContent: e.message })); }
  }, rerender ? 0 : 350);
}

function bindSetup() {
  for (const id of SIMPLE) $("#" + id).onchange = (e) => {
    cfg[id] = e.target.type === "number" ? +e.target.value : e.target.value;
    save(id === "output_device" || id === "backend");
  };
  $("#input_channel").onchange = (e) => { cfg.input_channel = Math.max(0, e.target.value - 1); save(); };
  $("#autostart").onchange = (e) => { cfg.autostart = e.target.checked; save(); };
  $("#capture_device").onchange = (e) => {
    const [kind, ...name] = e.target.value.split(":");
    cfg.capture_loopback = kind === "loop"; cfg.capture_device = name.join(":"); save();
  };
  $("#n_ways").onchange = (e) => { setWays(+e.target.value); save(true); };
  $("#add-speaker").onclick = () => {
    const used = cfg.speakers.flatMap((s) => s.channels), next = used.length ? Math.max(...used) + 1 : 0;
    cfg.speakers.push({ name: "Speaker " + (cfg.speakers.length + 1), input: cfg.speakers.length % 2,
      channels: cfg.ways.map((_, i) => next + i) });
    save(true);
  };
  $("#cal-btn").onclick = () => $("#cal-file").click();
  $("#cal-file").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try { await post("/api/calibration", { name: file.name, text: await file.text() }); renderSetup(); }
    catch (err) { alert(err.message); }
  };
}

// ---------- target controls ----------
const SLIDERS = { tilt: " dB/oct", bass_boost: " dB", bass_freq: " Hz", max_boost: " dB", max_cut: " dB", n_filters: "" };

function renderTarget() {
  const t = cfg.target;
  for (const [id, unit] of Object.entries(SLIDERS)) { $("#" + id).value = t[id]; $(`#${id}-o`).textContent = t[id] + unit; }
  $("#f_min").value = t.f_min || ""; $("#f_max").value = t.f_max || "";
  $("#points").value = (t.points || []).map((p) => p.join(" ")).join("\n");
  const key = `${t.tilt},${t.bass_boost}`;
  $("#preset").value = [...$("#preset").options].some((o) => o.value === key) ? key : "";
}

function parsePoints(text) {
  return text.split("\n").map((l) => l.trim().split(/[\s,;]+/).map(Number))
    .filter((p) => p.length >= 2 && p[0] > 0 && isFinite(p[1])).map((p) => [p[0], p[1]]);
}

function bindTarget() {
  for (const id of Object.keys(SLIDERS)) $("#" + id).oninput = (e) => { cfg.target[id] = +e.target.value; renderTarget(); save(); };
  for (const id of ["f_min", "f_max"]) $("#" + id).onchange = (e) => { cfg.target[id] = +e.target.value || null; save(); };
  $("#preset").onchange = (e) => {
    if (!e.target.value) return;
    [cfg.target.tilt, cfg.target.bass_boost] = e.target.value.split(",").map(Number);
    renderTarget(); save();
  };
  $("#points").onchange = (e) => { cfg.target.points = parsePoints(e.target.value); save(); };
  $("#points-file-btn").onclick = () => $("#points-file").click();
  $("#points-file").onchange = async (e) => {
    if (!e.target.files[0]) return;
    cfg.target.points = parsePoints(await e.target.files[0].text()); renderTarget(); save();
  };
}

// ---------- charts ----------
const NS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const fmtHz = (f) => (f >= 1000 ? +(f / 1000).toPrecision(3) + " kHz" : Math.round(f) + " Hz");

function chart(title, freq, series, range) {
  const W = 900, H = 320, L = 38, R = 10, T = 8, B = 24;
  const vals = series.flatMap((s) => s.data.filter((v, i) => v != null && freq[i] >= 30 && freq[i] <= 16000));
  const top = Math.min(Math.ceil((Math.max(...vals) + 2) / 5) * 5, 40);
  const bottom = Math.max(Math.floor((Math.min(...vals) - 2) / 5) * 5, top - 50);
  const x = (f) => L + (Math.log(f / 20) / Math.log(1000)) * (W - L - R);
  const y = (v) => T + ((top - v) / (top - bottom)) * (H - T - B);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  if (range) for (const [a, b] of [[20, range[0]], [range[1], 20000]]) if (b > a * 1.01)
    g.append(svg("rect", { x: x(a), y: T, width: x(b) - x(a), height: H - T - B, fill: "var(--shade)" }));
  for (let v = bottom; v <= top; v += 5) {
    g.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--grid)" }));
    const t = svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = v; g.append(t);
  }
  for (const f of [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]) {
    g.append(svg("line", { x1: x(f), x2: x(f), y1: T, y2: H - B, stroke: "var(--grid)" }));
    const t = svg("text", { x: x(f), y: H - 8, "text-anchor": f === 20000 ? "end" : "middle" });
    t.textContent = f >= 1000 ? f / 1000 + "k" : f; g.append(t);
  }
  const clip = "c" + Math.random().toString(36).slice(2);
  const cp = svg("clipPath", { id: clip });
  cp.append(svg("rect", { x: L, y: T, width: W - L - R, height: H - T - B }));
  g.append(cp);
  for (const s of series) {
    let d = "", pen = false;
    s.data.forEach((v, i) => { if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + x(freq[i]).toFixed(1) + " " + y(v).toFixed(1); pen = true; });
    g.append(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 2, "stroke-linejoin": "round",
      "stroke-dasharray": s.dash ? "6 4" : "", "clip-path": `url(#${clip})` }));
  }
  const cross = svg("line", { y1: T, y2: H - B, stroke: "var(--axis)", visibility: "hidden" });
  g.append(cross);
  const tip = $("#tip");
  g.onpointermove = (e) => {
    const box = g.getBoundingClientRect(), px = ((e.clientX - box.left) / box.width) * W;
    const f = 20 * Math.pow(1000, (px - L) / (W - L - R));
    if (f < 20 || f > 20000) return (g.onpointerleave());
    let i = 0;
    while (i < freq.length - 1 && freq[i + 1] < f) i++;
    cross.setAttribute("x1", x(freq[i])); cross.setAttribute("x2", x(freq[i])); cross.setAttribute("visibility", "visible");
    tip.replaceChildren(el("b", { textContent: fmtHz(freq[i]) }), ...series.filter((s) => s.data[i] != null).map((s) =>
      el("div", {}, el("i", { style: `background:${s.color}` }), s.name, el("span", { textContent: s.data[i].toFixed(1) + " dB" }))));
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = Math.min(e.clientX + 14, innerWidth - tw - 8) + "px";
    tip.style.top = e.clientY + 14 + "px";
  };
  g.onpointerleave = () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; };
  const legend = el("div", { className: "legend" }, ...series.filter((s) => !s.hide).map((s) =>
    el("span", {}, el("i", { className: s.dash ? "dash" : "", style: `border-color:${s.color}` }), s.name)));
  return el("div", { className: "chart" }, el("h4", { textContent: title }), legend, g);
}

const WAY_COLORS = ["var(--s1)", "var(--s3)", "var(--s2)"];

function renderResult() {
  $("#result").hidden = !D;
  if (!D) return;
  const charts = $("#charts");
  charts.replaceChildren();
  for (const c of D.curves) {
    const series = [{ name: "Before", color: "var(--s2)", data: c.measured, width: 1.5 }];
    series.push(c.verified ? { name: "After (measured)", color: "var(--s1)", data: c.verified }
      : { name: "After (predicted)", color: "var(--s1)", data: c.predicted });
    series.push({ name: "Target", color: "var(--ink)", data: D.target, dash: true, width: 1.5 });
    charts.append(chart(`${c.name}: frequency response (dB)`, D.freq, series, c.range));
  }
  if (D.curves[0].drivers.length) {
    const n = D.curves[0].drivers.length;
    for (const c of D.curves) {
      const series = [];
      c.drivers.forEach((d, i) => {
        const color = WAY_COLORS[n === 2 && i === 1 ? 2 : i];
        series.push({ name: d.name + " with crossover", color, data: d.filtered });
        series.push({ name: d.name + " alone", color, data: d.raw, dash: true, width: 1.2 });
      });
      series.push({ name: "Sum", color: "var(--ink)", data: c.measured, width: 1.5 });
      charts.append(chart(`${c.name}: crossover (dB)`, D.freq, series));
    }
  }
  $("#warnings").replaceChildren(...D.warnings.map((w) => el("li", { textContent: w })));

  const ms = (n) => ((n / D.fs) * 1000).toFixed(2) + " ms";
  const head = ["Speaker", "Driver", "DAC ch", "Level", "Delay", "Polarity"];
  const rows = D.speakers.flatMap((s) => s.ways.map((w, i) => el("tr", {},
    el("td", { textContent: i ? "" : s.name }), el("td", { textContent: w.name }),
    el("td", { className: "num", textContent: w.channel + 1 }),
    el("td", { className: "num", textContent: (s.gain + w.gain).toFixed(1) + " dB" }),
    el("td", { className: "num", textContent: `${ms(w.delay)} (${((w.delay / D.fs) * 34300).toFixed(1)} cm)` }),
    el("td", { textContent: w.invert ? "inverted" : "normal" }))));
  const eq = D.speakers.map((s) => el("details", {}, el("summary", { textContent: `${s.name}: ${s.eq.length} EQ filters` }),
    el("div", { className: "scroll" }, el("table", {}, el("tr", {}, ...["Frequency", "Gain", "Q"].map((h) => el("th", { textContent: h }))),
      ...s.eq.map((f) => el("tr", {}, el("td", { className: "num", textContent: f.freq + " Hz" }),
        el("td", { className: "num", textContent: f.gain.toFixed(1) + " dB" }), el("td", { className: "num", textContent: f.q })))))));
  $("#summary").replaceChildren(el("div", { className: "scroll" },
    el("table", {}, el("tr", {}, ...head.map((h) => el("th", { textContent: h }))), ...rows)), ...eq);
}

// ---------- status ----------
function update(state) {
  S = state;
  const job = S.job, busy = job.running;
  $("#demo").hidden = !S.demo;
  $("#engine").textContent = { on: "Correction on", off: "Correction off", demo: "No audio engine in demo" }[S.engine];
  $("#engine").className = "chip" + (S.engine === "on" ? " on" : "");
  $("#errors").replaceChildren(...S.errors.map((e) => el("li", { textContent: e })));
  $("#setup-summary").textContent = S.errors.length ? "needs attention"
    : `${S.cfg.speakers.length} × ${S.cfg.ways.length}-way on ${S.cfg.output_device}`;
  for (const id of ["run", "identify", "apply", "verify"]) $("#" + id).disabled = busy || S.errors.length > 0;
  $("#cancel").hidden = !busy;
  $("#job").hidden = !busy;
  $("#bar").style.width = Math.round(job.progress * 100) + "%";
  $("#job-msg").textContent = job.message;
  $("#prompt").hidden = !job.prompt;
  if (job.prompt) {
    const question = job.prompt.startsWith("Did");
    $("#prompt-msg").textContent = job.prompt;
    $("#prompt-yes").textContent = question ? "Yes" : "Continue";
    $("#prompt-no").textContent = question ? "No" : "Cancel";
  }
  const done = !busy && (job.error || job.result);
  $("#job-result").hidden = !done;
  $("#job-result").textContent = job.error || job.result || "";
  $("#job-result").className = "result" + (job.error ? " bad" : "");
  if (S.design_version !== version) {
    version = S.design_version;
    fetch("/api/design").then((r) => r.json()).then((d) => { D = d; renderResult(); });
  }
}

async function poll() {
  try { update(await (await fetch("/api/state")).json()); } catch (e) { /* server stopped */ }
  setTimeout(poll, S && S.job.running ? 400 : 2500);
}

function act(path, body, msgEl) {
  return async () => {
    try { const r = await post(path, body && body()); if (msgEl) $(msgEl).textContent = r.message || ""; }
    catch (e) { if (msgEl) $(msgEl).textContent = e.message; else alert(e.message); }
  };
}

(async function init() {
  S = await (await fetch("/api/state")).json();
  cfg = structuredClone(S.cfg);
  bindSetup(); bindTarget(); renderSetup(); renderTarget();
  $("#setup").open = S.errors.length > 0 || !S.has_design;
  $("#run").onclick = act("/api/run", () => ({ action: "measure", auto_apply: $("#auto_apply").checked }));
  $("#identify").onclick = act("/api/run", () => ({ action: "identify" }));
  $("#verify").onclick = act("/api/run", () => ({ action: "verify" }));
  $("#cancel").onclick = act("/api/cancel");
  $("#prompt-yes").onclick = act("/api/answer", () => ({ ok: true }));
  $("#prompt-no").onclick = act("/api/answer", () => ({ ok: false }));
  $("#apply").onclick = act("/api/apply", null, "#apply-msg");
  $("#bypass").onclick = act("/api/bypass", null, "#apply-msg");
  update(S);
  poll();
})();
