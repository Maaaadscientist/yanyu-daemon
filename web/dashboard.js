const stateLabels = {
  ready: "可采集",
  cooldown: "刷新中",
  travel_window: "可出发",
  failed: "失败",
  unknown: "未知"
};

const categoryLabels = {
  pen_livestock: "圈养牲畜",
  ranch_livestock: "牧场牲畜",
  wild_bear: "野熊",
  map_cow: "各地牛棚",
  mixed_livestock: "混合牲畜",
  fruit: "水果",
  collection: "采集",
  home_maintenance: "家宅",
  travel: "中转",
  other: "其他"
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let toastTimer;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function dateTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "-" : parsed.toLocaleString("zh-CN", { hour12: false });
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds <= 0) return "已到期";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}小时 ${minutes}分`;
  if (minutes) return `${minutes}分 ${secs}秒`;
  return `${secs}秒`;
}

function elapsedDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}小时 ${minutes}分`;
  if (minutes) return `${minutes}分 ${secs}秒`;
  return `${secs}秒`;
}

function statusCell(status) {
  return `<span class="status status-${escapeHtml(status)}">${escapeHtml(stateLabels[status] || status)}</span>`;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      "X-Yanyu-Request": "dashboard"
    },
    ...options
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

async function refresh() {
  try {
    const [status, events, acquisitions, summary] = await Promise.all([
      request("/api/status"),
      request("/api/events?limit=250"),
      request("/api/acquisitions?limit=250"),
      request("/api/summary")
    ]);
    renderStatus(status);
    renderEvents(events.events || []);
    renderAcquisitions(acquisitions.acquisitions || [], summary.acquisitions || {});
    $("#connection-state").textContent = status.scheduler_attached ? "调度器已连接" : "只读监控";
  } catch (error) {
    $("#connection-state").textContent = `连接失败: ${error.message}`;
  }
}

function renderStatus(payload) {
  const runtime = payload.runtime || {};
  const context = runtime.context || {};
  const checkpoint = runtime.checkpoint || {};
  $("#runtime-state").textContent = runtime.paused ? "已暂停" : (payload.scheduler_attached ? "运行中" : "只读");
  $("#current-task").textContent = context.task || checkpoint.task || "-";
  $("#current-step").textContent = stepLabel(context, checkpoint);
  $("#session-state").textContent = context.session_state || "-";
  $("#pause-total").textContent = elapsedDuration(runtime.total_pause_seconds || 0);
  $("#ready-count").textContent = payload.tasks.filter((item) => item.status === "ready").length;
  $("#failed-count").textContent = payload.tasks.filter((item) => item.status === "failed").length;
  $("#wild-bear-point-count").textContent = payload.points.filter((item) => item.category === "wild_bear").length;
  $("#map-cow-point-count").textContent = payload.points.filter((item) => item.category === "map_cow").length;
  $("#updated-at").textContent = dateTime(payload.generated_at);

  $$("[data-control]").forEach((button) => {
    button.disabled = !payload.scheduler_attached;
  });

  $("#task-rows").innerHTML = payload.tasks.map((task) => `
    <tr>
      <td><strong>${escapeHtml(task.name)}</strong><br><span class="mono muted">${escapeHtml(task.task)}</span></td>
      <td>${escapeHtml(categoryLabels[task.category] || task.category)}</td>
      <td>${statusCell(task.status)}</td>
      <td>${escapeHtml(task.interval_minutes)} 分</td>
      <td>${escapeHtml(duration(task.seconds_remaining))}</td>
      <td>${escapeHtml(dateTime(task.next_due))}</td>
      <td>${escapeHtml(dateTime(task.last_refresh_anchor))}</td>
      <td>${escapeHtml(task.failures)}</td>
    </tr>`).join("") || emptyRow(8);

  $("#point-rows").innerHTML = payload.points.map((point) => `
    <tr>
      <td><strong>${escapeHtml(point.label)}</strong><br><span class="mono muted">${escapeHtml(point.point_id)}</span></td>
      <td class="mono">${escapeHtml(point.task)}</td>
      <td>${escapeHtml(categoryLabels[point.category] || point.category)}</td>
      <td>${statusCell(point.status)}</td>
      <td>${escapeHtml(duration(point.seconds_remaining))}</td>
      <td>${escapeHtml(dateTime(point.last_refresh_anchor))}</td>
      <td>${escapeHtml(point.anchor_offset_samples ? `${point.anchor_offset_seconds.toFixed(1)} 秒` : "待学习")}</td>
      <td>${escapeHtml(point.samples)}</td>
    </tr>`).join("") || emptyRow(8);

  renderCheckpoint(runtime.paused ? checkpoint : null);
}

