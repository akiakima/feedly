"use strict";

// 暗号化された記事データ（data.json）を合言葉で復号して表示する。
// 既読・「あとで読む」はこの端末のブラウザにだけ保存する。

const PAGE = 60;
const KEY_STORE = "nm.key";
const READ_STORE = "nm.read";
const STAR_STORE = "nm.star";

const $ = (id) => document.getElementById(id);
const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v ? JSON.parse(v) : fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* 保存できない環境（プライベートモードなど）では記憶しない */
    }
  },
  remove(key) {
    try {
      localStorage.removeItem(key);
    } catch {}
  },
};

const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const toB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));

let blob = null;
let data = null;
let reads = store.get(READ_STORE, {});
let stars = store.get(STAR_STORE, {});
const state = { category: "", view: "unread", q: "", shown: PAGE };

// ---------------------------------------------------------------- 復号

async function deriveRawKey(password) {
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  return crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: b64(blob.salt), iterations: blob.iter },
    base,
    256,
  );
}

async function decrypt(rawKey) {
  const key = await crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(blob.iv) }, key, b64(blob.ct));
  const stream = new Blob([plain]).stream().pipeThrough(new DecompressionStream("gzip"));
  return JSON.parse(await new Response(stream).text());
}

async function loadBlob() {
  const res = await fetch("data.json", { cache: "no-store" });
  if (!res.ok) throw new Error(`データを読み込めませんでした (${res.status})`);
  blob = await res.json();
}

async function tryRememberedKey() {
  const saved = store.get(KEY_STORE, null);
  if (!saved || saved.salt !== blob.salt || saved.iter !== blob.iter) return false;
  try {
    data = await decrypt(b64(saved.key));
    return true;
  } catch {
    store.remove(KEY_STORE);
    return false;
  }
}

async function onLogin(e) {
  e.preventDefault();
  const button = e.target.querySelector("button");
  const error = $("login-error");
  error.hidden = true;
  button.disabled = true;
  button.textContent = "開いています…";
  try {
    const raw = await deriveRawKey($("password").value);
    data = await decrypt(raw);
    if ($("remember").checked) store.set(KEY_STORE, { salt: blob.salt, iter: blob.iter, key: toB64(raw) });
    $("password").value = "";
    showApp();
  } catch {
    error.textContent = "合言葉が違います。";
    error.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "開く";
  }
}

function lock() {
  store.remove(KEY_STORE);
  location.reload();
}

// ---------------------------------------------------------------- 表示

function isRead(a) {
  return Boolean(reads[a.id]);
}

function setRead(a, value) {
  if (value) reads[a.id] = 1;
  else delete reads[a.id];
  store.set(READ_STORE, reads);
}

function toggleStar(a) {
  if (stars[a.id]) delete stars[a.id];
  else stars[a.id] = a; // 記事がデータから消えても残るように中身ごと保存
  store.set(STAR_STORE, stars);
}

function pruneReads() {
  const ids = new Set(data.articles.map((a) => a.id));
  for (const id of Object.keys(reads)) if (!ids.has(id)) delete reads[id];
  store.set(READ_STORE, reads);
}

function relativeTime(iso) {
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return "たった今";
  if (sec < 3600) return `${Math.floor(sec / 60)}分前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}時間前`;
  if (sec < 86400 * 7) return `${Math.floor(sec / 86400)}日前`;
  return new Date(iso).toLocaleDateString("ja-JP");
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
}

function safeLink(url) {
  return /^https?:\/\//i.test(url) ? url : "#";
}

function filtered() {
  let list = state.view === "starred" ? Object.values(stars).sort((a, b) => b.p.localeCompare(a.p)) : data.articles;
  if (state.category) list = list.filter((a) => a.c === state.category);
  if (state.view === "unread") list = list.filter((a) => !isRead(a));
  for (const word of state.q.toLowerCase().split(/\s+/).filter(Boolean)) {
    list = list.filter((a) => `${a.t}\n${a.s}`.toLowerCase().includes(word));
  }
  return list;
}

