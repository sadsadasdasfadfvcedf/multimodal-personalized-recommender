const $ = id => document.getElementById(id);
const grid = $('pgrid'), stream = $('stream');
let busy = false, needReal = false, currentItems = [], picked = new Map(), ratings = new Map();
let activeFilters = [], sortMode = 'match', viewMode = 'grid', userSaid = [];
let pageSize = 8, lastQuery = null;   // null = 首页货架；字符串 = 最近一次文字需求
let liveTimer = null, liveItem = null, liveEventId = 0, liveBusy = false;
let currentLimit = 8, loadingMore = false, hasMore = true;
let focusItemId = null;   // 手势反馈默认目标：最近打开的商品，否则货架 Top1
let detailItemId = null;
let visionBusy = false;
const pendingItems = new Set();

/* ============ 术语翻译（表 3-3「说人话」） ============ */
const INTENT_ZH = {discover:'随便看看', similar:'找相似', filter:'条件筛选',
                   compare:'商品对比', feedback:'反馈', explain:'求解释', browse:'浏览', chitchat:'对话',
                   chat:'问答'};
const STATUS_ZH = {ok:'完成', degraded:'降级返回', clarification:'需要补充信息',
                   conflict:'条件冲突', empty:'没有结果', no_evidence:'无可核验依据',
                   error:'出错了', warn:'未识别'};
const STATUS_IC = {ok:'✓', degraded:'⚑', clarification:'?', conflict:'!',
                   empty:'∅', no_evidence:'⊘', error:'✕', warn:'!'};
const SRC_ZH = {'historical_mode':'历史成交价', 'category_median':'类目参考价',
                'catalog_default':'默认参考价', 'missing':'无价格记录'};
const MODE_ZH = {offline_snapshot:'内置离线快照', local_artifacts:'推荐系统发布制品',
                 http:'推荐系统实时接口', offline_stub:'离线场景桩', real:'真实模型'};

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const humanize = s => String(s ?? '')
  .replace(/\bhistorical_mode\b/g, '历史成交价')
  .replace(/\bcategory_median\b/g, '类目参考价')
  .replace(/\bcatalog_default\b/g, '默认参考价')
  .replace(/\boffline_snapshot\b/g, '内置离线快照')
  .replace(/\blocal_artifacts\b/g, '推荐系统发布制品')
  .replace(/\boffline_stub\b/g, '离线场景桩');
const escHuman = s => esc(humanize(s));
const zh = (map, k, d) => map[k] || d || k || '';
const money = v => v == null ? '—' : '£' + v;
/* 匹配度：把模型原始分映射成用户可读刻度（表 3-3） */
const matchPct = s => s == null ? null : Math.max(1, Math.min(99, Math.round(Number(s) * 100)));
const intentZh = r => zh(INTENT_ZH, ((r.intent_result||{}).intent), '—');
const statusZh = r => zh(STATUS_ZH, r.status || 'ok');

