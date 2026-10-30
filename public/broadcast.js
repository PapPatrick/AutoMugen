"use strict";
// Shared, text-only DOM rendering for operator and OBS surfaces.
window.Broadcast = {
  revision: "",
  requireCreditBetting(snapshot) {
    const chat = snapshot?.chat;
    if (!Array.isArray(chat?.pools) || ![2, 4].includes(chat.pools.length) ||
        !chat.pools.every((value) => Number.isSafeInteger(value) && value >= 0) ||
        !Number.isSafeInteger(chat.pool) || chat.pool !== chat.pools.reduce((sum, n) => sum + n, 0) ||
        !Array.isArray(chat.counts) || chat.counts.length !== chat.pools.length ||
        !chat.counts.every((value) => Number.isSafeInteger(value) && value >= 0) ||
        !Array.isArray(chat.leaderboard) ||
        !chat.leaderboard.every((row) => Number.isSafeInteger(row.credits) && row.credits >= 0)) {
      throw new Error("Betting API is outdated or incompatible. Restart the AutoMugen Python server, then refresh this page. Betting controls are disabled until the updated server responds.");
    }
  },
  choices(prediction, chat, tournament, container) {
    const show = prediction?.economy === "credits" &&
      prediction.tournament_id === tournament?.id && ["ACTIVE", "LOCKED"].includes(prediction.status);
    container.hidden = !show;
    if (!show) { container.replaceChildren(); delete container.dataset.signature; return; }
    const signature = JSON.stringify([prediction.players, prediction.status, chat.counts, chat.pools, this.revision]);
    if (container.dataset.signature === signature) return;
    container.dataset.signature = signature;
    const names = new Map(tournament.fighters.map((f) => [f.id, f.name]));
    container.replaceChildren(...prediction.players.map((id, i) => {
      const card = document.createElement("div"); card.className = "bet-choice";
      const label = document.createElement("strong");
      label.textContent = `${i + 1} · ${names.get(id)}`;
      const stats = document.createElement("span");
      stats.textContent = `${chat.counts[i]} bets · ${chat.pools[i].toLocaleString()} cr`;
      const command = document.createElement("code");
      command.textContent = prediction.status === "ACTIVE" ? `!bet 100 ${i + 1}` : "LOCKED";
      card.append(this.portrait(id, names.get(id)), label, stats, command);
      return card;
    }));
  },
  roundName(round, total) {
    return round === total - 1 ? "Grand final" : round === total - 2 ? "Semifinals" :
      round === total - 3 ? "Quarterfinals" : `Round ${round + 1}`;
  },
  portrait(id, name, large = false) {
    const box = document.createElement("span");
    box.className = `portrait${large ? " portrait-large" : ""}`;
    const fallback = document.createElement("span");
    fallback.className = "portrait-fallback";
    fallback.textContent = id ? (name || "?").slice(0, 2).toUpperCase() : "?";
    fallback.title = id ? "Portrait unavailable: initial-letter fallback" : "Awaiting winner";
    box.append(fallback);
    if (id) {
      const img = document.createElement("img");
      img.alt = name || id;
      img.loading = large ? "eager" : "lazy";
      img.src = `/api/portrait?id=${encodeURIComponent(id)}&v=${encodeURIComponent(this.revision)}`;
      img.addEventListener("load", () => { fallback.hidden = true; });
      img.addEventListener("error", () => { img.remove(); box.classList.add("missing"); });
      box.append(img);
    }
    return box;
  },
  tree(t, current, container, now = 0, paginate = false, betting = null) {
    const pages = paginate && t ? Math.max(1, t.fighters.length / 16) : 1;
    const page = Math.floor(now / 12) % pages;
    const signature = JSON.stringify([t, current?.id, page, this.revision, betting?.counts, betting?.pools]);
    if (container.dataset.signature === signature) return;
    container.dataset.signature = signature;
    container.replaceChildren();
    if (!t) {
      const empty = document.createElement("p");
      empty.className = "tree-empty";
      empty.textContent = "The arena is waiting. Seed your tournament in AutoMugen.";
      container.append(empty); return;
    }
    const names = new Map(t.fighters.map((f) => [f.id, f.name]));
    container.classList.toggle("tree-paged", pages > 1);
    container.dataset.page = pages > 1 ? `DRAW ${page + 1} / ${pages} · Championship rounds always visible` : "";
    t.rounds.forEach((matches, round) => {
      const col = document.createElement("section"); col.className = "tree-round";
      if (pages > 1 && round === Math.log2(t.fighters.length) - 3) col.classList.add("tree-page-root");
      if (pages > 1 && round === Math.log2(t.fighters.length) - 2) col.classList.add("tree-championship-start");
      const heading = document.createElement("h3");
      heading.textContent = this.roundName(round, t.rounds.length); col.append(heading);
      const stack = document.createElement("div"); stack.className = "tree-stack";
      const perPage = Math.max(1, 8 / (2 ** round));
      const visible = pages > 1 && round <= Math.log2(t.fighters.length) - 3 ?
        matches.slice(page * perPage, (page + 1) * perPage) : matches;
      visible.forEach((m) => {
        const wrapper = document.createElement("div"); wrapper.className = "tree-slot";
        const card = document.createElement("div"); card.className = "tree-match";
        if (m.id === current?.id) card.classList.add("up-next");
        if (m.winner) card.classList.add("finished");
        const tag = document.createElement("small");
        tag.textContent = `${m.id.toUpperCase()} · BO${m.best_of}${m.id === current?.id ? " · UP NEXT" : ""}`;
        card.append(tag);
        m.players.forEach((id, i) => {
          const row = document.createElement("div"); row.className = "tree-fighter";
          row.classList.toggle("won", Boolean(id && id === m.winner));
          row.classList.toggle("lost", Boolean(m.winner && id !== m.winner));
          const label = document.createElement("span"); label.textContent = names.get(id) || "Awaiting winner";
          label.title = names.get(id) || "Awaiting winner";
          const score = document.createElement("b"); score.textContent = m.winner === id && id ? "W" : m.wins[i];
          row.append(this.portrait(id, names.get(id)), label, score);
          const option = betting?.players?.indexOf(id);
          if (betting && (betting.kind === "champion" ? option >= 0 : m.id === current?.id)) {
            const counter = document.createElement("small");
            counter.className = "tree-bets";
            const index = option >= 0 ? option : i;
            counter.textContent = `${betting.kind === "champion" ? `#${index + 1} · ` : ""}${betting.counts[index]} bets / ${betting.pools[index].toLocaleString()} cr`;
            row.append(counter);
          }
          card.append(row);
        });
        wrapper.append(card); stack.append(wrapper);
      });
      col.append(stack); container.append(col);
    });
  },
  clock(seconds) {
    seconds = Math.max(0, Math.ceil(seconds));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  },
};
