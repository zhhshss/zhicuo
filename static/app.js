'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  view: 'dashboard',
  config: { configured: false, hasUserDomainId: false, camScannerConfigured: false },
  options: {},
  dashboard: null,
  library: { items: [], total: 0, page: 1, limit: 18, status: 'active' },
  selected: new Set(),
  captureFiles: [],
  captureMode: 'search',
  review: { items: [], index: 0, revealed: false },
  detail: null,
  editImageUrls: [],
  exportItems: [],
  exportJobs: {},
  regionDraft: null,
  regionDrag: null,
  regionHistory: [],
};

const SUBJECT_ICONS = { '语文': '文', '数学': '数', '英语': '英', '物理': '理', '化学': '化', '生物': '生', '政治': '政', '历史': '史', '地理': '地' };

function escapeHTML(value = '') {
  return String(value).replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function sanitizeHTML(value = '') {
  if (!value) return '';
  const doc = new DOMParser().parseFromString(`<div>${value}</div>`, 'text/html');
  doc.querySelectorAll('script,iframe,object,embed,link,style').forEach(node => node.remove());
  doc.querySelectorAll('*').forEach(node => {
    [...node.attributes].forEach(attr => {
      const name = attr.name.toLowerCase();
      const val = attr.value.trim().toLowerCase();
      if (name.startsWith('on') || name === 'srcdoc' || ((name === 'src' || name === 'href') && val.startsWith('javascript:'))) node.removeAttribute(attr.name);
    });
  });
  return doc.body.firstElementChild?.innerHTML || '';
}

function plainText(value = '') {
  const div = document.createElement('div');
  div.innerHTML = sanitizeHTML(value);
  return (div.textContent || '').replace(/\s+/g, ' ').trim();
}

function simpleMarkdown(value = '') {
  let text = escapeHTML(value);
  text = text.replace(/^### (.+)$/gm, '<h3>$1</h3>').replace(/^## (.+)$/gm, '<h2>$1</h2>').replace(/^# (.+)$/gm, '<h1>$1</h1>');
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
  text = text.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  return text.split(/\n{2,}/).map(part => /^<(h\d|blockquote)/.test(part) ? part : `<p>${part.replace(/\n/g, '<br>')}</p>`).join('');
}

function normalizeLatex(value = '') {
  return String(value)
    .replace(/[（]/g, '(').replace(/[）]/g, ')')
    .replace(/[，]/g, ',').replace(/[：]/g, ':')
    .replace(/\\?~\\?frac/g, '\\frac');
}

function renderMath(root = document) {
  if (typeof window.renderMathInElement !== 'function' || !root) return;
  try {
    window.renderMathInElement(root, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '$', right: '$', display: false },
      ],
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
      ignoredClasses: ['katex'],
      throwOnError: false,
      strict: 'ignore',
      preProcess: normalizeLatex,
    });
  } catch (error) {
    console.warn('公式渲染失败', error);
  }
}

function scheduleMath(root = document) {
  requestAnimationFrame(() => renderMath(root));
}

async function mapConcurrent(items, concurrency, worker, onProgress = null) {
  const results = new Array(items.length);
  let cursor = 0;
  let completed = 0;
  const run = async () => {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await worker(items[index], index);
      completed++;
      onProgress?.(completed, items.length);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, run));
  return results;
}

function formatDate(value, withTime = false) {
  if (!value) return '未安排';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}) }).format(date);
}

function toast(message, type = '') {
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  node.textContent = message;
  $('#toastStack').appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(data?.detail || data?.message || `请求失败（${response.status}）`);
  return data;
}

function debounce(fn, wait = 300) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}

function openModal(id) { $(`#${id}`).classList.add('open'); document.body.style.overflow = 'hidden'; }
function closeModal(id) { $(`#${id}`).classList.remove('open'); document.body.style.overflow = ''; }

async function switchView(view, pushHash = true) {
  if (!$(`#view-${view}`)) view = 'dashboard';
  state.view = view;
  $$('.view').forEach(node => node.classList.toggle('active', node.id === `view-${view}`));
  $$('.nav-item[data-view]').forEach(node => node.classList.toggle('active', node.dataset.view === view));
  $('#sidebar').classList.remove('open');
  if (pushHash) history.replaceState(null, '', `#${view}`);
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (view === 'dashboard') await loadDashboard();
  if (view === 'library') await loadLibrary();
  if (view === 'review') await loadReview();
  if (view === 'export') await refreshExportItems();
}

async function loadConfig() {
  state.config = await api('/api/config');
  const status = $('#aiStatus');
  status.classList.toggle('ready', state.config.configured);
  status.querySelector('span:last-child').textContent = state.config.configured ? 'AI 已连接' : 'AI 未配置';
  $('#captureConfigNotice').className = `notice ${state.config.configured ? 'success' : ''}`;
  $('#captureConfigNotice').textContent = state.config.configured ? 'AI 拍题服务已连接，可以开始识别。' : '需要先在“AI 与数据”中导入 139 云盘配置，才能调用拍题接口。';
  $('#configStatus').className = `notice ${state.config.configured ? 'success' : ''}`;
  const encryption = state.config.encrypted ? ` · ${state.config.algorithm} 加密存储` : '';
  $('#configStatus').textContent = state.config.configured ? `连接配置有效${state.config.hasUserDomainId ? '' : '，建议补充 userDomainId'}${encryption}` : '尚未配置 Token';
  const camStatus = $('#camScannerStatus');
  if (camStatus) {
    camStatus.className = `notice ${state.config.camScannerConfigured ? 'success' : ''}`;
    camStatus.textContent = state.config.camScannerConfigured ? 'CamScanner 凭证已配置' : '尚未配置 CamScanner 凭证';
  }
}

function aiModelLabel(model) {
  const name = String(model || '').replace(/^pp\//, '');
  return name.replace(/[-_]+/g, ' ').replace(/\b\w/g, char => char.toUpperCase());
}

function selectedAIModel() {
  return $('#exportAiModel')?.value || $('#captureAiModel')?.value || localStorage.getItem('mistake_ai_model') || 'pp/gemini-3.8-flash';
}

function applyAIModelOptions(models, preferred = '') {
  const values = [...new Set((models || []).filter(Boolean))];
  const saved = localStorage.getItem('mistake_ai_model');
  const selected = values.includes(saved) ? saved : values.includes(preferred) ? preferred : values[0] || 'pp/gemini-3.8-flash';
  $$('.ai-model-select').forEach(select => {
    select.innerHTML = values.map(model => `<option value="${escapeHTML(model)}">${escapeHTML(aiModelLabel(model))}${model === 'pp/gemini-3.8-flash' ? '（推荐）' : ''}</option>`).join('');
    select.value = selected;
    select.onchange = event => {
      const value = event.target.value;
      localStorage.setItem('mistake_ai_model', value);
      $$('.ai-model-select').forEach(other => { other.value = value; });
    };
  });
}

async function loadAIModels() {
  try {
    const result = await api('/api/ai/models');
    applyAIModelOptions(result.models, result.default);
  } catch (error) {
    console.warn('读取 AI 模型列表失败', error);
    applyAIModelOptions(['pp/gemini-3.8-flash'], 'pp/gemini-3.8-flash');
  }
}

async function loadOptions() {
  state.options = await api('/api/options');
  const mappings = [['filterSubject', 'subject', '全部学科'], ['filterNotebook', 'notebook', '全部错题本'], ['filterError', 'error_type', '全部错因']];
  mappings.forEach(([id, field, first]) => {
    const select = $(`#${id}`);
    const current = select.value;
    select.innerHTML = `<option value="">${first}</option>` + (state.options[field] || []).map(value => `<option>${escapeHTML(value)}</option>`).join('');
    select.value = current;
  });
}

async function loadDashboard() {
  const [dashboard, recent] = await Promise.all([
    api('/api/dashboard'),
    api('/api/mistakes?limit=6&sort=updated_desc&status=active'),
  ]);
  state.dashboard = dashboard;
  const stats = dashboard.stats;
  $('#statTotal').textContent = stats.total;
  $('#statDue').textContent = stats.due;
  $('#statStarred').textContent = stats.starred;
  $('#statMastery').textContent = stats.mastery;
  $('#navTotal').textContent = stats.total;
  $('#navDue').textContent = stats.due;
  $('#storageCount').textContent = `${stats.total} 题`;
  $('#storageProgress').style.width = `${Math.min(100, Math.max(2, stats.total / 5))}%`;
  const hour = new Date().getHours();
  $('#greetingLine').textContent = hour < 11 ? '早上好。用十分钟回顾昨天的错题，记忆会更牢。' : hour < 18 ? '每一道认真复盘的错题，都在缩短与答案的距离。' : '晚上好。少量、高质量的复习比机械刷题更有效。';
  renderTrend(dashboard.trend);
  renderSubjects(dashboard.subjects);
  renderRecent(recent.items);
}

function renderTrend(trend) {
  const max = Math.max(1, ...trend.map(item => item.value));
  $('#trendChart').innerHTML = trend.map(item => {
    const height = Math.max(4, item.value / max * 155);
    const day = new Date(`${item.day}T00:00:00`);
    const label = ['日','一','二','三','四','五','六'][day.getDay()];
    return `<div class="bar-column"><div class="bar" style="height:${height}px"><span class="bar-value">${item.value}</span></div><span class="bar-day">周${label}</span></div>`;
  }).join('');
}

function renderSubjects(subjects) {
  if (!subjects.length) { $('#subjectChart').innerHTML = '<div class="empty" style="padding:55px 10px">录入错题后，这里会展示学科分布。</div>'; return; }
  const max = Math.max(...subjects.map(item => item.value));
  $('#subjectChart').innerHTML = subjects.map(item => `<div class="subject-row"><b>${escapeHTML(item.name)}</b><div class="subject-track"><div class="subject-fill" style="width:${item.value / max * 100}%"></div></div><span class="subject-count">${item.value}</span></div>`).join('');
}

function renderRecent(items) {
  $('#recentList').innerHTML = items.length ? items.map(item => `<div class="recent-row" data-detail="${item.id}"><div class="subject-chip">${escapeHTML(SUBJECT_ICONS[item.subject] || '题')}</div><div><div class="recent-title">${escapeHTML(item.title || plainText(item.question).slice(0, 30) || '未命名错题')}</div><div class="recent-meta">${escapeHTML(item.subject)} · ${escapeHTML(item.error_type)} · ${formatDate(item.updated_at)}</div></div><span class="pill ${item.mastery >= 70 ? '' : 'coral'}">${item.mastery}%</span></div>`).join('') : '<div class="empty" style="padding:35px 10px">还没有错题，先录入第一道吧。</div>';
}

function libraryParams(overrides = {}) {
  const values = {
    q: $('#libQuery').value.trim(), subject: $('#filterSubject').value,
    notebook: $('#filterNotebook').value, error_type: $('#filterError').value,
    status: state.library.status, sort: $('#filterSort').value,
    limit: state.library.limit, offset: (state.library.page - 1) * state.library.limit,
    ...overrides,
  };
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== '' && value !== null && value !== undefined) params.set(key, value); });
  return params;
}

