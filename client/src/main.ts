import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

type Tab = "status" | "proposals" | "agents" | "settings";

interface Settings {
  mode: string;
  ssh_host: string;
  ssh_user: string;
  ssh_port: number;
  ssh_key: string;
  remote_port: number;
  local_port: number;
  direct_url: string;
  poll_seconds: number;
  vault_path: string;
}

interface SettingsInfo {
  settings: Settings;
  token_configured: boolean;
  ssh_available: boolean;
  platform: string;
  version: string;
}

interface RuntimeView {
  connected: boolean;
  latency_ms: number | null;
  last_check: number | null;
  last_error: string | null;
  local_port: number | null;
  retrying: boolean;
  retry_count: number;
}

interface SyncthingDevice {
  id: string;
  name: string;
  connected: boolean;
  paused: boolean;
  self: boolean;
  address: string;
  last_seen: string;
}

interface SyncthingSummary {
  configured: boolean;
  ok: boolean;
  error: string | null;
  my_id?: string;
  devices: SyncthingDevice[];
  online: number;
  total: number;
  updated?: number;
}

interface StatusData {
  ok: boolean;
  generated_at: number;
  vault: {
    notes: number;
    errors: number;
    warnings: number;
    check_ok: boolean;
    sync_conflicts: number;
    last_commit: { short: string; full: string; date: string; subject: string } | null;
  };
  dream: { last_run: string | number | null; reports: number };
  proposals: { files: number; pending: number; total: number };
  archive: { devices: number; files: number; size: number; latest: number | null };
  syncthing: SyncthingSummary;
}

interface ProposalSection {
  index: number;
  kind: string;
  label: string;
  target: string;
  status: string;
  description: string;
  body: string;
  diff: string[];
  problem: string | null;
  content_available: boolean;
}

interface ProposalListItem {
  name: string;
  title: string;
  mtime: number;
  pending: number;
  total: number;
  sections: Array<Omit<ProposalSection, "body" | "diff" | "content_available">>;
}

interface ProposalDetail {
  ok: boolean;
  name: string;
  title: string;
  sections: ProposalSection[];
}

interface AgentView {
  id: string;
  name: string;
  installed: boolean;
  integrated: boolean;
  manual: boolean;
  config_path: string;
}

interface AgentConfigView {
  vault_path: string;
  vault_exists: boolean;
  vault_ready: boolean;
  agents: AgentView[];
}

let currentTab: Tab = "status";
let settingsInfo: SettingsInfo | null = null;
let runtime: RuntimeView | null = null;
let wasConnected = false;
let proposals: ProposalListItem[] = [];
let agentConfig: AgentConfigView | null = null;
let detailName: string | null = null;
let statusTimer: number | null = null;
let toastTimer: number | null = null;
const IS_PANEL = document.documentElement.dataset.panel === "1";

const STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  accepted: "已批准",
  rejected: "已驳回",
};

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`缺少元素 #${id}`);
  return node as T;
}

function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      default: return "&#39;";
    }
  });
}

function setText(id: string, value: unknown) {
  el(id).textContent = String(value ?? "");
}

