"use strict";

// ------------------------------------------------------------ утилиты

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ссылки из чужих данных открываем только если это http(s)
function url(s) {
  return /^https?:\/\//i.test(String(s ?? "")) ? esc(s) : "#";
}

async function api(path, opts = {}) {
  const init = { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let resp;
  try {
    resp = await fetch(path, init);
  } catch {
    // окно start.bat закрыто или сервер упал — страница осталась открытой без сервера
    const err = new Error("Панель не связана с сервером: окно start.bat закрыто. Запусти start.bat заново и обнови страницу.");
    err.details = [err.message];
    throw err;
  }
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const err = new Error(typeof data?.detail === "string" ? data.detail : "ошибка сервера");
    err.details = Array.isArray(data?.detail) ? data.detail.map(d => (typeof d === "string" ? d : d.msg)) : [err.message];
    throw err;
  }
  return data;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(value));
  } catch { return null; }
}

const CURRENCY = { RUR: "₽", RUB: "₽", USD: "$", EUR: "€", BYN: "Br", BYR: "Br", KZT: "₸", UAH: "₴" };
const money = n => Number(n).toLocaleString("ru-RU");
function fmtSalary(v) {
  const { salary_from: lo, salary_to: hi } = v;
  if (lo == null && hi == null) return "";
  const cur = CURRENCY[v.currency] || v.currency || "";
  let s;
  if (lo != null && hi != null) s = lo === hi ? money(lo) : `${money(lo)} – ${money(hi)}`;
  else if (lo != null) s = `от ${money(lo)}`;
  else s = `до ${money(hi)}`;
  return `${s} ${cur}`.trim();
}
function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(String(iso).replace(/([+-]\d{2})(\d{2})$/, "$1:$2"));
  return isNaN(d) ? null : d;
}
function fmtDate(iso, withTime = false) {
  const d = parseDate(iso);
  if (!d) return iso || "";
  const opts = { day: "numeric", month: "short", year: "numeric" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleString("ru-RU", opts).replace(" г.", "");
}
function shortDate(iso) {
  const d = parseDate(iso);
  if (!d) return "";
  const days = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 864e5);
  if (days === 0) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (days === 1) return "вчера";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }).replace(".", "");
}
const today = () => new Date().toISOString().slice(0, 10);
const isTyping = el => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT");

const SOURCE_LABEL = { hh: "hh.ru", habr: "Habr Career", geekjob: "Geekjob", getmatch: "getmatch" };
const SOURCE_STATUS = { active: "активна", archived: "в архиве", deleted: "удалена" };
const EMPLOYER_TYPE = {
  company: "организация", agency: "кадровое агентство", project_director: "проект",
  private_recruiter: "частный рекрутер", private_individual: "частное лицо", self_employed: "самозанятый",
};
const STATUS_CLASS = { "отклик": "applied", "не подходит": "rejected" };
const QUIET_MARKS = new Set(["зп не указана"]); // и так видно по пустой зарплате

// ------------------------------------------------------------ состояние

const TABS = ["main", "side", "posts", "logs", "settings"];
const DEFAULT_FILTERS = {
  main: { q: "", view: "new", direction: "", source: "", has_salary: false, no_experience: false },
  side: { q: "", view: "new", source: "", has_salary: false, no_experience: false },
  posts: { q: "", view: "new", channel: "" },
};
const savedFilters = store("filters2") || {};
const S = {
  tab: "main",
  meta: null,
  items: [],
  total: 0,
  counts: {},
  selected: null,
  settingsSection: store("settingsSection") || "channels",
  filtersOpen: false,
  filters: Object.fromEntries(Object.entries(DEFAULT_FILTERS).map(([k, v]) => [k, { ...v, ...(savedFilters[k] || {}) }])),
  run: { log: [], result: null, running: false },
};
S.stale = { count: 0, days: 14 };
S.olderOpen = false;
// панель всегда открывается на «Новых»
for (const k of ["main", "side", "posts"]) S.filters[k].view = "new";
S.viewCounts = {};
// вкладки ленты не пересекаются: каждая карточка ровно в одной
const VIEWS = [
  ["new", "Новые", "ещё не открыта и без отметок"],
  ["seen", "Просмотренные", "открыта, но решения нет"],
  ["applied", "Отклики", ""],
  ["rejected", "Не подходит", ""],
];
const viewOf = it => it.status === "отклик" ? "applied" : it.status === "не подходит" ? "rejected" : it.seen ? "seen" : "new";
// карточка сменила вкладку: счётчики сразу, сама карточка остаётся на месте до перезагрузки списка
function moveView(kind, from, to) {
  if (from === to) return;
  S.viewCounts[from] = Math.max(0, (S.viewCounts[from] || 1) - 1);
  S.viewCounts[to] = (S.viewCounts[to] || 0) + 1;
  renderViews(kind);
}
function renderViews(kind) {
  const box = $("#views");
  if (!box) return;
  const f = S.filters[kind];
  box.innerHTML = VIEWS.map(([v, label, hint]) =>
    `<button class="view-tab ${f.view === v ? "on" : ""}" data-view="${v}" ${hint ? `title="${hint}"` : ""}>
      ${label}<span class="n">${S.viewCounts[v] || 0}</span></button>`).join("");
  $$("button", box).forEach(b => b.addEventListener("click", () => {
    f.view = b.dataset.view;
    reloadList(kind);
  }));
}
const PAGE = 100;
const saveFilters = () => store("filters2", S.filters);

// ------------------------------------------------------------ навигация

function tabFromHash() {
  const h = location.hash.slice(1);
  return TABS.includes(h) ? h : null;
}

function setTab(tab) {
  S.tab = tab;
  S.selected = null;
  S.items = [];
  S.filtersOpen = false;
  store("tab", tab);
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.tab === tab));
  render();
}

window.addEventListener("hashchange", () => setTab(tabFromHash() || "main"));

function render() {
  const view = $("#view");
  if (S.tab === "main" || S.tab === "side") return renderList(view, S.tab);
  if (S.tab === "posts") return renderList(view, "posts");
  if (S.tab === "logs") return renderLogs(view);
  if (S.tab === "settings") return renderSettings(view);
}

// версия этой страницы — из ссылки на app.js; если сервер уже новее, перезагружаемся сами
const PAGE_VERSION = (document.currentScript?.src.match(/[?&]v=(\d+)/) || [])[1];

async function loadMeta() {
  S.meta = await api("/api/meta");
  if (PAGE_VERSION && S.meta.version && S.meta.version !== PAGE_VERSION && !sessionStorage.getItem("reloadedFor" + S.meta.version)) {
    sessionStorage.setItem("reloadedFor" + S.meta.version, "1");
    location.reload();
    return;
  }
  const unseen = S.meta.unseen;
  $$("#nav a").forEach(a => {
    const c = $(".count", a);
    if (c) c.textContent = unseen[a.dataset.tab] || "";
  });
  if (!S.run.running) {
    const lr = S.meta.last_run;
    $("#runState").textContent = lr ? `Последний сбор ${fmtDate(lr.started_at, true)}` : "Сборов ещё не было";
  }
  if (S.meta.running && !S.run.running) pollRun();
}

// ------------------------------------------------------------ лента

const isPosts = kind => kind === "posts";
const itemKey = (kind, it) => (isPosts(kind) ? it.hash : it.id);
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
S.view = store("view") || "full"; // full — карточки, compact — строки
S.focus = -1;
S.open = new Set(); // развёрнутые карточки