async function loadLibrary() {
  const result = await api(`/api/mistakes?${libraryParams()}`);
  state.library.items = result.items;
  state.library.total = result.total;
  renderLibrary();
}

function renderLibrary() {
  const grid = $('#libraryGrid');
  if (!state.library.items.length) {
    grid.innerHTML = '<div class="empty"><div class="empty-icon">✓</div><h2>这里还很干净</h2><p>试试调整筛选条件，或录入一道新的错题。</p><button class="btn btn-primary" data-action="new">＋ 录入错题</button></div>';
  } else {
    grid.innerHTML = state.library.items.map(item => {
      const title = item.title || plainText(item.question).slice(0, 36) || '未命名错题';
      const tags = [...(item.knowledge_points || []), ...(item.tags || [])].slice(0, 4);
      return `<article class="card mistake-card" data-detail="${item.id}">
        <input class="mistake-check" type="checkbox" data-select="${item.id}" ${state.selected.has(item.id) ? 'checked' : ''} aria-label="选择错题">
        <div class="mistake-top"><span class="pill">${escapeHTML(item.subject)}</span><span class="pill coral">${escapeHTML(item.error_type)}</span><span class="star ${item.is_starred ? 'on' : ''}">★</span></div>
        <h2 class="mistake-title">${escapeHTML(title)}</h2><div class="mistake-excerpt">${escapeHTML(plainText(item.question))}</div>
        <div class="tag-list">${tags.map(tag => `<span class="tiny-tag">${escapeHTML(tag)}</span>`).join('')}</div>
        <div class="mastery-line"><div class="mastery-head"><span>掌握度</span><b>${item.mastery}%</b></div><div class="mastery-track"><div class="mastery-fill" style="width:${item.mastery}%"></div></div></div>
        <div class="mistake-foot"><span>难度 ${'●'.repeat(item.difficulty)}${'○'.repeat(5-item.difficulty)}</span><span>下次 ${formatDate(item.next_review_at)}</span></div>
      </article>`;
    }).join('');
  }
  const pages = Math.max(1, Math.ceil(state.library.total / state.library.limit));
  $('#pagination').innerHTML = `<button class="btn btn-sm btn-secondary" data-page="${state.library.page - 1}" ${state.library.page <= 1 ? 'disabled' : ''}>← 上一页</button><span>第 ${state.library.page} / ${pages} 页 · 共 ${state.library.total} 题</span><button class="btn btn-sm btn-secondary" data-page="${state.library.page + 1}" ${state.library.page >= pages ? 'disabled' : ''}>下一页 →</button>`;
  renderBulkBar();
}

function renderBulkBar() {
  $('#selectedCount').textContent = state.selected.size;
  $('#bulkBar').classList.toggle('show', state.selected.size > 0);
}

function resetEditor(item = null) {
  $('#editorForm').reset();
  $('#editId').value = item?.id || '';
  $('#editorTitle').textContent = item ? '编辑错题' : '录入错题';
  const mapping = {
    editTitle: 'title', editSubject: 'subject', editGrade: 'grade', editQuestion: 'question',
    editMyAnswer: 'my_answer', editAnswer: 'answer', editAnalysis: 'analysis', editErrorType: 'error_type',
    editQuestionType: 'question_type', editDifficulty: 'difficulty', editMastery: 'mastery',
    editNotebook: 'notebook', editSource: 'source', editNote: 'note',
  };
  Object.entries(mapping).forEach(([id, field]) => { $(`#${id}`).value = item?.[field] ?? (id === 'editNotebook' ? '默认错题本' : id === 'editDifficulty' ? 3 : id === 'editMastery' ? 0 : ''); });
  $('#editKnowledge').value = (item?.knowledge_points || []).join(', ');
  $('#editTags').value = (item?.tags || []).join(', ');
  $('#editStarred').checked = Boolean(item?.is_starred);
  state.editImageUrls = [...(item?.image_urls || [])];
  renderEditImages();
}

function renderEditImages() {
  $('#editImageStrip').innerHTML = state.editImageUrls.map((url, index) => `<div class="preview" style="min-width:140px"><img src="${escapeHTML(url)}"><button type="button" data-remove-edit-image="${index}">×</button></div>`).join('');
}

function editorPayload() {
  const split = value => value.split(/[,，]/).map(part => part.trim()).filter(Boolean);
  return {
    title: $('#editTitle').value.trim(), subject: $('#editSubject').value.trim() || '未分类', grade: $('#editGrade').value.trim(),
    question: $('#editQuestion').value.trim(), my_answer: $('#editMyAnswer').value.trim(), answer: $('#editAnswer').value.trim(),
    analysis: $('#editAnalysis').value.trim(), error_type: $('#editErrorType').value, question_type: $('#editQuestionType').value.trim(),
    difficulty: Number($('#editDifficulty').value), mastery: Number($('#editMastery').value), knowledge_points: split($('#editKnowledge').value),
    tags: split($('#editTags').value), notebook: $('#editNotebook').value.trim() || '默认错题本', source: $('#editSource').value.trim(),
    note: $('#editNote').value.trim(), is_starred: $('#editStarred').checked, image_urls: state.editImageUrls,
  };
}

async function uploadMedia(file) {
  const data = new FormData(); data.append('file', file);
  return api('/api/media', { method: 'POST', body: data });
}

async function compressImage(file, maxSide = 2560, quality = 0.88) {
  if (!file.type.startsWith('image/')) throw new Error('仅支持图片文件');
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
  const context = canvas.getContext('2d', { alpha: false });
  context.fillStyle = '#fff'; context.fillRect(0, 0, width, height);
  context.imageSmoothingEnabled = true; context.imageSmoothingQuality = 'high'; context.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();
  let blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
  if (!blob) throw new Error('浏览器压缩图片失败');
  if (blob.size > 2_600_000 && quality > .7) blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .76));
  const name = `${file.name.replace(/\.[^.]+$/, '') || 'question'}.jpg`;
  return new File([blob], name, { type: 'image/jpeg', lastModified: file.lastModified });
}

async function normalizeDetectedImageFile(file, rotateClockwise = false, deskewDegrees = 0, quality = 0.9) {
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const upright = document.createElement('canvas');
  upright.width = rotateClockwise ? bitmap.height : bitmap.width;
  upright.height = rotateClockwise ? bitmap.width : bitmap.height;
  const uprightContext = upright.getContext('2d', { alpha: false });
  uprightContext.fillStyle = '#fff'; uprightContext.fillRect(0, 0, upright.width, upright.height);
  uprightContext.imageSmoothingEnabled = true; uprightContext.imageSmoothingQuality = 'high';
  if (rotateClockwise) {
    uprightContext.translate(upright.width, 0); uprightContext.rotate(Math.PI / 2);
  }
  uprightContext.drawImage(bitmap, 0, 0); bitmap.close();
  let output = upright;
  if (Math.abs(deskewDegrees) >= 0.01) {
    output = document.createElement('canvas'); output.width = upright.width; output.height = upright.height;
    const context = output.getContext('2d', { alpha: false });
    context.fillStyle = '#fff'; context.fillRect(0, 0, output.width, output.height);
    context.translate(output.width / 2, output.height / 2);
    context.rotate(Number(deskewDegrees) * Math.PI / 180);
    context.drawImage(upright, -upright.width / 2, -upright.height / 2);
  }
  const blob = await new Promise(resolve => output.toBlob(resolve, 'image/jpeg', quality));
  if (!blob) throw new Error('浏览器旋转图片失败');
  const name = `${file.name.replace(/\.[^.]+$/, '') || 'question'}-upright.jpg`;
  return new File([blob], name, { type: 'image/jpeg', lastModified: file.lastModified });
}

