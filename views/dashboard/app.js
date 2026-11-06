const bridge = window.AstrBotPluginView;

const $ = (id) => document.getElementById(id);
const state = {
  config: null,
  models: [],
  sizes: [],
  presets: [],
  history: { page: 1, page_size: 10, total: 0, items: [] },
  gallery: { page: 1, page_size: 20, total: 0, items: [] },
  editingPreset: null,
  refImage: null,
  pollTimer: null,
  toastTimer: null,
};

const STATUS_TEXT = { ok: "成功", failed: "失败", running: "生成中" };
const SOURCE_TEXT = { tool: "AI", command: "指令", web: "控制台" };

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

let confirmResolve = null;

function askConfirm(text) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $("confirm-text").textContent = text;
    $("confirm").classList.remove("hidden");
  });
}

function closeConfirm(result) {
  $("confirm").classList.add("hidden");
  if (confirmResolve) {
    confirmResolve(result);
    confirmResolve = null;
  }
}

async function apiGet(endpoint, params) {
  try {
    return await bridge.apiGet(endpoint, params);
  } catch (error) {
    toast(error.message || String(error));
    throw error;
  }
}

async function apiPost(endpoint, body) {
  try {
    return await bridge.apiPost(endpoint, body || {});
  } catch (error) {
    toast(error.message || String(error));
    throw error;
  }
}

function fmtTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fmtDuration(ms) {
  if (!ms) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

function badge(status) {
  const cls = status === "ok" ? "badge-ok" : status === "failed" ? "badge-fail" : "badge-run";
  return `<span class="badge ${cls}">${STATUS_TEXT[status] || status}</span>`;
}

async function loadImageInto(img, recordId, index, full) {
  try {
    const result = await bridge.apiGet("image", { id: recordId, index: index || 0, full: full ? 1 : 0 });
    const src = result && (result.image || result.data);
    if (!src) throw new Error("接口没有返回图片数据");
    img.src = src;
  } catch (error) {
    img.alt = "图片加载失败";
    img.title = error.message || String(error);
  }
}

/* ------------------------------------------------------------------ tabs */

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${name}`);
  });
  if (name === "history") loadHistory(state.history.page);
  if (name === "gallery") loadGallery(state.gallery.page);
  if (name === "presets") loadPresets();
  if (name === "overview") loadOverview();
}

/* --------------------------------------------------------------- overview */

async function loadOverview() {
  const data = await apiGet("overview");
  state.configured = data.configured;
  $("stat-today").textContent = data.stats.today;
  $("stat-total").textContent = data.stats.total;
  $("stat-rate").textContent = data.stats.success_rate;
  $("stat-avg").textContent = data.stats.avg_ms ? (data.stats.avg_ms / 1000).toFixed(1) : "0";

  $("ov-model").textContent = data.summary.req_key_label;
  $("ov-size").textContent = data.summary.size_label;
  $("ov-tool").textContent = data.summary.enable_llm_tool ? "已开启" : "已关闭";

  $("banner-config").classList.toggle("hidden", !!data.configured);
  const pill = $("status-pill");
  pill.textContent = data.configured ? "已配置" : "未配置";
  pill.className = `pill ${data.configured ? "pill-ok" : "pill-warn"}`;

  const grid = $("recent-grid");
  grid.innerHTML = "";
  const items = (data.recent || []).filter((item) => item.images > 0);
  if (!items.length) {
    grid.innerHTML = '<div class="empty">还没有生成记录</div>';
    return;
  }
  for (const record of items) {
    const box = document.createElement("div");
    box.className = "item";
    box.innerHTML = `<img alt="生成图片" /><div class="cap">${escapeHtml(record.prompt || "")}</div>`;
    const img = box.querySelector("img");
    loadImageInto(img, record.id, 0, false);
    img.addEventListener("click", () => openModal(record.id, 0));
    grid.appendChild(box);
  }
}

async function refreshQuotaLabel() {
  try {
    const data = await apiGet("overview");
    const limit = data.summary.daily_limit || "∞";
    $("ov-quota").textContent = `${data.stats.today} / ${limit}`;
  } catch (error) {
    /* ignore */
  }
}

/* --------------------------------------------------------------- config */

async function loadConfig() {
  const data = await apiGet("config");
  state.config = data.config;
  state.models = data.models || [];
  state.sizes = data.sizes || [];

  fillSelect($("cfg-reqkey"), state.models);
  fillSelect($("cfg-size"), state.sizes);
  fillSelect($("gen-size"), state.sizes);
  fillSelect($("preset-size"), state.sizes);

  const cfg = data.config;
  $("cfg-ak").value = cfg.access_key_id || "";
  $("cfg-sk").value = "";
  $("cfg-sk").placeholder = cfg.secret_access_key ? "已保存，留空表示不修改" : "填写 SecretAccessKey";
  $("cfg-reqkey").value = cfg.req_key;
  $("cfg-size").value = cfg.size_preset;
  $("gen-size").value = cfg.size_preset;
  $("preset-size").value = cfg.size_preset;
  $("cfg-scale").value = cfg.scale;
  $("cfg-scale-val").textContent = cfg.scale;
  $("cfg-force").checked = cfg.force_single;
  $("cfg-img2img").checked = cfg.enable_img2img;
  $("cfg-maxinputs").value = cfg.max_input_images;
  $("ref-field").classList.toggle("hidden", !cfg.enable_img2img);
  $("cfg-r2").checked = cfg.r2_enabled;
  $("cfg-r2-delete").checked = cfg.r2_delete_after;
  $("cfg-r2-account").value = cfg.r2_account_id || "";
  $("cfg-r2-bucket").value = cfg.r2_bucket || "";
  $("cfg-r2-base").value = cfg.r2_public_base || "";
  $("cfg-r2-prefix").value = cfg.r2_prefix || "jimeng/";
  $("cfg-r2-ak").value = cfg.r2_access_key_id || "";
  $("cfg-r2-sk").value = "";
  $("cfg-r2-sk").placeholder = cfg.r2_secret_access_key ? "已保存，留空表示不修改" : "填写 R2 Secret";
  if (!cfg.enable_img2img) {
    state.refImage = null;
    $("ref-url").value = "";
    renderRefImage();
  }
  $("cfg-watermark").checked = cfg.watermark;
  $("cfg-timeout").value = cfg.timeout;
  $("cfg-interval").value = cfg.poll_interval;
  $("cfg-concurrency").value = cfg.max_concurrency;
  $("cfg-tool").checked = cfg.enable_llm_tool;
  $("cfg-toolwait").checked = cfg.tool_wait;
  $("cfg-hint").checked = cfg.inject_hint;
  $("cfg-command").checked = cfg.enable_command;
  $("cfg-daily").value = cfg.daily_limit;
  $("cfg-userdaily").value = cfg.user_daily_limit;
  $("cfg-cooldown").value = cfg.cooldown;
  $("cfg-admin").checked = cfg.admin_bypass;
  $("cfg-whitelist").value = (cfg.whitelist || []).join("\n");
}

function fillSelect(select, options) {
  select.innerHTML = "";
  for (const option of options) {
    const el = document.createElement("option");
    el.value = option.value;
    el.textContent = option.label;
    select.appendChild(el);
  }
}

async function saveConfig() {
  const payload = {
    access_key_id: $("cfg-ak").value.trim(),
    secret_access_key: $("cfg-sk").value,
    req_key: $("cfg-reqkey").value,
    size_preset: $("cfg-size").value,
    scale: Number($("cfg-scale").value),
    force_single: $("cfg-force").checked,
    enable_img2img: $("cfg-img2img").checked,
    max_input_images: Number($("cfg-maxinputs").value),
    r2_enabled: $("cfg-r2").checked,
    r2_delete_after: $("cfg-r2-delete").checked,
    r2_account_id: $("cfg-r2-account").value.trim(),
    r2_bucket: $("cfg-r2-bucket").value.trim(),
    r2_public_base: $("cfg-r2-base").value.trim(),
    r2_prefix: $("cfg-r2-prefix").value.trim(),
    r2_access_key_id: $("cfg-r2-ak").value.trim(),
    r2_secret_access_key: $("cfg-r2-sk").value,
    watermark: $("cfg-watermark").checked,
    timeout: Number($("cfg-timeout").value),
    poll_interval: Number($("cfg-interval").value),
    max_concurrency: Number($("cfg-concurrency").value),
    enable_llm_tool: $("cfg-tool").checked,
    tool_wait: $("cfg-toolwait").checked,
    inject_hint: $("cfg-hint").checked,
    enable_command: $("cfg-command").checked,
    daily_limit: Number($("cfg-daily").value),
    user_daily_limit: Number($("cfg-userdaily").value),
    cooldown: Number($("cfg-cooldown").value),
    admin_bypass: $("cfg-admin").checked,
    whitelist: $("cfg-whitelist").value.split(/\n+/).map((item) => item.trim()).filter(Boolean),
  };
  $("save-state").textContent = "保存中…";
  try {
    await apiPost("config/save", payload);
    $("save-state").textContent = "已保存";
    toast("设置已保存");
    await loadConfig();
    await loadOverview();
  } catch (error) {
    $("save-state").textContent = "保存失败";
  }
}

async function clearCredentials() {
  if (!(await askConfirm("确定清除已保存的 AccessKey / SecretAccessKey 吗？"))) return;
  await apiPost("config/save", { clear_credentials: true });
  toast("凭证已清除");
  await loadConfig();
  await loadOverview();
}

async function testR2() {
  const button = $("btn-r2-test");
  button.disabled = true;
  $("r2-test-result").textContent = "测试中…";
  try {
    const result = await apiPost("r2/test", {
      r2_account_id: $("cfg-r2-account").value.trim(),
      r2_bucket: $("cfg-r2-bucket").value.trim(),
      r2_public_base: $("cfg-r2-base").value.trim(),
      r2_access_key_id: $("cfg-r2-ak").value.trim(),
      r2_secret_access_key: $("cfg-r2-sk").value,
      r2_prefix: $("cfg-r2-prefix").value.trim(),
    });
    $("r2-test-result").textContent = result.ok
      ? `✅ ${result.status} · ${result.latency_ms}ms · ${result.bytes}B`
      : `⚠️ ${result.message || "公网读取失败"}`;
    if (result.ok) toast("R2 上传与公网读取正常");
  } catch (error) {
    $("r2-test-result").textContent = `❌ ${error.message || error}`;
  } finally {
    button.disabled = false;
  }
}

async function clearR2() {
  if (!(await askConfirm("确定清除已保存的 R2 AccessKey / Secret 吗？"))) return;
  await apiPost("config/save", { clear_r2_credentials: true });
  toast("R2 凭证已清除");
  await loadConfig();
}

/* ------------------------------------------------------------- generate */

function renderRefImage() {
  const box = $("ref-preview");
  if (state.refImage && state.refImage.preview) {
    box.innerHTML = `<img src="${state.refImage.preview}" alt="参考图" />`;
  } else {
    box.innerHTML = "未选择";
  }
}

async function uploadRefImage(file) {
  if (!file) return;
  $("btn-ref-pick").disabled = true;
  try {
    const result = await bridge.upload("upload", file);
    state.refImage = result;
    renderRefImage();
    toast("参考图已上传");
  } catch (error) {
    toast(error.message || String(error));
  } finally {
    $("btn-ref-pick").disabled = false;
    $("ref-file").value = "";
  }
}

function clearRefImage() {
  state.refImage = null;
  $("ref-url").value = "";
  renderRefImage();
}

async function startGenerate() {
  const prompt = $("gen-prompt").value.trim();
  if (!prompt) {
    toast("请先填写提示词");
    return;
  }
  const button = $("btn-generate");
  button.disabled = true;
  showStatus("提交任务…", true);
  $("gen-result").innerHTML = "";
  $("gen-meta").textContent = "";

  try {
    const payload = {
      prompt,
      size: $("gen-size").value,
    };
    if (state.refImage && state.refImage.id) payload.image_id = state.refImage.id;
    const refUrl = $("ref-url").value.trim();
    if (refUrl) payload.image_url = refUrl;
    const { task_id: taskId } = await apiPost("generate", payload);
    pollTask(taskId);
  } catch (error) {
    button.disabled = false;
    showStatus(`❌ ${error.message || error}`, false);
  }
}

function showStatus(text, spinning) {
  const el = $("gen-status");
  el.classList.remove("hidden");
  el.innerHTML = `${spinning ? '<span class="spinner"></span>' : ""}<span>${escapeHtml(text)}</span>`;
}

function pollTask(taskId) {
  clearTimeout(state.pollTimer);
  const tick = async () => {
    let task;
    try {
      task = await apiGet("task", { id: taskId });
    } catch (error) {
      $("btn-generate").disabled = false;
      showStatus(`❌ ${error.message || error}`, false);
      return;
    }
    if (task.status === "running") {
      showStatus("正在生成，请稍候…（通常 20～60 秒）", true);
      state.pollTimer = setTimeout(tick, 1500);
      return;
    }
    $("btn-generate").disabled = false;
    if (task.status === "failed") {
      showStatus(`❌ ${task.error || "生成失败"}`, false);
      loadOverview();
      return;
    }
    const record = task.result;
    showStatus("", false);
    $("gen-status").classList.add("hidden");
    $("gen-meta").textContent = `${fmtDuration(record.duration_ms)} · ${record.size}`;
    renderResult(record);
    await loadOverview();
    await loadHistory(state.history.page);
  };
  tick();
}

function renderResult(record) {
  const box = $("gen-result");
  box.innerHTML = "";
  const count = record.images || 0;
  for (let index = 0; index < count; index += 1) {
    const img = document.createElement("img");
    img.alt = "生成结果";
    loadImageInto(img, record.id, index, false);
    img.addEventListener("click", () => openModal(record.id, index));
    box.appendChild(img);
  }
  if (!count) box.innerHTML = '<div class="empty">没有返回图片</div>';
}

/* ---------------------------------------------------------------- history */

async function loadHistory(page) {
  const target = Math.max(1, page || 1);
  const data = await apiGet("history", { page: target, page_size: 10 });
  state.history = { ...data, page: target };
  const body = $("history-body");
  body.innerHTML = "";
  if (!data.items.length) {
    body.innerHTML = '<tr><td colspan="7" class="muted">暂无记录</td></tr>';
  }
  for (const record of data.items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtTime(record.created_at)}</td>
      <td>${escapeHtml(record.user_name || "—")}</td>
      <td>${SOURCE_TEXT[record.source] || record.source || "—"}</td>
      <td>${record.size || "—"}</td>
      <td>${fmtDuration(record.duration_ms)}</td>
      <td>${badge(record.status)}</td>
      <td class="right">
        ${record.images ? '<button class="btn btn-ghost btn-sm" data-act="view">查看</button>' : ""}
        <button class="btn btn-danger btn-sm" data-act="delete">删除</button>
      </td>`;
    tr.querySelector('[data-act="view"]')?.addEventListener("click", () => openModal(record.id, 0));
    tr.querySelector('[data-act="delete"]')?.addEventListener("click", () => deleteRecord(record.id));
    if (record.status === "failed" && record.error) tr.title = record.error;
    body.appendChild(tr);
  }
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  $("page-info").textContent = `第 ${data.page} / ${pages} 页 · 共 ${data.total} 条`;
  $("btn-prev").disabled = data.page <= 1;
  $("btn-next").disabled = data.page >= pages;
}

