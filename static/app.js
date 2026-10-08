// 每日 AI 新闻助手前端逻辑
// 用 fetch POST + ReadableStream 解析 SSE（因为 EventSource 只支持 GET）

let currentUser = null;

// ---------- 工具函数 ----------
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(e.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

function el(id) { return document.getElementById(id); }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// ---------- 调度状态 ----------
async function loadScheduler() {
  try {
    const s = await api('GET', '/api/scheduler/status');
    const dot = el('schedDot');
    dot.className = 'dot ' + (s.running ? 'running' : 'stopped');
    const next = s.next_run_time ? new Date(s.next_run_time).toLocaleString('zh-CN') : '无';
    el('schedText').textContent = `调度${s.running ? '运行中' : '已停止'} · 下次: ${next}`;
  } catch (e) {
    el('schedText').textContent = '调度状态获取失败';
  }
}

// ---------- 用户 ----------
async function loadUsers() {
  const users = await api('GET', '/api/users');
  const sel = el('userSelect');
  sel.innerHTML = '<option value="">-- 选择用户 --</option>' + users.map(u =>
    `<option value="${u.user_id}">${esc(u.name)}</option>`).join('');
}

async function createUser() {
  const name = el('userName').value.trim();
  const webhook = el('userWebhook').value.trim();
  if (!name) { alert('请填姓名'); return; }
  try {
    const u = await api('POST', '/api/users', { name, feishu_webhook: webhook || null });
    el('userName').value = '';
    el('userWebhook').value = '';
    await loadUsers();
    // 自动选中刚创建的用户，立即展开偏好/生成/历史区，避免用户再手动选
    el('userSelect').value = u.user_id;
    await selectUser();
  } catch (e) { alert('创建失败: ' + e.message); }
}

async function selectUser() {
  const id = el('userSelect').value;
  if (!id) {
    currentUser = null;
    el('prefCard').hidden = true;
    el('genCard').hidden = true;
    el('listCard').hidden = true;
    el('delUserBtn').hidden = true;
    el('userWebhookEdit').hidden = true;
    return;
  }
  currentUser = await api('GET', `/api/users/${id}`);
  el('prefUserLabel').textContent = `· ${currentUser.name}`;
  el('prefCard').hidden = false;
  el('genCard').hidden = false;
  el('listCard').hidden = false;
  el('delUserBtn').hidden = false;
  el('userWebhookEdit').hidden = false;
  el('editWebhook').value = currentUser.feishu_webhook || '';
  await loadPrefs(id);
  await loadBriefings(id);
  await loadTodayBriefing();  // 切换用户时展示今日已有简报（若有）
}

async function updateUser() {
  // 占位（deleteUser 用）
}

async function updateWebhook() {
  if (!currentUser) return;
  try {
    await api('PUT', `/api/users/${currentUser.user_id}`, { feishu_webhook: el('editWebhook').value.trim() });
    currentUser = await api('GET', `/api/users/${currentUser.user_id}`);
    alert('已更新');
  } catch (e) { alert('更新失败: ' + e.message); }
}

async function deleteUser() {
  if (!currentUser) return;
  if (!confirm(`确认删除用户「${currentUser.name}」？所有偏好和简报会级联删除。`)) return;
  try {
    await api('DELETE', `/api/users/${currentUser.user_id}`);
    currentUser = null;
    await loadUsers();
    await selectUser();
  } catch (e) { alert('删除失败: ' + e.message); }
}

// ---------- 偏好 ----------
async function loadPrefs(userId) {
  const prefs = await api('GET', `/api/users/${userId}/preferences`);
  el('prefChips').innerHTML = prefs.map(p => `
    <span class="chip">
      <span class="type-tag">${p.type}</span>${esc(p.value)}
      <span class="x" onclick="removePref(${p.pref_id})">×</span>
    </span>`).join('') || '<span class="muted">暂无偏好，添加后才能生成简报</span>';
}

async function addPref() {
  if (!currentUser) return;
  const type = el('prefType').value;
  const value = el('prefValue').value.trim();
  if (!value) { alert('请填偏好内容'); return; }
  try {
    await api('POST', `/api/users/${currentUser.user_id}/preferences`, { type, value });
    el('prefValue').value = '';
    await loadPrefs(currentUser.user_id);
  } catch (e) { alert('添加失败: ' + e.message); }
}

async function removePref(prefId) {
  if (!currentUser) return;
  try {
    await api('DELETE', `/api/preferences/${prefId}`);
    await loadPrefs(currentUser.user_id);
  } catch (e) { alert('删除失败: ' + e.message); }
}

// ---------- 生成简报（SSE 流式）----------
async function generateBriefing() {
  if (!currentUser) return;
  // 预检：必须有订阅偏好，否则后端会 400 直接失败
  let prefs = [];
  try {
    prefs = await api('GET', `/api/users/${currentUser.user_id}/preferences`);
  } catch (e) { /* 忽略，下面后端会再报 */ }
  if (!prefs.length) {
    alert('请先添加订阅偏好（话题或关键词），再生成简报');
    el('prefValue').focus();
    return;
  }
  el('genBtn').disabled = true;
  // 思考过程区显示（折叠态，用户可自行展开），清空旧日志
  el('thinkDetails').hidden = false;
  el('thinkDetails').open = false;  // 默认收起
  el('liveTag').classList.add('show');
  el('logList').innerHTML = '';
  // 简报区显示加载态
  el('briefingView').hidden = true;
  el('briefingEmpty').hidden = false;
  el('briefingEmpty').textContent = '生成中... Agent 正在按你的订阅话题搜集新闻，可展开下方"思考过程"查看实时日志';

  appendStep({ step: 0, type: 'thought', content: '启动 ReAct agent，开始生成今日 AI 新闻简报...' });

  let lastFinish = null;
  try {
    const resp = await fetch(`/api/users/${currentUser.user_id}/briefings/generate`, { method: 'POST' });
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (chunk.startsWith('data: ')) {
          try {
            const ev = JSON.parse(chunk.slice(6));
            appendStep(ev);
            if (ev.type === 'finish') lastFinish = ev;
          } catch (e) { /* skip */ }
        }
      }
    }
    // finish 后拉取简报渲染到简报区
    if (lastFinish && lastFinish.briefing_id) {
      await renderTodayBriefing(lastFinish.briefing_id);
    } else {
      await loadTodayBriefing();
    }
    await loadBriefings(currentUser.user_id);
  } catch (e) {
    appendStep({ step: 0, type: 'error', content: '生成失败: ' + e.message });
    el('briefingEmpty').textContent = '生成失败: ' + e.message;
  } finally {
    el('liveTag').classList.remove('show');
    el('genBtn').disabled = false;
  }
}