async function saveEditor(event) {
  event.preventDefault();
  const button = $('#saveMistake'); button.disabled = true; button.textContent = '保存中…';
  try {
    const images = [...$('#editImages').files];
    for (const file of images) state.editImageUrls.push((await uploadMedia(file)).url);
    const payload = editorPayload();
    if (!payload.question && !payload.image_urls.length) throw new Error('请填写题目内容或上传题目图片');
    const id = $('#editId').value;
    await api(id ? `/api/mistakes/${id}` : '/api/mistakes', { method: id ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    closeModal('editorModal'); toast(id ? '错题已更新' : '错题已收入题库');
    await Promise.all([loadOptions(), loadDashboard()]);
    if (state.view === 'library') await loadLibrary();
  } catch (error) { toast(error.message, 'error'); }
  finally { button.disabled = false; button.textContent = '保存错题'; }
}

async function showDetail(id) {
  const result = await api(`/api/mistakes/${id}`);
  const item = result.item; state.detail = item;
  $('#detailTitle').textContent = item.title || '错题详情';
  $('#detailBody').innerHTML = `<div class="detail-grid"><div class="detail-main">
    ${item.image_urls.length ? `<div class="image-strip">${item.image_urls.map(url => `<a href="${escapeHTML(url)}" target="_blank"><img src="${escapeHTML(url)}"></a>`).join('')}</div>` : ''}
    <h3>题目</h3><div class="rich rich-box">${sanitizeHTML(item.question) || '（图片题）'}</div>
    ${item.my_answer ? `<h3>原错误作答</h3><div class="rich rich-box">${sanitizeHTML(item.my_answer)}</div>` : ''}
    <h3>正确答案</h3><div class="rich rich-box">${sanitizeHTML(item.answer) || '（暂无）'}</div>
    ${item.analysis ? `<h3>解析与思路</h3><div class="rich rich-box">${sanitizeHTML(item.analysis)}</div>` : ''}
    ${item.note ? `<h3>复盘笔记</h3><div class="rich rich-box">${sanitizeHTML(item.note)}</div>` : ''}
  </div><aside class="detail-aside">
    ${detailProp('学科 / 年级', [item.subject,item.grade].filter(Boolean).join(' · '))}${detailProp('错因', item.error_type)}${detailProp('题型', item.question_type || '未填写')}${detailProp('难度', `${item.difficulty} / 5`)}${detailProp('掌握度', `${item.mastery}%`)}${detailProp('知识点', (item.knowledge_points || []).join(' · ') || '未填写')}${detailProp('标签', (item.tags || []).join(' · ') || '未填写')}${detailProp('复习次数', `${item.review_count} 次`)}${detailProp('下次复习', formatDate(item.next_review_at, true))}${detailProp('错题本', item.notebook)}${detailProp('来源', item.source || '未填写')}
  </aside></div>`;
  $('#detailArchive').textContent = item.status === 'archived' ? '恢复学习' : '归档';
  scheduleMath($('#detailBody'));
  openModal('detailModal');
}

function detailProp(label, value) { return `<div class="detail-prop"><small>${label}</small><b>${escapeHTML(value)}</b></div>`; }

async function deleteDetail() {
  if (!state.detail || !confirm('确定永久删除这道错题吗？此操作不可撤销。')) return;
  await api(`/api/mistakes/${state.detail.id}`, { method: 'DELETE' });
  state.selected.delete(state.detail.id); closeModal('detailModal'); toast('错题已删除');
  await Promise.all([loadLibrary(), loadDashboard(), loadOptions()]);
}

async function duplicateDetail() {
  if (!state.detail) return;
  const copy = { ...state.detail, title: `${state.detail.title || '错题'}（副本）` };
  delete copy.id; delete copy.created_at; delete copy.updated_at; delete copy.review_count; delete copy.correct_streak;
  await api('/api/mistakes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(copy) });
  closeModal('detailModal'); toast('已复制到错题库'); await loadLibrary();
}

async function archiveDetail() {
  if (!state.detail) return;
  const status = state.detail.status === 'archived' ? 'active' : 'archived';
  await api(`/api/mistakes/${state.detail.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) });
  closeModal('detailModal'); toast(status === 'active' ? '已恢复学习' : '已归档'); await Promise.all([loadLibrary(), loadDashboard()]);
}

async function batchAction(action) {
  if (!state.selected.size) return;
  if (action === 'delete' && !confirm(`确定永久删除选中的 ${state.selected.size} 道错题吗？`)) return;
  await api('/api/mistakes/batch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: [...state.selected], action, value: action === 'star' ? true : null }) });
  if (action === 'delete') state.selected.clear();
  toast(`批量操作完成：${state.selected.size || '所选'} 题`); await Promise.all([loadLibrary(), loadDashboard(), loadOptions()]);
}

function addCaptureFiles(list) {
  [...list].forEach(file => { if (file.type.startsWith('image/') && state.captureFiles.length < 8) state.captureFiles.push(file); });
  renderCapturePreviews();
}

function renderCapturePreviews() {
  $('#capturePreviews').innerHTML = state.captureFiles.map((file, index) => `<div class="preview"><img src="${URL.createObjectURL(file)}"><button data-remove-capture="${index}">×</button></div>`).join('');
}

async function runCapture() {
  if (!state.captureFiles.length) return toast('请先选择题目图片', 'error');
  if (state.captureMode === 'solve' && $('#solveMode').value === 'across' && state.captureFiles.length < 2) return toast('跨页模式至少需要两张图片', 'error');
  if (state.captureMode === 'search' && !state.config.configured && !$('#smartSegments').checked) return toast('请先配置 AI 连接', 'error');
  if (state.captureMode === 'solve' && !state.config.configured) return toast('请先配置 AI 连接', 'error');
  const button = $('#captureRun'); button.disabled = true;
  $('#captureProgress').classList.remove('hidden'); $('#captureResults').innerHTML = '';
  state.regionDraft = null;
  try {
    const preparedFiles = [];
    for (let index = 0; index < state.captureFiles.length; index++) {
      const original = state.captureFiles[index];
      $('#captureStatus').textContent = `正在压缩第 ${index + 1} / ${state.captureFiles.length} 张图片…`;
      preparedFiles.push(await compressImage(original));
    }
    if (state.captureMode === 'solve') {
      await uploadPreparedThenSolve(preparedFiles);
    } else if ($('#smartSegments').checked) {
      await prepareRegionDraft(preparedFiles);
    } else {
      if (!state.config.configured) return toast('请先配置 AI 连接', 'error');
      await uploadPreparedThenSearch(preparedFiles, false);
    }
  } catch (error) { toast(error.message, 'error'); $('#captureStatus').textContent = error.message; }
  finally { button.disabled = false; $('#captureProgress').classList.add('hidden'); }
}

async function uploadPreparedThenSolve(preparedFiles) {
  const remoteIds = [];
  for (let index = 0; index < preparedFiles.length; index++) {
    const file = preparedFiles[index];
    $('#captureStatus').textContent = `正在上传第 ${index + 1} / ${preparedFiles.length} 张图片…`;
    const form = new FormData(); form.append('file', file);
    remoteIds.push((await api('/api/upload', { method: 'POST', body: form })).fileId);
  }
  await captureSolve(remoteIds);
}

async function uploadPreparedThenSearch(preparedFiles, useRows) {
  const remoteIds = [];
  for (let index = 0; index < preparedFiles.length; index++) {
    const file = preparedFiles[index];
    $('#captureStatus').textContent = `正在上传第 ${index + 1} / ${preparedFiles.length} 张图片…`;
    const form = new FormData(); form.append('file', file);
    remoteIds.push((await api('/api/upload', { method: 'POST', body: form })).fileId);
  }
  await captureSearch(remoteIds, preparedFiles, useRows);
}

async function prepareRegionDraft(preparedFiles) {
  const pages = [];
  const useAi = $('#segmentDetector').value === 'ai';
  for (let index = 0; index < preparedFiles.length; index++) {
    const file = preparedFiles[index];
    $('#captureStatus').textContent = `${useAi ? 'Gemini 正在观察' : '本地 OCR 正在扫描'}第 ${index + 1} / ${preparedFiles.length} 页…`;
    const form = new FormData(); form.append('file', file);
    if (useAi) form.append('model', selectedAIModel());
    const detected = await api(useAi ? '/api/question-regions/ai' : '/api/question-regions', { method: 'POST', body: form });
    const needsTransform = detected.rotated || Math.abs(Number(detected.deskewAngle || 0)) >= 0.01;
    const uprightFile = needsTransform ? await normalizeDetectedImageFile(file, detected.rotated, detected.deskewAngle) : file;
    const width = detected.width || detected.image?.width || 1;
    const height = detected.height || detected.image?.height || 1;
    const regions = (detected.regions || []).map((region, order) => normalizeDraftRegion(region, order, width, height));
    pages.push({
      file: uprightFile,
      objectUrl: URL.createObjectURL(uprightFile),
      width,
      height,
      warnings: detected.warnings || [],
      detector: detected.detector || 'rapidocr-option-skeleton',
      regions,
    });
    if (detected.rotated || detected.deskewAngle) toast(`第 ${index + 1} 页已自动转正${detected.deskewAngle ? `并纠偏 ${Math.abs(detected.deskewAngle).toFixed(1)}°` : ''}`);
    if (detected.warnings?.length) toast(detected.warnings[0], 'error');
  }
  if (!pages.some(page => page.regions.length)) {
    if (!state.config.configured) throw new Error('没有识别到题框，且尚未配置 139 搜题');
    toast('没有识别到稳定题框，已改走整页搜题', 'error');
    await uploadPreparedThenSearch(preparedFiles, false);
    return;
  }
  state.regionDraft = { pages, pageIndex: 0, selectedId: pages[0].regions[0]?.id || null };
  state.regionHistory = [];
  renderRegionEditor();
  toast(`已拆出 ${pages.reduce((sum, page) => sum + page.regions.length, 0)} 个题框，请核对后再搜题`);
}

function normalizeDraftRegion(region, order, width, height) {
  const bbox = Array.isArray(region.bbox) ? region.bbox.map(Number) : [width * 0.03, height * (0.08 + order * 0.08), width * 0.97, height * (0.16 + order * 0.08)];
  const [x1, y1, x2, y2] = clampBBox(bbox, width, height);
  return {
    id: `r-${order}-${Math.round(y1)}-${Math.round(y2)}`,
    detectedNumber: Number(region.detectedNumber || order + 1),
    bbox: [x1, y1, x2, y2],
    inferred: Boolean(region.inferred),
    score: Number(region.score || 0),
    detector: region.detector || 'rapidocr-option-skeleton',
  };
}

function clampBBox([x1, y1, x2, y2], width, height) {
  const minWidth = Math.min(8, width), minHeight = Math.min(12, height);
  const left = Math.max(0, Math.min(width - minWidth, Math.min(x1, x2)));
  const top = Math.max(0, Math.min(height - minHeight, Math.min(y1, y2)));
  const right = Math.max(left + minWidth, Math.min(width, Math.max(x1, x2)));
  const bottom = Math.max(top + minHeight, Math.min(height, Math.max(y1, y2)));
  return [left, top, right, bottom];
}

function currentDraftPage() {
  return state.regionDraft?.pages[state.regionDraft.pageIndex] || null;
}

function sortDraftRegions(page) {
  page.regions.sort((a, b) => a.bbox[1] - b.bbox[1] || a.bbox[0] - b.bbox[0]);
  page.regions.forEach((region, index) => { region.detectedNumber = index + 1; });
}

function pushRegionHistory() {
  if (!state.regionDraft) return;
  state.regionHistory.push({
    pageIndex: state.regionDraft.pageIndex,
    selectedId: state.regionDraft.selectedId,
    pages: state.regionDraft.pages.map(page => page.regions.map(region => ({ ...region, bbox: [...region.bbox] }))),
  });
  if (state.regionHistory.length > 40) state.regionHistory.shift();
}

function undoRegionEdit() {
  const snapshot = state.regionHistory.pop();
  if (!snapshot || !state.regionDraft) return toast('没有可撤销的操作');
  snapshot.pages.forEach((regions, index) => { state.regionDraft.pages[index].regions = regions; });
  state.regionDraft.pageIndex = snapshot.pageIndex;
  state.regionDraft.selectedId = snapshot.selectedId;
  renderRegionEditor();
}

function renderRegionEditor() {
  const draft = state.regionDraft;
  if (!draft) return;
  const page = currentDraftPage();
  const total = draft.pages.reduce((sum, item) => sum + item.regions.length, 0);
  const warn = page.warnings.length ? ` · ${page.warnings.length} 条提示` : '';
  const selected = page.regions.find(region => region.id === draft.selectedId);
  $('#captureResults').innerHTML = `
    <article class="card region-card">
      <div class="card-head">
        <div>
          <h2 class="card-title">已拆出 ${total} 道题</h2>
          <div class="card-note">先核对题框，确认后再逐题搜。章节标题不要单独成框。${warn}</div>
        </div>
        <div class="region-page-nav">${draft.pages.map((_, index) => `<button class="btn btn-sm ${index === draft.pageIndex ? 'btn-primary' : 'btn-secondary'}" data-region-page="${index}">第 ${index + 1} 页</button>`).join('')}</div>
      </div>
      <div class="region-toolbar">
        <button class="btn btn-sm btn-secondary" data-region-action="add">＋ 新框</button>
        <button class="btn btn-sm btn-secondary" data-region-action="split">拆分</button>
        <button class="btn btn-sm btn-secondary" data-region-action="merge">合并下一题</button>
        <button class="btn btn-sm btn-secondary" data-region-action="delete">删除</button>
        <button class="btn btn-sm btn-secondary" data-region-action="sort">按位置排序</button>
        <button class="btn btn-sm btn-secondary" data-region-action="undo" ${state.regionHistory.length ? '' : 'disabled'}>↶ 撤销</button>
        <button class="btn btn-sm btn-primary" id="confirmRegions">确认并逐题搜</button>
      </div>
      <div class="region-editor-layout">
        <div>
          <div class="region-stage" id="regionStage">
            <img id="regionImage" src="${page.objectUrl}" alt="试卷预览">
            <div id="regionOverlay" class="region-overlay"></div>
          </div>
          <div class="card-note region-tip">拖动框体可移动；拖动四边或四角可缩放；在空白处按住拖动可直接画框。</div>
        </div>
        <aside class="region-panel">
          <b>${selected ? `正在编辑第 ${selected.detectedNumber} 题` : '请选择题框'}</b>
          <div class="region-nudges">
            <button class="btn btn-sm btn-secondary" data-region-action="nudge-up">↑</button>
            <button class="btn btn-sm btn-secondary" data-region-action="nudge-down">↓</button>
            <button class="btn btn-sm btn-secondary" data-region-action="nudge-left">←</button>
            <button class="btn btn-sm btn-secondary" data-region-action="nudge-right">→</button>
            <button class="btn btn-sm btn-secondary" data-region-action="expand">放大</button>
            <button class="btn btn-sm btn-secondary" data-region-action="contract">缩小</button>
          </div>
          <div class="region-list">${page.regions.map(region => `<button class="region-list-item ${region.id === draft.selectedId ? 'active' : ''}" data-region-select="${region.id}"><span>第 ${region.detectedNumber} 题</span><small>${Math.round((region.bbox[3] - region.bbox[1]) / page.height * 100)}% 高</small></button>`).join('')}</div>
          <div class="card-note">快捷键：方向键微调，Delete 删除，Ctrl/⌘+Z 撤销。</div>
        </aside>
      </div>
      <div class="card-note" style="margin-top:10px">当前检测：${page.detector}${warn}</div>
    </article>`;
  bindRegionOverlay();
  paintRegionBoxes();
}

function paintRegionBoxes() {
  const page = currentDraftPage();
  const overlay = $('#regionOverlay');
  const image = $('#regionImage');
  if (!page || !overlay || !image) return;
  const colors = ['#db5639', '#2e8c74', '#3467b8', '#b07a24', '#8040a0', '#1e78a0', '#b4466e', '#468232', '#5a5a5a', '#145aa0', '#965028'];
  overlay.innerHTML = page.regions.map(region => {
    const [x1, y1, x2, y2] = region.bbox;
    const selected = region.id === state.regionDraft.selectedId ? ' selected' : '';
    const color = colors[(region.detectedNumber - 1) % colors.length];
    return `<div class="region-box${selected}" data-region-id="${region.id}" style="left:${(x1 / page.width) * 100}%;top:${(y1 / page.height) * 100}%;width:${((x2 - x1) / page.width) * 100}%;height:${((y2 - y1) / page.height) * 100}%;--region-color:${color}">
      <span class="region-label">${region.detectedNumber}${region.inferred ? '~' : ''}</span>
      ${['n','e','s','w','nw','ne','se','sw'].map(handle => `<i class="region-handle ${handle}" data-handle="${handle}"></i>`).join('')}
    </div>`;
  }).join('');
}

function bindRegionOverlay() {
  const overlay = $('#regionOverlay');
  const image = $('#regionImage');
  if (!overlay || !image) return;
  const pointer = event => {
    const rect = overlay.getBoundingClientRect();
    const page = currentDraftPage();
    return {
      x: ((event.clientX - rect.left) / rect.width) * page.width,
      y: ((event.clientY - rect.top) / rect.height) * page.height,
    };
  };
  overlay.onpointerdown = event => {
    const page = currentDraftPage();
    if (!page) return;
    const handle = event.target.closest('[data-handle]');
    const box = event.target.closest('[data-region-id]');
    const point = pointer(event);
    if (handle && box) {
      const region = page.regions.find(item => item.id === box.dataset.regionId);
      state.regionDraft.selectedId = region.id;
      state.regionDrag = { type: handle.dataset.handle, id: region.id, startX: point.x, startY: point.y, bbox: [...region.bbox], recorded: false };
      overlay.setPointerCapture(event.pointerId);
      paintRegionBoxes();
      return;
    }
    if (box) {
      state.regionDraft.selectedId = box.dataset.regionId;
      const region = page.regions.find(item => item.id === box.dataset.regionId);
      state.regionDrag = { type: 'move', id: region.id, startX: point.x, startY: point.y, bbox: [...region.bbox], recorded: false };
      overlay.setPointerCapture(event.pointerId);
      paintRegionBoxes();
      return;
    }
    pushRegionHistory();
    const id = `r-new-${Date.now()}`;
    page.regions.push({ id, detectedNumber: page.regions.length + 1, bbox: [point.x, point.y, point.x + 8, point.y + 12], inferred: true, score: 0, detector: 'manual' });
    state.regionDraft.selectedId = id;
    state.regionDrag = { type: 'draw', id, startX: point.x, startY: point.y, bbox: [...page.regions.at(-1).bbox], recorded: true };
    overlay.setPointerCapture(event.pointerId);
    paintRegionBoxes();
  };
  overlay.onpointermove = event => {
    const drag = state.regionDrag;
    const page = currentDraftPage();
    if (!drag || !page) return;
    const region = page.regions.find(item => item.id === drag.id);
    if (!region) return;
    const point = pointer(event);
    if (!drag.recorded) { pushRegionHistory(); drag.recorded = true; }
    const next = [...drag.bbox];
    if (drag.type === 'draw') {
      next[0] = drag.startX; next[1] = drag.startY; next[2] = point.x; next[3] = point.y;
    } else if (drag.type === 'move') {
      const dx = point.x - drag.startX, dy = point.y - drag.startY;
      const boxWidth = drag.bbox[2] - drag.bbox[0], boxHeight = drag.bbox[3] - drag.bbox[1];
      next[0] = Math.max(0, Math.min(page.width - boxWidth, drag.bbox[0] + dx));
      next[1] = Math.max(0, Math.min(page.height - boxHeight, drag.bbox[1] + dy));
      next[2] = next[0] + boxWidth; next[3] = next[1] + boxHeight;
    } else {
      if (drag.type.includes('n')) next[1] = point.y;
      if (drag.type.includes('s')) next[3] = point.y;
      if (drag.type.includes('w')) next[0] = point.x;
      if (drag.type.includes('e')) next[2] = point.x;
    }
    region.bbox = clampBBox(next, page.width, page.height);
    paintRegionBoxes();
  };
  overlay.onpointerup = () => { state.regionDrag = null; };
  overlay.onpointercancel = () => { state.regionDrag = null; };
}

function regionAction(action) {
  const page = currentDraftPage();
  if (!page) return;
  if (action === 'undo') return undoRegionEdit();
  const selected = page.regions.find(item => item.id === state.regionDraft.selectedId);
  const mutating = ['add','delete','split','merge','sort','nudge-up','nudge-down','nudge-left','nudge-right','expand','contract'];
  if (mutating.includes(action)) pushRegionHistory();
  if (action === 'add') {
    const last = page.regions.at(-1);
    const y1 = last ? last.bbox[3] + page.height * 0.006 : page.height * 0.08;
    const region = { id: `r-add-${Date.now()}`, detectedNumber: page.regions.length + 1, bbox: [page.width * 0.03, y1, page.width * 0.97, Math.min(page.height, y1 + page.height * 0.07)], inferred: true, score: 0, detector: 'manual' };
    page.regions.push(region);
    state.regionDraft.selectedId = region.id;
  } else if (action === 'delete' && selected) {
    page.regions = page.regions.filter(item => item.id !== selected.id);
    state.regionDraft.selectedId = page.regions[0]?.id || null;
  } else if (action === 'split' && selected) {
    const [x1, y1, x2, y2] = selected.bbox;
    const mid = (y1 + y2) / 2;
    selected.bbox = [x1, y1, x2, mid];
    page.regions.push({ id: `r-split-${Date.now()}`, detectedNumber: selected.detectedNumber + 1, bbox: [x1, mid, x2, y2], inferred: true, score: 0, detector: 'manual' });
  } else if (action === 'merge' && selected) {
    sortDraftRegions(page);
    const index = page.regions.findIndex(item => item.id === selected.id);
    const next = page.regions[index + 1];
    if (!next) return toast('没有下一题可合并', 'error');
    selected.bbox = clampBBox([Math.min(selected.bbox[0], next.bbox[0]), Math.min(selected.bbox[1], next.bbox[1]), Math.max(selected.bbox[2], next.bbox[2]), Math.max(selected.bbox[3], next.bbox[3])], page.width, page.height);
    page.regions.splice(index + 1, 1);
  } else if (action === 'sort') {
    sortDraftRegions(page);
  } else if (selected && action.startsWith('nudge-')) {
    const stepX = page.width * 0.008, stepY = page.height * 0.008;
    const dx = action === 'nudge-left' ? -stepX : action === 'nudge-right' ? stepX : 0;
    const dy = action === 'nudge-up' ? -stepY : action === 'nudge-down' ? stepY : 0;
    const [x1, y1, x2, y2] = selected.bbox;
    const width = x2 - x1, height = y2 - y1;
    const left = Math.max(0, Math.min(page.width - width, x1 + dx));
    const top = Math.max(0, Math.min(page.height - height, y1 + dy));
    selected.bbox = [left, top, left + width, top + height];
  } else if (selected && (action === 'expand' || action === 'contract')) {
    const amountX = page.width * 0.012 * (action === 'expand' ? 1 : -1);
    const amountY = page.height * 0.012 * (action === 'expand' ? 1 : -1);
    selected.bbox = clampBBox([selected.bbox[0] - amountX, selected.bbox[1] - amountY, selected.bbox[2] + amountX, selected.bbox[3] + amountY], page.width, page.height);
  }
  if (action !== 'sort') sortDraftRegions(page);
  renderRegionEditor();
}

async function confirmRegionDraft() {
  if (!state.config.configured) return toast('请先配置 AI 连接后再搜题', 'error');
  const draft = state.regionDraft;
  if (!draft) return;
  draft.pages.forEach(sortDraftRegions);
  const button = $('#confirmRegions');
  if (button) button.disabled = true;
  $('#captureProgress').classList.remove('hidden');
  try {
    const allRows = [];
    for (const page of draft.pages) {
      const rows = await cropDraftRegions(page);
      allRows.push(...rows.map((row, index) => ({ ...row, detectedNumber: allRows.length + index + 1 })));
    }
    $('#captureStatus').textContent = `正在并发识别 0 / ${allRows.length}…`;
    const questions = await mapConcurrent(allRows, 3, async row => {
      try {
        const form = new FormData(); form.append('file', row.file);
        const uploaded = await api('/api/upload', { method: 'POST', body: form });
        const searched = await api('/api/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fileId: uploaded.fileId }) });
        const best = chooseBestCandidate(searched.data?.questions || []) || {};
        best.detectedNumber = row.detectedNumber;
        best.cropDataUrl = row.dataUrl;
        best.cropImageUrl = row.dataUrl;
        if (!plainText(best.questionContent || '') || candidateScore(best) < 45) {
          best.questionContent = `<div><b>第 ${row.detectedNumber} 题</b>（139 未返回可用题干）</div>`;
          best.questionAnswer = best.questionAnswer || '';
        }
        return best;
      } catch (error) {
        return {
          detectedNumber: row.detectedNumber,
          cropDataUrl: row.dataUrl,
          cropImageUrl: row.dataUrl,
          questionContent: `<div><b>第 ${row.detectedNumber} 题</b>（139 搜题失败，未保存本地题图）</div><p>${escapeHTML(error.message)}</p>`,
          questionAnswer: '',
          questionAnalysis: '',
        };
      }
    }, (done, total) => { $('#captureStatus').textContent = `正在并发识别 ${done} / ${total}…`; });
    renderRecognized(questions);
  } catch (error) {
    toast(error.message, 'error');
    $('#captureStatus').textContent = error.message;
  } finally {
    if (button) button.disabled = false;
    $('#captureProgress').classList.add('hidden');
  }
}

async function cropDraftRegions(page) {
  const bitmap = await createImageBitmap(page.file);
  const scaleX = bitmap.width / page.width;
  const scaleY = bitmap.height / page.height;
  const rows = [];
  for (const region of page.regions) {
    const [x1, y1, x2, y2] = region.bbox;
    const left = Math.max(0, Math.round(x1 * scaleX));
    const top = Math.max(0, Math.round(y1 * scaleY));
    const width = Math.max(24, Math.round((x2 - x1) * scaleX));
    const height = Math.max(24, Math.round((y2 - y1) * scaleY));
    const renderScale = Math.min(1.45, 2800 / width, 900 / height);
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(width * renderScale);
    canvas.height = Math.round(height * renderScale);
    const context = canvas.getContext('2d', { alpha: false });
    context.fillStyle = '#fff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    context.drawImage(bitmap, left, top, width, height, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.92));
    rows.push({
      detectedNumber: region.detectedNumber,
      file: new File([blob], `question-${region.detectedNumber}.jpg`, { type: 'image/jpeg' }),
      dataUrl: canvas.toDataURL('image/jpeg', 0.82),
    });
  }
  bitmap.close();
  return rows;
}

async function captureSearch(fileIds, preparedFiles, useRows = false) {
  $('#captureStatus').textContent = '正在识别并匹配题库…';
  const pages = await mapConcurrent(fileIds, 3, async fileId => {
    const result = await api('/api/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fileId }) });
    return result.data?.questions || [];
  }, (done, total) => { $('#captureStatus').textContent = `正在并发识别 ${done} / ${total} 页…`; });
  renderRecognized(pages.flat());
}

function renderRecognized(questions) {
  const unique = deduplicateQuestions(questions);
  if (!unique.length) {
    $('#captureResults').innerHTML = '<div class="card empty"><div class="empty-icon">?</div><h2>暂未识别到题目</h2><p>可以切换“AI 解原题”模式再试一次。</p></div>';
    return;
  }
  $('#captureResults').innerHTML = `<div class="card-head"><div><h2 class="card-title">识别到 ${unique.length} 道题</h2><div class="card-note">已按题框裁剪并搜题，请核对后收入错题库</div></div><button class="btn btn-primary btn-sm" id="saveAllRecognized">全部收入</button></div>` + unique.map((q, index) => recognizedCard(q, index)).join('');
  state.recognized = unique;
  scheduleMath($('#captureResults'));
  toast(`成功识别 ${unique.length} 道题`);
}

async function detectQuestionRows(file) {
  const form = new FormData(); form.append('file', file);
  const detected = await api('/api/question-regions', { method: 'POST', body: form });
  const needsTransform = detected.rotated || Math.abs(Number(detected.deskewAngle || 0)) >= 0.01;
  const uprightFile = needsTransform ? await normalizeDetectedImageFile(file, detected.rotated, detected.deskewAngle) : file;
  const page = {
    file: uprightFile,
    width: detected.width || detected.image?.width || 1,
    height: detected.height || detected.image?.height || 1,
    regions: (detected.regions || []).map((region, order) => normalizeDraftRegion(region, order, detected.width || 1, detected.height || 1)),
  };
  return cropDraftRegions(page);
}

window.__mistakeBookTest = { detectQuestionRows, chooseBestCandidate, deduplicateQuestions };

function chooseBestCandidate(candidates) {
  if (!candidates.length) return null;
  return [...candidates].sort((a, b) => candidateScore(b) - candidateScore(a))[0];
}

function candidateScore(question) {
  const text = plainText(question.questionContent || '');
  const answer = plainText(question.questionAnswer || '');
  let score = Math.min(text.length, 260) + Math.min(answer.length, 100) * .25 + Number(question.matchScore || 0);
  if (/证明[:：]?\s*[.。]?$/u.test(text)) score -= 250;
  if (text.length < 8) score -= 80;
  if (/<img\b/i.test(question.questionContent || '') && text.length < 8) score -= 20;
  return score;
}

function deduplicateQuestions(questions) {
  const result = [], keys = new Set();
  for (const question of questions) {
    const text = plainText(question.questionContent || '').replace(/\s+/g, '').slice(0, 120);
    const detected = question.detectedNumber || '';
    const printed = text.match(/^(\d{1,2})[.、．]/)?.[1] || '';
    const key = detected ? `row:${detected}` : printed ? `n:${printed}` : `t:${text.slice(0, 36)}`;
    if (keys.has(key) && text.length > 8) continue;
    keys.add(key); result.push(question);
  }
  return result;
}

function recognizedCard(question, index) {
  const points = (question.knowledgePoints || []).map(point => point.name || point).filter(Boolean);
  return `<article class="result-item"><div class="result-item-head"><div><b>第 ${question.detectedNumber || index + 1} 题</b> <span class="pill">${escapeHTML(question.subject || '未分类')}</span></div><button class="btn btn-sm btn-primary" data-save-recognized="${index}">＋ 收入错题本</button></div>${question.cropImageUrl ? `<img src="${question.cropImageUrl}" style="width:100%;max-height:210px;object-fit:contain;margin-top:10px;border-radius:9px;background:#f5f7f6">` : ''}<div class="rich rich-box">${sanitizeHTML(question.questionContent) || '（题目见上方裁剪图）'}</div><details style="margin-top:10px"><summary style="cursor:pointer;color:var(--teal)">查看答案与解析</summary><div class="rich rich-box"><b>答案：</b>${sanitizeHTML(question.questionAnswer) || '暂无'}<hr class="divider"><b>解析：</b>${sanitizeHTML(question.questionAnalysis) || '暂无'}</div></details><div class="tag-list">${points.map(point => `<span class="tiny-tag">${escapeHTML(point)}</span>`).join('')}</div></article>`;
}

function recognizedToItem(question, index = 0) {
  return {
    title: plainText(question.questionContent).slice(0, 38) || `AI 识别第 ${index + 1} 题`,
    question: question.questionContent || '', answer: question.questionAnswer || '', analysis: question.questionAnalysis || '',
    subject: typeof question.subject === 'string' ? question.subject : '未分类', source: 'AI 拍题识别',
    question_type: question.questionType || '', difficulty: 3, error_type: '知识盲区',
    knowledge_points: (question.knowledgePoints || []).map(point => point.name || point).filter(Boolean),
    tags: ['AI识别'], image_urls: [], notebook: '默认错题本',
  };
}

async function saveRecognized(index, editFirst = true) {
  const item = recognizedToItem(state.recognized[index], index);
  if (editFirst) { resetEditor(item); openModal('editorModal'); return; }
  await api('/api/mistakes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
}

async function saveAllRecognized() {
  const button = $('#saveAllRecognized'); button.disabled = true;
  try {
    for (let index = 0; index < state.recognized.length; index++) await saveRecognized(index, false);
    toast(`${state.recognized.length} 道题已全部收入错题库`); await Promise.all([loadDashboard(), loadOptions()]);
  } catch (error) { toast(error.message, 'error'); }
  finally { button.disabled = false; }
}

async function captureSolve(fileIds) {
  $('#captureStatus').textContent = 'AI 正在阅读题目并生成解析…';
  $('#captureResults').innerHTML = '<article class="result-item"><div class="result-item-head"><b>AI 解题结果</b><button class="btn btn-sm btn-primary hidden" id="saveSolveResult">＋ 收入错题本</button></div><div class="rich rich-box" id="solveOutput">正在思考…</div></article>';
  const response = await fetch('/api/solve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fileIds, names: state.captureFiles.map(file => file.name), mode: $('#solveMode').value, enableModelThinking: $('#deepThink').checked }) });
  if (!response.ok) { let info; try { info = await response.json(); } catch { info = {}; } throw new Error(info.detail || `AI 解题失败（${response.status}）`); }
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', output = '';
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n'); buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      try {
        const event = JSON.parse(line.slice(5));
        if (event.data?.flowResult?.outContent) output += event.data.flowResult.outContent;
        if (!event.success && event.message) output += `\n\n> ${event.message}`;
      } catch { /* 忽略上游心跳 */ }
    }
    $('#solveOutput').innerHTML = simpleMarkdown(output || '正在思考…');
  }
  state.solveOutput = output;
  $('#saveSolveResult').classList.remove('hidden');
  toast('AI 解题完成');
}

function solveToEditor() {
  resetEditor({ title: 'AI 解原题', question: 'AI 解题模式未返回结构化 139 题干', answer: '', analysis: state.solveOutput, subject: '未分类', source: 'AI 解原题', difficulty: 3, error_type: '知识盲区', tags: ['AI解题'], knowledge_points: [], image_urls: [], notebook: '默认错题本' });
  openModal('editorModal');
}

async function loadReview(shuffle = false) {
  const result = await api('/api/mistakes?due=true&status=active&sort=review_asc&limit=500');
  state.review.items = result.items;
  if (shuffle) state.review.items.sort(() => Math.random() - .5);
  state.review.index = 0; state.review.revealed = false;
  renderReview();
}

function renderReview() {
  const { items, index, revealed } = state.review;
  if (!items.length) {
    $('#reviewArea').innerHTML = '<div class="card empty"><div class="empty-icon">✓</div><h2>今日复习完成</h2><p>没有到期错题。可以去错题库自由浏览，或继续整理新题。</p><button class="btn btn-primary" data-view-jump="library">浏览错题库</button></div>'; return;
  }
  if (index >= items.length) {
    $('#reviewArea').innerHTML = `<div class="card empty"><div class="empty-icon">★</div><h2>本轮复习完成</h2><p>已完成 ${items.length} 道错题的主动回忆，做得不错。</p><button class="btn btn-primary" data-view-jump="dashboard">返回概览</button></div>`; return;
  }
  const item = items[index];
  $('#reviewArea').innerHTML = `<div class="review-progress"><b>${index + 1} / ${items.length}</b><div class="progress"><span style="width:${(index + 1) / items.length * 100}%"></span></div><button class="btn btn-sm btn-ghost" data-detail="${item.id}">详情</button></div><article class="card review-card"><div class="review-meta"><span class="pill">${escapeHTML(item.subject)}</span><span class="pill coral">${escapeHTML(item.error_type)}</span>${(item.knowledge_points || []).map(point => `<span class="tiny-tag">${escapeHTML(point)}</span>`).join('')}</div>${item.image_urls?.length ? `<div class="image-strip">${item.image_urls.map(url => `<img src="${escapeHTML(url)}">`).join('')}</div>` : ''}<div class="review-question rich">${sanitizeHTML(item.question) || '题目见图片'}</div>${revealed ? `<div class="answer-panel"><b>正确答案</b><div class="rich" style="margin-top:7px">${sanitizeHTML(item.answer) || '暂无答案'}</div>${item.analysis ? `<hr class="divider"><b>解析</b><div class="rich" style="margin-top:7px">${sanitizeHTML(item.analysis)}</div>` : ''}</div><p style="text-align:center;margin:18px 0 4px;color:var(--muted)">这道题你掌握得怎么样？</p><div class="review-actions"><button class="rating-btn" data-rating="0"><b>忘记了</b><small>明天再复习</small></button><button class="rating-btn" data-rating="1"><b>有点难</b><small>2 天后</small></button><button class="rating-btn" data-rating="2"><b>掌握了</b><small>4 天后</small></button><button class="rating-btn" data-rating="3"><b>很轻松</b><small>7 天后</small></button></div>` : `<button class="btn btn-primary" id="revealAnswer" style="display:flex;margin:35px auto 0">查看答案</button>`}</article>`;
  scheduleMath($('#reviewArea'));
}

async function rateReview(rating) {
  const item = state.review.items[state.review.index];
  await api(`/api/mistakes/${item.id}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rating: Number(rating) }) });
  state.review.index++; state.review.revealed = false; renderReview();
  loadDashboard();
}

