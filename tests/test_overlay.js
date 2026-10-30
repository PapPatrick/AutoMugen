"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const publicDir = path.join(__dirname, "..", "public");

class Element {
  constructor() {
    this.textContent = "";
    this.hidden = false;
    this.dataset = {};
    this.style = {};
    this.children = [];
    this.classList = { add() {}, toggle() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
}

const html = fs.readFileSync(path.join(publicDir, "overlay.html"), "utf8");
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]));
const body = new Element();
const context = vm.createContext({
  document: {
    body,
    getElementById(id) {
      assert.ok(elements.has(id), `Missing overlay element ${id}`);
      return elements.get(id);
    },
    createElement() { return new Element(); },
  },
  location: { search: "" },
  innerWidth: 1920, innerHeight: 1080,
  URLSearchParams, Date, AbortSignal,
  setInterval() {}, setTimeout() {},
  fetch: () => new Promise(() => {}),
  window: { addEventListener() {} },
});
vm.runInContext(fs.readFileSync(path.join(publicDir, "broadcast.js"), "utf8"), context);
context.Broadcast = context.window.Broadcast;
vm.runInContext(fs.readFileSync(path.join(publicDir, "overlay.js"), "utf8"), context);

const match = { id: "r1m1", players: ["one", "two"], winner: null, best_of: 1, wins: [0, 0] };
const tournament = {
  id: "tournament", champion: null,
  fighters: [{ id: "one", name: "One" }, { id: "two", name: "Two" }],
  rounds: [[match]],
};
const prediction = {
  id: "bets", economy: "credits", provider: "chat", status: "ACTIVE",
  tournament_id: "tournament", match_id: "r1m1", deadline: 1180,
};
const snapshot = {
  tournament, current_match: match,
  twitch: { prediction, error: null },
  broadcast: { mode: "chat", screen: "bracket", phase: "prediction", deadline: 1180 },
  chat: { connected: true, counts: [2, 1], pools: [300, 700], pool: 1000, leaderboard: [] },
};
assert.doesNotThrow(() => context.Broadcast.requireCreditBetting(snapshot));
for (const chat of [
  { connected: true, counts: [0, 0], leaderboard: [] },
  { ...snapshot.chat, pools: undefined },
  { ...snapshot.chat, pool: 0 },
  { ...snapshot.chat, pools: [-100, 1100] },
  { ...snapshot.chat, leaderboard: [{ login: "oldviewer", points: 10 }] },
]) {
  assert.throws(() => context.Broadcast.requireCreditBetting({ ...snapshot, chat }),
    /Restart the AutoMugen Python server/);
}
function render(data) {
  vm.runInContext(`state = ${JSON.stringify(data)}; lastUpdate = Date.now(); offset = 1000000 - Date.now(); render();`, context);
}
render(snapshot);
assert.equal(elements.get("pool-total").textContent, `${(1000).toLocaleString()} CREDITS`);
assert.equal(elements.get("pool-counter").hidden, false);
assert.match(elements.get("hero-bets1").textContent, /2 BETS.*300 CREDITS.*3.33x/);
assert.match(elements.get("overlay-phase").textContent, /CHAT BETTING/);
assert.match(elements.get("tree-subtitle").textContent, /!bet 100 1 = One/);
const firstCard = elements.get("stream-tree").children[0].children[1].children[0].children[0];
assert.equal(firstCard.children[1].children.at(-1).textContent, "2 bets / 300 cr");

render({ ...snapshot, twitch: { prediction: { ...prediction, status: "LOCKED" } } });
assert.match(elements.get("pool-label").textContent, /LOCKED/);
render({ ...snapshot, twitch: { prediction: { ...prediction, tournament_id: "different" } } });
assert.equal(elements.get("pool-counter").hidden, true);
assert.equal(elements.get("hero-bets1").textContent, "");
render({ ...snapshot, twitch: { prediction: { ...prediction, status: "CANCELED" } } });
assert.equal(elements.get("pool-counter").hidden, true);
assert.match(elements.get("overlay-phase").textContent, /STAKES REFUNDED/);

const result = {
  winner: "one", players: ["one", "two"], round: 0, match_id: "r1m1", expires_at: 1015,
  prediction: { provider: "chat", status: "RESOLVED", counts: [2, 1], pools: [300, 700], payout_credits: 1000, settlement: "PAID" },
};
render({ ...snapshot, result });
assert.equal(body.dataset.screen, "result");
assert.match(elements.get("result-payout").textContent, /1[,.]000 CREDITS PAID.*INCLUDES RETURNED STAKES/);
assert.match(elements.get("result-bets").textContent, /2 bets.*One.*300 CREDITS/);
render({ ...snapshot, result: { ...result, prediction: { ...result.prediction, settlement: "REFUNDED" } } });
assert.match(elements.get("result-payout").textContent, /REFUNDED.*NO TWO-SIDED/);
render({ ...snapshot, result: { ...result, prediction: { provider: "chat", status: "CANCELED", payout_credits: 1000 } } });
assert.equal(elements.get("result-payout").textContent, `${(1000).toLocaleString()} CREDITS REFUNDED`);
render({ ...snapshot, result: { ...result, prediction: null } });
assert.equal(elements.get("result-bets").textContent, "NO PREDICTION WINDOW · NO BETS");

const dashboardHtml = fs.readFileSync(path.join(publicDir, "index.html"), "utf8");
const dashboardIds = new Set([...dashboardHtml.matchAll(/id="([^"]+)"/g)].map((match) => match[1]));
const dashboardScript = fs.readFileSync(path.join(publicDir, "app.js"), "utf8");
for (const match of dashboardScript.matchAll(/\$\("([^"]+)"\)/g)) {
  assert.ok(dashboardIds.has(match[1]), `Missing dashboard element ${match[1]}`);
}
for (const removed of ["twitch-select", "twitch-login", "twitch-check", "prediction-abandon"]) {
  assert.equal(dashboardIds.has(removed), false);
}
console.log("Overlay credit counters, bracket totals, payouts, scoping and dashboard controls passed.");
