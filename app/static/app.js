/* pCloud Sync — interface */

const cards = new Map();
let plans = {};

/* -- formatting ----------------------------------------------------------- */

function bytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : n < 10 ? 2 : 1)} ${units[i]}`;
}

function count(n) {
  return (n ?? 0).toLocaleString();
}

function duration(s) {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(Math.round(s % 60)).padStart(2, "0")}`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}

function eta(s) {
  if (s == null || s === 0) return "—";
  return duration(s);
}

function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return `today ${time}`;
  return `${d.toLocaleDateString([], { day: "2-digit", month: "2-digit" })} ${time}`;
}

/* -- network -------------------------------------------------------------- */

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || body.detail || `Error ${res.status}`);
  return body;
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 6000);
}

/* -- card construction ---------------------------------------------------- */

function buildCard(profile) {
  const node = document.getElementById("tpl-profile").content.firstElementChild.cloneNode(true);
  node.dataset.profile = profile.id;
  node.querySelector("[data-name]").textContent = profile.name;
  node.querySelector("[data-local]").textContent = profile.local;
  node.querySelector("[data-remote]").textContent = profile.remote;

  node.querySelectorAll("[data-act]").forEach((btn) => {
    const a = btn.dataset.act;
    if (a === "edit")   { btn.addEventListener("click", () => editProfile(profile.id)); return; }
    if (a === "delete") { btn.addEventListener("click", () => deleteProfile(profile.id)); return; }
    btn.addEventListener("click", () => act(profile.id, a));
  });

  node.querySelector("[data-fig-deletes]").addEventListener("click", () =>
    openDrawer(profile.id, "delete")
  );
  node.querySelector("[data-fig-transfers]").addEventListener("click", () =>
    openDrawer(profile.id, "send")
  );

  document.getElementById("profiles").appendChild(node);
  cards.set(profile.id, node);
  return node;
}

async function act(id, action) {
  const card = cards.get(id);
  card.querySelectorAll("[data-act]").forEach((b) => (b.disabled = true));
  try {
    await api(`/api/profiles/${id}/${action}`, { method: "POST" });
    await refresh();
  } catch (err) {
    toast(err.message);
    card.querySelectorAll("[data-act]").forEach((b) => (b.disabled = false));
  }
}

/* -- rendering ------------------------------------------------------------- */

function renderRail(card, phase) {
  const order = { analysis: 0, validation: 1, transfer: 2 };
  const current = order[phase];
  card.querySelectorAll(".rail__step").forEach((step) => {
    const index = order[step.dataset.step];
    step.classList.toggle("is-active", phase === step.dataset.step);
    step.classList.toggle("is-done", current !== undefined && index < current);
  });
}