function media(item, cls, eager){
  const emoji = item.emoji || '🛍️';
  const load = eager ? ' loading="eager" fetchpriority="high"' : ' loading="lazy"';
  return '<div class="' + cls + '"><span class="ph">' + emoji + '</span>' +
    (item.img_url ? '<img' + load + ' src="' + esc(item.img_url) + '" alt="" onerror="this.remove()">' : '') +
    '</div>';
}
function badge(status, extra){
  const s = status || 'ok';
  return '<span class="st ' + s + '"><i></i>' + (STATUS_IC[s] || '·') + ' ' + esc(statusZh({status:s})) +
         (extra ? ' ' + esc(extra) : '') + '</span>';
}
function toast(msg){
  const t = $('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(window.__tt);
  window.__tt = setTimeout(() => {
    t.classList.remove('show');
    t.textContent = '';
  }, 2600);
}
function speakInBrowser(text){
  if (!text || !('speechSynthesis' in window) || typeof SpeechSynthesisUtterance === 'undefined') return false;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = 'zh-CN';
  u.rate = 1.05;
  u.pitch = 1;
  window.speechSynthesis.speak(u);
  return true;
}

/* ============ 筛选口径 → 可移除 chip（P1-5） ============ */
function filtersFrom(resp){
  const s = ((resp || {}).intent_result || {}).slots || {};
  const out = [];
  if (s.scene && s.scene.value)      out.push({key:'scene',    label:'场景 · ' + s.scene.value,    frag:s.scene.value});
  if (s.category && s.category.value) out.push({key:'category', label:'品类 · ' + s.category.value, frag:s.category.value});
  if (s.colors && s.colors.value && s.colors.value.length)
    out.push({key:'colors', label:'颜色 · ' + s.colors.value.join('/'), frag:s.colors.value.join('/')});
  if (s.budget_max && s.budget_max.value != null)
    out.push({key:'budget', label:'预算 · ≤ £' + s.budget_max.value, frag:s.budget_max.value + '以内'});
  if (s.budget_min && s.budget_min.value != null)
    out.push({key:'budget_min', label:'预算 · ≥ £' + s.budget_min.value, frag:s.budget_min.value + '以上'});
  return out;
}
function renderFilters(){
  $('fchips').innerHTML = activeFilters.map(f =>
    '<span class="fchip">' + esc(f.label) +
    '<button data-drop="' + esc(f.key) + '" aria-label="移除 ' + esc(f.label) + '">✕</button></span>').join('');
}
function dropFilter(key){
  if (busy){ toast('正在处理，请稍后再移除条件'); return; }
  const keep = activeFilters.filter(f => f.key !== key);
  activeFilters = keep; renderFilters();
  if (!keep.length){ toast('已移除全部条件，重新为你推荐'); ask('推荐一些热门商品', {silent:true}); return; }
  const q = '只看' + keep.map(f => f.frag).join('，');
  toast('已按剩余条件重新筛选');
  ask(q, {silent:true});
}

/* ============ 商品货架 ============ */
function sorted(items){
  const a = items.slice();
  if (sortMode === 'priceAsc')  a.sort((x,y) => (x.price ?? 1e9) - (y.price ?? 1e9));
  if (sortMode === 'priceDesc') a.sort((x,y) => (y.price ?? -1) - (x.price ?? -1));
  return a;   // match：保持后端返回顺序
}
function renderShelf(items, title, subtitle, resp, append){
  if (append){
    const seen = new Set(currentItems.map(x => String(x.item_id)));
    currentItems = currentItems.concat((items || []).filter(x => !seen.has(String(x.item_id))));
  } else {
    currentItems = items || [];
    currentLimit = currentItems.length;
    hasMore = currentItems.length > 0;
    if (!focusItemId || !currentItems.some(x => String(x.item_id) === focusItemId)){
      focusItemId = currentItems.length ? String(currentItems[0].item_id) : null;
    }
  }
  // 反馈响应只带 polarity/target，不含当前生效条件；不能因此把货架筛选 chip 清空。
  if (resp && ['discover', 'similar', 'filter', 'browse'].includes((resp.intent_result || {}).intent)){
    activeFilters = filtersFrom(resp);
  }
  renderFilters();
  $('shelfTitle').textContent = title || '推荐结果';
  $('shelfCnt').textContent = currentItems.length ? ('共 ' + currentItems.length + ' 件') : '';
  grid.className = 'pgrid' + (viewMode === 'list' ? ' list' : '');
  const lm = $('loadMore');
  if (lm) lm.hidden = !hasMore || !currentItems.length;
  if (!currentItems.length){
    grid.innerHTML = '';
    const e = $('empty'); e.style.display = 'block';
    e.innerHTML = '没有符合条件的商品。<div class="examples">' +
      ['换个大一点的预算试试','去掉颜色限制','看看同类热门'].map(t => '<button data-ex="'+esc(t)+'">'+esc(t)+'</button>').join('') +
      '</div>';
    e.querySelectorAll('[data-ex]').forEach(b => b.onclick = () => ask(b.dataset.ex));
    return;
  }
  $('empty').style.display = 'none';
  grid.innerHTML = sorted(currentItems).map((it, i) => {
    const rank = it.rank || (i + 1);
    const pct = matchPct(it.score);
    const hits = ((it.evidence || {}).matched_constraints || []).length;
    const tags = [];
    if (it.evidence_count) tags.push('<span class="t ev">' + it.evidence_count + ' 条依据</span>');
    if (hits) tags.push('<span class="t hit">命中 ' + hits + ' 项条件</span>');
    else if (it.category) tags.push('<span class="t">' + esc(it.category) + '</span>');
    const extra = tags.length > 2 ? tags.slice(0,2).join('') +
      '<span class="t more">+' + (tags.length - 2) + '</span>' : tags.join('');
    const isPicked = picked.has(it.item_id);
    return '<article class="pcard' + (isPicked ? ' picked' : '') + '" data-item="' + esc(it.item_id) + '">' +
      '<div class="rank' + (rank > 3 ? ' plain' : '') + '">TOP ' + rank + '</div>' +
      media(it, 'pimg', rank <= 2).replace('class="pimg"', 'class="pimg" data-open') +
      '<div class="pacts">' +
        '<button data-like="up" class="' + (ratings.get(it.item_id) === 'up' ? 'on' : '') + '" title="喜欢" aria-label="喜欢">👍</button>' +
        '<button data-like="down" class="' + (ratings.get(it.item_id) === 'down' ? 'on' : '') + '" title="不喜欢" aria-label="不喜欢">👎</button>' +
      '</div>' +
      '<div class="pbody">' +
        '<h3 data-open>' + esc(it.title) + '</h3>' +
        '<div class="prow"><span class="pprice">' + money(it.price) + '</span>' +
          (pct == null ? '' : '<span class="pmatch" title="模型原始分 ' + Number(it.score).toFixed(4) + '">' +
            pct + '% 匹配</span>') +
        '</div>' +
        '<div class="ptags">' + extra + '</div>' +
        '<button class="cmpbtn" data-cmp>' + (isPicked ? '✓ 已加入对比' : '加入对比') + '</button>' +
      '</div></article>';
  }).join('');
}

async function loadMore(){
  if (loadingMore || !hasMore || !currentItems.length) return;
  loadingMore = true;
  const btn = $('loadMore');
  btn.hidden = false; btn.disabled = true; btn.textContent = '正在加载更多商品…';
  const target = Math.min(120, currentLimit + 12);
  const text = lastQuery || '推荐一些热门商品';
  try{
    const d = await post('/api/more', {text, top_k: target});
    if (d.ok){
      const before = currentItems.length;
      renderShelf(d.items || [], $('shelfTitle').textContent, '', null, true);
      currentLimit = Math.max(target, currentItems.length);
      hasMore = currentItems.length > before && (d.items || []).length >= target && currentLimit < 120;
    } else {
      hasMore = false;
    }
  }catch(e){ hasMore = false; }
  btn.disabled = false; btn.textContent = '加载更多商品'; btn.hidden = !hasMore;
  loadingMore = false;
}

/* ============ 助手对话 ============ */
function addUser(text){
  userSaid.push(text);
  stream.insertAdjacentHTML('beforeend',
    '<div class="msg user"><div class="bub">' + esc(text) + '</div></div>');
  bottom();
}
function addBot(html){
  stream.insertAdjacentHTML('beforeend', '<div class="msg"><div class="bub bot">' + html + '</div></div>');
  bottom();
}
function addSkeleton(){
  stream.insertAdjacentHTML('beforeend',
    '<div class="msg" id="sk"><div class="bub bot" style="width:100%">' +
    '<div class="skb"><span class="skdot"></span><span style="color:#8b91a6">正在理解你的需求…</span></div>' +
    '</div></div>');
  bottom();
}
function addAction(icon, title, detail){
  const id = 'act-' + Math.random().toString(36).slice(2);
  stream.insertAdjacentHTML('beforeend',
    '<div class="msg"><div class="act pending" id="' + id + '"><span aria-hidden="true">' + icon + '</span>' +
    '<div style="flex:1;min-width:0"><div class="t">' + esc(title) + '</div>' +
    '<div class="d">' + esc(detail) + '</div></div>' +
    '<span class="badge pending"><span class="spin"></span></span></div></div>');
  bottom(); return id;
}
function finishAction(id, state, label, detail){
  const el = $(id); if (!el) return;
  el.className = 'act ' + state;
  const b = el.querySelector('.badge'); b.className = 'badge ' + state; b.textContent = label;
  if (detail) el.querySelector('.d').innerHTML = detail;
  bottom();
}
function updateAction(id, detail){
  const el = $(id); if (!el) return;
  const d = el.querySelector('.d'); if (d) d.textContent = detail;
  bottom();
}
/* P1-8：失败时给至少一个可点击的下一步 */
function actionSuggestions(kind){
  const how = {voice:'请靠近麦克风、在安静环境说一句完整的话，说完停顿 1 秒。',
               vision:'把要推荐的物品举到画面中央，确保有光，再点一次。'}[kind] || '';
  return '<div class="sugg">' +
    '<button data-retry="' + kind + '">重试一次</button>' +
    '<button data-type><span>改用打字</span></button>' +
    (how ? '<button data-help="' + esc(how) + '">怎么用？</button>' : '') + '</div>';
}
function renderBot(resp, updateShelf){
  const st = resp.status || 'ok';
  if (resp.event && resp.event.text) lastQuery = resp.event.text;
  let h = '<div class="rh">' + badge(st) +
          '<span class="k">意图 <b>' + esc(intentZh(resp)) + '</b></span></div>';
  if (resp.error && (!resp.clarification || resp.error.message !== resp.clarification))
    h += '<div class="errline">' + esc(resp.error.message) + '</div>';
  if (resp.clarification) h += '<div>' + esc(resp.clarification) + '</div>';
  if (resp.answer) h += '<div style="margin-top:6px;line-height:1.65">' + escHuman(resp.answer) + '</div>';
  const ws = resp.web_search || null;
  if (ws && ws.sources && ws.sources.length){
    h += '<div style="margin-top:8px;padding:8px 10px;border:1px solid #e5e8f0;border-radius:8px;background:#fafbff;font-size:12.5px">' +
         '🌐 已联网检索 <b>' + ws.count + '</b> 条来源' +
         (ws.latency_ms != null ? ' · ' + ws.latency_ms + 'ms' : '') +
         '<div style="margin-top:5px;display:grid;gap:3px">' +
         ws.sources.map(s => '<div>[' + s.index + '] <a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' +
           esc(s.title || s.url) + '</a>' + (s.site_name ? ' · ' + esc(s.site_name) : '') + '</div>').join('') +
         '</div></div>';
  } else if (ws && ws.error){
    h += '<div class="errline" style="margin-top:6px">联网搜索失败：' + esc(ws.error) + '</div>';
  }

  const slots = ((resp.intent_result || {}).slots) || {};
  const sv = Object.entries(slots).filter(([k, v]) => v && v.value !== undefined &&
    k !== 'budget_raw' && k !== 'category_alt');
  if (sv.length) h += '<div class="slots">' + sv.map(([k, v]) =>
    '<button class="slotchip" data-slot="' + esc(String(v.value)) + '">' + esc(k) + ' · ' +
    esc(String(v.value)) + '</button>').join('') + '</div>';

  const n = (resp.recommendations || []).length;
  if (n) h += '<div style="margin-top:6px">为你找到 <b>' + n + '</b> 件商品，已更新左侧货架。</div>';
  if (resp.browse) h += '<div style="margin-top:4px">已记录浏览：<b>' + esc(resp.browse.title || '') +
    '</b>（弱反馈，仅轻微上浮）</div>';
  if (resp.feedback){
    const fb = resp.feedback;
    const verb = fb.polarity === 'positive' ? '点赞' : fb.polarity === 'negative' ? '倒赞' : '';
    h += '<div style="margin-top:4px">' +
      (fb.polarity === 'none' || fb.cleared ? '已取消反馈' : '已记录 <b>' + esc(verb) + '</b>') +
      ' → ' + esc(fb.target_title || '') + '</div>';
    if (fb.polarity !== 'none' && fb.item_id){
      const p = fb.polarity === 'positive' ? 'up' : 'down';
      h += '<button class="cmpbtn" style="margin-top:5px" data-undo-feedback="' + esc(fb.item_id) +
        '" data-feedback-polarity="' + p + '">撤销' + (p === 'up' ? '点赞' : '倒赞') + '</button>';
    }
  }
  if (resp.before_after) h += '<div style="margin-top:4px;font-size:12.5px;color:#8b91a6">排序变化：<b style="color:#1b1f30">' +
    (resp.before_after.changed ? '有' : '无') + '</b></div>';

  const pres = resp.presentation || {};
  if (pres.audio_url) h += '<audio controls preload="none" src="' + esc(pres.audio_url) + '"></audio>';
  else if (pres.spoken_text) h += '<button class="ttsbtn" data-tts="' + encodeURIComponent(pres.spoken_text) + '">🔊 立即播报</button>';

  /* P1-7：运行细节默认折叠（寒暄回答不需要召回/排序依据） */
  if (!resp.answer) {
  const t = resp.trace || {}, lat = t.latency_ms || {}, modes = t.modes || {};
  const rec = t.recall || {}, rk = t.ranking || {};
  const detail = [
    '召回来源：' + ((rec.sources || []).map(s => s.source + ' ' + s.count).join(' · ') || '—'),
    '候选：合并 ' + (rec.merged_unique ?? '—') + ' → 保留 ' + (rec.kept ?? '—') +
      ' → 硬约束过滤后 ' + (rk.after_hard_filter ?? '—') + '（淘汰 ' + (rk.hard_dropped ?? '—') + '）',
    '模式：推荐 ' + zh(MODE_ZH, modes.recommender) + ' · 多模态 ' + zh(MODE_ZH, modes.multimodal),
    '耗时：意图 ' + (lat.intent_ms ?? '—') + 'ms · 召回 ' + (lat.recall_ms ?? '—') +
      'ms · 排序 ' + (lat.rank_ms ?? '—') + 'ms · 合计 ' + (lat.total_ms ?? '—') + 'ms',
  ].join('<br>');
  h += '<div class="fold"><button data-fold>▸ 展开依据</button>' +
       '<div class="body">' + detail + '</div></div>';
  }
  addBot(h);
  if (updateShelf && n) {
    renderShelf(resp.recommendations, shelfTitle(resp), shelfSub(resp), resp);
  }
}
function shelfTitle(resp){
  const i = (resp.intent_result || {}).intent;
  if (i === 'similar') return '相似商品';
  if (i === 'filter') return '筛选结果';
  if (i === 'discover') return '为你推荐';
  if (i === 'feedback' || i === 'browse') return '为你推荐';
  return '推荐结果';
}
function shelfSub(resp){
  const s = ((resp.intent_result || {}).slots) || {};
  const p = [];
  if (s.scene && s.scene.value) p.push(s.scene.value);
  if (s.category && s.category.value) p.push(s.category.value);
  if (s.budget_max && s.budget_max.value != null) p.push('≤£' + s.budget_max.value);
  return p.join(' · ');
}
function bottom(){ stream.scrollTop = stream.scrollHeight; }

/* ============ 请求 ============ */
async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify(body || {})});
  return await r.json();
}
function setBusy(b){
  busy = b;
  document.querySelectorAll('button').forEach(x => {
    if (['voice','gesture','vision'].includes(x.dataset.act)) x.disabled = b || needReal;
    else if (x.dataset.act || x.dataset.view || x.dataset.like || x.dataset.cmp || x.dataset.retry || x.dataset.drop ||
             ['sendbtn','sendbtn2','trayGo','trayClear','userBtn','gearBtn','clearChat'].includes(x.id)) x.disabled = b;
  });
  $('liveDot').classList.toggle('busy', b);
  $('liveTxt').textContent = b ? '处理中…' : '实时';
}
async function ask(text, opt){
  opt = opt || {};
  if (!text || busy) return;
  lastQuery = text;
  if (!opt.silent) addUser(text);
  setBusy(true); addSkeleton();
  try{
    const d = await post('/api/chat', {text, top_k: pageSize});
    $('sk')?.remove();
    if (d.ok) renderBot(d.response, true);
    else addBot('<div class="errline">' + esc(d.error) + '</div>' +
                '<div class="sugg"><button data-type><span>改用打字</span></button></div>');
  }catch(e){
    $('sk')?.remove();
    addBot('<div class="errline">网络请求失败：' + esc(e.message) + '</div>' +
           '<div class="sugg"><button data-type><span>重试</span></button></div>');
  }
  setBusy(false); $('q').focus();
}