function stepLabel(context, checkpoint) {
  const value = context.action_index ? context : checkpoint;
  if (!value || !value.route) return value?.phase || "空闲";
  const action = value.next_action_index || value.action_index || "-";
  const total = value.total_actions || "-";
  return `${value.route} ${action}/${total}`;
}

function renderCheckpoint(checkpoint) {
  const fields = checkpoint ? [
    ["任务", checkpoint.task],
    ["路线", checkpoint.route],
    ["动作", `${checkpoint.next_action_index || checkpoint.action_index || "-"}/${checkpoint.total_actions || "-"}`],
    ["动作名称", checkpoint.action_label],
    ["地图", checkpoint.map],
    ["坐标", Array.isArray(checkpoint.coordinate) ? `(${checkpoint.coordinate.join(", ")})` : null],
    ["暂停时间", dateTime(checkpoint.paused_at)],
    ["原因", checkpoint.reason],
    ["恢复策略", "废弃中间动作，从任务第一条路线重跑"]
  ] : [["状态", "无活动断点"]];
  $("#checkpoint-details").innerHTML = fields.map(([label, value]) =>
    `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value || "-")}</dd>`
  ).join("");
}

function renderEvents(events) {
  $("#event-rows").innerHTML = events.map((event) => `
    <tr>
      <td>${escapeHtml(dateTime(event.time))}</td>
      <td class="mono">${escapeHtml(event.event)}</td>
      <td>${escapeHtml(event.task || "-")}</td>
      <td>${escapeHtml(event.route || event.action_label || event.point_label || "-")}</td>
      <td>${escapeHtml(event.error || event.reason || event.next_due || "-")}</td>
    </tr>`).join("") || emptyRow(5);
}

function renderAcquisitions(records, summary) {
  $("#acquisition-rows").innerHTML = records.map((record) => `
    <tr>
      <td>${escapeHtml(dateTime(record.time))}</td>
      <td class="mono">${escapeHtml(record.task)}</td>
      <td>${escapeHtml(record.point_label || "-")}</td>
      <td>${escapeHtml(record.quantity)} ${escapeHtml(record.unit || "")}${record.estimated ? " (估算)" : ""}</td>
      <td>${escapeHtml(record.source || "-")}</td>
      <td>${escapeHtml(dateTime(record.next_due))}</td>
    </tr>`).join("") || emptyRow(6);

  const categories = Object.entries(summary.by_category || {});
  $("#summary-list").innerHTML = categories.map(([category, quantity]) => `
    <div class="summary-item"><span>${escapeHtml(categoryLabels[category] || category)}</span><strong>${escapeHtml(quantity)}</strong></div>
  `).join("") || '<div class="muted">暂无记录</div>';
}

function emptyRow(columns) {
  return `<tr><td colspan="${columns}" class="muted">暂无数据</td></tr>`;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = error ? "visible error" : "visible";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = ""; }, 2600);
}

$$('[data-tab]').forEach((button) => {
  button.addEventListener("click", () => {
    $$('[data-tab]').forEach((item) => item.classList.toggle("active", item === button));
    $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${button.dataset.tab}`));
  });
});

$$('[data-control]').forEach((button) => {
  button.addEventListener("click", async () => {
    if (["force-resume", "stop"].includes(button.dataset.control)) {
      const label = button.dataset.control === "stop"
        ? "确认安全停止调度器？"
        : "确认跳过会话识别，但仍从任务开头重跑？";
      if (!window.confirm(label)) return;
    }
    button.disabled = true;
    try {
      await request(`/api/control/${button.dataset.control}`, { method: "POST", body: "{}" });
      showToast("控制请求已提交");
      await refresh();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
});

$("#adjustment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.quantity = Number(payload.quantity);
  try {
    await request("/api/acquisitions/adjust", { method: "POST", body: JSON.stringify(payload) });
    showToast("台账已更新");
    event.currentTarget.reset();
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  }
});

refresh();
setInterval(refresh, 2000);
