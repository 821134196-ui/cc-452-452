/* 海州物流园 · 甩挂交接平台前端（零依赖原生 JS） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    const err = new Error(data.message || `请求失败 ${res.status}`);
    err.code = data.error;
    err.status = res.status;
    throw err;
  }
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ICON_TRACTOR = '<svg class="symbol tractor" width="30" height="16" viewBox="0 0 64 32"><use href="#i-tractor"/></svg>';
const ICON_TRAILER = '<svg class="symbol trailer" width="34" height="16" viewBox="0 0 64 32"><use href="#i-trailer"/></svg>';

const STATUS_CN = {
  expected: "待到园", queued: "待卸挂队列", slotted: "已入位交接中",
  handed_over: "交接完成待提挂", pickup_pending: "等待提挂",
  pickup_started: "提挂核验中", ready_to_exit: "待出园",
  exited: "已出园", closed: "周转已关闭", held: "异常挂起",
  abnormal_release: "异常放行",
};
const STATUS_PILL = {
  queued: "yellow", slotted: "blue", handed_over: "green",
  pickup_started: "blue", ready_to_exit: "yellow", closed: "gray",
  held: "red", abnormal_release: "pink",
};
const ROLE_CN = {
  gate: "门岗", dispatcher: "调度员", keyclerk: "钥匙员",
  inspector: "查验员", supervisor: "值班主管",
};
const EVENT_CN = {
  arrival: "到园签到", appointment_late: "预约迟到", slot_assigned: "分配车位",
  slot_occupied: "车位被占", slot_locked: "车位锁定", unhitched: "牵引挂车解绑",
  chocked: "轮挡确认", key_deposited: "钥匙入柜", key_wrong_cell: "钥匙错格",
  handover_completed: "交接完成", pickup_started: "发起提挂",
  driver_substituted: "司机替换", tractor_substituted: "牵引车替换",
  seal_verified: "铅封核对通过", seal_mismatch: "铅封不符",
  damage_found: "外观损伤", appearance_passed: "外观通过",
  cannot_pickup: "无法提挂", hitched: "提挂挂接", key_released: "钥匙发放",
  approval_requested: "异常申请", approval_approved: "主管批准",
  approval_rejected: "审批驳回", exit_gate: "出园放行", turn_closed: "关闭周转",
  wait_timeout: "等待超时", gate_fault: "道闸故障",
};
const POS_CN = { front: "前端", rear: "尾门", left: "左侧", right: "右侧" };

const state = {
  me: null,
  roleMatrix: {},
  tab: "arrivals",
  catalog: { drivers: [], tractors: [], trailers: [] },
  slots: [],
  timer: null,
};

const can = (action) => state.me && (state.roleMatrix[state.me.role] || []).includes(action);
const toast = (msg, ok = false) => {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = ok ? "ok" : "err";
  el.style.display = "block";
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.style.display = "none"), 3200);
};
const act = async (path, body) => {
  try {
    const r = await api(path, { method: "POST", body });
    toast("操作成功", true);
    await refresh();
    return r;
  } catch (e) {
    toast(`✕ ${e.message}`);
    throw e;
  }
};

/* --------------------------------------------------------------- 登录 */
async function boot() {
  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    state.tab = btn.dataset.tab;
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    refresh();
  });
  try {
    const [me, matrix] = await Promise.all([
      api("/api/me"), api("/api/role-matrix")]);
    state.roleMatrix = matrix.items;
    if (me.staff) {
      state.me = me.staff;
      renderSession();
      const cat = await api("/api/catalog");
      state.catalog = cat;
    }
  } catch (e) { /* 未登录 */ }
  tickClock();
  setInterval(tickClock, 1000);
  refresh();
  state.timer = setInterval(refresh, 8000);
}