/* ============ 点赞 / 倒赞（常驻，触屏可达） ============ */
function setCardPending(itemId, on){
  const key = String(itemId);
  if (on) pendingItems.add(key); else pendingItems.delete(key);
  const card = document.querySelector('.pcard[data-item="' + key + '"]');
  if (!card) return;
  card.classList.toggle('pending', on);
  card.querySelectorAll('[data-like],[data-cmp]').forEach(b => { b.disabled = on; });
}

function syncRatingUI(itemId){
  const key = String(itemId);
  const card = document.querySelector('.pcard[data-item="' + key + '"]');
  if (!card) return;
  card.querySelectorAll('[data-like]').forEach(b => {
    b.classList.toggle('on', b.dataset.like === ratings.get(key));
  });
}

function setDetailPending(itemId, on){
  if (String(detailItemId) !== String(itemId)) return;
  const sheet = $('sheet');
  if (!sheet) return;
  sheet.classList.toggle('pending', on);
  sheet.querySelectorAll('[data-rate],[data-cmp2]').forEach(b => { b.disabled = on; });
}

async function rate(itemId, polarity, forceCancel){
  const key = String(itemId);
  if (busy || pendingItems.has(key)) return;
  const cancel = !!forceCancel || ratings.get(key) === polarity;
  focusItemId = key;
  setCardPending(key, true);
  setDetailPending(key, true);
  const previous = ratings.get(key);
  if (cancel) ratings.delete(key); else ratings.set(key, polarity);
  syncRatingUI(key);
  if (String(detailItemId) === key) setModalRating(ratings.get(key) || null);
  const id = addAction(polarity === 'up' ? '👍' : '👎',
                       cancel ? '取消反馈' : (polarity === 'up' ? '点赞' : '倒赞'),
                       cancel ? '正在撤销该商品的反馈并重新排序…' : '正在写入会话偏好并重新排序…');
  try{
    const d = await post('/api/feedback', {item_id: key, polarity: cancel ? 'none' : polarity, top_k: pageSize});
    const r = d.response || {};
    const okStatus = r.status === 'ok' || r.status === 'degraded';
    if (d.ok && okStatus){
      const fb = r.feedback || {};
      finishAction(id, 'ok', cancel ? '已取消' : '已记录', esc(cancel ? '反馈' : fb.polarity === 'positive' ? '点赞' : '倒赞') +
        '：<b>' + esc(fb.target_title || key) + '</b>');
      if (cancel){
        ratings.delete(key);
        document.querySelectorAll('[data-undo-feedback="' + key + '"]').forEach(b => b.remove());
      } else {
        ratings.set(key, fb.polarity === 'negative' ? 'down' : polarity);
      }
      syncRatingUI(key);
      if (String(detailItemId) === key) setModalRating(ratings.get(key) || null);
      renderBot(r, true);
    } else {
      const msg = (r.error && r.error.message) || r.clarification || d.error || '反馈未生效';
      finishAction(id, 'warn', '未生效', esc(msg) + actionSuggestions('feedback'));
      if (previous) ratings.set(key, previous); else ratings.delete(key);
      syncRatingUI(key);
      if (String(detailItemId) === key) setModalRating(ratings.get(key) || null);
    }
  }catch(e){
    if (previous) ratings.set(key, previous); else ratings.delete(key);
    syncRatingUI(key);
    if (String(detailItemId) === key) setModalRating(ratings.get(key) || null);
    finishAction(id, 'err', '失败', esc(e.message) + actionSuggestions('feedback'));
    toast('反馈失败：' + e.message);
  }finally{
    setCardPending(key, false);
    setDetailPending(key, false);
  }
}