async function loadGallery(page) {
  const target = Math.max(1, page || 1);
  const data = await apiGet("gallery", { page: target });
  state.gallery = { ...data, page: target };

  $("gallery-count").textContent = `共 ${data.total} 张`;
  const grid = $("gallery-grid");
  grid.innerHTML = "";
  if (!data.items.length) {
    grid.innerHTML = '<div class="empty">还没有图片</div>';
  }
  for (const item of data.items) {
    const box = document.createElement("div");
    box.className = "item";
    box.innerHTML = `<img alt="图库图片" /><div class="cap">${escapeHtml(item.prompt || "")}</div>`;
    const img = box.querySelector("img");
    loadImageInto(img, item.record_id, item.index, false);
    img.title = item.created_at || "";
    img.addEventListener("click", () => openModal(item.record_id, item.index));
    grid.appendChild(box);
  }

  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  $("gallery-page-info").textContent = `第 ${data.page} / ${pages} 页`;
  $("gallery-prev").disabled = data.page <= 1;
  $("gallery-next").disabled = data.page >= pages;
}

async function deleteRecord(id) {  if (!(await askConfirm("删除这条记录和对应图片？"))) return;
  await apiPost("history/delete", { id });
  toast("已删除");
  await loadHistory(state.history.page);
  await loadOverview();
}