// 渲染指定简报到生成区的"今日简报"区
async function renderTodayBriefing(briefingId) {
  const b = await api('GET', `/api/briefings/${briefingId}`);
  el('briefingDate').textContent = b.briefing_date;
  el('briefingPushStatus').innerHTML = b.feishu_pushed
    ? '<span class="badge ok">已推送飞书</span>'
    : '<span class="badge fail">仅本地</span>';
  el('briefingCalls').textContent = `工具调用 ${b.tool_call_count} 次`;
  el('briefingContent').innerHTML = marked.parse(b.markdown_content || '(空)');
  el('briefingView').hidden = false;
  el('briefingEmpty').hidden = true;
}

// 从历史列表找今天的简报渲染（selectUser 切换用户时调用）
async function loadTodayBriefing() {
  if (!currentUser) return;
  let list = [];
  try { list = await api('GET', `/api/users/${currentUser.user_id}/briefings`); } catch {}
  const todayIso = new Date().toISOString().slice(0, 10);
  const todayBrief = list.find(b => b.briefing_date === todayIso);
  if (todayBrief) {
    await renderTodayBriefing(todayBrief.briefing_id);
  } else {
    el('briefingView').hidden = true;
    el('briefingEmpty').hidden = false;
    el('briefingEmpty').textContent = '尚无今日简报，点击上方按钮生成';
  }
}

// 把任意 input（dict 或 string）转成可显示的 JSON 字符串
function stringifyInput(input) {
  if (input == null) return '{}';
  if (typeof input === 'string') {
    try { return JSON.stringify(JSON.parse(input), null, 2); } catch { return input; }
  }
  try { return JSON.stringify(input, null, 2); } catch { return String(input); }
}

// 终端日志风格渲染：💡思考 / 🔧调用 / ✅结果 / 🔴报错 / 🏁完成
function appendStep(ev) {
  const list = el('logList');
  list.appendChild(renderTermLine(ev));
  list.scrollTop = list.scrollHeight;
}