/* ============ 商品对比（P1-6） ============ */
function renderTray(){
  const items = Array.from(picked.values());
  $('trayItems').innerHTML = items.map(it =>
    '<span class="titem"><span class="th">' + (it.emoji || '🛍️') +
    (it.img_url ? '<img src="' + esc(it.img_url) + '" alt="" onerror="this.remove()">' : '') + '</span>' +
    '<span class="nm">' + esc(it.title) + '</span><span class="pr">' + money(it.price) + '</span>' +
    '<button data-unpick="' + esc(it.item_id) + '" aria-label="移除">✕</button></span>').join('');
  $('tray').classList.toggle('show', items.length > 0);
  $('cmpPill').textContent = items.length;
  $('cmpPill').classList.toggle('show', items.length > 0);
  $('trayGo').disabled = items.length < 2;
  const top = $('toolbar').querySelector('[data-act="compare"]');
  if (top) top.classList.toggle('on', items.length > 0);
}
function syncPickUI(){
  document.querySelectorAll('.pcard').forEach(card => {
    const on = picked.has(card.dataset.item);
    card.classList.toggle('picked', on);
    const b = card.querySelector('[data-cmp]');
    if (b) b.textContent = on ? '✓ 已加入对比' : '加入对比';
  });
}
function toggleCompare(itemId, fallback){
  const it = currentItems.find(x => String(x.item_id) === String(itemId)) || fallback;
  if (!it) return false;
  if (picked.has(itemId)) picked.delete(itemId);
  else {
    if (picked.size >= 4){ toast('最多同时对比 4 件商品'); return false; }
    picked.set(itemId, {item_id: itemId, title: it.title, price: it.price,
                        emoji: it.emoji, img_url: it.img_url});
  }
  renderTray();
  syncPickUI();
  return true;
}
async function openCompare(){
  if (picked.size < 2){
    const need = picked.size === 0 ? '还没有选择商品' : '还需再选 1 件商品';
    toast('至少选择 2 件商品才能对比：' + need + '，请先点「加入对比」');
    addBot('<div class="slots">要对比商品，请先在商品卡上点「加入对比」，至少选择 2 件。</div>');
    return;
  }
  $('cmpModal').classList.add('show');
  $('cmpSheet').innerHTML = '<button class="x" data-close="#cmpModal" aria-label="关闭">✕</button>' +
    '<div class="sec"><span class="spin"></span> 正在读取商品字段…</div>';
  try{
    const d = await post('/api/products', {item_ids: Array.from(picked.keys())});
    if (!d.ok){ $('cmpSheet').innerHTML = '<button class="x" data-close="#cmpModal">✕</button>' +
      '<div class="sec errline">' + esc(d.error || '读取失败') + '</div>'; return; }
    const rows = [
      ['商品', p => '<img class="cmpimg" src="' + esc(p.img_url) + '" alt="" onerror="this.remove()">'],
      ['标题', p => esc(p.title)],
      ['参考价', p => '<b style="color:#dc2626">' + money(p.price) + '</b>'],
      ['类目', p => esc(p.emoji + ' ' + p.category)],
      ['价格来源', p => esc(zh(SRC_ZH, p.price_source, p.price_source || '—'))],
      ['历史成交', p => (p.transactions ?? '—') + ' 笔'],
      ['商品编号', p => esc(p.item_id)],
    ];
    const items = d.products || [];
    const body = rows.map(([label, fn]) => {
      const cells = items.map(p => fn(p));
      const diff = new Set(cells.map(c => String(c).replace(/<[^>]+>/g, ''))).size > 1;
      return '<tr><th>' + label + '</th>' + cells.map(c =>
        '<td class="' + (diff ? 'diff' : '') + '">' + c + '</td>').join('') + '</tr>';
    }).join('');
    $('cmpSheet').innerHTML =
      '<button class="x" data-close="#cmpModal" aria-label="关闭">✕</button>' +
      '<div class="sec"><h3 style="margin:0 0 10px;font-size:15px;color:#1b1f30">商品对比（' +
        items.length + ' 件）</h3>' +
      '<div style="overflow:auto"><table class="cmptable"><tr><th></th>' +
        items.map(p => '<th>' + esc(p.title) + '</th>').join('') + '</tr>' + body + '</table></div>' +
      '<div style="margin-top:10px;font-size:12.5px;color:#8b91a6">高亮单元格表示该项在两件商品间存在差异。</div>' +
      '</div>';
  }catch(e){
    $('cmpSheet').innerHTML = '<button class="x" data-close="#cmpModal">✕</button>' +
      '<div class="sec errline">' + esc(e.message) + '</div>';
  }
}

/* ============ 商品详情 ============ */
function pauseLiveGesture(){
  if (liveTimer !== null){ clearInterval(liveTimer); liveTimer = null; }
  liveItem = null;
  liveBusy = false;
}

async function stopLiveGesture(){
  // 停止前先处理一次最后的状态，避免「刚比完手势就点停止」丢掉最后一次事件
  if (liveItem !== null){
    try { await pollLiveGesture(true); } catch(e){}
  }
  const hadLiveItem = liveItem !== null;
  pauseLiveGesture();
  if (hadLiveItem){
    try { await post('/api/camera/stop'); } catch(e){}
  }
}

function setModalRating(dir){
  const up = $('sheet').querySelector('[data-rate="up"]');
  const down = $('sheet').querySelector('[data-rate="down"]');
  if (up){
    up.style.background = dir === 'up' ? '#dcfce7' : '#eef0fe';
    up.style.color = dir === 'up' ? '#166534' : '#3730a3';
    up.textContent = dir === 'up' ? '👍 已喜欢' : '👍 喜欢';
  }
  if (down){
    down.style.background = dir === 'down' ? '#fee2e2' : '#f1f2f7';
    down.style.color = dir === 'down' ? '#b91c1c' : '#5c6379';
    down.textContent = dir === 'down' ? '👎 已不喜欢' : '👎 不喜欢';
  }
}

async function handleLiveEvent(g){
  const stat = $('liveStat');
  if (g === 'thumbs_up' || g === 'thumbs_down'){
    const target = liveItem;
    if (!target) return;
    const dir = g === 'thumbs_up' ? 'up' : 'down';
    const cancel = ratings.get(String(target)) === dir;
    const r = await post('/api/feedback', {item_id: target, polarity: cancel ? 'none' : dir, top_k: pageSize});
    if (r.ok){
      if (cancel){
        ratings.delete(String(target));
        document.querySelectorAll('[data-undo-feedback="' + target + '"]').forEach(b => b.remove());
      } else {
        ratings.set(String(target), dir);
      }
      setModalRating(cancel ? null : dir);
      if (r.response) renderBot(r.response, true);
      const card = document.querySelector('.pcard[data-item="' + target + '"]');
      if (card) card.querySelectorAll('[data-like]').forEach(b => b.classList.toggle('on',
        !cancel && b.dataset.like === dir));
      if (stat) stat.textContent = cancel ? '已取消反馈，商品已恢复' : '已识别 ' + g + '，已更新当前商品';
    } else if (stat) stat.textContent = '识别到 ' + g + '，但反馈失败：' + (r.error || '');
  } else if (g){
    if (stat) stat.textContent = '已识别 ' + g + '，当前仅支持 👍/👎，已忽略';
  }
}

