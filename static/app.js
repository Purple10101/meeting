"use strict";

const $ = (id) => document.getElementById(id);
const el = {
  title: $("title"), device: $("device"), error: $("error"),
  pill: $("state-pill"), stateLabel: $("state-label"), timer: $("timer"), meter: $("meter-fill"),
  start: $("btn-start"), pause: $("btn-pause"), stop: $("btn-stop"),
  list: $("recordings"), empty: $("empty"), noResults: $("no-results"), count: $("count"), search: $("search"),
  tpl: $("recording-tpl"),
};

let state = "idle";
let pollTimer = null;

// --- API helper -----------------------------------------------------------

async function api(method, url, body) {
  const headers = { "X-Requested-With": "MeetingRecorder" };
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return res.status === 204 ? null : res.json();
}

function showError(err) {
  el.error.textContent = err ? err.message || String(err) : "";
  el.error.hidden = !err;
}

// --- Formatting -----------------------------------------------------------

function formatDuration(seconds) {
  const s = Math.floor(seconds || 0);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso) {
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function make(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

// --- Recorder -------------------------------------------------------------

const STATE_LABELS = { idle: "Ready", recording: "Recording", paused: "Paused" };

function renderStatus(status) {
  const changed = status.state !== state;
  state = status.state;

  el.pill.dataset.state = state;
  el.stateLabel.textContent = STATE_LABELS[state];
  el.timer.textContent = formatDuration(status.elapsed_seconds);
  // sqrt makes quiet speech visible on the meter without clipping loud peaks.
  el.meter.style.width = `${Math.min(100, Math.sqrt(status.level) * 100)}%`;

  el.start.disabled = state !== "idle";
  el.pause.disabled = state === "idle";
  el.stop.disabled = state === "idle";
  el.pause.textContent = state === "paused" ? "Resume" : "Pause";
  el.title.disabled = el.device.disabled = state !== "idle";

  if (changed) {
    // The state may have been changed from the tray menu, so refresh the library too.
    schedulePoll();
    loadRecordings();
  }
}

async function pollStatus() {
  try {
    renderStatus(await api("GET", "/api/status"));
  } catch (err) {
    // Server unreachable: keep polling quietly so the UI recovers when it returns.
  }
}

function schedulePoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(pollStatus, state === "idle" ? 3000 : 200);
}

async function loadDevices() {
  try {
    const devices = await api("GET", "/api/devices");
    el.device.replaceChildren(...devices.map((d) => {
      const opt = new Option(d.name + (d.is_default ? " (default)" : ""), d.index);
      opt.selected = d.is_default;
      return opt;
    }));
    if (!devices.length) el.device.append(new Option("No microphone found", ""));
  } catch (err) {
    showError(err);
  }
}

async function control(action, body) {
  showError(null);
  try {
    const result = await api("POST", `/api/recording/${action}`, body);
    await pollStatus();
    return result;
  } catch (err) {
    showError(err);
    await pollStatus();
  }
}

el.start.addEventListener("click", () => control("start", { title: el.title.value, device: el.device.value }));
el.pause.addEventListener("click", () => control(state === "paused" ? "resume" : "pause"));
el.stop.addEventListener("click", async () => {
  if (await control("stop")) el.title.value = "";
});

// --- Library --------------------------------------------------------------
//
// Each recording's DOM node is kept and updated in place (keyed by id), so the
// periodic refreshes during transcription don't interrupt playback or editing.

const entries = new Map(); // recording id -> { node, refs, rec, transcriptLoaded }
let loadSeq = 0;
let refreshTimer = null;
let searchTimer = null;

const isTranscribing = (rec) => rec.transcript_status === "queued" || rec.transcript_status === "running";

async function loadRecordings() {
  clearTimeout(refreshTimer);
  const seq = ++loadSeq;
  const query = el.search.value.trim();
  let recordings;
  try {
    recordings = await api("GET", `/api/recordings${query ? `?q=${encodeURIComponent(query)}` : ""}`);
  } catch (err) {
    showError(err);
    return;
  }
  if (seq !== loadSeq) return; // a newer request superseded this one
  renderRecordings(recordings, query);
  // Keep transcription progress fresh while anything is waiting or running.
  if (recordings.some(isTranscribing)) refreshTimer = setTimeout(loadRecordings, 1500);
}

function renderRecordings(recordings, query) {
  const ids = new Set(recordings.map((r) => r.id));
  for (const [id, entry] of entries) {
    if (!ids.has(id)) {
      releaseAudio(entry);
      entry.node.remove();
      entries.delete(id);
    }
  }
  recordings.forEach((rec, i) => {
    let entry = entries.get(rec.id);
    if (!entry) {
      entry = createEntry(rec);
      entries.set(rec.id, entry);
    }
    updateEntry(entry, rec);
    if (el.list.children[i] !== entry.node) el.list.insertBefore(entry.node, el.list.children[i] ?? null);
  });

  el.count.textContent = recordings.length ? `(${recordings.length})` : "";
  el.empty.hidden = recordings.length > 0 || !!query;
  el.noResults.hidden = recordings.length > 0 || !query;
}

function createEntry(rec) {
  const node = el.tpl.content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);
  const refs = {
    title: q(".rec-title"), meta: q(".rec-meta"), tStatus: q(".rec-transcript-status"),
    play: q(".act-play"), reveal: q(".act-reveal"), del: q(".act-delete"), audio: q(".rec-audio"),
    transcript: q(".rec-transcript"), tInfo: q(".transcript-info"), segments: q(".segments"),
    copy: q(".act-copy"), retranscribe: q(".act-retranscribe"),
    notesDetails: q(".rec-notes"), notes: q(".rec-notes textarea"),
  };
  const entry = { node, refs, rec: null, transcriptLoaded: false };
  const id = rec.id;

  refs.play.addEventListener("click", () => setAudioVisible(entry, refs.audio.hidden));
  refs.reveal.addEventListener("click", () => api("POST", `/api/recordings/${id}/show-in-folder`).catch(showError));

  const save = async (field, input) => {
    try {
      entry.rec = { ...entry.rec, ...(await api("PATCH", `/api/recordings/${id}`, { [field]: input.value })) };
      showError(null);
    } catch (err) {
      showError(err);
      input.value = entry.rec[field];
    }
  };
  refs.title.addEventListener("change", () => save("title", refs.title));
  refs.title.addEventListener("keydown", (e) => { if (e.key === "Enter") refs.title.blur(); });
  refs.notes.addEventListener("change", () => save("notes", refs.notes));

  refs.transcript.addEventListener("toggle", () => {
    if (refs.transcript.open && !entry.transcriptLoaded) loadTranscript(entry);
  });
  refs.copy.addEventListener("click", () => copyTranscript(entry));
  refs.retranscribe.addEventListener("click", () => {
    if (confirm("Transcribe this recording again? The current transcript will be replaced.")) transcribe(entry);
  });

  refs.del.addEventListener("click", async () => {
    if (!confirm(`Delete "${entry.rec.title}"? The audio file and transcript will be permanently removed.`)) return;
    // Release the browser's handle on the file, otherwise Windows won't let the server delete it.
    releaseAudio(entry);
    try {
      await api("DELETE", `/api/recordings/${id}`);
      loadRecordings();
    } catch (err) {
      showError(err);
    }
  });

  return entry;
}