function exportOptions() {
  const vertical = Number($('#marginVertical').value), horizontal = Number($('#marginHorizontal').value);
  return {
    title: $('#exportTitle').value.trim() || '我的错题集', subtitle: $('#exportSubtitle').value.trim(), paper: $('#paperSize').value,
    orientation: $('#orientation').value, margin_top: vertical, margin_bottom: vertical, margin_left: horizontal, margin_right: horizontal,
    font_size: Number($('#fontSize').value), line_spacing: Number($('#lineSpacing').value), blank_lines: Number($('#blankLines').value),
    answer_mode: $('#answerMode').value, include_analysis: $('#includeAnalysis').checked, include_my_answer: $('#includeMyAnswer').checked,
    include_note: $('#includeNote').checked, include_meta: $('#includeMeta').checked, page_break_each: $('#pageBreakEach').checked, page_numbers: $('#pageNumbers').checked,
  };
}

function exportRequest() {
  const scope = $('#exportScope').value;
  let ids = [], filters = {};
  if (scope === 'selected') ids = [...state.selected];
  else if (scope === 'all') filters = { status: 'active' };
  else if (scope === 'due') filters = { status: 'active', due: true };
  else if (scope === 'starred') filters = { status: 'active', starred: true };
  else filters = Object.fromEntries(libraryParams({ limit: '', offset: '' }));
  return { ids, filters, options: exportOptions() };
}

