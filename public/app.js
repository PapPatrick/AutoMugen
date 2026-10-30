"use strict";
const $ = (id) => document.getElementById(id);
let state = null;
let selected = new Set();
let rosterKey = null;
let busy = false;
let offset = 0;
let lastSnapshot = 0;
let errorSource = "";
const names = () => new Map((state?.tournament?.fighters || []).map((f) => [f.id, f.name]));

function showError(message, source = "action") {
  $("error").textContent = message;
  $("error").hidden = !message;
  errorSource = message ? source : "";
}

function matchData() {
  return {
    tournament_id: state?.tournament?.id, match_id: state?.current_match?.id,
    round_number: (state?.current_match?.wins || []).reduce((a, b) => a + b, 0) + 1,
  };
}

async function action(name, data = {}) {
  if (busy) return;
  busy = true;
  renderControls();
  showError("");
  try {
    const response = await fetch(`/api/action/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-AutoMugen-Token": state?.token || "" },
      body: JSON.stringify(data),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Server returned ${response.status}`);
    if (result.authorize_url) { location.assign(result.authorize_url); return; }
    accept(result);
  } catch (error) {
    showError(error.message);
  } finally {
    busy = false;
    renderControls();
  }
}

function accept(next) {
  lastSnapshot = 0;
  Broadcast.requireCreditBetting(next);
  state = next;
  Broadcast.revision = next.portrait_revision || "";
  offset = next.server_time * 1000 - Date.now();
  lastSnapshot = Date.now();
  $("connection").textContent = "LOCAL SERVER ONLINE";
  $("connection").className = "pill online";
  if (document.activeElement !== $("game-path")) $("game-path").value = state.game_path;
  $("roster-count").textContent = state.roster.fighters.length;
  const key = JSON.stringify([state.game_path, state.roster.fighters]);
  if (rosterKey !== key) {
    const previous = rosterKey;
    const previousIds = new Set(previous ? JSON.parse(previous)[1].map((f) => f.id) : []);
    selected = new Set(state.roster.fighters.filter((f) => !previous || selected.has(f.id) || !previousIds.has(f.id)).map((f) => f.id));
    rosterKey = key;
    renderRoster();
    const oldStage = $("stage").value;
    $("stage").replaceChildren(new Option("Random stage (each launch)", "random"), ...state.roster.stages.map((s) => new Option(s.name, s.id)));
    if (oldStage === "random" || state.roster.stages.some((s) => s.id === oldStage)) $("stage").value = oldStage;
  } else {
    // Stage definitions can change without changes to the fighter pool.
    const stageKey = JSON.stringify(state.roster.stages);
    if ($("stage").dataset.key !== stageKey) {
      const oldStage = $("stage").value;
      $("stage").replaceChildren(new Option("Random stage (each launch)", "random"), ...state.roster.stages.map((s) => new Option(s.name, s.id)));
      if (oldStage === "random" || state.roster.stages.some((s) => s.id === oldStage)) $("stage").value = oldStage;
    }
  }
  $("stage").dataset.key = JSON.stringify(state.roster.stages);
  const warnings = [...(state.discovery_error ? [state.discovery_error] : []), ...state.roster.warnings];
  $("warning-count").textContent = `Discovery notes (${warnings.length})`;
  $("warnings").hidden = !warnings.length;
  $("warning-list").replaceChildren(...warnings.map((line) => {
    const li = document.createElement("li"); li.textContent = line; return li;
  }));
  const report = state.portrait_update;
  $("portrait-update-status").textContent = report ? `${report.message} (${report.checked}/${report.total} checked, ${report.ready} ready, ${Object.keys(report.failures).length} failed)` :
    "Checks game data and creates missing or changed character images.";
  $("portrait-update-progress").hidden = !report;
  $("portrait-update-progress").max = report?.total || 1;
  $("portrait-update-progress").value = report?.checked || 0;
  if (state.discovery_error) showError(state.discovery_error, "discovery");
  else if (["connection", "discovery"].includes(errorSource)) showError("");
  renderTournament();
  $("twitch-badge").textContent = state.twitch.connected ? `AUTHORIZED / ${state.twitch.channel}` : "TWITCH NOT AUTHORIZED";
  $("twitch-message").textContent = state.twitch.message;
  const prediction = state.twitch.prediction;
  const chat = state.chat;
  $("chat-status").textContent = `${chat.connected ? `CONNECTED TO #${chat.channel} AS BOT ${chat.bot}` : chat.message} · ${chat.counts.map((n, i) => `#${i + 1}: ${n} bets / ${chat.pools[i].toLocaleString()} credits`).join(" · ")} · Pool ${chat.pool.toLocaleString()} credits.${Number.isInteger(chat.commands_received) ? ` Commands received ${chat.commands_received} / rejected ${chat.commands_rejected} / replies sent ${chat.replies_sent}.` : ""} ${chat.connected ? chat.message : ""}`;
  Broadcast.choices(prediction, chat, state.tournament, $("betting-choices"));
  $("chat-leaderboard").textContent = chat.leaderboard.length ?
    `Credit leaders: ${chat.leaderboard.map((row) => `${row.login} ${row.credits.toLocaleString()}`).join(" · ")}` :
    "Credit leaderboard: no bettors yet.";
  const legacy = state.twitch.legacy_prediction;
  $("prediction-status").textContent = state.twitch.legacy_pending && legacy ?
    `OLD CHANNEL POINTS PREDICTION / ${legacy.status} / ${legacy.title} / ${legacy.id || "creation outcome unknown"}. Handle manually on Twitch before acknowledging.` : prediction ?
    `VIRTUAL CREDITS / ${prediction.status} / ${prediction.match_id.toUpperCase()} / ${prediction.title}` :
    "No betting window. Before semifinals: 60 seconds per match. At semifinals: 3 minutes to pick the tournament champion. Local preview never accepts bets.";
  $("legacy-ack").hidden = !state.twitch.legacy_pending;
  renderControls();
}

