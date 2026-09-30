/* 物流园甩挂交接平台 —— 前端逻辑（无框架，原生 JS） */
"use strict";

let STATE = null;
let CURRENT_USER = null;
const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "操作失败");
  return data;
}

async function postAction(payload) {
  payload.user = CURRENT_USER.username;
  const data = await api("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return data.result;
}

function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = kind;
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.style.display = "none"), 3200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtWait(seconds) {
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return `${m}分${String(s).padStart(2, "0")}秒`;
}

/* ---------------- 车辆轮廓符号（牵引车 / 挂车不同） ---------------- */
function tractorSym(color = "#f4c20d", w = 30) {
  return `<svg class="sym" style="width:${w}px" viewBox="0 0 30 18">
    <rect x="1" y="2" width="15" height="14" rx="2" fill="none" stroke="${color}" stroke-width="1.6"/>
    <rect x="16" y="5" width="12" height="11" rx="1" fill="none" stroke="${color}" stroke-width="1.6"/>
    <circle cx="6" cy="16.5" r="1.6" fill="${color}"/><circle cx="12" cy="16.5" r="1.6" fill="${color}"/>
    <circle cx="21" cy="16.5" r="1.6" fill="${color}"/><circle cx="26" cy="16.5" r="1.6" fill="${color}"/>
  </svg>`;
}
function trailerSym(color = "#9ec5ff", w = 30) {
  return `<svg class="sym" style="width:${w}px" viewBox="0 0 30 18">
    <rect x="2" y="3" width="26" height="12" rx="2" fill="none" stroke="${color}" stroke-width="1.6"/>
    <circle cx="7" cy="16" r="1.5" fill="${color}"/><circle cx="11" cy="16" r="1.5" fill="${color}"/>
    <circle cx="19" cy="16" r="1.5" fill="${color}"/><circle cx="23" cy="16" r="1.5" fill="${color}"/>
  </svg>`;
}
function barcode(token) {
  // 由牌号生成确定性的“可扫描”竖条
  let seed = 0;
  for (const ch of token) seed = (seed * 31 + ch.charCodeAt(0)) & 0xffff;
  let bars = "";
  let x = seed;
  for (let i = 0; i < 18; i++) {
    x = (x * 1103515245 + 12345) & 0x7fffffff;
    const h = 8 + (x % 9);
    const w = x % 3 === 0 ? 2 : 1;
    bars += `<i style="height:${h}px;width:${w}px"></i>`;
  }
  return `<span class="barcode">${bars}</span>`;
}

/* ---------------- 弹窗表单 ---------------- */
function openModal(html) {
  $("modal").innerHTML = html;
  $("modal-bg").classList.add("open");
}
function closeModal() { $("modal-bg").classList.remove("open"); }
$("modal-bg").addEventListener("click", (e) => { if (e.target.id === "modal-bg") closeModal(); });

async function submitAction(payload, successMsg) {
  try {
    const r = await postAction(payload);
    closeModal();
    toast(successMsg || "操作成功", "ok");
    await refresh();
    return r;
  } catch (e) {
    toast(e.message, "err");
    await refresh();
    throw e;
  }
}

