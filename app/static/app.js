// 確認ダイアログ
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// 既読・あとで読むボタンをページ遷移なしで切り替える
document.addEventListener("click", async (e) => {
  const button = e.target.closest("button[data-action]");
  if (!button) return;
  e.preventDefault();
  const item = button.closest(".article");
  try {
    const res = await fetch(`/articles/${item.dataset.id}/${button.dataset.action}`, {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) throw new Error(res.status);
    const state = await res.json();
    item.classList.toggle("read", state.is_read);
    item.querySelector(".star").classList.toggle("on", state.is_starred);
  } catch {
    button.closest("form").submit();
  }
});

// 記事を開いたら既読表示にする（既読化はサーバー側のリダイレクトで行う）
document.addEventListener("click", (e) => {
  const link = e.target.closest("a[data-open]");
  if (link) link.closest(".article").classList.add("read");
});

// スマホのメニュー開閉（メニュー外をタップしたら閉じる）
document.addEventListener("click", (e) => {
  if (e.target.closest(".menu-btn")) {
    document.body.classList.toggle("nav-open");
  } else if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar, .menu-btn")) {
    document.body.classList.remove("nav-open");
  }
});