function renderList(view, kind) {
  const f = S.filters[kind];
  const title = { main: "Вакансии", side: "Подработка", posts: "Телеграм" }[kind];
  const hint = isPosts(kind) ? "Поиск по тексту поста" : "Поиск по названию, компании, описанию";
  S.focus = -1;
  S.open = new Set();
  view.innerHTML = `
    <div class="page"><div class="feed-layout">
      <aside class="facets" aria-label="Фильтры"></aside>
      <section class="feed">
        <header class="feed-head">
          <div class="feed-title">
            <h1>${title}</h1><span class="total"></span>
            <div class="right">
              ${isPosts(kind) ? "" : `<div class="seg small" id="viewSeg">
                <button data-v="full" class="${S.view === "full" ? "on" : ""}">Подробно</button>
                <button data-v="compact" class="${S.view === "compact" ? "on" : ""}">Кратко</button>
              </div>`}
              <button class="btn ghost small" id="seenAll" title="Перенести все новые в «Просмотренные»">Всё просмотрено</button>
            </div>
          </div>
          <nav class="views" id="views" aria-label="Вкладки"></nav>
          <input type="search" class="feed-search" placeholder="${hint}" value="${esc(f.q)}">
          <div class="feed-hint"><kbd>J</kbd> <kbd>K</kbd> переход · <kbd>Enter</kbd> подробнее · <kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> статус · <kbd>O</kbd> открыть · <kbd>/</kbd> поиск</div>
        </header>
        <div class="cards"></div>
      </section>
    </div></div>`;

  $(".feed-search", view).addEventListener("input", debounce(e => { f.q = e.target.value; reloadList(kind); }, 300));
  $$("#viewSeg button", view).forEach(b => b.addEventListener("click", () => {
    S.view = b.dataset.v;
    store("view", S.view);
    $$("#viewSeg button", view).forEach(x => x.classList.toggle("on", x === b));
    renderCards(kind);
  }));
  $("#seenAll", view).addEventListener("click", async () => {
    const path = isPosts(kind) ? "/api/tracking/post/seen-all" : `/api/tracking/vacancy/seen-all?kind=${kind}`;
    await api(path, { method: "POST" });
    await loadMeta();
    reloadList(kind);
  });
  renderFacets(kind);
  loadList(kind, false);
}

function reloadList(kind) {
  saveFilters();
  renderFacets(kind);
  loadList(kind, false);
}

function activeFilterCount(kind) {
  const f = S.filters[kind];
  return ["direction", "source", "channel", "has_salary", "no_experience"].filter(k => f[k]).length;
}

function renderFacets(kind) {
  const f = S.filters[kind];
  const box = $(".facets");
  if (!box) return;
  const radio = (name, value, label, count) =>
    `<button class="facet-opt ${f[name] === value ? "on" : ""}" data-name="${name}" data-value="${esc(value)}">
      <span>${esc(label)}</span>${count == null ? "" : `<span class="n">${count}</span>`}</button>`;
  const groups = [];
  if (!isPosts(kind)) {
    const checks = [["has_salary", "Зарплата указана"], ["no_experience", "Без опыта"]];
    groups.push(`<div class="facet"><h3>Условия</h3>${checks.map(([k, t]) =>
      `<label class="facet-check"><input type="checkbox" name="${k}" ${f[k] ? "checked" : ""}> ${t}</label>`).join("")}</div>`);
  }
  if (kind === "main" && (S.meta?.directions || []).length) {
    groups.push(`<div class="facet"><h3>Направление</h3>${radio("direction", "", "Любое")}${
      S.meta.directions.map(d => radio("direction", d, cap(d))).join("")}</div>`);
  }
  if (!isPosts(kind) && (S.meta?.sources || []).length > 1) {
    groups.push(`<div class="facet"><h3>Площадка</h3>${radio("source", "", "Любая")}${
      S.meta.sources.map(x => radio("source", x, SOURCE_LABEL[x] || x)).join("")}</div>`);
  }
  if (isPosts(kind) && (S.meta?.channels || []).length) {
    groups.push(`<div class="facet"><h3>Канал</h3><select name="channel"><option value="">Любой</option>${
      S.meta.channels.map(c => `<option value="${esc(c)}" ${c === f.channel ? "selected" : ""}>@${esc(c)}</option>`).join("")}</select></div>`);
  }
  const n = activeFilterCount(kind);
  box.innerHTML = `<details class="facets-box" ${window.matchMedia("(max-width: 900px)").matches ? "" : "open"}>
      <summary>Фильтры${n ? ` · ${n}` : ""}</summary>
      ${groups.join("")}
      ${n ? `<button class="btn ghost small" id="resetF">Сбросить фильтры</button>` : ""}
    </details>`;

  $$(".facet-opt[data-name]", box).forEach(b => b.addEventListener("click", () => { f[b.dataset.name] = b.dataset.value; reloadList(kind); }));
  $$("input[type=checkbox]", box).forEach(el => el.addEventListener("change", () => { f[el.name] = el.checked; reloadList(kind); }));
  $$("select", box).forEach(el => el.addEventListener("change", () => { f[el.name] = el.value; reloadList(kind); }));
  $("#resetF", box)?.addEventListener("click", () => {
    S.filters[kind] = { ...DEFAULT_FILTERS[kind], q: f.q, view: f.view };
    reloadList(kind);
  });
}

async function loadList(kind, append) {
  const f = S.filters[kind];
  const p = new URLSearchParams({ limit: PAGE, offset: append ? S.items.length : 0 });
  if (!isPosts(kind)) p.set("kind", kind);
  if (f.q) p.set("q", f.q);
  p.set("view", f.view || "new");
  if (f.direction && kind === "main") p.set("direction", f.direction);
  if (f.source) p.set("source", f.source);
  if (f.channel) p.set("channel", f.channel);
  if (!isPosts(kind)) for (const k of ["has_salary", "no_experience"]) if (f[k]) p.set(k, "true");
  // повторы и закрытые вакансии в ленте не показываются
  if (!isPosts(kind)) { p.set("hide_dups", "true"); p.set("hide_closed", "true"); }
  const data = await api(`${isPosts(kind) ? "/api/posts" : "/api/vacancies"}?${p}`);
  if (S.tab !== kind) return;
  S.items = append ? S.items.concat(data.items) : data.items;
  S.total = data.total;
  S.viewCounts = data.view_counts || {};
  S.stale = { count: data.stale_count || 0, days: data.stale_days || 14 };
  if (!append) { S.focus = -1; S.open = new Set(); }
  renderFacets(kind);
  renderViews(kind);
  renderCards(kind);
}

// ---------- карточки

function statusButtons(item) {
  // «новая» — состояние по умолчанию; кнопки переключают между ним и двумя решениями
  return `<div class="c-status">
    <button class="act ${item.status === "отклик" ? "on applied" : ""}" data-status="отклик" title="клавиша 2">Отклик</button>
    <button class="act ${item.status === "не подходит" ? "on rejected" : ""}" data-status="не подходит" title="клавиша 3">Не подходит</button>
  </div>`;
}

function skillChips(v, limit) {
  const mine = new Map((v.my_skills || []).map(s => [s.skill.toLowerCase(), s.level]));
  const chips = (v.key_skills || []).map(s => {
    const lvl = mine.get(s.toLowerCase());
    return [lvl ? 0 : 1, `<span class="skill ${lvl === "знаю" ? "know" : lvl === "частично" ? "part" : ""}">${esc(s)}</span>`];
  });
  // мои навыки, найденные в тексте, но не указанные работодателем
  for (const s of v.my_skills || []) {
    if (!(v.key_skills || []).some(k => k.toLowerCase() === s.skill.toLowerCase())) {
      chips.push([0, `<span class="skill ${s.level === "знаю" ? "know" : "part"}" title="есть в тексте вакансии">${esc(s.skill)}</span>`]);
    }
  }
  chips.sort((a, b) => a[0] - b[0]);
  const shown = chips.slice(0, limit).map(c => c[1]).join("");
  const rest = chips.length - limit;
  return shown ? `<div class="skills">${shown}${rest > 0 ? `<span class="skill more-skills">+${rest}</span>` : ""}</div>` : "";
}