function renderRoster() {
  const query = $("search").value.toLocaleLowerCase();
  const fighters = (state?.roster.fighters || []).filter((f) => `${f.id} ${f.name}`.toLocaleLowerCase().includes(query));
  $("roster").replaceChildren(...fighters.map((fighter) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox"; input.checked = selected.has(fighter.id);
    input.addEventListener("change", () => {
      if (input.checked) selected.add(fighter.id); else selected.delete(fighter.id);
      updateSelection();
    });
    const name = document.createElement("span"); name.textContent = fighter.name;
    const entry = document.createElement("small"); entry.textContent = fighter.id;
    name.append(entry); label.append(input, name); return label;
  }));
  updateSelection();
}

function updateSelection() {
  $("selected-count").textContent = `/ ${selected.size} selected`;
}

function renderTournament() {
  const t = state.tournament;
  const match = state.current_match;
  const lookup = names();
  const completed = t?.rounds.flat().filter((m) => m.winner).length || 0;
  $("progress").textContent = t ? `${completed} / ${t.fighters.length - 1} fights` : "Not seeded";
  $("seed-label").textContent = t ? `Seed: ${t.seed}` : "Ready when you are";
  $("match-status").textContent = state.launch.status.replaceAll("_", " ");
  $("empty-match").hidden = Boolean(t);
  $("active-match").hidden = !match;
  $("champion").hidden = !t?.champion;
  $("champion-name").textContent = lookup.get(t?.champion) || "";
  $("launch-message").textContent = state.launch.message;
  $("automation-message").textContent = state.automation.message;
  $("detection-status").textContent = !state.detection.supported ? "This executable build is unsupported. Automatic play is disabled; manual confirmation is required." : "Build supported. Stable engine results advance automatically after a clean exit; no confirmation gate.";
  $("chosen-stage").textContent = state.launch.stage ? `Last launch stage: ${state.launch.stage}` : "";
  $("launch-details").hidden = !state.launch.command;
  $("command").textContent = state.launch.command?.map((arg) => JSON.stringify(arg)).join(" ") || "";
  $("log-path").textContent = state.launch.log ? `Launcher + console + result log: ${state.launch.log}\nGame log: ${state.launch.game_log || state.launch.game_log_capture || "Captured when the game exits."}` : "";
  $("round-label").textContent = match ? roundName(t.rounds.findIndex((r) => r.includes(match) || r.some((m) => m.id === match.id)), t.rounds.length) : t?.champion ? "COMPLETE" : "STANDBY";
  if (match) {
    $("series-format").textContent = `Best of ${match.best_of} · ${match.wins.join(" – ")} · First to ${Math.floor(match.best_of / 2) + 1} round win(s). Each launch plays one round.`;
    ["p1", "p2"].forEach((id, i) => {
      $(id).textContent = lookup.get(match.players[i]);
      $(`${id}-id`).textContent = match.players[i];
      $(`win${i + 1}`).textContent = `Confirm ${lookup.get(match.players[i])} won this round`;
      const imageKey = match.players[i] + Broadcast.revision;
      if ($(`desk-portrait${i + 1}`).dataset.id !== imageKey) {
        $(`desk-portrait${i + 1}`).dataset.id = imageKey;
        $(`desk-portrait${i + 1}`).replaceChildren(Broadcast.portrait(match.players[i], lookup.get(match.players[i]), true));
      }
    });
  }
  Broadcast.tree(t, match, $("bracket"));
}