/* ---------------------------------------------------------------- presets */

async function loadPresets() {
  const data = await apiGet("presets");
  state.presets = data.items || [];
  const list = $("preset-list");
  list.innerHTML = "";
  if (!state.presets.length) {
    list.innerHTML = '<div class="empty">还没有预设</div>';
  }
  for (const preset of state.presets) {
    const item = document.createElement("div");
    item.className = "preset-item";
    item.innerHTML = `
      <header><b>${escapeHtml(preset.name)}</b><span class="muted">${preset.size || ""}</span></header>
      <p>${escapeHtml(preset.prompt)}</p>
      <div class="preset-actions">
        <button class="btn btn-sm" data-act="use">使用</button>
        <button class="btn btn-sm" data-act="edit">编辑</button>
        <button class="btn btn-danger btn-sm" data-act="del">删除</button>
      </div>`;
    item.querySelector('[data-act="use"]').addEventListener("click", () => {
      $("gen-prompt").value = preset.prompt;
      $("gen-size").value = preset.size || $("gen-size").value;
      switchTab("generate");
      toast(`已填入预设：${preset.name}`);
    });
    item.querySelector('[data-act="edit"]').addEventListener("click", () => {
      state.editingPreset = preset.id;
      $("preset-form-title").textContent = "编辑预设";
      $("preset-name").value = preset.name;
      $("preset-prompt").value = preset.prompt;
      $("preset-size").value = preset.size || "2048x2048";
    });
    item.querySelector('[data-act="del"]').addEventListener("click", () => deletePreset(preset.id));
    list.appendChild(item);
  }

  const chips = $("gen-presets");
  chips.innerHTML = "";
  for (const preset of state.presets) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = preset.name;
    chip.addEventListener("click", () => {
      $("gen-prompt").value = preset.prompt;
      if (preset.size) $("gen-size").value = preset.size;
    });
    chips.appendChild(chip);
  }
}