function vacancyCard(v, i) {
  const closed = v.source_status !== "active";
  const salary = fmtSalary(v);
  const src = SOURCE_LABEL[v.source] || "hh.ru";
  const meta = [
    `<b>${esc(v.employer_name || "компания не указана")}</b>`,
    esc(src),
    v.work_format && esc(v.work_format),
    v.experience && esc(v.experience),
    v.employment_form && v.employment_form !== "Полная" && esc(v.employment_form),
    v.area && v.area !== "Удалённо" && esc(v.area),
  ].filter(Boolean).join('<span class="dot">·</span>');
  const flags = [];
  if ((v.excluded || []).length) flags.push(`<span class="flag bad">отсеяно: ${esc(v.excluded.join(", "))}</span>`);
  if (closed) flags.push(`<span class="flag bad">${SOURCE_STATUS[v.source_status]} на ${esc(src)}</span>`);
  if (v.duplicate_of) flags.push(`<span class="flag quiet">похоже на повтор</span>`);
  if (v.status === "не подходит" && v.reject_reason) flags.push(`<span class="flag quiet">не подходит: ${esc(v.reject_reason)}</span>`);
  for (const m of (v.marks || []).filter(m => !QUIET_MARKS.has(m))) flags.push(`<span class="flag">${esc(m)}</span>`);
  const open = S.open.has(v.id);
  const compact = S.view === "compact" && !open;
  const cls = ["card", v.seen ? "" : "unseen", i === S.focus ? "focus" : "", compact ? "compact" : "",
    v.status === "не подходит" || v.duplicate_of || closed ? "dim" : ""].join(" ");
  return `
    <article class="${cls}" data-key="${esc(v.id)}" data-i="${i}">
      <div class="c-head">
        <h2 class="c-title"><a href="${url(v.url)}" target="_blank" rel="noopener" data-open-link>${esc(v.name)}</a></h2>
        <div class="c-salary ${salary ? "" : "none"}">${salary ? esc(salary) : "зп не указана"}</div>
      </div>
      <div class="c-meta">${meta}<span class="c-date">${esc(shortDate(v.published_at))}</span></div>
      ${compact ? (flags.length ? `<div class="c-flags">${flags.join("")}</div>` : "") : `
        ${v.excerpt ? `<p class="c-excerpt">${esc(v.excerpt)}</p>` : ""}
        ${skillChips(v, 8)}
        ${flags.length ? `<div class="c-flags">${flags.join("")}</div>` : ""}`}
      <div class="c-actions">
        ${statusButtons(v)}
        <span class="spacer"></span>
        <button class="act ghost" data-toggle>${open ? "Свернуть" : "Подробнее"}</button>
        <a class="act ghost" href="${url(v.url)}" target="_blank" rel="noopener" data-open-link>Открыть ↗</a>
      </div>
      ${open ? `<div class="c-more"><div class="loading">Загружаю…</div></div>` : ""}
    </article>`;
}

function postCard(p, i) {
  const open = S.open.has(p.hash);
  const cls = ["card", "post", p.seen ? "" : "unseen", i === S.focus ? "focus" : "", p.status === "не подходит" ? "dim" : ""].join(" ");
  return `
    <article class="${cls}" data-key="${esc(p.hash)}" data-i="${i}">
      <div class="c-meta"><b><a href="https://t.me/${esc(p.channel)}" target="_blank" rel="noopener">@${esc(p.channel)}</a></b><span class="dot">·</span>${esc(fmtDate(p.date, true))}
        ${(p.keywords || []).length ? `<span class="dot">·</span><span class="kw">${p.keywords.map(esc).join(", ")}</span>` : ""}</div>
      <div class="post-text ${open ? "open" : ""}">${esc(p.text)}</div>
      <div class="c-actions">
        ${statusButtons(p)}
        <span class="spacer"></span>
        <button class="act ghost" data-toggle>${open ? "Свернуть" : "Весь текст и заметки"}</button>
        <a class="act ghost" href="${url(p.url)}" target="_blank" rel="noopener" data-open-link>Открыть пост ↗</a>
      </div>
      ${open ? `<div class="c-more">${notesBlock(p, false)}</div>` : ""}
    </article>`;
}

// ---------- группы по времени: свежее не смешивается со старым
const AGE_GROUPS = [["today", "Сегодня"], ["yesterday", "Вчера"], ["week", "На этой неделе"], ["older", "Раньше"]];
function ageGroup(iso) {
  const d = parseDate(iso);
  if (!d) return "older";
  const days = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 864e5);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : days < 7 ? "week" : "older";
}

function groupedCards() {
  const groups = Object.fromEntries(AGE_GROUPS.map(([k]) => [k, []]));
  S.items.forEach((it, i) => groups[ageGroup(it.published_at)].push(vacancyCard(it, i)));
  return AGE_GROUPS.filter(([k]) => groups[k].length).map(([k, label]) => {
    const n = `<span class="n">${groups[k].length}</span>`, cards = groups[k].join("");
    if (k !== "older") return `<section class="group"><h3 class="group-head">${label}${n}</h3>${cards}</section>`;
    const bar = S.stale.count
      ? `<div class="stale-bar"><span>Без решения старше ${S.stale.days} дней: <b>${S.stale.count}</b></span>
          <button class="btn small" id="staleBtn">Убрать в «Не подходит»</button></div>` : "";
    return `<details class="group older" ${S.olderOpen ? "open" : ""}><summary class="group-head">${label}${n}</summary>${bar}${cards}</details>`;
  }).join("");
}

function renderCards(kind) {
  const box = $(".cards");
  if (!box) return;
  $(".feed-title .total").textContent = S.total ? String(S.total) : "";
  if (!S.items.length) {
    const filtered = S.filters[kind].q || activeFilterCount(kind);
    const EMPTY = { new: "Новых нет — всё разобрано.", seen: "Нет открытых без решения.",
      applied: "Откликов пока нет.", rejected: "Здесь пусто." };
    box.innerHTML = `<div class="list-empty">${filtered ? "Ничего не найдено — попробуйте снять фильтры."
      : S.meta?.last_run && S.filters[kind].view !== "new" ? EMPTY[S.filters[kind].view] : S.meta?.last_run && !S.viewCounts.new ? EMPTY.new
      : S.meta?.last_run ? (isPosts(kind) ? "Подходящих постов пока нет." : "Здесь пока пусто.")
      : "Пока пусто — нажмите «Запустить сбор» слева."}</div>`;
    return;
  }
  const card = isPosts(kind) ? postCard : vacancyCard;
  const grouped = !isPosts(kind) && ["new", "seen"].includes(S.filters[kind].view);
  box.innerHTML = (grouped ? groupedCards() : S.items.map((it, i) => card(it, i)).join("")) +
    (S.items.length < S.total ? `<button class="btn more">Показать ещё ${Math.min(PAGE, S.total - S.items.length)}</button>` : "");
  $$(".card", box).forEach(el => bindCard(kind, el));
  $(".more", box)?.addEventListener("click", () => loadList(kind, true));
  $(".group.older", box)?.addEventListener("toggle", e => { S.olderOpen = e.target.open; });
  $("#staleBtn", box)?.addEventListener("click", async () => {
    if (!confirm(`Убрать в «Не подходит» ${S.stale.count} вакансий старше ${S.stale.days} дней без решения?`)) return;
    const r = await api(`/api/tracking/vacancy/archive-stale?kind=${kind}`, { method: "POST" });
    toast(`Убрано старых вакансий: <b>${r.archived}</b>. Они во вкладке «Не подходит» с причиной «устарела».`);
    await loadMeta();
    reloadList(kind);
  });
  for (const key of S.open) fillMore(kind, key);
}

