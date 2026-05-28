/**
 * script.js — FactCheck Real-Time
 *
 * Fluxo de 3 ecrãs:
 *   Ecrã 1 (Input)         → utilizador cola URL e clica "Iniciar"
 *   Ecrã 2 (Pré-processo)  → backend analisa TUDO; frontend acumula resultados
 *   Ecrã 3 (Player)        → vídeo inicia; cartões aparecem sincronizados (Timeline Dinâmica)
 */

"use strict";

// ─── Estado global ────────────────────────────────────────────────────────────
const state = {
  ws:              null,
  videoId:         null,
  videoDuration:   0,
  allClaims:       [],   // Guarda TODAS as verificações recebidas
  stats:           { verdadeiro: 0, falso: 0, maisOuMenos: 0 },
  prepStats:       { verdadeiro: 0, falso: 0, maisOuMenos: 0, total: 0 },
  analysisProgressPct: 0,
  earlyTransition: false,
  prepTotalChunks: 0,
  prepCompletedChunks: 0,
  prepActiveChecks: 0,
};

let ytPlayer         = null;
let ytApiReady       = false;
let syncInterval     = null;
let prepStartTime    = null;
let prepTimerInterval = null;
let videoStartWall   = null;   // Date.now() quando o vídeo começou (fallback de sync)

// ─── YouTube IFrame API ───────────────────────────────────────────────────────

window.onYouTubeIframeAPIReady = function () {
  console.log("YouTube IFrame API Ready");
  ytApiReady = true;
  // Se já estamos no ecrã do player, inicializa agora
  if (state.videoId && document.getElementById("mainLayout").style.display === "flex") {
    _createYTPlayer(state.videoId);
  }
};

function _createYTPlayer(videoId) {
  if (!videoId) return;
  console.log("A inicializar YT.Player para:", videoId);

  // Se já existe um player, destrói-o para evitar conflitos
  try {
    if (ytPlayer && typeof ytPlayer.destroy === "function") {
      ytPlayer.destroy();
    }
  } catch (e) {
    console.warn("Erro ao destruir player antigo:", e);
  }
  ytPlayer = null;

  // Garante que o contentor está limpo e tem o div com o ID correto
  const wrapper = document.querySelector(".video-wrapper");
  if (wrapper) {
    wrapper.innerHTML = '<div id="ytPlayer"></div>';
  }

  // Verifica se a API está realmente disponível
  if (typeof YT === "undefined" || !YT.Player) {
    console.error("YT.Player não encontrado mesmo com API pronta.");
    return;
  }

  ytPlayer = new YT.Player("ytPlayer", {
    videoId: videoId,
    height: "100%",
    width:  "100%",
    playerVars: { 
      autoplay: 1, 
      rel: 0, 
      modestbranding: 1,
      origin: window.location.origin
    },
    events: {
      onReady: (e) => {
        console.log("YouTube Player Pronto");
        e.target.playVideo();
        videoStartWall = Date.now();
        _startSyncLoop();
      },
      onStateChange: (e) => {
        // Recalibra o relógio interno sempre que o estado muda (play, pause, seek)
        const ytTime = ytPlayer.getCurrentTime();
        videoStartWall = Date.now() - ytTime * 1000;
        console.log("Estado YT mudou:", e.data, "Tempo atual:", ytTime);
      },
      onError: (e) => {
        console.error("Erro no player do YouTube:", e.data);
        alert("Erro ao carregar o vídeo. Verifique se o vídeo permite reprodução externa.");
      }
    },
  });

  // Fallback para o loop de sincronização caso o onReady demore (ex: autoplay bloqueado)
  setTimeout(() => {
    if (!syncInterval) {
      console.log("A iniciar loop de sincronização via fallback...");
      videoStartWall = Date.now();
      _startSyncLoop();
    }
  }, 3000);
}

// ─── Loop de sincronização (Timeline Dinâmica) ────────────────────────────────