async function refreshExportItems() {
  const request = exportRequest();
  if (request.ids.length) {
    const details = await Promise.all(request.ids.slice(0, 8).map(id => api(`/api/mistakes/${id}`)));
    state.exportItems = details.map(result => result.item);
    $('#exportCount').textContent = `准备导出 ${request.ids.length} 题`;
  } else if ($('#exportScope').value === 'selected') {
    state.exportItems = []; $('#exportCount').textContent = '尚未选择题目，请先在错题库勾选';
  } else {
    const params = new URLSearchParams({ ...request.filters, limit: 8 });
    const result = await api(`/api/mistakes?${params}`);
    state.exportItems = result.items; $('#exportCount').textContent = `准备导出 ${result.total} 题`;
  }
  renderPaperPreview();
}

function renderPaperPreview() {
  const opt = exportOptions(), items = state.exportItems.slice(0, 2), paper = $('#paperPreview');
  paper.classList.toggle('landscape', opt.orientation === 'landscape');
  paper.style.fontSize = `${Math.max(8, opt.font_size * .85)}px`;
  paper.style.lineHeight = opt.line_spacing;
  if (!items.length) { paper.innerHTML = `<div class="paper-title">${escapeHTML(opt.title)}</div><div class="paper-subtitle">${escapeHTML(opt.subtitle)}</div><div class="empty" style="padding:120px 10px">选择错题后将在此预览</div>`; return; }
  paper.innerHTML = `<div class="paper-title">${escapeHTML(opt.title)}</div><div class="paper-subtitle">${escapeHTML(opt.subtitle)} · 共 ${$('#exportCount').textContent.match(/\d+/)?.[0] || items.length} 道</div>` + items.map((item, index) => `<section><div class="paper-item-title">${index + 1}. ${escapeHTML(item.title || '错题')}</div>${opt.include_meta ? `<div class="paper-meta">${escapeHTML([item.subject, item.grade, `难度 ${item.difficulty}/5`, ...(item.tags || [])].filter(Boolean).join(' · '))}</div>` : ''}<div class="paper-question">${escapeHTML(plainText(item.question) || '题目图片见原记录')}</div>${'<div class="answer-line"></div>'.repeat(opt.blank_lines)}${opt.include_my_answer && item.my_answer ? `<div class="paper-answer"><b>原作答：</b>${escapeHTML(plainText(item.my_answer))}</div>` : ''}${opt.answer_mode === 'inline' ? `<div class="paper-answer"><b>答案：</b>${escapeHTML(plainText(item.answer))}${opt.include_analysis && item.analysis ? `<br><b>解析：</b>${escapeHTML(plainText(item.analysis))}` : ''}</div>` : ''}</section>`).join('') + (opt.answer_mode === 'separate' ? '<div class="paper-item-title" style="margin-top:35px">参考答案见文末答案册</div>' : '');
  scheduleMath(paper);
}