function fmtTime(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

function fmtStamped(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number"
    ? new Date(value > 1e11 ? value : value * 1000)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function fmtStampedShort(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number"
    ? new Date(value > 1e11 ? value : value * 1000)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtAgo(ms: number | null | undefined): string {
  if (!ms) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return fmtTime(Math.floor(ms / 1000));
}

function fmtBytes(size: number): string {
  if (!size) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function toast(message: string, bad = false) {
  const node = el("toast");
  node.textContent = message;
  node.classList.toggle("bad", bad);
  node.hidden = false;
  if (toastTimer !== null) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    node.hidden = true;
  }, bad ? 6000 : 3200);
}

function confirmModal(options: {
  title: string;
  message: string;
  confirmLabel: string;
  danger?: boolean;
}): Promise<boolean> {
  return new Promise((resolve) => {
    const root = el("modal-root");
    root.innerHTML = `
      <div class="modal-backdrop">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <h3 id="modal-title">${esc(options.title)}</h3>
          <p class="modal-message">${esc(options.message)}</p>
          <div class="modal-actions">
            <button class="btn ghost" type="button" data-modal="cancel">取消</button>
            <button class="btn ${options.danger ? "danger" : "primary"}" type="button" data-modal="ok">
              ${esc(options.confirmLabel)}
            </button>
          </div>
        </div>
      </div>`;
    const close = (result: boolean) => {
      root.innerHTML = "";
      document.removeEventListener("keydown", onKey);
      resolve(result);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close(false);
    };
    document.addEventListener("keydown", onKey);
    root.querySelectorAll<HTMLElement>("[data-modal]").forEach((button) => {
      button.addEventListener("click", () => close(button.dataset.modal === "ok"));
    });
    root.querySelector<HTMLElement>("[data-modal='ok']")?.focus();
  });
}

// ---------- 状态 ----------

function renderRuntime(view: RuntimeView) {
  const was = wasConnected;
  runtime = view;
  const direct = settingsInfo?.settings.mode === "direct";
  const stateText = view.connected
    ? "已连接"
    : view.retrying
      ? `重连中（第 ${view.retry_count} 次）`
      : "未连接";
  setText("conn-latency", view.latency_ms !== null ? `${view.latency_ms} ms` : "—");
  setText(
    "conn-port",
    view.local_port !== null ? view.local_port : direct ? "直连" : "—",
  );
  setText("conn-check", view.last_check ? fmtAgo(view.last_check) : "—");

  const error = el("conn-error");
  const message = view.last_error ?? "";
  error.hidden = view.connected || !message;
  error.textContent = message;

  const pill = el("conn-pill");
  const label = view.connected ? "已连接" : view.retrying ? "重连中" : "未连接";
  pill.className = `stat-value conn ${view.connected ? "ok" : view.retrying ? "warn" : "bad"}`;
  pill.innerHTML = `<span class="dot"></span><span>${label}</span>`;

  const sidebarDot = el("sidebar-dot");
  sidebarDot.className = `dot ${view.connected ? "ok" : view.retrying ? "paused" : "idle"}`;
  setText("sidebar-state", stateText);
  setText(
    "sidebar-port",
    view.local_port !== null
      ? `127.0.0.1:${view.local_port}`
      : direct
        ? "直连模式"
        : "等待隧道",
  );

  wasConnected = view.connected;
  if (view.connected && !was) {
    void refreshStatus();
    if (currentTab === "proposals") void refreshProposals();
  }
}

async function refreshRuntime() {
  try {
    renderRuntime(await invoke<RuntimeView>("get_runtime"));
  } catch (error) {
    console.error(error);
  }
}

function renderStatus(data: StatusData) {
  const vault = data.vault;
  const dream = data.dream;
  const archive = data.archive;

  setText("sync-notes", vault.notes);
  const conflicts = el("sync-conflicts");
  conflicts.textContent = String(vault.sync_conflicts);
  conflicts.classList.toggle("warn", vault.sync_conflicts > 0);
  setText(
    "sync-check",
    vault.check_ok
      ? vault.errors > 0
        ? `${vault.errors} 个错误`
        : vault.warnings > 0
          ? `通过 · ${vault.warnings} 条提示`
          : "通过"
      : "未完成",
  );
  setText("archive-summary", `${archive.devices} 台 · ${archive.files} 文件`);
  setText("archive-size", fmtBytes(archive.size));
  setText("status-updated", `更新于 ${fmtTime(data.generated_at)}`);

  const commit = vault.last_commit;
  const commitNode = el("last-commit");
  if (commit) {
    commitNode.hidden = false;
    commitNode.innerHTML = `
      <span class="commit-label">最近提交</span>
      <code>${esc(commit.short)}</code>
      <span class="commit-subject">${esc(commit.subject)}</span>
      <span class="sub">${esc(fmtStamped(commit.date))}</span>`;
  } else {
    commitNode.hidden = true;
  }

  const dreamLast = el("dream-last");
  dreamLast.textContent = fmtStampedShort(dream.last_run);
  dreamLast.title = fmtStamped(dream.last_run);
  setText("dream-reports", dream.reports);
  setText("dream-pending", data.proposals.pending);
  setText("dream-pending-row", `${data.proposals.pending} 条`);
  setText(
    "stat-pending-sub",
    data.proposals.files > 0
      ? `${data.proposals.files} 个提案文件`
      : "暂无提案文件",
  );
  setNavPending(data.proposals.pending);

  renderSyncthing(data.syncthing);
}

function renderSyncthing(st: SyncthingSummary) {
  const list = el("syncthing-list");
  const note = el("syncthing-note");
  list.innerHTML = "";
  note.hidden = true;

  if (!st.configured) {
    setText("syncthing-summary", "未接入");
    setText("stat-devices", "—");
    setText("stat-devices-sub", "未接入");
    note.hidden = false;
    note.className = "notice";
    note.textContent = "服务器未配置 Syncthing API key，设备在线状态暂不可用。";
    return;
  }
  if (!st.ok) {
    setText("syncthing-summary", "不可达");
    setText("stat-devices", "—");
    setText("stat-devices-sub", "不可达");
    note.hidden = false;
    note.className = "notice bad";
    note.textContent = st.error ?? "无法读取 Syncthing 状态。";
    return;
  }

  setText("syncthing-summary", `${st.online} / ${st.total} 台在线`);
  setText("stat-devices", `${st.online}/${st.total}`);
  setText("stat-devices-sub", `${st.total} 台设备`);
  for (const device of st.devices) {
    const item = document.createElement("li");
    const state = device.paused ? "paused" : device.connected ? "ok" : "idle";
    item.innerHTML = `
      <span class="dot ${state}"></span>
      <span class="device-name">${esc(device.name)}</span>
      <span class="device-meta">${esc(device.self ? "本机" : device.paused ? "已暂停" : device.connected ? "在线" : "离线")}</span>
      ${device.last_seen ? `<span class="device-seen" title="${esc(fmtStamped(device.last_seen))}">最后活跃 ${esc(fmtStampedShort(device.last_seen))}</span>` : ""}`;
    list.appendChild(item);
  }
}

function renderStatusUnavailable(message: string) {
  setText("status-updated", message);
  setText("syncthing-summary", "—");
  el("syncthing-list").innerHTML = "";
}

async function refreshStatus() {
  try {
    renderStatus(await invoke<StatusData>("fetch_status"));
  } catch (error) {
    renderStatusUnavailable(`状态不可用：${String(error)}`);
  }
}

function setNavPending(count: number) {
  const badge = el("nav-pending");
  badge.textContent = String(count);
  badge.hidden = count <= 0;
}

function scheduleStatusPoll() {
  if (statusTimer !== null) window.clearInterval(statusTimer);
  const seconds = settingsInfo?.settings.poll_seconds || 10;
  statusTimer = window.setInterval(() => {
    if (runtime?.connected) void refreshStatus();
  }, Math.max(3, Math.min(120, seconds)) * 1000);
}

// ---------- 提案 ----------

function escapeAttr(value: string): string {
  return esc(value);
}

function diffHtml(lines: string[]): string {
  return lines.map((line) => {
    let cls = "ctx";
    if (line.startsWith("+++") || line.startsWith("---")) cls = "meta";
    else if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+")) cls = "add";
    else if (line.startsWith("-")) cls = "del";
    return `<span class="diff-line ${cls}">${esc(line)}</span>`;
  }).join("");
}

function sectionCard(name: string, section: ProposalSection): string {
  const badge = `<span class="badge ${esc(section.status)}">${esc(STATUS_LABELS[section.status] ?? section.status)}</span>`;
  const problem = section.problem
    ? `<p class="notice bad">无法自动应用：${esc(section.problem)}</p>`
    : "";
  const body = section.content_available || section.body
    ? `<details class="body-details" open>
         <summary>${section.status === "accepted" ? "目标文件当前内容" : "变更后的内容"}</summary>
         <pre class="body">${esc(section.body)}</pre>
       </details>`
    : "";
  const diff = section.diff.length
    ? `<details class="diff-details">
         <summary>查看补丁</summary>
         <pre class="diff">${diffHtml(section.diff)}</pre>
       </details>`
    : "";
  const actions = section.status === "pending"
    ? `<div class="section-actions">
         <button class="btn primary" type="button" data-decide="accept"
           data-name="${escapeAttr(name)}" data-index="${section.index}"
           data-desc="${escapeAttr(section.description)}"
           ${section.problem ? "disabled" : ""}>批准并写入</button>
         <button class="btn danger ghost" type="button" data-decide="reject"
           data-name="${escapeAttr(name)}" data-index="${section.index}"
           data-desc="${escapeAttr(section.description)}">驳回</button>
       </div>`
    : "";
  return `<article class="section-card">
    <div class="section-head">
      <span class="tag">${esc(section.label)}</span>
      <strong>${esc(section.description || "（无说明）")}</strong>
      ${badge}
    </div>
    <p class="sub">目标：<code>${esc(section.target)}</code></p>
    ${problem}${body}${diff}${actions}
  </article>`;
}

function renderProposalDetail(data: ProposalDetail) {
  setText("detail-title", data.title);
  const pending = data.sections.filter((s) => s.status === "pending").length;
  setText("detail-meta", `${data.name} · 共 ${data.sections.length} 条 · 待处理 ${pending} 条`);
  el("detail-sections").innerHTML = data.sections
    .map((section) => sectionCard(data.name, section))
    .join("");
}

async function loadProposal(name: string) {
  detailName = name;
  el("btn-refresh-proposals").hidden = true;
  el("proposal-list-card").hidden = true;
  el("proposal-list").hidden = true;
  el("proposal-empty").hidden = true;
  el("proposal-offline").hidden = true;
  el("proposal-detail").hidden = false;
  try {
    renderProposalDetail(await invoke<ProposalDetail>("fetch_proposal", { name }));
  } catch (error) {
    setText("detail-title", "读取失败");
    setText("detail-meta", "");
    el("detail-sections").innerHTML = `<p class="notice bad">${esc(error)}</p>`;
  }
}

function renderProposalList() {
  const list = el("proposal-list");
  const pending = proposals.reduce((sum, proposal) => sum + proposal.pending, 0);
  setText(
    "proposal-summary",
    proposals.length
      ? `共 ${proposals.length} 个提案文件 · ${pending} 条待处理`
      : "没有提案文件",
  );
  setNavPending(pending);
  el("btn-refresh-proposals").hidden = false;
  el("proposal-list-card").hidden = false;
  list.hidden = false;
  el("proposal-offline").hidden = true;
  el("proposal-empty").hidden = proposals.length > 0;
  list.innerHTML = proposals.map((proposal) => `
    <button class="proposal-item" type="button" data-open="${escapeAttr(proposal.name)}">
      <span class="proposal-item-head">
        <strong>${esc(proposal.title)}</strong>
        <span class="badge ${proposal.pending ? "pending" : "done"}">
          ${proposal.pending ? `待处理 ${proposal.pending}` : "已完成"}
        </span>
      </span>
      <span class="sub">${esc(fmtTime(proposal.mtime))} · 共 ${proposal.total} 条</span>
      <span class="items">${proposal.sections
        .map((section) => `<span class="tag">${esc(section.label)} ${esc(section.target)}</span>`)
        .join("")}</span>
    </button>`).join("");
}

function showProposalOffline(message: string) {
  el("btn-refresh-proposals").hidden = false;
  el("proposal-list-card").hidden = true;
  el("proposal-list").hidden = true;
  el("proposal-empty").hidden = true;
  el("proposal-detail").hidden = true;
  const offline = el("proposal-offline");
  offline.hidden = false;
  offline.textContent = message;
  setText("proposal-summary", "无法读取提案");
}

async function refreshProposals() {
  if (!runtime?.connected) {
    showProposalOffline("尚未连接服务器，或访问令牌未配置。先在设置里完成连接。");
    return;
  }
  try {
    const data = await invoke<{ proposals: ProposalListItem[] }>("fetch_proposals");
    proposals = data.proposals;
    renderProposalList();
    if (detailName) await loadProposal(detailName);
  } catch (error) {
    showProposalOffline(`无法读取提案：${String(error)}`);
  }
}

async function decide(
  button: HTMLButtonElement,
  name: string,
  index: number,
  action: "accept" | "reject",
  description: string,
) {
  const label = action === "accept" ? "批准并写入" : "驳回";
  const ok = await confirmModal({
    title: action === "accept" ? "批准这一条？" : "驳回这一条？",
    message: action === "accept"
      ? `将把补丁写入记忆库，并标记为已批准。\n\n${description}\n\n写入后需要所有设备同步才会看到改动。`
      : `目标文件不会改动，只把这一条标记为已驳回。\n\n${description}`,
    confirmLabel: label,
    danger: action === "reject",
  });
  if (!ok) return;
  button.disabled = true;
  try {
    await invoke("decide_proposal", { name, index, action });
    toast(action === "accept" ? "已批准并写入记忆库" : "已驳回");
    await refreshProposals();
    await refreshStatus();
  } catch (error) {
    toast(String(error), true);
    await refreshProposals();
  } finally {
    button.disabled = false;
  }
}

// ---------- 设置 ----------

type ParsedServer =
  | { mode: "direct"; direct_url: string }
  | { mode: "ssh"; ssh_user: string; ssh_host: string };

/** 「服务器」一个输入框表达两种连接：user@主机 走 SSH 隧道，http(s):// 走直连。 */
function parseServerAddress(raw: string): ParsedServer | null {
  const value = raw.trim();
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) {
    return { mode: "direct", direct_url: value.replace(/\/+$/, "") };
  }
  const match = value.match(/^([^@\s]+)@([^@\s/:]+)$/);
  if (!match) return null;
  return { mode: "ssh", ssh_user: match[1], ssh_host: match[2] };
}

function formatServerAddress(settings: Settings): string {
  if (settings.mode === "ssh") {
    return settings.ssh_user && settings.ssh_host
      ? `${settings.ssh_user}@${settings.ssh_host}`
      : "";
  }
  return settings.direct_url;
}

function fillSettings(info: SettingsInfo) {
  const settings = info.settings;
  (el<HTMLInputElement>("server-address")).value = formatServerAddress(settings);
  (el<HTMLInputElement>("ssh-port")).value = String(settings.ssh_port);
  (el<HTMLInputElement>("ssh-key")).value = settings.ssh_key;
  (el<HTMLInputElement>("remote-port")).value = String(settings.remote_port);
  (el<HTMLInputElement>("local-port")).value = String(settings.local_port);
  (el<HTMLInputElement>("poll-seconds")).value = String(settings.poll_seconds);

  const tokenInput = el<HTMLInputElement>("token-input");
  tokenInput.value = "";
  tokenInput.placeholder = info.token_configured
    ? "已保存，留空则不修改"
    : "输入访问令牌";
  el("btn-clear-token").hidden = !info.token_configured;

  const sshNote = el("ssh-note");
  if (!info.ssh_available) {
    sshNote.hidden = false;
    sshNote.className = "notice bad";
    sshNote.textContent = info.platform === "windows"
      ? "未找到 ssh 命令。请启用 Windows 的 OpenSSH 客户端，或改用直连地址。"
      : "未找到 ssh 命令。请安装 OpenSSH 客户端，或改用直连地址。";
  } else {
    sshNote.hidden = true;
  }

  document.documentElement.dataset.platform = info.platform;
  setText("version-note", `版本 ${info.version}`);
}

async function loadSettings(applyForm = true) {
  try {
    settingsInfo = await invoke<SettingsInfo>("get_settings");
    if (applyForm) fillSettings(settingsInfo);
    scheduleStatusPoll();
  } catch (error) {
    toast(`读取设置失败：${String(error)}`, true);
  }
}

function collectSettings(): Settings {
  const number = (id: string, fallback: number) => {
    const value = Number.parseInt((el<HTMLInputElement>(id)).value, 10);
    return Number.isFinite(value) ? value : fallback;
  };
  const server = parseServerAddress(el<HTMLInputElement>("server-address").value);
  if (!server) {
    throw new Error("服务器地址需要写成 user@主机，或 http(s)://地址。");
  }
  // 切到直连时保留原来的 SSH 配置，之后再切回隧道不用重填。
  const previous = settingsInfo?.settings;
  return {
    mode: server.mode,
    ssh_host: server.mode === "ssh" ? server.ssh_host : previous?.ssh_host ?? "",
    ssh_user: server.mode === "ssh" ? server.ssh_user : previous?.ssh_user ?? "",
    ssh_port: number("ssh-port", 22),
    ssh_key: el<HTMLInputElement>("ssh-key").value.trim(),
    remote_port: number("remote-port", 8787),
    local_port: number("local-port", 0),
    direct_url: server.mode === "direct" ? server.direct_url : "",
    poll_seconds: number("poll-seconds", 10),
    vault_path: previous?.vault_path ?? "~/memory-vault",
  };
}

async function saveSettings(event: SubmitEvent) {
  event.preventDefault();
  const token = el<HTMLInputElement>("token-input").value.trim();
  try {
    const settings = collectSettings();
    setText("settings-flash", "正在保存…");
    await invoke("save_settings", { settings, token: token || null });
    setText("settings-flash", "已保存，正在重新连接。");
    await loadSettings();
    await refreshRuntime();
    window.setTimeout(() => {
      void refreshRuntime();
      setText("settings-flash", "");
    }, 1200);
    toast("设置已保存");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setText("settings-flash", `保存失败：${message}`);
    toast(message, true);
  }
}

// ---------- Agent 接入 ----------

function renderAgents() {
  const config = agentConfig;
  const list = el("agent-list");
  const note = el("agent-vault-note");
  if (!config) {
    list.innerHTML = `<p class="empty">正在读取…</p>`;
    return;
  }

  const input = el<HTMLInputElement>("agent-vault-path");
  if (document.activeElement !== input) input.value = config.vault_path;

  note.hidden = config.vault_ready;
  if (!config.vault_ready) {
    note.className = "notice bad";
    note.textContent = config.vault_exists
      ? `vault 不完整：${config.vault_path} 缺少 AGENT_PROTOCOL.md 或 MEMORY.md。`
      : `vault 不存在：${config.vault_path}。先完成 Syncthing 同步，或改成正确路径。`;
  }

  list.innerHTML = config.agents.map((agent) => {
    let status: string;
    let statusClass = "";
    if (agent.manual) {
      status = "手动配置";
    } else if (!agent.installed) {
      status = "未安装";
      statusClass = " is-muted";
    } else if (agent.integrated) {
      status = "已接入";
      statusClass = " is-on";
    } else {
      status = "未接入";
      statusClass = " is-muted";
    }

    const canToggle = agent.installed && (agent.integrated || config.vault_ready);
    const control = agent.manual
      ? `<span class="agent-hint">Settings → Rules → User Rules</span>`
      : `<button class="switch${agent.integrated ? " is-on" : ""}" type="button"
           role="switch" aria-checked="${agent.integrated}"
           aria-label="${esc(agent.name)} 接入"
           data-agent="${esc(agent.id)}"
           ${canToggle ? "" : "disabled"}>
           <span class="switch-knob"></span>
         </button>`;

    return `
      <div class="agent-row">
        <div class="agent-main">
          <span class="agent-name">${esc(agent.name)}</span>
          <span class="agent-path mono">${esc(agent.config_path)}</span>
        </div>
        <div class="agent-side">
          <span class="agent-status${statusClass}">${status}</span>
          ${control}
        </div>
      </div>`;
  }).join("");
}

async function loadAgents() {
  try {
    agentConfig = await invoke<AgentConfigView>("get_agents");
    renderAgents();
  } catch (error) {
    el("agent-list").innerHTML = `<p class="empty">读取失败：${esc(String(error))}</p>`;
  }
}

async function toggleAgent(button: HTMLButtonElement) {
  const id = button.dataset.agent;
  if (!id) return;
  const enabled = button.getAttribute("aria-checked") !== "true";
  button.disabled = true;
  try {
    agentConfig = await invoke<AgentConfigView>("set_agent", { id, enabled });
    renderAgents();
    toast(enabled ? "已接入，重启对应 agent 后生效" : "已移除接入");
  } catch (error) {
    toast(String(error), true);
    await loadAgents();
  }
}

async function saveVaultPath() {
  const input = el<HTMLInputElement>("agent-vault-path");
  const button = el<HTMLButtonElement>("btn-save-vault");
  const previousPath = agentConfig?.vault_path;
  const hadIntegrated = agentConfig?.agents.some((agent) => agent.integrated) ?? false;
  button.disabled = true;
  try {
    agentConfig = await invoke<AgentConfigView>("save_vault_path", { path: input.value });
    if (settingsInfo) settingsInfo.settings.vault_path = agentConfig.vault_path;
    renderAgents();
    const pathChanged = previousPath !== undefined && previousPath !== agentConfig.vault_path;
    const notes = [
      hadIntegrated && pathChanged
        ? "路径已保存；请重新打开需要接入的 agent 开关"
        : "vault 路径已保存",
    ];
    if (/\s/.test(agentConfig.vault_path)) {
      notes.push("路径包含空格，部分 agent 可能无法解析引用");
    }
    toast(notes.join("。"));
  } catch (error) {
    toast(String(error), true);
  } finally {
    button.disabled = false;
  }
}

// ---------- 交互 ----------

function switchTab(requested: Tab) {
  // 面板只做速览；连接、Agent 配置留在主窗口。
  const tab: Tab = IS_PANEL && (requested === "settings" || requested === "agents")
    ? "status"
    : requested;
  currentTab = tab;
  const crumbs: Record<Tab, [string, string]> = {
    status: ["使用", "状态"],
    proposals: ["使用", "提案"],
    agents: ["系统", "Agent"],
    settings: ["系统", "设置"],
  };
  setText("crumb-root", crumbs[tab][0]);
  setText("crumb-page", crumbs[tab][1]);
  document.querySelectorAll<HTMLElement>("[data-tab]").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.tab === tab);
  });
  for (const name of ["status", "proposals", "agents", "settings"]) {
    const page = document.getElementById(`tab-${name}`);
    if (page) page.hidden = name !== tab;
  }
  if (tab === "status") void refreshStatus();
  if (tab === "proposals") void refreshProposals();
  if (tab === "agents") void loadAgents();
  if (tab === "settings") void loadSettings();
}

