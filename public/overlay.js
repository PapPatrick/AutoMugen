"use strict";
const $ = (id) => document.getElementById(id);
let state = null, offset = 0, lastUpdate = 0, signature = "", resultSignature = "";
// Operators can leave gameplay completely clear without affecting intermissions.
document.body.dataset.hud = new URLSearchParams(location.search).get("hud") === "off" ? "off" : "compact";
function fitBroadcast() {
  document.body.style.transform = `scale(${Math.min(innerWidth / 1920, innerHeight / 1080)})`;
}
window.addEventListener("resize", fitBroadcast);
fitBroadcast();

function renderBettingCounters(match, prediction) {
  const applicable = match && prediction?.economy === "credits" &&
    prediction.tournament_id === state.tournament?.id && (prediction.kind === "champion" || prediction.match_id === match.id) &&
    ["ACTIVE", "LOCKED"].includes(prediction.status);
  const counter = $("pool-counter");
  if (!applicable) {
    counter.hidden = true;
    $("hero-bets1").textContent = "";
    $("hero-bets2").textContent = "";
    return;
  }

  const total = state.chat.pool;
  const locked = prediction.status === "LOCKED";
  $("pool-label").textContent = `${locked ? "LOCKED " : ""}${prediction.kind === "champion" ? "CHAMPION" : "MATCH"} CREDIT POOL`;
  $("pool-total").textContent = `${total.toLocaleString()} CREDITS`;
  counter.hidden = false;

  [0, 1].forEach((index) => {
    const option = prediction.players.indexOf(match.players[index]);
    const credits = state.chat.pools[option] || 0, count = state.chat.counts[option] || 0;
    const odds = credits && total > credits ? ` · ${(total / credits).toFixed(2)}x EST.` : "";
    $(`hero-bets${index + 1}`).textContent =
      `${count.toLocaleString()} BETS · ${credits.toLocaleString()} CREDITS${odds}`;
  });
}

function render() {
  if (!state || Date.now() - lastUpdate > 5000) return;
  const now = (Date.now() + offset) / 1000;
  Broadcast.revision = state.portrait_revision || "";
  const t = state.tournament, m = state.current_match, b = state.broadcast;
  const p = state.twitch.prediction;
  const names = new Map((t?.fighters || []).map((f) => [f.id, f.name]));
  const remaining = Math.max(0, (b.deadline || now) - now);
  const result = state.result && state.result.expires_at > now ? state.result : null;
  const chatPrediction = p?.economy === "credits";
  const currentBetting = chatPrediction && p.tournament_id === t?.id && (p.kind === "champion" || p.match_id === m?.id);
  const chatOpen = currentBetting && p.status === "ACTIVE" && p.deadline > now && state.chat?.connected;
  const refunded = currentBetting && p.status === "CANCELED";
  const round = t?.rounds.findIndex((r) => r.some((match) => match.id === m?.id));
  document.body.dataset.screen = result ? "result" : b.screen;
  $("overlay-round").textContent = t?.champion ? "TOURNAMENT COMPLETE" :
    result ? `${Broadcast.roundName(result.round, t.rounds.length)} / ${result.match_id.toUpperCase()}` :
      m ? `${Broadcast.roundName(round, t.rounds.length)} / ${m.id.toUpperCase()}` : "AI FIGHT NIGHT";
  $("overlay-progress").textContent = t ? `${t.rounds.flat().filter((match) => match.winner).length} / ${t.fighters.length - 1} MATCHES` : "";
  $("overlay-title").textContent = result ? "MATCH RESULT" : chatOpen && p.kind === "champion" ? "Who will win the grand final?" :
    t?.champion ? `${names.get(t.champion)} is champion` : "The road to champion";
  $("game-context").textContent = m ? `${Broadcast.roundName(round, t.rounds.length)} / ${m.id.toUpperCase()}` : "";
  $("overlay-phase").textContent = result ? "MATCH COMPLETE · FINAL RESULT" :
    state.twitch.error ? "OPERATOR ATTENTION · BETTING PAUSED" :
    refunded ? "BETTING CANCELED · STAKES REFUNDED" :
    chatOpen ? p.kind === "champion" ? "PICK THE CHAMPION · !bet 100 <1–4>" : "MATCH BETTING · !bet 100 1 / !bet 100 2" :
    b.phase === "prediction" ?
      b.mode === "local" ? "LOCAL PREVIEW · NO BETTING" : "BETTING LOCKING / DISPLAY WINDOW" :
    b.phase === "setup" ? "WAITING FOR BETTING SETUP" :
    b.phase === "review" ? "MATCH PAUSED · OPERATOR REVIEW" :
    b.phase === "complete" ? "LAST FIGHTER STANDING" : b.phase === "fighting" ? "AI BATTLE IN PROGRESS" : "NEXT BATTLE";
  $("overlay-timer").textContent = result ? `RESULT · ${Broadcast.clock(result.expires_at - now)}` :
    refunded ? `REFUNDED${remaining ? ` / ${Broadcast.clock(remaining)}` : ""}` :
    b.phase === "prediction" ? Broadcast.clock(remaining) :
    chatOpen ? "BETTING OPEN" : currentBetting && p.status === "LOCKED" ? "BETS LOCKED" :
    refunded ? "STAKES REFUNDED" : b.mode === "local" ? "LOCAL PREVIEW" : "AUTO MUGEN";
  $("tree-subtitle").textContent = chatOpen ? `${p.players.map((id, i) => `${i + 1} = ${names.get(id)}`).join("; ")}. ${p.kind === "champion" ? "Pays after the grand final." : "Pays after this match."} !balance checks credits.` :
    b.phase === "setup" ? "Connect chat and prepare the betting window, or choose local preview." :
    b.mode === "local" ? "Preview only. No bets accepted." : "Virtual credits only. No cash value, prizes or redemption.";
  Broadcast.tree(t, m, $("stream-tree"), now, true, currentBetting ? state.chat : null);
  Broadcast.choices(p?.kind === "champion" ? p : null, state.chat, t, $("champion-choices"));
  $("chat-stream").hidden = !chatPrediction && b.mode !== "chat";
  $("chat-stream").textContent = state.chat ? `VIRTUAL CREDITS / NO CASH VALUE · START 1,000 · LOW BALANCE REFILL 100 · !balance${state.chat.leaderboard.length ? " · LEADERS " + state.chat.leaderboard.map((row) => `${row.login} ${row.credits.toLocaleString()}`).join(" / ") : ""}` : "";
  const nextSignature = JSON.stringify([m?.players, t?.champion, Broadcast.revision]);
  if (signature !== nextSignature) {
    signature = nextSignature;
    [0, 1].forEach((i) => {
      const id = t?.champion || m?.players[i];
      $(`hero${i + 1}`).replaceChildren(Broadcast.portrait(id, names.get(id), true));
    });
  }
  $("hero-name1").textContent = names.get(t?.champion || m?.players[0]) || "Awaiting fighter";
  $("hero-name2").textContent = t?.champion ? "Champion" : names.get(m?.players[1]) || "Awaiting fighter";
  renderBettingCounters(m, p);
  $("hero-series").textContent = m ? `BEST OF ${m.best_of} · ${m.wins.join(" — ")} · RANDOM STAGE` : "JUMP FORCE / AI VS AI";
  $("overlay-p1").textContent = names.get(m?.players[0]) || "";
  $("overlay-p2").textContent = names.get(m?.players[1]) || "";
  $("game-score").textContent = m ? `${m.wins.join(" : ")} · BO${m.best_of}` : "";

  if (result) renderResult(result, names, t.rounds.length);
}

