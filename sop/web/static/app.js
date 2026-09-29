"use strict";

// Test UI for the SOP harness. Everything from the server (including model output) is rendered
// with textContent, never as HTML.

const $ = (selector) => document.querySelector(selector);
const app = { sessionId: null, busy: false, playing: false, passcode: null, llmReady: false, passcodeResolver: null };

const EVENT_CLASS = {
  phase_transition: "k-transition",
  verified: "k-good",
  case_selected: "k-good",
  grounded_answer: "k-good",
  email_sent: "k-good",
  tool_denied: "k-bad",
  verification_failed: "k-bad",
  verification_locked: "k-bad",
  injection_ignored: "k-bad",
  guard_blocked: "k-bad",
  nlu_failed: "k-bad",
  reply_failed: "k-bad",
  fallback_reply: "k-bad",
  memory_saved: "k-mem",
  memory_used: "k-mem",
  out_of_scope: "k-warn",
  unanswerable: "k-warn",
  human_offered: "k-warn",
  handoff_created: "k-warn",
  deadline_caveat: "k-warn",
  emotion: "k-warn",
  gate_pushback: "k-warn",
  third_party_caller: "k-warn",
  case_ambiguous: "k-warn",
  case_no_match: "k-warn",
  factor_declined: "k-warn",
  factor_unusable: "k-warn",
  email_skipped: "k-warn",
  email_not_sent: "k-warn",
  account_located: "k-transition",
  representative_on_file: "k-good",
  consent_approved: "k-good",
  consent_requested: "k-warn",
  consent_checked: "k-warn",
  consent_timeout: "k-bad",
  representative_not_on_file: "k-bad",
  representative_changed: "k-bad",
  email_deferred: "k-warn",
  caller_ended: "k-warn",
};

// ---------------------------------------------------------------- DOM helpers

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function heading(card, title, extra) {
  const h = el("h3", null, title);
  if (extra) h.appendChild(extra);
  card.appendChild(h);
}

function pill(text, tone) {
  return el("span", `pill ${tone}`, text);
}

function tags(values, emptyText = "none") {
  const wrap = el("div", "tags");
  if (!values || values.length === 0) {
    wrap.appendChild(el("span", "empty", emptyText));
    return wrap;
  }
  values.forEach((value) => wrap.appendChild(el("span", "tag", value)));
  return wrap;
}

function kv(rows) {
  const dl = el("dl", "kv");
  rows.forEach(([key, value]) => {
    dl.appendChild(el("dt", null, key));
    const dd = el("dd");
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    dl.appendChild(dd);
  });
  return dl;
}

function meter(value, max) {
  const wrap = el("div", "meter");
  const bar = el("i");
  bar.style.width = `${Math.min(100, (value / Math.max(max, 1)) * 100)}%`;
  wrap.appendChild(bar);
  return wrap;
}

