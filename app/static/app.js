const $ = (sel) => document.querySelector(sel);

function badge(status) {
  const label = {
    current: "现行",
    stale: "待重算",
    uncomputed: "未计算",
    pending: "排队中",
    running: "运行中",
    done: "完成",
    failed: "失败",
    complete: "核齐",
    mismatch: "不一致",
    no_manifest: "无清单",
  }[status] || status;
  return `<span class="badge ${status}">${label}</span>`;
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderDay(data) {
  const panel = $("#day-panel");
  panel.hidden = false;
  const head = `
    <div class="panel-head">
      <h3>${esc(data.date)} · UTC 服务日</h3>
      ${badge(data.status)}
      ${data.computed_at ? `<span class="mono small">计算于 ${esc(data.computed_at)}</span>` : ""}
    </div>`;

  if (data.status !== "current") {
    const msg = data.status === "stale"
      ? `结果已过期，不能用于排班。${data.last_error ? "原因：" + esc(data.last_error) : ""} 请在下方触发重算。`
      : "该日尚未计算完成。";
    panel.innerHTML = head + `<div class="notice ${data.status}">${msg}</div>` +
      `<button class="recompute" data-date="${esc(data.date)}">重算 ${esc(data.date)}</button>`;
    bindRecompute();
    return;
  }

  const pairs = data.station_pairs.map((row) => `
    <tr><td>${esc(row.from_station)} → ${esc(row.to_station)}</td>
        <td class="num">${row.trips}</td></tr>`).join("");
  const pending = data.pending.map((row) => `
    <tr><td class="mono">${esc(row.card_id)}</td>
        <td class="mono">${esc(row.tap_time)}</td>
        <td>${esc(row.station)}</td>
        <td>${row.direction === "in" ? "进站" : "出站"}</td>
        <td class="small">${esc(row.reason)}</td></tr>`).join("");

  panel.innerHTML = head + `
    <div class="stat-row">
      <div class="stat"><div class="k">配对行程</div><div class="v">${data.trip_total}</div></div>
      <div class="stat"><div class="k">待核刷卡</div><div class="v">${data.pending_total}</div></div>
      <div class="stat"><div class="k">站间流向</div><div class="v">${data.station_pairs.length}</div></div>
    </div>
    <div class="cols">
      <div>
        <h3>站间人数（按进站日）</h3>
        <table class="grid">
          <thead><tr><th>区间</th><th class="num">人数</th></tr></thead>
          <tbody>${pairs || '<tr><td colspan="2" class="empty">当日无配对行程</td></tr>'}</tbody>
        </table>
      </div>
      <div>
        <h3>待核记录（不计入客流）</h3>
        <table class="grid">
          <thead><tr><th>卡号</th><th>时刻 (UTC)</th><th>站点</th><th>方向</th><th>原因</th></tr></thead>
          <tbody>${pending || '<tr><td colspan="5" class="empty">当日无待核记录</td></tr>'}</tbody>
        </table>
      </div>
    </div>`;
  bindRecompute();
}

async function loadDay(date) {
  document.querySelectorAll(".day-tab").forEach((el) => {
    el.classList.toggle("active", el.dataset.date === date);
  });
  const res = await fetch(`/api/days/${date}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    $("#day-panel").hidden = false;
    $("#day-panel").innerHTML = `<div class="notice stale">${esc(body.detail || "加载失败")}</div>`;
    return;
  }
  renderDay(await res.json());
}

document.querySelectorAll(".day-tab").forEach((el) => {
  el.addEventListener("click", () => loadDay(el.dataset.date));
});

function bindRecompute() {
  document.querySelectorAll(".recompute").forEach((el) => {
    el.addEventListener("click", async () => {
      el.disabled = true;
      const res = await fetch(`/api/recompute?days=${encodeURIComponent(el.dataset.date)}`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) alert(body.detail || "重算失败");
      await refreshTabs();
      await loadDay(el.dataset.date);
    });
  });
}

async function refreshTabs() {
  const res = await fetch("/api/days");
  if (!res.ok) return;
  const { days } = await res.json();
  const tabs = $("#day-tabs");
  const current = document.querySelector(".day-tab.active")?.dataset.date;
  tabs.innerHTML = days.length
    ? days.map((d) => `
      <button class="day-tab ${d.status === "stale" ? "stale" : ""} ${d.date === current ? "active" : ""}" data-date="${esc(d.date)}">
        <span class="date">${esc(d.date)}</span>
        <span class="badge ${d.status}">${d.status === "current" ? "现行" : "待重算"}</span>
      </button>`).join("")
    : '<p class="empty">尚无数据，请先上传。</p>';
  tabs.querySelectorAll(".day-tab").forEach((el) => {
    el.addEventListener("click", () => loadDay(el.dataset.date));
  });
}

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fileInput = $("#csv");
  const result = $("#upload-result");
  const btn = $("#upload-btn");
  if (!fileInput.files.length) return;
  const form = new FormData();
  form.append("file", fileInput.files[0]);
  btn.disabled = true;
  result.hidden = false;
  result.className = "result";
  result.textContent = "上传并重算中…";
  try {
    const res = await fetch("/api/uploads", { method: "POST", body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      result.className = "result error";
      result.textContent = body.detail || `上传失败（HTTP ${res.status}）`;
      return;
    }
    const states = Object.entries(body.day_states || {})
      .map(([day, status]) => `${day}: ${status === "current" ? "已转为现行" : "待重算"}`).join("\n");
    result.textContent =
      `批次 #${body.batch_id} 已受理：${body.rows} 行，新增 ${body.inserted}，忽略 ${body.ignored}。\n` +
      `涉及日期：${(body.affected_days || []).join(", ") || "无"}\n${states}`;
    await refreshTabs();
    if (body.affected_days?.length) await loadDay(body.affected_days[0]);
  } catch (err) {
    result.className = "result error";
    result.textContent = `网络错误：${err}`;
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// manifest reconciliation
// ---------------------------------------------------------------------------

const RECON_KIND_LABEL = {
  missing: "缺失",
  revision: "修订不符",
  unexpected: "清单外",
};

function renderReconSummary(summary) {
  const box = $("#recon-summary");
  if (!box) return;
  const items = [
    ["站点日", summary.total],
    ["核齐", summary.complete],
    ["不一致", summary.mismatch],
    ["待核", summary.pending],
    ["核收失败", summary.failed],
    ["无清单", summary.no_manifest],
  ];
  box.innerHTML = items.map(([k, v]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${v ?? 0}</div></div>`).join("");
}

async function refreshRecon() {
  const res = await fetch("/api/recon");
  if (!res.ok) return;
  const body = await res.json();
  renderReconSummary(body.summary);
  const tbody = $("#recon-body");
  if (!tbody) return;
  tbody.innerHTML = body.reconciliation.length
    ? body.reconciliation.map((r) => `
      <tr class="recon-row" data-station="${esc(r.station)}" data-date="${esc(r.date)}">
        <td>${esc(r.station)}</td>
        <td class="mono">${esc(r.date)}</td>
        <td>${badge(r.status)}</td>
        <td class="small">${esc(r.manifest_filename || "—")}${r.manifest_rows != null ? `（${r.manifest_rows} 行）` : ""}</td>
        <td class="num">${r.missing ?? "—"}</td>
        <td class="num">${r.revision_mismatch ?? "—"}</td>
        <td class="num">${r.unexpected ?? "—"}</td>
        <td class="mono small">${esc(r.checked_at || r.last_error || "")}</td>
        <td><button class="ghost-btn recon-open" data-station="${esc(r.station)}" data-date="${esc(r.date)}">明细</button></td>
      </tr>`).join("")
    : '<tr><td colspan="9" class="empty">尚无清单或刷卡数据</td></tr>';
  bindReconOpen();
}

function renderReconDetail(data) {
  const panel = $("#recon-panel");
  if (!panel) return;
  panel.hidden = false;
  const manifest = data.manifest;
  const head = `
    <div class="panel-head">
      <h3>${esc(data.station)} · ${esc(data.date)} · UTC</h3>
      ${badge(data.status)}
      ${data.checked_at ? `<span class="mono small">核收于 ${esc(data.checked_at)}</span>` : ""}
    </div>`;

  let notice = "";
  if (data.status === "no_manifest") {
    notice = `<div class="notice stale">该站点日没有清单，不能判为核齐。请先上传完整清单。</div>`;
  } else if (data.status === "pending") {
    notice = `<div class="notice stale">待核：旧核收结论已撤回，结果尚未产生。${data.last_error ? "原因：" + esc(data.last_error) : ""}</div>`;
  } else if (data.status === "failed") {
    notice = `<div class="notice stale">核收失败，不能判为核齐。${data.last_error ? "原因：" + esc(data.last_error) : ""}</div>`;
  } else if (data.status === "mismatch") {
    notice = `<div class="notice stale">清单与已受理刷卡不一致：缺失 ${data.counts.missing}，修订不符 ${data.counts.revision}，清单外 ${data.counts.unexpected}。</div>`;
  } else if (data.status === "complete") {
    notice = `<div class="notice ok">核齐：清单与已受理刷卡完全一致（${data.manifest_entries} 个事件）。</div>`;
  }

  const diffs = (data.diffs || []).map((d) => `
    <tr>
      <td>${esc(RECON_KIND_LABEL[d.kind] || d.kind)}</td>
      <td class="mono">${esc(d.event_no)}</td>
      <td class="num">${d.accepted_rev ?? "—"}</td>
      <td class="num">${d.manifest_rev ?? "—"}</td>
      <td class="small">${esc(d.detail || "")}</td>
    </tr>`).join("");

  const history = (data.manifest_history || []).map((m) => `
    <tr>
      <td>#${m.id}</td>
      <td class="mono small">${esc(m.filename)}</td>
      <td class="mono small">${esc(m.uploaded_at)}</td>
      <td>${m.row_count}</td>
      <td>${m.is_active ? '<span class="badge current">现行</span>' : '<span class="badge stale">已被替换</span>'}</td>    </tr>`).join("");

  panel.innerHTML = head + notice + `
    <div class="stat-row">
      <div class="stat"><div class="k">清单事件</div><div class="v">${data.manifest_entries ?? 0}</div></div>
      <div class="stat"><div class="k">已受理刷卡</div><div class="v">${data.accepted_taps ?? 0}</div></div>
      <div class="stat"><div class="k">差异</div><div class="v">${(data.diffs || []).length}</div></div>
    </div>
    <div class="cols">
      <div>
        <h3>差异明细</h3>
        <table class="grid">
          <thead><tr><th>类型</th><th>事件号</th><th class="num">已受理修订</th><th class="num">清单修订</th><th>说明</th></tr></thead>
          <tbody>${diffs || '<tr><td colspan="5" class="empty">无差异</td></tr>'}</tbody>
        </table>
      </div>
      <div>
        <h3>清单历史</h3>
        <table class="grid">
          <thead><tr><th>清单</th><th>文件</th><th>上传时间 (UTC)</th><th>行数</th><th>状态</th></tr></thead>
          <tbody>${history || '<tr><td colspan="5" class="empty">尚无清单</td></tr>'}</tbody>
        </table>
        <p class="hint">现行清单：${manifest ? `<span class="mono">${esc(manifest.filename)}</span>（#${manifest.id}，${esc(manifest.uploaded_at)}）` : "无"}</p>
        <button class="recon-retry" data-station="${esc(data.station)}" data-date="${esc(data.date)}"
          ${manifest ? "" : "disabled"}>重新核收</button>
      </div>
    </div>`;
  bindReconRetry();
}

async function loadReconDetail(station, date) {
  const res = await fetch(`/api/recon/${encodeURIComponent(station)}/${encodeURIComponent(date)}`);
  const body = await res.json().catch(() => ({}));
  const panel = $("#recon-panel");
  if (!panel) return;
  if (!res.ok) {
    panel.hidden = false;
    panel.innerHTML = `<div class="notice stale">${esc(body.detail || "加载失败")}</div>`;
    return;
  }
  renderReconDetail(body);
}

function bindReconOpen() {
  document.querySelectorAll(".recon-open").forEach((el) => {
    el.addEventListener("click", () => loadReconDetail(el.dataset.station, el.dataset.date));
  });
}

function bindReconRetry() {
  document.querySelectorAll(".recon-retry").forEach((el) => {
    el.addEventListener("click", async () => {
      el.disabled = true;
      const res = await fetch(
        `/api/recon/${encodeURIComponent(el.dataset.station)}/${encodeURIComponent(el.dataset.date)}/retry`,
        { method: "POST" },
      );
      const body = await res.json().catch(() => ({}));
      if (!res.ok) alert(body.detail || "重核失败");
      else renderReconDetail({ ...body.reconciliation, manifest_history: [] });
      await refreshRecon();
      if (res.ok) await loadReconDetail(el.dataset.station, el.dataset.date);
    });
  });
}

bindReconOpen();

$("#manifest-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const station = $("#manifest-station").value.trim();
  const date = $("#manifest-date").value;
  const fileInput = $("#manifest-file");
  const result = $("#manifest-result");
  const btn = $("#manifest-btn");
  if (!station || !date || !fileInput.files.length) return;
  const form = new FormData();
  form.append("station", station);
  form.append("date", date);
  form.append("file", fileInput.files[0]);
  btn.disabled = true;
  result.hidden = false;
  result.className = "result";
  result.textContent = "上传清单并核收中…";
  try {
    const res = await fetch("/api/manifests", { method: "POST", body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      result.className = "result error";
      result.textContent = body.detail || `上传失败（HTTP ${res.status}）`;
      return;
    }
    const rec = body.reconciliation || {};
    const replaced = body.replaced_manifest_id
      ? `已替换旧清单 #${body.replaced_manifest_id}，旧结论已撤回。`
      : "";
    const label = { complete: "核齐", mismatch: "不一致" }[rec.status] || "待核";
    result.className = rec.status === "complete" ? "result" : "result error";
    result.textContent =
      `清单 #${body.manifest_id} 已受理：${body.rows} 行（重复事件折叠 ${body.duplicate_rows}）。${replaced}\n` +
      `核收状态：${label}。` +
      (rec.counts ? ` 缺失 ${rec.counts.missing}，修订不符 ${rec.counts.revision}，清单外 ${rec.counts.unexpected}。` : "");
    await refreshRecon();
    await loadReconDetail(body.station, body.date);
  } catch (err) {
    result.className = "result error";
    result.textContent = `网络错误：${err}`;
  } finally {
    btn.disabled = false;
  }
});