function bind() {
  document.querySelectorAll<HTMLElement>("[data-tab]").forEach((item) => {
    item.addEventListener("click", () => switchTab(item.dataset.tab as Tab));
  });
  document.querySelectorAll<HTMLElement>("[data-goto]").forEach((item) => {
    item.addEventListener("click", () => switchTab(item.dataset.goto as Tab));
  });

  el("btn-reconnect").addEventListener("click", async () => {
    toast("正在重新连接");
    await invoke("reconnect");
    window.setTimeout(() => void refreshRuntime(), 800);
  });
  el("btn-open-web").addEventListener("click", () => openWeb());
  el("btn-panel-main").addEventListener("click", () => void invoke("show_main_window"));
  el("btn-panel-quit").addEventListener("click", () => void invoke("quit_app"));
  if (IS_PANEL) {
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") void invoke("hide_panel");
    });
  }
  el("btn-settings-reconnect").addEventListener("click", async () => {
    await invoke("reconnect");
    toast("正在重新连接");
    window.setTimeout(() => void refreshRuntime(), 800);
  });

  el("settings-form").addEventListener("submit", (event) => void saveSettings(event));
  el("btn-clear-token").addEventListener("click", async () => {
    const ok = await confirmModal({
      title: "清除访问令牌？",
      message: "清除后客户端无法读取状态和提案，直到重新输入令牌。",
      confirmLabel: "清除",
      danger: true,
    });
    if (!ok) return;
    try {
      await invoke("clear_token");
      await loadSettings();
      toast("令牌已清除");
    } catch (error) {
      toast(String(error), true);
    }
  });

  el("agent-list").addEventListener("click", (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-agent]");
    if (button && !button.disabled) void toggleAgent(button);
  });
  el("btn-save-vault").addEventListener("click", () => void saveVaultPath());
  el("agent-vault-path").addEventListener("keydown", (event) => {
    if ((event as KeyboardEvent).key === "Enter") {
      event.preventDefault();
      void saveVaultPath();
    }
  });

  el("btn-refresh-proposals").addEventListener("click", () => void refreshProposals());
  el("btn-refresh-detail").addEventListener("click", () => {
    if (detailName) void loadProposal(detailName);
  });
  el("btn-back").addEventListener("click", () => {
    detailName = null;
    el("proposal-detail").hidden = true;
    void refreshProposals();
  });
  el("btn-open-proposal").addEventListener("click", () => {
    if (detailName) void openWeb(`proposals/${encodeURIComponent(detailName)}`);
  });
  el("proposal-list").addEventListener("click", (event) => {
    const target = (event.target as HTMLElement).closest<HTMLElement>("[data-open]");
    if (target?.dataset.open) void loadProposal(target.dataset.open);
  });
  el("detail-sections").addEventListener("click", (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-decide]");
    if (!button || button.disabled) return;
    const action = button.dataset.decide as "accept" | "reject";
    void decide(
      button,
      button.dataset.name ?? "",
      Number.parseInt(button.dataset.index ?? "0", 10),
      action,
      button.dataset.desc ?? "",
    );
  });
}

