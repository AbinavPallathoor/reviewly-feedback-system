/* Reviewly embeddable widgets.
 *
 * Full reviews block:
 *   <div data-reviewly-sku="SKU" data-reviewly-key="pk_..."
 *        data-accent="#111" data-star="#e5a00d" data-font="Inter" data-radius="8"></div>
 * Star snippet (for product grids):
 *   <span data-reviewly-stars="SKU" data-reviewly-key="pk_..."></span>
 *   <script async src="https://reviewly/widget.js"></script>
 *
 * The browser fetches reviews straight from Reviewly and renders them in a closed Shadow DOM,
 * so the host page can't restyle or rewrite them. The host can only theme the accent, star
 * colour, font and corner radius. The attribution line is fixed.
 */
(function () {
  "use strict";
  const script = document.currentScript || document.querySelector('script[src*="widget.js"]');
  const BASE = new URL(script.src).origin;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const stars = (n, size) => {
    let out = "";
    for (let i = 1; i <= 5; i++) out += `<span class="${i <= Math.floor(n + 0.25) ? "on" : "off"}">★</span>`;
    return `<span class="stars" style="font-size:${size || 15}px" aria-label="${n} out of 5">${out}</span>`;
  };

  const CSS = (o) => `
    :host { all: initial; display: block; }
    .rw { --accent: ${o.accent}; --star: ${o.star}; --r: ${o.radius}px; --fg: #1b1f24; --mute: #6a717b; --line: #e3e6ea; --soft: #f5f6f8;
          font-family: ${o.font}; color: var(--fg); line-height: 1.5; font-size: 15px; }
    .rw * { box-sizing: border-box; }
    .top { display: grid; grid-template-columns: 200px 1fr; gap: 32px; align-items: center; padding-bottom: 20px; border-bottom: 1px solid var(--line); }
    @media (max-width: 560px) { .top { grid-template-columns: 1fr; gap: 16px; } }
    .avg { font-size: 40px; font-weight: 700; line-height: 1.1; }
    .meta { color: var(--mute); font-size: 13px; margin-top: 2px; }
    .stars { color: var(--star); letter-spacing: 1px; }
    .stars .off { color: #d5d9de; }
    .dist { display: grid; gap: 5px; max-width: 420px; }
    .row { display: grid; grid-template-columns: 44px 1fr 28px; gap: 10px; align-items: center; font-size: 13px; color: var(--mute); }
    .track { height: 6px; background: var(--soft); border-radius: 3px; overflow: hidden; }
    .fill { height: 100%; background: var(--star); }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .crit { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 12px; font-size: 13px; color: var(--mute); }
    .crit b { color: var(--fg); font-weight: 600; }
    .rev { padding: 18px 0; border-bottom: 1px solid var(--line); }
    .head { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
    .who { font-weight: 600; font-size: 14px; }
    .ver { font-size: 12px; color: #0f6e50; }
    .date { color: var(--mute); font-size: 13px; margin-left: auto; }
    h4 { margin: 6px 0 2px; font-size: 15px; font-weight: 600; }
    p { margin: 0; }
    .text { font-size: 14px; color: #333a42; }
    .reply { margin-top: 10px; padding: 10px 12px; background: var(--soft); border-radius: var(--r); font-size: 13px; color: #333a42; }
    .reply b { display: block; margin-bottom: 2px; color: var(--fg); font-weight: 600; }
    .foot2 { display: flex; gap: 14px; margin-top: 8px; font-size: 12px; color: var(--mute); }
    .foot2 a { color: var(--mute); }
    button.more { font: inherit; font-size: 14px; margin-top: 16px; padding: 9px 16px; cursor: pointer;
                  background: #fff; color: var(--fg); border: 1px solid #c9ced5; border-radius: var(--r); }
    button.more:hover { border-color: var(--fg); }
    .empty, .err { padding: 24px 0; color: var(--mute); font-size: 14px; }
    /* Attribution: deliberately ignores every theme option. */
    .trust { all: initial; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 18px;
             font: 12px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color: #6a717b; }
    .trust .seal { all: initial; display: inline-grid; place-items: center; width: 16px; height: 16px; border-radius: 4px;
                   background: #0f6e50; color: #fff; font: 700 10px system-ui, sans-serif; }
    .trust a { all: initial; color: #0f6e50; font: 500 12px system-ui, sans-serif; cursor: pointer; text-decoration: underline; }
  `;

  function render(root, o, data) {
    const s = data.summary, total = s.total || 0;
    const dist = [5, 4, 3, 2, 1].map((k) => {
      const n = s.distribution[String(k)] || 0, pct = total ? Math.round((100 * n) / total) : 0;
      return `<div class="row"><span>${k} star</span><div class="track"><div class="fill" style="width:${pct}%"></div></div><span class="num">${n}</span></div>`;
    }).join("");
    const crit = Object.entries(s.criteria || {}).map(([k, v]) => `<span>${esc(k)} <b>${v}</b></span>`).join("");
    const reviewHtml = (r) => `
      <article class="rev">
        <div class="head">${stars(r.rating, 14)}<span class="who">${esc(r.author)}</span>
          ${r.verified ? '<span class="ver">✓ Verified purchase</span>' : ""}
          <span class="date">${esc(r.date)}${r.edited ? " · edited" : ""}</span></div>
        ${r.title ? `<h4>${esc(r.title)}</h4>` : ""}<p class="text">${esc(r.text)}</p>
        ${r.reply ? `<div class="reply"><b>Response from ${esc(data.product.seller)}</b>${esc(r.reply.text)}</div>` : ""}
        <div class="foot2">${r.helpful ? `<span>${r.helpful} found this helpful</span>` : ""}
          <a href="${esc(r.verify_url)}" target="_blank" rel="noopener">Verify on Reviewly</a></div>
      </article>`;
    const PAGE = 5;
    root.innerHTML = `<style>${CSS(o)}</style>
      <section class="rw">
        <div class="top">
          <div><div class="avg">${Number(s.average || 0).toFixed(1)}</div>${stars(s.average || 0, 17)}
            <div class="meta">Based on ${total} review${total === 1 ? "" : "s"}</div></div>
          <div><div class="dist">${dist}</div>${crit ? `<div class="crit">${crit}</div>` : ""}</div>
        </div>
        <div class="list">${data.reviews.length ? data.reviews.slice(0, PAGE).map(reviewHtml).join("") : '<div class="empty">No reviews yet.</div>'}</div>
        ${data.reviews.length > PAGE ? `<button class="more">Show all ${data.reviews.length} reviews</button>` : ""}
        ${trust(data)}
      </section>`;
    const more = root.querySelector("button.more");
    if (more) more.addEventListener("click", () => {
      root.querySelector(".list").innerHTML = data.reviews.map(reviewHtml).join("");
      more.remove();
    });
  }

  function trust(data) {
    return `<div class="trust"><span class="seal">R</span><span>Reviews collected and verified by Reviewly. The store can't edit or remove them.</span>
      ${data ? `<a href="${esc(data.product.url)}" target="_blank" rel="noopener">View on Reviewly</a>` : ""}</div>`;
  }

  function options(el) {
    return {
      accent: el.dataset.accent || "#1b1f24",
      star: el.dataset.star || "#e5a00d",
      font: el.dataset.font ? `"${el.dataset.font}", system-ui, sans-serif` : "system-ui, -apple-system, sans-serif",
      radius: parseInt(el.dataset.radius || "8", 10),
    };
  }

  function mount(el) {
    if (el.dataset.reviewlyMounted) return;
    el.dataset.reviewlyMounted = "1";
    const o = options(el);
    const root = el.attachShadow({ mode: "closed" });
    root.innerHTML = `<style>${CSS(o)}</style><section class="rw"><div class="meta">Loading reviews…</div></section>`;
    const url = `${BASE}/api/v1/public/products/${encodeURIComponent(el.dataset.reviewlySku)}?key=${encodeURIComponent(el.dataset.reviewlyKey || "")}`;
    fetch(url)
      .then((r) => r.json().then((d) => (r.ok ? d : Promise.reject(d.error || r.statusText))))
      .then((d) => render(root, o, d))
      .catch(() => {
        root.innerHTML = `<style>${CSS(o)}</style><section class="rw"><div class="err">Reviews are unavailable right now.</div>${trust(null)}</section>`;
      });
  }

  function mountStars(els) {
    const byKey = {};
    els.forEach((el) => { if (!el.dataset.reviewlyMounted) (byKey[el.dataset.reviewlyKey] ||= []).push(el); });
    Object.entries(byKey).forEach(([key, list]) => {
      const skus = [...new Set(list.map((el) => el.dataset.reviewlyStars))];
      fetch(`${BASE}/api/v1/public/summary?key=${encodeURIComponent(key)}&skus=${encodeURIComponent(skus.join(","))}`)
        .then((r) => (r.ok ? r.json() : {}))
        .then((data) => list.forEach((el) => {
          el.dataset.reviewlyMounted = "1";
          const s = data[el.dataset.reviewlyStars], o = options(el);
          const root = el.attachShadow({ mode: "closed" });
          root.innerHTML = `<style>:host{all:initial;display:inline-block}.s{font:13px system-ui,sans-serif;color:#6a717b;white-space:nowrap}
            .stars{color:${o.star};letter-spacing:1px}.stars .off{color:#d5d9de}</style>` +
            (s && s.total ? `<span class="s">${stars(s.average, 13)} ${Number(s.average).toFixed(1)} (${s.total})</span>`
                          : `<span class="s">No reviews yet</span>`);
        }))
        .catch(() => {});
    });
  }

  const scan = () => {
    document.querySelectorAll("[data-reviewly-sku]").forEach(mount);
    mountStars(document.querySelectorAll("[data-reviewly-stars]"));
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan);
  else scan();
})();