/* ================= 1. 到园看板 ================= */
function renderDashboard(s) {
  const open = s.open_cycles;
  const queued = s.phase_counts.queued || 0;
  const over = s.waiting.filter((w) => w.over).length;
  const openExc = s.exceptions.filter((e) => e.status === "open").length;
  const exit = s.phase_counts.exit_pending || 0;
  $("stat-row").innerHTML = [
    stat(open.length, "在园周转工单", ""),
    stat(queued, "待卸挂排队", queued ? "alert" : ""),
    stat(over, "等待超时", over ? "alert" : "ok"),
    stat(openExc, "未关闭异常单", openExc ? "alert" : "ok"),
  ].join("");

  $("pipeline").innerHTML = s.phase_order
    .filter((p) => p !== "closed")
    .map((p) => {
      const n = s.phase_counts[p] || 0;
      return `<div class="pipe-step ${n ? "hot" : ""}">
        <div class="n">${n}</div><div class="t">${s.phase_cn[p]}</div></div>`;
    }).join("");

  $("wait-body").innerHTML = s.waiting.length
    ? s.waiting.map((w) => {
        const cy = s.cycles.find((c) => c.code === w.cycle);
        return `<tr>
          <td class="mono">${w.cycle}</td><td>${esc(cy?.trailer_plate || "")}</td>
          <td>${w.kind_cn}</td>
          <td class="timer ${w.over ? "over" : ""}">${fmtWait(w.elapsed_s)}</td>
          <td>${Math.round(w.threshold_s / 60)}分</td>
          <td>${w.over ? '<span class="badge red">超时告警</span>' : '<span class="badge ok">等待中</span>'}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="6" class="muted">暂无等待中的工单</td></tr>`;

  const openExcRows = s.exceptions.filter((e) => e.status === "open").slice(0, 8);
  $("exc-dash-body").innerHTML = openExcRows.length
    ? openExcRows.map((e) => `<tr>
        <td><span class="badge yellow">${e.kind_cn}</span></td>
        <td>${esc(e.description.slice(0, 42))}</td>
        <td>${e.status === "open" ? '<span class="badge red">待处理</span>' : esc(e.status)}</td>
        <td>${esc(e.raised_by)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">无未关闭异常</td></tr>`;

  $("event-list").innerHTML = s.events.slice(0, 30).map((e) =>
    `<div class="evt-row"><span class="ts">${e.ts.slice(11)}</span>
      <span class="k">${esc(e.kind)}</span><span>${esc(e.detail)}</span></div>`).join("");

  $("cnt-exit") && ($("cnt-exit").textContent = exit);
  $("cnt-abn") && ($("cnt-abn").textContent =
    s.exceptions.filter((e) => e.kind === "abnormal_exit" && e.status === "open").length);
  $("cnt-closed") && ($("cnt-closed").textContent = s.closed_today);
}
function stat(num, lbl, cls) {
  return `<div class="stat ${cls}"><div class="num">${num}</div><div class="lbl">${lbl}</div></div>`;
}

/* ================= 2. 甩挂队列与交接 ================= */
function renderQueue(s) {
  $("ci-plate").innerHTML = s.appointments
    .filter((a) => a.status === "booked")
    .map((a) => `<option value="${esc(a.tractor_plate)}">${esc(a.tractor_plate)} 挂 ${esc(a.trailer_plate)} · 预约 ${esc(a.planned_arrival.slice(11))}</option>`)
    .join("") || `<option value="">（暂无可签到预约）</option>`;

  $("queue-body").innerHTML = s.queue.length
    ? s.queue.map((c) => `<tr>
        <td class="mono">${c.code}</td>
        <td>${tractorSym()} ${esc(c.tractor_plate)}</td>
        <td>${trailerSym()} ${esc(c.trailer_plate)}</td>
        <td>${esc(c.driver_name || "")}</td>
        <td class="timer ${c.queue_elapsed > 5 ? "over" : ""}">${c.queue_elapsed ?? 0} 分钟</td>
        <td>
          <button class="sm" onclick='openAssign("${c.code}")'>分配车位</button>
          <button class="sm ghost" onclick='snapshotStallRace("${c.code}")' title="两名调度员同时抢一个车位">⚡模拟抢占</button>
        </td></tr>`).join("")
    : `<tr><td colspan="6" class="muted">队列已清空</td></tr>`;

  const active = s.open_cycles.filter((c) =>
    ["stall_assigned", "unhitched", "parked", "chocked", "keys_cabinet"].includes(c.phase));
  $("handoff-cards").innerHTML = active.map((c) => handoffCard(s, c)).join("")
    || `<p class="muted">无在途交接工单</p>`;
}

const STEPS = [
  ["stall_assigned", "分位"], ["unhitched", "解绑"], ["parked", "停妥查验"],
  ["chocked", "轮挡"], ["keys_cabinet", "钥匙入柜"],
];
function stepBar(phase) {
  const order = ["queued", ...STEPS.map((x) => x[0])];
  const idx = order.indexOf(phase);
  return `<div class="pipeline">` + STEPS.map(([p, label], i) => {
    const stateIdx = order.indexOf(phase);
    const done = stateIdx > i + 1 || (phase === "keys_cabinet");
    const cur = stateIdx === i + 1;
    return `<div class="pipe-step ${cur ? "hot" : ""}" style="min-width:0;padding:5px">
      <div class="n" style="font-size:14px;color:${done ? "#3fae6a" : cur ? "#f4c20d" : "#666"}">${done ? "✓" : i + 1}</div>
      <div class="t">${label}</div></div>`;
  }).join("") + `</div>`;
}

function handoffCard(s, c) {
  const btns = [];
  if (c.phase === "stall_assigned")
    btns.push(btn("解除牵引/挂接", `openUnhitch("${c.code}")`));
  if (c.phase === "unhitched")
    btns.push(btn("入位外观+铅封", `openIntake("${c.code}")`, "danger", false));
  if (c.phase === "parked")
    btns.push(btn("轮挡确认", `doChock("${c.code}")`));
  if (c.phase === "chocked")
    btns.push(btn("钥匙扫码入柜", `openStoreKeys("${c.code}")`));
  if (c.phase === "keys_cabinet")
    btns.push(`<span class="badge ok">甩挂交接完成 · 钥匙 ${esc(c.key_token)} @ ${esc(c.key_cell)}</span>`);

  const damages = s.exceptions.filter((e) => e.cycle_code === c.code && e.kind === "trailer_damage");
  return `<div class="card">
    <h3>${trailerSym()} ${esc(c.trailer_plate)} <span class="muted mono" style="font-size:11px">${c.code}</span></h3>
    <div class="muted" style="margin-bottom:6px">车位 <b class="mono">${esc(c.stall_code || "-")}</b> · 牵引车 ${esc(c.tractor_plate)} · 司机 ${esc(c.driver_name || "")}
      · 当前 <span class="badge yellow">${c.phase_cn}</span></div>
    ${stepBar(c.phase)}
    <div class="pill-actions">${btns.join(" ")}</div>
    ${damages.length ? `<div class="badge red" style="margin-top:6px">⚠ 损伤已记录：${esc(damages[0].description.slice(0, 30))}</div>` : ""}
  </div>`;
}
function btn(label, onclick, cls = "", disabled = false) {
  return `<button class="sm ${cls}" ${disabled ? "disabled" : ""} onclick='${onclick}'>${label}</button>`;
}

window.openAssign = function (cycle) {
  const free = Object.values(STATE.stall_zones).flat().filter((x) => x.status === "free");
  const opts = free.map((x) => `<option value="${x.code}">${x.code}</option>`).join("");
  openModal(`<h3>分配车位 — ${cycle}</h3>
    <div class="form-row"><label>空闲车位</label><select id="m-stall">${opts}</select></div>
    <div class="form-inline"><button onclick='submitAssign("${cycle}")'>确认分位（调度员）</button>
    <button class="ghost" onclick="closeModal()">取消</button></div>`);
};
window.submitAssign = async function (cycle) {
  const stall = $("m-stall").value;
  await submitAction({ action: "assign_stall", cycle, stall }, `已分配 ${stall}`);
};
window.snapshotStallRace = async function (cycle) {
  const free = Object.values(STATE.stall_zones).flat().find((x) => x.status === "free");
  if (!free) return toast("没有空闲车位", "err");
  // 两个并发请求抢同一车位，只有首个成功（服务端串行 + 条件 UPDATE）
  const payload = (u) => JSON.stringify({ action: "assign_stall", cycle, stall: free.code, user: u });
  const [r1, r2] = await Promise.allSettled([
    fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: payload("li") }).then((r) => r.json()),
    fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: payload("wang") }).then((r) => r.json()),
  ]);
  const win = r1.value.ok ? "李调度" : r2.value.ok ? "王调度" : "无人";
  const loseMsg = (!r1.value.ok ? r1.value.error : !r2.value.ok ? r2.value.error : "");
  toast(`抢占 ${free.code}：${win}成功，另一方失败（${loseMsg}）`, r1.value.ok || r2.value.ok ? "ok" : "err");
  refresh();
};
window.openUnhitch = function (cycle) {
  openModal(`<h3>解除挂接 — ${cycle}</h3><p>将解除牵引车与挂车的有效挂接关系，解绑后挂车等待查验停妥。</p>
    <button onclick='submitAction({action:"unhitch",cycle:"${cycle}"},"已解绑")'>确认解绑（调度员）</button>
    <button class="ghost" onclick="closeModal()">取消</button>`);
};
window.openIntake = function (cycle) {
  const sides = [["front", "前部"], ["rear", "后部"], ["left", "左侧"], ["right", "右侧"], ["roof", "顶部"]];
  openModal(`<h3>入位外观检查 + 铅封登记 — ${cycle}</h3>
    <div class="form-row"><label>铅封号</label><input id="m-seal" value="SEAL-${Math.floor(1000 + Math.random() * 9000)}"></div>
    <div class="form-row"><label>发现损伤的方位（勾选后填描述）</label>
      <div class="checkgrid">${sides.map(([k, v]) =>
        `<label><input type="checkbox" class="dmg-side" value="${k}">${v}</label>`).join("")}</div>
    </div>
    <div class="form-row"><label>损伤描述</label><input id="m-dmg-note" placeholder="如：厢板凹陷约 8cm"></div>
    <div class="form-inline"><button onclick='submitIntake("${cycle}")'>提交五方位照片并停妥（查验员）</button>
    <button class="ghost" onclick="closeModal()">取消</button></div>`);
};
window.submitIntake = async function (cycle) {
  const damages = {};
  document.querySelectorAll(".dmg-side:checked").forEach((cb) => {
    damages[cb.value] = $("m-dmg-note").value || "现场发现损伤";
  });
  await submitAction({ action: "intake_check", cycle, seal: $("m-seal").value, damages },
    "外观照片已存档，挂车停妥");
};
window.doChock = async function (cycle) {
  await submitAction({ action: "chock", cycle }, "轮挡已确认");
};
window.openStoreKeys = function (cycle) {
  const c = STATE.cycles.find((x) => x.code === cycle);
  const token = "KB-" + String((c.id % 24) + 1).padStart(2, "0");
  const cells = [];
  for (let r = 1; r <= 3; r++) for (let col = 1; col <= 8; col++)
    cells.push(`R${r}-${String(col).padStart(2, "0")}`);
  openModal(`<h3>钥匙扫码入柜 — ${cycle}</h3>
    <p>钥匙牌号 <b class="mono">${token}</b> ${barcode(token)}</p>
    <div class="form-row"><label>扫描/选择柜格（故意选错可触发“钥匙错格”异常）</label>
      <select id="m-cell">${cells.map((x, i) => `<option ${i === (c.id % 24) ? "selected" : ""}>${x}</option>`).join("")}</select></div>
    <button onclick='submitStore("${cycle}","${token}")'>确认入柜（钥匙员）</button>
    <button class="ghost" onclick="closeModal()">取消</button>`);
};
window.submitStore = async function (cycle, token) {
  await submitAction({ action: "store_keys", cycle, token, cell: $("m-cell").value },
    `钥匙 ${token} 已入柜，甩挂交接完成`);
};

