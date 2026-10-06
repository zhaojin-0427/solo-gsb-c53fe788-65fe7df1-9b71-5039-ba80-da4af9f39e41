/* DMX 灯具地址编排台 —— 原生 JavaScript 前端 */
"use strict";

const state = {
  catalog: [],
  fixtures: [],
  lines: [],
  reservations: [],
  version: null,
  latestPublishedPlanId: null,
  preview: null, // 当前展示的预览 payload（feasible 时）
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${res.status}`;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.detail = detail; // 结构化 detail（如回放整表拒收的逐项错误）供页面渲染
    throw err;
  }
  return data;
}

function toast(message, kind = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------- Tabs ----------
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`#tab-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "published") loadPublished();
  });
});

// ---------- 全局版本 ----------
async function refreshVersion() {
  const v = await api("/api/plans/version");
  state.version = v.config_version;
  state.latestPublishedPlanId = v.latest_published_plan_id;
  $("#version-badge").textContent = `配置版本 v${v.config_version}`;
  $("#published-badge").textContent = v.latest_published_plan_id
    ? `已发布 #${v.latest_published_plan_id}`
    : "未发布";
  $("#published-badge").classList.toggle("badge-muted", !v.latest_published_plan_id);
  refreshStaleBanner();
}

function refreshStaleBanner() {
  const banner = $("#stale-banner");
  if (state.preview && state.preview.config_version !== state.version) {
    banner.textContent =
      `配置已变化（当前 v${state.version}，预览基于 v${state.preview.config_version}），请重新生成预览；旧预览无法发布。`;
    banner.classList.remove("hidden");
    $("#btn-publish").disabled = true;
  } else {
    banner.classList.add("hidden");
  }
}

// ---------- 型录 ----------
async function loadCatalog() {
  state.catalog = await api("/api/catalog");
  const list = $("#catalog-list");
  if (!state.catalog.length) {
    list.innerHTML = '<p class="muted">尚未导入任何型号。</p>';
  } else {
    list.innerHTML = state.catalog
      .map(
        (t) => `
      <div class="type-card">
        <div class="title">${esc(t.manufacturer)} ${esc(t.name)}
          <button class="danger" data-del-type="${t.id}" style="float:right;font-size:12px">删除型号</button>
        </div>
        <div>${t.modes
          .map((m) => `<span class="mode-chip">${esc(m.name)} · ${m.channels}ch</span>`)
          .join("")}</div>
      </div>`
      )
      .join("");
  }
  fillFixtureModeSelect();
}