function renderResult(result, names, totalRounds) {
  const winnerName = names.get(result.winner) || result.winner;
  $("result-winner").textContent = `${winnerName} wins`;
  $("result-match").textContent = `${Broadcast.roundName(result.round, totalRounds)} · ${result.match_id.toUpperCase()}`;
  const portraitSignature = `${result.winner}:${Broadcast.revision}`;
  if (resultSignature !== portraitSignature) {
    resultSignature = portraitSignature;
    $("result-portrait").replaceChildren(Broadcast.portrait(result.winner, winnerName, true));
  }

  const bets = $("result-bets");
  bets.replaceChildren();
  const prediction = result.prediction;
  if (!prediction) {
    bets.textContent = "NO PREDICTION WINDOW · NO BETS";
    $("result-payout").textContent = "";
    return;
  }
  if (prediction.status === "CANCELED") {
    bets.textContent = "BETTING CANCELED";
    $("result-payout").textContent = `${prediction.payout_credits.toLocaleString()} CREDITS REFUNDED`;
    return;
  }

  if (prediction.provider === "chat") {
    bets.textContent = prediction.counts.map((count, index) =>
      `${count} bets · ${names.get((prediction.players || result.players)[index]) || (prediction.players || result.players)[index]} · ${prediction.pools[index].toLocaleString()} CREDITS`
    ).join("   /   ");
    $("result-payout").textContent = prediction.payout_credits === null ?
      "CREDIT PAYOUT PENDING" : prediction.settlement === "REFUNDED" ?
        `${prediction.payout_credits.toLocaleString()} CREDITS REFUNDED · NO WINNING OR OPPOSING STAKES` :
        `${prediction.payout_credits.toLocaleString()} CREDITS PAID TO WINNERS · INCLUDES RETURNED STAKES`;
  }
}

async function poll() {
  try {
    const response = await fetch("/api/overlay", { signal: AbortSignal.timeout(12000) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Server unavailable");
    Broadcast.requireCreditBetting(data);
    state = data;
    offset = data.server_time * 1000 - Date.now();
    lastUpdate = Date.now();
    $("overlay-notice").hidden = true;
    render();
  } catch (error) {
    $("overlay-notice").hidden = false;
    $("overlay-notice").textContent = `CONTROL OFFLINE · DISPLAY STALE · ${error.message}`;
    $("overlay-timer").textContent = "OFFLINE";
  }
  setTimeout(poll, 1000);
}
setInterval(() => {
  if (state && Date.now() - lastUpdate > 5000) {
    $("overlay-notice").hidden = false;
    $("overlay-notice").textContent = "CONTROL UPDATE DELAYED · DISPLAY STALE";
    $("overlay-timer").textContent = "STALE";
  } else render();
}, 250);
poll();