const EXPORT_JOBS_STORAGE_KEY = 'mistake_book_export_jobs_v1';

function saveExportJobs() {
  localStorage.setItem(EXPORT_JOBS_STORAGE_KEY, JSON.stringify(state.exportJobs));
}

function exportTypeName(type) {
  return type === 'pdf' ? 'PDF' : type === 'docx' ? '可编辑 Word' : type === 'aiword' ? 'AI 识别 Word' : 'OCR Word';
}

function renderExportJobStatus() {
  const panel = $('#exportJobStatus');
  const jobs = Object.values(state.exportJobs).filter(Boolean);
  if (panel) panel.classList.toggle('hidden', !jobs.length);
  if (panel) panel.innerHTML = jobs.map(job => {
    const finished = job.status === 'completed';
    const failed = job.status === 'failed';
    const action = finished
      ? `<button class="btn btn-sm btn-secondary export-job-action" data-download-export-job="${escapeHTML(job.id)}">下载</button>`
      : failed
        ? job.type === 'aiword' ? '<span class="export-job-action muted">请重新选择图片</span>' : `<button class="btn btn-sm btn-secondary export-job-action" data-retry-export="${escapeHTML(job.type)}">重试</button>`
        : '<span class="export-job-action muted">后台处理中</span>';
    return `<div class="export-job-row ${failed ? 'failed' : ''}"><div class="export-job-name">${escapeHTML(exportTypeName(job.type))}</div><div class="export-job-detail"><div class="export-job-message"><span>${escapeHTML(job.message || '等待处理')}</span><span>${Number(job.progress) || 0}%</span></div><div class="export-job-progress"><span style="width:${Math.max(0, Math.min(100, Number(job.progress) || 0))}%"></span></div></div>${action}</div>`;
  }).join('');

  const aiStatus = $('#captureAiWordStatus');
  const aiJob = state.exportJobs.aiword;
  if (aiStatus) {
    aiStatus.className = `notice ${aiJob?.status === 'completed' ? 'success' : aiJob?.status === 'failed' ? 'error' : ''}${aiJob ? '' : ' hidden'}`;
    aiStatus.textContent = aiJob ? `${aiJob.message || 'AI Word 处理中'}${aiJob.status === 'running' || aiJob.status === 'queued' ? ` · ${Number(aiJob.progress) || 0}%` : ''}` : '';
  }

  const activeTypes = new Set(jobs.filter(job => ['queued', 'running'].includes(job.status)).map(job => job.type));
  if ($('#downloadDocx')) $('#downloadDocx').disabled = activeTypes.has('camscanner');
  if ($('#downloadPdf')) $('#downloadPdf').disabled = activeTypes.has('pdf');
  if ($('#downloadDocx')) $('#downloadDocx').textContent = activeTypes.has('camscanner') ? 'OCR Word 后台处理中' : 'W OCR Word';
  if ($('#downloadPdf')) $('#downloadPdf').textContent = activeTypes.has('pdf') ? 'PDF 后台处理中' : '▣ 导出 PDF';
}