async function pollLiveGesture(force){
  if ((!force && liveTimer === null) || liveItem === null) return;
  try{
    const d = await (await fetch('/api/camera/state')).json();
    const s = d.state || {};
    const stat = $('liveStat');
    if (stat){
      const g = s.current_gesture;
      const conf = Math.round((s.current_confidence || 0) * 100);
      stat.textContent = (s.running === false ? '摄像头已停止' :
        ((g ? '当前：' + g + ' ' + conf + '%' : '未检测到手') + ' · ' + Number(s.fps || 0).toFixed(1) + ' fps'));
    }
    const events = Array.isArray(s.events) ? s.events : [];
    let pending = events.filter(e => Number(e.id) > liveEventId);
    if (!pending.length && s.event_id && s.event_id !== liveEventId){
      pending = [{id: s.event_id, gesture: s.last_event_gesture}];
    }
    if (!pending.length || liveBusy) return;
    liveBusy = true;
    try{
      for (const e of pending){
        liveEventId = Math.max(liveEventId, Number(e.id) || 0);
        await handleLiveEvent(e.gesture);
      }
    } finally { liveBusy = false; }
  }catch(e){ /* 轮询失败时保留上一状态 */ }
}

function renderLivePanel(itemId){
  const box = $('modalGesture'); if (!box) return;
  box.innerHTML =
    '<div class="livehead"><span>📷 手势直播</span><button data-stop-live="1">停止</button></div>' +
    '<img class="livecam" id="liveCam" src="/camera/stream?t=' + Date.now() + '" alt="手势直播画面">' +
    '<div class="livestat" id="liveStat">正在识别手势…</div>' +
    '<div class="livehint">👍 点赞 · 👎 倒赞 · 保持约 1 秒；换手势不需要把手移出画面</div>' +
    '<button class="cmpbtn" data-single-gesture="1" style="margin-top:6px">改用单次识别</button>';
  const stop = box.querySelector('[data-stop-live]');
  if (stop) stop.onclick = async () => { await stopLiveGesture(); box.innerHTML = '<div class="mstatus">已停止手势直播</div>'; };
  const single = box.querySelector('[data-single-gesture]');
  if (single) single.onclick = async () => { await stopLiveGesture(); modalGesture(itemId); };
}

async function startLiveGesture(itemId){
  const box = $('modalGesture'); if (!box) return;
  liveItem = String(itemId);
  if (liveTimer !== null){
    // 摄像头已经在直播：只切换目标商品，不重新打开设备
    try{
      const d = await (await fetch('/api/camera/state')).json();
      liveEventId = Number((d.state || {}).event_id || liveEventId);
    }catch(e){}
    renderLivePanel(itemId);
    return;
  }
  box.innerHTML = '<div class="mstatus">正在打开摄像头…</div>';
  try{
    const current = await (await fetch('/api/camera/state')).json();
    const state = current.state || {};
    if (state.running){
      liveEventId = Number(state.event_id || 0);
      renderLivePanel(itemId);
      liveTimer = setInterval(pollLiveGesture, 300);
      return;
    }
    const d = await post('/api/camera/start');
    if (!d.ok){
      liveItem = null;
      box.innerHTML = '<div class="mstatus err">' + esc(d.error || '摄像头打开失败') +
        '</div><div class="sugg"><button data-retry-live="1">重试直播</button>' +
        '<button data-single-gesture="1">改用单次识别</button></div>';
      const retry = box.querySelector('[data-retry-live]');
      if (retry) retry.onclick = () => startLiveGesture(itemId);
      const b = box.querySelector('[data-single-gesture]');
      if (b) b.onclick = () => modalGesture(itemId);
      return;
    }
    liveEventId = Number((d.state || {}).event_id || 0);
    renderLivePanel(itemId);
    liveTimer = setInterval(pollLiveGesture, 300);
  }catch(e){
    liveItem = null;
    box.innerHTML = '<div class="mstatus err">摄像头启动失败：' + esc(e.message) +
      '</div><div class="sugg"><button data-retry-live="1">重试直播</button></div>';
    const retry = box.querySelector('[data-retry-live]');
    if (retry) retry.onclick = () => startLiveGesture(itemId);
  }
}

async function modalGesture(itemId){
  const box = $('modalGesture'); if (!box || box.dataset.busy === '1') return;
  box.dataset.busy = '1';
  const status = (text, cls) => {
    box.innerHTML = '<div class="mstatus' + (cls ? ' ' + cls : '') + '">' + text + '</div>';
  };
  for (let left = 3; left > 0; left--){
    status('✋ 请把手举到脸旁边，保持手势… ' + left);
    await new Promise(r => setTimeout(r, 1000));
  }
  status('正在连拍并识别…');
  try{
    const d = await post('/api/gesture', {item_id: itemId, top_k: pageSize});
    const shot = d.preview_url ? '<a class="shotwrap" href="' + esc(d.preview_url) +
      '" target="_blank" rel="noopener"><img class="shot" src="' + esc(d.preview_url) +
      '" alt="手势抓拍画面"></a><div class="shotcap">抓拍画面 · 点击查看大图</div>' : '';
    if (d.ok && (d.gesture === 'thumbs_up' || d.gesture === 'thumbs_down')){
      const pol = (((d.response || {}).feedback) || {}).polarity ||
                  (d.gesture === 'thumbs_up' ? 'positive' : 'negative');
      const dir = pol === 'positive' ? 'up' : 'down';
      const cancel = ratings.get(String(itemId)) === dir;
      if (cancel){
        const c = await post('/api/feedback', {item_id: itemId, polarity: 'none', top_k: pageSize});
        if (c.ok){
          ratings.delete(String(itemId));
          document.querySelectorAll('[data-undo-feedback="' + itemId + '"]').forEach(b => b.remove());
          setModalRating(null);
          if (c.response) renderBot(c.response, true);
          box.innerHTML = '<div class="mstatus ok">已识别 ' + esc(d.gesture) + '，已取消该商品反馈</div>' + shot;
        } else {
          box.innerHTML = '<div class="mstatus err">取消反馈失败：' + esc(c.error || '') + '</div>' + shot;
        }
        box.dataset.busy = '0';
        return;
      }
      ratings.set(String(itemId), dir);
      setModalRating(dir);
      const card = document.querySelector('.pcard[data-item="' + itemId + '"]');
      if (card) card.querySelectorAll('[data-like]').forEach(b => b.classList.toggle('on', b.dataset.like === dir));
      if (d.response) renderBot(d.response, true);
      box.innerHTML = '<div class="mstatus ok">已识别 ' + esc(d.gesture) + '，已更新该商品评价</div>' + shot;
    } else if (d.ok){
      box.innerHTML = '<div class="mstatus ok">已识别 ' + esc(d.gesture) +
        (d.message ? ' · ' + esc(d.message) : '') + '</div>' + shot;
    } else {
      box.innerHTML = '<div class="mstatus err">' + esc(d.error || '手势未识别') + '</div>' + shot;
    }
  }catch(e){
    box.innerHTML = '<div class="mstatus err">网络失败：' + esc(e.message) + '</div>';
  }
  box.dataset.busy = '0';
}

