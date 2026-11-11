// Мини-приложение «Метрика АСП» для MAX. Вся логика — на сервере бота (webapp.py),
// здесь только экраны. Каждый запрос подписан initData из MAX — так сервер знает,
// кто открыл приложение.
(function () {
  "use strict";
  const WA = window.WebApp || null;
  const initData = (WA && WA.initData) || "";
  const app = document.getElementById("app");
  const nav = document.getElementById("nav");
  let me = null;
  let tab = "new";
  let draft = null;      // текущая оценка

  // ------------------------------------------------------------ helpers
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v) => Math.round(Number(v)).toLocaleString("ru-RU");
  const money = (v) => (v == null || v === "" ? "—" : Math.round(Number(v)).toLocaleString("ru-RU") + " ₽");
  const when = (ts) => {
    if (!ts) return "";
    const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
    if (isNaN(d)) return String(ts);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  const linkify = (text) => esc(text).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" data-ext="1">$1</a>');

  async function api(method, body) {
    const resp = await fetch("/api/" + method, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Init-Data": initData },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await resp.json(); } catch (e) { /* пусто */ }
    if (!resp.ok || data.error) throw new Error(data.error || ("Ошибка " + resp.status));
    return data;
  }

  // поле суммы: «2800000» -> «2 800 000» прямо при вводе
  function moneyInput(el) {
    el.addEventListener("input", () => {
      const digits = el.value.replace(/\D/g, "");
      el.value = digits ? Number(digits).toLocaleString("ru-RU") : "";
    });
  }

  function render(html) { app.innerHTML = html; bindLinks(); window.scrollTo(0, 0); }
  function spinner(text) { render(`<div class="spinner">${esc(text || "Загрузка…")}</div>`); }
  function errorBox(e) { return `<div class="error">${esc(e.message || e)}</div>`; }

  function bindLinks() {
    app.querySelectorAll("a[data-ext]").forEach((a) => a.addEventListener("click", (ev) => {
      if (WA && WA.openLink) { ev.preventDefault(); WA.openLink(a.href); }
    }));
  }

  function $(sel) { return app.querySelector(sel); }
  function on(sel, fn) { const el = $(sel); if (el) el.addEventListener("click", fn); }

  // ------------------------------------------------------------ navigation
  function drawNav() {
    const tabs = [["new", "🚗", "Оценка"], ["approvals", "✅", "Согласования"], ["my", "📋", "Мои"]];
    if (me.history) tabs.push(["history", "🗂", "История"]);
    if (me.admin) tabs.push(["admin", "⚙️", "Админ"]);
    nav.innerHTML = tabs.map(([id, ico, title]) =>
      `<a href="#" data-tab="${id}" class="${tab === id ? "on" : ""}"><span class="ico">${ico}</span>${title}` +
      (id === "approvals" && me.approvals ? `<span class="badge">${me.approvals}</span>` : "") + "</a>").join("");
    nav.hidden = false;
    nav.querySelectorAll("a").forEach((a) => a.addEventListener("click", (ev) => {
      ev.preventDefault(); go(a.dataset.tab);
    }));
  }

  function go(t) {
    tab = t; drawNav();
    ({ new: screenNew, approvals: screenApprovals, my: screenMy, history: screenHistory, admin: screenAdmin }[t] || screenNew)();
  }

  // ------------------------------------------------------------ login
  function screenLogin(msg) {
    nav.hidden = true;
    if (!me.bot_enabled) { render(`<h1>Метрика АСП</h1><div class="note">Бот временно отключён администратором.</div>`); return; }
    if (!me.email_login) {
      render(`<h1>Метрика АСП</h1><div class="card">У вас пока нет доступа.
        <button id="req">Запросить доступ у администратора</button></div>${msg ? `<div class="note">${esc(msg)}</div>` : ""}`);
      on("#req", async () => { try { const r = await api("login", {}); screenLogin(r.text); } catch (e) { screenLogin(e.message); } });
      return;
    }
    render(`<h1>Вход</h1>
      <div class="card">
        <div>Оценка и согласование выкупа авто. Войдите по корпоративной почте (${esc(me.email_domains)}) — придёт код из 6 цифр.</div>
        <label>Корпоративная почта</label><input id="email" type="email" inputmode="email" autocomplete="email" placeholder="i.ivanov${esc((me.email_domains || "").split(",")[0])}">
        <button id="send">Получить код</button>
        <label>Код из письма</label><input id="code" inputmode="numeric" maxlength="7" placeholder="000000">
        <button id="check" class="secondary">Войти</button>
      </div>${msg ? `<div class="note">${esc(msg)}</div>` : ""}`);
    on("#send", async () => {
      try { const r = await api("login", { email: $("#email").value }); screenLogin(r.text); }
      catch (e) { screenLogin(e.message); }
    });
    on("#check", async () => {
      try {
        const r = await api("login", { code: $("#code").value });
        if (r.allowed) { await start(); } else { screenLogin(r.text); }
      } catch (e) { screenLogin(e.message); }
    });
  }

  // ------------------------------------------------------------ new appraisal
  function screenNew(msg) {
    draft = null;
    render(`<h1>Новая оценка</h1>
      <div class="card">
        <label>Ссылка на оценку из Автохаба, VIN или номер сделки</label>
        <input id="q" placeholder="https://… или XW8ZZZ…" autocomplete="off">
        <button id="find">Найти оценку</button>
      </div>${msg ? `<div class="note">${linkify(msg)}</div>` : ""}
      <p class="muted">Ручной ввод без Автохаба и предварительный расчёт — в чате с ботом.</p>`);
    on("#find", lookup);
    $("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") lookup(); });
  }

  async function lookup() {
    const q = $("#q").value.trim();
    if (!q) return;
    spinner("Ищу оценку в Автохабе…");
    try { const r = await api("lookup", { q }); draft = Object.assign(r, { _scroll: !!r.result }); drawDraft(); }
    catch (e) { screenNew(); app.insertAdjacentHTML("beforeend", errorBox(e)); }
  }

  function chips(name, options, value) {
    return `<div class="chips" data-name="${name}">` + options.map((o) => {
      const v = typeof o === "object" ? o.value : o, t = typeof o === "object" ? o.label : o;
      return `<span class="chip ${v === value ? "on" : ""}" data-v="${esc(v)}">${esc(t)}</span>`;
    }).join("") + "</div>";
  }

  function collect() {
    const f = {};
    app.querySelectorAll(".chips[data-name]").forEach((c) => {
      const on = c.querySelector(".chip.on"); if (on) f[c.dataset.name] = on.dataset.v;
    });
    app.querySelectorAll("input[data-f]").forEach((i) => { f[i.dataset.f] = i.value.trim(); });
    return f;
  }

  function drawDraft(errorText) {
    const d = draft.draft, o = d.options, ed = d.editable || [];
    const res = draft.result || null;
    const autoLine = (label, value) => `<span>${label}</span><b>${value == null || value === "" ? '<span class="muted">не заполнено</span>' : esc(value)}</b>`;
    let html = `<h1>${esc(d.car)}</h1>
      <div class="card"><div class="kv">
        ${autoLine("Автосалон", d.salon_label || d.dealer_name)}
        ${autoLine("Оценщик", d.appraiser)}
        ${autoLine("VIN", d.vin)}
        ${autoLine("Год выпуска", d.year)}
        ${autoLine("Пробег", d.mileage == null ? null : num(d.mileage) + " км")}
        ${ed.includes("reception_type") ? "" : autoLine("Тип приёма", d.reception_type)}
        ${autoLine("ДТП / окрасов", (d.accidents ?? "—") + " / " + (d.repainted ?? "—"))}
        ${ed.includes("colors") ? "" : autoLine("Окрасы", d.colors)}
        ${ed.includes("extra") ? "" : autoLine("Доп. данные", d.extra)}
      </div></div>
      <div class="card"><div class="kv">
        <span>Запрос бизнеса</span><b>${d.negotiated == null ? '<span class="muted">не заполнено</span>' : money(d.negotiated)}</b>
        <span>Прогноз бизнеса (продажа)</span><b>${d.planned == null ? '<span class="muted">не заполнено</span>' : money(d.planned)}</b>
        <span>Предпродажная подготовка</span><b>${d.presale ? money(d.presale) : "не запланирована"}</b>
        <span>Авито-оценка</span><b>${money(d.avito_price)}</b>
        <span>ПЦП Автохаб</span><b>${money(d.autohub_ptsp)}</b>
      </div>
      <div class="muted" style="margin-top:6px">Данные из Автохаба. Ошибка — исправьте в Автохабе и нажмите «Обновить».
        ${d.maxposter_url ? `<a href="${esc(d.maxposter_url)}" data-ext="1">Автохаб →</a>` : ""}
        ${d.autoteka_url ? ` · <a href="${esc(d.autoteka_url)}" data-ext="1">Автотека →</a>` : ""}</div>
      <button class="secondary" id="refresh">🔄 Обновить из Автохаба</button></div>`;
    if (draft.missing_maxposter && draft.missing_maxposter.length)
      html += `<div class="error">Не заполнено в Автохабе:<br>• ${draft.missing_maxposter.map(esc).join("<br>• ")}<br>
        Без этого на согласование не отправить. Заполните оценку в Автохабе и нажмите «Обновить из Автохаба».</div>`;
    if (d.warnings && d.warnings.length) html += `<div class="note">${linkify(d.warnings.join("\n"))}</div>`;
    const labels = { reception_type: "Тип приёма", colors: "Окрасы", condition: "Тех. состояние", extra: "Доп. данные" };
    if (ed.length) {
      html += `<div class="card"><div class="muted">Этого нет в Автохабе — выберите:</div>` +
        ed.map((k) => `<label>${labels[k]}</label>${chips(k, o[k], d[k])}`).join("") +
        `<button id="calc">Посчитать</button></div>`;
    }
    if (errorText) html += `<div class="error">${esc(errorText)}</div>`;
    if (draft.missing && draft.missing.length && draft._tried)
      html += `<div class="error">Выберите: ${esc(draft.missing.join(", "))}</div>`;
    if (res) html += resultHtml(res, draft.missing_maxposter && draft.missing_maxposter.length);
    render(html);
    app.querySelectorAll(".chips[data-name]").forEach((c) => c.addEventListener("click", (ev) => {
      const chip = ev.target.closest(".chip"); if (!chip) return;
      c.querySelectorAll(".chip").forEach((x) => x.classList.remove("on")); chip.classList.add("on");
    }));
    app.querySelectorAll(".chip.cat").forEach((c) => c.addEventListener("click", () => setCategory(c.dataset.cat)));
    on("#calc", calc);
    on("#refresh", refresh);
    on("#submit", () => submit(false));
    if (res && draft._scroll) { $("#result").scrollIntoView({ behavior: "smooth" }); draft._scroll = false; }
  }

  async function refresh() {
    const fields = collect();
    spinner("Обновляю из Автохаба…");
    try { draft = await api("refresh", { fields }); drawDraft(); }
    catch (e) { drawDraft(e.message); }
  }

  // Категория: по метрике — автоматически; оценщик может только понизить (А → В/С, В → С)
  function categoryHtml(r) {
    const all = ["Категория А", "Категория В", "Категория С"];
    const allowed = r.category_options || [r.category];
    const chipsHtml = all.map((c) => {
      const letter = c.replace("Категория ", "");
      if (!allowed.includes(c)) return `<span class="chip off">${letter}</span>`;
      return `<span class="chip cat ${c === r.category ? "on" : ""}" data-cat="${esc(c)}">${letter}</span>`;
    }).join("");
    const lowered = r.category_auto && r.category !== r.category_auto;
    return `<div class="catrow"><span class="catlabel">Категория</span><div class="chips">${chipsHtml}</div></div>
      <div class="muted">${lowered ? `Понижена оценщиком. По метрике — ${esc(r.category_auto.replace("Категория ", ""))}, расчёт ниже — по ${esc(r.category.replace("Категория ", ""))}.`
        : `По метрике. Можно понизить — нажмите ${allowed.length > 1 ? allowed.slice(1).map((c) => c.replace("Категория ", "")).join(" или ") : "— ниже уже некуда"}.`}</div>`;
  }

  function resultHtml(r, blocked) {
    const cell = (v) => (v == null ? "—" : num(v));
    const rows = (r.compare || []).map((x) => {
      const arrow = x.arrow === "🔺" ? "▲" : x.arrow === "🔻" ? "▼" : "";
      const cls = x.good === true ? "up" : x.good === false ? "dn" : "";
      return `<tr><td>${esc(x.label)}</td><td>${cell(x.metric)}</td>
        <td class="dc">${arrow ? `<span class="${cls}">${arrow}</span> ` : ""}${cell(x.dc)}</td>
        <td>${x.label === "Подготовка" ? cell(x.metric) : cell(x.ddc)}</td></tr>`;
    }).join("");
    return `<div class="card" id="result">
      <div class="status">${esc(r.status_line || r.status_text)}</div>
      ${categoryHtml(r)}
      ${rows ? `<table class="cmp"><tr><th></th><th>Метрика</th><th>Прогноз ДЦ</th><th>ДДЦ</th></tr>${rows}</table>
        <div class="muted" style="margin-top:6px">▲▼ — выше/ниже метрики: <span class="up">зелёная</span> — в пользу компании, <span class="dn">красная</span> — против.</div>` : ""}
      ${r.recommendation ? `<pre>${esc(r.recommendation)}</pre>` : ""}
      ${r.card_preview ? `<details><summary>Как увидят согласующие</summary><pre>${linkify(r.card_preview)}</pre></details>` : ""}
      ${blocked ? `<div class="note">Отправка на согласование — после заполнения оценки в Автохабе.</div>`
                : `<button id="submit">📨 Отправить на согласование</button>`}
    </div>`;
  }

  async function setCategory(cat) {
    const auto = draft.result && draft.result.category_auto;
    const fields = collect();
    fields.category = cat === auto ? "auto" : cat;
    try {
      const r = await api("calc", { fields });
      draft = Object.assign(r, { _tried: true, _scroll: true });
      drawDraft();
    } catch (e) { drawDraft(e.message); }
  }

  async function calc() {
    const fields = collect();
    try {
      const r = await api("calc", { fields });
      draft = Object.assign(r, { _tried: true, _scroll: true });
      drawDraft();
    } catch (e) { drawDraft(e.message); }
  }

  async function submit(dupOk) {
    const fields = collect();
    const btn = $("#submit"); if (btn) { btn.disabled = true; btn.textContent = "Отправляю…"; }
    try {
      const r = await api("submit", { fields, dup_ok: dupOk });
      if (r.duplicate) {
        if (confirm(r.text)) return submit(true);
        if (btn) { btn.disabled = false; btn.textContent = "📨 Отправить на согласование"; }
        return;
      }
      screenNew(r.text);
    } catch (e) { drawDraft(e.message); }
  }

  // ------------------------------------------------------------ approvals
  async function screenApprovals(msg) {
    spinner();
    try {
      const r = await api("approvals");
      me.approvals = r.items.length; drawNav();
      let html = `<h1>Согласования</h1>${msg ? `<div class="note">${linkify(msg)}</div>` : ""}`;
      if (!r.items.length) html += `<div class="card muted">Нет сделок, которые ждут вашего решения.</div>`;
      r.items.forEach((it) => {
        const btns = it.actions.map((a) => ({
          approve: `<button class="small" data-act="approve" data-t="${it.token}">✅ Согласовать</button>`,
          adjust: `<button class="small secondary" data-act="adjust" data-t="${it.token}">🔄 С корректировкой</button>`,
          decline: `<button class="small secondary" data-act="decline" data-t="${it.token}">❌ Отказ</button>`,
          pats_done: `<button class="small" data-act="pats_done" data-t="${it.token}">✅ Готово (ушло на склад)</button>`,
          pats_req_diag: `<button class="small secondary" data-act="pats_req_diag" data-t="${it.token}">📎 Запросить диагностику</button>`,
          pats_req_cat: `<button class="small secondary" data-act="pats_req_cat" data-t="${it.token}">🏷 Исправить категорию</button>`,
        }[a] || "")).join("");
        html += `<div class="card"><div class="muted">${esc(it.stage_text)} · ${esc(it.salon)}</div>
          <div class="status">${esc(it.car)} — ${money(it.price)}</div>
          <details><summary>Карточка сделки</summary><pre>${linkify(it.card)}</pre></details>
          <div id="adj-${it.token}"></div><div>${btns}</div></div>`;
      });
      render(html);
      app.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", () => decide(b.dataset.t, b.dataset.act)));
    } catch (e) { render(`<h1>Согласования</h1>${errorBox(e)}`); }
  }

  async function decide(token, action) {
    if (action === "adjust") {
      const box = document.getElementById("adj-" + token);
      if (!box.innerHTML) {
        box.innerHTML = `<label>Новая цена выкупа, ₽</label><input id="ap-${token}" inputmode="numeric" data-money="1">
          <label>Новая ПЦП (цена продажи), ₽</label><input id="af-${token}" inputmode="numeric" data-money="1">
          <button class="small" id="as-${token}">Вернуть оценщику на корректировку</button>`;
        box.querySelectorAll("input[data-money]").forEach(moneyInput);
        document.getElementById("as-" + token).addEventListener("click", () => doDecide(token, "adjust", {
          price: document.getElementById("ap-" + token).value, forecast: document.getElementById("af-" + token).value }));
      }
      return;
    }
    const ask = { decline: "Отказать в приёме? Автомобиль не будет куплен.", approve: "Согласовать сделку?",
      pats_done: "Подтвердить приёмку (проверено, ушло на склад)?" }[action];
    if (ask && !confirm(ask)) return;
    doDecide(token, action, {});
  }

  async function doDecide(token, action, extra) {
    spinner("Сохраняю решение…");
    try { const r = await api("decide", Object.assign({ token, action }, extra)); screenApprovals(r.text); }
    catch (e) { screenApprovals("⚠️ " + e.message); }
  }

  // ------------------------------------------------------------ my
  async function screenMy() {
    spinner();
    try {
      const r = await api("my");
      let html = `<h1>Мои оценки</h1>`;
      if (!r.items.length) html += `<div class="card muted">Пока нет оценок.</div>`;
      else html += `<div class="card">` + r.items.map((it) => `<div class="list-item">
          <div><b>${esc(it.car)}</b> <span class="muted">${esc(when(it.ts))}</span></div>
          <div class="muted">${esc(it.salon || "")}${it.price ? " · " + money(it.price) : ""}${it.vin ? " · VIN …" + esc(String(it.vin).slice(-6)) : ""}</div>
          <div>${esc(it.status_text)} → ${esc(it.outcome)}</div>
          ${it.last_event ? `<div class="muted">${esc(it.last_event)}</div>` : ""}
          ${it.maxposter_url ? `<a href="${esc(it.maxposter_url)}" data-ext="1">Автохаб →</a>` : ""}</div>`).join("") + `</div>`;
      render(html);
    } catch (e) { render(`<h1>Мои оценки</h1>${errorBox(e)}`); }
  }

  // ------------------------------------------------------------ history
  let histSalon = "all", histPeriod = "today";
  async function screenHistory() {
    spinner();
    try {
      const opts = await api("history", { only_options: true });
      if (!opts.salons.some((s) => s.value === histSalon)) histSalon = opts.salons[0].value;
      const periods = [{ value: "today", label: "Сегодня" }, { value: "yday", label: "Вчера" }, { value: "7d", label: "7 дней" },
        { value: "month", label: "Этот месяц" }, { value: "pmonth", label: "Прошлый месяц" }, { value: "custom", label: "Свои даты" }];
      render(`<h1>История оценок</h1><div class="card">
        <label>Салон</label>${chips("salon", opts.salons, histSalon)}
        <label>Период</label>${chips("period", periods, histPeriod)}
        <div class="row" id="dates" ${histPeriod === "custom" ? "" : "hidden"}>
          <div><label>С</label><input type="date" id="from"></div><div><label>По</label><input type="date" id="to"></div></div>
        <button id="show">Показать</button></div><div id="hist"></div>`);
      app.querySelectorAll(".chips").forEach((c) => c.addEventListener("click", (ev) => {
        const chip = ev.target.closest(".chip"); if (!chip) return;
        c.querySelectorAll(".chip").forEach((x) => x.classList.remove("on")); chip.classList.add("on");
        if (c.dataset.name === "salon") histSalon = chip.dataset.v;
        if (c.dataset.name === "period") { histPeriod = chip.dataset.v; $("#dates").hidden = histPeriod !== "custom"; }
      }));
      on("#show", loadHistory);
    } catch (e) { render(`<h1>История оценок</h1>${errorBox(e)}`); }
  }

  async function loadHistory() {
    const box = $("#hist"); box.innerHTML = `<div class="spinner">Собираю оценки из Автохаба…</div>`;
    try {
      const r = await api("history", { salon: histSalon, period: histPeriod, from: $("#from").value, to: $("#to").value });
      box.innerHTML = `<pre>${esc(r.summary)}</pre>${r.note ? `<div class="note">${esc(r.note)}</div>` : ""}<div class="card">` +
        (r.items.length ? r.items.map((it) => `<div class="list-item">
          <div><b>${esc(it.car)}</b> <span class="muted">${esc(when(it.ts))}</span></div>
          <div class="muted">${[it.salon, it.avito ? "Авито " + money(it.avito) : "", it.price ? "цена " + money(it.price) : "",
            it.category, it.reception_type, it.appraiser ? "оценщик " + it.appraiser : ""].filter(Boolean).map(esc).join(" · ")}</div>
          <div>${it.sent ? "📨 " + esc(it.status_text) + " → " + esc(it.outcome) : "⚪ на согласование не отправлялась"}</div>
          ${it.mp_status ? `<div class="muted">Автохаб: ${esc(it.mp_status)}</div>` : ""}
          ${it.maxposter_url ? `<a href="${esc(it.maxposter_url)}" data-ext="1">Автохаб →</a>` : ""}</div>`).join("")
          : `<div class="muted">За этот период оценок нет.</div>`) + `</div>`;
      bindLinks();
    } catch (e) { box.innerHTML = errorBox(e); }
  }

  // ------------------------------------------------------------ admin
  async function screenAdmin(msg) {
    spinner();
    try {
      const r = await api("admin");
      let html = `<h1>Админ-панель</h1>${msg ? `<div class="note">${linkify(msg)}</div>` : ""}
        <div class="card"><div class="status">${r.bot_enabled ? "🟢 Бот работает" : "🔴 Бот остановлен"}</div>
          <div class="muted">Заявок в очереди: ${r.queue}</div>
          ${r.bot_enabled ? `<button class="secondary" data-a="bot_stop">⛔ Остановить бота</button>`
                          : `<button data-a="bot_start">▶️ Запустить бота</button>`}
          <button class="secondary" data-a="report">📊 Отчёт за 7 дней</button></div>`;
      html += `<h2>Заявки на доступ (${r.pending.length})</h2><div class="card">` + (r.pending.length ? r.pending.map((p) =>
        `<div class="list-item"><b>${esc(p.name || "без имени")}</b> <span class="muted">ID ${esc(p.id)} · ${esc(p.requested_at)}</span><div>
         <button class="small" data-a="approve_user" data-id="${esc(p.id)}">✅ Одобрить</button>
         <button class="small secondary" data-a="deny_user" data-id="${esc(p.id)}">❌ Отклонить</button></div></div>`).join("")
        : `<div class="muted">Нет заявок.</div>`) + `</div>`;
      html += `<h2>Пользователи (${r.users.length})</h2><div class="card">` + (r.users.length ? r.users.map((u) =>
        `<div class="list-item"><b>${esc(u.name || "без имени")}</b> <span class="muted">${esc(u.email)} · ID ${esc(u.id)}</span>
         <div><button class="small secondary" data-a="revoke_user" data-id="${esc(u.id)}" data-name="${esc(u.name)}">🚫 Закрыть доступ</button></div></div>`).join("")
        : `<div class="muted">Пока никого.</div>`) + `</div>`;
      html += `<h2>Чаты салонов</h2><div class="card">` + (r.salons.length ? r.salons.map((s) =>
        `<div class="list-item">${esc(s.name)} <span class="muted">· чат ${esc(s.chat_id)}</span></div>`).join("")
        : `<div class="muted">Не привязаны. В чате салона напишите /register_salon и нажмите кнопку салона.</div>`) +
        `<div class="muted" style="margin-top:6px">Чат ПАЦ: ${r.pats_chat ? esc(r.pats_chat) : "не задан (/register_pats в чате ПАЦ)"}</div></div>`;
      render(html);
      app.querySelectorAll("button[data-a]").forEach((b) => b.addEventListener("click", async () => {
        const a = b.dataset.a;
        if (a === "bot_stop" && !confirm("Остановить бота для всех? Незавершённые заявки будут закрыты.")) return;
        if (a === "revoke_user" && !confirm(`Закрыть доступ: ${b.dataset.name || b.dataset.id}?`)) return;
        try { const res = await api("admin_action", { action: a, id: b.dataset.id }); screenAdmin(res.text); }
        catch (e) { screenAdmin("⚠️ " + e.message); }
      }));
    } catch (e) { render(`<h1>Админ-панель</h1>${errorBox(e)}`); }
  }

  // ------------------------------------------------------------ start
  async function start() {
    if (!initData) {
      render(`<h1>Метрика АСП</h1><div class="note">Откройте приложение из чата с ботом «Метрика_оценка АСП» в MAX — кнопка «Открыть».</div>`);
      return;
    }
    spinner();
    try {
      me = await api("me");
      if (!me.allowed) { screenLogin(); return; }
      const startTab = WA && WA.initDataUnsafe && WA.initDataUnsafe.start_param;
      go(["approvals", "my", "history", "admin"].includes(startTab) ? startTab : (me.approvals ? "approvals" : "new"));
    } catch (e) { render(`<h1>Метрика АСП</h1>${errorBox(e)}`); }
  }
  start();
})();