function triggerExportDownload(job) {
  const link = document.createElement('a');
  link.href = job.download_url || `/api/export/jobs/${encodeURIComponent(job.id)}/download`;
  link.download = job.filename || '';
  document.body.appendChild(link);
  link.click();
  link.remove();
  job.downloaded = true;
  saveExportJobs();
  const fallbackMessage = job.fallback === 'ai-fallback-image'
    ? 'AI 暂时不可用，已保留原图生成 Word'
    : job.fallback
      ? 'CamScanner 暂不可用，已生成保留公式与配图的版式 Word'
      : `${exportTypeName(job.type)} 已生成`;
  toast(fallbackMessage);
}

async function pollExportJob(type, jobId) {
  let connectionFailures = 0;
  while (state.exportJobs[type]?.id === jobId) {
    try {
      const response = await fetch(`/api/export/jobs/${encodeURIComponent(jobId)}`);
      if (response.status === 404) {
        state.exportJobs[type] = { ...state.exportJobs[type], status: 'failed', progress: 100, message: '后台任务已失效，请重新导出' };
        saveExportJobs(); renderExportJobStatus(); return;
      }
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '查询导出进度失败');
      connectionFailures = 0;
      const downloaded = state.exportJobs[type]?.downloaded;
      state.exportJobs[type] = { ...result.job, downloaded };
      saveExportJobs(); renderExportJobStatus();
      if (result.job.status === 'completed') {
        if (!downloaded) triggerExportDownload(state.exportJobs[type]);
        return;
      }
      if (result.job.status === 'failed') {
        toast(`${exportTypeName(type)} 导出失败：${result.job.message}`, 'error');
        return;
      }
    } catch (error) {
      connectionFailures++;
      if (connectionFailures >= 5) {
        state.exportJobs[type] = { ...state.exportJobs[type], message: '暂时无法查询进度，将继续重试' };
        saveExportJobs(); renderExportJobStatus();
      }
    }
    await new Promise(resolve => setTimeout(resolve, connectionFailures ? 3000 : 1000));
  }
}

function resumeExportJobs() {
  try {
    state.exportJobs = JSON.parse(localStorage.getItem(EXPORT_JOBS_STORAGE_KEY) || '{}') || {};
  } catch (_) {
    state.exportJobs = {};
  }
  renderExportJobStatus();
  Object.entries(state.exportJobs).forEach(([type, job]) => {
    if (job?.id && ['queued', 'running'].includes(job.status)) pollExportJob(type, job.id);
  });
}