function tickClock() {
  const now = new Date();
  $("#clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
}

function renderSession() {
  if (!state.me) { $("#session").innerHTML = ""; return; }
  $("#session").innerHTML =
    `<b>${esc(state.me.name)}</b> <span class="role-badge">${ROLE_CN[state.me.role]}</span>
     <button class="ghost" style="margin-left:8px" onclick="doLogout()">退出</button>`;
}

async function doLogout() {
  await api("/api/logout", { method: "POST", body: {} });
  location.reload();
}
window.doLogout = doLogout;

async function showLogin() {
  if ($(".login-mask")) return;
  const { items } = await api("/api/staff");
  const mask = document.createElement("div");
  mask.className = "login-mask";
  mask.innerHTML = `<div class="login-box">
    <h2>岗位登录</h2>
    <p>选择园区岗位进入（演示环境免密登录，工号即账号）。不同岗位可见相同看板，操作权限不同。</p>
    <div class="staff-grid">
      ${items.map((s) => `<button class="staff-btn" data-login="${esc(s.login)}">
        <b>${esc(s.name)} <span class="role-badge">${ROLE_CN[s.role]}</span></b>
        <small>${esc(s.login)}</small></button>`).join("")}
    </div></div>`;
  document.body.appendChild(mask);
  mask.addEventListener("click", async (e) => {
    const btn = e.target.closest(".staff-btn");
    if (!btn) return;
    try {
      const r = await api("/api/login", { method: "POST", body: { login: btn.dataset.login } });
      state.me = r.staff;
      renderSession();
      const cat = await api("/api/catalog");
      state.catalog = cat;
      mask.remove();
      refresh();
    } catch (e2) { toast(e2.message); }
  });
}

/* --------------------------------------------------------------- 刷新 */
async function refresh() {
  if (!state.me) {
    $("#view").innerHTML = emptyHint("请先登录园区岗位");
    showLogin();
    return;
  }
  const renderers = {
    arrivals: renderArrivals, queue: renderQueue, slots: renderSlots,
    hitches: renderHitches, keys: renderKeys, appearance: renderAppearance,
    exits: renderExits, events: renderEvents,
  };
  try {
    await renderers[state.tab]();
  } catch (e) {
    if (e.status === 401) { state.me = null; location.reload(); return; }
    $("#view").innerHTML = `<div class="panel">加载失败：${esc(e.message)}</div>`;
  }
}
const emptyHint = (t) => `<div class="panel muted" style="padding:40px;text-align:center">${esc(t)}</div>`;
const pill = (status, cn) => `<span class="pill ${STATUS_PILL[status] || "gray"}">${cn || STATUS_CN[status] || status}</span>`;

/* ----------------------------------------------------------- 到园看板 */
async function renderArrivals() {
  const [{ items }, { items: turns }] = await Promise.all([
    api("/api/arrivals"), api("/api/turns")]);
  const openCodes = new Set(turns.filter((t) => t.status !== "closed").map((t) => t.code));
  const late = items.filter((a) => a.status === "late" || a.is_late_now).length;
  const arrived = items.filter((a) => ["arrived", "late"].includes(a.status)).length;
  $("#view").innerHTML = `
    <div class="kpis">
      <div class="kpi"><div class="n">${items.length}</div><div class="l">今日预约</div></div>
      <div class="kpi ok"><div class="n">${arrived}</div><div class="l">已签到</div></div>
      <div class="kpi bad"><div class="n">${late}</div><div class="l">迟到 / 超时未到</div></div>
    </div>
    <div class="panel">
      <h2>车辆到园看板 <span class="hint">门岗核对预约后抬杆，车辆进入待卸挂队列；迟到与司机替换单独留痕</span></h2>
      <table><thead><tr>
        <th>预约号</th><th>计划到园</th><th>牵引车</th><th>挂车 / 铅封</th>
        <th>预约司机</th><th>类型</th><th>状态</th><th class="right">签到操作</th>
      </tr></thead><tbody>
      ${items.map((a) => {
        const st = a.status;
        const stPill = st === "late" || a.is_late_now
          ? '<span class="pill red">迟到</span>'
          : st === "arrived" ? '<span class="pill green">已到园</span>'
          : st === "done" ? '<span class="pill gray">已完成</span>'
          : '<span class="pill yellow">待到园</span>';
        const open = openCodes.has(`T-${a.code}`) || st === "done";
        return `<tr class="clickable" data-turn-code="T-${a.code}">
          <td class="plate">${esc(a.code)}</td>
          <td>${esc(a.planned_arrival.slice(11, 16))}</td>
          <td>${ICON_TRACTOR}<span class="plate">${esc(a.tractor_plate)}</span></td>
          <td>${ICON_TRAILER}<span class="plate">${esc(a.trailer_plate)}</span></td>
          <td>${esc(a.driver_name)}</td>
          <td><span class="muted">${({ drop: "卸挂", pickup: "提挂", both: "甩挂" })[a.kind]}</span></td>
          <td>${stPill}</td>
          <td class="right">${(!open && can("arrive"))
            ? `<button class="act" data-arrive="${a.code}">签到放行</button>`
            : open ? '<span class="muted">—</span>' : ""}</td>
        </tr>`;
      }).join("")}
      </tbody></table>
    </div>`;
  $("#view tbody").addEventListener("click", onArrivalClick);
}

function onArrivalClick(e) {
  const btn = e.target.closest("[data-arrive]");
  const tr = e.target.closest("tr[data-turn-code]");
  if (btn) {
    e.stopPropagation();
    const code = btn.dataset.arrive;
    // 简化：实际司机/牵引车默认与预约一致；异常替换由详情中登记
    act("/api/actions/arrive", { appointment_code: code });
    return;
  }
  if (tr) {
    api("/api/turns").then(({ items }) => {
      const t = items.find((x) => x.code === tr.dataset.turnCode);
      if (t) openDrawer(t.id);
    });
  }
}

/* ------------------------------------------------------------ 甩挂队列 */
async function renderQueue() {
  const [{ items }, { map }] = await Promise.all([
    api("/api/queue"), api("/api/slots")]);
  state.slots = map.slots;
  const freeSlots = map.slots.filter((s) => s.status === "free");
  const overtime = items.filter((t) => t.overtime).length;
  $("#view").innerHTML = `
    <div class="kpis">
      <div class="kpi"><div class="n">${items.length}</div><div class="l">队列 / 交接中</div></div>
      <div class="kpi ${overtime ? "bad" : "ok"}"><div class="n">${overtime}</div><div class="l">等待超时 ≥30min</div></div>
      <div class="kpi"><div class="n">${freeSlots.length}</div><div class="l">空闲车位</div></div>
    </div>
    <div class="panel"><h2>待卸挂队列 <span class="hint">四步交接：挂车停妥 → 解绑 → 轮挡 → 钥匙入柜</span></h2>
      ${items.length === 0 ? emptyHint("队列已清空") : `<table><thead><tr>
        <th>周转单</th><th>挂车</th><th>到园牵引/司机</th><th>等待</th>
        <th>交接进度</th><th>车位</th><th class="right">操作</th>
      </tr></thead><tbody>
      ${items.map((t) => {
        const steps = [
          ["slotted_at", "停妥"], ["unhitched_at", "解绑"],
          ["chocked_at", "轮挡"], ["key_deposited_at", "钥匙入柜"]];
        const flow = steps.map(([k, cn], i) => {
          const cls = t[k] ? "done" : (i === steps.findIndex(([x]) => !t[x]) ? "cur" : "");
          return `<span class="step ${cls}">${cn}</span>`;
        }).join('<span class="arrow">→</span>');
        const pct = Math.min(100, t.waiting_min * 100 / 30);
        return `<tr>
          <td><a class="plate" href="#" data-detail="${t.id}">${esc(t.code)}</a></td>
          <td>${ICON_TRAILER}<span class="plate">${esc(t.trailer_plate)}</span></td>
          <td class="muted">${esc(t.in_tractor_plate || "")} / ${esc(t.in_driver_name || "")}</td>
          <td><span class="waitbar ${t.overtime ? "over" : ""}"><i style="width:${pct}%"></i></span>
              <b style="margin-left:6px">${t.waiting_min}′</b>
              ${t.overtime ? ' <span class="pill red">超时</span>' : ""}</td>
          <td><div class="flow">${flow}</div></td>
          <td>${t.slot ? `<b>${esc(t.slot.code)}</b>` : '<span class="muted">未分配</span>'}</td>
          <td class="right nowrap">${queueActions(t, freeSlots)}</td>
        </tr>`;
      }).join("")}</tbody></table>`}
    </div>`;
  bindActionDelegation($("#view tbody"));
}

function queueActions(t, freeSlots) {
  const parts = [];
  if (!t.slot_id && can("assign_slot")) {
    const opts = freeSlots.map((s) => `<option value="${s.code}">${s.code}</option>`).join("");
    parts.push(`<span class="row-form">
      <select id="slot-${t.id}">${opts}</select>
      <button class="act" data-action="assign" data-id="${t.id}">分配车位</button></span>`);
  }
  if (t.slot_id && !t.unhitched_at && can("unhitch"))
    parts.push(`<button class="act" data-action="unhitch" data-id="${t.id}">登记解绑</button>`);
  if (t.unhitched_at && !t.chocked_at && can("chock"))
    parts.push(`<button class="ghost" data-action="chock" data-id="${t.id}">确认轮挡</button>`);
  if (t.chocked_at && !t.key_deposited_at && can("deposit_key"))
    parts.push(`<button class="act" data-action="deposit-key" data-id="${t.id}">钥匙入柜</button>`);
  return parts.join(" ");
}

function bindActionDelegation(root) {
  if (!root) return;
  root.addEventListener("click", async (e) => {
    const det = e.target.closest("[data-detail]");
    if (det) { e.preventDefault(); openDrawer(Number(det.dataset.detail)); return; }
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const a = btn.dataset.action;
    if (a === "assign") {
      const slot = $(`#slot-${id}`).value;
      return act("/api/actions/assign-slot", { turn_id: id, slot_code: slot });
    }
    if (a === "deposit-key") return depositKeyDialog(id);
    const map = {
      unhitch: ["/api/actions/unhitch", {}],
      chock: ["/api/actions/chock", {}],
    };
    if (map[a]) return act(map[a][0], { turn_id: id, ...map[a][1] });
  });
}

async function depositKeyDialog(turnId) {
  const box = prompt("钥匙柜编号（BOX-A / BOX-B / BOX-C）", "BOX-A");
  if (!box) return;
  const cell = prompt("格口号（两位数字）", "08");
  if (!cell) return;
  try {
    await act("/api/actions/deposit-key", { turn_id: turnId, box_id: box, cell_no: cell });
  } catch (e) { /* 错格已留痕，可换格口重试 */ }
}

/* -------------------------------------------------------------- 车位图 */
async function renderSlots() {
  const { map } = await api("/api/slots");
  state.slots = map.slots;
  const zones = Object.entries(map.zones);
  $("#view").innerHTML = `
    <div class="panel"><h2>停车位平面分布图 <span class="hint">按真实编号排列（区-排-列）；虚线为空位，信号黄为占用，红色为锁定维保</span></h2>
      <div class="muted" style="margin-bottom:10px">
        <span class="pill gray">虚线框 = 空闲</span>
        <span class="pill yellow">黄框 = 占用</span>
        <span class="pill red">红框 = 锁定</span>
        点击占用车位查看周转单；队列页为挂车分配车位。
      </div>
      ${zones.map(([zone, slots]) => {
        const cols = Math.max(...slots.map((s) => s.col_no));
        const rows = [...new Set(slots.map((s) => s.row_no))].sort((a, b) => a - b);
        return `<div class="zone-map">
          <div class="zone-title">${zone} 区 <span class="muted">${rows.length} 排 × ${cols} 列</span></div>
          ${rows.map((r) => {
            const rowSlots = slots.filter((s) => s.row_no === r)
              .sort((a, b) => a.col_no - b.col_no);
            return `
              <div class="slot-grid" style="grid-template-columns:repeat(${cols},1fr)">
                ${rowSlots.map(slotCard).join("")}
              </div>
              <div class="lane"><span>行车道 →</span></div>`;
          }).join("")}
        </div>`;
      }).join("")}
    </div>`;
  $("#view .panel").addEventListener("click", (e) => {
    const card = e.target.closest(".slot[data-turn]");
    if (card) openDrawer(Number(card.dataset.turn));
  });
}

function slotCard(s) {
  const statusCn = { free: "空闲", occupied: "占用", locked: "锁定维保" }[s.status];
  return `<div class="slot ${s.status}" ${s.turn_id ? `data-turn="${s.turn_id}"` : ""}>
    <div><span class="code">${esc(s.code)}</span>
      ${s.status === "occupied" ? `<span class="mini">${ICON_TRAILER}</span>` : ""}</div>
    <div class="st">${s.status === "occupied"
      ? `${esc(s.trailer_plate || "")}<br><span class="muted">${esc(s.turn_code || "")}</span>`
      : statusCn}</div>
  </div>`;
}

/* ------------------------------------------------------------ 挂接操作 */
async function renderHitches() {
  const [{ items: hitches }, { items: turns }] = await Promise.all([
    api("/api/hitches"), api("/api/turns")]);
  const active = turns.filter((t) =>
    ["handed_over", "pickup_started", "held"].includes(t.status));
  $("#view").innerHTML = `
    <div class="panel"><h2>当前有效挂接关系 <span class="hint">一辆挂车同一时刻仅允许一组有效挂接（数据库唯一约束）</span></h2>
      ${hitches.length === 0 ? emptyHint("暂无挂接中的车组") : `<div class="keyboard-rack">
        ${hitches.map((h) => `<div class="keycard in_cabinet">
          <div class="pair-card">
            ${ICON_TRACTOR}<div><b class="plate">${esc(h.tractor_plate)}</b><div class="meta">牵引车</div></div>
            <span class="link-hitch"></span>
            ${ICON_TRAILER}<div><b class="plate">${esc(h.trailer_plate)}</b><div class="meta">挂车 · ${esc(h.turn_code)}</div></div>
          </div></div>`).join("")}
      </div>`}
    </div>
    <div class="panel"><h2>提挂核验与挂接 <span class="hint">核对司机 → 铅封 → 外观，全部通过方可挂接；异常须主管批准</span></h2>
      <table><thead><tr><th>周转单</th><th>车位/挂车</th><th>提挂牵引/司机</th>
        <th>铅封</th><th>外观</th><th>状态</th><th class="right">操作</th></tr></thead><tbody>
      ${active.length === 0 ? `<tr><td colspan="7">${emptyHint("暂无待提挂车辆")}</td></tr>`
        : active.map((t) => `<tr>
        <td><a href="#" data-detail="${t.id}" class="plate">${esc(t.code)}</a></td>
        <td>${esc(t.slot ? t.slot.code : "")} ${ICON_TRAILER}<span class="plate">${esc(t.trailer_plate)}</span></td>
        <td>${esc(t.out_tractor_plate || "—")}<br><span class="muted">${esc(t.out_driver_name || "")}</span></td>
        <td>${t.seal_verified_at ? '<span class="pill green">已核对</span>' : '<span class="pill gray">未核对</span>'}</td>
        <td>${appearanceChip(t)}</td>
        <td>${pill(t.status)}${t.hold_reason ? `<div class="muted">${esc(t.hold_reason)}</div>` : ""}</td>
        <td class="right nowrap">${pickupActions(t)}</td>
      </tr>`).join("")}
      </tbody></table>
    </div>`;
  $("#view tbody").addEventListener("click", onHitchClick);
  $$("#view [data-detail]").forEach((a) =>
    a.addEventListener("click", (e) => { e.preventDefault(); openDrawer(Number(a.dataset.detail)); }));
}

function appearanceChip(t) {
  // 由 exits 接口数据更全；此处依据字段粗判
  return t.appearance_out_at ? '<span class="pill green">已检</span>'
    : '<span class="pill gray">未检</span>';
}

function pickupActions(t) {
  const p = [];
  if (t.status === "handed_over" && can("start_pickup"))
    p.push(`<button class="act" data-pa="start" data-id="${t.id}">发起提挂</button>`);
  if (["pickup_started", "held"].includes(t.status) && !t.seal_verified_at && can("verify_seal"))
    p.push(`<button class="act" data-pa="seal" data-id="${t.id}">核对铅封</button>`);
  if (["pickup_started", "held"].includes(t.status) && !t.appearance_out_at && can("inspect_appearance"))
    p.push(`<button class="ghost" data-pa="appear" data-id="${t.id}">提挂外观</button>`);
  if (t.status === "pickup_started" && can("cannot_pickup"))
    p.push(`<button class="ghost" data-pa="cannot" data-id="${t.id}">无法提挂</button>`);
  if (t.status === "pickup_started" && can("hitch"))
    p.push(`<button class="act" data-pa="hitch" data-id="${t.id}">挂接</button>`);
  if (t.status === "held" && can("decide_approval") === false)
    p.push(`<button class="ghost danger" data-pa="request" data-id="${t.id}">申请异常放行</button>`);
  if (t.status === "held" && can("decide_approval"))
    p.push(`<button class="act" data-pa="approve" data-id="${t.id}">审批台</button>`);
  return p.join(" ");
}

async function onHitchClick(e) {
  const btn = e.target.closest("[data-pa]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const a = btn.dataset.pa;
  if (a === "start") return startPickupDialog(id);
  if (a === "seal") return sealDialog(id);
  if (a === "appear") return inspectDialog(id, "departure");
  if (a === "cannot") {
    const reason = prompt("无法提挂原因（故障/缺件/放弃）", "牵引鞍座气路故障");
    if (reason) return act("/api/actions/cannot-pickup", { turn_id: id, reason });
    return;
  }
  if (a === "hitch") return act("/api/actions/hitch", { turn_id: id });
  if (a === "request") return requestApprovalDialog(id);
  if (a === "approve") return openDrawer(id, "approvals");
}

function startPickupDialog(id) {
  const tractors = state.catalog.tractors
    .filter((t) => ["idle", "dropped", "outbound"].includes(t.status))
    .map((t) => `<option value="${t.id}">${t.plate} · ${t.model}</option>`).join("");
  const drivers = state.catalog.drivers
    .map((d) => `<option value="${d.id}">${d.name} · ${d.employer}</option>`).join("");
  const html = `
    <h3>发起提挂 · 周转单 #${id}</h3>
    <label class="fld">提挂牵引车
      <select id="dlg-tractor">${tractors}</select></label>
    <label class="fld" style="margin-top:8px">提挂司机
      <select id="dlg-driver">${drivers}</select></label>
    <div class="right" style="margin-top:14px">
      <button class="ghost" id="dlg-cancel">取消</button>
      <button class="act" id="dlg-ok">确认提挂</button></div>`;
  openCustom(html, async () =>
    act("/api/actions/start-pickup", {
      turn_id: id,
      out_tractor_id: Number($("#dlg-tractor").value),
      out_driver_id: Number($("#dlg-driver").value),
    }));
}

function sealDialog(id) {
  const trailer = state.catalog.trailers.find((x) => true);
  const html = `
    <h3>铅封核对 · 周转单 #${id}</h3>
    <p class="muted">请用扫码枪扫描挂车尾门铅封号，与登记值自动比对。</p>
    <label class="fld">实扫铅封号 <input id="dlg-seal" placeholder="SL-7xxx-xx" value=""></label>
    <div class="right" style="margin-top:14px">
      <button class="ghost" id="dlg-cancel">取消</button>
      <button class="act" id="dlg-ok">提交核对</button></div>`;
  openCustom(html, async () => {
    const r = await act("/api/actions/verify-seal",
      { turn_id: id, seal_no: $("#dlg-seal").value.trim() });
    if (r && r.result && r.result.match === false)
      toast("✕ 铅封不符，车辆已挂起，请主管审批");
  });
}

function inspectDialog(id, phase) {
  const html = `
    <h3>${phase === "arrival" ? "到园" : "提挂"}外观检查 · #${id}</h3>
    <p class="muted">照片由“外观对照”页模拟相机采集；此处登记结论。</p>
    <div class="row-form">
      <label class="fld">结论
        <select id="dlg-result">
          <option value="pass">合格放行</option>
          <option value="damage">发现损伤</option>
        </select></label>
    </div>
    <label class="fld" style="margin-top:8px">损伤描述（损伤时必填）
      <textarea id="dlg-desc" rows="2"></textarea></label>
    <div class="right" style="margin-top:14px">
      <button class="ghost" id="dlg-cancel">取消</button>
      <button class="act" id="dlg-ok">提交检查</button></div>`;
  openCustom(html, () => act("/api/actions/inspect", {
    turn_id: id, phase,
    result: $("#dlg-result").value,
    damage_desc: $("#dlg-desc").value,
  }));
}

function requestApprovalDialog(id) {
  const html = `
    <h3>异常放行申请 · #${id}</h3>
    <label class="fld">异常类型
      <select id="dlg-reason">
        <option value="seal_mismatch">铅封不符</option>
        <option value="appearance_damage">外观损伤</option>
        <option value="cannot_pickup">无法提挂</option>
        <option value="driver_substituted">司机替换存疑</option>
      </select></label>
    <label class="fld" style="margin-top:8px">情况说明
      <textarea id="dlg-detail" rows="3"></textarea></label>
    <div class="right" style="margin-top:14px">
      <button class="ghost" id="dlg-cancel">取消</button>
      <button class="act" id="dlg-ok">提交主管审批</button></div>`;
  openCustom(html, () => act("/api/actions/request-approval", {
    turn_id: id, reason_code: $("#dlg-reason").value, detail: $("#dlg-detail").value,
  }));
}

function openCustom(innerHtml, onOk) {
  closeCustom();
  const scrim = document.createElement("div");
  scrim.className = "scrim"; scrim.id = "custom-scrim";
  const box = document.createElement("div");
  box.className = "drawer"; box.id = "custom-drawer";
  box.innerHTML = `<div class="panel">${innerHtml}</div>`;
  document.body.appendChild(scrim);
  document.body.appendChild(box);
  $("#dlg-cancel", box).onclick = closeCustom;
  scrim.onclick = closeCustom;
  $("#dlg-ok", box).onclick = async () => {
    try { await onOk(); closeCustom(); } catch (e) { /* toast 已提示 */ }
  };
}
function closeCustom() {
  $("#custom-scrim")?.remove();
  $("#custom-drawer")?.remove();
}

/* ------------------------------------------------------------ 钥匙台账 */
function barcodeBars(tag) {
  // 由标签确定性生成密集竖条，模拟可扫描条码
  let seed = 0;
  for (const ch of tag) seed = (seed * 31 + ch.charCodeAt(0)) & 0xffff;
  let s = "";
  for (let i = 0; i < 46; i++) {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    const w = (seed % 3) + 1;
    s += "█".repeat(w);
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    i += w - 1;
  }
  return s;
}

async function renderKeys() {
  const { items } = await api("/api/keys");
  const stat = {
    in_cabinet: items.filter((k) => k.status === "in_cabinet").length,
    checked_out: items.filter((k) => k.status === "checked_out").length,
    wrong_cell: items.filter((k) => k.status === "wrong_cell").length,
  };
  $("#view").innerHTML = `
    <div class="kpis">
      <div class="kpi ok"><div class="n">${stat.in_cabinet}</div><div class="l">柜内保管</div></div>
      <div class="kpi"><div class="n">${stat.checked_out}</div><div class="l">已发放随车</div></div>
      <div class="kpi bad"><div class="n">${stat.wrong_cell}</div><div class="l">错格告警</div></div>
    </div>
    <div class="panel"><h2>钥匙柜密集清单 <span class="hint">点击条码可全选扫描；取钥格口与登记不符即错格告警</span></h2>
      <div class="keyboard-rack">
      ${items.map((k) => {
        const cn = { in_cabinet: "柜内", checked_out: "已发放", wrong_cell: "错格" }[k.status];
        const cls = { in_cabinet: "green", checked_out: "blue", wrong_cell: "red" }[k.status];
        return `<div class="keycard ${k.status}">
          <div>
            <div class="barcode" title="点击复制/扫描">${barcodeBars(k.tag)}<br>${esc(k.tag)}</div>
            <div class="meta" style="margin-top:5px">
              <b>${esc(k.box_id)}-${esc(k.cell_no)}</b> · ${esc(k.trailer_plate)} · ${esc(k.turn_code)}
              ${k.note ? `<br><span style="color:var(--bad)">${esc(k.note)}</span>` : ""}
            </div>
          </div>
          <div style="margin-left:auto;text-align:center">
            <span class="pill ${cls}">${cn}</span>
            ${k.status === "in_cabinet" && can("release_key")
              ? `<br><button class="act" style="margin-top:6px" data-release="${k.id}" data-turn="${k.turn_id}">取钥放行</button>` : ""}
          </div>
        </div>`;
      }).join("")}
      </div>
    </div>`;
  $("#view .panel").addEventListener("click", async (e) => {
    const bc = e.target.closest(".barcode");
    if (bc) {
      const range = document.createRange();
      range.selectNodeContents(bc);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      return;
    }
    const btn = e.target.closest("[data-release]");
    if (btn) {
      const cell = prompt("扫描格口号（留空使用登记格口）", "");
      if (cell === null) return;
      try {
        await act("/api/actions/release-key",
          { turn_id: Number(btn.dataset.turn), cell_no: cell || null });
      } catch (e2) { /* 错格已告警 */ }
    }
  });
}

/* ------------------------------------------------------------ 外观对照 */
async function renderAppearance() {
  const { items } = await api("/api/turns");
  const inspectable = items.filter((t) =>
    !["closed"].includes(t.status) && t.arrived_at);
  const sel = inspectable.map((t) =>
    `<option value="${t.id}">${t.code} · ${t.trailer_plate}</option>`).join("");
  $("#view").innerHTML = `
    <div class="panel"><h2>外观对照 <span class="hint">照片在车辆轮廓对应方位展开，到园 / 提挂两次检查可比对</span></h2>
      <div class="row-form">
        <label class="fld">选择周转单 <select id="appear-turn">${sel}</select></label>
        <span id="appear-status" class="muted"></span>
      </div>
      <div id="appear-body" style="margin-top:14px"></div>
    </div>`;
  const selectEl = $("#appear-turn");
  const draw = () => drawAppearance(Number(selectEl.value));
  selectEl.addEventListener("change", draw);
  if (selectEl.value) draw();
}

async function drawAppearance(turnId) {
  const { turn } = await api(`/api/turns/${turnId}`);
  const arr = turn.inspections.filter((i) => i.phase === "arrival").pop();
  const dep = turn.inspections.filter((i) => i.phase === "departure").pop();
  const photoMap = (insp) => Object.fromEntries((insp?.photos || []).map((p) => [p.pos, p]));
  const aPhotos = photoMap(arr), dPhotos = photoMap(dep);
  $("#appear-status").innerHTML =
    `到园：${resultPill(arr?.result)} 提挂：${resultPill(dep?.result)}` +
    (turn.hold_reason ? ` <span class="pill red">${esc(turn.hold_reason)}</span>` : "");

  const tile = (pos, ph, phase) => {
    if (!ph) {
      return `<div class="photo-tile missing">
        <div style="height:90px;display:flex;align-items:center;justify-content:center"
          class="muted">无照片</div>
        <div class="cap"><span class="pos-label">${POS_CN[pos]}</span><span>${phase}</span></div>
      </div>`;
    }
    const dmg = (phase === "到园" ? arr?.result : dep?.result) === "damage" &&
      /损|凹|刮|变形|异常/.test(ph.note || "");
    return `<div class="photo-tile ${dmg ? "damage" : ""}">
      <img src="/photos/${esc(ph.path)}?t=${Date.now()}" alt="${POS_CN[pos]}" data-full="/photos/${esc(ph.path)}">
      <div class="cap"><span class="pos-label">${POS_CN[pos]}</span>
        <span>${dmg ? "⚠ " + esc(ph.note) : phase}</span></div>
    </div>`;
  };

  $("#appear-body").innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px">
      ${["arrival", "departure"].map((phase) => {
        const phs = phase === "arrival" ? aPhotos : dPhotos;
        const insp = phase === "arrival" ? arr : dep;
        const title = phase === "arrival" ? "到园外观（签到后）" : "提挂外观（挂接前）";
        return `<div>
          <h4 style="color:var(--yellow);margin:0 0 6px">${title}
            <span class="muted">${insp ? esc(insp.staff_name || "") + " · " + esc(insp.checked_at) : "尚未检查"}</span></h4>
          ${vehicleOutline(phase, ["front", "left", "right", "rear"].map((pos) => tile(pos, phs[pos], phase === "arrival" ? "到园" : "提挂")))}
          ${insp?.damage_desc ? `<p style="color:var(--bad);font-size:12.5px">损伤描述：${esc(insp.damage_desc)}</p>` : ""}
        </div>`;
      }).join("")}
    </div>
    <div class="dsec">
      <h4>模拟现场相机（按方位补拍）</h4>
      <div class="row-form">
        <select id="cam-pos">
          ${["front", "rear", "left", "right"].map((p) =>
            `<option value="${p}">${POS_CN[p]}</option>`).join("")}
        </select>
        <label class="fld" style="flex-direction:row;align-items:center;gap:6px">
          <input type="checkbox" id="cam-damage" style="width:auto"> 标记异常损伤</label>
        <input id="cam-note" placeholder="备注（可选）" style="min-width:220px">
        <button class="act" id="cam-shoot">拍照生成</button>
        <span class="muted">照片为本地 SVG 模拟图，检查结论请在挂接操作页登记。</span>
      </div>
    </div>`;

  $("#cam-shoot").onclick = async () => {
    await act("/api/mock/photo", {
      turn_id: turnId, pos: $("#cam-pos").value,
      damaged: $("#cam-damage").checked, note: $("#cam-note").value,
    });
    drawAppearance(turnId);
  };
  $$("#appear-body img[data-full]").forEach((img) =>
    img.onclick = () => window.open(img.dataset.full, "_blank"));
}

function resultPill(r) {
  if (!r) return '<span class="pill gray">未检</span>';
  return r === "pass" ? '<span class="pill green">合格</span>'
    : '<span class="pill red">损伤</span>';
}

function vehicleOutline(phase, tiles) {
  // tiles: [front, left, right, rear]；照片环绕车辆平面轮廓展开
  const [front, left, right, rear] = tiles;
  return `
  <div style="display:grid;grid-template-columns:1fr 150px 1fr;gap:8px;align-items:center">
    <div></div><div>${front}</div><div></div>
    <div>${left}</div>
    <div style="text-align:center">
      <svg viewBox="0 0 120 220" width="128" height="224" aria-label="车辆平面轮廓">
        <rect x="38" y="6" width="44" height="30" rx="5" fill="none" stroke="#9fd0e8" stroke-width="2"/>
        <text x="60" y="25" fill="#9fd0e8" font-size="10" text-anchor="middle">牵引</text>
        <rect x="24" y="42" width="72" height="8" rx="2" fill="none" stroke="#f6c945" stroke-width="1.6"/>
        <rect x="16" y="56" width="88" height="140" rx="6" fill="rgba(246,201,69,.06)"
          stroke="#f6c945" stroke-width="2.2"/>
        <line x1="60" y1="60" x2="60" y2="192" stroke="#f6c945" stroke-width="1" stroke-dasharray="6 6"/>
        <circle cx="20" cy="86" r="8" fill="#2b2f33" stroke="#eef0f2" stroke-width="2"/>
        <circle cx="100" cy="86" r="8" fill="#2b2f33" stroke="#eef0f2" stroke-width="2"/>
        <circle cx="20" cy="166" r="8" fill="#2b2f33" stroke="#eef0f2" stroke-width="2"/>
        <circle cx="100" cy="166" r="8" fill="#2b2f33" stroke="#eef0f2" stroke-width="2"/>
        <text x="60" y="132" fill="#f6c945" font-size="11" text-anchor="middle">挂车</text>
        <text x="60" y="15" fill="#9fd0e8" font-size="14" text-anchor="middle">↑ 前</text>
      </svg>
    </div>
    <div>${right}</div>
    <div></div><div>${rear}</div><div></div>
  </div>`;
}

/* ------------------------------------------------------------ 出园核验 */
async function renderExits() {
  const [{ items }, { items: approvals0 }] = await Promise.all([
    api("/api/exits"), api("/api/events")]);
  const pendingAp = approvals0.filter((e) => e.type === "approval_requested").length;
  $("#view").innerHTML = `
    <div class="kpis">
      <div class="kpi"><div class="n">${items.length}</div><div class="l">核验中车辆</div></div>
      <div class="kpi bad"><div class="n">${items.filter((t) => t.status === "held").length}</div><div class="l">挂起待处理</div></div>
      <div class="kpi warn"><div class="n">${items.filter((t) => t.checks.abnormal_approved).length}</div><div class="l">已批准异常</div></div>
    </div>
    <div class="panel"><h2>出园核验 <span class="hint">挂接 + 钥匙发放齐备方可放行；异常车辆须值班主管批准</span></h2>
      ${items.length === 0 ? emptyHint("暂无待出园车辆") : `<table><thead><tr>
        <th>周转单</th><th>车组</th><th>核验清单</th><th>异常审批</th><th>状态</th><th class="right">操作</th>
      </tr></thead><tbody>
      ${items.map((t) => `<tr>
        <td><a href="#" data-detail="${t.id}" class="plate">${esc(t.code)}</a></td>
        <td>${ICON_TRACTOR}<span class="plate">${esc(t.out_tractor_plate || "")}</span><br>
            ${ICON_TRAILER}<span class="plate">${esc(t.trailer_plate)}</span></td>
        <td><div class="checks">
          ${checkChip("发起提挂", t.checks.pickup_started)}
          ${checkChip("铅封", t.checks.seal_verified)}
          ${checkChip("外观", t.checks.appearance_departure === "pass",
             t.checks.appearance_departure === "damage" ? "外观损伤" : null)}
          ${checkChip("已挂接", t.checks.hitched)}
          ${checkChip("钥匙已发", t.checks.key_released)}
        </div></td>
        <td>${t.checks.abnormal_approved
          ? `<span class="pill pink">已批准·${esc(t.checks.abnormal_approved.reason_code)}</span>`
          : (t.abnormal ? '<span class="pill red">待批准</span>' : '<span class="muted">—</span>')}</td>
        <td>${pill(t.status)}</td>
        <td class="right nowrap">
          ${t.status === "held" && can("decide_approval")
            ? `<button class="act" data-ex="approve" data-id="${t.id}">前往审批</button>` : ""}
          ${t.status === "ready_to_exit" && can("exit_gate")
            ? `<button class="act ${t.abnormal ? "danger" : ""}" data-ex="exit" data-id="${t.id}">
               ${t.abnormal ? "异常放行出园" : "核验放行"}</button>` : ""}
        </td>
      </tr>`).join("")}
      </tbody></table>`}
    </div>`;
  $("#view tbody")?.addEventListener("click", async (e) => {
    const det = e.target.closest("[data-detail]");
    if (det) { e.preventDefault(); return openDrawer(Number(det.dataset.detail)); }
    const btn = e.target.closest("[data-ex]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    if (btn.dataset.ex === "exit") {
      const msg = btn.classList.contains("danger")
        ? "该车辆为异常批准车辆，确认抬杆放行并关闭周转？"
        : "核验通过，确认抬杆放行并关闭本次周转？";
      if (!confirm(msg)) return;
      return act("/api/actions/exit", { turn_id: id });
    }
    if (btn.dataset.ex === "approve") return openDrawer(id, "approvals");
  });
}

function checkChip(cn, ok, badText) {
  if (ok === true) return `<span class="chk yes">✓ ${cn}</span>`;
  if (badText) return `<span class="chk no">✕ ${badText}</span>`;
  return `<span class="chk no">○ ${cn}</span>`;
}

/* ------------------------------------------------------------ 异常流水 */
async function renderEvents() {
  const { items } = await api("/api/events");
  const notable = items.filter((e) => e.severity !== "info");
  $("#view").innerHTML = `
    <div class="panel"><h2>异常与预警流水 <span class="hint">迟到 / 车位占用 / 钥匙错格 / 铅封不符 / 损伤 / 司机替换 / 无法提挂 / 超时</span></h2>
      <table><thead><tr><th>时间</th><th>类型</th><th>级别</th><th>周转单</th><th>详情</th></tr></thead><tbody>
      ${notable.map((e) => {
        let detail = "";
        try { detail = JSON.stringify(JSON.parse(e.detail), null, 0); } catch { detail = e.detail; }
        const sevCn = { warn: "预警", exception: "异常", critical: "关键" }[e.severity] || e.severity;
        const sevCls = { warn: "yellow", exception: "red", critical: "pink" }[e.severity] || "gray";
        return `<tr>
          <td class="nowrap muted">${esc(e.ts)}</td>
          <td><b>${EVENT_CN[e.type] || e.type}</b></td>
          <td><span class="pill ${sevCls}">${sevCn}</span></td>
          <td>${e.turn_id ? `<a href="#" data-detail="${e.turn_id}" class="plate">#${e.turn_id}</a>` : ""}</td>
          <td class="muted" style="max-width:420px">${esc(detail)}</td>
        </tr>`;
      }).join("")}
      </tbody></table>
    </div>`;
  $("#view tbody")?.addEventListener("click", (e) => {
    const a = e.target.closest("[data-detail]");
    if (a) { e.preventDefault(); openDrawer(Number(a.dataset.detail)); }
  });
}

/* ------------------------------------------------------------ 详情抽屉 */
async function openDrawer(turnId, anchor) {
  $("#scrim").hidden = false;
  const dr = $("#drawer");
  dr.hidden = false;
  dr.innerHTML = `<div class="muted">加载中…</div>`;
  const { turn } = await api(`/api/turns/${turnId}`);
  const step = (k, cn, by, at) =>
    `<div class="step ${turn[k] ? "done" : ""}">${cn}${turn[k] ? ` · ${esc((at || "").slice(11, 16))}` : ""}</div>`;
  dr.innerHTML = `
    <button class="ghost close" id="dr-close">关闭</button>
    <h3>${esc(turn.code)} ${pill(turn.status)}</h3>
    <div class="muted">挂车 <span class="plate">${esc(turn.trailer_plate)}</span>
      （${esc(turn.trailer_spec)}） · 登记铅封 <b>${esc(turn.seal_no || "—")}</b></div>
    <div class="dsec"><h4>甩挂交接四步</h4>
      <div class="flow">
        ${step("slotted_at", "停妥入位")} <span class="arrow">→</span>
        ${step("unhitched_at", "解绑登记")} <span class="arrow">→</span>
        ${step("chocked_at", "轮挡确认")} <span class="arrow">→</span>
        ${step("key_deposited_at", "钥匙入柜")}
      </div>
      ${turn.slot ? `<p class="muted" style="margin-top:8px">车位 <b>${esc(turn.slot.code)}</b>
        （${turn.slot.zone} 区 ${turn.slot.row_no} 排 ${turn.slot.col_no} 列）</p>` : ""}
    </div>
    <div class="dsec"><h4>提挂与出园</h4>
      <dl class="kv">
        <dt>到园牵引</dt><dd class="plate">${esc(turn.in_tractor_plate || "—")} / ${esc(turn.in_driver_name || "")}</dd>
        <dt>提挂牵引</dt><dd class="plate">${esc(turn.out_tractor_plate || "—")} / ${esc(turn.out_driver_name || "")}</dd>
        <dt>铅封核对</dt><dd>${turn.seal_verified_at ? esc(turn.seal_verified_at) : "未完成"}</dd>
        <dt>完成挂接</dt><dd>${turn.hitched_at ? esc(turn.hitched_at) : "未完成"}</dd>
        <dt>钥匙发放</dt><dd>${turn.key_released_at ? esc(turn.key_released_at) : "未发放"}</dd>
        <dt>出园 / 关单</dt><dd>${turn.exited_at ? esc(turn.exited_at) + (turn.abnormal ? "（异常放行）" : "") : "未出园"}</dd>
        <dt>等待时长</dt><dd>${turn.wait_minutes} 分钟</dd>
      </dl>
    </div>
    <div class="dsec" id="dr-approvals"><h4>异常放行审批</h4>
      ${turn.approvals.length === 0 ? '<p class="muted">无审批记录</p>' : turn.approvals.map((a) => `
        <div class="eventline ${a.status === "approved" ? "critical" : a.status === "rejected" ? "warn" : ""}">
          <b>${esc(a.reason_code)}</b> · ${esc(a.detail || "")}<br>
          <span class="muted">${esc(a.status)} ${a.decided_at ? "· " + esc(a.decided_at) : ""}</span>
          ${a.status === "pending" && can("decide_approval") ? `
            <span style="float:right">
              <button class="act" data-apv="${a.id}" data-ok="1">批准放行</button>
              <button class="ghost" data-apv="${a.id}" data-ok="0">驳回</button>
            </span>` : ""}
        </div>`).join("")}
    </div>
    <div class="dsec"><h4>外观检查与照片</h4>
      ${turn.inspections.length === 0 ? '<p class="muted">暂无检查记录</p>' : turn.inspections.map((i) => `
        <div style="margin-bottom:10px">
          <b>${i.phase === "arrival" ? "到园检查" : "提挂检查"}</b>
          ${resultPill(i.result)} <span class="muted">${esc(i.checked_at)}</span>
          ${i.damage_desc ? `<div style="color:var(--bad)">${esc(i.damage_desc)}</div>` : ""}
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:5px">
            ${i.photos.map((p) => `<a href="/photos/${esc(p.path)}" target="_blank">
              <img src="/photos/${esc(p.path)}" width="104" height="65"
                style="border:1px solid var(--line);border-radius:4px"
                title="${POS_CN[p.pos]}：${esc(p.note)}"></a>`).join("")}
          </div>
        </div>`).join("")}
    </div>
    <div class="dsec"><h4>事件流水</h4>
      ${turn.events.map((e) => `
        <div class="eventline ${e.severity !== "info" ? e.severity : ""}">
          <span class="t">${esc(e.ts.slice(11))}</span>
          <b>${EVENT_CN[e.type] || e.type}</b>
          <span class="muted">${esc(e.detail && e.detail !== "{}" ? e.detail : "")}</span>
        </div>`).join("")}
    </div>`;
  $("#dr-close").onclick = closeDrawer;
  $("#scrim").onclick = closeDrawer;
  $$("#drawer [data-apv]").forEach((b) => b.onclick = async () => {
    await act("/api/actions/decide-approval", {
      approval_id: Number(b.dataset.apv), approve: b.dataset.ok === "1",
    });
    openDrawer(turnId);
  });
  if (anchor === "approvals") $("#dr-approvals").scrollIntoView();
}
function closeDrawer() {
  $("#drawer").hidden = true;
  $("#scrim").hidden = true;
}

boot();
