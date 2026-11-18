"use strict";
// Vanilla JS chat client. Model output is inserted with textContent only (never innerHTML).

const $ = (id) => document.getElementById(id);
const EXAMPLES = [
  ["销售", "6204-2RS 的外径和宽度是多少？"],
  ["客服", "那个18的接近开关，PNP常开的，能感应多远？"],
  ["客服", "25×3 的 NBR O型圈耐温不够，换哪个型号？"],
  ["销售", "给沪东衡砺报个价：6204-2RS 200个，M8×20内六角螺钉 5箱"],
  ["客服", "6204-ZZ 多少钱一个？"],
  ["客服", "6204-2RS 的额定动载荷是多少？"],
];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

const STATUS = { answered: "已回答", refused: "拒答", denied: "无权限", quote: "报价草稿", clarify: "需澄清", error: "出错" };

function fileUrl(citation, role) {
  const base = `/files/${encodeURIComponent(citation.doc)}?role=${encodeURIComponent(role)}`;
  const page = /^p(\d+)$/.exec(citation.locator);
  return page ? `${base}#page=${page[1]}` : base;
}

async function showSource(citation, role) {
  const res = await fetch(`/api/chunk/${encodeURIComponent(citation.chunk_id)}?role=${encodeURIComponent(role)}`);
  const panel = $("source");
  panel.classList.remove("muted");
  panel.replaceChildren();
  if (!res.ok) { panel.textContent = "无法查看该片段"; return; }
  const chunk = await res.json();
  panel.append(el("strong", {}, `[${citation.n}] ${chunk.label}`), "\n", chunk.text, "\n",
    el("a", { href: fileUrl(citation, role), target: "_blank" }, "打开原文件"));
}

function answerText(text, citations, role) {
  const byN = Object.fromEntries(citations.map((c) => [c.n, c]));
  const node = el("div", { class: "text" });
  for (const part of text.split(/(\[\d+\])/)) {
    const m = /^\[(\d+)\]$/.exec(part);
    if (m && byN[m[1]]) node.append(el("sup", { class: "cite", onclick: () => showSource(byN[m[1]], role) }, part));
    else node.append(part);
  }
  return node;
}

function quoteTable(q) {
  const rows = q.lines.map((l) => el("tr", {},
    el("td", {}, l.line_no), el("td", {}, l.sku), el("td", {}, `${l.name} ${l.spec}`),
    el("td", { class: "num" }, `${l.quantity} ${l.unit}`), el("td", { class: "num" }, l.unit_price.toFixed(2)),
    el("td", {}, l.price_basis), el("td", { class: "num" }, l.amount.toFixed(2))));
  const table = el("table", {},
    el("tr", {}, ...["行", "物料编码", "品名规格", "数量", "含税单价", "依据", "金额"].map((h) => el("th", {}, h))),
    ...rows,
    el("tr", {}, el("td", { colspan: "6" }, "含税合计"), el("td", { class: "num" }, q.total.toFixed(2))));
  const box = el("div", {}, table);
  if (q.file) box.append(el("a", { href: `/quotes/${encodeURIComponent(q.file)}` }, `下载 Excel 草稿 ${q.file}`));
  return box;
}

function renderTrace(trace) {
  const panel = $("trace");
  panel.classList.remove("muted");
  panel.replaceChildren();
  const table = el("table", {}, el("tr", {}, ...["阶段", "耗时ms", "模型", "tokens", "$"].map((h) => el("th", {}, h))));
  for (const s of trace.stages) {
    table.append(el("tr", {}, el("td", {}, s.name + (s.replayed ? "（回放）" : "")), el("td", { class: "num" }, s.ms),
      el("td", {}, s.model || "本地"), el("td", { class: "num" }, s.input_tokens + s.output_tokens),
      el("td", { class: "num" }, s.cost_usd.toFixed(6))));
  }
  panel.append(table, `合计 ${(trace.total_ms / 1000).toFixed(2)}s，估算 $${trace.total_cost_usd.toFixed(6)}`);
}

function renderAnswer(a) {
  const box = el("div", { class: "msg" },
    el("div", { class: "meta" }, `角色：${a.role}`, el("span", { class: `status ${a.status}` }, STATUS[a.status] || a.status)),
    answerText(a.text, a.citations, a.role));
  if (a.quote && a.quote.lines.length) box.append(quoteTable(a.quote));
  if (a.alternatives.length) {
    box.append(el("div", { class: "alts" }, "系统匹配的替代候选：" + a.alternatives.map((x) => `${x.spec}（${x.note}）`).join("；")));
  }
  if (a.citations.length) {
    box.append(el("ol", { class: "sources" }, ...a.citations.map((c) => el("li", { value: c.n },
      el("a", { href: "#", onclick: (e) => { e.preventDefault(); showSource(c, a.role); } }, c.label)))));
  }
  for (const w of a.warnings) box.append(el("div", { class: "warning" }, "⚠ " + w));
  $("messages").append(box);
  renderTrace(a.trace);
}

async function ask(question) {
  const role = $("role").value;
  const num = (id) => ($(id).value === "" ? null : Number($(id).value));
  const body = {
    question, role,
    filters: { model: $("f-model").value, material: $("f-material").value, size: $("f-size").value,
      price_min: num("f-min"), price_max: num("f-max") },
  };
  $("messages").append(el("div", { class: "msg user" }, el("div", { class: "meta" }, `我（${role}）`), question));
  $("send").disabled = true;
  try {
    const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderAnswer(await res.json());
  } catch (err) {
    $("messages").append(el("div", { class: "msg" }, el("span", { class: "status error" }, "出错"), " ", err.message));
  } finally {
    $("send").disabled = false;
    $("messages").scrollTop = $("messages").scrollHeight;
    loadLogs();
  }
}

async function loadLogs() {
  const res = await fetch("/api/logs?limit=20");
  const logs = await res.json();
  $("logs").replaceChildren(...logs.map((r) => el("div", { class: "log" },
    el("span", { class: `status ${r.status}` }, STATUS[r.status] || r.status), ` ${r.role} · ${r.channel} · `,
    `${(r.total_ms / 1000).toFixed(1)}s · $${r.cost_usd.toFixed(5)}`, el("br"), r.question)));
  if (!logs.length) $("logs").textContent = "暂无记录";
}

async function init() {
  const info = await (await fetch("/api/info")).json();
  $("mode").textContent = info.label;
  $("examples").append(...EXAMPLES.map(([role, q]) => el("button", {
    type: "button", class: "chip", onclick: () => { $("role").value = role; ask(q); },
  }, `${role}｜${q}`)));
  $("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("question").value.trim();
    if (q) { $("question").value = ""; ask(q); }
  });
  $("question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ask").requestSubmit(); }
  });
  $("refresh").addEventListener("click", loadLogs);
  loadLogs();
}

init();
