"use strict";
(async () => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  history.replaceState(null, "", "/auth/callback");
  try {
    if (fragment.has("error")) throw new Error("Twitch authorization was denied. Start again from the dashboard.");
    if (!fragment.get("access_token") || !fragment.get("state")) throw new Error("Missing Twitch authorization response.");
    const snapshot = await fetch("/api/state").then((r) => r.json());
    const response = await fetch("/api/action/twitch_callback", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-AutoMugen-Token": snapshot.token },
      body: JSON.stringify({ access_token: fragment.get("access_token"), state: fragment.get("state") }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Authorization failed.");
    document.getElementById("auth-status").textContent =
      `Bot authorized as ${result.chat.bot}, joining #${result.chat.channel}. Return to the dashboard and wait for Connected before preparing credit betting.`;
  } catch (error) {
    document.getElementById("auth-status").textContent = error.message;
  } finally {
    fragment.delete("access_token");
  }
})();