function renderSidebar() {
  const side = $("sidebar");
  side.replaceChildren();
  const unread = {};
  let total = 0;
  for (const a of data.articles) {
    if (isRead(a)) continue;
    unread[a.c] = (unread[a.c] || 0) + 1;
    total += 1;
  }
  const item = (label, count, active, onClick) => {
    const b = el("button", { type: "button", class: `nav-item${active ? " active" : ""}` },
      el("span", { text: label }), el("span", { class: "count", text: count ? String(count) : "" }));
    b.addEventListener("click", () => {
      onClick();
      document.body.classList.remove("nav-open");
      render();
    });
    side.append(b);
  };
  item("すべて", total, !state.category && state.view !== "starred", () => {
    state.category = "";
    if (state.view === "starred") state.view = "unread";
  });
  item("★ あとで読む", Object.keys(stars).length, state.view === "starred", () => {
    state.category = "";
    state.view = "starred";
  });
  side.append(el("div", { class: "nav-heading", text: "ジャンル" }));
  const names = [...data.categories];
  for (const a of data.articles) if (!names.includes(a.c)) names.push(a.c);
  for (const name of names) {
    if (!data.articles.some((a) => a.c === name)) continue;
    item(name, unread[name], state.category === name && state.view !== "starred", () => {
      state.category = name;
      if (state.view === "starred") state.view = "unread";
    });
  }
}

function renderArticle(a) {
  const li = el("li", { class: `article${isRead(a) ? " read" : ""}` });
  const link = el("a", { class: "article-title", href: safeLink(a.l), target: "_blank", rel: "noopener noreferrer", text: a.t });
  link.addEventListener("click", () => {
    setRead(a, true);
    li.classList.add("read");
    renderSidebar();
  });
  const meta = el("div", { class: "meta" },
    el("span", { text: a.f }),
    el("span", { class: `tag${a.c === "IR・開示" ? " ir" : ""}`, text: a.c }),
    el("time", { datetime: a.p, text: relativeTime(a.p) }));
  const body = el("div", { class: "article-body" }, link, a.s ? el("p", { class: "summary", text: a.s }) : null, meta);

  const star = el("button", { type: "button", class: `icon star${stars[a.id] ? " on" : ""}`, title: "あとで読む", text: "★" });
  star.addEventListener("click", () => {
    toggleStar(a);
    star.classList.toggle("on", Boolean(stars[a.id]));
    renderSidebar();
  });
  const check = el("button", { type: "button", class: "icon readmark", title: "既読 / 未読", text: "✓" });
  check.addEventListener("click", () => {
    setRead(a, !isRead(a));
    li.classList.toggle("read", isRead(a));
    renderSidebar();
  });
  li.append(body, el("div", { class: "article-actions" }, star, check));
  return li;
}

function render() {
  renderSidebar();
  const list = filtered();
  $("title").textContent = state.view === "starred" ? "あとで読む" : state.category || "すべて";
  const updated = new Date(data.generated_at).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  $("meta").textContent = `${state.q ? `「${state.q}」の検索結果 · ` : ""}${list.length} 件 · 最終更新 ${updated}`;
  $("tabs").hidden = state.view === "starred";
  $("mark-all").hidden = state.view === "starred";
  for (const b of $("tabs").querySelectorAll("button")) b.classList.toggle("active", b.dataset.view === state.view);

  const ul = $("articles");
  ul.replaceChildren(...list.slice(0, state.shown).map(renderArticle));
  $("more").hidden = list.length <= state.shown;
  const empty = $("empty");
  empty.hidden = list.length > 0;
  empty.textContent = state.view === "unread" ? "未読の記事はありません。" : state.view === "starred" ? "「あとで読む」に保存した記事はありません。" : "記事がありません。";
}

function showApp() {
  pruneReads();
  $("login").hidden = true;
  $("app").hidden = false;
  for (const id of ["logout"]) $(id).hidden = false;
  document.querySelector(".search").hidden = false;
  document.querySelector(".menu-btn").hidden = false;
  render();
}

// ---------------------------------------------------------------- イベント

$("login-form").addEventListener("submit", onLogin);
$("logout").addEventListener("click", lock);
$("more").addEventListener("click", () => {
  state.shown += PAGE;
  render();
});
$("mark-all").addEventListener("click", () => {
  const list = filtered();
  if (!list.length || !confirm(`表示中の ${list.length} 件を既読にしますか？`)) return;
  for (const a of list) reads[a.id] = 1;
  store.set(READ_STORE, reads);
  render();
});
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (!b) return;
  state.view = b.dataset.view;
  state.shown = PAGE;
  render();
});
document.querySelector(".search").addEventListener("input", (e) => {
  state.q = e.target.value.trim();
  if (state.q && state.view === "unread") state.view = "all";
  state.shown = PAGE;
  render();
});
document.addEventListener("click", (e) => {
  if (e.target.closest(".menu-btn")) document.body.classList.toggle("nav-open");
  else if (!e.target.closest(".sidebar")) document.body.classList.remove("nav-open");
});

(async () => {
  try {
    await loadBlob();
    if (await tryRememberedKey()) showApp();
    else $("password").focus();
  } catch (err) {
    $("login-error").textContent = err.message;
    $("login-error").hidden = false;
  }
})();