function detailEvidence(local){
  const ev = (local || {}).evidence || {};
  const sources = (local || {}).sources || [];
  const matched = ev.matched_constraints || [];
  const rows = [];
  if (sources.length){
    rows.push('<li><span>推荐来源</span><b>' + sources.map(esc).join(' · ') + '</b></li>');
  }
  if (matched.length){
    const labels = matched.map(m => typeof m === 'string' ? m :
      (m.constraint || m.reason || m.field || '')).filter(Boolean);
    if (labels.length) rows.push('<li><span>命中条件</span><b>' + labels.map(esc).join(' · ') + '</b></li>');
  }
  if (local && local.evidence_count){
    rows.push('<li><span>可核验依据</span><b>' + Number(local.evidence_count) + ' 条</b></li>');
  }
  if (local && local.explanation_confidence != null){
    rows.push('<li><span>解释置信度</span><b>' + Math.round(Number(local.explanation_confidence) * 100) + '%</b></li>');
  }
  if (local && local.explanation_source){
    rows.push('<li><span>解释生成</span><b>' +
      (local.explanation_source === 'llm' ? '在线模型 + 证据门控' : '模板逐句有据') + '</b></li>');
  }
  return rows.length
    ? '<ul class="evidence-list">' + rows.join('') + '</ul>'
    : '<div class="evidence-empty">当前商品没有额外证据字段。</div>';
}

async function openDetail(itemId){
  detailItemId = String(itemId);
  focusItemId = detailItemId;
  $('modal').classList.add('show');
  $('sheet').innerHTML = '<button class="x" data-close="#modal" aria-label="关闭">✕</button>' +
    '<div class="sec"><span class="spin"></span> 正在读取商品详情…</div>';
  fetch('/api/view', {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify({item_id: itemId})}).catch(() => {});
  try{
    const d = await (await fetch('/api/product/' + encodeURIComponent(itemId))).json();
    if (!d.ok){
      $('sheet').innerHTML = '<button class="x" data-close="#modal">✕</button>' +
        '<div class="sec errline">' + esc(d.error) + '</div>'; return;
    }
    const p = d.product;
    const local = currentItems.find(x => String(x.item_id) === String(itemId));
    const reasonText = (local && (local.explanation || local.reason)) || p.reason || '';
    const pickedNow = picked.has(p.item_id);
    $('sheet').innerHTML =
      '<button class="x" data-close="#modal" aria-label="关闭">✕</button>' +
      '<div class="detail">' +
        '<div class="detail-media">' + media({img_url:p.img_url, emoji:p.emoji}, 'big', true) + '</div>' +
        '<div class="detail-info">' +
          '<div class="detail-kicker">商品详情 · 推荐依据可核验</div>' +
          '<h2>' + esc(p.title) + '</h2>' +
          '<div class="en">' + esc(p.english_title || '') + '</div>' +
          '<div class="detail-price">' + money(p.price) +
            ' <span>' + esc(p.currency || '') + '</span></div>' +
          '<section class="detail-reason"><h3>推荐理由</h3>' +
            '<div class="reason" data-reason>' +
              escHuman(reasonText || '当前商品暂无推荐理由。') + '</div>' +
          '</section>' +
          '<section class="detail-evidence"><h3>证据与来源</h3>' +
            detailEvidence(local) +
          '</section>' +
          '<div class="detail-actions">' +
            '<button class="dact" data-rate="up">👍 喜欢</button>' +
            '<button class="dact" data-rate="down">👎 不喜欢</button>' +
            '<button class="dact" data-cmp2="' + esc(p.item_id) + '">' +
              (pickedNow ? '✓ 已加入对比' : '▦ 加入对比') + '</button>' +
            '<button class="dact primary" data-ask-product title="选中当前商品并在右侧继续提问">💬 继续问这件</button>' +
          '</div>' +
          '<div class="detail-hint">“继续问这件”会选中当前商品并聚焦右侧输入框，' +
            '例如问“为什么推荐它”“还有别的颜色吗”。</div>' +
          '<dl class="kv">' +
            '<dt>类目</dt><dd>' + esc(p.emoji + ' ' + p.category) + '</dd>' +
            '<dt>价格来源</dt><dd>' + esc(zh(SRC_ZH, p.price_source, p.price_source || '—')) + '</dd>' +
            '<dt>历史成交</dt><dd>' + (p.transactions ?? '—') + ' 笔</dd>' +
            '<dt>商品编号</dt><dd>' + esc(p.item_id) + '</dd>' +
          '</dl>' +
          '<div class="gesture-wrap"><h3>手势快捷操作</h3>' +
            '<div id="modalGesture" class="mgesture"></div></div>' +
        '</div>' +
      '</div>';
    $('sheet').querySelectorAll('[data-rate]').forEach(el => {
      el.onclick = ev => { ev.stopPropagation(); rate(itemId, el.dataset.rate); };
    });
    setModalRating(ratings.get(String(itemId)) || null);
    const c2 = $('sheet').querySelector('[data-cmp2]');
    if (c2) c2.onclick = ev => {
      ev.stopPropagation();
      if (toggleCompare(itemId, p)){
        c2.textContent = picked.has(itemId) ? '✓ 已加入对比' : '▦ 加入对比';
        toast('已更新对比栏');
      }
    };
    const askProduct = $('sheet').querySelector('[data-ask-product]');
    if (askProduct) askProduct.onclick = ev => {
      ev.stopPropagation();
      focusItemId = String(itemId);
      closeAll();
      if (window.innerWidth <= 1023){
        $('assistant').classList.add('show');
        $('fab').textContent = '✕';
      }
      setTimeout(() => {
        $('q2').focus();
        toast('已选中此商品，可问：为什么推荐它 / 还有别的颜色吗');
      }, 0);
    };
    await startLiveGesture(itemId);   // 进入商品详情自动开启摄像头直播
  }catch(e){
    $('sheet').innerHTML = '<button class="x" data-close="#modal">✕</button>' +
      '<div class="sec errline">' + esc(e.message) + '</div>';
  }
}
function closeAll(){
  pauseLiveGesture();
  detailItemId = null;
  $('modal').classList.remove('show');
  $('cmpModal').classList.remove('show');
}

/* ============ 工具动作 ============ */
async function act(kind){
  if (busy) return;
  if (kind === 'compare'){ openCompare(); return; }
  if (needReal && ['voice','vision'].includes(kind)){
    addBot('<div class="slots">该功能需要真实设备，请以 <code>python webui.py --real</code> 启动。</div>');
    return;
  }
  const localized = kind === 'vision';
  if (localized){
    if (visionBusy) return;
    visionBusy = true;
  } else {
    setBusy(true);
  }
  if (kind === 'llm'){
    try{
      const d = await post('/api/llm');
      $('llmbtn').textContent = 'LLM 解释：' + (d.enabled ? '开' : '关');
      addBot('<div class="slots">' + (d.enabled
        ? '已开启真实 LLM 生成解释（每条约 10~25 秒）。解释须连过事实 / 格式 / 流畅度三道门控，否则回退模板。'
        : '已关闭 LLM，解释回退为模板（毫秒级、逐句有据）。') + '</div>');
    }catch(e){ addBot('<div class="errline">' + esc(e.message) + '</div>'); }
    setBusy(false); return;
  }
  if (kind === 'undo' || kind === 'reset'){
    const id = addAction(kind === 'undo' ? '↩' : '⟳',
      kind === 'undo' ? '撤销上一轮' : '重新开始会话', '正在处理…');
    try{
      const d = await post('/api/' + kind);
      if (d.ok) finishAction(id, 'ok', '完成', kind === 'undo' ? '已回滚到上一轮状态' : '已开启全新会话');
      else finishAction(id, 'err', '失败', esc(d.error));
      if (d.ok && kind === 'reset'){
        picked.clear(); renderTray(); syncPickUI();
        ratings.clear();
        activeFilters = []; renderFilters();
        await loadHomeShelf(true);
      }
    }catch(e){ finishAction(id, 'err', '失败', esc(e.message)); }
    setBusy(false); return;
  }
  const meta = {voice:{i:'🎤', t:'语音输入', d:'正在录音 4 秒…'},
                vision:{i:'📷', t:'识图推荐', d:'正在识别画面中的主要物品并生成同类推荐（本地 VL 约 60~90 秒）…'}}[kind];
  const id = addAction(meta.i, meta.t, meta.d);
  try{
    const payload = kind === 'voice' ? {seconds: 4, top_k: pageSize} : {top_k: pageSize};
    const d = await post('/api/' + kind, payload);
    if (kind === 'voice'){
      if (d.transcript){
        finishAction(id, d.ok ? 'ok' : 'warn', d.ok ? '已识别' : '未识别',
                     '识别结果：<b>' + esc(d.transcript) + '</b>' + (d.ok ? '' : actionSuggestions('voice')));
        if (d.ok) renderBot(d.response, true);
      } else {
        finishAction(id, 'err', '未识别到有效语音',
                     esc(d.error || '没听清内容') + actionSuggestions('voice'));
      }
    } else if (kind === 'vision'){
      if (d.description){
        const why = d.ok ? '' : '<div class="errline" style="margin-top:6px">' + esc(d.error || '') + '</div>';
        finishAction(id, d.ok ? 'ok' : 'warn', d.ok ? '已完成' : '需重试',
                     '识别到：<b>' + esc(d.description) + '</b>' + why +
                     (d.ok ? '' : actionSuggestions('vision')));
        if (d.ok) renderBot(d.response, true);
      } else finishAction(id, 'err', '失败', esc(d.error) + actionSuggestions('vision'));
    } else if (!d.ok){
      finishAction(id, 'err', '失败', esc(d.error) + actionSuggestions(kind));
    } else { finishAction(id, 'ok', '完成', ''); if (d.response) renderBot(d.response, true); }
  }catch(e){ finishAction(id, 'err', '失败', esc(e.message) + actionSuggestions(kind)); }
  if (localized) visionBusy = false; else setBusy(false);
}