/* ================= 3. 车位图 ================= */
function renderStalls(s) {
  $("zones").innerHTML = Object.entries(s.stall_zones).map(([zone, stalls]) => `
    <div class="zone-map">
      <div class="zone-title">${zone} 区（俯视平面，车道在下方）</div>
      <div class="stall-grid">${stalls.map(stallCell).join("")}</div>
    </div>`).join("");
}
function stallCell(x) {
  if (x.status === "free")
    return `<div class="stall free"><span class="code">${x.code}</span><span class="plate">空闲</span></div>`;
  return `<div class="stall occupied" onclick='jumpAppearance("${x.cycle_code}")' title="点击查看外观">
    <span class="code">${x.code}</span>
    ${trailerSym("#f4c20d", 26)}
    <span class="plate">${esc(x.trailer_plate)}</span>
    <span class="code">${esc(x.cycle_code || "")}</span>
  </div>`;
}
window.jumpAppearance = function (cycle) {
  if (!cycle) return;
  switchView("appearance");
  $("appearance-cycle").value = cycle;
  renderAppearance(STATE);
};

/* ================= 4. 挂接操作 ================= */
function renderCouple(s) {
  $("coupling-body").innerHTML = s.couplings.length
    ? s.couplings.map((k) => `<tr>
        <td>${tractorSym()} ${esc(k.tractor_plate)}</td>
        <td>⇄</td><td>${trailerSym()} ${esc(k.trailer_plate)}</td>
        <td class="mono">${esc(k.coupled_at)}</td><td>${esc(k.by_user)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">当前无有效挂接</td></tr>`;

  const pickups = s.open_cycles.filter((c) =>
    ["keys_cabinet", "pickup_pending", "pickup_verified"].includes(c.phase));
  $("pickup-cards").innerHTML = pickups.map((c) => {
    const b = [];
    if (c.phase === "keys_cabinet")
      b.push(btn("安排提挂车/司机", `openStartPickup("${c.code}")`));
    if (c.phase === "pickup_pending") {
      b.push(btn("提挂核对", `openVerify("${c.code}")`));
      b.push(btn("无法提挂", `openCannot("${c.code}")`, "danger"));
    }
    if (c.phase === "pickup_verified") {
      b.push(btn("取钥匙", `openRetrieve("${c.code}")`));
      b.push(btn("挂接并释放车位", `doHitch("${c.code}")`));
    }
    const drv = c.pickup_driver_name ? `${esc(c.pickup_driver_name)}（提挂）` : `${esc(c.driver_name || "")}（待安排）`;
    return `<div class="card">
      <h3>${trailerSym()} ${esc(c.trailer_plate)} <span class="muted mono" style="font-size:11px">${c.code}</span></h3>
      <div class="muted" style="margin:4px 0">车位 ${esc(c.stall_code)} · 铅封 <b class="mono">${esc(c.seal_code || "-")}</b> · 司机 ${drv}</div>
      <div style="margin:4px 0"><span class="badge yellow">${c.phase_cn}</span>
        ${c.pickup_elapsed != null ? ` <span class="timer ${c.pickup_elapsed > 7 ? "over" : ""}">已等待 ${c.pickup_elapsed} 分</span>` : ""}</div>
      <div class="pill-actions">${b.join(" ")}</div></div>`;
  }).join("") || `<p class="muted">暂无可提挂工单</p>`;
}
window.openStartPickup = function (cycle) {
  const tractors = STATE.tractors.map((p) => `<option>${esc(p)}</option>`).join("");
  const drivers = STATE.drivers.map((d) =>
    `<option value="${d.license_no}">${esc(d.name)} (${d.license_no})</option>`).join("");
  openModal(`<h3>安排提挂 — ${cycle}</h3>
    <div class="form-row"><label>提挂牵引车</label><select id="m-tractor">${tractors}</select></div>
    <div class="form-row"><label>提挂司机（与预约不同将记录“司机替换”异常）</label><select id="m-driver">${drivers}</select></div>
    <button onclick='submitStartPickup("${cycle}")'>确认安排（调度员）</button>
    <button class="ghost" onclick="closeModal()">取消</button>`);
};
window.submitStartPickup = async function (cycle) {
  await submitAction({ action: "start_pickup", cycle,
    tractor: $("m-tractor").value, driver: $("m-driver").value }, "已进入提挂核对");
};
window.openVerify = function (cycle) {
  const c = STATE.cycles.find((x) => x.code === cycle);
  const drivers = STATE.drivers.map((d) =>
    `<option value="${d.license_no}" ${d.license_no === c.pickup_driver_license ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  openModal(`<h3>提挂三项核对 — ${cycle}</h3>
    <div class="form-row"><label>到场司机</label><select id="m-driver">${drivers}</select></div>
    <div class="form-row"><label>现场铅封号（登记值 <b class="mono">${esc(c.seal_code)}</b>，改错可触发不符）</label>
      <input id="m-seal" value="${esc(c.seal_code)}"></div>
    <div class="form-row"><label>外观新增损伤方位</label>
      <div class="checkgrid">${["front", "rear", "left", "right", "roof"].map((k) =>
        `<label><input type="checkbox" class="pdmg" value="${k}">${({ front: "前", rear: "后", left: "左", right: "右", roof: "顶" })[k]}</label>`).join("")}</div></div>
    <button onclick='submitVerify("${cycle}")'>核对通过（查验员）</button>
    <button class="ghost" onclick="closeModal()">取消</button>`);
};
window.submitVerify = async function (cycle) {
  const damages = {};
  document.querySelectorAll(".pdmg:checked").forEach((cb) => (damages[cb.value] = "提挂复核发现新伤"));
  await submitAction({ action: "verify_pickup", cycle,
    driver: $("m-driver").value, seal: $("m-seal").value, damages }, "三项核对通过");
};
window.openCannot = function (cycle) {
  openModal(`<h3>无法提挂登记 — ${cycle}</h3>
    <div class="form-row"><label>原因</label>
      <textarea id="m-reason" rows="3" placeholder="如：牵引鞍座故障，无法挂接"></textarea></div>
    <button class="danger" onclick='submitAction({action:"cannot_pick",cycle:"${cycle}",reason:document.getElementById("m-reason").value||"现场无法提挂"},"已登记无法提挂")'>登记（调度员）</button>
    <button class="ghost" onclick="closeModal()">取消</button>`);
};
window.openRetrieve = function (cycle) {
  const c = STATE.cycles.find((x) => x.code === cycle);
  const token = c.key_token;
  const cells = [];
  for (let r = 1; r <= 3; r++) for (let col = 1; col <= 8; col++)
    cells.push(`R${r}-${String(col).padStart(2, "0")}`);
  openModal(`<h3>取走钥匙 — ${cycle}</h3>
    <p>扫描牌号 <b class="mono">${esc(token)}</b> ${barcode(token)}，柜格应为 <b class="mono">${esc(c.key_cell)}</b></p>
    <div class="form-row"><label>钥匙柜扫描格口</label><select id="m-cell">
      ${cells.map((x) => `<option ${x === c.key_cell ? "selected" : ""}>${x}</option>`).join("")}</select></div>
    <button onclick='submitAction({action:"retrieve_keys",cycle:"${cycle}",token:"${esc(token)}",cell:document.getElementById("m-cell").value},"钥匙已取走")'>确认取钥匙（钥匙员）</button>
    <button class="ghost" onclick="closeModal()">取消</button>`);
};
window.doHitch = async function (cycle) {
  await submitAction({ action: "hitch", cycle }, "挂接成功，车位释放，前往出园核验");
};

/* ================= 5. 钥匙柜台账 ================= */
function renderKeys(s) {
  // 24 格柜
  const byCell = {};
  Object.values(s.cabinet_now).forEach((k) => (byCell[k.cell] = k));
  let html = "";
  for (let r = 1; r <= 3; r++) {
    for (let col = 1; col <= 8; col++) {
      const code = `R${r}-${String(col).padStart(2, "0")}`;
      const k = byCell[code];
      const cls = k ? (k.status === "misplaced" ? "misplaced" : "in") : "empty";
      html += `<div class="cell ${cls}" ${k ? `onclick='toast("牌号 ${esc(k.token)} / 挂车 ${esc(k.trailer_plate)}")'` : ""}>
        <div class="cellcode">${code}</div>
        ${k ? `<div class="token">${esc(k.token)}</div>${barcode(k.token)}` : `<div class="cellcode">·</div>`}
      </div>`;
    }
  }
  $("cabinet-grid").innerHTML = html;

  const statusCn = { in_cabinet: ["在柜", "ok"], lent_out: ["借出", "yellow"],
    released: ["已取走", "gray"], misplaced: ["错格", "red"] };
  $("key-ledger-body").innerHTML = s.keys.map((k) => {
    const [t, cls] = statusCn[k.status] || [k.status, "gray"];
    return `<tr>
      <td>${barcode(k.token)}</td>
      <td class="mono">${esc(k.token)}</td><td class="mono">${esc(k.cell)}</td>
      <td>${esc(k.trailer_plate || "")}</td><td>${esc(k.tractor_plate || "")}</td>
      <td><span class="badge ${cls}">${t}</span></td>
      <td class="mono">${esc(k.logged_at.slice(5))}</td><td>${esc(k.by_user)}</td>
      <td class="muted">${esc(k.note || "")}</td></tr>`;
  }).join("");
}

/* ================= 6. 外观对照 ================= */
const SIDE_CN = { front: "前部", rear: "后部", left: "左侧", right: "右侧", roof: "顶部" };
function renderAppearance(s) {
  const sel = $("appearance-cycle");
  const prev = sel.value;
  sel.innerHTML = s.cycles
    .filter((c) => s.appearance[c.id] && s.appearance[c.id].some((a) => a.stage === "intake"))
    .map((c) => `<option value="${c.code}">${c.code} · ${c.trailer_plate}（${c.phase_cn}）</option>`).join("");
  if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
  const code = sel.value || sel.options[0]?.value;
  if (!code) { $("appearance-board").innerHTML = `<p class="muted">暂无已查验工单</p>`; $("seal-body").innerHTML = ""; return; }

  const c = s.cycles.find((x) => x.code === code);
  const shots = s.appearance[c.id] || [];
  const intake = {}, pickup = {};
  shots.forEach((a) => (a.stage === "intake" ? intake : pickup)[a.side] = a);
  $("appearance-title").textContent = `${c.trailer_plate}（${c.code}）外观：入位 vs 提挂，照片在车辆轮廓对应方位旁展开`;

  const photoCard = (a, stageCn) => a ? `
    <div class="photo-card ${a.damage ? "damage" : ""}">
      <h4><span>${stageCn}·${SIDE_CN[a.side]}</span>${a.damage ? '<span class="badge red">损伤</span>' : '<span class="badge ok">正常</span>'}</h4>
      <img src="${a.url}" onclick="openLightbox('${a.url}')" alt="${stageCn}${a.side}">
      ${a.note ? `<div class="muted" style="font-size:11px;margin-top:3px">${esc(a.note)}</div>` : ""}
    </div>` : `<div class="photo-card"><h4><span>${stageCn}·${SIDE_CN["front"] && ""}无照片</span></h4>
      <div style="height:90px;display:flex;align-items:center;justify-content:center" class="muted">未拍摄</div></div>`;

  const pair = (side) => `<div style="display:flex;flex-direction:column;gap:8px">
      ${photoCard(intake[side], "入位")}${photoCard(pickup[side], "提挂")}</div>`;

  $("appearance-board").innerHTML = `
    <div class="vehicle-board">
      <div class="side-rail">${pair("left")}</div>
      <div class="vehicle-center">
        <div style="color:var(--signal);font-weight:700">车辆轮廓方位图</div>
        ${vehicleOutline()}
        <div class="slot ${intake.front ? "has" : ""}">前 部 FRONT</div>
        <div style="display:flex;gap:8px;width:100%">
          <div class="slot ${intake.left ? "has" : ""}" style="flex:1">← 左侧 LEFT</div>
          <div class="slot ${intake.roof ? "has" : ""}" style="flex:1">顶 部 ROOF</div>
          <div class="slot ${intake.right ? "has" : ""}" style="flex:1">右侧 RIGHT →</div>
        </div>
        <div class="slot ${intake.rear ? "has" : ""}" style="width:100%">后 部 REAR</div>
        <div class="muted" style="font-size:11px">${trailerSym("#9ec5ff", 60)} 挂车 ${esc(c.trailer_plate)} · 车位 ${esc(c.stall_code || "-")}</div>
      </div>
      <div class="side-rail">${pair("right")}</div>
    </div>
    <div class="grid cols-3" style="margin-top:10px">
      ${pair("front")}${pair("rear")}${pair("roof")}
    </div>`;

  const seals = s.seals.filter((x) => x.cycle_id === c.id);
  $("seal-body").innerHTML = seals.map((x) => `<tr>
    <td>${x.stage === "intake" ? "入位登记" : "提挂核对"}</td>
    <td class="mono">${esc(x.seal_code)}</td>
    <td>${x.matched == null ? "—" : x.matched ? '<span class="badge ok">相符</span>' : '<span class="badge red">不符</span>'}</td>
    <td>${esc(x.checked_by)}</td><td class="mono">${esc(x.checked_at.slice(5))}</td></tr>`).join("");
}
function vehicleOutline() {
  // 俯视挂车轮廓，标注四个方位
  return `<svg width="180" height="230" viewBox="0 0 180 230">
    <rect x="30" y="20" width="120" height="190" rx="10" fill="none" stroke="#f4c20d" stroke-width="2.5" stroke-dasharray="6 4"/>
    <rect x="70" y="2" width="40" height="20" rx="4" fill="none" stroke="#9ec5ff" stroke-width="2"/>
    <text x="90" y="15" text-anchor="middle" fill="#9ec5ff" font-size="10">前</text>
    <circle cx="45" cy="60" r="10" fill="none" stroke="#9ec5ff" stroke-width="1.5"/>
    <circle cx="45" cy="170" r="10" fill="none" stroke="#9ec5ff" stroke-width="1.5"/>
    <circle cx="135" cy="60" r="10" fill="none" stroke="#9ec5ff" stroke-width="1.5"/>
    <circle cx="135" cy="170" r="10" fill="none" stroke="#9ec5ff" stroke-width="1.5"/>
    <text x="90" y="225" text-anchor="middle" fill="#9ec5ff" font-size="10">后</text>
  </svg>`;
}
window.openLightbox = function (url) {
  $("lightbox-img").src = url;
  $("lightbox-bg").classList.add("open");
};
$("lightbox-bg").addEventListener("click", () => $("lightbox-bg").classList.remove("open"));
window.scrollToSide = function () {};

/* ================= 7. 出园核验与异常审批 ================= */
function renderExit(s) {
  const cards = s.open_cycles.filter((c) =>
    ["exit_pending", "pickup_pending", "pickup_verified", "hitched"].includes(c.phase));
  $("exit-cards").innerHTML = cards.map((c) => {
    const abnormal = c.abnormal_exit ? (c.exit_approved ? "已批准异常放行" : "异常放行待批准") : "";
    const canExit = c.phase === "exit_pending" || (c.abnormal_exit && c.exit_approved);
    return `<div class="card" style="${c.abnormal_exit ? "border-left-color:var(--danger)" : ""}">
      <h3>${tractorSym()} ${esc(c.pickup_tractor_plate || c.tractor_plate)} ⇄ ${trailerSym()} ${esc(c.trailer_plate)}</h3>
      <div class="mono muted" style="font-size:11px">${c.code}</div>
      <table style="margin:6px 0">
        <tr><td>司机核对</td><td>${c.driver_confirmed ? '<span class="badge ok">✓</span>' : '<span class="badge gray">—</span>'} ${esc(c.pickup_driver_name || c.driver_name || "")}</td></tr>
        <tr><td>铅封</td><td>${c.seal_confirmed ? '<span class="badge ok">✓ 相符</span>' : '<span class="badge yellow">未核对/异常</span>'} <span class="mono">${esc(c.seal_code || "")}</span></td></tr>
        <tr><td>外观</td><td>${c.appearance_confirmed ? '<span class="badge ok">✓ 已对照</span>' : '<span class="badge gray">—</span>'}</td></tr>
        <tr><td>阶段</td><td><span class="badge yellow">${c.phase_cn}</span></td></tr>
        ${abnormal ? `<tr><td>异常</td><td><span class="badge red">${abnormal}</span></td></tr>` : ""}
      </table>
      <div class="pill-actions">
        ${canExit ? btn("出园确认 / 抬杆关闭", `doExit("${c.code}")`, c.abnormal_exit ? "danger" : "") : ""}
        ${!c.abnormal_exit && c.phase !== "exit_pending" ? btn("申请异常放行", `openAbn("${c.code}")`, "danger") : ""}
      </div></div>`;
  }).join("") || `<p class="muted">暂无待出园车辆</p>`;

  $("exc-body").innerHTML = s.exceptions.map((e) => `<tr>
    <td>${e.id}</td><td><span class="badge ${e.kind === "abnormal_exit" ? "red" : "yellow"}">${e.kind_cn}</span></td>
    <td>${esc(e.description)}</td>
    <td>${esc(e.raised_by)}</td>
    <td>${e.status === "open" ? '<span class="badge yellow">待处理</span>' :
      e.status === "approved" ? '<span class="badge ok">已批准</span>' : '<span class="badge gray">已驳回</span>'}</td>
    <td>${e.status === "open"
      ? `<button class="sm" onclick='decide(${e.id},true)'>批准</button>
         <button class="sm danger" onclick='decide(${e.id},false)'>驳回</button>`
      : `<span class="muted">${esc(e.decided_by || "")}</span>`}</td></tr>`).join("");
}
window.openAbn = function (cycle) {
  openModal(`<h3>异常放行申请 — ${cycle}</h3>
    <div class="form-row"><label>异常情况说明（铅封不符/损伤/无法提挂等）</label>
      <textarea id="m-note" rows="3"></textarea></div>
    <button class="danger" onclick='submitAction({action:"request_abnormal_exit",cycle:"${cycle}",note:document.getElementById("m-note").value||"现场申请异常放行"},"已提交，等待值班主管批准")'>提交申请（门岗）</button>
    <button class="ghost" onclick="closeModal()">取消</button>`);
};
window.decide = async function (id, approve) {
  try {
    await postAction({ action: "decide_exception", exception_id: id, approve, note: approve ? "主管现场核准" : "不符合放行条件" });
    toast(approve ? "已批准异常放行" : "已驳回", approve ? "ok" : "");
    refresh();
  } catch (e) { toast(e.message, "err"); }
};
window.doExit = async function (cycle) {
  try {
    const r = await postAction({ action: "exit_confirm", cycle });
    toast(r.abnormal ? "异常车辆放行，周转已关闭" : "出园确认，本次周转关闭", "ok");
    refresh();
  } catch (e) { toast(e.message, "err"); refresh(); }
};

/* ---------------- 登录角色 / Tab / 轮询 ---------------- */
function renderUserSelect(s) {
  if (CURRENT_USER && s.users.some((u) => u.username === CURRENT_USER.username)) return;
  const sel = $("user-select");
  sel.innerHTML = s.users.map((u) =>
    `<option value="${u.username}">${u.display_name} · ${({ guard: "门岗", dispatcher: "调度员", key_clerk: "钥匙员", inspector: "查验员", supervisor: "值班主管" })[u.role]}</option>`).join("");
  CURRENT_USER = s.users[0];
  sel.value = CURRENT_USER.username;
}
$("user-select").addEventListener("change", (e) => {
  CURRENT_USER = STATE.users.find((u) => u.username === e.target.value);
  toast(`已切换为 ${CURRENT_USER.display_name}`, "ok");
});
$("btn-reseed").addEventListener("click", async () => {
  await api("/api/reseed", { method: "POST" });
  toast("已重新加载高峰样例", "ok");
  refresh();
});

const VIEWS = ["dashboard", "queue", "stalls", "couple", "keys", "appearance", "exit"];
function switchView(v) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  VIEWS.forEach((x) => $(`view-${x}`).classList.toggle("active", x === v));
  if (STATE) renderAll(STATE);
}
document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => switchView(b.dataset.view)));

$("appearance-cycle") && $("appearance-cycle").addEventListener("change", () => renderAppearance(STATE));

function renderAll(s) {
  $("server-time").textContent = s.server_time;
  renderDashboard(s);
  renderQueue(s);
  renderStalls(s);
  renderCouple(s);
  renderKeys(s);
  renderAppearance(s);
  renderExit(s);
}
async function refresh() {
  try {
    const data = await api("/api/state");
    STATE = data.state;
    renderUserSelect(STATE);
    renderAll(STATE);
  } catch (e) {
    console.error(e);
  }
}
refresh();
setInterval(refresh, 4000);
