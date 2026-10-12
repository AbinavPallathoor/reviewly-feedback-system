// Reviewly front-end helpers (Alpine components + small animations)
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("reviewly-theme", t); } catch (e) {}
  document.dispatchEvent(new CustomEvent("themechange", { detail: t }));
}
function toggleTheme() {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function toast(message, kind = "success") {
  window.dispatchEvent(new CustomEvent("toast", { detail: { message, kind } }));
}

document.addEventListener("alpine:init", () => {
  Alpine.data("toaster", (initial) => ({
    items: [],
    init() {
      (initial || []).forEach(([kind, msg], i) => setTimeout(() => this.add(msg, kind), 120 + i * 120));
      window.addEventListener("toast", (e) => this.add(e.detail.message, e.detail.kind));
    },
    add(message, kind) {
      const id = Date.now() + Math.random();
      this.items.push({ id, message, kind, show: true });
      setTimeout(() => this.dismiss(id), 3800);
    },
    dismiss(id) {
      const t = this.items.find((x) => x.id === id);
      if (t) t.show = false;
      setTimeout(() => (this.items = this.items.filter((x) => x.id !== id)), 250);
    },
  }));

  // Star rating input with hover preview. Writes to a hidden input named `name`.
  Alpine.data("stars", (value = 0) => ({
    value: Number(value) || 0,
    hover: 0,
    get shown() { return this.hover || this.value; },
  }));

  // Up/down vote with toggle and pop animation.
  Alpine.data("voter", (rid, up, down, mine, canVote) => ({
    up, down, mine,
    async vote(type, el) {
      if (!canVote) return toast("Log in to vote on reviews.", "error");
      el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop");
      const res = await fetch(`/reviews/${rid}/vote`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type }),
      });
      const data = await res.json().catch(() => ({ error: "Something went wrong." }));
      if (data.error) return toast(data.error, "error");
      Object.assign(this, { up: data.up, down: data.down, mine: data.mine });
    },
  }));
});

function countUp(el) {
  const target = parseFloat(el.dataset.count);
  if (isNaN(target)) return;
  const decimals = (el.dataset.count.split(".")[1] || "").length;
  if (reduceMotion) return (el.textContent = target.toFixed(decimals));
  const start = performance.now(), dur = 900;
  const step = (now) => {
    const p = Math.min(1, (now - start) / dur), eased = 1 - Math.pow(1 - p, 3);
    el.textContent = (target * eased).toFixed(decimals);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".reveal").forEach((el, i) => el.style.setProperty("--i", Math.min(i, 14)));
  document.querySelectorAll("[data-count]").forEach(countUp);
  requestAnimationFrame(() =>
    setTimeout(() => document.querySelectorAll(".fill[data-w]").forEach((el) => (el.style.width = el.dataset.w + "%")), 80)
  );
});
