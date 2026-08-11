<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>פנסיה</title>
<style>
  :root {
    --blue-dark: #0b4f8a; --blue: #1e6fb8; --blue-light: #eaf3fb;
    --border: #d9e6f0; --text: #1c2b39; --red: #d92b2b; --red-light: #fdecec;
    --green: #1a8a4a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "Segoe UI", Tahoma, Arial, sans-serif;
    background: #f4f8fb; color: var(--text); padding: 22px;
  }
  .top { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }
  .top h2 { margin: 0; color: var(--blue-dark); font-size: 19px; }
  .crumb { font-size: 13px; color: #5b7186; }
  .back-btn {
    background: #fff; color: var(--blue); border: 1px solid var(--blue); border-radius: 8px;
    padding: 7px 14px; font-size: 13.5px; cursor: pointer; font-weight: 600;
  }
  .back-btn:hover { background: var(--blue-light); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }
  .card {
    background: #fff; border: 1px solid var(--border); border-radius: 14px; padding: 20px 18px;
    cursor: pointer; transition: box-shadow .15s, border-color .15s; position: relative;
  }
  .card:hover { border-color: var(--blue); box-shadow: 0 4px 14px rgba(30,111,184,0.12); }
  .card .icon { font-size: 26px; }
  .card .name { font-weight: 700; font-size: 15.5px; margin-top: 10px; }
  .card .meta { font-size: 12.5px; color: #5b7186; margin-top: 6px; line-height: 1.6; }
  .card .status-dot {
    position: absolute; top: 16px; left: 16px; width: 11px; height: 11px; border-radius: 50%;
  }
  .status-dot.issue { background: var(--red); }
  .status-dot.ok { background: var(--green); }
  .fund-card .name { text-align: center; font-size: 14.5px; }
  .fund-card { text-align: center; }
  .fund-card .icon { display: block; }
  .fund-logo { height: 32px; max-width: 100px; object-fit: contain; }
  .form-wrap { max-width: 460px; }
  .form-card { background: #fff; border: 1px solid var(--border); border-radius: 14px; padding: 22px; }
  label { display: block; font-size: 13px; color: #5b7186; margin: 14px 0 5px; }
  label:first-of-type { margin-top: 0; }
  input[type="text"], input[type="password"], textarea {
    width: 100%; padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 14px; font-family: inherit; color: var(--text);
  }
  textarea { resize: vertical; }
  .checkbox-row { display: flex; align-items: center; gap: 8px; margin-top: 16px; font-size: 13.5px; }
  .checkbox-row input { width: auto; }
  .account-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .account-row input { flex: 1; }
  .account-remove {
    width: 30px; height: 30px; flex-shrink: 0; border: 1px solid var(--border); border-radius: 8px;
    background: #fff; color: var(--red); cursor: pointer; font-size: 15px; line-height: 1;
  }
  .account-remove:hover { background: var(--red-light); }
  .account-remove:disabled { opacity: 0.35; cursor: default; }
  .add-account-btn {
    background: none; border: 1px dashed var(--border); border-radius: 8px; color: var(--blue);
    font-size: 13px; padding: 7px 12px; cursor: pointer; width: 100%; margin-top: 4px;
  }
  .add-account-btn:hover { background: var(--blue-light); }
  .add-account-btn:disabled { opacity: 0.4; cursor: default; }
  .actions { display: flex; gap: 10px; margin-top: 20px; }
  .btn {
    background: var(--blue); color: #fff; border: none; border-radius: 8px; padding: 10px 20px;
    font-size: 14px; cursor: pointer; font-weight: 600;
  }
  .btn:hover { background: var(--blue-dark); }
  .btn.secondary { background: #fff; color: var(--blue); border: 1px solid var(--blue); }
  .status-msg { font-size: 13px; color: #5b7186; margin-top: 12px; }
  .hidden { display: none; }
</style>
</head>
<body>

  <div id="view-companies">
    <div class="top"><h2>🏦 פנסיה — לפי חברה</h2></div>
    <div class="grid" id="company-grid"></div>
  </div>

  <div id="view-funds" class="hidden">
    <div class="top">
      <button class="back-btn" onclick="showCompanies()">→ חזרה לחברות</button>
      <h2 id="fund-company-title"></h2>
    </div>
    <div class="grid" id="fund-grid"></div>
  </div>

  <div id="view-form" class="hidden">
    <div class="top">
      <button class="back-btn" onclick="showFunds()">→ חזרה לקרנות</button>
      <h2 id="form-title"></h2>
    </div>
    <div class="form-wrap">
      <div class="form-card">
        <label>קישור לדיווח / אתר הקרן</label>
        <input type="text" id="f-url" placeholder="https://...">

        <label>שם משתמש באתר</label>
        <input type="text" id="f-username">

        <label>סיסמה באתר</label>
        <input type="password" id="f-password" placeholder="השאר ריק כדי לשמור על הסיסמה הקיימת">

        <label>חשבונות להעברה</label>
        <div id="account-rows"></div>
        <button type="button" class="add-account-btn" id="add-account-btn" onclick="addAccountRow()">+ הוסף חשבון</button>

        <label>הערות</label>
        <textarea id="f-notes" rows="3"></textarea>

        <div class="checkbox-row">
          <input type="checkbox" id="f-issue">
          <label style="margin:0;" for="f-issue">יש בעיה / חוב שצריך לטפל בו</label>
        </div>

        <div class="actions">
          <button class="btn" onclick="saveCell()">שמירה</button>
          <button class="btn secondary" onclick="showFunds()">ביטול</button>
        </div>
        <div class="status-msg" id="form-status"></div>
      </div>
    </div>
  </div>

<script>
let companies = [];
let funds = [];
let currentCompany = null;
let currentFund = null;
let fundSummary = {};

// Official domains, used to pull each fund's real logo via Clearbit's logo
// API instead of a generic icon. Falls back to the building emoji if a
// logo fails to load (onerror handler on the <img> below).
const FUND_DOMAINS = {
  "מיטב דש": "meitav.co.il",
  "מנורה": "menoramivt.co.il",
  "כלל": "clalbit.co.il",
  "אלטשולר": "as-invest.co.il",
  "מגדל": "migdal.co.il",
  "פניקס": "fnx.co.il",
  "הראל": "harel-group.co.il",
  "מור": "more.co.il",
  "אנליסט": "analyst.co.il",
};

// A couple of funds don't have a domain-based logo indexed anywhere
// (Clearbit/Google/DuckDuckGo all miss them) -- direct Wikimedia file
// links found by hand take priority over the domain lookup for these.
const FUND_LOGO_OVERRIDES = {
  "כלל": "https://commons.wikimedia.org/wiki/Special:FilePath/%D7%9C%D7%95%D7%92%D7%95_%D7%9B%D7%9C%D7%9C_%D7%91%D7%99%D7%98%D7%95%D7%97.svg",
  "אלטשולר": "https://he.wikipedia.org/wiki/Special:FilePath/%D7%90%D7%9C%D7%98%D7%A9%D7%95%D7%9C%D7%A8_%D7%A9%D7%97%D7%9D.png",
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function loadCompanies() {
  const res = await fetch("/api/pension/companies");
  companies = res.ok ? await res.json() : [];
  const res2 = await fetch("/api/pension/funds");
  funds = res2.ok ? await res2.json() : [];
  renderCompanies();
}

function renderCompanies() {
  document.getElementById("company-grid").innerHTML = companies.map(c => `
    <div class="card" onclick="showFunds('${escapeHtml(c.name)}')">
      <div class="icon">🏢</div>
      <div class="name">${escapeHtml(c.name)}</div>
      <div class="meta">ח.פ ${escapeHtml(c.hp)}<br>תיק ניכויים ${escapeHtml(c.tik)}</div>
    </div>
  `).join("");
}

function showCompanies() {
  document.getElementById("view-funds").classList.add("hidden");
  document.getElementById("view-form").classList.add("hidden");
  document.getElementById("view-companies").classList.remove("hidden");
}

async function showFunds(companyName) {
  if (companyName) currentCompany = companyName;
  document.getElementById("view-companies").classList.add("hidden");
  document.getElementById("view-form").classList.add("hidden");
  document.getElementById("view-funds").classList.remove("hidden");
  document.getElementById("fund-company-title").textContent = currentCompany + " — קרנות פנסיה";

  const res = await fetch(`/api/pension/summary?company=${encodeURIComponent(currentCompany)}`);
  fundSummary = res.ok ? await res.json() : {};

  document.getElementById("fund-grid").innerHTML = funds.map(f => {
    const s = fundSummary[f];
    let dot = "";
    if (s) dot = `<span class="status-dot ${s.has_issue ? "issue" : "ok"}"></span>`;
    const domain = FUND_DOMAINS[f];
    const override = FUND_LOGO_OVERRIDES[f];
    // Chain of logo sources tried in order via onerror, since no single
    // service indexes every Israeli finance company: a hand-picked direct
    // link (if we found one) or Clearbit's logo first, then Google's
    // favicon, then DuckDuckGo's icon service, then the building emoji.
    const chain = [
      override || (domain ? `https://logo.clearbit.com/${domain}?size=120` : null),
      domain ? `https://www.google.com/s2/favicons?domain=${domain}&sz=64` : null,
      domain ? `https://icons.duckduckgo.com/ip3/${domain}.ico` : null,
    ].filter(Boolean);
    const logo = chain.length
      ? `<img class="fund-logo" src="${chain[0]}" alt="${escapeHtml(f)}" data-chain='${JSON.stringify(chain.slice(1))}' onerror="const c=JSON.parse(this.dataset.chain||'[]');if(c.length){this.src=c.shift();this.dataset.chain=JSON.stringify(c);}else{this.replaceWith(Object.assign(document.createElement('span'),{className:'icon',textContent:'🏦'}));}">`
      : `<span class="icon">🏦</span>`;
    return `
      <div class="card fund-card" onclick="openCell('${escapeHtml(f)}')">
        ${dot}
        ${logo}
        <div class="name">${escapeHtml(f)}</div>
      </div>
    `;
  }).join("");
}

const MIN_ACCOUNTS = 2;
const MAX_ACCOUNTS = 5;

function renderAccountRows(values) {
  const wrap = document.getElementById("account-rows");
  wrap.innerHTML = values.map((v, i) => `
    <div class="account-row">
      <input type="text" class="account-input" placeholder="שם החשבון" value="${escapeHtml(v)}">
      <button type="button" class="account-remove" onclick="removeAccountRow(${i})" ${values.length <= MIN_ACCOUNTS ? "disabled" : ""}>×</button>
    </div>
  `).join("");
  document.getElementById("add-account-btn").disabled = values.length >= MAX_ACCOUNTS;
}

function getAccountValues() {
  return Array.from(document.querySelectorAll(".account-input")).map(el => el.value);
}

function addAccountRow() {
  const values = getAccountValues();
  if (values.length >= MAX_ACCOUNTS) return;
  values.push("");
  renderAccountRows(values);
}

function removeAccountRow(idx) {
  const values = getAccountValues();
  if (values.length <= MIN_ACCOUNTS) return;
  values.splice(idx, 1);
  renderAccountRows(values);
}

async function openCell(fundName) {
  currentFund = fundName;
  document.getElementById("view-funds").classList.add("hidden");
  document.getElementById("view-form").classList.remove("hidden");
  document.getElementById("form-title").textContent = currentCompany + " · " + currentFund;
  document.getElementById("form-status").textContent = "טוען...";

  document.getElementById("f-url").value = "";
  document.getElementById("f-username").value = "";
  document.getElementById("f-password").value = "";
  document.getElementById("f-notes").value = "";
  document.getElementById("f-issue").checked = false;
  renderAccountRows(["", ""]);

  const res = await fetch(`/api/pension/cell?company=${encodeURIComponent(currentCompany)}&fund=${encodeURIComponent(currentFund)}`);
  const data = res.ok ? await res.json() : {};
  if (data && data.id) {
    document.getElementById("f-url").value = data.portal_url || "";
    document.getElementById("f-username").value = data.portal_username || "";
    document.getElementById("f-notes").value = data.notes || "";
    document.getElementById("f-issue").checked = !!data.has_issue;
    const accounts = (data.transfer_accounts && data.transfer_accounts.length) ? data.transfer_accounts.slice() : [];
    while (accounts.length < MIN_ACCOUNTS) accounts.push("");
    renderAccountRows(accounts);
    document.getElementById("form-status").textContent = "";
  } else {
    document.getElementById("form-status").textContent = "";
  }
}

async function saveCell() {
  const statusEl = document.getElementById("form-status");
  statusEl.textContent = "שומר...";
  const payload = {
    company: currentCompany,
    fund: currentFund,
    portal_url: document.getElementById("f-url").value,
    portal_username: document.getElementById("f-username").value,
    portal_password: document.getElementById("f-password").value,
    notes: document.getElementById("f-notes").value,
    has_issue: document.getElementById("f-issue").checked,
    transfer_accounts: getAccountValues(),
  };
  const res = await fetch("/api/pension/cell", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (res.ok) {
    statusEl.textContent = "נשמר.";
    setTimeout(() => showFunds(), 500);
  } else {
    statusEl.textContent = "שגיאה: " + (await res.text());
  }
}

loadCompanies();
</script>
</body>
</html>
