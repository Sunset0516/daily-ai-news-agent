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
    await api('POST', '/api/users', { name, feishu_webhook: webhook || null });
    el('userName').value = '';
    el('userWebhook').value = '';
    await loadUsers();
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
  el('genBtn').disabled = true;
  el('thinkLog').hidden = false;
  el('logList').innerHTML = '';

  // 渲染起始提示
  appendStep({ step: 0, type: 'thought', content: '开始生成，调用 LLM...' });

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
          } catch (e) { /* skip */ }
        }
      }
    }
    await loadBriefings(currentUser.user_id);
  } catch (e) {
    appendStep({ step: 0, type: 'error', content: '生成失败: ' + e.message });
  } finally {
    el('genBtn').disabled = false;
  }
}

function appendStep(ev) {
  const list = el('logList');
  const card = document.createElement('div');
  let cls = 'step-card ' + (ev.type || '');
  if (ev.type === 'finish') cls = 'step-card finish';
  let tag = ev.type || '';
  let body = '';
  if (ev.type === 'action') {
    body = `工具: ${esc(ev.tool)}\n输入: ${esc(ev.input)}`;
  } else if (ev.type === 'observation') {
    body = `${ev.tool ? '[' + esc(ev.tool) + '] ' : ''}${esc(ev.content)}`;
    // 推送失败标红
    if (typeof ev.content === 'string' && ev.content.startsWith('ERROR')) cls = 'step-card error';
  } else if (ev.type === 'finish') {
    const pushed = ev.feishu_pushed ? '✅ 已推送飞书' : '⚠ 仅本地（推送失败）';
    body = `${pushed}\n文件: ${esc(ev.file_path || '(无)')}\n工具调用次数: ${ev.tool_call_count}`;
    if (ev.push_error) body += `\n失败原因: ${esc(ev.push_error)}`;
    if (ev.briefing_id) body += `\n简报 ID: ${ev.briefing_id}`;
  } else if (ev.type === 'error') {
    body = esc(ev.content);
  } else {
    body = esc(ev.content);
  }
  card.className = cls;
  card.innerHTML = `<span class="step-no">#${ev.step}</span><span class="tag">${esc(tag)}</span><span class="content">${body}</span>`;
  list.appendChild(card);
  list.scrollTop = list.scrollHeight;
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
  // 思考过程回看
  const log = b.agent_log || [];
  el('thinkBody').innerHTML = log.map(ev => {
    let cls = 'step-card ' + (ev.type || '');
    if (ev.type === 'finish') cls = 'step-card finish';
    let body = '';
    if (ev.type === 'action') body = `工具: ${esc(ev.tool)}<br>输入: ${esc(ev.input)}`;
    else if (ev.type === 'observation') {
      body = `${ev.tool ? '[' + esc(ev.tool) + '] ' : ''}${esc(ev.content)}`;
      if (typeof ev.content === 'string' && ev.content.startsWith('ERROR')) cls = 'step-card error';
    } else if (ev.type === 'finish') {
      body = `飞书: ${ev.feishu_pushed ? '已推送' : '未推送'}<br>文件: ${esc(ev.file_path)}<br>调用次数: ${ev.tool_call_count}`;
    } else body = esc(ev.content);
    return `<div class="${cls}"><span class="step-no">#${ev.step}</span><span class="tag">${esc(ev.type)}</span><span class="content">${body}</span></div>`;
  }).join('') || '<div class="muted">无思考过程记录</div>';
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