$("#catalog-list").addEventListener("click", async (e) => {
  const id = e.target.dataset.delType;
  if (id && confirm("确认删除该型号及其全部模式？被灯具实例引用时将被拒绝。")) {
    try {
      await api(`/api/catalog/${id}`, { method: "DELETE" });
      toast("已删除", "success");
      await Promise.all([loadCatalog(), refreshVersion(), loadFixtures()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

const SAMPLE_CSV = `manufacturer,name,mode,channels
ACME,Solar 360,Basic,9
ACME,Solar 360,Extended,14
ACME,Solar 360,Full,22
BrightCo,MegaPar 200,3CH,3
BrightCo,MegaPar 200,8CH,8
BrightCo,MegaPar 200,11CH,11
LitePro,Beam 150,Standard,16
LitePro,Beam 150,Macro,20
StrobeKing,Storm 1k,Basic,2
StrobeKing,Storm 1k,DMX Pro,5`;

$("#btn-load-sample").addEventListener("click", () => {
  $("#catalog-text").value = SAMPLE_CSV;
});

async function importCatalogBody(body, isForm) {
  const opts = isForm ? { method: "POST", body } : { method: "POST", body: JSON.stringify(body) };
  const result = await api("/api/catalog/import", opts);
  $("#catalog-msg").innerHTML = `<span class="muted">新增型号 ${result.created_types}，更新型号 ${result.updated_types}，新增模式 ${result.created_modes}，更新模式通道数 ${result.updated_modes}</span>`;
  toast("型录导入成功", "success");
  await Promise.all([loadCatalog(), refreshVersion()]);
}

$("#catalog-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    await importCatalogBody(form, true);
    e.target.value = "";
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#btn-import-text").addEventListener("click", async () => {
  const text = $("#catalog-text").value.trim();
  if (!text) return toast("请先粘贴 CSV/JSON 内容", "error");
  try {
    let rows;
    if (text.startsWith("[") || text.startsWith("{")) {
      const parsed = JSON.parse(text);
      rows = Array.isArray(parsed) ? parsed : [parsed];
      await importCatalogBody(rows, false);
    } else {
      const form = new FormData();
      form.append("file", new Blob([text], { type: "text/csv" }), "catalog.csv");
      await importCatalogBody(form, true);
    }
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------- 灯具实例 ----------
function fillFixtureModeSelect() {
  const sel = $("#fixture-mode-select");
  const current = sel.value;
  sel.innerHTML = state.catalog
    .flatMap((t) =>
      t.modes.map(
        (m) =>
          `<option value="${m.id}">${esc(t.manufacturer)} ${esc(t.name)} / ${esc(m.name)} (${m.channels}ch)</option>`
      )
    )
    .join("");
  if (current) sel.value = current;
}

async function loadFixtures() {
  state.fixtures = await api("/api/fixtures");
  $("#fixture-count").textContent = state.fixtures.length;
  $("#fixture-tbody").innerHTML = state.fixtures
    .map(
      (f) => `
    <tr>
      <td>${f.id}</td>
      <td>${esc(f.name)}</td>
      <td>${esc(f.type_name || "-")}</td>
      <td>${esc(f.mode_name || "-")}</td>
      <td>${f.channels}</td>
      <td>${f.previous_line ?? "—"}</td>
      <td>${f.previous_start ?? "—"}</td>
      <td>${f.locked ? "🔒" : ""}</td>
      <td>
        <button data-edit="${f.id}">编辑</button>
        <button class="danger" data-del="${f.id}">删除</button>
      </td>
    </tr>`
    )
    .join("");
}

$("#fixture-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    name: fd.get("name").trim(),
    mode_id: Number(fd.get("mode_id")),
    previous_line: fd.get("previous_line") ? Number(fd.get("previous_line")) : null,
    previous_start: fd.get("previous_start") ? Number(fd.get("previous_start")) : null,
    locked: fd.get("locked") === "on",
  };
  try {
    await api("/api/fixtures", { method: "POST", body: JSON.stringify(body) });
    toast("灯具已添加", "success");
    e.target.reset();
    await Promise.all([loadFixtures(), refreshVersion()]);
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#fixture-tbody").addEventListener("click", async (e) => {
  const delId = e.target.dataset.del;
  const editId = e.target.dataset.edit;
  if (delId && confirm("确认删除该灯具实例？")) {
    try {
      await api(`/api/fixtures/${delId}`, { method: "DELETE" });
      toast("已删除", "success");
      await Promise.all([loadFixtures(), refreshVersion()]);
    } catch (err) {
      toast(err.message, "error");
    }
  } else if (editId) {
    const f = state.fixtures.find((x) => x.id === Number(editId));
    const name = prompt("灯具名称", f.name);
    if (name === null) return;
    const line = prompt("既有线路 (1-4，留空表示无)", f.previous_line ?? "");
    if (line === null) return;
    const start = prompt("既有起始地址 (1-512，留空表示无)", f.previous_start ?? "");
    if (start === null) return;
    const locked = confirm("确定 = 锁定到既有线路与地址；取消 = 不锁定");
    try {
      await api(`/api/fixtures/${editId}`, {
        method: "PUT",
        body: JSON.stringify({
          name: name.trim() || f.name,
          previous_line: line.trim() ? Number(line) : null,
          previous_start: start.trim() ? Number(start) : null,
          locked,
        }),
      });
      toast("已更新", "success");
      await Promise.all([loadFixtures(), refreshVersion()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

// ---------- 线路与保留区 ----------
async function loadLines() {
  state.lines = await api("/api/lines");
  $("#line-tbody").innerHTML = state.lines
    .map(
      (l) => `
    <tr>
      <td>${l.id}</td>
      <td><input value="${esc(l.name)}" data-line-name="${l.id}" style="width:180px" /></td>
      <td>
        <label class="switch">
          <input type="checkbox" data-line-active="${l.id}" ${l.is_active ? "checked" : ""} />
          <span class="slider"></span>
        </label>
      </td>
      <td><button data-line-save="${l.id}">保存</button></td>
    </tr>`
    )
    .join("");
  $("#res-line").innerHTML = state.lines
    .map((l) => `<option value="${l.id}">${l.id} - ${esc(l.name)}</option>`)
    .join("");
}

$("#line-tbody").addEventListener("click", async (e) => {
  const id = e.target.dataset.lineSave;
  if (!id) return;
  const name = $(`[data-line-name="${id}"]`).value;
  const isActive = $(`[data-line-active="${id}"]`).checked;
  try {
    await api(`/api/lines/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name, is_active: isActive }),
    });
    toast("线路已更新", "success");
    await Promise.all([loadLines(), refreshVersion(), loadReservations()]);
  } catch (err) {
    toast(err.message, "error");
    await loadLines();
  }
});

async function loadReservations() {
  state.reservations = await api("/api/reservations");
  const lineName = Object.fromEntries(state.lines.map((l) => [l.id, l.name]));
  $("#reservation-tbody").innerHTML = state.reservations
    .map(
      (r) => `
    <tr>
      <td>${r.id}</td>
      <td>${r.line_id} - ${esc(lineName[r.line_id] || "")}</td>
      <td>${r.start}</td>
      <td>${r.end}</td>
      <td>${r.end - r.start + 1}</td>
      <td>${esc(r.reason)}</td>
      <td><button class="danger" data-del-res="${r.id}">删除</button></td>
    </tr>`
    )
    .join("");
}

$("#reservation-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    line_id: Number(fd.get("line_id")),
    start: Number(fd.get("start")),
    end: Number(fd.get("end")),
    reason: fd.get("reason") || "",
  };
  try {
    await api("/api/reservations", { method: "POST", body: JSON.stringify(body) });
    toast("保留区已添加", "success");
    e.target.reset();
    await Promise.all([loadReservations(), refreshVersion()]);
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#reservation-tbody").addEventListener("click", async (e) => {
  const id = e.target.dataset.delRes;
  if (id && confirm("确认删除该保留区？")) {
    try {
      await api(`/api/reservations/${id}`, { method: "DELETE" });
      toast("已删除", "success");
      await Promise.all([loadReservations(), refreshVersion()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

// ---------- 编排预览 ----------
$("#btn-preview").addEventListener("click", generatePreview);

async function generatePreview() {
  try {
    const result = await api("/api/plans/preview", { method: "POST" });
    // 只有逐级证明最优（optimal）的预览才可发布；未证最优结果不得进入发布流程。
    state.preview = result.feasible && result.optimality_status === "optimal" ? result : null;
    renderPreview(result);
    await refreshVersion();
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderPreview(p) {
  const conflictPanel = $("#conflict-panel");
  const unprovenPanel = $("#unproven-panel");
  const migrationPanel = $("#migration-panel");
  const allocPanel = $("#alloc-panel");

  if (p.optimality_status === "unproven") {
    // 未证最优：一律不可发布。按是否取得完整可用排布（feasible）分两种展示——
    // 有候选：展示候选排布仅供参考；空候选：暂无可展示排布，且不当作已证无解。
    const hasCandidate = p.feasible && Array.isArray(p.allocations) && p.allocations.length > 0;
    unprovenPanel.classList.remove("hidden");
    $("#unproven-msg").textContent = p.message || "未能在限定时间内证明最优。";
    $("#unproven-detail").textContent = hasCandidate
      ? "求解器在限定时间内只找到可行候选，未能逐级证明“迁移台数最少 → 启用线路数最少 → 字典序最小”。下列排布仅供参考，未生成可发布草稿；请减少灯具数量或稍后重新生成预览。"
      : "求解器在限定时间内未取得任何可展示的候选排布（暂无可展示排布）。这不是已证明无解：约束可能仍有可行排布，只是本次求解未能取得。未生成草稿，也不能发布；请减少灯具数量、放宽部分锁定或稍后重新生成预览。";
    conflictPanel.classList.add("hidden");
    $("#btn-publish").disabled = true;
    if (hasCandidate) {
      allocPanel.classList.remove("hidden");
      $("#plan-summary").textContent =
        `候选预览（未证最优，不可发布）· 基于配置 v${p.config_version}` +
        ` · 迁移 ${p.migration_count} 台 · 启用 ${p.used_lines} 条线路`;
      renderTracks(p.occupancy);
      renderMigrations(p.migrations);
      renderAllocations(p.allocations);
    } else {
      migrationPanel.classList.add("hidden");
      allocPanel.classList.add("hidden");
      $("#tracks").innerHTML = "";
      $("#plan-summary").textContent =
        `暂无可展示排布（未证最优，未生成草稿）· 基于配置 v${p.config_version}`;
    }
    return;
  }

  unprovenPanel.classList.add("hidden");

  if (!p.feasible) {
    conflictPanel.classList.remove("hidden");
    migrationPanel.classList.add("hidden");
    allocPanel.classList.add("hidden");
    $("#tracks").innerHTML = "";
    $("#btn-publish").disabled = true;
    $("#plan-summary").textContent = "无可行方案";
    $("#conflict-list").innerHTML = p.conflicts
      .map((c) => `<li><strong>[${esc(c.type)}]</strong> ${esc(c.message)}</li>`)
      .join("");
    return;
  }

  conflictPanel.classList.add("hidden");
  allocPanel.classList.remove("hidden");
  $("#plan-summary").textContent =
    `预览 #${p.plan_id} · 基于配置 v${p.config_version} · ✅ 已逐级证明最优` +
    ` · 迁移 ${p.migration_count} 台 · 启用 ${p.used_lines} 条线路`;

  renderTracks(p.occupancy);
  renderMigrations(p.migrations);
  renderAllocations(p.allocations);

  const fresh = p.config_version === state.version;
  $("#btn-publish").disabled = !fresh;
}

function renderTracks(occupancy) {
  const pct = (addr) => `calc(${(addr - 1) / 512 * 100}%)`;
  const widthPct = (a, b) => `calc(${(b - a + 1) / 512 * 100}%)`;
  $("#tracks").innerHTML = occupancy
    .map((line) => {
      const resBlocks = line.reservations
        .map(
          (r) =>
            `<div class="block reserved" style="left:${pct(r.start)};width:${widthPct(r.start, r.end)}"
              title="保留区 #${r.id}：${r.start}-${r.end} ${esc(r.reason)}">保 #${r.id}</div>`
        )
        .join("");
      const fxBlocks = line.fixtures
        .map((f) => {
          const cls = f.locked ? "locked" : f.migrated ? "migrated" : "fixture";
          return `<div class="block ${cls}" style="left:${pct(f.start)};width:${widthPct(f.start, f.end)}"
            title="#${f.fixture_id} ${esc(f.name)}：${f.start}-${f.end}（${f.channels}ch）${f.locked ? " 🔒锁定" : f.migrated ? " 迁移" : ""}">
            ${f.channels >= 14 ? esc(f.name) : f.fixture_id}</div>`;
        })
        .join("");
      return `
      <div class="track ${line.is_active ? "" : "disabled"}">
        <div class="track-head">
          <strong>线路 ${line.line_id} · ${esc(line.name)}</strong>${line.is_active ? "" : "（已停用）"}
          <span class="muted">已用 ${line.used_channels}/512，空闲 ${line.free_channels}</span>
        </div>
        <div class="track-bar">${line.is_active ? resBlocks + fxBlocks : ""}</div>
        <div class="track-scale"><span>1</span><span>128</span><span>256</span><span>384</span><span>512</span></div>
      </div>`;
    })
    .join("");
}

function addrOr(v) {
  return v ?? "—";
}

function renderMigrations(migrations) {
  const panel = $("#migration-panel");
  $("#migration-count").textContent = migrations.length;
  if (!migrations.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("#migration-tbody").innerHTML = migrations
    .map(
      (m) => `
    <tr>
      <td>${m.fixture_id}</td><td>${esc(m.name)}</td>
      <td>${addrOr(m.from_line)}</td><td>${addrOr(m.from_start)}</td>
      <td>➡️</td>
      <td>${m.to_line}</td><td>${m.to_start}</td>
      <td>${m.locked ? "🔒" : ""}</td>
    </tr>`
    )
    .join("");
}

function renderAllocations(allocations) {
  $("#alloc-tbody").innerHTML = allocations
    .map(
      (a) => `
    <tr>
      <td>${a.fixture_id}</td><td>${esc(a.name)}</td>
      <td>${a.line_id}</td><td>${a.start}</td><td>${a.end}</td><td>${a.channels}</td>
      <td>${a.locked ? "🔒 锁定" : a.migrated ? "🟠 迁移" : "✅ 原位"}</td>
    </tr>`
    )
    .join("");
}

$("#btn-publish").addEventListener("click", async () => {
  if (!state.preview) return;
  if (state.preview.optimality_status !== "optimal") {
    toast("该预览未证明最优，不能发布", "error");
    return;
  }
  if (state.preview.config_version !== state.version) {
    toast("配置版本已过期，请重新生成预览", "error");
    return;
  }
  if (!confirm("确认按当前预览原子发布？发布后旧预览立即失效。")) return;
  try {
    const result = await api(`/api/plans/${state.preview.plan_id}/publish`, { method: "POST" });
    toast(`已发布 #${result.plan_id}（配置 v${result.config_version}）`, "success");
    state.preview = null;
    $("#plan-summary").textContent = "发布完成，可在“已发布”页签下载 CSV";
    $("#btn-publish").disabled = true;
    await Promise.all([refreshVersion(), loadFixtures()]);
    loadPublished();
  } catch (err) {
    toast(err.message, "error");
    await refreshVersion();
  }
});

// ---------- 已发布 ----------
// 名称来源标注：无快照旧记录的名称是当前值（current），灯具已删除则不可用
// （unavailable）；快照记录无该字段，名称即发布时事实，不加标注。
function publishedNameSuffix(source) {
  if (source === "current") return ' <span class="muted">（当前名称）</span>';
  if (source === "unavailable") return ' <span class="muted">（名称不可用）</span>';
  return "";
}

async function loadPublished() {
  const data = await api("/api/plans/published/latest");
  const empty = $("#published-empty");
  const content = $("#published-content");
  if (!data.has_published) {
    empty.classList.remove("hidden");
    content.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");
  content.classList.remove("hidden");
  await loadVerifyPlans();
  // 已发布地址表来自发布时的不可变快照：发布后改名、切换锁定、调整保留区
  // 都不会改变此表与 CSV；仅旧库缺少快照的记录按字段来源裁决重建并提示——
  // 名称标注“当前名称”，无法还原的锁定状态显示“未知”而非“否”。
  const legacy = data.from_snapshot === false;
  const when = new Date(data.published_at || data.created_at).toLocaleString("zh-CN");
  $("#published-meta").textContent =
    `发布 #${data.plan_id} · 配置 v${data.config_version} · ${when} · 迁移 ${data.migration_count} 台 · 启用 ${data.used_lines} 条线路`;
  const notice = $("#published-notice");
  if (legacy) {
    notice.textContent =
      data.notice || "该发布记录缺少快照：地址与迁移标记为发布时事实，名称为当前值，锁定/迁移起点/保留区占用未知。";
    notice.classList.remove("hidden");
  } else {
    notice.classList.add("hidden");
  }
  $("#btn-download-csv").href = `/api/plans/published/${data.plan_id}/csv`;
  // 除地址表外，同步渲染迁移清单与各线路占用块，供逐项核对
  renderPublishedTracks(data.occupancy || []);
  renderPublishedMigrations(data.migrations || []);
  $("#published-tbody").innerHTML = data.allocations
    .map((a) => {
      const nameSuffix = publishedNameSuffix(a.name_source);
      // locked 为 null 表示历史锁定状态无法还原：显示“未知”，不得当作未锁定
      const lockedCell = a.locked === null || a.locked === undefined ? "未知" : a.locked ? "🔒" : "";
      return `
    <tr>
      <td>${a.fixture_id}</td><td>${esc(a.name)}${nameSuffix}</td>
      <td>${a.line_id}</td><td>${a.start}</td><td>${a.end}</td><td>${a.channels}</td>
      <td>${a.migrated ? "🟠 是" : "否"}</td><td>${lockedCell}</td>
    </tr>`;
    })
    .join("");
}

// 已发布占用采用独立渲染，不复用预览的 renderTracks：预览组件要求 reservations
// 必为数组且停用线路不画块；而无快照旧记录的占用中 reservations 与含保留区的
// 已用/空闲数为 null（未知）、名称与线路启停是当前现状。若复用，旧记录的 null
// 会被当成“无保留区/零通道”、现状停用会隐藏发布时灯具块——两者都会改写历史
// 占用。独立渲染让旧记录的 null 与现状启停只以“未知/当前”标注呈现，快照记录
// 则照常显示完整快照值；预览路径行为完全不变。
function renderPublishedTracks(occupancy) {
  const pct = (addr) => `calc(${(addr - 1) / 512 * 100}%)`;
  const widthPct = (a, b) => `calc(${(b - a + 1) / 512 * 100}%)`;
  $("#published-tracks").innerHTML = occupancy
    .map((line) => {
      // 保留区无法还原（null）时不画保留块、不计入已用：显示“未知”，不得当作无保留区
      const resUnknown = line.reservations === null || line.reservations === undefined;
      const resBlocks = resUnknown
        ? ""
        : line.reservations
            .map(
              (r) =>
                `<div class="block reserved" style="left:${pct(r.start)};width:${widthPct(r.start, r.end)}"
                  title="保留区 #${r.id}：${r.start}-${r.end} ${esc(r.reason)}">保 #${r.id}</div>`
            )
            .join("");
      // 灯具块是发布时事实：线路后来停用（或当时停用）也必须画出，不得隐藏
      const fxBlocks = (line.fixtures || [])
        .map((f) => {
          // 历史锁定未知（null）时按普通灯具着色并标注“锁定未知”，不得当作未锁定；
          // 迁移标记是发布时事实，与锁定状态各自独立标注
          const lockedUnknown = f.locked === null || f.locked === undefined;
          const cls = f.locked ? "locked" : f.migrated ? "migrated" : "fixture";
          const stateTip =
            (f.migrated ? " 迁移" : "") + (lockedUnknown ? " 锁定未知" : f.locked ? " 🔒锁定" : "");
          const nameTip =
            f.name_source === "current" ? "（当前名称）" : f.name_source === "unavailable" ? "（名称不可用）" : "";
          return `<div class="block ${cls}" style="left:${pct(f.start)};width:${widthPct(f.start, f.end)}"
            title="#${f.fixture_id} ${esc(f.name)}${nameTip}：${f.start}-${f.end}（${f.channels}ch）${stateTip}">
            ${f.channels >= 14 ? esc(f.name) : f.fixture_id}</div>`;
        })
        .join("");
      // 线路名称与启停为当前现状时标注来源；启停是现状，不影响发布时灯具块的展示
      const lineNameSuffix = line.name_source === "current" ? ' <span class="muted">（当前名称）</span>' : "";
      const activeLabel = line.is_active
        ? line.is_active_source === "current"
          ? ' <span class="muted">（当前启用）</span>'
          : ""
        : line.is_active_source === "current"
          ? ' <span class="muted">（当前已停用）</span>'
          : "（已停用）";
      // 含保留区的已用/空闲无法还原（null）时显示“未知”，不得当作零；
      // 另给出仅灯具占用的事实部分（fixtures_used_channels）
      const size = line.size ?? 512;
      let usage;
      if (line.used_channels === null || line.used_channels === undefined) {
        usage = `已用 未知/${size}，空闲 未知`;
        if (line.fixtures_used_channels !== null && line.fixtures_used_channels !== undefined) {
          usage += `（其中灯具占用 ${line.fixtures_used_channels} 通道，为发布时事实）`;
        }
      } else {
        usage = `已用 ${line.used_channels}/${size}，空闲 ${line.free_channels}`;
      }
      const resNote = resUnknown ? " · 保留区：未知（无法还原）" : "";
      return `
      <div class="track ${line.is_active ? "" : "disabled"}">
        <div class="track-head">
          <strong>线路 ${line.line_id} · ${esc(line.name)}</strong>${lineNameSuffix}${activeLabel}
          <span class="muted">${usage}${resNote}</span>
        </div>
        <div class="track-bar">${resBlocks + fxBlocks}</div>
        <div class="track-scale"><span>1</span><span>128</span><span>256</span><span>384</span><span>512</span></div>
      </div>`;
    })
    .join("");
}

function renderPublishedMigrations(migrations) {
  const panel = $("#published-migration-panel");
  $("#published-migration-count").textContent = migrations.length;
  if (!migrations.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("#published-migration-tbody").innerHTML = migrations
    .map((m) => {
      const nameSuffix = publishedNameSuffix(m.name_source);
      // 迁移落点（新线路/新地址）是留存事实；起点无法还原（null）时显示“未知”，
      // 不得当作无起点；历史锁定未知时同样显示“未知”，不得当作未锁定
      const fromLine = m.from_line === null || m.from_line === undefined ? "未知" : m.from_line;
      const fromStart = m.from_start === null || m.from_start === undefined ? "未知" : m.from_start;
      const lockedCell = m.locked === null || m.locked === undefined ? "未知" : m.locked ? "🔒" : "";
      return `
    <tr>
      <td>${m.fixture_id}</td><td>${esc(m.name)}${nameSuffix}</td>
      <td>${fromLine}</td><td>${fromStart}</td>
      <td>➡️</td>
      <td>${m.to_line}</td><td>${m.to_start}</td>
      <td>${lockedCell}</td>
    </tr>`;
    })
    .join("");
}

// ---------- 现场地址核验 ----------
// 已发布方案选择器：可选择任一已发布版本，核验以该版本发布时的落点为基准，
// 灯具后来改名、删除或再次发布都不影响核验结果。DMX 信号回放共用同一份列表。
async function loadVerifyPlans() {
  const plans = await api("/api/plans/published");
  const options = plans
    .map((p) => {
      const when = p.published_at ? new Date(p.published_at).toLocaleString("zh-CN") : "-";
      return `<option value="${p.plan_id}">发布 #${p.plan_id} · 配置 v${p.config_version} · ${when}</option>`;
    })
    .join("");
  $("#verify-plan-select").innerHTML = options;
  $("#replay-plan-select").innerHTML = options;
  // 换场施工单的两个版本选择器：默认“起点=上一版、目标=最新版”
  $("#changeover-from-select").innerHTML = options;
  $("#changeover-to-select").innerHTML = options;
  if (plans.length >= 2) {
    $("#changeover-from-select").value = plans[1].plan_id;
    $("#changeover-to-select").value = plans[0].plan_id;
  }
}

$("#btn-verify").addEventListener("click", async () => {
  const planId = $("#verify-plan-select").value;
  const file = $("#verify-file").files[0];
  if (!planId) return toast("请选择要核验的已发布方案", "error");
  if (!file) return toast("请选择现场 CSV 文件", "error");
  const form = new FormData();
  form.append("file", file);
  try {
    const report = await api(`/api/plans/${planId}/verify`, { method: "POST", body: form });
    renderVerifyReport(report);
  } catch (err) {
    $("#verify-result").innerHTML = "";
    toast(err.message, "error");
  }
});

const VERIFY_FIELD_LABELS = { line: "线路", start: "起始地址", channels: "通道数" };

// 渲染核验报告：整表结论横幅 + 逐项清单（隔离行/缺失/额外/不符/现场重叠）。
// 地址差异（不符清单）与跨行重叠（现场区间重叠）分开展示，互不混入。
function renderVerifyReport(r) {
  const box = $("#verify-result");
  const c = r.counts;
  let html = "";
  // 旧记录缺少快照时提示字段来源：名称/锁定/保留区不参与裁决
  if (r.notice) {
    html += `<div class="banner warn">${esc(r.notice)}</div>`;
  }
  const baselineNote = r.from_snapshot ? "发布快照" : "留存明细（无快照旧记录）";
  if (r.consistent) {
    html += `<div class="banner ok">✅ 整表一致：现场 ${c.checked} 台灯具与发布 #${r.plan_id}（基准：${baselineNote}）的落点完全一致。</div>`;
  } else {
    html += `<div class="banner err">❌ 整表不一致（基准：发布 #${r.plan_id}，${baselineNote}）——
      缺失 ${c.missing} 台 · 额外 ${c.extra} 台 · 线路/地址/通道不符 ${c.mismatched} 台 ·
      现场区间重叠 ${c.overlap_pairs} 处 · 隔离异常行 ${c.quarantined_rows} 行；
      已按 ID 对齐核对 ${c.checked} 台，其中 ${c.matched} 台一致。</div>`;
  }

  if (r.quarantined.length) {
    html += `
      <h3>⚠️ 隔离异常行 <span class="count">${r.quarantined.length}</span></h3>
      <p class="muted">以下行未参与对齐核对；只要存在隔离行，整表结论就不会是“一致”。</p>
      <table class="data">
        <thead><tr><th>行号</th><th>灯具ID</th><th>原始内容</th><th>隔离原因</th></tr></thead>
        <tbody>${r.quarantined
          .map(
            (q) => `<tr><td>${q.row}</td><td>${q.fixture_id ?? "—"}</td>
              <td><code>${esc(q.raw)}</code></td><td>${q.reasons.map(esc).join("；")}</td></tr>`
          )
          .join("")}</tbody>
      </table>`;
  }

  if (r.missing.length) {
    html += `
      <h3>📭 缺失（发布有、现场无）<span class="count">${r.missing.length}</span></h3>
      <table class="data">
        <thead><tr><th>灯具ID</th><th>名称</th><th>线路</th><th>起始</th><th>结束</th><th>通道数</th></tr></thead>
        <tbody>${r.missing
          .map(
            (m) => `<tr><td>${m.fixture_id}</td><td>${esc(m.name)}${publishedNameSuffix(m.name_source)}</td>
              <td>${m.line_id}</td><td>${m.start}</td><td>${m.end}</td><td>${m.channels}</td></tr>`
          )
          .join("")}</tbody>
      </table>`;
  }

  if (r.extra.length) {
    html += `
      <h3>➕ 额外（现场有、发布无）<span class="count">${r.extra.length}</span></h3>
      <table class="data">
        <thead><tr><th>行号</th><th>灯具ID</th><th>线路</th><th>起始</th><th>结束</th><th>通道数</th></tr></thead>
        <tbody>${r.extra
          .map(
            (x) => `<tr><td>${x.row}</td><td>${x.fixture_id}</td>
              <td>${x.line}</td><td>${x.start}</td><td>${x.end}</td><td>${x.channels}</td></tr>`
          )
          .join("")}</tbody>
      </table>`;
  }

  if (r.mismatches.length) {
    html += `
      <h3>🔀 线路/地址/通道不符 <span class="count">${r.mismatches.length}</span></h3>
      <table class="data">
        <thead><tr><th>灯具ID</th><th>名称</th><th>现场行号</th><th>差异项</th><th>发布落点</th><th>现场值</th></tr></thead>
        <tbody>${r.mismatches
          .flatMap((m) =>
            m.diffs.map(
              (d) => `<tr><td>${m.fixture_id}</td><td>${esc(m.name)}${publishedNameSuffix(m.name_source)}</td>
                <td>${m.row}</td><td>${VERIFY_FIELD_LABELS[d.field]}</td>
                <td>${d.expected}</td><td class="diff-bad">${d.actual}</td></tr>`
            )
          )
          .join("")}</tbody>
      </table>`;
  }

  if (r.overlaps.length) {
    html += `
      <h3>🧨 现场区间重叠 <span class="count">${r.overlaps.length}</span></h3>
      <p class="muted">同一线路上两台灯具的通道区间（闭区间）相交，与上方的地址差异无关，需现场排查。</p>
      <table class="data">
        <thead><tr><th>线路</th><th>灯具 A</th><th>区间 A</th><th>灯具 B</th><th>区间 B</th><th>重叠区间</th></tr></thead>
        <tbody>${r.overlaps
          .map(
            (o) => `<tr><td>${o.line}</td>
              <td>#${o.a.fixture_id}（行 ${o.a.row}）</td><td>${o.a.start}-${o.a.end}</td>
              <td>#${o.b.fixture_id}（行 ${o.b.row}）</td><td>${o.b.start}-${o.b.end}</td>
              <td class="diff-bad">${o.overlap_start}-${o.overlap_end}</td></tr>`
          )
          .join("")}</tbody>
      </table>`;
  }

  box.innerHTML = html;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- DMX 信号回放 ----------
// 回放状态：报告 + 槽位历史（key "线路:通道" -> 逐帧写入事件）+ 当前帧游标。
// 帧内查询用二分定位“截至当前帧的最后一次写入”，未写入即初值 0——
// 与服务端“槽位初值 0、未更新延续上一帧”的重建规则一致。
let replayState = null;

$("#btn-replay").addEventListener("click", async () => {
  const planId = $("#replay-plan-select").value;
  const file = $("#replay-file").files[0];
  if (!planId) return toast("请选择要回放的已发布方案", "error");
  if (!file) return toast("请选择信号 CSV 文件", "error");
  const form = new FormData();
  form.append("file", file);
  form.append("conflict_policy", $("#replay-policy").value);
  try {
    const report = await api(`/api/plans/${planId}/replay`, { method: "POST", body: form });
    renderReplayReport(report);
  } catch (err) {
    renderReplayRejection(err);
  }
});

function replayOwnerLabel(u) {
  if (u.owner === "fixture") {
    const suffix = u.fixture_name_source === "unavailable"
      ? "（名称不可用）" : u.fixture_name_source === "current" ? "（当前名称）" : "";
    return `灯具 #${u.fixture_id} ${esc(u.fixture_name)}${suffix} · 相对通道 ${u.relative_channel}`;
  }
  if (u.owner === "reservation") {
    return `保留区 #${u.reservation_id}（${u.reservation_start}-${u.reservation_end}）`;
  }
  if (u.owner === "unassigned") return "未分配通道";
  return "归属未知（旧记录无快照）";
}

function buildReplayState(r) {
  const histories = new Map();
  r.frames.forEach((fr, fi) => {
    for (const u of fr.updates) {
      const key = `${u.line_id}:${u.channel}`;
      let h = histories.get(key);
      if (!h) {
        h = {
          key,
          line_id: u.line_id,
          channel: u.channel,
          owner: u.owner,
          ownerUpdate: u,
          events: [],
        };
        histories.set(key, h);
      }
      h.events.push({ fi, value: u.value, row: u.row });
    }
  });
  return { report: r, histories, frameIndex: 0 };
}

// 截至第 fi 帧（含）该槽位的取值：无写入则为初值 0；同帧多事件取最后一次
function replayValueAt(history, fi) {
  let lo = 0;
  let hi = history.events.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (history.events[mid].fi <= fi) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found < 0 ? 0 : history.events[found].value;
}

function renderReplayReport(r) {
  replayState = buildReplayState(r);
  replayState.frameIndex = 0;
  const c = r.counts;
  let html = "";

  if (r.notice) html += `<div class="banner warn">${esc(r.notice)}</div>`;
  const baselineNote = r.from_snapshot
    ? "发布快照（灯具闭区间与保留区均为发布时事实）"
    : "留存明细（无快照旧记录：灯具落点为发布时事实，灯具外归属未知）";
  const policyLabel = r.conflict_policy === "last" ? "同槽不同值按文件顺序取末值" : "同槽不同值整表拒收";
  html += `
    <div class="banner ok">
      回放完成（基准：${baselineNote}）：共 <strong>${c.frames}</strong> 帧 /
      <strong>${c.data_rows}</strong> 行写入，时间 ${c.time_start}–${c.time_end} ms
      （时长 ${c.duration_ms} ms）；策略：${policyLabel}。
      灯具槽写入 ${c.fixture_updates}（其中改变读数 ${c.fixture_changed_updates}），
      保留区 ${c.reservation_updates}、未分配 ${c.unassigned_updates}、归属未知 ${c.unknown_updates}，
      灯具外非零写入 ${c.external_nonzero_writes}。
    </div>`;

  if (r.conflicts && r.conflicts.length) {
    html += `
      <div class="banner warn">
        ⚔️ 检测到 <strong>${r.conflicts.length}</strong> 处“同一毫秒同一槽位不同值”，
        已按文件顺序取<strong>末值</strong>，采用结果标注如下（行顺序会影响读数，请勿随意重排）：
      </div>
      <table class="data">
        <thead><tr><th>时间(ms)</th><th>线路</th><th>通道</th><th>各行取值（按文件顺序）</th><th>已采用</th></tr></thead>
        <tbody>${r.conflicts
          .map(
            (cf) => `<tr><td>${cf.time_ms}</td><td>${cf.line_id}</td><td>${cf.channel}</td>
              <td>${cf.rows.map((x) => `行 ${x.row} → ${x.value}`).map(esc).join("；")}</td>
              <td class="diff-bad">行 ${cf.chosen_row} → ${cf.chosen_value}</td></tr>`
          )
          .join("")}</tbody>
      </table>`;
  }

  // 帧浏览器只渲染一次：滑块在逐帧重绘时不被替换，拖动过程中可以连续回放
  html += `
    <div class="replay-nav">
      <button data-replay-jump="first">⏮ 首帧</button>
      <button data-replay-jump="prev">◀ 上一帧</button>
      <input type="range" id="replay-frame-slider" min="0" max="${r.frames.length - 1}"
        value="0" step="1" style="flex:1" />
      <button data-replay-jump="next">下一帧 ▶</button>
      <button data-replay-jump="last">末帧 ⏭</button>
      <strong id="replay-frame-label"></strong>
    </div>
    <div id="replay-frame-view"></div>`;

  $("#replay-result").innerHTML = html;
  drawReplayFrame();
  $("#replay-result").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderReplayRejection(err) {
  const box = $("#replay-result");
  let html = "";
  const detail = err && err.detail;
  if (detail && typeof detail === "object" && detail.message) {
    html += `<div class="banner err"><strong>整表拒收：</strong>${esc(detail.message)}</div>`;
    if (detail.code === "slot_conflicts") {
      html += `
        <h3>⚔️ 同毫秒同槽不同值</h3>
        <table class="data">
          <thead><tr><th>时间(ms)</th><th>线路</th><th>通道</th><th>各行取值（按文件顺序）</th><th>末值策略将采用</th></tr></thead>
          <tbody>${(detail.errors || [])
            .map(
              (c) => `<tr><td>${c.time_ms}</td><td>${c.line_id}</td><td>${c.channel}</td>
                <td>${c.rows.map((x) => `行 ${x.row} → ${x.value}`).map(esc).join("；")}</td>
                <td class="muted">行 ${c.chosen_row} → ${c.chosen_value}</td></tr>`
            )
            .join("")}</tbody>
        </table>`;
    } else {
      html += `
        <h3>⚠️ 非法行</h3>
        <table class="data">
          <thead><tr><th>行号</th><th>原始内容</th><th>原因</th></tr></thead>
          <tbody>${(detail.errors || [])
            .map(
              (e) => `<tr><td>${e.row}</td><td><code>${esc(e.raw ?? "")}</code></td>
                <td>${(e.reasons || []).map(esc).join("；")}</td></tr>`
            )
            .join("")}</tbody>
        </table>`;
    }
  } else {
    html = `<div class="banner err"><strong>回放被拒绝：</strong>${esc((err && err.message) || "请求失败")}</div>`;
  }
  box.innerHTML = html;
  toast("回放被拒绝", "error");
}

function drawReplayFrame() {
  const s = replayState;
  if (!s) return;
  const r = s.report;
  const fi = s.frameIndex;
  const frame = r.frames[fi];

  // 滑块与帧标签原地更新（不重建滑块 DOM，保持拖动连续性）
  const slider = $("#replay-frame-slider");
  if (slider && Number(slider.value) !== fi) slider.value = fi;
  const label = $("#replay-frame-label");
  if (label) {
    label.textContent = `第 ${fi + 1}/${r.frames.length} 帧 · t = ${frame.time_ms} ms · 本帧 ${frame.updates.length} 行写入`;
  }

  let html = "";

  // 本帧逐行写入（文件顺序，即末值策略的裁决顺序）
  html += `
    <h3>本帧写入（按文件顺序）</h3>
    <table class="data">
      <thead><tr><th>行号</th><th>线路</th><th>通道</th><th>归属</th><th>值</th></tr></thead>
      <tbody>${frame.updates
        .map((u) => {
          const valueCell = u.changed
            ? `<span class="diff-bad">${u.previous_value} → ${u.value}</span>`
            : `${u.value}（无变化）`;
          return `<tr${u.value === 0 ? ' class="zero-row"' : ""}>
            <td>${u.row}</td><td>${u.line_id}</td><td>${u.channel}</td>
            <td>${replayOwnerLabel(u)}</td><td>${valueCell}</td></tr>`;
        })
        .join("")}</tbody>
    </table>`;

  // 本帧各灯具相对通道读数（只列非零通道；本帧改变的通道高亮标注）
  const changedFixtureChannels = new Map(); // key "灯具ID:相对通道" -> 写入事件
  for (const u of frame.updates) {
    if (u.owner === "fixture") changedFixtureChannels.set(`${u.fixture_id}:${u.relative_channel}`, u);
  }
  html += `<h3>灯具相对通道读数（t = ${frame.time_ms} ms）</h3><div class="replay-fixtures">`;
  for (const fx of r.final_fixtures) {
    const chips = [];
    for (let rel = 1; rel <= fx.channels; rel++) {
      const h = s.histories.get(`${fx.line_id}:${fx.start + rel - 1}`);
      const v = replayValueAt(h || { events: [] }, fi);
      if (v === 0) continue;
      const ch = changedFixtureChannels.get(`${fx.fixture_id}:${rel}`);
      const change = ch
        ? ` <em class="chg">${ch.previous_value}→${ch.value}</em>`
        : "";
      chips.push(
        `<span class="dmx-chip${ch ? " changed" : ""}">ch${rel}: <strong>${v}</strong>${change}</span>`
      );
    }
    const nameSuffix = publishedNameSuffix(fx.name_source);
    html += `
      <div class="replay-fixture">
        <div class="title">#${fx.fixture_id} ${esc(fx.name)}${nameSuffix}
          <span class="muted">线路 ${fx.line_id} · 地址 ${fx.start}-${fx.end} · ${fx.channels} 通道</span>
        </div>
        <div>${chips.length ? chips.join(" ") : '<span class="muted">全部为 0（黑场）</span>'}</div>
      </div>`;
  }
  html += "</div>";

  // 灯具外槽位：当前帧所有非零值（含上一帧延续），按归属分组。
  // 无快照时归属一律 unknown——绝不按当前保留区判为空位异常。
  const groups = { reservation: [], unassigned: [], unknown: [] };
  for (const h of s.histories.values()) {
    if (h.owner === "fixture") continue;
    const v = replayValueAt(h, fi);
    if (v === 0) continue;
    const nowEvent = h.events[h.events.length - 1];
    const writtenNow = nowEvent && nowEvent.fi === fi;
    groups[h.owner].push({ h, v, writtenNow });
  }
  const sortSlot = (a, b) =>
    a.h.line_id - b.h.line_id || a.h.channel - b.h.channel;
  groups.reservation.sort(sortSlot);
  groups.unassigned.sort(sortSlot);
  groups.unknown.sort(sortSlot);

  const slotRows = (items) =>
    items
      .map(({ h, v, writtenNow }) => {
        const u = h.ownerUpdate;
        const where = h.owner === "reservation"
          ? `保留区 #${u.reservation_id}（${u.reservation_start}-${u.reservation_end}）`
          : h.owner === "unassigned"
            ? "未分配"
            : "归属未知";
        return `<tr${writtenNow ? ' class="written-now"' : ""}>
          <td>${h.line_id}</td><td>${h.channel}</td><td>${where}</td>
          <td><strong>${v}</strong>${writtenNow ? ' <span class="muted">（本帧写入）</span>' : ' <span class="muted">（延续）</span>'}</td></tr>`;
      })
      .join("");

  if (r.baseline.reservations_known) {
    html += `
      <h3>保留区非零值 <span class="count">${groups.reservation.length}</span></h3>
      ${groups.reservation.length
        ? `<table class="data"><thead><tr><th>线路</th><th>通道</th><th>归属</th><th>当前值</th></tr></thead>
           <tbody>${slotRows(groups.reservation)}</tbody></table>`
        : '<p class="muted">当前帧保留区通道全部为 0。</p>'}
      <h3>未分配通道非零值 <span class="count">${groups.unassigned.length}</span></h3>
      ${groups.unassigned.length
        ? `<table class="data"><thead><tr><th>线路</th><th>通道</th><th>归属</th><th>当前值</th></tr></thead>
           <tbody>${slotRows(groups.unassigned)}</tbody></table>`
        : '<p class="muted">当前帧未分配通道全部为 0。</p>'}`;
  } else {
    html += `
      <h3>灯具外非零值（归属未知） <span class="count">${groups.unknown.length}</span></h3>
      <p class="muted">
        该发布记录无快照，保留区占用无法还原：以下灯具外非零值继续显示，
        但归属未知——不会用<strong>当前</strong>保留区把它们判成空位异常。
      </p>
      ${groups.unknown.length
        ? `<table class="data"><thead><tr><th>线路</th><th>通道</th><th>归属</th><th>当前值</th></tr></thead>
           <tbody>${slotRows(groups.unknown)}</tbody></table>`
        : '<p class="muted">当前帧灯具外通道全部为 0。</p>'}`;
  }

  $("#replay-frame-view").innerHTML = html;
}

// 帧浏览器交互（事件委托：按钮跳转、滑块拖动）
$("#replay-result").addEventListener("click", (e) => {
  const jump = e.target.dataset && e.target.dataset.replayJump;
  if (!jump || !replayState) return;
  const last = replayState.report.frames.length - 1;
  const cur = replayState.frameIndex;
  replayState.frameIndex =
    jump === "first" ? 0
    : jump === "last" ? last
    : jump === "prev" ? Math.max(0, cur - 1)
    : Math.min(last, cur + 1);
  drawReplayFrame();
});
$("#replay-result").addEventListener("input", (e) => {
  if (!e.target.id || e.target.id !== "replay-frame-slider" || !replayState) return;
  replayState.frameIndex = Number(e.target.value);
  drawReplayFrame();
});

// ---------- 双版本换场施工单 ----------
// 只读：起点与目标均取自各自发布快照；服务端逐步模拟后返回有序步骤或拒绝原因。
$("#btn-changeover").addEventListener("click", async () => {
  const fromId = $("#changeover-from-select").value;
  const toId = $("#changeover-to-select").value;
  if (!fromId || !toId) return toast("请选择两份已发布方案", "error");
  if (fromId === toId) return toast("请选择两份不同的已发布方案", "error");
  const box = $("#changeover-result");
  box.innerHTML = '<p class="muted">正在从两份发布快照还原并逐步模拟……</p>';
  try {
    const report = await api(`/api/plans/changeover?from=${fromId}&to=${toId}`);
    renderChangeover(report);
  } catch (err) {
    renderChangeoverRejection(err);
  }
});

// 选择起点/目标后即时给出“同一份方案”的前端提示，避免无谓请求
$("#changeover-from-select").addEventListener("change", warnSameChangeoverPlans);
$("#changeover-to-select").addEventListener("change", warnSameChangeoverPlans);
function warnSameChangeoverPlans() {
  const btn = $("#btn-changeover");
  const same =
    $("#changeover-from-select").value &&
    $("#changeover-from-select").value === $("#changeover-to-select").value;
  btn.disabled = same;
  btn.title = same ? "起点与目标必须是两份不同的已发布方案" : "";
}

const CHANGEOVER_ACTION_LABELS = {
  remove: "🧹 拆除",
  relocate: "🚚 迁址",
  temp_relocate: "⏳ 临时周转",
  channel_adjust: "🔧 通道数调整",
  install: "🆕 接入",
};

function fmtIval(iv) {
  if (!iv) return "—";
  return `线路 ${iv.line_id} · ${iv.start}-${iv.end}（${iv.channels}ch）`;
}

function renderChangeover(r) {
  const box = $("#changeover-result");
  if (!r.feasible) {
    let html = `
      <div class="banner err">
        <strong>无法生成安全施工单：</strong>${esc(r.message || "找不到安全周转位。")}
      </div>
      <p class="muted">下列灯具的目标位被占用，且启用线路上没有足够的“空闲且不挡住新版落点”的连续区间可作周转：</p>`;
    html += `<table class="data">
      <thead><tr><th>需周转灯具</th><th>目标区间</th><th>占用目标位的灯具</th><th>各启用线路最大可用连续区间</th></tr></thead>
      <tbody>${(r.blocking || [])
        .map((b) => `<tr>
          <td>#${b.fixture_id}</td>
          <td>${fmtIval(b.target)}</td>
          <td>${b.blocking_fixture_ids.length ? b.blocking_fixture_ids.map((x) => `#${x}`).join("、") : "—"}</td>
          <td>${(b.free_runs || [])
            .map((f) => `线路 ${f.line_id}：最大 ${f.largest_neutral_free_channels}ch（需要 ${f.needed_channels}ch）`)
            .join("<br>")}</td>
        </tr>`)
        .join("")}</tbody></table>`;
    box.innerHTML = html;
    return;
  }

  const c = r.counts;
  const steps = r.steps || [];
  let html = `
    <div class="banner ok">
      换场施工单（发布 #${r.from_plan_id} ➡️ 发布 #${r.to_id}）：共 <strong>${c.total_steps}</strong> 步，
      其中临时周转 <strong>${c.temporary_steps}</strong> 步。
      拆除 ${c.remove} 台 · 迁址 ${c.relocate} 台 · 通道数调整 ${c.channel_adjust} 台
      （缩短 ${c.channel_adjust_shrink} / 加长 ${c.channel_adjust_grow}）· 接入 ${c.install} 台。
      每一步均经逐步模拟：中间态不重叠、不压保留区，末态等于发布 #${r.to_plan_id}。
    </div>
    <table class="data">
      <thead><tr><th>#</th><th>操作</th><th>灯具</th><th>释放区间</th><th>占用区间</th><th>说明</th></tr></thead>
      <tbody>${steps
        .map((s) => {
          const label = CHANGEOVER_ACTION_LABELS[s.action] || s.action;
          const rel = s.release.length
            ? s.release.map((x) => fmtIval(x)).join("<br>")
            : "—";
          const chNote =
            s.action === "channel_adjust"
              ? `${s.channels_before}ch ➡️ ${s.channels_after}ch${s.note ? "；" + esc(s.note) : ""}`
              : esc(s.note || "");
          return `<tr${s.temporary ? ' class="temp-step"' : ""}>
            <td>${s.seq}${s.temporary ? ' <span class="count">临时</span>' : ""}</td>
            <td>${label}</td>
            <td>#${s.fixture_id} ${esc(s.fixture_name)}</td>
            <td>${rel}</td>
            <td>${fmtIval(s.occupy)}</td>
            <td>${chNote}</td>
          </tr>`;
        })
        .join("")}</tbody>
    </table>`;
  box.innerHTML = html;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderChangeoverRejection(err) {
  const box = $("#changeover-result");
  let html = "";
  const detail = err && err.detail;
  if (detail && typeof detail === "object" && detail.message) {
    html += `<div class="banner err"><strong>不能生成换场施工单：</strong>${esc(detail.message)}</div>`;
    const reasonLabels = {
      missing_snapshot: "方案缺少发布快照",
      snapshot_inconsistent: "快照数据不一致",
      lines_mismatch: "线路启停两版不一致",
      reservations_mismatch: "保留区两版不一致",
      simulation_failed: "施工模拟自检失败",
    };
    for (const reason of detail.reasons || []) {
      if (reason.type === "line_active_mismatch") {
        html += `
          <h3>线路启停差异</h3>
          <table class="data"><thead><tr><th>线路</th><th>旧版</th><th>新版</th></tr></thead>
          <tbody>${reason.lines
            .map(
              (l) => `<tr><td>线路 ${l.line_id}</td>
                <td>${l.from_a_active ? "启用" : "停用"}</td>
                <td class="diff-bad">${l.to_b_active ? "启用" : "停用"}</td></tr>`
            )
            .join("")}</tbody></table>`;
      } else if (reason.type === "reservation_mismatch") {
        html += reason.lines
          .map(
            (l) => `
          <h3>线路 ${l.line_id} 的保留区差异</h3>
          <table class="data">
            <thead><tr><th>旧版（起点）保留区</th><th>新版（目标）保留区</th></tr></thead>
            <tbody><tr>
              <td>${l.from_a.length ? l.from_a.map((r) => `${r.start}-${r.end}`).join("、") : "（无）"}</td>
              <td class="diff-bad">${l.to_b.length ? l.to_b.map((r) => `${r.start}-${r.end}`).join("、") : "（无）"}</td>
            </tr></tbody></table>`
          )
          .join("");
      } else if (reason.reason === "missing_snapshot") {
        html += `<p class="muted">方案 #${reason.plan_id} 缺少发布快照（可能是旧库遗留记录），保留区与发布时线路启停无法还原。</p>`;
      } else {
        html += `<p class="muted">${esc(reasonLabels[detail.code] || detail.code)}</p>`;
      }
    }
  } else {
    html = `<div class="banner err"><strong>请求失败：</strong>${esc((err && err.message) || "未知错误")}</div>`;
  }
  box.innerHTML = html;
  toast("无法生成施工单", "error");
}

// ---------- 启动 ----------
(async function init() {
  try {
    await Promise.all([loadCatalog(), loadFixtures(), loadLines(), refreshVersion()]);
    await loadReservations();
  } catch (err) {
    toast("初始化失败：" + err.message, "error");
  }
})();
