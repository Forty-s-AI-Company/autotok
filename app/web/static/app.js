/* AutoTok 操作台：側邊欄、選單、摺疊記憶、更新時間、提示訊息與按鈕狀態。 */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var store = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };

  /* 側邊欄收合 */
  var app = $('#app');
  function setCollapsed(c) {
    app.classList.toggle('collapsed', c);
    document.documentElement.classList.toggle('pre-collapsed', c);
    var b = $('#collapseBtn'); if (b) b.setAttribute('aria-label', c ? '展開側邊欄' : '收合側邊欄');
    store.set('collapsed', c ? '1' : '0');
    closeMenus();
  }
  if (store.get('collapsed', '0') === '1') app.classList.add('collapsed');
  $('#collapseBtn').addEventListener('click', function () { setCollapsed(!app.classList.contains('collapsed')); });
  document.addEventListener('keydown', function (e) {
    var tag = document.activeElement && document.activeElement.tagName;
    if (e.key === '[' && !/INPUT|SELECT|TEXTAREA/.test(tag || '')) setCollapsed(!app.classList.contains('collapsed'));
    if (e.key === 'Escape') closeMenus();
  });

  /* 彈出選單 */
  function closeMenus() {
    $$('.menu').forEach(function (m) { m.hidden = true; });
    ['acctBtn', 'sysBtn'].forEach(function (id) { var b = document.getElementById(id); if (b) b.setAttribute('aria-expanded', 'false'); });
  }
  function bindMenu(btnId, menuId) {
    var btn = document.getElementById(btnId), menu = document.getElementById(menuId);
    if (!btn || !menu) return;
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = menu.hidden; closeMenus(); menu.hidden = !open; btn.setAttribute('aria-expanded', String(open));
    });
  }
  bindMenu('acctBtn', 'acctMenu'); bindMenu('sysBtn', 'sysMenu');
  document.addEventListener('click', function (e) { if (!e.target.closest('.menu')) closeMenus(); });

  /* 主題 */
  $('#themeBtn').addEventListener('click', function () {
    var r = document.documentElement;
    var cur = r.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    r.dataset.theme = cur === 'dark' ? 'light' : 'dark'; store.set('theme', r.dataset.theme);
  });

  /* 摺疊區塊的開關狀態（details[data-key]）會被記住 */
  $$('details[data-key]').forEach(function (d) {
    var k = 'open:' + location.pathname + ':' + d.dataset.key, saved = store.get(k, null);
    if (saved !== null) d.open = saved === '1';
    d.addEventListener('toggle', function () { store.set(k, d.open ? '1' : '0'); });
  });

  /* 更新時間：顯示絕對時間與相對時間，超過 data-stale 分鐘變色 */
  function rel(min) {
    if (min < 1) return '剛剛';
    if (min < 60) return Math.round(min) + ' 分鐘前';
    if (min < 1440) return Math.round(min / 60) + ' 小時前';
    return Math.round(min / 1440) + ' 天前';
  }
  function paintTimes() {
    $$('time[data-ts]').forEach(function (t) {
      var d = new Date(t.dataset.ts); if (isNaN(d)) return;
      var min = (Date.now() - d.getTime()) / 60000;
      var base = t.textContent.split(' · ')[0];
      t.textContent = base + ' · ' + rel(min);
      var f = t.closest('.fresh'), lim = f ? +f.dataset.stale : 0;
      if (f) f.classList.toggle('stale', lim > 0 && min > lim);
    });
  }
  paintTimes(); setInterval(paintTimes, 30000);

  /* 提示訊息 */
  function toast(kind, msg) {
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || 'ok'); el.textContent = msg; el.setAttribute('role', 'status');
    $('#toasts').appendChild(el);
    setTimeout(function () { el.remove(); }, kind === 'err' ? 9000 : 4500);
  }
  window.autotokToast = toast;
  document.body.addEventListener('toast', function (e) { var d = e.detail || {}; toast(d.kind, d.msg || d.value); });
  document.body.addEventListener('pageRefresh', function () { setTimeout(function () { location.reload(); }, 300); });
  document.body.addEventListener('jobFinished', function (e) {
    var d = e.detail || {}; if (d.msg) toast(d.kind, d.msg);
    if (!document.body.hasAttribute('data-refresh-on-job')) return;
    var dirty = $$('textarea, input[type=text], input[type=file]').some(function (el) { return el.value && el.value !== el.defaultValue; });
    if (dirty) toast('ok', '結果已更新，儲存或送出手上的輸入後重新整理頁面即可看到。');
    else setTimeout(function () { location.reload(); }, 600);
  });

  /* htmx：安全 token、目前帳號、按鈕與表單的忙碌狀態、錯誤顯示 */
  document.body.addEventListener('htmx:configRequest', function (e) {
    var t = $('meta[name="autotok-token"]'), a = $('meta[name="autotok-account"]');
    if (t) e.detail.headers['X-AutoTok-Token'] = t.content;
    if (a) e.detail.headers['X-AutoTok-Account'] = a.content;
  });
  /* data-busy 可以放在按鈕本身，或放在 form 內的送出按鈕（請求由 form 發出時） */
  function busyTarget(elt) {
    if (!elt || !elt.matches) return null;
    if (elt.matches('[data-busy]')) return elt;
    if (elt.tagName === 'FORM') return $('[data-busy]', elt);
    return null;
  }
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    var el = busyTarget(e.detail.elt); if (!el || el.dataset.state === 'loading') return;
    var bl = $('.bl', el) || el; el.dataset.idle = bl.textContent; bl.textContent = el.dataset.busy; el.dataset.state = 'loading';
    if (el.tagName === 'BUTTON') el.disabled = true;
  });
  document.body.addEventListener('htmx:afterRequest', function (e) {
    var el = busyTarget(e.detail.elt); if (!el) return;
    var bl = $('.bl', el) || el; delete el.dataset.state; if (el.dataset.idle) bl.textContent = el.dataset.idle;
    if (el.tagName === 'BUTTON') el.disabled = false;
  });
  function errText(xhr) {
    var t = xhr && xhr.responseText ? xhr.responseText.trim() : '';
    if (t && t.length < 200 && t.charAt(0) !== '<' && t.charAt(0) !== '{') return t;
    return '請求失敗（' + (xhr ? xhr.status : '無回應') + '），請重新整理頁面後再試。';
  }
  document.body.addEventListener('htmx:responseError', function (e) { toast('err', errText(e.detail.xhr)); });
  document.body.addEventListener('htmx:sendError', function () { toast('err', '連不上本機伺服器，請確認操作台仍在執行。'); });
})();