function updateEntry(entry, rec) {
  const { refs } = entry;
  const prev = entry.rec;
  entry.rec = rec;
  const inProgress = rec.status === "recording";

  // Don't clobber a field the user is typing in.
  if (document.activeElement !== refs.title) refs.title.value = rec.title;
  if (document.activeElement !== refs.notes) refs.notes.value = rec.notes;
  if (!prev && rec.notes) refs.notesDetails.open = true;

  refs.meta.replaceChildren(inProgress
    ? `${formatDate(rec.started_at)} · in progress`
    : `${formatDate(rec.started_at)} · ${formatDuration(rec.duration_seconds)} · ${formatSize(rec.size_bytes)}`);
  if (rec.status !== "completed") {
    refs.meta.append(" ", make("span", {
      className: `badge ${rec.status}`, textContent: rec.status,
      title: rec.status === "interrupted" ? "The app closed during this recording; audio up to that point was kept." : "",
    }));
  }
  refs.play.disabled = refs.reveal.disabled = refs.del.disabled = inProgress;

  renderTranscriptStatus(entry);

  const done = rec.transcript_status === "completed";
  if (prev && prev.transcript_status !== rec.transcript_status) entry.transcriptLoaded = false;
  refs.transcript.hidden = !done;
  if (done && refs.transcript.open && !entry.transcriptLoaded) loadTranscript(entry);
}