function _startSyncLoop() {
  clearInterval(syncInterval);
  
  syncInterval = setInterval(() => {
    const t = _getCurrentVideoTime();
    if (t < 0) return;

    let currentStats = { verdadeiro: 0, falso: 0, maisOuMenos: 0 };
    let lastVisibleCard = null;

    // Percorre TODOS os cartões que estão na página
    document.querySelectorAll(".claim-card").forEach(card => {
      const cardTs = parseFloat(card.dataset.timestamp);
      const isVisible = card.style.display !== "none";

      if (cardTs <= t + 0.3) {
        // Se o vídeo já passou deste timestamp, MOSTRA o cartão
        if (!isVisible) {
          card.style.display = "flex"; // USAR FLEX PARA BATER COM CSS
          lastVisibleCard = card;
        }
        
        const v = card.dataset.verdict;
        if (v === "verdadeiro") currentStats.verdadeiro++;
        else if (v === "falso") currentStats.falso++;
        else currentStats.maisOuMenos++;

      } else {
        // Se o utilizador andou com o vídeo para TRÁS deste tempo, ESCONDE
        if (isVisible) {
          card.style.display = "none";
        }
      }
    });

    // Faz scroll suave apenas se um novo cartão acabou de aparecer na timeline
    if (lastVisibleCard) {
        lastVisibleCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    // Atualiza as estatísticas do painel para corresponderem APENAS ao que está visível
    document.getElementById("countTrue").textContent  = currentStats.verdadeiro;
    document.getElementById("countFalse").textContent = currentStats.falso;
    document.getElementById("countMixed").textContent = currentStats.maisOuMenos;

    // Atualiza o banner da fila (quantos faltam aparecer)
    const pending = state.allClaims.filter(c => c.timestamp > t + 0.3);
    _updateQueueBanner(pending);

  }, 250); // Corre 4 vezes por segundo
}

/**
 * Lê o tempo actual do vídeo.
 */
function _getCurrentVideoTime() {
  try {
    if (ytPlayer && typeof ytPlayer.getCurrentTime === "function") {
      const t = ytPlayer.getCurrentTime();
      if (t >= 0) return t;
    }
  } catch (_) {}
  if (videoStartWall !== null) {
    return (Date.now() - videoStartWall) / 1000;
  }
  return -1;
}

// ─── Entrada ──────────────────────────────────────────────────────────────────

function startFactCheck() {
  const input = document.getElementById("youtubeUrl");
  const url   = input.value.trim();
  if (!url) { shakeInput(input); return; }

  const videoId = extractVideoId(url);
  if (!videoId) { shakeInput(input); showInputError("URL inválido."); return; }

  state.videoId  = videoId;
  prepStartTime  = Date.now();
  resetState();
  _startPrepTimer();

  _showScreen("prep");
  connectWebSocket(url);
}

// ─── WebSocket ────────────────────────────────────────────────────────────────

function connectWebSocket(url) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  state.ws = new WebSocket(`${proto}//${location.host}/ws/factcheck`);

  state.ws.onopen    = () => state.ws.send(JSON.stringify({ url }));
  state.ws.onmessage = (e) => { try { handleServerMessage(JSON.parse(e.data)); } catch {} };
  state.ws.onerror   = () => _showPrepError("Erro de ligação. Verifique se o backend está activo.");
  state.ws.onclose   = () => {};
}

// ─── Dispatcher ───────────────────────────────────────────────────────────────

