(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, props = {}, ...children) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  };

  const state = { player: null, games: [], pollTimer: null, busy: false };

  // ------------------------------------------------------------------ theme toggle
  const fancyCss = $("fancy-css");
  const toggle = $("theme-toggle");
  function applyTheme(theme) {
    fancyCss.disabled = theme === "bare";
    toggle.checked = theme !== "bare";
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch (e) {}
  }
  applyTheme(fancyCss.disabled ? "bare" : "stunning");
  toggle.addEventListener("change", () => applyTheme(toggle.checked ? "stunning" : "bare"));

  // ------------------------------------------------------------------ helpers
  async function getJSON(url, options) {
    const res = await fetch(url, options);
    let data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
    return data;
  }
  const radio = (name) => document.querySelector(`input[name="${name}"]:checked`).value;
  function fillSelect(select, options, placeholder) {
    select.replaceChildren();
    if (placeholder) select.append(el("option", { value: "", textContent: placeholder }));
    options.forEach((o) => select.append(o));
  }
  const fmtDuration = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "");

  // ------------------------------------------------------------------ step 1: player search
  const input = $("player-input");
  const list = $("player-suggestions");
  let searchTimer = null;
  let searchCtrl = null;
  let activeIndex = -1;

  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (q.length < 2) { list.hidden = true; return; }
    searchTimer = setTimeout(() => searchPlayers(q), 180);
  });

  input.addEventListener("keydown", (e) => {
    const items = [...list.querySelectorAll("button")];
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = (activeIndex + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items.forEach((b, i) => b.classList.toggle("active", i === activeIndex));
      items[activeIndex].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      items[Math.max(0, activeIndex)].click();
    } else if (e.key === "Escape") {
      list.hidden = true;
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) list.hidden = true;
  });

  async function searchPlayers(q) {
    searchCtrl?.abort();
    searchCtrl = new AbortController();
    try {
      const players = await getJSON(`/api/players?q=${encodeURIComponent(q)}`, { signal: searchCtrl.signal });
      activeIndex = -1;
      list.replaceChildren();
      if (!players.length) {
        list.append(el("li", { className: "empty", textContent: "No players found" }));
      }
      players.forEach((p) => {
        const btn = el("button", { type: "button" },
          el("span", { textContent: p.name }),
          el("small", { textContent: p.active ? " Active" : " Retired" }));
        btn.addEventListener("click", () => selectPlayer(p));
        list.append(el("li", { role: "option" }, btn));
      });
      list.hidden = false;
    } catch (e) {
      if (e.name !== "AbortError") console.error(e);
    }
  }

  async function selectPlayer(p) {
    state.player = p;
    input.value = p.name;
    list.hidden = true;
    $("player-name").textContent = p.name;
    $("player-meta").textContent = p.active ? "Active player" : "Retired";
    const img = $("player-headshot");
    img.onerror = () => { img.style.visibility = "hidden"; };
    img.onload = () => { img.style.visibility = "visible"; };
    img.src = `https://cdn.nba.com/headshots/nba/latest/260x190/${p.id}.png`;
    $("player-card").hidden = false;

    const seasonSel = $("season-select");
    const gameSel = $("game-select");
    seasonSel.disabled = gameSel.disabled = true;
    fillSelect(seasonSel, [], "Loading seasons…");
    fillSelect(gameSel, [], "Choose a season first");
    $("game-preview").hidden = true;
    updateMakeButton();
    setStatus("Looking up seasons…");

    try {
      const seasons = await getJSON(`/api/seasons?player_id=${p.id}`);
      if (state.player !== p) return;
      if (!seasons.length) { setStatus("No NBA seasons on record for this player."); return; }
      fillSelect(seasonSel, seasons.map((s) => el("option", { value: s, textContent: s })));
      seasonSel.disabled = false;
      setStatus("");
      loadGames();
    } catch (e) {
      setStatus(e.message, true);
    }
  }

  // ------------------------------------------------------------------ step 2: season + game
  function setStatus(msg, isError = false) {
    const s = $("game-status");
    s.textContent = msg;
    s.classList.toggle("is-error", isError);
  }

  $("season-select").addEventListener("change", loadGames);
  $("game-select").addEventListener("change", () => { showPreview(); updateMakeButton(); });

  async function loadGames() {
    const season = $("season-select").value;
    const gameSel = $("game-select");
    const player = state.player;
    if (!season || !player) return;
    gameSel.disabled = true;
    fillSelect(gameSel, [], "Loading games…");
    $("game-preview").hidden = true;
    updateMakeButton();
    setStatus(`Loading ${player.name}'s ${season} games…`);
    try {
      const games = await getJSON(`/api/games?player_id=${player.id}&season=${season}`);
      if (state.player !== player || $("season-select").value !== season) return;
      state.games = games;
      if (!games.length) {
        fillSelect(gameSel, [], "No games found");
        setStatus("No games found for that season.");
        return;
      }
      const groups = {};
      games.forEach((g, i) => {
        const label = `${g.date_label}  ·  ${g.matchup}  ·  ${g.wl || "-"}  ·  ${g.pts ?? 0} PTS / ${g.reb ?? 0} REB / ${g.ast ?? 0} AST`;
        (groups[g.type] ||= []).push(el("option", { value: String(i), textContent: label }));
      });
      const order = ["Playoffs", "Play-In", "Regular Season"];
      const groupNodes = order.filter((t) => groups[t]).map((t) => {
        const og = el("optgroup", { label: t });
        og.append(...groups[t]);
        return og;
      });
      fillSelect(gameSel, groupNodes, "Choose a game…");
      gameSel.disabled = false;
      setStatus(`${games.length} games in ${season}.`);
    } catch (e) {
      fillSelect(gameSel, [], "Couldn't load games");
      setStatus(e.message, true);
    }
  }

  function selectedGame() {
    const v = $("game-select").value;
    return v === "" ? null : state.games[Number(v)];
  }

  function showPreview() {
    const g = selectedGame();
    const box = $("game-preview");
    if (!g) { box.hidden = true; return; }
    const stat = (n, label) => el("div", { className: "stat" }, el("b", { textContent: n ?? "-" }), el("span", { textContent: label }));
    box.replaceChildren(
      el("div", { className: "matchup" },
        el("span", { className: "wl " + (g.wl === "W" ? "win" : "loss"), textContent: g.wl || "-" }),
        el("span", { textContent: `${g.matchup}  ·  ${g.date_label}  ·  ${g.type}` })),
      el("div", { className: "stats" }, stat(g.pts, "PTS"), stat(g.reb, "REB"), stat(g.ast, "AST"), stat(g.min, "MIN"))
    );
    box.hidden = false;
  }

  // ------------------------------------------------------------------ step 3/4: make the reel
  function updateMakeButton() {
    $("make-btn").disabled = state.busy || !state.player || !selectedGame();
  }

  $("make-btn").addEventListener("click", () => makeReel());

  async function makeReel(videoId = null) {
    const game = selectedGame();
    if (!game || !state.player || state.busy) return;
    state.busy = true;
    updateMakeButton();
    clearTimeout(state.pollTimer);

    const card = $("step-result");
    card.hidden = false;
    card.classList.add("busy");
    $("progress-wrap").hidden = false;
    $("result").hidden = true;
    $("error").hidden = true;
    $("alternatives").hidden = true;
    $("video").removeAttribute("src");
    $("video").load();
    setProgress(1, "Starting…");
    card.scrollIntoView({ behavior: "smooth", block: "start" });

    try {
      const { job_id } = await getJSON("/api/highlight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          player_id: state.player.id,
          game,
          focus: radio("focus"),
          length: Number(radio("length")),
          fmt: radio("fmt"),
          video_id: videoId,
        }),
      });
      poll(job_id);
    } catch (e) {
      finishWithError(e.message, []);
    }
  }

  function setProgress(pct, msg) {
    $("progress").value = pct;
    $("progress-text").textContent = msg;
  }

  async function poll(jobId) {
    try {
      const job = await getJSON(`/api/jobs/${jobId}`);
      setProgress(job.progress, job.message);
      if (job.status === "done") return showResult(job.result);
      if (job.status === "error") return finishWithError(job.error, job.candidates);
    } catch (e) {
      // network hiccup: keep polling
    }
    state.pollTimer = setTimeout(() => poll(jobId), 1000);
  }

  function endBusy() {
    state.busy = false;
    $("step-result").classList.remove("busy");
    updateMakeButton();
  }

  function showResult(r) {
    endBusy();
    $("progress-wrap").hidden = true;
    const video = $("video");
    video.src = r.video_url + "?t=" + Date.now();
    $("result").classList.toggle("vertical", r.fmt === "vertical");
    $("download-link").href = r.download_url;
    $("download-link").setAttribute("download", r.download_name);
    $("reel-note").textContent = r.note || "";
    $("play-list").replaceChildren(...(r.plays || []).map((p) =>
      el("li", {},
        el("span", { className: "play-when", textContent: p.label }),
        el("span", { className: "play-kind", textContent: p.kind }),
        el("span", { className: "play-desc", textContent: p.desc }))));
    $("play-list").hidden = !(r.plays || []).length;
    const links = (r.sources || []).map((s) =>
      el("a", { href: s.url, target: "_blank", rel: "noopener",
                textContent: s.channel ? `${s.title} (${s.channel})` : s.title }));
    $("source-links").replaceChildren(...links.flatMap((a, i) => (i ? [" · ", a] : [a])));
    $("result").hidden = false;
    showAlternatives(r.candidates, "Wrong footage? Try another source:");
    video.play().catch(() => {});
  }

  function finishWithError(message, candidates) {
    endBusy();
    $("progress-wrap").hidden = true;
    $("error").textContent = message;
    $("error").hidden = false;
    showAlternatives(candidates, "These videos came up in the search. Pick one if it's the right game:");
  }

  function showAlternatives(candidates, title) {
    const box = $("alternatives");
    const ul = $("alt-list");
    ul.replaceChildren();
    (candidates || []).forEach((c) => {
      const btn = el("button", { type: "button", className: "alt" },
        el("span", { className: "alt-title", textContent: c.title }),
        el("small", { textContent: [c.channel, fmtDuration(c.duration)].filter(Boolean).join(" · ") }));
      btn.addEventListener("click", () => makeReel(c.id));
      const link = el("a", { href: c.url, target: "_blank", rel: "noopener", textContent: "watch on YouTube" });
      ul.append(el("li", {}, btn, " ", link));
    });
    $("alt-title").textContent = title;
    box.hidden = !ul.children.length;
  }
})();