function progress(text, value, max) {
  const box = el("div");
  box.appendChild(el("div", null, text));
  box.appendChild(meter(value, max));
  return box;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- passcode and API

function loadPasscode() {
  try {
    return sessionStorage.getItem("demo-passcode");
  } catch {
    return null;
  }
}

function savePasscode(value) {
  try {
    sessionStorage.setItem("demo-passcode", value);
  } catch {
    /* storage unavailable: the passcode lasts for this page only */
  }
}

function askPasscode(wrong) {
  $("#passcode-overlay").hidden = false;
  $("#passcode-error").hidden = !wrong;
  $("#passcode-input").value = "";
  $("#passcode-input").focus();
  return new Promise((resolve) => {
    app.passcodeResolver = resolve;
  });
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (app.passcode) headers["X-Demo-Passcode"] = app.passcode;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    app.passcode = await askPasscode(Boolean(app.passcode));
    savePasscode(app.passcode);
    return api(path, options);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

// ---------------------------------------------------------------- chat

function scrollChat() {
  const chat = $("#chat");
  chat.scrollTop = chat.scrollHeight;
}

function addMessage(role, text, metaText) {
  const row = el("div", `msg ${role}`);
  row.appendChild(el("div", "bubble", text));
  if (metaText) row.appendChild(el("div", "meta", metaText));
  $("#chat").appendChild(row);
  scrollChat();
  return row;
}

function showTyping() {
  const row = el("div", "msg assistant typing");
  const bubble = el("div", "bubble");
  const dots = el("span", "dots");
  for (let i = 0; i < 3; i += 1) dots.appendChild(el("span", null, "●"));
  bubble.appendChild(dots);
  row.appendChild(bubble);
  $("#chat").appendChild(row);
  scrollChat();
  return row;
}

function phaseTrail(state) {
  const moves = state.phase_history.filter((move) => move.turn === state.turn);
  if (moves.length === 0) return state.phase;
  return [moves[0].from, ...moves.map((move) => move.to)].join(" → ");
}

function setBusy(busy) {
  app.busy = busy;
  const locked = busy || app.playing || !app.llmReady;
  $("#send").disabled = locked;
  $("#input").disabled = locked;
  $("#new-chat").disabled = busy || app.playing;
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.disabled = (busy || app.playing || !app.llmReady) && !chip.classList.contains("playing");
  });
}

async function newChat(consentScenario) {
  const data = await api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ consent_scenario: consentScenario || null }),
  });
  app.sessionId = data.session_id;
  clear($("#chat"));
  data.transcript.forEach((message) => addMessage(message.role, message.content));
  render(data.state);
}