/* 分頁標籤：[data-tabs] 內的按鈕以 data-tab 對應同一區塊內的 [data-pane] */
document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-tabs] [data-tab]'); if (!b) return;
  var box = b.closest('[data-tabs]'), root = box.parentElement;
  box.querySelectorAll('[data-tab]').forEach(function (x) { x.setAttribute('aria-selected', String(x === b)); });
  root.querySelectorAll('[data-pane]').forEach(function (p) { p.hidden = p.dataset.pane !== b.dataset.tab; });
});

/* ---- 圖片輪播（Swiper）與放大檢視 ---- */
(function () {
  'use strict';
  function visible(el) { return !!(el && el.offsetParent !== null); }

  /* 縮圖列：一排 6 張剛好排滿；超過 6 張用左右箭頭切換 */
  function initGalleries(root) {
    if (!window.Swiper) return;
    (root || document).querySelectorAll('.gallery').forEach(function (g) {
      var el = g.querySelector('.swiper'); if (!el || !visible(g)) return;
      if (el.swiper) { el.swiper.update(); return; }
      new window.Swiper(el, {
        slidesPerView: 3, spaceBetween: 8, watchOverflow: true, a11y: false,
        breakpoints: { 560: { slidesPerView: 4 }, 860: { slidesPerView: 6 } },
        navigation: { prevEl: g.querySelector('.g-prev'), nextEl: g.querySelector('.g-next'), lockClass: 'g-lock' }
      });
    });
  }
  window.autotokInitGalleries = initGalleries;
  document.addEventListener('toggle', function (e) { if (e.target.open) initGalleries(e.target); }, true);
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-tabs] [data-tab]')) setTimeout(function () { initGalleries(document); }, 0);
  });
  window.addEventListener('load', function () { initGalleries(document); });

  /* 放大檢視：單張大圖，可左右切換、鍵盤方向鍵、雙擊放大、Esc 關閉 */
  var lb = null, lastFocus = null;
  function closeLightbox() {
    if (!lb) return;
    if (lb.swiper) lb.swiper.destroy(true, true);
    lb.root.remove(); lb = null; document.body.classList.remove('lb-open');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function openLightbox(urls, index) {
    if (!window.Swiper || !urls.length) return;
    closeLightbox(); lastFocus = document.activeElement;
    var root = document.createElement('div');
    root.className = 'lb'; root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true'); root.setAttribute('aria-label', '圖片檢視');
    root.innerHTML = '<div class="swiper lb-swiper"><div class="swiper-wrapper"></div></div>' +
      '<button type="button" class="lb-btn lb-close" aria-label="關閉">&times;</button>' +
      '<button type="button" class="lb-btn lb-prev" aria-label="上一張"><svg class="i"><use href="#i-chev"/></svg></button>' +
      '<button type="button" class="lb-btn lb-next" aria-label="下一張"><svg class="i"><use href="#i-chev"/></svg></button>' +
      '<div class="lb-count" aria-live="polite"></div>';
    var wrap = root.querySelector('.swiper-wrapper');
    urls.forEach(function (u, k) {
      var s = document.createElement('div'); s.className = 'swiper-slide';
      var z = document.createElement('div'); z.className = 'swiper-zoom-container';
      var img = document.createElement('img'); img.src = u; img.alt = '第 ' + (k + 1) + ' 張';
      z.appendChild(img); s.appendChild(z); wrap.appendChild(s);
    });
    document.body.appendChild(root); document.body.classList.add('lb-open');
    var count = root.querySelector('.lb-count');
    var sw = new window.Swiper(root.querySelector('.lb-swiper'), {
      initialSlide: index, zoom: { maxRatio: 3 }, keyboard: { enabled: true }, a11y: false,
      navigation: { prevEl: root.querySelector('.lb-prev'), nextEl: root.querySelector('.lb-next') },
      on: { init: function (s) { count.textContent = (s.activeIndex + 1) + ' / ' + urls.length; },
            slideChange: function (s) { count.textContent = (s.activeIndex + 1) + ' / ' + urls.length; } }
    });
    root.addEventListener('click', function (e) { if (e.target === root || e.target.classList.contains('swiper-slide') || e.target.classList.contains('swiper-zoom-container')) closeLightbox(); });
    root.querySelector('.lb-close').addEventListener('click', closeLightbox);
    root.querySelector('.lb-close').focus();
    lb = { root: root, swiper: sw };
  }
  window.autotokLightbox = openLightbox;
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && lb) { e.stopPropagation(); closeLightbox(); } }, true);
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-lb]'); if (!a) return;
    e.preventDefault();
    var links = Array.prototype.slice.call(a.closest('.gallery').querySelectorAll('a[data-lb]'));
    openLightbox(links.map(function (x) { return x.getAttribute('href'); }), links.indexOf(a));
  });

  /* 從別頁帶著 #post_id 進來：只展開那一篇並捲動到它 */
  function openFromHash() {
    var id = decodeURIComponent(location.hash.slice(1)); if (!id) return;
    var target = document.getElementById(id);
    if (!target || target.tagName !== 'DETAILS' || !target.classList.contains('post')) return;
    document.querySelectorAll('details.post').forEach(function (d) { d.open = d === target; });
    target.scrollIntoView({ block: 'start' });
    target.classList.add('hl'); setTimeout(function () { target.classList.remove('hl'); }, 1800);
    initGalleries(target);
  }
  window.addEventListener('DOMContentLoaded', openFromHash);
  window.addEventListener('hashchange', openFromHash);
})();