/* ============ 运行状态抽屉 ============ */
async function openStatus(){
  $('statusDrawer').classList.add('show'); $('mask').classList.add('show');
  $('statusBody').innerHTML = '<span class="spin"></span> 读取中…';
  try{
    const info = await (await fetch('/api/info')).json();
    const st = await post('/api/state');
    const s = st.state || {};
    const m = info.modes || {};
    const row = (k, v) => '<div class="kvrow"><span class="k">' + k + '</span><span class="v">' + esc(v) + '</span></div>';
    $('statusBody').innerHTML =
      '<h3 style="margin:0 0 6px;font-size:13px;color:#8b91a6">运行模式</h3>' +
      row('用户', info.user_id) + row('会话', info.session_id) +
      row('推荐适配器', zh(MODE_ZH, m.recommender)) + row('多模态适配器', zh(MODE_ZH, m.multimodal)) +
      row('LLM 解释', info.llm ? '已开启' : '模板') +
      row('对话后端', (m.llm_provider === 'online' ? '在线模型' : m.llm_provider === 'local' ? '本地 Qwen' : '未启用')) +
      row('联网搜索', (info.llm_chat || {}).web_search ? '开 · 最多 ' + ((info.llm_chat || {}).web_search_count || 5) + ' 条' : '关') +
      row('语音播报', info.voice ? '开' : '关') +
      '<h3 style="margin:14px 0 6px;font-size:13px;color:#8b91a6">本次会话</h3>' +
      row('轮次', String(s.turns ?? 0)) +
      row('已点赞', JSON.stringify(s.likes || {})) +
      row('已倒赞', JSON.stringify(s.dislikes || {})) +
      row('已浏览', JSON.stringify(s.viewed_items || {})) +
      row('生效条件', Object.keys(s.slot_state || {}).filter(k => k !== 'budget_raw' && k !== 'category_alt').join('、') || '（无）') +
      row('焦点商品', s.focus_item_id || '（无）');
  }catch(e){ $('statusBody').innerHTML = '<div class="errline">' + esc(e.message) + '</div>'; }
}
function closeStatus(){ $('statusDrawer').classList.remove('show'); $('mask').classList.remove('show'); }

