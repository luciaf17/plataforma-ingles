// Listening runner: plays the voiced lines in sequence as one track, counts
// full play-throughs (two max, spec 5.2), and offers 0.85x / 1.0x speed.
(function () {
  const cfg = JSON.parse(document.getElementById("listening-config").textContent);
  const csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  const audio = document.getElementById("audio");
  const play = document.getElementById("play");
  const icon = document.getElementById("play-icon");
  const progress = document.getElementById("progress");
  const status = document.getElementById("player-status");
  const listensEl = document.getElementById("listens");
  const speedBtn = document.getElementById("speed");

  let listens = cfg.listens;
  let index = 0;
  let playing = false;
  let started = false;
  let rate = 1;
  const total = cfg.segments.length;

  function left() { return Math.max(0, cfg.max_listens - listens); }
  function renderListens() {
    const n = left();
    listensEl.textContent = n + " listen" + (n === 1 ? "" : "s") + " left";
    listensEl.className = "pill " + (n ? "amber" : "red");
    play.disabled = n === 0 && !playing;
  }
  function setIcon(isPlaying) {
    icon.innerHTML = isPlaying
      ? '<path d="M7 4h4v16H7zM13 4h4v16h-4z" fill="currentColor"/>'
      : '<path d="M7 4v16l13-8z" fill="currentColor"/>';
  }
  function renderProgress() {
    const frac = total ? (index + (audio.duration ? audio.currentTime / audio.duration : 0)) / total : 0;
    progress.style.width = Math.min(100, Math.round(frac * 100)) + "%";
    status.textContent = playing ? "Playing · part " + (index + 1) + " of " + total : (started ? "Paused" : "Ready");
  }

  async function countListen() {
    listens += 1;
    renderListens();
    try {
      const res = await fetch(cfg.listened_url, { method: "POST", headers: { "X-CSRFToken": csrf } });
      const data = await res.json();
      if (res.ok) { listens = data.listens; renderListens(); }
    } catch (e) { /* the client count still applies */ }
  }

  function loadSegment(i) {
    index = i;
    audio.src = cfg.segments[i];
    audio.playbackRate = rate;
  }
  async function start() {
    if (!total || left() === 0) return;
    if (!started) { started = true; await countListen(); loadSegment(0); }
    playing = true;
    setIcon(true);
    audio.playbackRate = rate;
    audio.play().catch(() => { playing = false; setIcon(false); status.textContent = "Tap again to play."; });
  }
  function pause() {
    playing = false;
    audio.pause();
    setIcon(false);
    renderProgress();
  }

  play.addEventListener("click", () => (playing ? pause() : start()));
  audio.addEventListener("timeupdate", renderProgress);
  audio.addEventListener("ended", () => {
    if (index + 1 < total) {
      loadSegment(index + 1);
      audio.play().catch(() => {});
    } else {
      playing = false;
      started = false;
      index = 0;
      setIcon(false);
      progress.style.width = "100%";
      status.textContent = left() ? "Finished. You can listen once more." : "Finished. No listens left; answer from memory.";
      renderListens();
    }
  });
  speedBtn.addEventListener("click", () => {
    rate = rate === 1 ? 0.85 : 1;
    speedBtn.textContent = rate === 1 ? "1.0×" : "0.85×";
    audio.playbackRate = rate;
  });

  renderListens();
  renderProgress();
  document.getElementById("listening-form").addEventListener("submit", () => {
    audio.pause();
    document.getElementById("submit").disabled = true;
  });
})();