function roundName(round, total) {
  return round === total - 1 ? "Final" : round === total - 2 ? "Semifinals" : round === total - 3 ? "Quarterfinals" : `Round ${round + 1}`;
}

function renderControls() {
  const match = state?.current_match;
  const status = state?.launch.status;
  const stale = !lastSnapshot || Date.now() - lastSnapshot > 6000;
  const updating = state?.portrait_update?.status === "running";
  const blocked = busy || stale || updating;
  const automatic = Boolean(state?.automation.enabled);
  $("auto-start").disabled = blocked || automatic || !match || !state?.detection.supported || ["running", "launching", "recovery_required"].includes(status);
  $("auto-pause").disabled = blocked || !automatic;
  const live = ["running", "launching", "recovery_required"].includes(status);
  const presentation = state?.tournament?.presentation;
  const remaining = Math.max(0, (presentation?.ready_at || 0) - (Date.now() + offset) / 1000);
  $("countdown").textContent = !presentation?.prepared ? "Prepare betting / presentation" :
    remaining ? `${Broadcast.clock(remaining)} until launch ready · ${presentation.mode === "local" ? "LOCAL ONLY" : "VIRTUAL CREDIT BETTING"}` :
    "Presentation complete / waiting for launch or betting lock";
  $("window").disabled = blocked || automatic || !match || live || presentation?.prepared;
  $("local-preview").disabled = $("window").disabled || ["ACTIVE", "LOCKED", "CREATING"].includes(state?.twitch.prediction?.status);
  $("prediction-sync").disabled = blocked || live;
  $("prediction-cancel").disabled = blocked || live || !state?.twitch.prediction || ["RESOLVED", "CANCELED", "ABANDONED"].includes(state?.twitch.prediction?.status);
  $("window").textContent = "Prepare chat betting";
  for (const id of ["chat-select", "chat-login", "chat-reconnect", "chat-reopen", "chat-continue", "legacy-ack"]) $(id).disabled = blocked || live || automatic;
  $("chat-reopen").hidden = state?.twitch.prediction?.provider !== "chat" || state.twitch.prediction.status !== "CANCELED";
  $("chat-continue").hidden = $("chat-reopen").hidden;
  $("launch").disabled = blocked || automatic || !match || live || remaining > 0 || !presentation?.prepared || Boolean(state?.twitch.error) || !state?.roster.stages.length;
  $("launch").textContent = status === "awaiting_result" ? "Replay AI match" : "Launch AI match";
  $("recover").hidden = status !== "recovery_required";
  $("recover").disabled = blocked;
  $("win1").disabled = $("win2").disabled = blocked || automatic || status !== "awaiting_result";
  $("create-form").querySelector("button[type=submit]").disabled = blocked || automatic || live || !state?.roster.fighters.length;
  $("settings-form").querySelector("button[type=submit]").disabled = blocked || live || Boolean(state?.tournament && !state.tournament.champion);
  $("refresh").disabled = blocked;
  $("update-portraits").disabled = blocked || automatic || live;
  $("stage").disabled = blocked || automatic || live;
}

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault(); action("settings", { game_path: $("game-path").value });
});
$("create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const replace = Boolean(state?.tournament);
  if (replace && !confirm("Replace the saved tournament? Its progress will be overwritten. Export or copy runtime\\state.json first if you need to keep it.")) return;
  action("create", {
    fighters: state.roster.fighters.filter((f) => selected.has(f.id)).map((f) => f.id),
    size: Number($("size").value), seconds: Number($("seconds").value), seed: $("seed").value, replace,
  });
});
$("refresh").addEventListener("click", () => action("refresh"));
$("update-portraits").addEventListener("click", () => action("update_portraits"));
$("auto-start").addEventListener("click", () => {
  if (confirm("Start the automatic tournament on this PC? This launches games until a champion is found, records detected round winners, and pauses on ambiguous results. Any previous unconfirmed round will be replayed. Rounds exceeding 180 seconds are stopped without awarding a win.")) action("auto_start");
});
$("auto-pause").addEventListener("click", () => action("auto_pause"));
$("search").addEventListener("input", renderRoster);
$("select-all").addEventListener("click", () => { selected = new Set(state?.roster.fighters.map((f) => f.id)); renderRoster(); });
$("select-none").addEventListener("click", () => { selected.clear(); renderRoster(); });
$("window").addEventListener("click", () => action("window", matchData()));
$("local-preview").addEventListener("click", () => {
  if (confirm("Use LOCAL PREVIEW for this tournament? No credit bets will be accepted. Future windows in this tournament will also be local-only.")) action("prediction_local");
});
$("chat-select").addEventListener("click", () => action("chat_select"));
$("chat-login").addEventListener("click", () => action("chat_login"));
$("chat-reconnect").addEventListener("click", () => action("chat_reconnect"));
$("chat-reopen").addEventListener("click", () => action("chat_reopen"));
$("chat-continue").addEventListener("click", () => action("chat_continue"));
$("prediction-sync").addEventListener("click", () => action("prediction_sync"));
$("prediction-cancel").addEventListener("click", () => {
  if (confirm("Cancel this betting window and refund all virtual-credit stakes? Automatic launches will pause.")) action("prediction_cancel");
});
$("legacy-ack").addEventListener("click", () => {
  if (confirm("Confirm the old Channel Points prediction was canceled/resolved manually in Twitch Creator Dashboard, or never existed. AutoMugen has not refunded or changed it. Continue?")) action("legacy_ack", { verified_handled: true });
});
$("launch").addEventListener("click", () => {
  if (confirm("Launch MUGEN on this PC? This opens the game with two AI fighters. Close the game after observing a clear outcome.")) action("launch", { ...matchData(), stage: $("stage").value });
});
$("recover").addEventListener("click", () => {
  if (confirm("Have you closed all game instances? No winner will be inferred. You may then confirm an observed outcome or replay.")) action("recover", matchData());
});
[0, 1].forEach((i) => $(`win${i + 1}`).addEventListener("click", () => {
  const winner = state.current_match.players[i];
  if (confirm(`Confirm you personally observed ${names().get(winner)} win this round? This saves a round win and advances the bracket only when the series is won.`)) action("result", { ...matchData(), winner });
}));
$("overlay-url").textContent = `${location.origin}/overlay`;
async function poll() {
  if (!busy) {
    try {
      const response = await fetch("/api/state", { signal: AbortSignal.timeout(5000) });
      const next = await response.json();
      if (!response.ok) throw new Error(next.error || "Server unavailable.");
      if (!busy) accept(next);
    } catch (error) {
      $("connection").textContent = "SERVER UNAVAILABLE";
      $("connection").className = "pill amber";
      showError(`Dashboard update failed: ${error.message}. Controls are disabled once state becomes stale.`, "connection");
    }
  }
  setTimeout(poll, 1500);
}
setInterval(renderControls, 250);
poll();