async function openWeb(path?: string) {
  try {
    await invoke("open_web", { path: path ?? null });
  } catch (error) {
    toast(String(error), true);
  }
}

async function init() {
  if (IS_PANEL) {
    // 面板显示后 Rust 会探测页面是否还活着；没响应就重载，避免 WebKit 回收内容进程后空白。
    await listen("panel-probe", () => {
      void invoke("panel_alive");
    });
  }
  if (!IS_PANEL) {
    document.querySelector(".topbar")?.setAttribute("data-tauri-drag-region", "");
  }
  bind();
  await loadSettings();
  await refreshRuntime();
  await refreshStatus();
  scheduleStatusPoll();

  window.setInterval(() => void refreshRuntime(), 2500);
  window.setInterval(() => {
    if (currentTab === "proposals" && !detailName && runtime?.connected) {
      void refreshProposals();
    }
  }, 30000);

  await listen("runtime-changed", async () => {
    await refreshRuntime();
  });
  await listen("panel-shown", async () => {
    (document.activeElement as HTMLElement | null)?.blur();
    await refreshRuntime();
    if (currentTab === "status") await refreshStatus();
    if (currentTab === "proposals") await refreshProposals();
  });
  await listen<string>("client-error", (event) => toast(event.payload, true));
}

void init();