// 渲染单条事件为 DOM 节点（实时流和回看共用，兼容旧格式 agent_log）
function renderTermLine(ev) {
  const line = document.createElement('div');
  const type = ev.type || 'thought';
  let cls = 'term-line ' + type;

  let html = '';
  if (type === 'thought') {
    html = `<span class="ico">💡</span><span class="lbl">[思考]</span><span class="text">${esc(ev.content)}</span>`;
  } else if (type === 'action') {
    const argsStr = stringifyInput(ev.input);
    const needFold = argsStr.length > 80 || argsStr.includes('\n');
    let argsHtml;
    if (needFold) {
      argsHtml = `<details><summary>参数</summary><pre><code>${esc(argsStr)}</code></pre></details>`;
    } else {
      argsHtml = `<code class="args-inline">(${esc(argsStr)})</code>`;
    }
    html = `<span class="ico">🔧</span><span class="lbl">[调用]</span><span class="tool">${esc(ev.tool)}</span>${argsHtml}`;
  } else if (type === 'observation') {
    const content = String(ev.content ?? '');
    const isError = (ev.status === 'error') || (content.startsWith('ERROR'));
    if (isError) cls = 'term-line observation error';
    const ico = isError ? '🔴' : '✅';
    const lbl = isError ? '[报错]' : '[结果]';
    const dur = (ev.duration_ms != null)
      ? ` <span class="dur">(${(ev.duration_ms / 1000).toFixed(2)}s)</span>` : '';
    const tool = ev.tool ? `<span class="tool">${esc(ev.tool)}</span>` : '';
    const needFold = content.length > 160 || content.includes('\n');
    let body;
    if (needFold) {
      const preview = content.slice(0, 120).replace(/\n/g, ' ');
      body = `<details><summary><code>${esc(preview)}…</code></summary><pre><code>${esc(content)}</code></pre></details>`;
    } else {
      body = `<code class="obs-text">${esc(content)}</code>`;
    }
    html = `<span class="ico">${ico}</span><span class="lbl">${lbl}</span>${tool}${dur}${body}`;
  } else if (type === 'finish') {
    const ok = ev.feishu_pushed;
    const head = ok ? '🟢 已推送飞书' : '🟡 仅本地（推送失败或未配 webhook）';
    html = `<span class="ico">🏁</span><span class="lbl">[完成]</span><span class="text">${head} · 文件: ${esc(ev.file_path || '(无)')} · 工具调用 ${ev.tool_call_count} 次</span>`;
    if (ev.push_error) html += `<div class="err-detail">失败原因: ${esc(ev.push_error)}</div>`;
    if (ev.briefing_id) html += `<div class="dur">简报 ID: ${ev.briefing_id}</div>`;
  } else if (type === 'error') {
    html = `<span class="ico">🔴</span><span class="lbl">[错误]</span><span class="text">${esc(ev.content)}</span>`;
  } else {
    html = `<span class="ico">•</span><span class="text">${esc(ev.content)}</span>`;
  }
  line.className = cls;
  line.innerHTML = html;
  return line;
}

// ---------- 简报列表 + 详情 ----------
async function loadBriefings(userId) {
  const list = await api('GET', `/api/users/${userId}/briefings`);
  const tbody = el('briefingTable').querySelector('tbody');
  tbody.innerHTML = list.map(b => `
    <tr>
      <td>${esc(b.briefing_date)}</td>
      <td>${esc(b.title)}</td>
      <td>${b.feishu_pushed ? '<span class="badge ok">已推送</span>' : '<span class="badge fail">未推送</span>'}</td>
      <td>${b.tool_call_count}</td>
      <td><button onclick="showBriefing(${b.briefing_id})">查看</button></td>
    </tr>`).join('') || '<tr><td colspan="5" class="muted">暂无简报</td></tr>';
}

async function showBriefing(id) {
  const b = await api('GET', `/api/briefings/${id}`);
  el('detailTitle').textContent = b.title;
  el('briefingBody').innerHTML = marked.parse(b.markdown_content || '(空)');
  // 思考过程回看（终端样式，复用 renderTermLine，兼容旧格式事件）
  const log = b.agent_log || [];
  const tb = el('thinkBody');
  tb.classList.add('term-lookback');
  tb.innerHTML = '';
  if (log.length) {
    log.forEach(ev => tb.appendChild(renderTermLine(ev)));
  } else {
    tb.innerHTML = '<div class="muted">无思考过程记录</div>';
  }
  showTab('briefing');
  el('detailModal').hidden = false;
}

function showTab(tab) {
  el('briefingBody').hidden = tab !== 'briefing';
  el('thinkBody').hidden = tab !== 'think';
  document.querySelectorAll('.modal-tabs .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
}

function closeModal() { el('detailModal').hidden = true; }

// ---------- 初始化 ----------
window.addEventListener('DOMContentLoaded', async () => {
  await loadScheduler();
  await loadUsers();
  setInterval(loadScheduler, 30000); // 30 秒刷新调度状态
});