async function send(text) {
  if (!text.trim() || app.busy) return;
  if (!app.sessionId) await newChat();
  setBusy(true);
  addMessage("user", text);
  const typing = showTyping();
  try {
    const data = await api(`/api/sessions/${app.sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    typing.remove();
    addMessage("assistant", data.reply, phaseTrail(data.state));
    render(data.state);
  } catch (error) {
    typing.remove();
    addMessage("assistant error", error.message, "request failed");
  } finally {
    setBusy(false);
  }
}

async function play(scenario, chip) {
  if (app.playing || app.busy) return;
  app.playing = true;
  chip.classList.add("playing");
  setBusy(false);
  try {
    await newChat(scenario.consent_scenario);
    for (const text of scenario.messages) {
      await sleep(400);
      await send(text);
    }
  } catch (error) {
    addMessage("assistant error", error.message, "request failed");
  } finally {
    app.playing = false;
    chip.classList.remove("playing");
    setBusy(false);
  }
}

function submitInput() {
  if (app.playing || app.busy) return;
  const input = $("#input");
  const text = input.value;
  if (!text.trim()) return;
  input.value = "";
  autosize();
  send(text);
}

function autosize() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

// ---------------------------------------------------------------- operator view

function render(state) {
  renderPhase(state);
  renderIdentity(state);
  renderMemory(state);
  renderCase(state);
  renderPolicy(state);
  renderEffects(state);
  renderEvents(state);
}

function renderPhase(state) {
  const card = clear($("#phase-card"));
  const spec = state.phases[state.phase];
  const terminalPill = spec.terminal ? pill(state.phase, state.phase === "ENDED" ? "ok" : "warn") : null;
  heading(card, "SOP progress", terminalPill);
  const visited = new Set(state.phase_history.map((move) => move.from));
  const stepper = el("div", "stepper");
  state.workflow.forEach((name) => {
    let className = "step";
    if (name === state.phase) className += " current";
    else if (visited.has(name)) className += " done";
    stepper.appendChild(el("div", className, name));
  });
  card.appendChild(stepper);
  card.appendChild(
    kv([
      ["Goal", spec.goal],
      ["Model freedom", `${spec.autonomy}: ${spec.autonomy_note}`],
      ["Tools allowed", tags(spec.tools)],
      ["Model can see", tags(spec.reply_context)],
      ["Exit guard", spec.exit_guard],
    ]),
  );
}

function renderIdentity(state) {
  const identity = state.identity;
  const card = clear($("#identity-card"));
  const tone = identity.status === "verified" ? "ok" : identity.status === "locked" ? "bad" : "warn";
  heading(card, "Identity gate", pill(identity.status, tone));
  const rows = [];
  if (identity.status === "verified") {
    rows.push(["Verified as", identity.verified_as]);
    rows.push(["Via", tags(identity.verified_via)]);
  } else {
    rows.push([
      "Progress",
      progress(`${identity.provided.length} of ${identity.required} required details`, identity.provided.length, identity.required),
    ]);
    rows.push(["Provided", tags(identity.provided)]);
  }
  rows.push(["Declined", tags(identity.declined)]);
  rows.push(["Failed attempts", `${identity.failed_attempts} of ${identity.max_attempts}`]);
  rows.push(["Policy number", identity.policy_number_hint ? `${identity.policy_number_hint} (lookup hint, not a factor)` : null]);
  rows.push(["Caller role", state.caller_role]);
  if (state.caller_role === "representative") {
    const consent = state.consent;
    rows.push(["Representative", `${consent.representative || "name not given"} (${consent.relationship || "relationship not given"})`]);
    rows.push(["Account located", consent.account_located ? "yes (no access yet)" : "no"]);
    rows.push([
      "Policyholder consent",
      `${consent.status || "not requested"} · check ${consent.checks} of ${consent.max_checks} · simulated: ${consent.scenario}`,
    ]);
  }
  card.appendChild(kv(rows));
}

function renderMemory(state) {
  const card = clear($("#memory-card"));
  heading(card, "Memory", el("span", "hint", "saved whenever it is said"));
  if (state.memory.length === 0) {
    card.appendChild(el("p", "empty", "Nothing remembered yet."));
    return;
  }
  const list = el("ul", "memory");
  state.memory.forEach((item) => {
    const li = el("li");
    const top = el("div", "mem-top");
    top.appendChild(el("span", "tag", item.kind));
    top.appendChild(el("span", null, item.value));
    li.appendChild(top);
    const flow = item.used_in.length
      ? `said in ${item.phase_said} → used in ${item.used_in.join(", ")}`
      : `said in ${item.phase_said}`;
    li.appendChild(el("div", "mem-flow", flow));
    list.appendChild(li);
  });
  card.appendChild(list);
}

function renderCase(state) {
  const card = clear($("#case-card"));
  heading(card, "Claim");
  const active = state.active_case;
  const discussed = Object.entries(state.discussed).map(
    ([caseId, topics]) => `${caseId}: ${[...new Set(topics)].join(", ") || "selected"}`,
  );
  card.appendChild(
    kv([
      ["Active claim", active ? `${active.case_id} · ${active.case_type} · filed ${active.filed_date} · ${active.status}` : null],
      ["Candidates", tags(state.candidates)],
      ["Caller intent", state.intent],
      ["Claim hints", state.case_hints],
      ["Discussed", discussed.length ? tags(discussed) : null],
    ]),
  );
}

function renderPolicy(state) {
  const card = clear($("#policy-card"));
  heading(card, "Conversation policy");
  const { counters, limits } = state;
  card.appendChild(
    kv([
      [
        "Out of scope",
        progress(`${counters.oos_consecutive} in a row (human offered at ${limits.oos_threshold})`, counters.oos_consecutive, limits.oos_threshold),
      ],
      [
        "Gate pushback",
        progress(`${counters.gate_pushbacks} (human offered at ${limits.max_gate_pushbacks})`, counters.gate_pushbacks, limits.max_gate_pushbacks),
      ],
      ["Sentiment", state.sentiment],
      ["Waiting for", state.pending_question],
    ]),
  );
}

function summaryText(summary) {
  const lines = [];
  if (summary.claims.length === 0) lines.push("• No claims on file.");
  summary.claims.forEach((claim) => {
    lines.push(`• ${claim.status}`);
    if (claim.topics.length) lines.push(`  Topics: ${claim.topics.join(", ")}`);
  });
  if (summary.caller_notes && summary.caller_notes.length) {
    lines.push("", "What you told us:");
    summary.caller_notes.forEach((note) => lines.push(`• ${note}`));
  }
  lines.push("", "Next steps:");
  summary.next_steps.forEach((step) => lines.push(`• ${step}`));
  return lines.join("\n");
}

function renderEffects(state) {
  const card = clear($("#effects-card"));
  heading(card, "Side effects", el("span", "hint", "simulated in this demo"));
  const email = state.email;
  let status = "not offered yet";
  if (email.message) status = "sent, with explicit consent";
  else if (email.decision === "skip") status = "skipped by the caller";
  else if (email.decision === "not_sent") status = "not sent (no clear consent)";
  else if (email.offered) status = "offered; waiting for send or skip";
  const handoff = state.handoff;
  card.appendChild(
    kv([
      ["Email summary", status],
      ["Recipient", email.to],
      ["Human handoff", handoff ? `${handoff.ticket_id}: ${handoff.reason}` : null],
    ]),
  );
  if (email.message) {
    card.appendChild(el("div", "sublabel", `Outbox · ${email.message.id} · ${email.message.subject}`));
    card.appendChild(el("pre", "preview", email.message.body));
  } else if (email.offered && email.summary) {
    card.appendChild(el("div", "sublabel", "Draft summary (not sent)"));
    card.appendChild(el("pre", "preview", summaryText(email.summary)));
  }
}

function renderEvents(state) {
  const card = clear($("#events-card"));
  heading(card, "Audit log", el("span", "hint", "newest first"));
  const list = el("ol", "events");
  [...state.events].reverse().forEach((event) => {
    const li = el("li", "event");
    li.appendChild(el("span", "turn", `T${event.turn}`));
    const body = el("div");
    body.appendChild(el("span", `kind ${EVENT_CLASS[event.kind] || "k-info"}`, event.kind));
    body.appendChild(el("span", null, event.detail));
    li.appendChild(body);
    list.appendChild(li);
  });
  card.appendChild(list);
}

// ---------------------------------------------------------------- startup

function showBanner(text) {
  const banner = $("#banner");
  banner.textContent = text;
  banner.hidden = false;
}

async function init() {
  app.passcode = loadPasscode();
  $("#passcode-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("#passcode-input").value.trim();
    if (!value) return;
    $("#passcode-overlay").hidden = true;
    if (app.passcodeResolver) app.passcodeResolver(value);
  });

  const config = await api("/api/config");
  app.llmReady = config.llm_ready;
  $("#model-badge").textContent = config.llm_ready ? `${config.provider} · ${config.model}` : "no model configured";
  $("#date-badge").textContent = `as of ${config.as_of_date}`;
  if (!config.llm_ready) {
    showBanner("No model configured: set LLM_API_KEY (or OPENAI_API_KEY / ANTHROPIC_API_KEY) and restart the server.");
  }

  const chips = $("#scenarios");
  config.scenarios.forEach((scenario) => {
    const chip = el("button", "chip", scenario.title);
    chip.type = "button";
    chip.title = scenario.description;
    chip.addEventListener("click", () => play(scenario, chip));
    chips.appendChild(chip);
  });

  $("#new-chat").addEventListener("click", () => {
    if (!app.playing && !app.busy) newChat().catch((error) => showBanner(error.message));
  });
  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    submitInput();
  });
  $("#input").addEventListener("keydown", (event) => {
    // isComposing: don't send while an IME (e.g. Chinese input) is still composing.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitInput();
    }
  });
  $("#input").addEventListener("input", autosize);

  await newChat();
  setBusy(false);
  $("#input").focus();
}

init().catch((error) => showBanner(error.message));