/* ============ 事件绑定 ============ */
function submitAsk(inputId){
  const el = $(inputId); const t = el.value.trim();
  if (!t) return;
  if (busy){ toast('正在处理中，请稍候'); return; }
  el.value = '';
  ask(t);
}
$('searchForm').addEventListener('submit', ev => { ev.preventDefault(); submitAsk('q'); });
$('sendbtn2').addEventListener('click', () => submitAsk('q2'));
$('q2').addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); submitAsk('q2'); } });
$('toolbar').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-act]'); if (b) act(b.dataset.act);
});
$('statusBtn').addEventListener('click', openStatus);
$('gearBtn').addEventListener('click', openStatus);
$('statusClose').addEventListener('click', closeStatus);
$('mask').addEventListener('click', closeStatus);
function switchUser(userId){
  if (busy){ toast('正在处理中，请稍候'); return; }
  setBusy(true);
  post('/api/user', {user_id: userId}).then(d => {
    if (d.ok){ toast('已切换到 ' + d.account.name); location.reload(); }
    else { toast(d.error || '切换失败'); setBusy(false); }
  }).catch(e => { toast(e.message); setBusy(false); });
}
function renderUserMenu(){
  const accs = window.__accounts || [];
  const cur = (window.__account || {}).user_id;
  $('userMenu').innerHTML = accs.map(a => {
    const active = a.user_id === cur;
    return '<button role="menuitem" data-user="' + esc(a.user_id) + '"' + (active ? ' aria-current="true"' : '') + '>' +
      '<span class="uava2" style="background:' + esc(a.color || '#6d5dfc') + '">' +
        esc(String(a.name || '?').slice(0, 1)) + '</span>' +
      '<span><span class="nm">' + esc(a.name) + '</span><br><span class="ps">' + esc(a.persona) + '</span></span>' +
      (active ? '<span class="cur">当前</span>' : '') +
      '</button>';
  }).join('');
}
function toggleUserMenu(open){
  const m = $('userMenu');
  if (!m) return;
  const show = open === undefined ? !m.classList.contains('show') : !!open;
  if (show) renderUserMenu();
  m.classList.toggle('show', show);
  $('userBtn').setAttribute('aria-expanded', String(show));
}
$('userBtn').addEventListener('click', () => toggleUserMenu());
$('userMenu').addEventListener('click', ev => {
  const b = ev.target.closest('[data-user]');
  if (!b) return;
  toggleUserMenu(false);
  switchUser(b.dataset.user);
});
document.addEventListener('click', ev => {
  if (!ev.target.closest('#userMenu') && !ev.target.closest('#userBtn')) toggleUserMenu(false);
});
$('clearChat').addEventListener('click', () => { stream.innerHTML = ''; toast('对话已清空（会话状态不受影响）'); });
$('fchips').addEventListener('click', ev => {
  const b = ev.target.closest('[data-drop]'); if (b) dropFilter(b.dataset.drop);
});
$('sortSel').addEventListener('change', ev => { sortMode = ev.target.value; renderShelf(currentItems); });
$('loadMore').addEventListener('click', loadMore);
if ('IntersectionObserver' in window){
  new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) loadMore();
  }, {rootMargin: '320px'}).observe($('loadMore'));
}
$('pageSize').addEventListener('change', async ev => {
  pageSize = Number(ev.target.value) || 8;
  if (lastQuery === null){ await loadHomeShelf(true); return; }
  if (!currentItems.length){ toast('当前结果不是商品列表，无法展开'); return; }
  if (busy) return;
  setBusy(true);
  try{
    const d = await post('/api/more', {text: lastQuery, top_k: pageSize});
    if (d.ok) renderShelf(d.items, $('shelfTitle').textContent, '', null);
    else toast(d.error || '展开失败');
  }catch(e){ toast('展开失败：' + e.message); }
  setBusy(false);
});
$('viewSwitch').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-view]'); if (!b) return;
  viewMode = b.dataset.view;
  $('viewSwitch').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  renderShelf(currentItems);
});
grid.addEventListener('click', ev => {
  const card = ev.target.closest('.pcard'); if (!card) return;
  const id = card.dataset.item;
  const like = ev.target.closest('[data-like]');
  if (like){ ev.stopPropagation(); rate(id, like.dataset.like); return; }
  if (ev.target.closest('[data-cmp]')){ ev.stopPropagation(); toggleCompare(id); return; }
  if (ev.target.closest('[data-open]')) openDetail(id);
});
stream.addEventListener('click', ev => {
  const uf = ev.target.closest('[data-undo-feedback]');
  if (uf){ rate(uf.dataset.undoFeedback, uf.dataset.feedbackPolarity, true); return; }
  const fold = ev.target.closest('[data-fold]');
  if (fold){ const f = fold.closest('.fold'); f.classList.toggle('open');
             fold.textContent = (f.classList.contains('open') ? '▾ 收起依据' : '▸ 展开依据'); return; }
  const slot = ev.target.closest('[data-slot]');
  if (slot){
    if (busy){ toast('正在处理中，请稍候'); return; }
    const t = slot.dataset.slot; $('q').value = ''; ask(t); return;
  }
  const rt = ev.target.closest('[data-retry]');
  if (rt){ act(rt.dataset.retry); return; }
  if (ev.target.closest('[data-type]')){ $('q').focus(); toast('已切到输入框，直接打字即可'); return; }
  const hp = ev.target.closest('[data-help]');
  if (hp){ toast(hp.dataset.help); return; }
});
$('trayItems').addEventListener('click', ev => {
  const b = ev.target.closest('[data-unpick]'); if (b) toggleCompare(b.dataset.unpick);
});
$('trayClear').addEventListener('click', () => { picked.clear(); renderTray(); syncPickUI(); toast('已清空对比栏'); });
$('trayGo').addEventListener('click', openCompare);
document.addEventListener('click', async ev => {
  const tts = ev.target.closest('[data-tts]');
  if (tts && !tts.disabled){
    const text = decodeURIComponent(tts.dataset.tts || '');
    if (speakInBrowser(text)){
      tts.textContent = '🔊 再播一次';
      toast('已使用浏览器语音即时播报');
      return;
    }
    tts.disabled = true; tts.textContent = '合成中…';
    try{
      const d = await post('/api/tts', {text});
      if (d.ok){ new Audio(d.audio_url).play(); tts.textContent = '🔊 重新播放'; }
      else { tts.textContent = '语音不可用'; }
    }catch(e){ tts.textContent = '语音失败'; }
    tts.disabled = false;
    return;
  }
  const c = ev.target.closest('[data-close]');
  if (c){
    ev.stopPropagation();
    document.querySelector(c.dataset.close).classList.remove('show');
    if (c.dataset.close === '#modal') pauseLiveGesture();
  }
});
$('modal').addEventListener('click', ev => { if (ev.target === $('modal')) closeAll(); });
$('cmpModal').addEventListener('click', ev => { if (ev.target === $('cmpModal')) closeAll(); });
document.addEventListener('keydown', ev => { if (ev.key === 'Escape'){ closeAll(); closeStatus(); toggleUserMenu(false); } });
$('toBottom').addEventListener('click', bottom);
stream.addEventListener('scroll', () => {
  const far = stream.scrollHeight - stream.scrollTop - stream.clientHeight > 160;
  $('toBottom').classList.toggle('show', far);
});
$('fab').addEventListener('click', () => {
  $('assistant').classList.toggle('show');
  $('fab').textContent = $('assistant').classList.contains('show') ? '✕' : '💬';
});
function syncTabbar(){
  const narrow = window.innerWidth <= 639;
  $('tabbar').classList.toggle('is-on', narrow);
  if (window.innerWidth > 1023) $('assistant').classList.remove('show');
}
window.addEventListener('resize', syncTabbar);
syncTabbar();

$('tabbar').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-tab]'); if (!b) return;
  $('tabbar').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  const tab = b.dataset.tab;
  if (tab === 'chat'){ $('assistant').classList.add('show'); $('fab').textContent = '✕'; }
  else if (tab === 'status'){ $('assistant').classList.remove('show'); $('fab').textContent = '💬'; openStatus(); }
  else { $('assistant').classList.remove('show'); $('fab').textContent = '💬'; window.scrollTo({top:0,behavior:'smooth'}); }
});

async function loadHomeShelf(force){
  if (!force && userSaid.length) return;   // 用户已经提问，别用首页结果覆盖
  lastQuery = null;
  try{
    const h = await (await fetch('/api/home?limit=' + pageSize)).json();
    if (h.ok && h.items && h.items.length){
      renderShelf(h.items, '猜你喜欢', '基于 v146 个性化精排的默认推荐');
    } else {
      renderShelf([], '开始逛逛');
    }
  }catch(e){
    grid.innerHTML = ''; $('empty').style.display = 'block';
    $('empty').innerHTML = '未能连接服务：' + esc(e.message) +
      '<div class="examples"><button data-reload>重试</button></div>';
    const rb = $('empty').querySelector('[data-reload]');
    if (rb) rb.onclick = () => location.reload();
  }
}

/* ============ 启动（P2：先骨架后内容） ============ */
(function bootSkeleton(){
  grid.innerHTML = Array.from({length: 4}).map(() =>
    '<div class="skel"><div class="sk1"></div><div class="sk2"></div><div class="sk3"></div></div>').join('');
  stream.innerHTML = '<div class="skb"><span class="skdot"></span><span style="color:#8b91a6">正在连接推荐助手…</span></div>';
})();

(async function boot(){
  const info = await (await fetch('/api/info')).json();
  const m = info.modes || {};
  needReal = m.multimodal !== 'real';
  if (!needReal){
    fetch('/api/camera/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}).catch(() => {});
  }
  $('chips').innerHTML = ['推荐 ' + zh(MODE_ZH, m.recommender), '多模态 ' + zh(MODE_ZH, m.multimodal),
                          '对话 ' + (m.llm_provider === 'online' ? '在线' : m.llm_provider === 'local' ? '本地' : '关'),
                          '搜索 ' + ((info.llm_chat || {}).web_search ? '开' : '关'),
                          '语音 ' + (info.voice ? '开' : '关')]
    .map(t => '<span class="chip' + (needReal ? ' dim' : '') + '">' + esc(t) + '</span>').join('');
  window.__accounts = info.accounts || [];
  window.__account = info.account || {};
  const acc = info.account || {name: info.user_id, color: '#6d5dfc'};
  $('uname').textContent = acc.name;
  $('uava').textContent = String(acc.name || '?').slice(0, 1);
  if (acc.color) $('uava').style.background = acc.color;
  $('llmbtn').textContent = 'LLM 解释：' + (info.llm ? '开' : '关');
  setBusy(false);
  stream.innerHTML = '';
  await loadHomeShelf();
  addBot('<div style="font-weight:620;margin-bottom:5px">你好，我是推荐助手</div>' +
    '<div style="font-size:13px;color:#8b91a6">在上方说一句需求，或点工具条用<b>语音 / 识图推荐</b>提要求；手势可在商品详情内直接使用。' +
    '推荐结果在左侧货架：<b>点商品看详情</b>、<b>卡片右上角点赞 / 倒赞</b>、<b>点「加入对比」做字段对照</b>。' +
    (needReal ? '<br>当前为<b>离线模式</b>；用 <code>python webui.py --real</code> 可启用麦克风与摄像头。' : '') +
    '</div>');
  $('q').focus();
})();

window.addEventListener('pagehide', () => {
  if (!needReal){
    fetch('/api/camera/stop', {method:'POST', keepalive:true}).catch(() => {});
  }
});