/* ---- 提示氣泡：滑鼠移上或鍵盤聚焦 [data-tip] 時顯示（不會被捲動容器裁切） ---- */
(function () {
  'use strict';
  var pop = null;
  function hide() { if (pop) { pop.remove(); pop = null; } }
  function show(el) {
    hide(); var text = el.getAttribute('data-tip'); if (!text) return;
    pop = document.createElement('div'); pop.className = 'tip-pop'; pop.textContent = text; pop.setAttribute('role', 'tooltip');
    document.body.appendChild(pop);
    var r = el.getBoundingClientRect(), p = pop.getBoundingClientRect();
    var left = Math.min(Math.max(8, r.left + r.width / 2 - p.width / 2), window.innerWidth - p.width - 8);
    var top = r.top - p.height - 8; if (top < 8) top = r.bottom + 8;
    pop.style.left = left + 'px'; pop.style.top = top + 'px';
  }
  document.addEventListener('mouseover', function (e) { var t = e.target.closest('[data-tip]'); if (t) show(t); });
  document.addEventListener('mouseout', function (e) { if (e.target.closest('[data-tip]')) hide(); });
  document.addEventListener('focusin', function (e) { var t = e.target.closest('[data-tip]'); if (t) show(t); });
  document.addEventListener('focusout', hide);
  document.addEventListener('scroll', hide, true);
})();

/* ---- 站內跳轉：網址 #id 指到摺疊區塊時，展開它、捲到位並短暫標示 ---- */
(function () {
  'use strict';
  function jump() {
    var id = decodeURIComponent(location.hash.slice(1)); if (!id) return;
    var t = document.getElementById(id);
    if (!t || t.tagName !== 'DETAILS' || t.classList.contains('post')) return;
    t.open = true; t.scrollIntoView({ block: 'start', behavior: 'smooth' });
    t.classList.add('hl'); setTimeout(function () { t.classList.remove('hl'); }, 1800);
  }
  window.addEventListener('DOMContentLoaded', function () { setTimeout(jump, 50); });
  window.addEventListener('hashchange', jump);
  /* 已經在同一頁、同一個帳號時，連結不會重新載入頁面，直接跳過去 */
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-jump]'); if (!a) return;
    var u = new URL(a.href, location.href);
    if (u.pathname === location.pathname && u.search === location.search) {
      e.preventDefault();
      if (location.hash === u.hash) jump(); else location.hash = u.hash;
    }
  });
})();

/* 帶入：按鈕的 data-fill 指向輸入框，data-value 是要填入的值 */
document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-fill]'); if (!b) return;
  var el = document.querySelector(b.getAttribute('data-fill')); if (el) { el.value = b.getAttribute('data-value'); el.focus(); }
});