async function savePreset() {
  const name = $("preset-name").value.trim();
  const prompt = $("preset-prompt").value.trim();
  if (!name || !prompt) {
    toast("名称和提示词都不能为空");
    return;
  }
  await apiPost("presets/save", {
    id: state.editingPreset,
    name,
    prompt,
    size: $("preset-size").value,
  });
  toast("预设已保存");
  resetPresetForm();
  await loadPresets();
}

async function deletePreset(id) {
  if (!(await askConfirm("删除这个预设？"))) return;
  await apiPost("presets/delete", { id });
  await loadPresets();
}

function resetPresetForm() {
  state.editingPreset = null;
  $("preset-form-title").textContent = "新增预设";
  $("preset-name").value = "";
  $("preset-prompt").value = "";
  $("preset-size").value = (state.config && state.config.size_preset) || "2048x2048";
}

/* ------------------------------------------------------------------ modal */

async function openModal(recordId, index) {
  const modal = $("modal");
  modal.classList.remove("hidden");
  $("modal-img").src = "";
  await loadImageInto($("modal-img"), recordId, index, true);
}

function closeModal() {
  $("modal").classList.add("hidden");
  $("modal-img").src = "";
}

/* ------------------------------------------------------------------ misc */

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function refreshAll() {
  await loadConfig();
  await loadOverview();
  await loadPresets();
  await loadHistory(1);
  await refreshQuotaLabel();
}

async function init() {
  if (!bridge) {
    document.body.innerHTML = '<div class="app"><div class="card">Bridge 未加载，请从 AstrBot 插件页打开此视图。</div></div>';
    return;
  }
  const context = await bridge.ready();
  document.title = context.pageTitle || "即梦生图控制台";

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });
  document.querySelectorAll("[data-goto]").forEach((el) => {
    el.addEventListener("click", (event) => {
      event.preventDefault();
      switchTab(el.dataset.goto);
    });
  });

  $("btn-generate").addEventListener("click", startGenerate);
  $("btn-ref-pick").addEventListener("click", () => $("ref-file").click());
  $("ref-file").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (file) uploadRefImage(file);
  });
  $("btn-ref-clear").addEventListener("click", clearRefImage);
  $("gen-prompt").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") startGenerate();
  });
  $("btn-refresh").addEventListener("click", () => refreshAll().then(() => toast("已刷新")));
  $("btn-save").addEventListener("click", saveConfig);
  $("btn-clear").addEventListener("click", clearCredentials);
  $("btn-r2-test").addEventListener("click", testR2);
  $("btn-r2-clear").addEventListener("click", clearR2);
  $("cfg-scale").addEventListener("input", () => {
    $("cfg-scale-val").textContent = $("cfg-scale").value;
  });
  $("btn-prev").addEventListener("click", () => loadHistory(state.history.page - 1));
  $("btn-next").addEventListener("click", () => loadHistory(state.history.page + 1));
  $("gallery-prev").addEventListener("click", () => loadGallery(state.gallery.page - 1));
  $("gallery-next").addEventListener("click", () => loadGallery(state.gallery.page + 1));
  $("btn-preset-save").addEventListener("click", savePreset);
  $("btn-preset-reset").addEventListener("click", resetPresetForm);
  $("modal-close").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (event) => {
    if (event.target.dataset.close) closeModal();
  });
  $("confirm-ok").addEventListener("click", () => closeConfirm(true));
  $("confirm-cancel").addEventListener("click", () => closeConfirm(false));
  $("confirm").addEventListener("click", (event) => {
    if (event.target.dataset.confirm === "0") closeConfirm(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("confirm").classList.contains("hidden")) {
      closeConfirm(false);
    } else {
      closeModal();
    }
  });

  await refreshAll();
}

init().catch((error) => {
  console.error(error);
  toast(error.message || String(error));
});