function rerenderCard(kind, key) {
  const i = S.items.findIndex(x => itemKey(kind, x) === key);
  const el = $(`.card[data-key="${CSS.escape(key)}"]`);
  if (i < 0 || !el) return;
  el.outerHTML = (isPosts(kind) ? postCard : vacancyCard)(S.items[i], i);
  const fresh = $(`.card[data-key="${CSS.escape(key)}"]`);
  bindCard(kind, fresh);
  if (S.open.has(key)) fillMore(kind, key);
}

function bindCard(kind, el) {
  const key = el.dataset.key;
  const item = S.items.find(x => itemKey(kind, x) === key);
  $$("[data-status]", el).forEach(b => b.addEventListener("click", e => {
    e.stopPropagation();
    setStatus(kind, item, item.status === b.dataset.status ? "новая" : b.dataset.status);
  }));
  $("[data-toggle]", el).addEventListener("click", e => { e.stopPropagation(); toggleOpen(kind, key); });
  $$("[data-open-link]", el).forEach(a => a.addEventListener("click", () => markSeen(kind, item)));
  el.addEventListener("click", e => {
    if (e.target.closest("a, button, input, textarea, .c-more")) return;
    S.focus = Number(el.dataset.i);
    $$(".card.focus").forEach(c => c.classList.remove("focus"));
    el.classList.add("focus");
  });
}

function markSeen(kind, item) {
  if (!item || item.seen) return;
  const from = viewOf(item);
  item.seen = 1;
  moveView(kind, from, viewOf(item));
  api(`/api/tracking/${isPosts(kind) ? "post" : "vacancy"}/${encodeURIComponent(itemKey(kind, item))}`,
    { method: "PATCH", body: { seen: true } }).then(loadMeta);
  $(`.card[data-key="${CSS.escape(itemKey(kind, item))}"]`)?.classList.remove("unseen");
}

function toggleOpen(kind, key) {
  const item = S.items.find(x => itemKey(kind, x) === key);
  if (S.open.has(key)) S.open.delete(key);
  else { S.open.add(key); markSeen(kind, item); }
  rerenderCard(kind, key);
}

