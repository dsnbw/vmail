(function () {
  "use strict";

  const PAGE_META = {
    overview: ["Tổng quan hệ thống", "Quản trị toàn diện · Gọn nhẹ · Miễn phí trọn đời"],
    domains: ["Tên miền & Tự động hóa DNS", "Kiểm tra MX/SPF/DKIM/DMARC & đồng bộ Cloudflare 1-click"],
    accounts: ["Hộp thư & Email Alias", "Tạo hộp thư không giới hạn, đặt dung lượng và chuyển tiếp"],
    relays: ["SMTP Relay & Kiểm tra gửi", "Chuyển tiếp Brevo/Resend/Gmail và gửi thử xác minh 250 OK"],
    apikeys: ["Transactional Mail API", "Gửi email giao dịch tốc độ cao qua HTTP REST API"],
    marketing: ["Danh bạ, Mẫu & Chiến dịch", "Email marketing có giới hạn nhịp gửi, chống spam & theo dõi mở"],
    spam: ["Chống Spam & Chạy kín", "Lọc thư rác, danh sách tin cậy/chặn và cô lập hoàn toàn"],
    logs: ["Nhật ký gửi nhận & Hàng đợi", "Theo dõi chi tiết delivery, inbound bridge và hàng đợi MTA"],
  };

  const state = {
    user: null,
    activeTab: "overview",
    domains: [],
    accounts: [],
    relays: { routes: [], default_route: "" },
    cfSettings: { email: "", key_masked: "", configured: false },
    marketingSub: "campaigns",
    selectedListId: "",
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  function fmtBytes(b) {
    const n = Number(b || 0);
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    return d.toLocaleString("vi-VN", { hour12: false });
  }

  function toast(msg, type = "ok") {
    const wrap = $("toast-wrap");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => {
      el.remove();
    }, 3600);
  }

  function openModal(title, htmlContent, onBind) {
    $("modal-title").textContent = title;
    $("modal-body").innerHTML = htmlContent;
    $("modal-backdrop").classList.remove("hidden");
    if (onBind) onBind($("modal-body"));
  }

  function closeModal() {
    $("modal-backdrop").classList.add("hidden");
    $("modal-body").innerHTML = "";
  }

  const _inflightGets = new Map();
  const _getCache = new Map();
  const _GET_TTL_MS = 4000;

  async function api(path, opts = {}) {
    const method = String(opts.method || "GET").toUpperCase();
    const cleanPath = path.startsWith("/") ? "." + path : "./" + path;
    if (method !== "GET") {
      _getCache.clear();
    } else if (!opts.noCache) {
      const hit = _getCache.get(cleanPath);
      if (hit && hit.exp > Date.now()) {
        return hit.data;
      }
      if (_inflightGets.has(cleanPath)) {
        return _inflightGets.get(cleanPath);
      }
    }

    const run = (async () => {
      const res = await fetch(cleanPath, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
        ...opts,
      });
      const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
      if (!res.ok || data.ok === false) {
        if (res.status === 401 && path !== "/api/me" && path !== "/api/login") {
          showLogin();
        }
        throw new Error(data.error || `Lỗi HTTP ${res.status}`);
      }
      if (method === "GET" && path !== "/api/me") {
        _getCache.set(cleanPath, { exp: Date.now() + _GET_TTL_MS, data });
      }
      return data;
    })();

    if (method === "GET") {
      _inflightGets.set(cleanPath, run);
      run.finally(() => _inflightGets.delete(cleanPath));
    }
    return run;
  }

  function showLogin() {
    $("app-shell").classList.add("hidden");
    $("login-screen").classList.remove("hidden");
  }

  function showApp() {
    $("login-screen").classList.add("hidden");
    $("app-shell").classList.remove("hidden");
    $("top-user").textContent = state.user?.email || state.user?.name || "admin";
    switchTab(state.activeTab || "overview");
  }

  function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll(".nav-item").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tab);
    });
    document.querySelectorAll(".tab-view").forEach((sec) => {
      sec.classList.toggle("hidden", sec.id !== `tab-${tab}`);
    });
    const meta = PAGE_META[tab] || ["Vmail", ""];
    $("page-title").textContent = meta[0];
    $("page-sub").textContent = meta[1];
    loadTab(tab);
  }

  async function loadTab(tab) {
    try {
      if (tab === "overview") await renderOverview();
      else if (tab === "domains") await renderDomains();
      else if (tab === "accounts") await renderAccounts();
      else if (tab === "relays") await renderRelays();
      else if (tab === "apikeys") await renderApiKeys();
      else if (tab === "marketing") await renderMarketing();
      else if (tab === "spam") await renderSpam();
      else if (tab === "logs") await renderLogs();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  // ---------------- TAB 1: OVERVIEW ----------------
  function buildDailyChart(daily) {
    if (!daily || !daily.length) return '<div class="muted">Chưa có dữ liệu 7 ngày gần nhất.</div>';
    const maxVal = Math.max(4, ...daily.map((d) => Math.max(d.delivered || 0, d.inbound || 0, d.failed || 0)));
    const w = 660;
    const h = 175;
    const padL = 32;
    const padB = 26;
    const usableH = h - padB - 14;
    const colW = (w - padL - 12) / daily.length;
    let bars = "";
    daily.forEach((d, i) => {
      const x0 = padL + i * colW + 8;
      const bw = Math.max(10, Math.min(20, (colW - 18) / 2));
      const hd = Math.round(((d.delivered || 0) / maxVal) * usableH);
      const hi = Math.round(((d.inbound || 0) / maxVal) * usableH);
      const hf = Math.round(((d.failed || 0) / maxVal) * usableH);
      const yBase = h - padB;
      bars += `<rect x="${x0}" y="${yBase - hd}" width="${bw}" height="${hd}" rx="3" fill="#1a73e8"><title>${d.date}: Gửi ${d.delivered}</title></rect>`;
      bars += `<rect x="${x0 + bw + 3}" y="${yBase - hi}" width="${bw}" height="${hi}" rx="3" fill="#137333"><title>${d.date}: Nhận ${d.inbound}</title></rect>`;
      if (hf > 0) {
        bars += `<rect x="${x0 + bw * 2 + 6}" y="${yBase - hf}" width="6" height="${hf}" rx="2" fill="#c5221f"><title>${d.date}: Lỗi ${d.failed}</title></rect>`;
      }
      bars += `<text x="${x0 + bw}" y="${h - 7}" text-anchor="middle" font-size="11" fill="#64748b">${esc((d.date || "").slice(5))}</text>`;
    });
    return `
      <svg class="chart-box" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        <line x1="${padL}" y1="${h - padB}" x2="${w - 8}" y2="${h - padB}" stroke="#cbd5e1" stroke-width="1" />
        ${bars}
      </svg>
      <div class="row" style="margin-top:6px;font-size:12px;color:#64748b;gap:16px">
        <span style="flex:0">🔵 Gửi thành công (250 OK)</span>
        <span style="flex:0">🟢 Thư đến Inbox</span>
        <span style="flex:0">🔴 Thất bại / Bounce</span>
      </div>`;
  }

  async function renderOverview() {
    const el = $("tab-overview");
    el.innerHTML = '<div class="card">Đang tải dữ liệu tổng quan...</div>';
    const data = await api("/api/overview");
    const s = data.stats;
    const h = data.health;
    $("top-route-badge").textContent = `Route: ${s.default_route || "MX trực tiếp"}`;

    const portBadges = Object.entries(h.ports || {})
      .map(([k, ok]) => `<span class="badge ${ok ? "badge-ok" : "badge-err"}">${esc(k)}: ${ok ? "OK" : "OFF"}</span>`)
      .join(" ");

    const procRows = (h.processes || [])
      .map(
        (p) =>
          `<tr><td><strong>${esc(p.name)}</strong></td><td>${p.running ? '<span class="badge badge-ok">Đang chạy</span>' : '<span class="badge badge-err">Ngừng</span>'}</td><td>${p.rss_mb} MB</td></tr>`
      )
      .join("");

    const eventRows = (data.recent_events || [])
      .map(
        (e) =>
          `<tr>
            <td class="muted">${esc(e.time || "")}</td>
            <td><span class="badge ${e.status === "delivered" || e.status === "inbound" ? "badge-ok" : e.status === "spam" ? "badge-warn" : "badge-err"}">${esc(e.type)}</span></td>
            <td>${esc(e.from)}</td>
            <td>${esc(e.to)}</td>
            <td class="muted">${esc(e.detail)}</td>
          </tr>`
      )
      .join("");

    el.innerHTML = `
      <div class="grid grid-4" style="margin-bottom:16px">
        <div class="card">
          <div class="stat-label">Thư gửi thành công</div>
          <div class="stat-val" style="color:var(--primary)">${s.totals.delivered}</div>
          <div class="stat-sub">Hôm nay: ${s.today.delivered} · Hàng đợi: ${s.queue}</div>
        </div>
        <div class="card">
          <div class="stat-label">Thư đến (Inbound)</div>
          <div class="stat-val" style="color:var(--ok)">${s.totals.inbound}</div>
          <div class="stat-sub">Hôm nay: ${s.today.inbound} · Bridge Cloudflare OK</div>
        </div>
        <div class="card">
          <div class="stat-label">Tài nguyên &amp; Hạ tầng</div>
          <div class="stat-val">${h.ram_used_mb} <span style="font-size:14px;font-weight:500">/ ${h.ram_total_mb} MB</span></div>
          <div class="stat-sub">Ổ cứng trống ${h.disk_free_gb} GB · Load ${h.load_1m}</div>
        </div>
        <div class="card">
          <div class="stat-label">Tài sản Mail Server</div>
          <div class="stat-val">${s.domains} <span style="font-size:14px;font-weight:500">tên miền</span></div>
          <div class="stat-sub">${s.accounts} hộp thư · ${s.api_keys} API keys · ${s.campaigns} chiến dịch</div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-bottom:16px">
        <div class="card">
          <div class="card-head">
            <h3 class="card-title">Lưu lượng Mail 7 ngày gần nhất</h3>
            <button class="btn btn-outline btn-sm" id="ov-refresh-btn">Làm mới</button>
          </div>
          ${buildDailyChart(s.daily)}
        </div>

        <div class="card">
          <div class="card-head">
            <h3 class="card-title">Sức khỏe tiến trình &amp; Cổng kết nối</h3>
            <span class="badge badge-info">Chạy kín · Độc lập hoàn toàn</span>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">${portBadges}</div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Tiến trình</th><th>Trạng thái</th><th>RAM</th></tr></thead>
              <tbody>${procRows}</tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">Sự kiện gửi/nhận mới nhất</h3>
          <button class="btn btn-outline btn-sm" id="ov-goto-logs">Xem toàn bộ nhật ký →</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Thời gian</th><th>Loại</th><th>Từ</th><th>Đến</th><th>Chi tiết</th></tr></thead>
            <tbody>${eventRows || '<tr><td colspan="5" class="muted">Chưa có sự kiện nào trong nhật ký.</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    `;
    $("ov-refresh-btn").addEventListener("click", renderOverview);
    $("ov-goto-logs").addEventListener("click", () => switchTab("logs"));
  }

  // ---------------- TAB 2: DOMAINS & DNS ----------------
  async function renderDomains() {
    const el = $("tab-domains");
    el.innerHTML = '<div class="card">Đang tải danh sách tên miền...</div>';
    const [dRes, cfRes] = await Promise.all([api("/api/domains"), api("/api/settings/cf")]);
    state.domains = dRes.domains || [];
    state.cfSettings = cfRes;

    const rows = state.domains
      .map(
        (d) => `
      <tr>
        <td>
          <strong>${esc(d.name)}</strong>
          <div class="muted" style="font-size:12px;margin-top:2px">
            Webmail: <a href="https://mail.${esc(d.name)}/" target="_blank" rel="noopener" style="color:var(--primary);text-decoration:none">https://mail.${esc(d.name)} ↗</a>
          </div>
        </td>
        <td>${d.enable ? '<span class="badge badge-ok">Hoạt động</span>' : '<span class="badge badge-err">Tắt</span>'}</td>
        <td><code>${esc(d.catchAllAddress || "Không bật")}</code></td>
        <td class="muted">${fmtTime(d.createdAt)}</td>
        <td>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="btn btn-outline btn-sm act-check-dns" data-name="${esc(d.name)}">🔍 Check DNS</button>
            <button class="btn btn-primary btn-sm act-fix-dns" data-name="${esc(d.name)}">⚡ Fix CF DNS</button>
            <button class="btn btn-outline btn-sm act-catchall" data-id="${esc(d.id)}" data-name="${esc(d.name)}" data-addr="${esc(d.catchAllAddress || "")}">Catch-All</button>
            <button class="btn btn-outline btn-sm act-zone" data-name="${esc(d.name)}">DKIM/Zone</button>
            <button class="btn btn-danger btn-sm act-del-dom" data-id="${esc(d.id)}" data-name="${esc(d.name)}">Xóa</button>
          </div>
        </td>
      </tr>`
      )
      .join("");

    el.innerHTML = `
      <div class="card" style="margin-bottom:16px">
        <div class="card-head">
          <div>
            <h3 class="card-title">Quản lý Tên miền &amp; Tự động hóa Cloudflare</h3>
            <div class="muted">Cloudflare API: ${cfRes.configured ? `<strong>${esc(cfRes.email)}</strong> (${esc(cfRes.key_masked)})` : "Chưa cấu hình"}</div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn btn-outline" id="btn-cf-cfg">⚙ Cấu hình Cloudflare</button>
            <button class="btn btn-ok" id="btn-sync-cf">🔄 Đồng bộ toàn bộ Cloudflare</button>
            <button class="btn btn-primary" id="btn-add-domain">+ Thêm tên miền</button>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Tên miền</th><th>Trạng thái</th><th>Catch-All</th><th>Ngày tạo</th><th>Thao tác</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="5" class="muted">Chưa có tên miền nào.</td></tr>'}</tbody>
          </table>
        </div>
      </div>
      <div id="dns-result-box"></div>
    `;

    $("btn-cf-cfg").addEventListener("click", () => {
      openModal(
        "Cấu hình Cloudflare Global API",
        `<form id="cf-form">
          <label>Cloudflare Account Email</label>
          <input id="cf-email" type="email" value="${esc(state.cfSettings.email || "")}" required />
          <label>Global API Key</label>
          <input id="cf-key" type="password" placeholder="Nhập Global API Key mới" required />
          <button type="submit" class="btn btn-primary btn-block">Lưu thông tin Cloudflare</button>
        </form>`,
        (box) => {
          box.querySelector("#cf-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            try {
              await api("/api/settings/cf", {
                method: "POST",
                body: JSON.stringify({ email: box.querySelector("#cf-email").value.trim(), api_key: box.querySelector("#cf-key").value.trim() }),
              });
              toast("Đã lưu cấu hình Cloudflare!");
              closeModal();
              renderDomains();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    });

    $("btn-sync-cf").addEventListener("click", async () => {
      try {
        toast("Đang đồng bộ toàn bộ zone từ Cloudflare...", "ok");
        const r = await api("/api/domains/sync-cf", { method: "POST", body: "{}" });
        const s = r.summary;
        toast(`Đồng bộ xong: ${s.total_cf_zones} zones CF, thêm mới ${s.added_domains.length} domain!`);
        renderDomains();
      } catch (err) {
        toast(err.message, "err");
      }
    });

    $("btn-add-domain").addEventListener("click", () => {
      openModal(
        "Thêm tên miền Mail mới",
        `<form id="add-dom-form">
          <label>Tên miền (ví dụ: congty.vn)</label>
          <input id="new-dom-name" type="text" placeholder="example.com" required />
          <button type="submit" class="btn btn-primary btn-block">Tạo tên miền &amp; Sinh khóa DKIM</button>
        </form>`,
        (box) => {
          box.querySelector("#add-dom-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            try {
              await api("/api/domains", { method: "POST", body: JSON.stringify({ name: box.querySelector("#new-dom-name").value.trim() }) });
              toast("Đã thêm tên miền thành công!");
              closeModal();
              renderDomains();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    });

    el.querySelectorAll(".act-check-dns").forEach((btn) =>
      btn.addEventListener("click", async () => {
        const dname = btn.dataset.name;
        const box = $("dns-result-box");
        box.innerHTML = `<div class="card">Đang truy vấn DNS trực tiếp cho <strong>${esc(dname)}</strong>...</div>`;
        try {
          const r = await api(`/api/domains/${encodeURIComponent(dname)}/check-dns`, { method: "POST", body: "{}" });
          const rowsDns = (r.dns.records || [])
            .map(
              (rec) => `<tr>
                <td><span class="badge badge-info">${esc(rec.type)}</span></td>
                <td><code>${esc(rec.host)}</code></td>
                <td>${esc(rec.label)}</td>
                <td>${rec.ok ? '<span class="badge badge-ok">Đạt</span>' : '<span class="badge badge-err">Thiếu / Lệch</span>'}</td>
                <td class="muted" style="max-width:340px;word-break:break-all">${esc((rec.actual || []).join(" | ") || rec.expected)}</td>
              </tr>`
            )
            .join("");
          box.innerHTML = `<div class="card">
            <div class="card-head">
              <h3 class="card-title">Kết quả DNS thực tế: ${esc(dname)}</h3>
              <span class="badge ${r.dns.score === r.dns.max_score ? "badge-ok" : "badge-warn"}">Điểm chuẩn gửi Inbox: ${r.dns.score}/${r.dns.max_score}</span>
            </div>
            <div class="table-wrap"><table>
              <thead><tr><th>Loại</th><th>Host</th><th>Mục đích</th><th>Trạng thái</th><th>Giá trị thực tế / Gợi ý</th></tr></thead>
              <tbody>${rowsDns}</tbody>
            </table></div>
          </div>`;
        } catch (err) {
          box.innerHTML = "";
          toast(err.message, "err");
        }
      })
    );

    el.querySelectorAll(".act-fix-dns").forEach((btn) =>
      btn.addEventListener("click", async () => {
        const dname = btn.dataset.name;
        if (!confirm(`Tự động cập nhật Webmail (mail.${dname}), MX, SPF, DKIM, DMARC & Email Routing Cloudflare cho ${dname}?`)) return;
        try {
          toast(`Đang cấu hình DNS Cloudflare cho ${dname}...`);
          const r = await api(`/api/domains/${encodeURIComponent(dname)}/fix-dns`, { method: "POST", body: "{}" });
          toast(`Đã cập nhật DNS + Catch-All Worker (${r.result.email_routing}) cho ${dname}!`);
          el.querySelector(`.act-check-dns[data-name="${CSS.escape(dname)}"]`)?.click();
        } catch (err) {
          toast(err.message, "err");
        }
      })
    );

    el.querySelectorAll(".act-catchall").forEach((btn) =>
      btn.addEventListener("click", () => {
        openModal(
          `Catch-All cho ${btn.dataset.name}`,
          `<form id="ca-form">
            <label>Địa chỉ nhận toàn bộ thư gửi nhầm địa chỉ (để trống để tắt)</label>
            <input id="ca-addr" type="email" value="${esc(btn.dataset.addr)}" placeholder="admin@${esc(btn.dataset.name)}" />
            <button type="submit" class="btn btn-primary btn-block">Lưu Catch-All</button>
          </form>`,
          (box) => {
            box.querySelector("#ca-form").addEventListener("submit", async (e) => {
              e.preventDefault();
              try {
                await api(`/api/domains/${encodeURIComponent(btn.dataset.id)}/catchall`, {
                  method: "POST",
                  body: JSON.stringify({ catchAllAddress: box.querySelector("#ca-addr").value.trim() }),
                });
                toast("Đã cập nhật Catch-All!");
                closeModal();
                renderDomains();
              } catch (err) {
                toast(err.message, "err");
              }
            });
          }
        );
      })
    );

    el.querySelectorAll(".act-zone").forEach((btn) =>
      btn.addEventListener("click", () => {
        const dom = state.domains.find((x) => x.name === btn.dataset.name);
        openModal(
          `DNS Zone & Khóa DKIM: ${btn.dataset.name}`,
          `<p class="muted">Sao chép các bản ghi dưới đây nếu tên miền đặt ở nhà cung cấp DNS khác ngoài Cloudflare:</p>
           <pre>${esc(dom?.dnsZoneFile || "Chưa có dữ liệu Zone.")}</pre>`
        );
      })
    );

    el.querySelectorAll(".act-del-dom").forEach((btn) =>
      btn.addEventListener("click", async () => {
        if (!confirm(`Xóa tên miền ${btn.dataset.name} khỏi hệ thống mail?`)) return;
        try {
          await api(`/api/domains/${encodeURIComponent(btn.dataset.id)}`, { method: "DELETE" });
          toast("Đã xóa tên miền.");
          renderDomains();
        } catch (err) {
          toast(err.message, "err");
        }
      })
    );
  }

  // ---------------- TAB 3: ACCOUNTS & ALIASES ----------------
  async function renderAccounts() {
    const el = $("tab-accounts");
    el.innerHTML = '<div class="card">Đang tải danh sách hộp thư...</div>';
    const [aRes, dRes] = await Promise.all([api("/api/accounts"), api("/api/domains")]);
    state.accounts = aRes.accounts || [];
    state.domains = dRes.domains || [];

    const drawTable = (kw = "") => {
      const q = kw.trim().toLowerCase();
      const filtered = state.accounts.filter(
        (a) => !q || (a.email || "").toLowerCase().includes(q) || (a.description || "").toLowerCase().includes(q)
      );
      const tbody = filtered
        .map((a) => {
          const aliases = (a.aliases || []).map((al) => `<span class="badge badge-info">${esc(al)}</span>`).join(" ");
          const qTxt = a.quotaMb ? `${fmtBytes(a.usedBytes)} / ${a.quotaMb} MB` : `${fmtBytes(a.usedBytes)} / Không giới hạn`;
          return `<tr>
            <td><strong>${esc(a.email)}</strong><div class="muted">${esc(a.description || "")}</div></td>
            <td><span class="badge ${a.role === "Admin" ? "badge-warn" : "badge-ok"}">${esc(a.role)}</span></td>
            <td>${aliases || '<span class="muted">-</span>'}</td>
            <td>${esc(qTxt)}</td>
            <td>
              <div style="display:flex;gap:6px">
                <button class="btn btn-outline btn-sm act-edit-acc" data-id="${esc(a.id)}">Sửa</button>
                <button class="btn btn-danger btn-sm act-del-acc" data-id="${esc(a.id)}" data-email="${esc(a.email)}">Xóa</button>
              </div>
            </td>
          </tr>`;
        })
        .join("");
      $("acc-tbody").innerHTML = tbody || '<tr><td colspan="5" class="muted">Không có hộp thư phù hợp.</td></tr>';

      el.querySelectorAll(".act-edit-acc").forEach((btn) =>
        btn.addEventListener("click", () => {
          const acc = state.accounts.find((x) => x.id === btn.dataset.id);
          if (!acc) return;
          openModal(
            `Cập nhật hộp thư: ${acc.email}`,
            `<form id="edit-acc-form">
              <label>Tên hiển thị / Mô tả</label>
              <input id="ea-desc" type="text" value="${esc(acc.description)}" />
              <div class="row">
                <div>
                  <label>Vai trò</label>
                  <select id="ea-role">
                    <option value="User" ${acc.role === "User" ? "selected" : ""}>User</option>
                    <option value="Admin" ${acc.role === "Admin" ? "selected" : ""}>Admin</option>
                  </select>
                </div>
                <div>
                  <label>Giới hạn dung lượng (MB, 0 = không giới hạn)</label>
                  <input id="ea-quota" type="number" min="0" value="${acc.quotaMb || 0}" />
                </div>
              </div>
              <label>Email Alias phụ (phân cách bằng dấu phẩy, ví dụ: info, support)</label>
              <input id="ea-aliases" type="text" value="${esc((acc.aliases || []).join(", "))}" />
              <label>Mật khẩu mới (để trống nếu giữ nguyên)</label>
              <input id="ea-pass" type="password" placeholder="Tối thiểu 6 ký tự" />
              <button type="submit" class="btn btn-primary btn-block">Lưu thay đổi</button>
            </form>`,
            (box) => {
              box.querySelector("#edit-acc-form").addEventListener("submit", async (e) => {
                e.preventDefault();
                const aliases = box
                  .querySelector("#ea-aliases")
                  .value.split(",")
                  .map((s) => s.trim())
                  .filter(Boolean);
                const payload = {
                  description: box.querySelector("#ea-desc").value.trim(),
                  role: box.querySelector("#ea-role").value,
                  quotaMb: Number(box.querySelector("#ea-quota").value || 0),
                  aliases,
                };
                const np = box.querySelector("#ea-pass").value;
                if (np) payload.password = np;
                try {
                  await api(`/api/accounts/${encodeURIComponent(acc.id)}`, { method: "PATCH", body: JSON.stringify(payload) });
                  toast("Đã cập nhật hộp thư!");
                  closeModal();
                  renderAccounts();
                } catch (err) {
                  toast(err.message, "err");
                }
              });
            }
          );
        })
      );

      el.querySelectorAll(".act-del-acc").forEach((btn) =>
        btn.addEventListener("click", async () => {
          if (!confirm(`Xóa hộp thư ${btn.dataset.email}?`)) return;
          try {
            await api(`/api/accounts/${encodeURIComponent(btn.dataset.id)}`, { method: "DELETE" });
            toast("Đã xóa hộp thư.");
            renderAccounts();
          } catch (err) {
            toast(err.message, "err");
          }
        })
      );
    };

    el.innerHTML = `
      <div class="card">
        <div class="card-head">
          <div style="display:flex;gap:10px;align-items:center;flex:1;max-width:420px">
            <input id="acc-search" type="search" placeholder="Tìm theo email hoặc tên..." />
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn btn-outline" id="btn-imap-guide">📱 Cấu hình IMAP / SMTP</button>
            <button class="btn btn-primary" id="btn-add-acc">+ Tạo hộp thư mới</button>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Địa chỉ Email</th><th>Quyền</th><th>Email Alias</th><th>Dung lượng</th><th>Thao tác</th></tr></thead>
            <tbody id="acc-tbody"></tbody>
          </table>
        </div>
      </div>
    `;

    drawTable();
    $("acc-search").addEventListener("input", (e) => drawTable(e.target.value));

    $("btn-imap-guide").addEventListener("click", () => {
      const mHost = window.location.hostname || "mail.example.com";
      const mDom = mHost.replace(/^mail\./i, "");
      openModal(
        "Thông số kết nối Mail Client (Outlook / Thunderbird / Apple Mail)",
        `<p>Hệ thống hỗ trợ chuẩn IMAP/SMTP SSL trực tiếp:</p>
         <pre>Máy chủ nhận thư (IMAP): ${esc(mHost)} · Cổng 993 · Bảo mật SSL/TLS
Máy chủ gửi thư (SMTP):  ${esc(mHost)} · Cổng 465 · Bảo mật SSL/TLS (hoặc 587 STARTTLS)
Tên đăng nhập:           Địa chỉ email đầy đủ (ví dụ admin@${esc(mDom)})</pre>`
      );
    });

    $("btn-add-acc").addEventListener("click", () => {
      const domOpts = state.domains.map((d) => `<option value="${esc(d.name)}">${esc(d.name)}</option>`).join("");
      openModal(
        "Tạo hộp thư Email mới",
        `<form id="add-acc-form">
          <div class="row">
            <div><label>Tên tài khoản</label><input id="na-local" type="text" placeholder="support" required /></div>
            <div><label>Tên miền</label><select id="na-dom">${domOpts}</select></div>
          </div>
          <label>Tên hiển thị</label>
          <input id="na-desc" type="text" placeholder="Bộ phận CSKH" />
          <label>Mật khẩu <button type="button" class="btn btn-outline btn-sm" id="na-gen-pw" style="margin-left:8px">Sinh ngẫu nhiên</button></label>
          <input id="na-pass" type="text" placeholder="Tối thiểu 6 ký tự" required />
          <div class="row">
            <div><label>Vai trò</label><select id="na-role"><option value="User">User</option><option value="Admin">Admin</option></select></div>
            <div><label>Quota (MB, 0 = Không giới hạn)</label><input id="na-quota" type="number" min="0" value="0" /></div>
          </div>
          <label>Alias (tùy chọn, cách nhau dấu phẩy)</label>
          <input id="na-aliases" type="text" placeholder="cskh, hello" />
          <button type="submit" class="btn btn-primary btn-block">Tạo hộp thư ngay</button>
        </form>`,
        (box) => {
          box.querySelector("#na-gen-pw").addEventListener("click", () => {
            const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789@#!";
            let pw = "";
            for (let i = 0; i < 14; i++) pw += chars[Math.floor(Math.random() * chars.length)];
            box.querySelector("#na-pass").value = pw;
          });
          box.querySelector("#add-acc-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            const email = `${box.querySelector("#na-local").value.trim().toLowerCase()}@${box.querySelector("#na-dom").value}`;
            const aliases = box
              .querySelector("#na-aliases")
              .value.split(",")
              .map((s) => s.trim())
              .filter(Boolean);
            try {
              await api("/api/accounts", {
                method: "POST",
                body: JSON.stringify({
                  email,
                  password: box.querySelector("#na-pass").value,
                  description: box.querySelector("#na-desc").value.trim(),
                  role: box.querySelector("#na-role").value,
                  quotaMb: Number(box.querySelector("#na-quota").value || 0),
                  aliases,
                }),
              });
              toast(`Đã tạo hộp thư ${email}!`);
              closeModal();
              renderAccounts();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    });
  }

  // ---------------- TAB 4: SMTP RELAY & TEST SEND ----------------
  async function renderRelays() {
    const el = $("tab-relays");
    el.innerHTML = '<div class="card">Đang tải cấu hình SMTP Relay...</div>';
    const [rRes, aRes] = await Promise.all([api("/api/relays"), api("/api/accounts")]);
    state.relays = rRes;
    state.accounts = aRes.accounts || [];
    $("top-route-badge").textContent = `Route: ${rRes.default_route || "MX trực tiếp"}`;

    const presets = {
      brevo: { name: "brevo-relay", host: "smtp-relay.brevo.com", port: 587, implicitTls: false, desc: "Brevo SMTP Relay" },
      resend: { name: "resend-relay", host: "smtp.resend.com", port: 465, implicitTls: true, desc: "Resend SMTP Relay" },
      sendgrid: { name: "sendgrid-relay", host: "smtp.sendgrid.net", port: 587, implicitTls: false, desc: "SendGrid SMTP" },
      gmail: { name: "gmail-relay", host: "smtp.gmail.com", port: 587, implicitTls: false, desc: "Gmail App Password Relay" },
    };

    const routeRows = (rRes.routes || [])
      .filter((r) => r["@type"] !== "Local")
      .map((r) => {
        const rname = r.name || r.id;
        const isDef = rname === rRes.default_route;
        const isRelay = r["@type"] === "Relay";
        return `<tr>
          <td><strong>${esc(rname)}</strong> ${isDef ? '<span class="badge badge-ok">Mặc định</span>' : ""}</td>
          <td><code>${esc(r.host ? `${r.host}:${r.port || 587}` : "MX Direct")}</code></td>
          <td><code>${esc(r.authUsername || "-")}</code></td>
          <td>${isRelay ? (r.implicitTls ? "Implicit TLS" : "STARTTLS") : "Direct"}</td>
          <td>
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              ${!isDef ? `<button class="btn btn-primary btn-sm act-set-def" data-id="${esc(rname)}">Đặt mặc định</button>` : ""}
              ${isRelay ? `<button class="btn btn-outline btn-sm act-test-conn" data-host="${esc(r.host || "")}" data-port="${esc(r.port || 587)}" data-tls="${r.implicitTls ? "1" : "0"}">🔌 Test TCP/TLS</button>` : ""}
              ${isRelay ? `<button class="btn btn-outline btn-sm act-edit-route" data-id="${esc(rname)}">Cấu hình</button>` : ""}
            </div>
          </td>
        </tr>`;
      })
      .join("");

    const fromOpts = state.accounts.map((a) => `<option value="${esc(a.email)}">${esc(a.email)}</option>`).join("");

    el.innerHTML = `
      <div class="grid grid-2">
        <div class="card">
          <div class="card-head">
            <h3 class="card-title">Danh sách tuyến gửi Outbound (Relay)</h3>
            <button class="btn btn-primary btn-sm" id="btn-new-relay">+ Thêm / Sửa Relay</button>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
            <span class="muted" style="line-height:28px">Mẫu nhanh:</span>
            <button class="btn btn-outline btn-sm preset-btn" data-p="brevo">Brevo (587)</button>
            <button class="btn btn-outline btn-sm preset-btn" data-p="resend">Resend (465)</button>
            <button class="btn btn-outline btn-sm preset-btn" data-p="sendgrid">SendGrid (587)</button>
            <button class="btn btn-outline btn-sm preset-btn" data-p="gmail">Gmail (587)</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Route</th><th>Máy chủ</th><th>Tài khoản</th><th>TLS</th><th>Thao tác</th></tr></thead>
              <tbody>${routeRows || '<tr><td colspan="5" class="muted">Chưa có route relay nào.</td></tr>'}</tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <h3 class="card-title">Gửi thư kiểm tra &amp; Xác minh đường truyền SMTP</h3>
          </div>
          <div class="muted" style="margin-bottom:8px">Lưu ý: Với IP mạng nhà (Viettel/FPT/VNPT) không có PTR riêng, Google/Gmail từ chối gửi thẳng (550 5.7.1) nên phải gửi qua SMTP Relay (Brevo, Resend hoặc Gmail SMTP). Nếu gửi qua Brevo báo 250 OK nhưng hộp thư đích chưa thấy, hãy kiểm tra mục Spam và vào Brevo bấm <strong>Verify &amp; Authenticate domain</strong> + kiểm tra <strong>Transactional Logs</strong> xem tài khoản Brevo đã kích hoạt SMTP hay chưa.</div>
          <form id="test-send-form">
            <label>Gửi từ hộp thư</label>
            <select id="ts-from">${fromOpts}</select>
            <label>Đến địa chỉ Email</label>
            <input id="ts-to" type="email" placeholder="nguoinhan@gmail.com" required />
            <label>Tiêu đề</label>
            <input id="ts-sub" type="text" value="[Vmail] Kiểm tra kết nối gửi thư chuẩn Inbox" required />
            <label>Nội dung</label>
            <textarea id="ts-body">Đây là thư kiểm tra trực tiếp từ Vmail Server để xác minh cấu hình SPF, DKIM, DMARC và SMTP Relay.</textarea>
            <button type="submit" class="btn btn-primary btn-block" id="ts-btn">🚀 Gửi thử &amp; Xác minh ngay</button>
          </form>
          <div id="ts-result"></div>
        </div>
      </div>
    `;

    const openRelayModal = (preset = {}) => {
      openModal(
        "Cấu hình SMTP Relay Outbound",
        `<form id="relay-form">
          <label>Tên Route</label>
          <input id="rl-name" type="text" value="${esc(preset.name || preset.id || "brevo-relay")}" required />
          <div class="row">
            <div><label>SMTP Host</label><input id="rl-host" type="text" value="${esc(preset.host || "")}" required /></div>
            <div><label>Cổng (Port)</label><input id="rl-port" type="number" value="${preset.port || 587}" required /></div>
          </div>
          <label>Username / Login SMTP</label>
          <input id="rl-user" type="text" value="${esc(preset.authUsername || "")}" />
          <label>SMTP Key / Password (nhập lại nếu đổi)</label>
          <input id="rl-sec" type="password" placeholder="Giữ trống nếu không đổi" />
          <div class="row" style="margin-top:10px">
            <label style="display:flex;gap:8px;align-items:center"><input id="rl-tls" type="checkbox" style="width:auto" ${preset.implicitTls ? "checked" : ""}/> Implicit TLS (465)</label>
            <label style="display:flex;gap:8px;align-items:center"><input id="rl-def" type="checkbox" style="width:auto" checked/> Đặt làm route mặc định</label>
          </div>
          <button type="submit" class="btn btn-primary btn-block">Lưu &amp; Tải lại MTA ngay</button>
        </form>`,
        (box) => {
          box.querySelector("#relay-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            try {
              await api("/api/relays/upsert", {
                method: "POST",
                body: JSON.stringify({
                  name: box.querySelector("#rl-name").value.trim(),
                  host: box.querySelector("#rl-host").value.trim(),
                  port: Number(box.querySelector("#rl-port").value || 587),
                  authUsername: box.querySelector("#rl-user").value.trim(),
                  authSecret: box.querySelector("#rl-sec").value.trim(),
                  implicitTls: box.querySelector("#rl-tls").checked,
                  setDefault: box.querySelector("#rl-def").checked,
                }),
              });
              toast("Đã lưu SMTP Relay và Reload MTA!");
              closeModal();
              renderRelays();
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    };

    $("btn-new-relay").addEventListener("click", () => openRelayModal({}));
    el.querySelectorAll(".preset-btn").forEach((b) => b.addEventListener("click", () => openRelayModal(presets[b.dataset.p])));
    el.querySelectorAll(".act-edit-route").forEach((b) =>
      b.addEventListener("click", () => {
        const r = (state.relays.routes || []).find((x) => (x.name || x.id) === b.dataset.id) || {};
        openRelayModal(r);
      })
    );
    el.querySelectorAll(".act-test-conn").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          const r = await api("/api/relays/test-conn", {
            method: "POST",
            body: JSON.stringify({ host: b.dataset.host, port: Number(b.dataset.port || 587), implicitTls: b.dataset.tls === "1" }),
          });
          toast(`Kết nối TLS tới ${b.dataset.host}:${b.dataset.port} thành công (${r.conn.elapsed_ms}ms)!`);
        } catch (err) {
          toast(`Lỗi kết nối ${b.dataset.host}:${b.dataset.port}: ${err.message}`, "err");
        }
      })
    );
    el.querySelectorAll(".act-set-def").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api("/api/relays/default", { method: "POST", body: JSON.stringify({ route: b.dataset.id }) });
          toast(`Đã chọn ${b.dataset.id} làm route gửi mặc định!`);
          renderRelays();
        } catch (err) {
          toast(err.message, "err");
        }
      })
    );

    $("test-send-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("ts-btn");
      const out = $("ts-result");
      btn.disabled = true;
      btn.textContent = "Đang gửi và kiểm tra log MTA...";
      out.innerHTML = "";
      try {
        const r = await api("/api/test-send", {
          method: "POST",
          body: JSON.stringify({
            from: $("ts-from").value,
            to: $("ts-to").value.trim(),
            subject: $("ts-sub").value.trim(),
            body: $("ts-body").value,
          }),
        });
        const d = r.delivery;
        const isRelay = d.via_relay === true;
        const titleMsg = d.delivered
          ? isRelay
            ? `📤 Đã chuyển sang Relay ${esc(d.relay_host)} (250 OK: queued)`
            : "✅ Đã giao trực tiếp tới MX đích (250 OK)"
          : d.delivered === false
          ? "❌ Giao thư thất bại"
          : "⏳ Đã vào hàng đợi MTA";
        const extraNote =
          d.delivered && isRelay
            ? `<div style="margin-top:6px;font-size:12.5px;color:#374151">Relay đã tiếp nhận vào hàng đợi gửi. Chiều gửi tiếp theo tới Gmail phụ thuộc vào trạng thái kích hoạt SMTP &amp; xác thực domain trên tài khoản <strong>${esc(d.relay_host)}</strong>.</div>`
            : "";
        out.innerHTML = `<div class="${d.delivered === false ? "err-box" : "ok-box"}">
          <strong>${titleMsg}</strong><br/>
          Relay/MX: <code>${esc(d.relay_host || "-")}</code> · Code: <code>${esc(d.code || "-")}</code> · Queue: <code>${esc(d.queue_id || "-")}</code><br/>
          Phản hồi SMTP: <code>${esc(d.detail || "")}</code>
          ${extraNote}
        </div>`;
      } catch (err) {
        out.innerHTML = `<div class="err-box">${esc(err.message)}</div>`;
      } finally {
        btn.disabled = false;
        btn.textContent = "🚀 Gửi thử & Xác minh ngay";
      }
    });
  }

  // ---------------- TAB 5: TRANSACTIONAL API ----------------
  async function renderApiKeys() {
    const el = $("tab-apikeys");
    el.innerHTML = '<div class="card">Đang tải API Keys...</div>';
    const [kRes, aRes] = await Promise.all([api("/api/apikeys"), api("/api/accounts")]);
    state.accounts = aRes.accounts || [];

    const rows = (kRes.keys || [])
      .map(
        (k) => `<tr>
          <td><strong>${esc(k.name)}</strong></td>
          <td><code>${esc(k.key_prefix)}</code></td>
          <td><code>${esc(k.default_from)}</code></td>
          <td>${k.sent_today} / ${k.daily_limit || "∞"}</td>
          <td class="muted">${esc(k.allowed_ips || "Mọi IP")}</td>
          <td><button class="btn btn-danger btn-sm act-del-key" data-id="${esc(k.id)}">Thu hồi</button></td>
        </tr>`
      )
      .join("");

    el.innerHTML = `
      <div class="card" style="margin-bottom:16px">
        <div class="card-head">
          <h3 class="card-title">Quản lý API Key (Transactional Email)</h3>
          <button class="btn btn-primary" id="btn-new-key">+ Tạo API Key mới</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Tên ứng dụng</th><th>Prefix</th><th>Email gửi mặc định</th><th>Đã gửi hôm nay</th><th>IP cho phép</th><th>Thao tác</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="6" class="muted">Chưa có API Key nào.</td></tr>'}</tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h3 class="card-title" style="margin-bottom:8px">Mẫu tích hợp nhanh (POST /vmail-api/v1/send)</h3>
        <pre>curl -X POST ${esc(window.location.origin)}/vmail-api/v1/send \\
  -H "Authorization: Bearer &lt;YOUR_VMAIL_API_KEY&gt;" \\
  -H "Content-Type: application/json" \\
  -d '{
    "to": "khachhang@gmail.com",
    "from": "${esc(state.accounts[0]?.email || ("admin@" + window.location.hostname.replace(/^mail\./i, "")))}",
    "from_name": "Vmail System",
    "subject": "Mã xác thực OTP của bạn",
    "html": "&lt;h2&gt;Mã OTP: 482910&lt;/h2&gt;&lt;p&gt;Có hiệu lực trong 5 phút.&lt;/p&gt;",
    "verify": true
  }'</pre>
      </div>
    `;

    $("btn-new-key").addEventListener("click", () => {
      const fromOpts = state.accounts.map((a) => `<option value="${esc(a.email)}">${esc(a.email)}</option>`).join("");
      openModal(
        "Tạo Transactional API Key mới",
        `<form id="ak-form">
          <label>Tên ứng dụng / Website</label>
          <input id="ak-name" type="text" placeholder="Website Laravel / App OTP" required />
          <label>Địa chỉ gửi mặc định (From)</label>
          <select id="ak-from">${fromOpts}</select>
          <div class="row">
            <div><label>Giới hạn gửi / ngày</label><input id="ak-limit" type="number" min="0" value="300" /></div>
            <div><label>Giới hạn IP (tùy chọn, cách nhau dấu phẩy)</label><input id="ak-ips" type="text" placeholder="Để trống = cho phép mọi IP" /></div>
          </div>
          <button type="submit" class="btn btn-primary btn-block">Sinh API Key</button>
        </form>`,
        (box) => {
          box.querySelector("#ak-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            try {
              const r = await api("/api/apikeys", {
                method: "POST",
                body: JSON.stringify({
                  name: box.querySelector("#ak-name").value.trim(),
                  default_from: box.querySelector("#ak-from").value,
                  daily_limit: Number(box.querySelector("#ak-limit").value || 300),
                  allowed_ips: box.querySelector("#ak-ips").value.trim(),
                }),
              });
              openModal(
                "Sao chép API Key của bạn",
                `<div class="ok-box">Hãy lưu lại API Key dưới đây ngay vì lý do bảo mật chỉ hiển thị 1 lần duy nhất:</div>
                 <pre>${esc(r.raw_key)}</pre>
                 <button class="btn btn-primary btn-block" id="ak-done">Đã lưu lại</button>`,
                (b2) => b2.querySelector("#ak-done").addEventListener("click", () => { closeModal(); renderApiKeys(); })
              );
            } catch (err) {
              toast(err.message, "err");
            }
          });
        }
      );
    });

    el.querySelectorAll(".act-del-key").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Thu hồi API Key này? Ứng dụng đang dùng key sẽ ngừng gửi được thư.")) return;
        try {
          await api(`/api/apikeys/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" });
          toast("Đã thu hồi API Key.");
          renderApiKeys();
        } catch (err) {
          toast(err.message, "err");
        }
      })
    );
  }

  // ---------------- TAB 6: MARKETING (LISTS, TEMPLATES, CAMPAIGNS) ----------------
  async function renderMarketing() {
    const el = $("tab-marketing");
    el.innerHTML = `
      <div class="subtabs">
        <button class="subtab ${state.marketingSub === "campaigns" ? "active" : ""}" data-sub="campaigns">📣 Chiến dịch gửi mail</button>
        <button class="subtab ${state.marketingSub === "contacts" ? "active" : ""}" data-sub="contacts">👥 Nhóm &amp; Danh bạ</button>
        <button class="subtab ${state.marketingSub === "templates" ? "active" : ""}" data-sub="templates">🎨 Mẫu Email HTML</button>
      </div>
      <div id="mkt-body"><div class="card">Đang tải...</div></div>
    `;
    el.querySelectorAll(".subtab").forEach((b) =>
      b.addEventListener("click", () => {
        state.marketingSub = b.dataset.sub;
        renderMarketing();
      })
    );

    const body = $("mkt-body");
    if (state.marketingSub === "campaigns") {
      const [cRes, lRes, tRes, aRes] = await Promise.all([api("/api/campaigns"), api("/api/lists"), api("/api/templates"), api("/api/accounts")]);
      const lists = lRes.lists || [];
      const tpls = tRes.templates || [];
      const accs = aRes.accounts || [];
      const rows = (cRes.campaigns || [])
        .map((c) => {
          const badge =
            c.status === "completed"
              ? "badge-ok"
              : c.status === "running"
              ? "badge-info"
              : c.status === "failed"
              ? "badge-err"
              : "badge-warn";
          return `<tr>
            <td><strong>${esc(c.name)}</strong><div class="muted">${esc(c.subject)}</div></td>
            <td><span class="badge ${badge}">${esc(c.status)}</span></td>
            <td>${c.sent_count}/${c.total_count} (Lỗi: ${c.failed_count})</td>
            <td>Mở: ${c.open_count} · Hủy: ${c.unsub_count}</td>
            <td class="muted">${c.delay_ms}ms / thư · Max ${c.daily_limit}/ngày</td>
            <td>
              <div style="display:flex;gap:6px">
                ${c.status !== "running" && c.status !== "completed" ? `<button class="btn btn-ok btn-sm act-cmp" data-act="start" data-id="${esc(c.id)}">▶ Chạy</button>` : ""}
                ${c.status === "running" ? `<button class="btn btn-outline btn-sm act-cmp" data-act="pause" data-id="${esc(c.id)}">⏸ Tạm dừng</button>` : ""}
                <button class="btn btn-danger btn-sm act-cmp" data-act="delete" data-id="${esc(c.id)}">Xóa</button>
              </div>
              ${c.last_error ? `<div class="muted" style="color:var(--err);font-size:11.5px;margin-top:4px">${esc(c.last_error)}</div>` : ""}
            </td>
          </tr>`;
        })
        .join("");

      body.innerHTML = `<div class="card">
        <div class="card-head">
          <h3 class="card-title">Chiến dịch Email Marketing (Tự động giãn nhịp bảo vệ IP/Uy tín)</h3>
          <button class="btn btn-primary" id="btn-new-cmp">+ Tạo chiến dịch mới</button>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Chiến dịch</th><th>Trạng thái</th><th>Tiến độ</th><th>Tương tác</th><th>Nhịp gửi (Warmup)</th><th>Thao tác</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="6" class="muted">Chưa có chiến dịch nào.</td></tr>'}</tbody>
        </table></div>
      </div>`;

      body.querySelectorAll(".act-cmp").forEach((b) =>
        b.addEventListener("click", async () => {
          if (b.dataset.act === "delete" && !confirm("Xóa chiến dịch này?")) return;
          try {
            await api(`/api/campaigns/${encodeURIComponent(b.dataset.id)}/action`, { method: "POST", body: JSON.stringify({ action: b.dataset.act }) });
            toast("Đã cập nhật chiến dịch!");
            renderMarketing();
          } catch (err) {
            toast(err.message, "err");
          }
        })
      );

      $("btn-new-cmp").addEventListener("click", () => {
        const lOpts = lists.map((l) => `<option value="${esc(l.id)}">${esc(l.name)} (${l.active_count} người nhận)</option>`).join("");
        const tOpts = tpls.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
        const fOpts = accs.map((a) => `<option value="${esc(a.email)}">${esc(a.email)}</option>`).join("");
        openModal(
          "Tạo chiến dịch gửi hàng loạt",
          `<form id="cmp-form">
            <label>Tên chiến dịch</label>
            <input id="cp-name" type="text" placeholder="Thông báo khuyến mãi tháng 11" required />
            <div class="row">
              <div><label>Nhóm danh bạ nhận</label><select id="cp-list">${lOpts}</select></div>
              <div><label>Mẫu Email</label><select id="cp-tpl">${tOpts}</select></div>
            </div>
            <div class="row">
              <div><label>Gửi từ hộp thư</label><select id="cp-from">${fOpts}</select></div>
              <div><label>Tên người gửi</label><input id="cp-fname" type="text" value="Vmail Team" /></div>
            </div>
            <label>Tiêu đề (hỗ trợ {{name}}, để trống = lấy theo mẫu)</label>
            <input id="cp-sub" type="text" placeholder="Xin chào {{name}}, thông báo mới từ Vmail" />
            <div class="row">
              <div><label>Giãn cách giữa mỗi thư (ms)</label><input id="cp-delay" type="number" min="300" value="1500" /></div>
              <div><label>Giới hạn tối đa/ngày</label><input id="cp-limit" type="number" min="1" value="250" /></div>
            </div>
            <div class="row" style="margin-top:8px">
              <label style="display:flex;gap:8px;align-items:center"><input id="cp-open" type="checkbox" style="width:auto" checked/> Theo dõi tỷ lệ mở</label>
              <label style="display:flex;gap:8px;align-items:center"><input id="cp-unsub" type="checkbox" style="width:auto" checked/> Chèn link Hủy đăng ký (chống Spam)</label>
              <label style="display:flex;gap:8px;align-items:center"><input id="cp-auto" type="checkbox" style="width:auto" checked/> Chạy ngay sau khi tạo</label>
            </div>
            <button type="submit" class="btn btn-primary btn-block">Khởi tạo chiến dịch</button>
          </form>`,
          (box) => {
            box.querySelector("#cmp-form").addEventListener("submit", async (e) => {
              e.preventDefault();
              try {
                await api("/api/campaigns", {
                  method: "POST",
                  body: JSON.stringify({
                    name: box.querySelector("#cp-name").value.trim(),
                    list_id: box.querySelector("#cp-list").value,
                    template_id: box.querySelector("#cp-tpl").value,
                    from_email: box.querySelector("#cp-from").value,
                    from_name: box.querySelector("#cp-fname").value.trim(),
                    subject: box.querySelector("#cp-sub").value.trim(),
                    delay_ms: Number(box.querySelector("#cp-delay").value || 1500),
                    daily_limit: Number(box.querySelector("#cp-limit").value || 250),
                    track_opens: box.querySelector("#cp-open").checked,
                    include_unsub: box.querySelector("#cp-unsub").checked,
                    auto_start: box.querySelector("#cp-auto").checked,
                  }),
                });
                toast("Đã tạo chiến dịch!");
                closeModal();
                renderMarketing();
              } catch (err) {
                toast(err.message, "err");
              }
            });
          }
        );
      });
    } else if (state.marketingSub === "contacts") {
      const lRes = await api("/api/lists");
      const lists = lRes.lists || [];
      if (!state.selectedListId && lists.length) state.selectedListId = lists[0].id;
      const cRes = await api(`/api/contacts?list_id=${encodeURIComponent(state.selectedListId || "")}`);
      const listBtns = lists
        .map(
          (l) => `<button class="btn ${l.id === state.selectedListId ? "btn-primary" : "btn-outline"} btn-sm act-pick-list" data-id="${esc(l.id)}">${esc(l.name)} (${l.active_count}/${l.total_count})</button>`
        )
        .join(" ");
      const ctRows = (cRes.contacts || [])
        .map(
          (c) => `<tr>
            <td><strong>${esc(c.email)}</strong></td>
            <td>${esc(c.name || "-")}</td>
            <td><span class="badge ${c.status === "subscribed" ? "badge-ok" : "badge-warn"}">${esc(c.status)}</span></td>
            <td>${esc(c.tags || "-")}</td>
            <td><button class="btn btn-danger btn-sm act-del-ct" data-id="${esc(c.id)}">Xóa</button></td>
          </tr>`
        )
        .join("");

      body.innerHTML = `<div class="card">
        <div class="card-head">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">${listBtns}</div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-outline" id="btn-new-list">+ Tạo nhóm mới</button>
            <button class="btn btn-primary" id="btn-imp-ct">+ Nhập danh bạ hàng loạt</button>
          </div>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Email</th><th>Họ tên</th><th>Trạng thái</th><th>Nhãn</th><th>Thao tác</th></tr></thead>
          <tbody>${ctRows || '<tr><td colspan="5" class="muted">Nhóm này chưa có liên hệ nào.</td></tr>'}</tbody>
        </table></div>
      </div>`;

      body.querySelectorAll(".act-pick-list").forEach((b) =>
        b.addEventListener("click", () => {
          state.selectedListId = b.dataset.id;
          renderMarketing();
        })
      );
      body.querySelectorAll(".act-del-ct").forEach((b) =>
        b.addEventListener("click", async () => {
          b.disabled = true;
          try {
            await api(`/api/contacts/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" });
            toast("Đã xóa liên hệ.");
            renderMarketing();
          } catch (err) {
            toast(err.message, "err");
            b.disabled = false;
          }
        })
      );
      $("btn-new-list").addEventListener("click", () => {
        openModal(
          "Tạo nhóm danh bạ mới",
          `<form id="nl-form">
            <label>Tên nhóm</label><input id="nl-name" type="text" placeholder="Khách hàng VIP" required />
            <label>Mô tả</label><input id="nl-desc" type="text" />
            <button type="submit" class="btn btn-primary btn-block">Tạo nhóm</button>
          </form>`,
          (box) => {
            box.querySelector("#nl-form").addEventListener("submit", async (e) => {
              e.preventDefault();
              const subBtn = box.querySelector("#nl-form button[type='submit']");
              if (subBtn) subBtn.disabled = true;
              try {
                const r = await api("/api/lists", {
                  method: "POST",
                  body: JSON.stringify({ name: box.querySelector("#nl-name").value.trim(), description: box.querySelector("#nl-desc").value.trim() }),
                });
                state.selectedListId = r.id;
                toast("Đã tạo nhóm danh bạ!");
                closeModal();
                renderMarketing();
              } catch (err) {
                toast(err.message, "err");
                if (subBtn) subBtn.disabled = false;
              }
            });
          }
        );
      });
      $("btn-imp-ct").addEventListener("click", () => {
        openModal(
          "Nhập danh bạ hàng loạt (CSV / Mỗi dòng 1 email)",
          `<form id="imp-form">
            <label>Dán danh sách (định dạng: email, Tên hoặc chỉ email)</label>
            <textarea id="imp-raw" rows="8" placeholder="an@gmail.com, Nguyễn Văn An&#10;binh@yahoo.com, Trần Bình"></textarea>
            <label>Gắn nhãn (Tag)</label>
            <input id="imp-tags" type="text" placeholder="khach-moi" />
            <button type="submit" class="btn btn-primary btn-block">Nhập danh bạ ngay</button>
          </form>`,
          (box) => {
            box.querySelector("#imp-form").addEventListener("submit", async (e) => {
              e.preventDefault();
              const subBtn = box.querySelector("#imp-form button[type='submit']");
              if (subBtn) subBtn.disabled = true;
              try {
                const r = await api("/api/contacts/import", {
                  method: "POST",
                  body: JSON.stringify({
                    list_id: state.selectedListId || "list_default",
                    raw: box.querySelector("#imp-raw").value,
                    tags: box.querySelector("#imp-tags").value.trim(),
                  }),
                });
                toast(`Đã thêm ${r.added} liên hệ (bỏ qua ${r.skipped} dòng trùng/lỗi).`);
                closeModal();
                renderMarketing();
              } catch (err) {
                toast(err.message, "err");
                if (subBtn) subBtn.disabled = false;
              }
            });
          }
        );
      });
    } else {
      const tRes = await api("/api/templates");
      const tpls = tRes.templates || [];
      const rows = tpls
        .map(
          (t) => `<tr>
            <td><strong>${esc(t.name)}</strong></td>
            <td>${esc(t.subject)}</td>
            <td class="muted">${fmtTime(t.updated_at)}</td>
            <td>
              <div style="display:flex;gap:6px">
                <button class="btn btn-outline btn-sm act-edit-tpl" data-id="${esc(t.id)}">Sửa / Xem trước</button>
                <button class="btn btn-danger btn-sm act-del-tpl" data-id="${esc(t.id)}">Xóa</button>
              </div>
            </td>
          </tr>`
        )
        .join("");

      body.innerHTML = `<div class="card">
        <div class="card-head">
          <h3 class="card-title">Thư viện mẫu Email HTML (Hỗ trợ biến {{name}}, {{email}})</h3>
          <button class="btn btn-primary" id="btn-new-tpl">+ Tạo mẫu Email mới</button>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Tên mẫu</th><th>Tiêu đề mặc định</th><th>Cập nhật</th><th>Thao tác</th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
      </div>`;

      const openTplModal = (tpl = {}) => {
        openModal(
          tpl.id ? `Sửa mẫu: ${tpl.name}` : "Tạo mẫu Email HTML mới",
          `<form id="tpl-form">
            <label>Tên mẫu</label><input id="tp-name" type="text" value="${esc(tpl.name || "")}" required />
            <label>Tiêu đề Email</label><input id="tp-sub" type="text" value="${esc(tpl.subject || "")}" required />
            <label>Nội dung HTML</label><textarea id="tp-html" rows="8" required>${esc(tpl.html_body || "<div style='font-family:Arial,sans-serif'><h2>Xin chào {{name}},</h2><p>Nội dung thư...</p></div>")}</textarea>
            <button type="submit" class="btn btn-primary btn-block">Lưu mẫu Email</button>
          </form>`,
          (box) => {
            box.querySelector("#tpl-form").addEventListener("submit", async (e) => {
              e.preventDefault();
              const subBtn = box.querySelector("#tpl-form button[type='submit']");
              if (subBtn) subBtn.disabled = true;
              try {
                await api("/api/templates", {
                  method: "POST",
                  body: JSON.stringify({
                    id: tpl.id || "",
                    name: box.querySelector("#tp-name").value.trim(),
                    subject: box.querySelector("#tp-sub").value.trim(),
                    html_body: box.querySelector("#tp-html").value,
                  }),
                });
                toast("Đã lưu mẫu Email!");
                closeModal();
                renderMarketing();
              } catch (err) {
                toast(err.message, "err");
                if (subBtn) subBtn.disabled = false;
              }
            });
          }
        );
      };

      $("btn-new-tpl").addEventListener("click", () => openTplModal({}));
      body.querySelectorAll(".act-edit-tpl").forEach((b) =>
        b.addEventListener("click", () => openTplModal(tpls.find((x) => x.id === b.dataset.id) || {}))
      );
      body.querySelectorAll(".act-del-tpl").forEach((b) =>
        b.addEventListener("click", async () => {
          if (!confirm("Xóa mẫu email này?")) return;
          b.disabled = true;
          try {
            await api(`/api/templates/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" });
            toast("Đã xóa mẫu email.");
            renderMarketing();
          } catch (err) {
            toast(err.message, "err");
            b.disabled = false;
          }
        })
      );
    }
  }

  // ---------------- TAB 7: ANTI-SPAM & ISOLATION ----------------
  async function renderSpam() {
    const el = $("tab-spam");
    el.innerHTML = '<div class="card">Đang tải cấu hình bộ lọc Spam...</div>';
    const data = await api("/api/spam");
    const st = data.settings || {};
    const lists = data.domain_lists || [];
    const trusted = lists.filter((x) => x.namespace === "trusted-domains");
    const blocked = lists.filter((x) => x.namespace === "blocked-domains");
    const lookups = (data.http_lookups || [])
      .map(
        (lk) => `<tr>
          <td><strong>${esc(lk.id)}</strong><div class="muted">${esc(lk.url || "")}</div></td>
          <td><button class="btn ${lk.enable ? "btn-ok" : "btn-outline"} btn-sm act-lk-toggle" data-id="${esc(lk.id)}" data-en="${lk.enable ? "1" : "0"}">${lk.enable ? "Đang bật" : "Đang tắt"}</button></td>
        </tr>`
      )
      .join("");

    const chipList = (arr) =>
      arr
        .map(
          (i) =>
            `<span class="badge badge-info" style="margin:3px">${esc(i.key)} <button class="icon-btn act-del-mkey" data-id="${esc(i.id)}" style="font-size:12px;margin-left:4px">✕</button></span>`
        )
        .join("") || '<span class="muted">Danh sách trống</span>';

    el.innerHTML = `
      <div class="grid grid-2" style="margin-bottom:16px">
        <div class="card">
          <div class="card-head">
            <h3 class="card-title">Ngưỡng chấm điểm Spam (Vmail Anti-Spam Engine)</h3>
          </div>
          <form id="spam-form">
            <label style="display:flex;gap:8px;align-items:center"><input id="sp-en" type="checkbox" style="width:auto" ${st.enable ? "checked" : ""}/> Bật bộ lọc Spam tự động (SPF/DKIM/DMARC + DNSBL + Bayes)</label>
            <div class="row">
              <div><label>Điểm đánh dấu Spam (vào Thư rác)</label><input id="sp-s1" type="number" step="0.5" value="${st.scoreSpam ?? 5}" /></div>
              <div><label>Điểm loại bỏ (Discard)</label><input id="sp-s2" type="number" step="0.5" value="${st.scoreDiscard ?? 12}" /></div>
              <div><label>Điểm từ chối SMTP (Reject)</label><input id="sp-s3" type="number" step="0.5" value="${st.scoreReject ?? 16}" /></div>
            </div>
            <div class="row" style="margin-top:8px">
              <label style="display:flex;gap:8px;align-items:center"><input id="sp-tc" type="checkbox" style="width:auto" ${st.trustContacts ? "checked" : ""}/> Tin cậy người trong Danh bạ</label>
              <label style="display:flex;gap:8px;align-items:center"><input id="sp-tr" type="checkbox" style="width:auto" ${st.trustReplies ? "checked" : ""}/> Tin cậy thư trả lời</label>
            </div>
            <button type="submit" class="btn btn-primary btn-block">Lưu cấu hình lọc Spam</button>
          </form>
        </div>

        <div class="card">
          <div class="card-head">
            <h3 class="card-title">Trạng thái chạy kín &amp; An toàn hệ thống</h3>
            <span class="badge badge-ok">100% Kín · Độc lập hoàn toàn</span>
          </div>
          <div class="ok-box" style="margin-top:0">
            <div>🔒 Chế độ chạy kín nội bộ (Telemetry Sinkhole): <strong>${data.isolation?.telemetry_isolated ? "ĐANG BẢO VỆ" : "Chưa bật"}</strong></div>
            <div style="margin-top:6px">🔒 Cô lập kiểm tra kết nối bên ngoài (Vendor Isolation): <strong>${data.isolation?.vendor_isolated ? "ĐANG BẢO VỆ" : "Chưa bật"}</strong></div>
            <div style="margin-top:6px">🛡️ Strict CSP + Referrer-Policy + Security Headers: <strong>Đang bật trên /admin/</strong></div>
          </div>
          <h4 style="margin:14px 0 8px">Danh sách chặn mối đe dọa trực tuyến (Phishing / Disposable)</h4>
          <div class="table-wrap"><table><tbody>${lookups}</tbody></table></div>
        </div>
      </div>

      <div class="grid grid-2">
        <div class="card">
          <div class="card-head"><h3 class="card-title">✅ Tên miền tin cậy (Whitelist)</h3></div>
          <div style="margin-bottom:10px">${chipList(trusted)}</div>
          <form class="row" id="wl-form">
            <input id="wl-key" type="text" placeholder="partner.com" required />
            <button type="submit" class="btn btn-ok" style="flex:0">Thêm Whitelist</button>
          </form>
        </div>
        <div class="card">
          <div class="card-head"><h3 class="card-title">🚫 Tên miền chặn tuyệt đối (Blacklist)</h3></div>
          <div style="margin-bottom:10px">${chipList(blocked)}</div>
          <form class="row" id="bl-form">
            <input id="bl-key" type="text" placeholder="spammer.xyz" required />
            <button type="submit" class="btn btn-danger" style="flex:0">Chặn tên miền</button>
          </form>
        </div>
      </div>
    `;

    $("spam-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const subBtn = $("spam-form").querySelector("button[type='submit']");
      if (subBtn) subBtn.disabled = true;
      try {
        await api("/api/spam/settings", {
          method: "POST",
          body: JSON.stringify({
            enable: $("sp-en").checked,
            scoreSpam: Number($("sp-s1").value),
            scoreDiscard: Number($("sp-s2").value),
            scoreReject: Number($("sp-s3").value),
            trustContacts: $("sp-tc").checked,
            trustReplies: $("sp-tr").checked,
          }),
        });
        toast("Đã lưu cấu hình Anti-Spam!");
      } catch (err) {
        toast(err.message, "err");
      } finally {
        if (subBtn) subBtn.disabled = false;
      }
    });

    $("wl-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const subBtn = $("wl-form").querySelector("button[type='submit']");
      if (subBtn) subBtn.disabled = true;
      try {
        await api("/api/spam/domain-list", { method: "POST", body: JSON.stringify({ namespace: "trusted-domains", key: $("wl-key").value.trim() }) });
        toast("Đã thêm Whitelist!");
        renderSpam();
      } catch (err) {
        toast(err.message, "err");
        if (subBtn) subBtn.disabled = false;
      }
    });

    $("bl-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const subBtn = $("bl-form").querySelector("button[type='submit']");
      if (subBtn) subBtn.disabled = true;
      try {
        await api("/api/spam/domain-list", { method: "POST", body: JSON.stringify({ namespace: "blocked-domains", key: $("bl-key").value.trim() }) });
        toast("Đã thêm Blacklist!");
        renderSpam();
      } catch (err) {
        toast(err.message, "err");
        if (subBtn) subBtn.disabled = false;
      }
    });

    el.querySelectorAll(".act-del-mkey").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api(`/api/spam/domain-list/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" });
          toast("Đã xóa khỏi danh sách.");
          renderSpam();
        } catch (err) {
          toast(err.message, "err");
          b.disabled = false;
        }
      })
    );

    el.querySelectorAll(".act-lk-toggle").forEach((b) =>
      b.addEventListener("click", async () => {
        const nextEn = b.dataset.en !== "1";
        b.disabled = true;
        try {
          await api("/api/spam/lookup-toggle", { method: "POST", body: JSON.stringify({ id: b.dataset.id, enable: nextEn }) });
          toast("Đã cập nhật bộ lọc URL.");
          renderSpam();
        } catch (err) {
          toast(err.message, "err");
          b.disabled = false;
        }
      })
    );
  }

  // ---------------- TAB 8: LOGS & QUEUE ----------------
  async function renderLogs() {
    const el = $("tab-logs");
    el.innerHTML = '<div class="card">Đang tải nhật ký &amp; hàng đợi MTA...</div>';
    const [lRes, qRes] = await Promise.all([api("/api/logs"), api("/api/queue")]);
    const events = lRes.events || [];
    const queue = qRes.queue || [];

    const drawEvents = (kw = "") => {
      const q = kw.trim().toLowerCase();
      const filtered = events.filter(
        (e) =>
          !q ||
          (e.from || "").toLowerCase().includes(q) ||
          (e.to || "").toLowerCase().includes(q) ||
          (e.detail || "").toLowerCase().includes(q)
      );
      $("logs-tbody").innerHTML =
        filtered
          .map(
            (e) => `<tr>
              <td class="muted">${esc(e.time)}</td>
              <td><span class="badge ${e.status === "delivered" || e.status === "inbound" ? "badge-ok" : e.status === "spam" ? "badge-warn" : "badge-err"}">${esc(e.type)}</span></td>
              <td>${esc(e.from)}</td>
              <td>${esc(e.to)}</td>
              <td class="muted">${esc(e.detail)}</td>
            </tr>`
          )
          .join("") || '<tr><td colspan="5" class="muted">Không có dữ liệu phù hợp.</td></tr>';
    };

    const qRows = queue
      .map(
        (q) => `<tr>
          <td><code>${esc(q.id)}</code></td>
          <td>${esc(q.sender || "-")}</td>
          <td>${esc((q.recipients || []).map((r) => r.address || r).join(", "))}</td>
          <td>${esc(q.status || "queued")}</td>
          <td><button class="btn btn-danger btn-sm act-del-q" data-id="${esc(q.id)}">Hủy thư</button></td>
        </tr>`
      )
      .join("");

    el.innerHTML = `
      <div class="card" style="margin-bottom:16px">
        <div class="card-head">
          <h3 class="card-title">Hàng đợi gửi thư MTA hiện tại (${queue.length})</h3>
          <button class="btn btn-outline btn-sm" id="logs-reload">🔄 Làm mới</button>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Queue ID</th><th>Người gửi</th><th>Người nhận</th><th>Trạng thái</th><th>Thao tác</th></tr></thead>
          <tbody>${qRows || '<tr><td colspan="5" class="muted">Hàng đợi trống (mọi thư đã được xử lý xong).</td></tr>'}</tbody>
        </table></div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">Nhật ký giao dịch thư thời gian thực</h3>
          <input id="logs-search" type="search" placeholder="Lọc theo người gửi, người nhận, mã 250 OK..." style="max-width:340px" />
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Thời gian</th><th>Loại</th><th>Từ</th><th>Đến</th><th>Chi tiết SMTP / Spam</th></tr></thead>
          <tbody id="logs-tbody"></tbody>
        </table></div>
      </div>
    `;

    drawEvents();
    $("logs-search").addEventListener("input", (e) => drawEvents(e.target.value));
    $("logs-reload").addEventListener("click", renderLogs);
    el.querySelectorAll(".act-del-q").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api(`/api/queue/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" });
          toast("Đã xóa thư khỏi hàng đợi.");
          renderLogs();
        } catch (err) {
          toast(err.message, "err");
          b.disabled = false;
        }
      })
    );
  }

  // ---------------- BOOTSTRAP ----------------
  async function bootstrap() {
    $("modal-close").addEventListener("click", closeModal);
    $("modal-backdrop").addEventListener("click", (e) => {
      if (e.target === $("modal-backdrop")) closeModal();
    });
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.addEventListener("click", () => switchTab(b.dataset.tab))
    );
    $("logout-btn").addEventListener("click", async () => {
      await api("/api/logout", { method: "POST", body: "{}" }).catch(() => {});
      showLogin();
    });
    $("login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const errBox = $("login-err");
      errBox.classList.add("hidden");
      $("login-btn").disabled = true;
      try {
        const r = await api("/api/login", {
          method: "POST",
          body: JSON.stringify({ username: $("login-user").value.trim(), password: $("login-pass").value }),
        });
        state.user = r.user;
        showApp();
      } catch (err) {
        errBox.textContent = err.message;
        errBox.classList.remove("hidden");
      } finally {
        $("login-btn").disabled = false;
      }
    });

    try {
      const me = await api("/api/me");
      state.user = me.user;
      showApp();
    } catch (_) {
      showLogin();
    }
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();