function renderTranscriptStatus(entry) {
  const { rec, refs } = entry;
  const box = refs.tStatus;
  box.replaceChildren();
  box.hidden = !isTranscribing(rec) && rec.transcript_status !== "failed";

  if (rec.transcript_status === "queued") {
    box.append("Waiting to transcribe…");
  } else if (rec.transcript_status === "running") {
    const pct = Math.round((rec.transcript_progress || 0) * 100);
    const bar = make("span", { className: "progress" }, make("span"));
    bar.firstChild.style.width = `${pct}%`;
    box.append(pct ? `Transcribing… ${pct}%` : "Starting transcription…", bar);
  } else if (rec.transcript_status === "failed") {
    const retry = make("button", { className: "btn small", type: "button", textContent: "Retry" });
    retry.addEventListener("click", () => transcribe(entry));
    box.append(make("span", { className: "failed", textContent: "Transcription failed", title: rec.transcript_error || "" }), " ", retry);
  }
}

async function transcribe(entry) {
  showError(null);
  try {
    await api("POST", `/api/recordings/${entry.rec.id}/transcribe`);
    loadRecordings();
  } catch (err) {
    showError(err);
  }
}

async function loadTranscript(entry) {
  const { refs } = entry;
  entry.transcriptLoaded = true;
  try {
    const t = await api("GET", `/api/recordings/${entry.rec.id}/transcript`);
    entry.transcript = t;
    refs.tInfo.textContent = [t.language && `Language: ${t.language.toUpperCase()}`, t.model && `Model: ${t.model}`]
      .filter(Boolean).join(" · ");
    refs.copy.disabled = !t.segments.length;
    if (!t.segments.length) {
      refs.segments.replaceChildren(make("li", { className: "muted", textContent: "No speech was detected in this recording." }));
      return;
    }
    refs.segments.replaceChildren(...t.segments.map((seg) => {
      const ts = make("button", { className: "ts", type: "button", textContent: formatDuration(seg.start), title: "Play from here" });
      ts.addEventListener("click", () => playFrom(entry, seg.start));
      return make("li", {}, ts, make("span", { textContent: seg.text }));
    }));
  } catch (err) {
    entry.transcriptLoaded = false;
    showError(err);
  }
}

async function copyTranscript(entry) {
  const text = entry.transcript.segments.map((s) => `[${formatDuration(s.start)}] ${s.text}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Fallback for when the async clipboard API is unavailable.
    const ta = make("textarea", { value: text });
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  entry.refs.copy.textContent = "Copied";
  setTimeout(() => { entry.refs.copy.textContent = "Copy"; }, 1500);
}

// --- Audio player ---

function setAudioVisible(entry, visible) {
  const { audio, play } = entry.refs;
  if (visible) {
    if (!audio.getAttribute("src")) audio.src = `/api/recordings/${entry.rec.id}/audio`;
    audio.hidden = false;
    audio.play();
    play.textContent = "Hide";
  } else {
    audio.pause();
    audio.hidden = true;
    play.textContent = "Play";
  }
}

function playFrom(entry, seconds) {
  const { audio } = entry.refs;
  setAudioVisible(entry, true);
  if (audio.readyState === 0) {
    audio.addEventListener("loadedmetadata", () => { audio.currentTime = seconds; }, { once: true });
  } else {
    audio.currentTime = seconds;
  }
}

function releaseAudio(entry) {
  const { audio } = entry.refs;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
}

el.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadRecordings, 250);
});

// --- Boot -----------------------------------------------------------------

loadDevices();
loadRecordings();
pollStatus().then(schedulePoll);