async function fillMore(kind, key) {
  const el = $(`.card[data-key="${CSS.escape(key)}"] .c-more`);
  if (!el) return;
  const item = S.items.find(x => itemKey(kind, x) === key);
  if (isPosts(kind)) { bindNotes(el, kind, item); return; }
  const v = await api(`/api/vacancies/${encodeURIComponent(key)}`);
  if (!S.open.has(key)) return;
  const facts = [
    ["Занятость", v.employment_form], ["График", v.schedule], ["Регион", v.area !== "Удалённо" ? v.area : ""],
    ["Работодатель", EMPLOYER_TYPE[v.employer_type] || v.employer_type],
    ["Направление", v.direction || (v.kind === "side" ? "подработка" : "")],
    ["Опубликована", fmtDate(v.published_at, true)], ["Найдено по", (v.queries || []).join("; ")],
  ].filter(([, x]) => x);
  const desc = (v.segments || []).map(s => s.level ? `<mark class="hl-${s.level}" title="${esc(s.skill)}">${esc(s.t)}</mark>` : esc(s.t)).join("");
  const links = [];
  if (v.original) links.push(`Похоже на повтор вакансии «${esc(v.original.name)}» — <a href="${url(v.original.url)}" target="_blank" rel="noopener">открыть оригинал ↗</a>`);
  if (v.repeats?.length) links.push(`${v.duplicate_of ? "Другие повторы" : "Повторы этой вакансии"}: ${v.repeats.map(r => `<a href="${url(r.url)}" target="_blank" rel="noopener">${esc(fmtDate(r.published_at))}</a>`).join(", ")}`);
  if (v.detail_status === "compacted") links.push("Описание удалено при очистке: вакансия старше 30 дней. Название, компания и ссылка остались.");
  else if (v.detail_status !== "ok") links.push(`Полное описание не загружено${v.detail_status === "pending" ? " — догрузится при следующем сборе" : ""}.`);
  el.innerHTML = `
    ${links.map(l => `<div class="banner">${l}</div>`).join("")}
    <dl class="facts">${facts.map(([k, x]) => `<div><dt>${esc(k)}</dt><dd>${esc(x)}</dd></div>`).join("")}</dl>
    ${(v.key_skills || []).length > 8 ? `<div class="d-section"><h3>Все навыки</h3>${skillChips(v, 99)}</div>` : ""}
    ${notesBlock(v, true)}
    <div class="d-section"><h3>Описание</h3><div class="desc">${desc || '<span class="muted">нет текста</span>'}</div></div>
    <button class="act ghost" data-collapse>Свернуть</button>`;
  bindNotes(el, kind, item);
  $("[data-collapse]", el).addEventListener("click", () => {
    toggleOpen(kind, key);
    $(`.card[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({ block: "nearest" });
  });
}

function notesBlock(item, withDate) {
  return `
    <div class="d-section notes-section">
      <h3>Мои заметки</h3>
      <div class="notes ${withDate ? "" : "no-date"}">
        ${withDate ? `<label class="date-field"><span>Дата отклика</span><input type="date" name="applied_at" value="${esc(item.applied_at || "")}"></label>` : ""}
        <textarea name="notes" placeholder="Заметка: контакты, ответы, договорённости…">${esc(item.notes || "")}</textarea>
        <div class="saved"></div>
      </div>
    </div>`;
}

function bindNotes(el, kind, item) {
  const type = isPosts(kind) ? "post" : "vacancy";
  const key = itemKey(kind, item);
  const saved = $(".saved", el);
  const save = async body => {
    try {
      const t = await api(`/api/tracking/${type}/${encodeURIComponent(key)}`, { method: "PATCH", body });
      Object.assign(item, { applied_at: t.applied_at, notes: t.notes });
      saved.textContent = "сохранено";
    } catch (e) { saved.textContent = e.message; }
  };
  $("input[name=applied_at]", el)?.addEventListener("change", e => save({ applied_at: e.target.value || null }));
  $("textarea[name=notes]", el)?.addEventListener("input", debounce(e => save({ notes: e.target.value }), 600));
}

const REJECT_REASONS = ["стек", "опыт", "зарплата", "компания", "формат", "неинтересно"];

// карточка с новым решением уходит из текущей вкладки сразу
function dropItem(kind, key) {
  const i = S.items.findIndex(x => itemKey(kind, x) === key);
  if (i < 0) return;
  S.items.splice(i, 1);
  S.total = Math.max(0, S.total - 1);
  S.focus = S.items.length ? Math.min(i, S.items.length - 1) : -1;
  S.open.delete(key);
  renderCards(kind);
}

async function setStatus(kind, item, status) {
  const type = isPosts(kind) ? "post" : "vacancy";
  const key = itemKey(kind, item);
  const was = { status: item.status, seen: item.seen };
  const body = { status, seen: true };
  if (status === "отклик" && !item.applied_at) body.applied_at = today();
  try {
    const t = await api(`/api/tracking/${type}/${encodeURIComponent(key)}`, { method: "PATCH", body });
    const from = viewOf(item);
    Object.assign(item, { status: t.status, applied_at: t.applied_at, seen: 1, reject_reason: t.reject_reason });
    moveView(kind, from, viewOf(item));
    if (viewOf(item) !== S.filters[kind].view) dropItem(kind, key);
    else rerenderCard(kind, key);
    loadMeta();
    if (status === "не подходит") rejectToast(kind, type, key, was);
    else if (status === "отклик") undoToast(kind, type, key, was, "Отклик отмечен.");
  } catch (e) { toast(esc(e.message)); }
}

function bindUndo(kind, type, key, was) {
  $("#toast [data-undo]")?.addEventListener("click", async () => {
    await api(`/api/tracking/${type}/${encodeURIComponent(key)}`,
      { method: "PATCH", body: { status: "новая", seen: !!was.seen } }).catch(e => toast(esc(e.message)));
    $("#toast").hidden = true;
    await loadMeta();
    reloadList(kind);
  });
}

function undoToast(kind, type, key, was, text) {
  toast(`${text} <a data-undo>Отменить</a>`, 8000);
  bindUndo(kind, type, key, was);
}

// «Не подходит» — одно нажатие; причину можно указать сразу (она пригодится для правил отсева) или не указывать
function rejectToast(kind, type, key, was) {
  toast(`<b>Не подходит.</b> Причина (необязательно):<div class="reasons">${
    REJECT_REASONS.map(r => `<button class="chip" data-reason="${r}">${r}</button>`).join("")}</div>
    <a data-undo>Отменить</a>`, 10000);
  bindUndo(kind, type, key, was);
  $$("#toast [data-reason]").forEach(b => b.addEventListener("click", async () => {
    try {
      await api(`/api/tracking/${type}/${encodeURIComponent(key)}`, { method: "PATCH", body: { reject_reason: b.dataset.reason } });
      toast(`Не подходит: <b>${b.dataset.reason}</b>. <a data-undo>Отменить</a>`, 6000);
      bindUndo(kind, type, key, was);
    } catch (e) { toast(esc(e.message)); }
  }));
}

function moveFocus(kind, delta) {
  if (!S.items.length) return;
  S.focus = Math.max(0, Math.min(S.items.length - 1, S.focus < 0 ? 0 : S.focus + delta));
  $$(".card.focus").forEach(c => c.classList.remove("focus"));
  const el = $(`.card[data-i="${S.focus}"]`);
  if (el) {
    const group = el.closest("details");
    if (group && !group.open) group.open = true;
    el.classList.add("focus");
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

// клавиши: J/K или ↓/↑ — переход, Enter — подробнее, 1/2/3 — статус, O — открыть, / — поиск
document.addEventListener("keydown", e => {
  if (!["main", "side", "posts"].includes(S.tab) || e.ctrlKey || e.metaKey || e.altKey) return;
  if (isTyping(document.activeElement)) {
    if (e.key === "Escape") document.activeElement.blur();
    return;
  }
  const kind = S.tab;
  const item = S.items[S.focus];
  const k = e.key.toLowerCase();
  if (k === "arrowdown" || k === "j" || k === "о") { e.preventDefault(); moveFocus(kind, 1); }
  else if (k === "arrowup" || k === "k" || k === "л") { e.preventDefault(); moveFocus(kind, -1); }
  else if (k === "/" || k === ".") { e.preventDefault(); $(".feed-search")?.focus(); }
  else if (!item) return;
  else if (k === "enter" || k === " ") { e.preventDefault(); toggleOpen(kind, itemKey(kind, item)); }
  else if (k === "1") setStatus(kind, item, "новая");
  else if (k === "2") setStatus(kind, item, "отклик");
  else if (k === "3") setStatus(kind, item, "не подходит");
  else if (k === "o" || k === "щ") { markSeen(kind, item); window.open(url(item.url), "_blank", "noopener"); }
});

// ------------------------------------------------------------ сборы и логи

async function renderLogs(view) {
  view.innerHTML = `
    <div class="page"><div class="page-inner">
      <h1>Сборы и логи</h1>
      <div class="block" id="runBlock"></div>
      <div class="block">
        <div class="head"><h2>Проверка доступа</h2><button class="btn" id="checkBtn">Проверить</button></div>
        <p class="muted">Ответы hh.ru, площадок и Telegram. Занимает до минуты.</p>
        <div id="checkRes"></div>
      </div>
      <div class="block">
        <div class="head"><h2>Выгрузки</h2><a class="btn" href="/api/export.xlsx">Всё в Excel</a></div>
        <div id="dumps" class="muted">…</div>
      </div>
      <div class="block" id="stored"></div>
      <div class="block">
        <h2>История сборов</h2>
        <div id="runs">…</div>
      </div>
    </div></div>`;
  renderRunBlock();
  $("#checkBtn").addEventListener("click", async e => {
    e.target.disabled = true;
    $("#checkRes").innerHTML = `<p class="muted">Проверяю…</p>`;
    try {
      const res = await api("/api/check", { method: "POST" });
      const ok = res.results.every(r => r.ok);
      $("#checkRes").innerHTML = `<p class="${ok ? "ok" : "err"}"><b>${ok ? "hh.ru отвечает" : "hh.ru не отвечает"}</b> — ${esc(res.results[0]?.message || "")}</p>
        <pre class="log">${esc(res.report)}</pre><p class="muted">Отчёт сохранён: <code>${esc(res.path || "")}</code></p>`;
    } catch (err) {
      $("#checkRes").innerHTML = `<p class="err">${esc(err.message)}</p>`;
    } finally { e.target.disabled = false; }
  });
  const dumps = await api("/api/dumps");
  $("#dumps").innerHTML = dumps.length
    ? `<p>Новое за каждый сбор, файлы в папке <code>dumps</code>:</p><div class="files">${dumps.slice(0, 40).map(d => `<a href="/api/dumps/${esc(d)}" download>${esc(d)}</a>`).join("")}</div>`
    : "Дампов пока нет.";
  const st0 = S.meta?.stored;
  if (st0) {
    $("#stored").innerHTML = `<h2>База</h2><p class="muted">Вакансий в базе: <b>${st0.total}</b>. Из них скрыто правилами отсева: <b>${st0.excluded}</b>
      (они хранятся, чтобы не собираться заново), с удалёнными описаниями при очистке: <b>${st0.compacted}</b>.
      Очистка идёт сама при каждом сборе: описания отклонённых и устаревших вакансий старше 30 дней удаляются, откликнутые и с заметками не трогаются.</p>`;
  }
  const runs = await api("/api/runs");
  const st = { ok: "успешно", partial: "с ошибками", failed: "прерван", running: "идёт" };
  $("#runs").innerHTML = runs.length ? `
    <table>
      <tr><th>Начало</th><th>Итог</th><th class="num">Запросов</th><th class="num">Вакансий</th><th class="num">Подработки</th><th class="num">Постов</th><th>Замечания</th></tr>
      ${runs.map(r => `<tr>
        <td class="nowrap">${esc(fmtDate(r.started_at, true))}</td>
        <td class="nowrap ${r.status === "ok" ? "ok" : r.status === "running" ? "" : "err"}">${st[r.status] || esc(r.status)}</td>
        <td class="num">${r.requests}</td><td class="num">${r.new_main}</td><td class="num">${r.new_side}</td><td class="num">${r.new_posts}</td>
        <td>${[
          (r.skipped_channels || []).length ? `пропущены каналы: ${r.skipped_channels.map(c => "@" + esc(c)).join(", ")}` : "",
          ...(r.errors || []).map(esc),
        ].filter(Boolean).map(x => `<div>${x}</div>`).join("")}</td>
      </tr>`).join("")}
    </table>` : `<p class="muted">Сборов ещё не было.</p>`;
}

function renderRunBlock() {
  const box = $("#runBlock");
  if (!box) return;
  const r = S.run;
  box.hidden = !r.running && !r.result && !r.log.length;
  if (box.hidden) return;
  const title = r.running ? "Идёт сбор" : { ok: "Сбор завершён", partial: "Сбор завершён с ошибками", failed: "Сбор прерван" }[r.result?.status] || "Сбор";
  const res = r.result;
  box.innerHTML = `
    <div class="head"><h2>${title}</h2></div>
    ${res && !r.running ? `<p>Новых вакансий: <b>${res.new_main ?? 0}</b>, подработки: <b>${res.new_side ?? 0}</b>, постов: <b>${res.new_posts ?? 0}</b>${res.dump ? ` · <a href="/api/dumps/${esc(res.dump)}" download>дамп ${esc(res.dump)}</a>` : ""}</p>` : ""}
    <pre class="log" id="runLog">${esc(r.log.join("\n"))}</pre>`;
  const pre = $("#runLog");
  pre.scrollTop = pre.scrollHeight;
}

// ------------------------------------------------------------ запуск сбора

function toast(html, ms = 12000) {
  const t = $("#toast");
  t.innerHTML = `<button class="close" title="закрыть">×</button>${html}`;
  t.hidden = false;
  $(".close", t).addEventListener("click", () => { t.hidden = true; });
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms);
}

$("#runBtn").addEventListener("click", async () => {
  try {
    await api("/api/run", { method: "POST" });
  } catch (e) {
    if (!/уже идёт/.test(e.message)) { toast(esc(e.message)); return; }
  }
  pollRun();
});

async function pollRun() {
  if (S.run.running) return;
  S.run = { log: [], result: null, running: true };
  const btn = $("#runBtn"), state = $("#runState");
  btn.disabled = true;
  btn.textContent = "Идёт сбор…";
  renderRunBlock();
  let since = 0, st;
  do {
    st = await api(`/api/run?since=${since}`).catch(() => null);
    if (st) {
      S.run.log.push(...st.log);
      since = st.log_len;
      const last = S.run.log.filter(l => !l.startsWith("  ")).pop() || "";
      state.innerHTML = `${esc(last.slice(0, 80))}<br><a href="#logs">ход сбора</a>`;
      const pre = $("#runLog");
      if (pre) { pre.textContent = S.run.log.join("\n"); pre.scrollTop = pre.scrollHeight; }
    }
    if (st?.running) await new Promise(r => setTimeout(r, 1000));
  } while (st?.running);
  S.run.running = false;
  S.run.result = st?.result || null;
  btn.disabled = false;
  btn.textContent = "Запустить сбор";
  const r = S.run.result;
  if (r) {
    const label = { ok: "Сбор завершён", partial: "Сбор завершён с ошибками", failed: "Сбор прерван" }[r.status] || "Сбор завершён";
    toast(`<b>${label}</b><br>Новых вакансий: ${r.new_main ?? 0}, подработки: ${r.new_side ?? 0}, постов: ${r.new_posts ?? 0}
      ${(r.errors || []).length ? `<br>Ошибок: ${r.errors.length} — <a href="#logs">подробности</a>` : ""}`);
  }
  await loadMeta();
  if (S.tab === "logs") renderLogs($("#view"));
  else if (["main", "side", "posts"].includes(S.tab)) loadList(S.tab, false);
}

// ------------------------------------------------------------ настройки

const SECTIONS = [
  ["channels", "Телеграм-каналы"],
  ["queries", "Запросы hh"],
  ["exclude", "Отсев"],
  ["marks", "Пометки"],
  ["skills", "Мои навыки"],
  ["boards", "Площадки"],
  ["settings", "Параметры"],
];
const PATTERN_HINT = `Одна строка — один вариант. Обычная фраза ищется целым словом, без учёта регистра, «ё» = «е».
  Звёздочка в конце — любое окончание: <code>таргетолог*</code> найдёт «таргетолога», «таргетологом».
  Для сложных случаев — регулярка с префиксом <code>re:</code>.`;

function renderSettings(view) {
  view.innerHTML = `
    <div class="page"><div class="page-inner">
      <h1>Настройки</h1>
      <div class="settings">
        <div class="side">${SECTIONS.map(([k, t]) => `<button data-sec="${k}" class="${k === S.settingsSection ? "active" : ""}">${t}</button>`).join("")}</div>
        <div id="secBody"></div>
      </div>
    </div></div>`;
  $$(".side button", view).forEach(b => b.addEventListener("click", () => {
    S.settingsSection = b.dataset.sec;
    store("settingsSection", S.settingsSection);
    $$(".side button", view).forEach(x => x.classList.toggle("active", x === b));
    loadSection();
  }));
  loadSection();
}

const lines = text => text.split("\n").map(s => s.trim()).filter(Boolean);

async function loadSection() {
  const body = $("#secBody");
  const name = S.settingsSection;
  const data = await api(`/api/config/${name}`);
  const renderers = { channels: formChannels, queries: formQueries, exclude: formExclude, marks: formMarks, skills: formSkills, boards: formBoards, settings: formParams };
  body.innerHTML = `<form class="form" autocomplete="off"></form>`;
  const form = $("form", body);
  const collect = renderers[name](form, data);
  form.insertAdjacentHTML("beforeend", `<div class="actions"><button class="btn primary" type="submit">Сохранить</button><span class="saved"></span></div><ul class="errors"></ul>`);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const saved = $(".actions .saved", form), errs = $(".errors", form);
    errs.innerHTML = "";
    try {
      const res = await api(`/api/config/${name}`, { method: "PUT", body: collect() });
      saved.textContent = res._stats
        ? `сохранено · отсеяно вакансий: ${res._stats.vacancies}${res._stats.returned ? `, вернулось: ${res._stats.returned}` : ""}`
        : "сохранено";
      setTimeout(() => (saved.textContent = ""), 2500);
      loadMeta();
    } catch (err) {
      saved.textContent = "";
      errs.innerHTML = (err.details || [err.message]).map(x => `<li>${esc(x)}</li>`).join("");
    }
  });
}

// универсальный список строк с кнопками «добавить» и «удалить»
function itemList(form, cls, items, rowHtml, addLabel, blank) {
  const box = document.createElement("div");
  box.className = "items";
  form.appendChild(box);
  const add = item => {
    const el = document.createElement("div");
    el.className = `item ${cls}`;
    el.innerHTML = rowHtml(item) + `<button type="button" class="btn small danger" title="удалить">✕</button>`;
    $("button.danger", el).addEventListener("click", () => el.remove());
    box.appendChild(el);
  };
  items.forEach(add);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn small";
  btn.textContent = addLabel;
  btn.addEventListener("click", () => add(blank()));
  form.appendChild(btn);
  return () => $$(".item", box);
}

function formChannels(form, data) {
  form.innerHTML = `<h2>Телеграм-каналы</h2>
    <p class="hint">Публичные каналы. Можно вставить ссылку <code>https://t.me/имя</code> или <code>@имя</code>. Если у канала выключено веб-превью, он будет пропускаться — это видно в «Логах».</p>`;
  const rows = itemList(form, "inline", data.channels || [],
    c => `<input type="checkbox" name="enabled" ${c.enabled !== false ? "checked" : ""} title="включён">
          <input type="text" name="name" value="${esc(c.name || "")}" placeholder="https://t.me/channel">`,
    "+ канал", () => ({ name: "", enabled: true }));
  form.insertAdjacentHTML("beforeend", `<div class="sub"><h2>Ключевые слова</h2>
    <p class="hint">Пост попадает в панель, если в нём есть хотя бы одно слово. ${PATTERN_HINT}</p>
    <textarea name="keywords" rows="10">${esc((data.keywords || []).join("\n"))}</textarea></div>`);
  return () => ({
    channels: rows().map(r => ({ name: $("[name=name]", r).value, enabled: $("[name=enabled]", r).checked })),
    keywords: lines($("[name=keywords]", form).value),
  });
}

function formQueries(form, data) {
  const dirs = data.directions || [];
  const side = data.side_jobs || {};
  form.innerHTML = `<h2>Запросы hh</h2>
    <p class="hint">Синтаксис — <a href="https://hh.ru/article/1175" target="_blank" rel="noopener">язык поиска hh</a>: <code>OR</code>, фразы в кавычках.
    <b>id</b> — постоянный ключ запроса: по нему запоминается, с какой даты брать вакансии. Новый id = новый запрос, он возьмёт вакансии за первые N дней.</p>
    <div class="field"><span>Направления (по одному в строке; порядок = приоритет)</span><textarea name="directions" rows="3">${esc(dirs.join("\n"))}</textarea></div>`;
  const dirSelect = d => `<select name="direction"><option value="">—</option>${dirs.map(x => `<option ${x === d ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`;
  const rows = itemList(form, "query", data.queries || [],
    q => `<input type="checkbox" name="enabled" ${q.enabled !== false ? "checked" : ""} title="включён">
          <input type="text" name="id" value="${esc(q.id || "")}" placeholder="id латиницей">
          <input type="text" name="text" value="${esc(q.text || "")}" placeholder="текст запроса">
          ${dirSelect(q.direction)}`,
    "+ запрос", () => ({ id: "", text: "", direction: dirs[0] || "", enabled: true }));
  const forms = side.employment_forms || ["PART", "PROJECT"];
  form.insertAdjacentHTML("beforeend", `<div class="sub"><h2>Подработка</h2>
    <p class="hint">Отдельный поиск по частичной и проектной занятости, удалённо. Попадает во вкладку «Подработка».</p>
    <label class="field"><span><input type="checkbox" name="side_enabled" ${side.enabled !== false ? "checked" : ""}> искать подработку</span></label>
    <div class="field"><span>Текст запроса (пусто — любая тема)</span><textarea name="side_text" rows="4">${esc(side.text || "")}</textarea></div>
    <label class="field"><span><input type="checkbox" name="side_name" ${side.search_field === "name" ? "checked" : ""}> искать только в названии вакансии</span></label>
    <div class="field"><span>Тип занятости</span>
      <label><input type="checkbox" name="form_PART" ${forms.includes("PART") ? "checked" : ""}> частичная</label>
      <label><input type="checkbox" name="form_PROJECT" ${forms.includes("PROJECT") ? "checked" : ""}> проектная / подработка</label>
    </div></div>`);
  return () => ({
    directions: lines($("[name=directions]", form).value),
    queries: rows().map(r => ({
      id: $("[name=id]", r).value, text: $("[name=text]", r).value,
      direction: $("[name=direction]", r).value, enabled: $("[name=enabled]", r).checked,
    })),
    side_jobs: {
      enabled: $("[name=side_enabled]", form).checked,
      text: $("[name=side_text]", form).value.replace(/\s*\n\s*/g, " ").trim(),
      search_field: $("[name=side_name]", form).checked ? "name" : "",
      employment_forms: ["PART", "PROJECT"].filter(f => $(`[name=form_${f}]`, form).checked),
    },
  });
}

// типы правил без слов
const NO_WORDS = ["salary", "duplicate", "closed"];

function formExclude(form, data) {
  form.innerHTML = `<h2>Отсев</h2>
    <p class="hint">Вакансии, которые точно не нужны. Отсеянная вакансия скрывается из ленты и дампа, но остаётся в базе:
    заново не соберётся, а если поменять правило — вернётся. Посмотреть отсеянное — фильтр «Только отсеянные» в ленте.
    Вакансии, где есть отклик или заметка, не отсеиваются. ${PATTERN_HINT}</p>
    <p class="hint"><b>Где искать:</b> «в названии» — только заголовок вакансии; «в требованиях» — описание без разделов
    «Будет плюсом / Желательно»; «в постах Телеграма» — весь текст поста, только для постов. Посты проверяются
    как основная работа: правила по названию — по первым строкам поста, по требованиям — по его тексту.
    <b>Исключения</b> ищутся там же: если нашлось, вакансия остаётся.</p>`;
  const opt = (value, cur, label) => `<option value="${value}" ${value === cur ? "selected" : ""}>${label}</option>`;
  const rows = itemList(form, "rule", data.rules || [],
    r => `<input type="checkbox" name="enabled" ${r.enabled !== false ? "checked" : ""} title="включено">
          <input type="text" name="name" value="${esc(r.name || "")}" placeholder="название правила">
          <select name="where">${opt("title", r.where || "title", "в названии")}${opt("requirements", r.where, "в требованиях")}${opt("post", r.where, "в постах Телеграма")}${opt("salary", r.where, "зарплата меньше")}${opt("duplicate", r.where, "повтор вакансии")}${opt("closed", r.where, "закрыта на сайте")}</select>
          <select name="scope">${opt("main", r.scope, "основная работа")}${opt("side", r.scope, "подработка")}${opt("all", r.scope || "all", "везде")}</select>
          <label class="rule-field rule-salary" ${r.where === "salary" ? "" : "hidden"}><span>Зарплата меньше, ₽ в месяц (другие валюты — по примерному курсу; без зарплаты — не отсеивается)</span>
            <input type="number" name="salary_below" min="0" step="1000" value="${esc(r.salary_below || "")}"></label>
          <label class="rule-field rule-words" ${NO_WORDS.includes(r.where) ? "hidden" : ""}><span>Слова</span><textarea name="patterns" placeholder="по одному в строке">${esc((r.patterns || []).join("\n"))}</textarea></label>
          <label class="rule-field rule-words" ${NO_WORDS.includes(r.where) ? "hidden" : ""}><span>Исключения</span><textarea name="unless" placeholder="необязательно">${esc((r.unless || []).join("\n"))}</textarea></label>
          <label class="rule-check" ${NO_WORDS.includes(r.where) ? "hidden" : ""}><input type="checkbox" name="unless_no_experience" ${r.unless_no_experience ? "checked" : ""}> не отсеивать, если опыт не нужен</label>`,
    "+ правило", () => ({ name: "", enabled: true, where: "title", scope: "main", patterns: [] }));
  const syncKind = r => {
    const where = $("[name=where]", r).value;
    $(".rule-salary", r).hidden = where !== "salary";
    $$(".rule-words, .rule-check", r).forEach(x => (x.hidden = NO_WORDS.includes(where)));
  };
  form.addEventListener("change", e => { if (e.target.name === "where") syncKind(e.target.closest(".item")); });
  return () => ({
    rules: rows().map(r => ({
      name: $("[name=name]", r).value,
      enabled: $("[name=enabled]", r).checked,
      where: $("[name=where]", r).value,
      scope: $("[name=scope]", r).value,
      patterns: lines($("[name=patterns]", r).value),
      unless: lines($("[name=unless]", r).value),
      unless_no_experience: $("[name=unless_no_experience]", r).checked,
      salary_below: Number($("[name=salary_below]", r).value) || 0,
    })),
  });
}

function formMarks(form, data) {
  form.innerHTML = `<h2>Пометки</h2>
    <p class="hint">Подсказки к вакансиям, ничего не отбрасывают. Ищутся в названии и описании. ${PATTERN_HINT}
    Пометки пересчитываются у всех вакансий при следующем сборе.</p>`;
  const API_LABEL = { no_salary: "по данным hh: зарплата не указана", private_employer: "по данным hh: работодатель частное лицо или самозанятый" };
  const rows = itemList(form, "mark", data.marks || [],
    m => `<input type="checkbox" name="enabled" ${m.enabled !== false ? "checked" : ""} title="включена">
          <input type="text" name="name" value="${esc(m.name || "")}" placeholder="название пометки" data-source="${esc(m.source || "")}">
          ${m.source ? `<span class="muted" style="grid-column:1/-1">${API_LABEL[m.source] || m.source}</span>` : `<textarea name="patterns" placeholder="слова и фразы, по одной в строке">${esc((m.patterns || []).join("\n"))}</textarea>`}`,
    "+ пометка", () => ({ name: "", enabled: true, patterns: [] }));
  return () => ({
    marks: rows().map(r => {
      const name = $("[name=name]", r);
      const m = { name: name.value, enabled: $("[name=enabled]", r).checked };
      if (name.dataset.source) m.source = name.dataset.source;
      else m.patterns = lines($("[name=patterns]", r).value);
      return m;
    }),
  });
}

function formSkills(form, data) {
  form.innerHTML = `<h2>Мои навыки</h2>
    <p class="hint">Нужны только для подсветки в описании вакансии: «знаю» — зелёным, «частично» — жёлтым. Ничего не считают и не отбрасывают.
    В «вариантах» — как ещё навык может быть написан, через запятую. ${PATTERN_HINT}</p>`;
  const rows = itemList(form, "skill", data.skills || [],
    s => `<input type="text" name="name" value="${esc(s.name || "")}" placeholder="навык">
          <select name="level">${["знаю", "частично"].map(l => `<option ${l === s.level ? "selected" : ""}>${l}</option>`).join("")}</select>
          <input type="text" name="variants" value="${esc((s.variants || []).join(", "))}" placeholder="варианты через запятую">`,
    "+ навык", () => ({ name: "", level: "знаю", variants: [] }));
  return () => ({
    skills: rows().map(r => ({
      name: $("[name=name]", r).value, level: $("[name=level]", r).value,
      variants: $("[name=variants]", r).value.split(",").map(s => s.trim()).filter(Boolean),
    })),
  });
}

function formBoards(form, data) {
  const b = data.boards || {};
  const dirs = S.meta?.directions || [];
  const BOARD_INFO = {
    habr: ["Habr Career", "ищет по словам сам, только удалёнка"],
    geekjob: ["Geekjob", "отдаёт всю ленту, слова ищутся в названии вакансии"],
    getmatch: ["getmatch", "отдаёт все вакансии, слова ищутся в названии, описании и навыках"],
  };
  form.innerHTML = `<h2>Площадки</h2>
    <p class="hint">Русскоязычные IT-площадки помимо hh. Вакансии попадают во вкладку «Вакансии» с меткой площадки.</p>
    <div class="field">${Object.entries(BOARD_INFO).map(([k, [t, d]]) =>
      `<label><input type="checkbox" name="b_${k}" ${b[k]?.enabled !== false ? "checked" : ""}> <b>${t}</b> <span class="muted">— ${d}</span></label>`).join("")}</div>
    <div class="sub"><h2>Слова для поиска</h2>
    <p class="hint">Язык запросов hh (<code>OR</code>, кавычки) здесь не работает: одно слово или фраза в строке. ${PATTERN_HINT}</p></div>`;
  const dirSelect = d => `<select name="direction"><option value="">—</option>${dirs.map(x => `<option ${x === d ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`;
  const rows = itemList(form, "inline", data.terms || [],
    t => `<input type="text" name="text" value="${esc(t.text || "")}" placeholder="слово или фраза">${dirSelect(t.direction)}`,
    "+ слово", () => ({ text: "", direction: dirs[0] || "" }));
  return () => ({
    boards: Object.fromEntries(Object.keys(BOARD_INFO).map(k => [k, { enabled: $(`[name=b_${k}]`, form).checked }])),
    terms: rows().map(r => ({ text: $("[name=text]", r).value, direction: $("[name=direction]", r).value })),
  });
}

function formParams(form, data) {
  const hh = data.hh || {}, tg = data.telegram || {}, bd = data.boards || {}, dup = data.duplicates || {};
  const areas = (hh.areas || []).map(Number);
  const other = areas.filter(a => ![16, 113, 40].includes(a));
  const num = (name, value, label, step = 1) =>
    `<label class="field"><span>${label}</span><input type="number" name="${name}" value="${esc(value ?? "")}" step="${step}"></label>`;
  form.innerHTML = `<h2>Параметры сбора</h2>
    <div class="field"><span>Откуда брать вакансии hh</span>
      <label><input type="radio" name="mode" value="web" ${hh.mode !== "api" ? "checked" : ""}> страницы сайта hh.ru — без токена</label>
      <label><input type="radio" name="mode" value="api" ${hh.mode === "api" ? "checked" : ""}> API hh.ru — сейчас без токена приложения отвечает 403</label>
    </div>
    <label class="field"><span>Почта для hh.ru (нужна только для API)</span><input type="email" name="email" value="${esc(hh.contact_email || "")}"></label>
    <div class="field"><span>Страны</span>
      <label><input type="checkbox" name="area16" ${areas.includes(16) ? "checked" : ""}> Беларусь</label>
      <label><input type="checkbox" name="area113" ${areas.includes(113) ? "checked" : ""}> Россия</label>
      <label><input type="checkbox" name="area40" ${areas.includes(40) ? "checked" : ""}> Казахстан</label>
      <input type="text" name="areas_other" value="${esc(other.join(", "))}" placeholder="другие id регионов hh через запятую">
    </div>
    <div class="grid2">
      ${num("lookback", hh.initial_lookback_days, "hh: дней назад при первом сборе (и для нового запроса)")}
      ${num("web_pause", hh.web_pause_seconds, "hh, сайт: пауза между страницами, сек", 0.1)}
      ${num("pause", hh.pause_seconds, "hh, API: пауза между запросами, сек", 0.1)}
      ${num("recheck", hh.recheck_every_days, "hh: проверять, не закрыта ли вакансия, раз в N дней")}
      ${num("overlap", hh.overlap_minutes, "hh: запас назад от прошлого сбора, минут")}
      ${num("tg_lookback", tg.initial_lookback_days, "Телеграм: дней назад при первом чтении канала")}
      ${num("tg_pause", tg.pause_seconds, "Телеграм: пауза между страницами, сек", 0.1)}
      ${num("bd_lookback", bd.initial_lookback_days, "Площадки: дней назад при первом сборе")}
      ${num("bd_pause", bd.pause_seconds, "Площадки: пауза между запросами, сек", 0.1)}
      ${num("similarity", dup.title_similarity, "Повторы: похожесть названий (0.5–1)", 0.01)}
    </div>`;
  return () => {
    const v = n => $(`[name=${n}]`, form).value;
    const areasOut = [];
    if ($("[name=area16]", form).checked) areasOut.push(16);
    if ($("[name=area113]", form).checked) areasOut.push(113);
    if ($("[name=area40]", form).checked) areasOut.push(40);
    v("areas_other").split(",").map(s => s.trim()).filter(Boolean).forEach(a => areasOut.push(a));
    return {
      hh: {
        contact_email: v("email"), app_name: hh.app_name, areas: areasOut, host: hh.host,
        mode: $("[name=mode]:checked", form)?.value || "web", web_pause_seconds: v("web_pause"),
        pause_seconds: v("pause"), initial_lookback_days: v("lookback"),
        overlap_minutes: v("overlap"), recheck_every_days: v("recheck"),
      },
      telegram: { pause_seconds: v("tg_pause"), initial_lookback_days: v("tg_lookback") },
      boards: { pause_seconds: v("bd_pause"), initial_lookback_days: v("bd_lookback") },
      duplicates: { title_similarity: v("similarity") },
    };
  };
}

// ------------------------------------------------------------ старт

loadMeta()
  .then(() => {
    const tab = tabFromHash() || store("tab") || "main";
    if (location.hash.slice(1) !== tab) history.replaceState(null, "", "#" + tab);
    setTab(tab);
  })
  .catch(e => {
    $("#view").innerHTML = `<div class="list-empty err">Не удалось загрузить панель: ${esc(e.message)}</div>`;
  });