function age(iso) {
  if (!iso) return null;
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 90) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)} h ago`;
  return `${Math.round(secs / 86400)} d ago`;
}

function renderVerdict(card, plan) {
  const el = card.querySelector("[data-verdict]");
  if (!plan || plan.empty) { el.hidden = true; return; }

  const parts = [];
  if (plan.transfers > 0) {
    parts.push(`<b class="v-send">${count(plan.transfers)} file${plan.transfers > 1 ? "s" : ""}</b> `
             + `to upload, ${bytes(plan.bytes_to_send)} leaving `
             + `<b class="v-send">your machine</b>`);
  }
  if (plan.moved > 0) {
    parts.push(`<b class="v-free">${count(plan.moved)} file${plan.moved > 1 ? "s" : ""}</b> `
             + `will be moved by pCloud, with nothing uploaded`);
  }
  if (plan.deletes > 0) {
    parts.push(`<b class="v-cut">${count(plan.deletes)} file${plan.deletes > 1 ? "s" : ""}</b> `
             + `will be deleted from the cloud`);
  }

  let sentence = parts.length
    ? parts.join(" · ").replace(/^./, (c) => c.toUpperCase())
    : "Nothing to transfer.";
  if (plan.deletes === 0 && plan.transfers > 0) sentence += " · no deletions";

  // A plan recovered at startup can be old: the transfer re-evaluates the
  // real situation anyway, but the figures on screen date from the moment
  // of the analysis.
  const ageText = age(plan.computed_at);
  const stale = plan.computed_at
    && (Date.now() - new Date(plan.computed_at).getTime()) > 6 * 3600 * 1000;
  if (ageText) {
    sentence += `<span class="verdict__age${stale ? " is-old" : ""}">`
              + `analyzed ${ageText}${stale ? " — a fresh analysis would be more reliable" : ""}`
              + `</span>`;
  }

  el.innerHTML = sentence;
  el.hidden = false;
}

function renderWire(card, plan) {
  const wire = card.querySelector("[data-wire]");
  const free = plan ? (plan.bytes_moved || 0) : 0;
  const send = plan ? (plan.bytes_to_send || 0) : 0;

  // Comparing two tracks only makes sense when both exist. With one, the
  // bar teaches nothing the summary sentence does not already say.
  if (!plan || plan.empty || free === 0 || send === 0) { wire.hidden = true; return; }
  const total = Math.max(free + send, 1);

  const freeTrack = card.querySelector("[data-track-free]");
  const sendTrack = card.querySelector("[data-track-send]");

  // 12% floor so a minority track stays readable
  const pct = (v) => Math.max(12, Math.round((v / total) * 100));
  freeTrack.style.width = free > 0 ? `${pct(free)}%` : "0";
  sendTrack.style.width = send > 0 ? `${pct(send)}%` : "0";
  freeTrack.hidden = free === 0;
  sendTrack.hidden = send === 0;

  card.querySelector("[data-free-label]").textContent =
    free > 0 ? `${bytes(free)} · ${count(plan.moved)} files` : "";
  card.querySelector("[data-send-label]").textContent =
    send > 0 ? `${bytes(send)} · ${count(plan.transfers)} files` : "";

  wire.hidden = false;
}

function renderFigures(card, plan) {
  const figures = card.querySelector("[data-figures]");
  if (!plan || plan.empty) { figures.hidden = true; return; }

  card.querySelector("[data-fig-moved]").textContent = count(plan.moved);
  const uploads = card.querySelector("[data-fig-transfers]");
  uploads.textContent = count(plan.transfers);
  uploads.disabled = plan.transfers === 0;
  card.querySelector("[data-fig-transfers-wrap]").classList.toggle("is-zero", plan.transfers === 0);
  card.querySelector("[data-fig-checks]").textContent = count(plan.checks);

  const deletes = card.querySelector("[data-fig-deletes]");
  deletes.textContent = count(plan.deletes);
  deletes.disabled = plan.deletes === 0;
  card.querySelector("[data-fig-deletes-wrap]").classList.toggle("is-zero", plan.deletes === 0);

  figures.hidden = false;
}

// Local timestamp of the last refresh, per profile. Lets the clock advance
// between two server responses.
const ticking = {};

function renderLive(card, id, phase, live, elapsedServer, plan) {
  const box = card.querySelector("[data-live]");
  const running = phase === "analysis" || phase === "transfer";

  if (!running) {
    box.hidden = true;
    delete ticking[id];
    return;
  }

  const l = live || {};
  const send = phase === "transfer";

  // Clock: the base comes from the server, the flow is computed here. So it
  // keeps advancing even when the server does not answer, which is exactly
  // the moment the user needs to know the thing is alive.
  const base = Number(l.elapsed || elapsedServer || 0);
  if (!ticking[id] || Math.abs(ticking[id].base - base) > 2) {
    ticking[id] = { base, at: Date.now() };
  }
  const secs = ticking[id].base + (Date.now() - ticking[id].at) / 1000;
  card.querySelector("[data-live-clock]").textContent = duration(secs);

  card.querySelector("[data-pulse]").classList.toggle("pulse--send", send);

  const checks = Number(l.checks || 0);
  const totalChecks = Number(l.total_checks || 0);
  const bytesDone = Number(l.bytes || 0);
  const bytesTotal = Number(l.total_bytes || 0);
  const listed = Number(l.listed || 0);
  const localTotal = Number(l.local_total || 0);
  const localDone = Boolean(l.local_done);

  // Denominator. rclone's totalChecks is a sliding total that grows at the
  // same pace as checks: it would show 99% forever. So our own count of
  // local files is used, plus the moves, because rclone counts one extra
  // comparison for each of them.
  const renames = Number(l.renames || 0);
  const denom = localTotal > 0 ? localTotal + renames : totalChecks;
  const approx = !localDone;

  let what;
  if (send) {
    what = "Transfer in progress";
  } else if (checks === 0) {
    what = "Taking file inventory";
  } else {
    what = "Comparing checksums";
  }
  card.querySelector("[data-live-what]").textContent = what;

  const bar = card.querySelector("[data-live-bar]");
  const fill = card.querySelector("[data-live-fill]");
  bar.classList.toggle("live__bar--send", send);

  // A transfer may upload nothing at all: a pure reorganisation
  // (server-side moves) or deletions only. bytesTotal then stays at zero
  // and the bar never appeared, although the plan gives the expected total
  // and the stats the real progress.
  const movedTotal = send && plan ? Number(plan.moved || 0) : 0;
  const deleteTotal = send && plan ? Number(plan.deletes || 0) : 0;
  const deletesDone = Number(l.deletes || 0);

  let pct = null;
  if (send && bytesTotal > 0) {
    pct = Math.round((bytesDone / bytesTotal) * 100);
  } else if (send && movedTotal > 0) {
    pct = Math.min(100, Math.round((renames / movedTotal) * 100));
  } else if (send && deleteTotal > 0) {
    pct = Math.min(100, Math.round((deletesDone / deleteTotal) * 100));
  } else if (!send && localDone && denom > 0 && checks > 0) {
    // Until the local count is finished, the denominator is undervalued:
    // showing a percentage then overstates it, and the monotonic lock
    // would freeze that too-high value afterwards.
    // Capped at 99: the denominator remains an estimate, and a bar at
    // 100% on a running operation would lie.
    pct = Math.min(99, Math.round((checks / denom) * 100));
    // The denominator grows with the discovered moves. Without this lock,
    // the bar would occasionally move backwards, which looks like the work
    // is being undone.
    const memo = ticking[id];
    if (memo) {
      pct = Math.max(pct, memo.pct || 0);
      memo.pct = pct;
    }
  }

  if (pct === null) {
    bar.hidden = true;
  } else {
    bar.hidden = false;
    fill.style.width = `${Math.min(pct, 100)}%`;
  }

  // Each value becomes its own element: concatenated into a single string,
  // they merge into one flex item and the gap no longer separates them.
  const bits = [];
  if (send) {
    if (bytesTotal > 0) {
      bits.push(`<b>${bytes(bytesDone)}</b> / ${bytes(bytesTotal)}${pct !== null ? ` · ${pct}%` : ""}`);
      bits.push(`<span class="n-send">${bytes(l.speed)}/s</span>`);
      if (l.eta) bits.push(`${eta(l.eta)} left`);
      if (l.renames) bits.push(`<span class="n-free">${count(l.renames)} moved</span>`);
    } else if (movedTotal > 0) {
      // "0 B / 0 B · 0 B/s" teaches nothing when nothing uploads: show the
      // progress of the moves, the only thing advancing.
      bits.push(`<b class="n-free">${count(renames)}</b> / ${count(movedTotal)} moved`
              + `${pct !== null ? ` · ${pct}%` : ""}`);
      if (deleteTotal > 0) {
        bits.push(`<span class="n-cut">${count(deletesDone)} / ${count(deleteTotal)} deletions</span>`);
      }
    } else if (deleteTotal > 0) {
      bits.push(`<b class="n-cut">${count(deletesDone)}</b> / ${count(deleteTotal)} deletions`
              + `${pct !== null ? ` · ${pct}%` : ""}`);
    } else {
      bits.push(`<b>${bytes(bytesDone)}</b> / ${bytes(bytesTotal)}`);
    }
  } else if (checks > 0) {
    bits.push(denom > 0
      ? `<b>${count(checks)}</b> / ${approx ? "~" : ""}${count(denom)} comparisons`
        + `${pct !== null ? ` · ${pct}%` : ""}`
      : `<b>${count(checks)}</b> comparisons`);
    if (l.renames) bits.push(`<span class="n-free">${count(l.renames)} moves</span>`);
    if (l.seen_copies) bits.push(`<span class="n-send">${count(l.seen_copies)} to upload</span>`);
    if (l.seen_deletes) bits.push(`<span class="n-cut">${count(l.seen_deletes)} deletions</span>`);
  } else {
    // Inventory phase: no comparison and no percentage possible, show what
    // actually moves to prove the machine is advancing.
    bits.push(localTotal > 0
      ? `<b>${approx ? "~" : ""}${count(localTotal)}</b> local files counted`
      : "counting local files…");
    if (listed) bits.push(`${count(listed)} entries listed`);
    if (l.seen_copies) bits.push(`<span class="n-send">${count(l.seen_copies)} to upload</span>`);
  }
  if (l.errors) bits.push(`<span class="n-cut">${count(l.errors)} errors</span>`);

  const row = card.querySelector("[data-live-row]");
  row.innerHTML = "";
  bits.forEach((b) => {
    const el = document.createElement("span");
    el.innerHTML = b;
    row.appendChild(el);
  });

  // Files in flight
  const list = card.querySelector("[data-live-files]");
  list.innerHTML = "";
  const items = send
    ? (l.transferring || []).map((f) => [f.name, `${Math.round(f.percentage)}%`])
    : (l.checking || []).map((n) => [n, ""]);
  items.forEach(([name, right]) => {
    const li = document.createElement("li");
    const a = document.createElement("span");
    a.textContent = name;
    const b = document.createElement("span");
    b.textContent = right;
    li.append(a, b);
    list.appendChild(li);
  });

  box.hidden = false;
}

function renderCard(profile) {
  const card = cards.get(profile.id) || buildCard(profile);
  const st = profile.state;
  const running = st.phase === "analysis" || st.phase === "transfer";

  renderRail(card, st.phase);
  card.querySelector("[data-status]").textContent = st.message || "";

  // Scheduling and last success
  const schedule = profile.schedule
    ? `scheduled ${profile.schedule}${profile.next_run ? " · next " + when(profile.next_run) : ""}`
    : "manual start";
  card.querySelector("[data-schedule]").textContent = schedule;
  card.querySelector("[data-lastrun]").textContent = profile.last_success
    ? `last transfer ${when(profile.last_success.ended_at)}`
    : "no transfer recorded";

  renderVerdict(card, st.plan);
  renderWire(card, st.plan);
  renderFigures(card, st.plan);
  renderLive(card, profile.id, st.phase, st.live, st.elapsed, st.plan);

  const blocked = card.querySelector("[data-blocked]");
  blocked.hidden = !st.blocked_reason;
  blocked.textContent = st.blocked_reason || "";

  const error = card.querySelector("[data-error]");
  const problem = st.phase === "error" ? st.last_error : "";
  error.hidden = !problem;
  error.textContent = problem || "";

  if (!profile.local_exists) {
    error.hidden = false;
    error.textContent = `Local folder not found: ${profile.local}. Check that the drive is connected.`;
  }

  // Buttons
  const btn = (a) => card.querySelector(`[data-act="${a}"]`);
  btn("analyse").hidden = running || st.phase === "validation";
  btn("run").hidden = running || st.phase === "validation";
  btn("sync").hidden = st.phase !== "validation";
  btn("cancel").hidden = !running && st.phase !== "validation";
  btn("cancel").textContent = running ? "Stop" : "Discard plan";

  btn("edit").hidden = running;
  btn("delete").hidden = running;

  card.querySelectorAll("[data-act]").forEach((b) => (b.disabled = false));

  plans[profile.id] = { name: profile.name, plan: st.plan };
  known[profile.id] = profile;
  if (running) busyProfiles.add(profile.id);
  else busyProfiles.delete(profile.id);
}

/* -- drawer ---------------------------------------------------------------- */

let drawerState = null;   // { items, isSend, total, folders }

async function openDrawer(id, kind) {
  const entry = plans[id];
  if (!entry || !entry.plan) return;
  const isSend = kind === "send";
  const total = isSend ? entry.plan.transfers : entry.plan.deletes;
  if (!total) return;

  document.getElementById("drawer-title").textContent = isSend
    ? `${count(total)} files to upload — ${entry.name}`
    : `${count(total)} deletions — ${entry.name}`;

  const body = document.getElementById("drawer-body");
  body.innerHTML = '<p class="picker__load">Loading the list…</p>';
  document.getElementById("drawer").hidden = false;
  document.getElementById("drawer-close").focus();

  // The complete list is requested here, not in the current state: that one
  // is refreshed every second and would carry thousands of paths for
  // nothing.
  let plan;
  try {
    plan = await api(`/api/profiles/${id}/plan`);
  } catch (err) {
    body.innerHTML = `<p class="picker__empty">${err.message}</p>`;
    return;
  }

  const items = isSend
    ? (plan.send_samples || [])
    : (plan.delete_samples || []).map((c) => ({ path: c, bytes: 0 }));

  drawerState = { items, isSend, total, folders: isSend ? plan.send_by_folder : plan.delete_by_folder };
  paintDrawer("");
}

function paintDrawer(filter) {
  const { items, isSend, total, folders } = drawerState;
  const body = document.getElementById("drawer-body");
  body.innerHTML = "";

  const intro = document.createElement("p");
  intro.className = "drawer__intro";
  intro.textContent = isSend
    ? "These files exist nowhere on pCloud. They have to go up from your "
      + "machine and will use your bandwidth. Files that merely moved are "
      + "not listed here: pCloud repositions them without transferring "
      + "anything."
    : "These files exist on pCloud but no longer exist locally. The transfer "
      + "will remove them from the cloud. If unexpected folders appear "
      + "below, discard the plan.";
  body.appendChild(intro);

  // Tools: filter and copy
  const tools = document.createElement("div");
  tools.className = "drawer__tools";

  const field = document.createElement("input");
  field.type = "search";
  field.placeholder = "Filter by name or folder…";
  field.value = filter;
  field.addEventListener("input", () => {
    const pos = field.selectionStart;
    paintDrawer(field.value);
    const fresh = document.querySelector(".drawer__tools input");
    if (fresh) { fresh.focus(); fresh.setSelectionRange(pos, pos); }
  });

  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "btn btn--ghost";
  copyBtn.textContent = "Copy list";
  copyBtn.addEventListener("click", () => {
    const text = matches().map((i) => i.path).join("\n");
    navigator.clipboard.writeText(text)
      .then(() => { copyBtn.textContent = "Copied"; setTimeout(() => (copyBtn.textContent = "Copy list"), 1800); })
      .catch(() => toast("Copying is not available in this context."));
  });

  tools.append(field, copyBtn);
  body.appendChild(tools);

  function matches() {
    const f = filter.trim().toLowerCase();
    return f ? items.filter((i) => i.path.toLowerCase().includes(f)) : items;
  }

  const visible = matches();

  if (folders && folders.length && !filter) {
    const h = document.createElement("h3");
    h.textContent = `By folder (${count(folders.length)})`;
    const wrap = document.createElement("div");
    wrap.className = "folders";
    folders.forEach((f) => {
      const row = document.createElement("div");
      const name = document.createElement("span");
      name.textContent = f.folder;
      const right = document.createElement("strong");
      right.className = isSend ? "f-send" : "f-cut";
      right.textContent = isSend && f.bytes
        ? `${count(f.count)} · ${bytes(f.bytes)}`
        : count(f.count);
      row.append(name, right);
      wrap.appendChild(row);
    });
    body.append(h, wrap);
  }

  const h = document.createElement("h3");
  h.textContent = filter
    ? `${count(visible.length)} result${visible.length > 1 ? "s" : ""} out of ${count(items.length)}`
    : `All files (${count(items.length)})`;
  body.appendChild(h);

  if (!visible.length) {
    const none = document.createElement("p");
    none.className = "picker__empty";
    none.textContent = "No file matches.";
    body.appendChild(none);
    return;
  }

  const ul = document.createElement("ul");
  ul.className = "samples";

  // Chunked display: injecting nine thousand rows at once freezes the
  // interface for seconds.
  const CHUNK = 400;
  let rendered = 0;

  const addChunk = () => {
    const batch = visible.slice(rendered, rendered + CHUNK);
    batch.forEach((it) => {
      const li = document.createElement("li");
      if (isSend && it.bytes) {
        li.className = "samples__row";
        const name = document.createElement("span");
        name.textContent = it.path;
        const size = document.createElement("em");
        size.textContent = bytes(it.bytes);
        li.append(name, size);
      } else {
        li.textContent = it.path;
      }
      ul.appendChild(li);
    });
    rendered += batch.length;
    moreBtn.hidden = rendered >= visible.length;
    moreBtn.textContent = `Show the next ${count(Math.min(CHUNK, visible.length - rendered))} `
                        + `(${count(rendered)} / ${count(visible.length)})`;
  };

  const moreBtn = document.createElement("button");
  moreBtn.type = "button";
  moreBtn.className = "btn btn--ghost drawer__more";
  moreBtn.addEventListener("click", addChunk);

  body.append(ul, moreBtn);
  addChunk();
}

function closeDrawer() {
  document.getElementById("drawer").hidden = true;
  drawerState = null;
}

/* -- history ---------------------------------------------------------------- */

const STATUS = {
  success:    ["Success", "tag--ok"],
  failed:     ["Failed", "tag--ko"],
  running:    ["Running", "tag--run"],
  cancelled:  ["Cancelled", "tag--run"],
  validation: ["Waiting", "tag--wait"],
};

async function loadHistory() {
  const tbody = document.getElementById("history");
  try {
    const { runs } = await api("/api/history?limit=60");
    tbody.innerHTML = "";

    if (!runs.length) {
      tbody.innerHTML =
        '<tr><td colspan="10" class="empty">No runs yet. '
        + 'Start an analysis to begin.</td></tr>';
      return;
    }

    runs.forEach((r) => {
      const [label, cls] = STATUS[r.status] || [r.status, "tag--run"];
      const tr = document.createElement("tr");
      tr.className = "log__line";
      // The profile name is free text: injected as-is into the HTML, a name
      // containing "<" used to break the row.
      tr.innerHTML = `
        <td>${when(r.started_at)}</td>
        <td data-cell-name></td>
        <td>${r.kind === "analysis" ? "Analysis" : "Transfer"}</td>
        <td class="num moved">${count(r.moved)}</td>
        <td class="num sent">${count(r.transferred)}</td>
        <td class="num ${r.deleted ? "cut" : ""}">${count(r.deleted)}</td>
        <td class="num">${r.kind === "analysis" ? "—" : bytes(r.bytes)}</td>
        <td class="num">${duration(r.duration_s)}</td>
        <td><span class="tag ${cls}">${label}</span></td>`;
      tr.querySelector("[data-cell-name]").textContent = r.profile_name;

      const actions = document.createElement("td");
      actions.className = "log__actions";

      // Rerun: an interrupted analysis cannot resume, rclone keeps no
      // checkpoint. The button therefore restarts from scratch.
      if (known[r.profile_id]) {
        const retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.className = "iconbtn";
        retryBtn.title = r.status === "cancelled"
          ? "Rerun this analysis from the start"
          : "Rerun";
        retryBtn.textContent = "↻";
        retryBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          rerunRun(r);
        });
        actions.appendChild(retryBtn);
      }

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "iconbtn iconbtn--cut";
      delBtn.title = "Delete this row";
      delBtn.textContent = "✕";
      delBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await api(`/api/history/${r.id}`, { method: "DELETE" });
          tr.remove();
          if (!document.querySelectorAll("#history tr").length) loadHistory();
        } catch (err) {
          toast(err.message);
        }
      });
      actions.appendChild(delBtn);

      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty">${err.message}</td></tr>`;
  }
}