async function downloadExport(type) {
  const request = exportRequest();
  if ($('#exportScope').value === 'selected' && !request.ids.length) return toast('请先在错题库选择要导出的题目', 'error');
  const button = type === 'pdf' ? $('#downloadPdf') : $('#downloadDocx');
  if (['queued', 'running'].includes(state.exportJobs[type]?.status)) return toast(`${exportTypeName(type)} 已在后台处理中`);
  const old = button.textContent; button.disabled = true; button.textContent = '正在提交…';
  try {
    const result = await api(`/api/export/jobs/${type}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) });
    state.exportJobs[type] = result.job;
    saveExportJobs(); renderExportJobStatus();
    toast(`${exportTypeName(type)} 已提交后台处理，可以继续使用错题本`);
    pollExportJob(type, result.job.id);
  } catch (error) { toast(error.message, 'error'); }
  finally {
    if (!['queued', 'running'].includes(state.exportJobs[type]?.status)) {
      button.disabled = false; button.textContent = old;
    }
  }
}

async function runAiWordExport(triggerButton = null) {
  if (['queued', 'running'].includes(state.exportJobs.aiword?.status)) return toast('AI 识别 Word 已在后台处理中');
  if (!state.captureFiles.length && triggerButton?.id === 'aiWordRun') return toast('请先选择要识别的整页图片', 'error');
  if (!state.captureFiles.length && !$('#exportScope')) return toast('请先选择要识别的整页图片', 'error');
  const button = triggerButton || $('#aiWordRun') || $('#exportAiWord');
  const buttonLabel = button?.id === 'exportAiWord' ? '✦ AI 识别 Word' : '▣ AI 识别文字与图片，生成 Word';
  if (button) { button.disabled = true; button.textContent = '正在提交 AI OCR…'; }
  const form = new FormData();
  if (state.captureFiles.length) {
    state.captureFiles.forEach(file => form.append('files', file, file.name));
  } else {
    const request = exportRequest();
    if ($('#exportScope')?.value === 'selected' && !request.ids.length) {
      if (button) { button.disabled = false; button.textContent = buttonLabel; }
      return toast('请先在错题库选择要导出的题目', 'error');
    }
    let sourceItems;
    if (request.ids.length) {
      sourceItems = (await Promise.all(request.ids.slice(0, 50).map(id => api(`/api/mistakes/${id}`)))).map(result => result.item);
    } else {
      const params = new URLSearchParams({ ...request.filters, limit: 50 });
      sourceItems = (await api(`/api/mistakes?${params}`)).items;
    }
    if (!sourceItems?.length) {
      if (button) { button.disabled = false; button.textContent = buttonLabel; }
      return toast('当前范围没有可导出的错题', 'error');
    }
    form.append('items', JSON.stringify(sourceItems));
  }
  const aiModel = selectedAIModel();
  form.append('title', $('#exportTitle')?.value?.trim() || 'AI 识别文档');
  form.append('options', JSON.stringify({
    title: $('#exportTitle')?.value?.trim() || 'AI 识别文档',
    subtitle: 'AI OCR · 文字可编辑 · 图片已裁切',
    paper: $('#paperSize')?.value || 'A4', orientation: $('#orientation')?.value || 'portrait',
    margin_top: Number($('#marginVertical')?.value || 18), margin_bottom: Number($('#marginVertical')?.value || 18),
    margin_left: Number($('#marginHorizontal')?.value || 18), margin_right: Number($('#marginHorizontal')?.value || 18),
    font_size: Number($('#fontSize')?.value || 11), line_spacing: Number($('#lineSpacing')?.value || 1.5), page_numbers: true,
    answer_mode: $('#answerMode')?.value || 'inline',
    include_analysis: $('#includeAnalysis')?.checked ?? true,
    include_my_answer: $('#includeMyAnswer')?.checked ?? true,
    include_note: $('#includeNote')?.checked ?? true,
    include_meta: $('#includeMeta')?.checked ?? true,
    ai_model: aiModel,
  }));
  try {
    const result = await api('/api/ai-word/jobs', { method: 'POST', body: form });
    state.exportJobs.aiword = result.job; saveExportJobs(); renderExportJobStatus();
    toast('AI 识别 Word 已提交后台处理'); pollExportJob('aiword', result.job.id);
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    if (button) { button.disabled = false; button.textContent = buttonLabel; }
  }
}

async function saveConfig() {
  try {
    await api('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: $('#token').value, userDomainId: $('#uid').value }) });
    localStorage.setItem('mistake_uid', $('#uid').value); toast('AI 配置已保存'); await loadConfig();
  } catch (error) { toast(error.message, 'error'); }
}

async function saveCamScanner() {
  try {
    await api('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      camScannerS2: $('#camScannerS2').value,
      camScannerCssu: $('#camScannerCssu').value,
      camScannerCsste: $('#camScannerCsste').value,
    }) });
    ['#camScannerS2', '#camScannerCssu', '#camScannerCsste'].forEach(selector => { $(selector).value = ''; });
    toast('CamScanner 凭证已加密保存'); await loadConfig();
  } catch (error) { toast(error.message, 'error'); }
}

async function importConfig() {
  try {
    const file = $('#configFile').files[0];
    if (file) { const form = new FormData(); form.append('file', file); await api('/api/config/import-file', { method: 'POST', body: form }); }
    else {
      const value = $('#configPaste').value.trim(); if (!value) throw new Error('请选择配置文件或粘贴 JSON');
      const parsed = JSON.parse(value); await api('/api/config/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(parsed) });
    }
    toast('配置导入成功'); await loadConfig();
  } catch (error) { toast(error.message.includes('JSON') ? 'JSON 格式不正确' : error.message, 'error'); }
}

function bindEvents() {
  document.addEventListener('click', async event => {
    const viewButton = event.target.closest('[data-view], [data-view-jump]');
    if (viewButton) { await switchView(viewButton.dataset.view || viewButton.dataset.viewJump); return; }
    const close = event.target.closest('[data-close]'); if (close) { closeModal(close.dataset.close); return; }
    if (event.target.classList.contains('modal-backdrop')) closeModal(event.target.id);
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (action === 'new') { resetEditor(); openModal('editorModal'); }
    if (action === 'export-selected') { $('#exportScope').value = 'selected'; await switchView('export'); }
    const detail = event.target.closest('[data-detail]'); if (detail && !event.target.closest('[data-select]')) await showDetail(Number(detail.dataset.detail));
    const selector = event.target.closest('[data-select]');
    if (selector) { event.stopPropagation(); const id = Number(selector.dataset.select); selector.checked ? state.selected.add(id) : state.selected.delete(id); renderBulkBar(); }
    const page = event.target.closest('[data-page]'); if (page && !page.disabled) { state.library.page = Number(page.dataset.page); await loadLibrary(); }
    const batch = event.target.closest('[data-batch]'); if (batch) await batchAction(batch.dataset.batch);
    const removeCapture = event.target.closest('[data-remove-capture]'); if (removeCapture) { state.captureFiles.splice(Number(removeCapture.dataset.removeCapture), 1); renderCapturePreviews(); }
    const removeEdit = event.target.closest('[data-remove-edit-image]'); if (removeEdit) { state.editImageUrls.splice(Number(removeEdit.dataset.removeEditImage), 1); renderEditImages(); }
    const saveResult = event.target.closest('[data-save-recognized]'); if (saveResult) await saveRecognized(Number(saveResult.dataset.saveRecognized));
    const rating = event.target.closest('[data-rating]'); if (rating) await rateReview(rating.dataset.rating);
  });
  $('#mobileMenu').onclick = () => $('#sidebar').classList.toggle('open');
  $('#editorForm').addEventListener('submit', saveEditor);
  $('#detailDelete').onclick = deleteDetail; $('#detailDuplicate').onclick = duplicateDetail; $('#detailArchive').onclick = archiveDetail;
  $('#detailEdit').onclick = () => { const item = state.detail; closeModal('detailModal'); resetEditor(item); openModal('editorModal'); };
  $('#captureDrop').onclick = () => $('#captureFiles').click();
  $('#captureFiles').onchange = event => { addCaptureFiles(event.target.files); event.target.value = ''; };
  $('#captureDrop').ondragover = event => { event.preventDefault(); $('#captureDrop').classList.add('drag'); };
  $('#captureDrop').ondragleave = () => $('#captureDrop').classList.remove('drag');
  $('#captureDrop').ondrop = event => { event.preventDefault(); $('#captureDrop').classList.remove('drag'); addCaptureFiles(event.dataTransfer.files); };
  $$('.mode-option').forEach(button => button.onclick = () => {
    state.captureMode = button.dataset.captureMode;
    $$('.mode-option').forEach(node => node.classList.toggle('active', node === button));
    $('#solveOptions').classList.toggle('hidden', state.captureMode !== 'solve');
    $('#segmentDetectorField').classList.toggle('hidden', state.captureMode !== 'search' || !$('#smartSegments').checked);
  });
  $('#smartSegments').onchange = event => $('#segmentDetectorField').classList.toggle('hidden', !event.target.checked || state.captureMode !== 'search');
  $('#captureRun').onclick = runCapture;
  $('#captureResults').addEventListener('click', event => {
    if (event.target.closest('#saveAllRecognized')) saveAllRecognized();
    if (event.target.closest('#saveSolveResult')) solveToEditor();
    if (event.target.closest('#confirmRegions')) confirmRegionDraft();
    const pageButton = event.target.closest('[data-region-page]');
    if (pageButton && state.regionDraft) { state.regionDraft.pageIndex = Number(pageButton.dataset.regionPage); renderRegionEditor(); }
    const regionSelect = event.target.closest('[data-region-select]');
    if (regionSelect && state.regionDraft) { state.regionDraft.selectedId = regionSelect.dataset.regionSelect; renderRegionEditor(); }
    const actionButton = event.target.closest('[data-region-action]');
    if (actionButton) regionAction(actionButton.dataset.regionAction);
  });
  const libraryChanged = debounce(() => { state.library.page = 1; loadLibrary(); }, 260);
  $('#libQuery').addEventListener('input', libraryChanged);
  ['filterSubject','filterNotebook','filterError','filterSort'].forEach(id => $(`#${id}`).addEventListener('change', () => { state.library.page = 1; loadLibrary(); }));
  $$('.filter-tab').forEach(button => button.onclick = () => { state.library.status = button.dataset.status; $$('.filter-tab').forEach(node => node.classList.toggle('active', node === button)); state.library.page = 1; loadLibrary(); });
  $('#globalSearch').addEventListener('keydown', event => { if (event.key === 'Enter') { $('#libQuery').value = event.target.value; switchView('library'); } });
  document.addEventListener('keydown', event => {
    const editingRegion = state.regionDraft && $('#regionOverlay') && !event.target.matches('input,textarea,select');
    if (editingRegion && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); undoRegionEdit(); return; }
    if (editingRegion && event.key === 'Delete') { event.preventDefault(); regionAction('delete'); return; }
    if (editingRegion && ['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key)) {
      event.preventDefault();
      regionAction(`nudge-${event.key.slice(5).toLowerCase()}`);
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('#globalSearch').focus(); }
    if (event.key === 'Escape') $$('.modal-backdrop.open').forEach(node => closeModal(node.id));
  });
  $('#shuffleReview').onclick = () => loadReview(true);
  $('#reviewArea').addEventListener('click', event => { if (event.target.closest('#revealAnswer')) { state.review.revealed = true; renderReview(); } });
  $('#exportScope').onchange = refreshExportItems;
  $$('.export-control').forEach(control => {
    control.addEventListener('input', debounce(renderPaperPreview, 100));
    control.addEventListener('change', renderPaperPreview);
  });
  $('#downloadDocx').onclick = () => downloadExport('camscanner'); $('#downloadPdf').onclick = () => downloadExport('pdf');
  $('#aiWordRun').onclick = () => runAiWordExport($('#aiWordRun'));
  $('#exportAiWord').onclick = async () => {
    await runAiWordExport($('#exportAiWord'));
  };
  $('#exportJobStatus').addEventListener('click', event => {
    const download = event.target.closest('[data-download-export-job]');
    if (download) {
      const job = Object.values(state.exportJobs).find(item => item?.id === download.dataset.downloadExportJob);
      if (job) triggerExportDownload(job);
      return;
    }
    const retry = event.target.closest('[data-retry-export]');
    if (retry) downloadExport(retry.dataset.retryExport);
  });
  $('#saveConfig').onclick = saveConfig; $('#saveCamScanner').onclick = saveCamScanner; $('#importConfig').onclick = importConfig;
  $('#exportConfig').onclick = () => { window.location = '/api/config/export'; };
  $('#downloadBackup').onclick = () => { window.location = '/api/backup'; };
}

async function init() {
  bindEvents();
  resumeExportJobs();
  $('#uid').value = localStorage.getItem('mistake_uid') || '';
  try {
    await Promise.all([loadConfig(), loadOptions(), loadDashboard(), loadAIModels()]);
    const initial = location.hash.slice(1) || 'dashboard'; await switchView(initial, false);
  } catch (error) { toast(`初始化失败：${error.message}`, 'error'); }
}

init();