function handleServerMessage(msg) {
  switch (msg.type) {
    case "status":
      _setPrepSubtitle(msg.message);
      _handlePrepStatus(msg);
      break;

    case "ready":
      state.videoDuration = msg.video_duration || 0;
      state.prepTotalChunks = msg.total_chunks || 0;
      _setPrepWorkflowState("transcript", "done", `${msg.total_segments || 0} linhas`);
      _setPrepWorkflowState("extract", "active", `${state.prepTotalChunks} segmentos`);
      _setPrepWorkflowState("verify", "", "A aguardar");
      _setPrepWorkflowState("finish", "", "A aguardar");
      _setPrepMetric("prepChunksDone", `0/${state.prepTotalChunks}`);
      _setPrepCurrentAction("Transcrição pronta. A procurar afirmações verificáveis.");
      _setPrepCurrentSegment("A aguardar o primeiro segmento");
      _setPrepSubtitle(`Transcrição extraída. A analisar ${msg.total_chunks} segmentos…`);
      break;

    case "fact_check":
      // Guarda SEMPRE na lista global
      state.allClaims.push(msg);
      state.allClaims.sort((a, b) => a.timestamp - b.timestamp);
      _updatePrepStats(msg.verdict);
      _finishPrepCheck();
      _addPrepFeedItem(msg);
      _setPrepWorkflowState("verify", "active", `${state.prepStats.total} verificadas`);
      _setPrepCurrentAction("Resultado pronto. A continuar a análise.");
      _maybeTransitionToPlayerEarly();
      
      // Se o player já estiver visível, renderiza o cartão (escondido) para o loop o apanhar
      if (document.getElementById("mainLayout").style.display !== "none") {
          renderClaimCard(msg);
          _addTimelineMarkerOnly(msg);
      }
      break;

    case "progress":
      _updatePrepProgress(msg.current, msg.total, msg.status);
      break;

    case "checking":
      _startPrepCheck();
      _setPrepWorkflowState("extract", "done", "Afirmações encontradas");
      _setPrepWorkflowState("verify", "active", "Pesquisa e classificação");
      _setPrepCurrentClaim(`[${formatTimestamp(msg.timestamp || 0)}] ${msg.claim}`);
      _setPrepSubtitle(`A verificar ${formatTimestamp(msg.timestamp || 0)} · "${msg.claim.substring(0, 70)}…"`);
      break;

    case "complete":
      if (prepTimerInterval) { clearInterval(prepTimerInterval); prepTimerInterval = null; }
      _updatePrepElapsed();
      _setPrepWorkflowState("extract", "done", "Concluído");
      _setPrepWorkflowState("verify", "done", `${state.prepStats.total} verificadas`);
      _setPrepWorkflowState("finish", "done", "Pronto");
      _setPrepCurrentAction(msg.message || "Análise concluída.");
      _setPrepCurrentClaim("Todas as afirmações foram tratadas");
      if (document.getElementById("mainLayout").style.display !== "none") {
        // Player já visível (transição antecipada) — só esconde a barra de fundo
        const bgBar = document.getElementById("bgProcessingBar");
        if (bgBar) bgBar.style.display = "none";
      } else {
        _transitionToPlayer(false);
      }
      break;

    case "error":
      _showPrepError(msg.message);
      break;
  }
}

// ─── Ecrã de Pré-processamento ───────────────────────────────────────────────

function _setPrepSubtitle(text) {
  document.getElementById("prepSubtitle").textContent = text;
}

function _handlePrepStatus(msg) {
  const message = msg.message || "";
  if (msg.stage === "transcript" || message.includes("transcrição") || message.includes("metadados")) {
    _setPrepWorkflowState("transcript", "active", "A obter dados");
    _setPrepCurrentAction(message);
    return;
  }

  if (typeof msg.chunk_index === "number") {
    const total = msg.total_chunks || state.prepTotalChunks || 0;
    const label = `Segmento ${msg.chunk_index + 1}/${total} · ${formatTimestamp(msg.timestamp || 0)}`;
    _setPrepWorkflowState("extract", "active", label);
    _setPrepCurrentAction("A extrair afirmações verificáveis.");
    _setPrepCurrentSegment(label);
    return;
  }

  if (message) _setPrepCurrentAction(message);
}

function _setPrepWorkflowState(step, status, meta = "") {
  const ids = {
    transcript: ["prepStepTranscript", "prepStepTranscriptMeta"],
    extract: ["prepStepExtract", "prepStepExtractMeta"],
    verify: ["prepStepVerify", "prepStepVerifyMeta"],
    finish: ["prepStepFinish", "prepStepFinishMeta"],
  };
  const pair = ids[step];
  if (!pair) return;
  const el = document.getElementById(pair[0]);
  const metaEl = document.getElementById(pair[1]);
  if (!el) return;
  el.classList.remove("active", "done");
  if (status) el.classList.add(status);
  if (metaEl && meta) metaEl.textContent = meta;
}