async function rerunRun(r) {
  const p = known[r.profile_id];
  if (!p) { toast("This profile no longer exists."); return; }
  if (busyProfiles.has(r.profile_id)) {
    toast("An operation is already running on this profile.");
    return;
  }
  const action = r.kind === "analysis" ? "analyse" : "run";
  try {
    await api(`/api/profiles/${r.profile_id}/${action}`, { method: "POST" });
    await refresh();
    await loadHistory();
    cards.get(r.profile_id)?.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (err) {
    toast(err.message);
  }
}

async function clearHistory() {
  if (!confirm("Clear the whole run history?\n\n"
             + "Backups and files are not touched.")) return;
  try {
    const { deleted } = await api("/api/history", { method: "DELETE" });
    toast(`${count(deleted)} row${deleted > 1 ? "s" : ""} deleted.`);
    await loadHistory();
  } catch (err) {
    toast(err.message);
  }
}

/* -- polling loop ------------------------------------------------------------ */

async function refresh() {
  try {
    const data = await api("/api/state");
    const order = data.order || Object.keys(data.profiles);

    // Remove the cards of profiles that no longer exist
    [...cards.keys()].forEach((id) => {
      if (!order.includes(id)) { cards.get(id).remove(); cards.delete(id); delete known[id]; }
    });

    order.forEach((id) => data.profiles[id] && renderCard(data.profiles[id]));

    // Put the cards back in store order
    const host = document.getElementById("profiles");
    order.forEach((id) => { const c = cards.get(id); if (c) host.appendChild(c); });

    document.getElementById("blank").hidden = order.length > 0;
    const spared = data.totals?.moved || 0;
    document.getElementById("meter-spared").textContent = count(spared);
    document.querySelector(".meter").classList.toggle("is-zero", spared === 0);
  } catch (err) {
    toast(err.message);
  }
}

async function checkEngine() {
  const el = document.getElementById("engine");
  const text = el.querySelector(".engine__text");
  try {
    const info = await api("/api/engine");
    if (info.ok) {
      el.className = "engine is-ok";
      text.textContent = `rclone ${info.version}`;
    } else {
      el.className = "engine is-down";
      text.textContent = info.error;
    }
  } catch (err) {
    el.className = "engine is-down";
    text.textContent = "Engine unreachable";
  }
}

// The clock advances on its own, twice a second, independently of server
// responses. That is what visually separates a machine that is computing
// from a frozen application.
setInterval(() => {
  for (const [id, t] of Object.entries(ticking)) {
    const card = cards.get(id);
    if (!card) { delete ticking[id]; continue; }
    const clock = card.querySelector("[data-live-clock]");
    if (clock) clock.textContent = duration(t.base + (Date.now() - t.at) / 1000);
  }
}, 500);

function anyRunning() {
  return Object.keys(ticking).length > 0;
}

let historyTick = 0;

async function tick() {
  await refresh();
  historyTick += 1;
  if (historyTick % 5 === 0 || !anyRunning()) await loadHistory();
  setTimeout(tick, anyRunning() ? 1200 : 4000);
}

/* -- startup ------------------------------------------------------------------ */

document.getElementById("drawer-close").addEventListener("click", closeDrawer);
document.getElementById("drawer").addEventListener("click", (e) => {
  if (e.target.id === "drawer") closeDrawer();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
});
document.getElementById("refresh-history").addEventListener("click", loadHistory);
document.getElementById("clear-history").addEventListener("click", clearHistory);

checkEngine();
setInterval(checkEngine, 30000);
tick();
loadHistory();

/* ==========================================================================
   Wizard: pick sources, a destination, settings
   ========================================================================== */

const wz = {
  step: 1,
  picked: [],        // selected local paths
  remoteBase: "",    // receiving pCloud folder
  editing: null,     // id of the profile being edited, else null
};

const $ = (id) => document.getElementById(id);

function wzOpen(profile = null) {
  wz.editing = profile ? profile.id : null;
  wz.picked = profile ? [profile.local] : [];
  wz.remoteBase = "";
  wz.step = profile ? 3 : 1;

  $("wz-title").textContent = profile ? "Edit backup" : "Add a backup";
  $("wz-msg").textContent = "";
  $("wizard").hidden = false;

  if (profile) {
    renderForms([{ local: profile.local, remote: profile.remote, profile }]);
  } else {
    loadLocal("");
  }
  paintStep();
}

function wzClose() {
  $("wizard").hidden = true;
  wz.picked = [];
  wz.editing = null;
}

function paintStep() {
  document.querySelectorAll("[data-wstep]").forEach((li) => {
    const n = Number(li.dataset.wstep);
    li.classList.toggle("is-active", n === wz.step);
    li.classList.toggle("is-done", n < wz.step);
  });
  document.querySelectorAll("[data-wpane]").forEach((pane) => {
    pane.hidden = Number(pane.dataset.wpane) !== wz.step;
  });
  $("wz-steps").hidden = !!wz.editing;

  $("wz-back").hidden = wz.step === 1 || !!wz.editing;
  const next = $("wz-next");
  next.textContent = wz.step === 3 ? (wz.editing ? "Save" : "Create") : "Continue";
  next.disabled = wz.step === 1 ? wz.picked.length === 0
                : wz.step === 2 ? !wz.remoteBase
                : false;
}

/* -- local browser ---------------------------------------------------------- */

function crumbsLocal(path, parent) {
  const nav = $("wz-local-crumbs");
  nav.innerHTML = "";

  const root = document.createElement("button");
  root.type = "button";
  root.textContent = "Drives";
  root.onclick = () => loadLocal("");
  nav.appendChild(root);

  if (path) {
    const sep = document.createElement("span");
    sep.textContent = "›";
    const here = document.createElement("span");
    here.className = "crumbs__here";
    here.textContent = path;
    nav.append(sep, here);
  }

  if (parent !== null && parent !== undefined && path) {
    const up = document.createElement("button");
    up.type = "button";
    up.textContent = "↑ go up";
    up.onclick = () => loadLocal(parent);
    nav.appendChild(up);
  }
}

async function loadLocal(path) {
  const list = $("wz-local-list");
  list.innerHTML = '<li class="picker__load">Reading…</li>';
  try {
    const data = await api(`/api/browse/local?path=${encodeURIComponent(path)}`);
    crumbsLocal(data.path, data.parent);
    list.innerHTML = "";

    if (!data.entries.length) {
      list.innerHTML = '<li class="picker__empty">No sub-folder here. You can tick the current folder above.</li>';
    }

    data.entries.forEach((e) => {
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "prow";
      if (wz.picked.includes(e.path)) row.classList.add("prow--picked");

      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = wz.picked.includes(e.path);
      box.onchange = () => togglePick(e.path, box.checked, row);

      const name = document.createElement("span");
      name.className = "prow__name";
      name.textContent = e.label;
      name.onclick = () => { box.checked = !box.checked; box.onchange(); };

      row.append(box, name);

      if (e.total) {
        const meta = document.createElement("span");
        meta.className = "prow__meta";
        meta.textContent = `${bytes(e.total - e.free)} / ${bytes(e.total)}`;
        row.appendChild(meta);
      }

      const open = document.createElement("button");
      open.type = "button";
      open.className = "prow__open";
      open.textContent = "open";
      open.onclick = () => loadLocal(e.path);
      row.appendChild(open);

      li.appendChild(row);
      list.appendChild(li);
    });

    // Allow ticking the current folder itself
    if (data.path) {
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "prow";
      if (wz.picked.includes(data.path)) row.classList.add("prow--picked");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = wz.picked.includes(data.path);
      box.onchange = () => togglePick(data.path, box.checked, row);
      const name = document.createElement("span");
      name.className = "prow__name";
      name.innerHTML = `<em>This whole folder</em> — ${data.path}`;
      name.onclick = () => { box.checked = !box.checked; box.onchange(); };
      row.append(box, name);
      li.appendChild(row);
      list.insertBefore(li, list.firstChild);
    }
  } catch (err) {
    list.innerHTML = `<li class="picker__empty">${err.message}</li>`;
  }
}

function togglePick(path, on, row) {
  wz.picked = on ? [...new Set([...wz.picked, path])] : wz.picked.filter((p) => p !== path);
  if (row) row.classList.toggle("prow--picked", on);
  renderPicked();
  paintStep();
}

function renderPicked() {
  const box = $("wz-chosen");
  const list = $("wz-chosen-list");
  list.innerHTML = "";
  wz.picked.forEach((p) => {
    const li = document.createElement("li");
    li.textContent = p;
    list.appendChild(li);
  });
  box.hidden = wz.picked.length === 0;
}

/* -- remote browser ----------------------------------------------------------- */

async function loadRemote(remote, path) {
  const list = $("wz-remote-list");
  list.innerHTML = '<li class="picker__load">Reading pCloud…</li>';
  try {
    const q = `remote=${encodeURIComponent(remote || "")}&path=${encodeURIComponent(path || "")}`;
    const data = await api(`/api/browse/remote?${q}`);

    const nav = $("wz-remote-crumbs");
    nav.innerHTML = "";
    const root = document.createElement("button");
    root.type = "button";
    root.textContent = "Remotes";
    root.onclick = () => loadRemote("", "");
    nav.appendChild(root);

    if (data.remote) {
      const sep = document.createElement("span");
      sep.textContent = "›";
      const here = document.createElement("span");
      here.className = "crumbs__here";
      here.textContent = data.remote + data.path;
      nav.append(sep, here);
      if (data.parent !== null) {
        const up = document.createElement("button");
        up.type = "button";
        up.textContent = "↑ go up";
        up.onclick = () => loadRemote(data.remote, data.parent);
        nav.appendChild(up);
      }
    }

    list.innerHTML = "";

    if (data.remote) {
      const full = data.remote + data.path;
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "prow";
      if (wz.remoteBase === full) row.classList.add("prow--picked");
      const box = document.createElement("input");
      box.type = "radio";
      box.name = "wz-remote";
      box.checked = wz.remoteBase === full;
      box.onchange = () => setRemoteBase(full);
      const name = document.createElement("span");
      name.className = "prow__name";
      name.innerHTML = `<em>Send here</em> — ${full}`;
      name.onclick = () => setRemoteBase(full);
      row.append(box, name);
      li.appendChild(row);
      list.appendChild(li);
    }

    if (!data.entries.length && !data.remote) {
      list.innerHTML = '<li class="picker__empty">No rclone remote configured. Run <code>rclone config</code>.</li>';
    }

    data.entries.forEach((e) => {
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "prow";
      const name = document.createElement("span");
      name.className = "prow__name";
      name.textContent = e.label;
      const open = document.createElement("button");
      open.type = "button";
      open.className = "prow__open";
      open.textContent = "open";
      const go = () => e.kind === "remote"
        ? loadRemote(e.path, "")
        : loadRemote(data.remote, e.path);
      name.onclick = go;
      open.onclick = go;
      row.append(name, open);
      li.appendChild(row);
      list.appendChild(li);
    });
  } catch (err) {
    list.innerHTML = `<li class="picker__empty">${err.message}</li>`;
  }
}

async function setRemoteBase(full) {
  wz.remoteBase = full;
  const list = $("wz-preview-list");
  list.innerHTML = "";
  for (const local of wz.picked) {
    const q = `local=${encodeURIComponent(local)}&base=${encodeURIComponent(full)}`;
    const { remote } = await api(`/api/suggest-remote?${q}`);
    const li = document.createElement("li");
    li.className = "is-remote";
    li.textContent = remote;
    list.appendChild(li);
  }
  $("wz-preview").hidden = wz.picked.length === 0;
  document.querySelectorAll("#wz-remote-list .prow").forEach((r) => {
    r.classList.toggle("prow--picked", r.querySelector("input")?.checked);
  });
  paintStep();
}

/* -- forms ---------------------------------------------------------------------- */

function baseName(path) {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function renderForms(items) {
  const box = $("wz-forms");
  box.innerHTML = "";
  items.forEach((item) => {
    const node = $("tpl-wz-form").content.firstElementChild.cloneNode(true);
    node.querySelector("[data-wf-path]").textContent = item.local;
    node.dataset.local = item.local;

    const p = item.profile;
    node.querySelector("[data-wf-name]").value = p ? p.name : baseName(item.local);
    node.querySelector("[data-wf-remote]").value = item.remote || "";
    const sched = node.querySelector("[data-wf-schedule]");
    const schedule = p ? p.schedule : "";
    // A schedule missing from the list (config.yaml accepts any HH:MM)
    // would leave the select on "Manual": saving the edit would then
    // silently erase the scheduling.
    if (schedule && ![...sched.options].some((o) => o.value === schedule)) {
      const opt = document.createElement("option");
      opt.value = schedule;
      opt.textContent = `Daily at ${schedule}`;
      sched.appendChild(opt);
    }
    sched.value = schedule;
    node.querySelector("[data-wf-max]").value = p ? p.max_deletes : 500;
    node.querySelector("[data-wf-auto]").checked = p ? p.auto : false;

    box.appendChild(node);
  });
}

async function buildForms() {
  const items = [];
  for (const local of wz.picked) {
    const q = `local=${encodeURIComponent(local)}&base=${encodeURIComponent(wz.remoteBase)}`;
    const { remote } = await api(`/api/suggest-remote?${q}`);
    items.push({ local, remote });
  }
  renderForms(items);
}

function collectForms() {
  return [...document.querySelectorAll("#wz-forms .wform")].map((f) => ({
    local: f.dataset.local,
    name: f.querySelector("[data-wf-name]").value.trim(),
    remote: f.querySelector("[data-wf-remote]").value.trim(),
    schedule: f.querySelector("[data-wf-schedule]").value,
    max_deletes: Number(f.querySelector("[data-wf-max]").value),
    auto: f.querySelector("[data-wf-auto]").checked,
  }));
}

/* -- wizard navigation ------------------------------------------------------------ */

async function wzNext() {
  $("wz-msg").textContent = "";

  if (wz.step === 1) { wz.step = 2; loadRemote("", ""); paintStep(); return; }
  if (wz.step === 2) {
    // The step only advances once the forms are built: a failed suggestion
    // used to leave the wizard on step 3 with no content.
    try {
      await buildForms();
    } catch (err) {
      $("wz-msg").textContent = err.message;
      return;
    }
    wz.step = 3;
    paintStep();
    return;
  }

  const entries = collectForms();
  const btn = $("wz-next");
  btn.disabled = true;
  try {
    if (wz.editing) {
      await api(`/api/profiles/${wz.editing}`, {
        method: "PATCH",
        body: JSON.stringify(entries[0]),
      });
    } else {
      await api("/api/profiles", {
        method: "POST",
        body: JSON.stringify({ profiles: entries }),
      });
    }
    wzClose();
    await refresh();
    await loadHistory();
  } catch (err) {
    $("wz-msg").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

function wzBack() {
  if (wz.step > 1) wz.step -= 1;
  if (wz.step === 1) loadLocal("");
  paintStep();
}

/* -- edit and delete from a card ---------------------------------------------------- */

let known = {};
const busyProfiles = new Set();

async function editProfile(id) {
  const p = known[id];
  if (p) wzOpen(p);
}

async function deleteProfile(id) {
  const p = known[id];
  const label = p ? p.name : id;
  if (!confirm(`Delete the backup “${label}”?\n\nFiles already on pCloud are not touched.`)) return;
  try {
    await api(`/api/profiles/${id}`, { method: "DELETE" });
    cards.get(id)?.remove();
    cards.delete(id);
    await refresh();
  } catch (err) {
    toast(err.message);
  }
}

/* -- wiring --------------------------------------------------------------------------- */

$("add-profile").addEventListener("click", () => wzOpen());
$("add-first").addEventListener("click", () => wzOpen());
$("wz-next").addEventListener("click", wzNext);
$("wz-back").addEventListener("click", wzBack);
document.querySelectorAll("[data-wz-close]").forEach((b) =>
  b.addEventListener("click", wzClose)
);
$("wizard").addEventListener("click", (e) => { if (e.target.id === "wizard") wzClose(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("wizard").hidden) wzClose();
});
