// Speaking runner: push-to-talk loop, timer and phase changes (spec 5.1, 4.1b).
// Vanilla JS on purpose. The server owns the plan; this file owns the clock.
(function () {
  const cfg = JSON.parse(document.getElementById("lesson-config").textContent);
  const csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;

  const el = {
    mic: document.getElementById("mic"),
    status: document.getElementById("status"),
    transcript: document.getElementById("transcript"),
    timer: document.getElementById("timer"),
    phasePill: document.getElementById("phase-pill"),
    planItems: Array.from(document.querySelectorAll("#plan-list li")),
    audio: document.getElementById("tutor-audio"),
    textForm: document.getElementById("text-form"),
    textInput: document.getElementById("text-input"),
  };

  // ---- phases and clock -------------------------------------------------
  const phases = cfg.phases.map((p, i) => ({ ...p, index: i }));
  let starts = [];
  let acc = 0;
  phases.forEach((p) => { starts.push(acc); acc += p.minutes * 60; });
  const total = acc || cfg.duration_min * 60;

  let elapsed = cfg.elapsed_seconds;
  let phaseIndex = phaseFor(elapsed);
  let busy = false;
  // The clock moves the phase, but the tutor only mentions it in its next
  // reply, so a phase change never interrupts what it is saying.
  let phaseChanged = false;
  let phaseChangedAt = 0;
  const NUDGE_AFTER_MS = 25000;

  function phaseFor(sec) {
    let idx = 0;
    for (let i = 0; i < phases.length; i++) if (sec >= starts[i]) idx = i;
    return idx;
  }
  function elapsedInPhase() { return Math.max(0, elapsed - starts[phaseIndex]); }
  function mmss(s) { s = Math.max(0, Math.floor(s)); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }

  function renderPhase() {
    const p = phases[phaseIndex];
    el.phasePill.textContent = p ? p.title || p.key : "";
    el.planItems.forEach((li, i) => {
      li.classList.toggle("done", i < phaseIndex);
      li.classList.toggle("now", i === phaseIndex);
    });
  }
  function renderTimer() {
    el.timer.textContent = mmss(elapsed) + " / " + mmss(total);
    document.body.classList.toggle("timeup", elapsed >= total);
  }
  function tick() {
    elapsed += 1;
    renderTimer();
    const idx = phaseFor(elapsed);
    if (idx > phaseIndex) {
      phaseIndex = idx;
      phaseChanged = true;
      phaseChangedAt = Date.now();
      renderPhase();
    }
    maybeNudge();
  }
  // If she says nothing for a while after the phase changed, the tutor opens
  // the new phase itself so the class does not stall.
  function maybeNudge() {
    if (!phaseChanged || busy || !el.audio.paused || recording()) return;
    if (Date.now() - phaseChangedAt < NUDGE_AFTER_MS) return;
    phaseChanged = false;
    askTutor("phase_start");
  }

  // ---- transcript -------------------------------------------------------
  function appendLine(role, html, pending) {
    const div = document.createElement("div");
    div.className = "msg " + (role === "tutor" ? "tutor" : "you") + (pending ? " pending" : "");
    div.innerHTML = html;
    el.transcript.appendChild(div);
    el.transcript.scrollTop = el.transcript.scrollHeight;
    return div;
  }
  function escapeHtml(s) { return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function setStatus(msg, isError) {
    el.status.textContent = msg;
    el.status.classList.toggle("error", !!isError);
  }
  function setBusy(on) {
    busy = on;
    el.mic.disabled = on;
  }

  const audioQueue = [];
  function playTutor(tutor) {
    appendLine("tutor", tutor.html);
    if (!tutor.audio_url) return;
    audioQueue.push(tutor.audio_url);
    playNext();
  }
  function playNext() {
    if (!audioQueue.length || !el.audio.paused) return;
    el.audio.src = audioQueue.shift();
    el.audio.play().catch(() => setStatus("Tap the page once to enable audio, then hold to talk.", false));
  }
  el.audio.addEventListener("ended", playNext);

  // ---- server calls -----------------------------------------------------
  async function post(url, body) {
    const res = await fetch(url, { method: "POST", headers: { "X-CSRFToken": csrf }, body });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) throw new Error(data.error || ("Request failed (" + res.status + ")"));
    return data;
  }

  async function askTutor(event) {
    setBusy(true);
    setStatus("Tutor is thinking…");
    const body = new FormData();
    body.append("phase", phases[phaseIndex].key);
    body.append("event", event);
    body.append("elapsed_in_phase", elapsedInPhase());
    if (phaseChanged) body.append("phase_changed", "1");
    try {
      const data = await post(cfg.tutor_url, body);
      phaseChanged = false;
      playTutor(data.tutor);
      setStatus("Hold to talk · release to send · or hold the space bar");
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function sendTurn({ blob, text, durationMs }) {
    setBusy(true);
    const line = appendLine("you", text ? escapeHtml(text) : "…", true);
    setStatus(blob ? "Transcribing…" : "Sending…");
    const body = new FormData();
    if (blob) body.append("audio", blob, "turn.webm");
    if (text) body.append("text", text);
    body.append("phase", phases[phaseIndex].key);
    body.append("elapsed_in_phase", elapsedInPhase());
    if (phaseChanged) body.append("phase_changed", "1");
    if (durationMs) body.append("duration_ms", durationMs);
    try {
      const data = await post(cfg.turn_url, body);
      phaseChanged = false;
      line.textContent = data.learner.text;
      line.classList.remove("pending");
      setStatus("Tutor is thinking…");
      playTutor(data.tutor);
      setStatus("Hold to talk · release to send · or hold the space bar");
    } catch (err) {
      if (line.textContent === "…") line.remove();
      setStatus(err.message, true);
    } finally {
      setBusy(false);
    }
  }

  // ---- recording --------------------------------------------------------
  let stream = null;
  let recorder = null;
  function recording() { return !!recorder && recorder.state === "recording"; }
  let chunks = [];
  let recStart = 0;

  async function ensureStream() {
    if (stream) return stream;
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return stream;
  }
  function mimeType() {
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
    return candidates.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
  }
  async function startRecording() {
    if (busy || recording()) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      setStatus("This browser can't record audio. Use 'Type instead'.", true);
      return;
    }
    try {
      await ensureStream();
    } catch (e) {
      setStatus("Microphone blocked. Allow it in the browser, or use 'Type instead'.", true);
      return;
    }
    // She interrupted: drop whatever the tutor had queued.
    el.audio.pause();
    audioQueue.length = 0;
    chunks = [];
    recorder = new MediaRecorder(stream, mimeType() ? { mimeType: mimeType() } : undefined);
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = () => {
      const durationMs = Date.now() - recStart;
      el.mic.classList.remove("recording");
      if (durationMs < 400 || !chunks.length) {
        setStatus("That was too short. Hold the button while you speak.", true);
        return;
      }
      sendTurn({ blob: new Blob(chunks, { type: recorder.mimeType || "audio/webm" }), durationMs });
    };
    recStart = Date.now();
    recorder.start();
    el.mic.classList.add("recording");
    setStatus("Listening… release to send");
  }
  function stopRecording() {
    if (recorder && recorder.state === "recording") recorder.stop();
  }

  el.mic.addEventListener("pointerdown", (e) => { e.preventDefault(); startRecording(); });
  el.mic.addEventListener("pointerup", stopRecording);
  el.mic.addEventListener("pointerleave", stopRecording);
  el.mic.addEventListener("pointercancel", stopRecording);
  el.mic.addEventListener("contextmenu", (e) => e.preventDefault());

  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || e.target === el.textInput) return;
    e.preventDefault();
    startRecording();
  });
  document.addEventListener("keyup", (e) => {
    if (e.code !== "Space" || e.target === el.textInput) return;
    e.preventDefault();
    stopRecording();
  });

  el.textForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = el.textInput.value.trim();
    if (!text || busy) return;
    el.textInput.value = "";
    sendTurn({ text });
  });

  // ---- boot -------------------------------------------------------------
  renderPhase();
  renderTimer();
  setInterval(tick, 1000);
  if (!cfg.has_turns) askTutor("lesson_start");
  // The tutor keeps talking while she thinks; pressing the mic stops it.
})();
