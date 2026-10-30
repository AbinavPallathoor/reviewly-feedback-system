// Reviewly front-end helpers (Alpine components)
function toast(message, kind = "success") {
  window.dispatchEvent(new CustomEvent("toast", { detail: { message, kind } }));
}

document.addEventListener("alpine:init", () => {
  Alpine.data("toaster", (initial) => ({
    items: [],
    init() {
      (initial || []).forEach(([kind, msg], i) => setTimeout(() => this.add(msg, kind), 150 + i * 120));
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

  // Up/down vote; clicking the same vote again removes it.
  Alpine.data("voter", (rid, up, down, mine, canVote) => ({
    up, down, mine,
    async vote(type, el) {
      if (!canVote) return toast("Log in to vote on reviews.", "error");
      const res = await fetch(`/reviews/${rid}/vote`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type }),
      });
      const data = await res.json().catch(() => ({ error: "Something went wrong." }));
      if (data.error) return toast(data.error, "error");
      Object.assign(this, { up: data.up, down: data.down, mine: data.mine });
    },
  }));
});

document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) lucide.createIcons();
  setTimeout(() => document.querySelectorAll(".bar-fill[data-w]").forEach((el) => (el.style.width = el.dataset.w + "%")), 120);
});