function _setPrepMetric(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function _setPrepCurrentAction(text) {
  const el = document.getElementById("prepCurrentAction");
  if (el) el.textContent = text || "A processar...";
}

function _setPrepCurrentSegment(text) {
  const el = document.getElementById("prepCurrentSegment");
  if (el) el.textContent = text || "Ainda não iniciado";
}

function _setPrepCurrentClaim(text) {
  const el = document.getElementById("prepCurrentClaim");
  if (el) el.textContent = text || "Nenhuma em verificação";
}

function _startPrepCheck() {
  state.prepActiveChecks++;
  const suffix = state.prepActiveChecks > 1 ? `${state.prepActiveChecks} em curso` : "1 em curso";
  _setPrepWorkflowState("verify", "active", suffix);
}

function _finishPrepCheck() {
  state.prepActiveChecks = Math.max(0, state.prepActiveChecks - 1);
  _setPrepMetric("prepVerifiedCount", state.prepStats.total);
}

function _startPrepTimer() {
  clearInterval(prepTimerInterval);
  _updatePrepElapsed();
  prepTimerInterval = setInterval(_updatePrepElapsed, 1000);
}

function _updatePrepElapsed() {
  if (!prepStartTime) return;
  const elapsed = Math.floor((Date.now() - prepStartTime) / 1000);
  _setPrepMetric("prepElapsedTime", elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : `${elapsed}s`);
}

function _updatePrepProgress(current, total, statusText) {
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  state.analysisProgressPct = pct;
  state.prepCompletedChunks = current;
  state.prepTotalChunks = total;
  document.getElementById("prepProgressBar").style.width = `${pct}%`;
  document.getElementById("prepProgressPct").textContent = `${pct}%`;
  _setPrepMetric("prepChunksDone", `${current}/${total}`);
  _setPrepWorkflowState("extract", current >= total ? "done" : "active", `${current}/${total} segmentos`);

  if (prepStartTime && current > 1) {
    const elapsed  = (Date.now() - prepStartTime) / 1000;
    const rate     = current / elapsed;
    const remaining = Math.ceil((total - current) / rate);
    document.getElementById("prepTimeEstimate").textContent =
      remaining > 60
        ? `~${Math.ceil(remaining / 60)} min restantes`
        : `~${remaining}s restantes`;
  }
  if (statusText) _setPrepSubtitle(statusText);

  // Mantém a barra de segundo plano coerente caso o player já esteja visível.
  const bgFill = document.getElementById("bgProgressFill");
  const bgPct  = document.getElementById("bgProgressPct");
  if (bgFill) bgFill.style.width = `${pct}%`;
  if (bgPct)  bgPct.textContent  = `${pct}%`;
  if (statusText) {
    _setPrepCurrentAction(statusText);
    _setPrepCurrentSegment(`${current}/${total} segmentos concluídos`);
  }

  _maybeTransitionToPlayerEarly();
}

function _maybeTransitionToPlayerEarly() {
  const playerVisible = document.getElementById("mainLayout").style.display !== "none";
  const hasVerifiedClaim = state.prepStats.total > 0;

  if (
    state.analysisProgressPct >= 50 &&
    hasVerifiedClaim &&
    !state.earlyTransition &&
    !playerVisible
  ) {
    state.earlyTransition = true;
    _transitionToPlayer(true);
  }
}

function _updatePrepStats(verdict) {
  state.prepStats.total++;
  const v = verdict.toLowerCase();
  if (v === "verdadeiro") state.prepStats.verdadeiro++;
  else if (v === "falso") state.prepStats.falso++;
  else state.prepStats.maisOuMenos++;

  document.getElementById("prepCountTrue").textContent  = state.prepStats.verdadeiro;
  document.getElementById("prepCountFalse").textContent = state.prepStats.falso;
  document.getElementById("prepCountMixed").textContent = state.prepStats.maisOuMenos;
  document.getElementById("prepCountTotal").textContent = state.prepStats.total;
}

function _addPrepFeedItem(claim) {
  const feed = document.getElementById("prepFeed");
  const empty = feed.querySelector(".prep-feed-empty");
  if (empty) empty.remove();

  const cls  = getVerdictClass(claim.verdict);
  const item = document.createElement("div");
  item.className = `prep-item ${cls}`;
  item.dataset.timestamp = claim.timestamp;
  item.innerHTML = `
    <span class="prep-item-ts">${formatTimestamp(claim.timestamp)}</span>
    <span class="prep-item-verdict ${cls}">${claim.verdict}</span>
    <span class="prep-item-claim">${escapeHtml(claim.claim.substring(0, 80))}${claim.claim.length > 80 ? "…" : ""}</span>
  `;
  const items = [...feed.querySelectorAll(".prep-item")];
  const after = items.find(el => parseFloat(el.dataset.timestamp) > claim.timestamp);
  after ? feed.insertBefore(item, after) : feed.appendChild(item);
  feed.scrollTop = 0;
}

function _showPrepError(msg) {
  document.getElementById("prepTitle").textContent = "Erro";
  _setPrepSubtitle(msg);
  document.getElementById("prepProgressBar").style.background = "var(--false-dot)";
}

/**
 * Transição para o player.
 * @param {boolean} isEarly - true quando chamada aos 50% (processamento continua em fundo)
 */
function _transitionToPlayer(isEarly = false) {
  console.log("Transição iniciada para o player. VideoID:", state.videoId, "Antecipada:", isEarly);
  _showScreen("player");

  const emptyState = document.getElementById("emptyState");
  if (emptyState) emptyState.remove();

  // Renderiza os cartões já recebidos (escondidos por defeito) e marcadores da timeline
  state.allClaims.forEach(c => {
    renderClaimCard(c);
    _addTimelineMarkerOnly(c);
  });

  const liveDot = document.getElementById("liveDot");
  if (liveDot) liveDot.classList.add("pulsing");

  // Mostra barra de progresso de segundo plano se ainda a analisar
  const bgBar = document.getElementById("bgProcessingBar");
  if (bgBar) bgBar.style.display = isEarly ? "flex" : "none";

  setTimeout(() => {
    if (state.videoId && typeof YT !== "undefined" && YT.Player) {
      _createYTPlayer(state.videoId);
    }
  }, 100);
}

// ─── Navegação entre ecrãs ───────────────────────────────────────────────────

function _showScreen(screen) {
  document.getElementById("inputSection").style.display  = screen === "input"  ? "flex"  : "none";
  document.getElementById("prepSection").style.display   = screen === "prep"   ? "flex"  : "none";
  document.getElementById("mainLayout").style.display    = screen === "player" ? "flex"  : "none";
}

// ─── Renderização do cartão ───────────────────────────────────────────────────

function renderClaimCard(claim) {
  const feed = document.getElementById("claimsFeed");
  const emptyState = document.getElementById("emptyState");
  if (emptyState) emptyState.remove();

  // Evita duplicados
  if (feed.querySelector(`.claim-card[data-timestamp="${claim.timestamp}"]`)) return;

  const cls        = getVerdictClass(claim.verdict);
  const confidence = Math.round((claim.confidence || 0.5) * 100);
  const ctxHtml    = buildContextHtml(claim.context_used, claim.sources, claim.search_type);

  const card = document.createElement("div");
  card.className = `claim-card ${cls}`;
  card.dataset.timestamp = claim.timestamp;
  card.dataset.verdict = claim.verdict.toLowerCase(); // Guarda o veredicto para o loop ler
  card.style.display = "none"; // IMPORTANTE: Entra sempre ESCONDIDO!

  card.innerHTML = `
    <div class="claim-card-header">
      <span class="verdict-badge ${cls}">
        <svg class="verdict-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.5">
          ${getVerdictSVG(claim.verdict)}
        </svg>
        ${claim.verdict}
      </span>
      <span class="claim-timestamp" onclick="seekToTime(${claim.timestamp})">
        ⏱ ${formatTimestamp(claim.timestamp)}
      </span>
    </div>
    <p class="claim-text">"${escapeHtml(claim.claim)}"</p>
    <p class="claim-justification">${escapeHtml(claim.justification)}</p>
    <div class="confidence-row">
      <div class="confidence-track">
        <div class="confidence-fill" style="width:${confidence}%"></div>
      </div>
      <span class="confidence-label">Confiança: ${confidence}%</span>
    </div>
    ${ctxHtml}
  `;

  const cards = [...feed.querySelectorAll(".claim-card")];
  const after = cards.find(c => parseFloat(c.dataset.timestamp) > claim.timestamp);
  after ? feed.insertBefore(card, after) : feed.appendChild(card);
}

// ─── Timeline ─────────────────────────────────────────────────────────────────

function _addTimelineMarkerOnly(claim) {
  const track = document.getElementById("timelineTrack");
  if (!track || !claim.timestamp) return;
  if (track.querySelector(`[data-ts="${claim.timestamp}"]`)) return;

  const pct = state.videoDuration > 0 ? (claim.timestamp / state.videoDuration) * 100 : 0;
  const m = document.createElement("div");
  m.className = `timeline-marker ${getVerdictClass(claim.verdict)}`;
  m.style.left = `${Math.min(Math.max(pct, 0.5), 99.5)}%`;
  m.dataset.ts = claim.timestamp;
  m.title = `[${formatTimestamp(claim.timestamp)}] ${claim.claim.substring(0, 60)}…`;
  m.addEventListener("click", () => { seekToTime(claim.timestamp); highlightCard(claim.timestamp); });
  track.appendChild(m);
}

// ─── Acções do utilizador ────────────────────────────────────────────────────

function seekToTime(seconds) {
  if (ytPlayer && typeof ytPlayer.seekTo === "function") {
    ytPlayer.seekTo(Math.floor(seconds), true);
    ytPlayer.playVideo();
  }
}



function highlightCard(timestamp) {
  const card = document.querySelector(`.claim-card[data-timestamp="${timestamp}"]`);
  if (card) {
    card.style.display = "flex"; // Garante que fica visível se o utilizador clicar na timeline
    document.querySelectorAll(".claim-card.highlighted").forEach(c => c.classList.remove("highlighted"));
    card.classList.add("highlighted");
    card.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function resetApp() {
  if (state.ws)       { state.ws.close(); state.ws = null; }
  if (syncInterval)   { clearInterval(syncInterval); syncInterval = null; }
  if (ytPlayer)       { ytPlayer.destroy(); ytPlayer = null; }
  resetState();
  state.videoId  = null;
  prepStartTime  = null;
  _showScreen("input");
}

// ─── Utilitários de estado ────────────────────────────────────────────────────

function _updateQueueBanner(pending = []) {
  const banner = document.getElementById("queueBanner");
  if (!banner) return;
  if (pending.length === 0) {
    banner.style.display = "none";
    return;
  }
  pending.sort((a, b) => a.timestamp - b.timestamp);
  banner.style.display = "flex";
  document.getElementById("queueCount").textContent = pending.length;
  document.getElementById("queueNext").textContent = formatTimestamp(pending[0].timestamp);
}

function resetState() {
  state.allClaims = [];
  state.videoDuration = 0;
  state.stats     = { verdadeiro: 0, falso: 0, maisOuMenos: 0 };
  state.prepStats = { verdadeiro: 0, falso: 0, maisOuMenos: 0, total: 0 };
  state.analysisProgressPct = 0;
  state.earlyTransition = false;
  state.prepTotalChunks = 0;
  state.prepCompletedChunks = 0;
  state.prepActiveChecks = 0;

  ["countTrue","countFalse","countMixed"].forEach(id => {
    const el = document.getElementById(id); if (el) el.textContent = "0";
  });
  ["prepCountTrue","prepCountFalse","prepCountMixed","prepCountTotal"].forEach(id => {
    const el = document.getElementById(id); if (el) el.textContent = "0";
  });

  const feed = document.getElementById("claimsFeed");
  if (feed) feed.innerHTML = `
    <div class="empty-state" id="emptyState">
      <svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="1.5" class="empty-icon">
        <circle cx="32" cy="32" r="28"/>
        <path d="M20 32h24M32 20v24" stroke-linecap="round"/>
      </svg>
      <p>As verificações aparecem aqui sincronizadas com o vídeo.</p>
    </div>`;

  const pf = document.getElementById("prepFeed");
  if (pf) pf.innerHTML = `<p class="prep-feed-empty">As afirmações encontradas aparecem aqui…</p>`;

  const pb = document.getElementById("prepProgressBar");
  if (pb) { pb.style.width = "0%"; pb.style.background = ""; }
  const ptt = document.getElementById("prepTitle");
  if (ptt) ptt.textContent = "A analisar o debate…";

  const track = document.getElementById("timelineTrack");
  if (track) [...track.querySelectorAll(".timeline-marker")].forEach(m => m.remove());

  const qb = document.getElementById("queueBanner");
  if (qb) qb.style.display = "none";

  const ld = document.getElementById("liveDot");
  if (ld) ld.classList.remove("pulsing");

  const bgBar = document.getElementById("bgProcessingBar");
  if (bgBar) bgBar.style.display = "none";

  if (syncInterval) { clearInterval(syncInterval); syncInterval = null; }
  if (prepTimerInterval) { clearInterval(prepTimerInterval); prepTimerInterval = null; }

  _setPrepMetric("prepChunksDone", "0/0");
  _setPrepMetric("prepVerifiedCount", "0");
  _setPrepMetric("prepElapsedTime", "0s");
  _setPrepWorkflowState("transcript", "active", "A iniciar");
  _setPrepWorkflowState("extract", "", "A aguardar");
  _setPrepWorkflowState("verify", "", "A aguardar");
  _setPrepWorkflowState("finish", "", "A aguardar");
  _setPrepCurrentAction("A preparar ligação...");
  _setPrepCurrentSegment("Ainda não iniciado");
  _setPrepCurrentClaim("Nenhuma em verificação");
}

// ─── Contexto RAG ────────────────────────────────────────────────────────────

function buildContextHtml(contextUsed, sources, searchType) {
  const isWeb = searchType === "web";
  const icon  = isWeb ? "🌐" : "🔎";
  const label = isWeb ? "Pesquisa Web (DuckDuckGo)" : "Pesquisa externa";

  const linksHtml = (sources || []).map(s =>
    s.startsWith("http")
      ? `<a href="${escapeHtml(s)}" target="_blank" class="source-link">${escapeHtml(_domainOnly(s))}</a>`
      : `<span class="source-tag">${escapeHtml(s)}</span>`
  ).join("");

  if (!contextUsed || !contextUsed.length) {
    return `<div class="rag-sources-row">${icon} ${label} ${linksHtml}</div>`;
  }

  const excerpts = contextUsed
    .map(c => `<blockquote class="rag-excerpt">${escapeHtml(c.substring(0, 260))}${c.length > 260 ? "…" : ""}</blockquote>`)
    .join("");

  return `
    <div class="rag-context">
      <button class="rag-toggle" onclick="toggleContext(this)">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" class="rag-toggle-icon">
          <polyline points="4 6 8 10 12 6"/>
        </svg>
        ${icon} Ver contexto (${label})
      </button>
      <div class="rag-body" style="display:none">
        ${excerpts}
        ${linksHtml ? `<div class="rag-links">${linksHtml}</div>` : ""}
      </div>
    </div>`;
}

function toggleContext(btn) {
  const body = btn.nextElementSibling;
  const open = body.style.display === "none";
  body.style.display = open ? "block" : "none";
  btn.querySelector(".rag-toggle-icon").style.transform = open ? "rotate(180deg)" : "";
}

function _domainOnly(url) {
  try { return new URL(url).hostname.replace("www.", ""); } catch { return url.substring(0, 40); }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function extractVideoId(url) {
  const m = url.match(/(?:v=|\/videos\/|embed\/|youtu\.be\/|\/v\/|watch\?v=)([0-9A-Za-z_-]{11})/);
  return m ? m[1] : null;
}

function formatTimestamp(s) {
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2,"0")}:${String(sec).padStart(2,"0")}`
    : `${m}:${String(sec).padStart(2,"0")}`;
}

function getVerdictClass(v) {
  v = (v||"").toLowerCase();
  return v === "verdadeiro" ? "verdict-true" : v === "falso" ? "verdict-false" : "verdict-mixed";
}

function getVerdictSVG(v) {
  v = (v||"").toLowerCase();
  if (v === "verdadeiro") return '<polyline points="2 8 6 12 14 4"/>';
  if (v === "falso")      return '<line x1="2" y1="2" x2="14" y2="14"/><line x1="14" y1="2" x2="2" y2="14"/>';
  return '<line x1="2" y1="8" x2="14" y2="8"/>';
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function shakeInput(el) {
  el.style.animation = "none"; el.offsetHeight;
  el.style.animation = "shake 0.3s ease";
  el.style.borderColor = "var(--false-border)";
  setTimeout(() => { el.style.borderColor = ""; el.style.animation = ""; }, 600);
}

function showInputError(msg) {
  const el = document.getElementById("youtubeUrl");
  el.placeholder = msg;
  setTimeout(() => { el.placeholder = "https://www.youtube.com/watch?v=..."; }, 3000);
}

// ─── Teclado ─────────────────────────────────────────────────────────────────

document.addEventListener("keydown", e => {
  if (e.key === "Enter" && document.activeElement.id === "youtubeUrl") startFactCheck();
  if (e.key === "Escape") resetApp();
});

// ─── Redimensionamento de Painéis ───────────────────────────────────────────

function _initResizers() {
  // Utilitário: configura um resizer com pointer capture para não perder eventos
  function _makeResizer(el, onMove, cursor) {
    if (!el) return;
    el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");
      document.body.style.userSelect = "none";
      document.body.style.cursor = cursor;

      const onUp = () => {
        el.releasePointerCapture(e.pointerId);
        el.classList.remove("dragging");
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
      };

      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
    });
  }

  // 1. Resizer Horizontal (Largura dos painéis)
  const leftPanel = document.querySelector(".panel-left");
  _makeResizer(
    document.getElementById("mainResizer"),
    (e) => {
      const newWidth = e.clientX;
      if (newWidth >= 300 && newWidth <= 900) leftPanel.style.width = `${newWidth}px`;
    },
    "col-resize"
  );

  // 2. Resizer Vertical (Altura Timeline)
  const timelineSection = document.querySelector(".timeline-section");
  const panelRight = document.querySelector(".panel-right");
  _makeResizer(
    document.getElementById("timelineResizer"),
    (e) => {
      if (!panelRight || !timelineSection) return;
      const pr = panelRight.getBoundingClientRect();
      const newH = Math.max(70, Math.min(pr.height - 160, pr.bottom - e.clientY));
      timelineSection.style.height = `${newH}px`;
      timelineSection.style.flexShrink = "0";
    },
    "row-resize"
  );
}

// Inicializa quando o DOM estiver pronto
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _initResizers);
} else {
  _initResizers();
}

// ─── CSS injectado ───────────────────────────────────────────────────────────

const _s = document.createElement("style");
_s.textContent = `@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-6px)}40%{transform:translateX(6px)}60%{transform:translateX(-4px)}80%{transform:translateX(4px)}}`;
document.head.appendChild(_s);
