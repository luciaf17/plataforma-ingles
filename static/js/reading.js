// Reading runner: tap a word to look it up (it lands in the file as a target
// VocabItem), highlight glossary terms, count words in the production box.
(function () {
  const cfg = JSON.parse(document.getElementById("reading-config").textContent);
  const csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  const glossary = new Set(cfg.glossary.map((g) => g.term.toLowerCase()));
  const glossaryWords = new Set();
  cfg.glossary.forEach((g) => g.term.toLowerCase().split(/\s+/).forEach((w) => glossaryWords.add(w)));

  // Wrap every word of the text in a span so it can be tapped.
  const text = document.getElementById("reading-text");
  text.querySelectorAll("p").forEach((p) => {
    const parts = [];
    p.childNodes.forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        node.textContent.split(/(\s+)/).forEach((tok) => {
          if (!tok) return;
          if (/^\s+$/.test(tok)) { parts.push(document.createTextNode(tok)); return; }
          const span = document.createElement("span");
          span.className = "w";
          const clean = tok.toLowerCase().replace(/^[^a-z0-9']+|[^a-z0-9']+$/g, "");
          if (glossaryWords.has(clean)) span.classList.add("gl");
          span.textContent = tok;
          span.dataset.word = clean;
          parts.push(span);
        });
      } else {
        parts.push(node.cloneNode(true));
      }
    });
    p.replaceChildren(...parts);
  });

  const lookup = document.getElementById("lookup");
  const body = document.getElementById("lookup-body");

  function show(data) {
    lookup.hidden = false;
    const es = data.note_es ? `<p class="es">${escapeHtml(data.note_es)}</p>` : "";
    body.innerHTML = `<b>${escapeHtml(data.term)}</b><p>${escapeHtml(data.definition_en)}</p>` +
      (data.example ? `<q>${escapeHtml(data.example)}</q>` : "") + es +
      `<p style="margin-top:8px"><span class="pill amber">${data.created ? "added to your file" : "already in your file"} · ${escapeHtml(data.status)}</span></p>`;
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  async function lookUp(word, sentence, el) {
    if (!word) return;
    lookup.hidden = false;
    body.innerHTML = `<b>${escapeHtml(word)}</b><p>Looking it up…</p>`;
    const form = new FormData();
    form.append("word", word);
    form.append("sentence", sentence || "");
    try {
      const res = await fetch(cfg.vocab_url, { method: "POST", headers: { "X-CSRFToken": csrf }, body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Lookup failed");
      show(data);
      if (el) el.classList.add("seen");
    } catch (err) {
      body.innerHTML = `<p class="hint">${escapeHtml(err.message)}</p>`;
    }
  }

  text.addEventListener("click", (e) => {
    const span = e.target.closest(".w");
    if (!span) return;
    const sentence = span.parentElement.textContent.trim().slice(0, 400);
    lookUp(span.dataset.word, sentence, span);
  });
  document.querySelectorAll("[data-glossary]").forEach((dt) => {
    dt.addEventListener("click", () => lookUp(dt.dataset.glossary, "", null));
  });

  // Word counter for the production answer.
  const production = document.getElementById("production");
  const counter = document.getElementById("counter");
  function count() {
    const v = production.value.trim();
    counter.textContent = (v ? v.split(/\s+/).length : 0) + " words";
  }
  production.addEventListener("input", count);
  count();
  document.getElementById("reading-form").addEventListener("submit", () => {
    const btn = document.getElementById("submit");
    btn.disabled = true;
    btn.textContent = "Checking…";
  });
})();
