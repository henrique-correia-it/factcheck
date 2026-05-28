"""
main.py
-------
Servidor principal da aplicação FactCheck.

Usa FastAPI como framework web com suporte a WebSockets para comunicação
bidirecional em tempo real entre o backend e o frontend.

Fluxo de uma sessão:
  1. Cliente abre WebSocket em /ws/factcheck
  2. Cliente envia {"url": "https://youtube.com/watch?v=..."}
  3. Backend extrai transcrição e envia actualizações de status
  4. Backend processa chunks de transcrição: extrai claims → RAG → classificação
  5. Backend envia cada verificação em tempo real para o cliente
  6. Frontend exibe os resultados sincronizados com o vídeo
"""

import asyncio
import json
import logging
import os
import re
import difflib
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Módulos do projecto
from transcript_handler import extract_video_id, get_transcript, get_video_metadata
from claim_extractor import ClaimExtractor
from rag_pipeline import FactCheckRAG
from analysis_log import AnalysisRunLogger

# --- Configuração ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Silenciar logs ruidosos de bibliotecas externas
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("ddgs").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "5"))  # segmentos de transcrição por chunk
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", str(max(1, min(5, CHUNK_SIZE // 5)))))
FACT_CHECK_CONCURRENCY = int(os.getenv("FACT_CHECK_CONCURRENCY", "4"))
CHUNK_CONCURRENCY = int(os.getenv("CHUNK_CONCURRENCY", "2"))

# --- Inicialização da aplicação ---
app = FastAPI(
    title="FactCheck Real-Time API",
    description="API de verificação de factos em tempo real para debates do YouTube.",
    version="1.0.0",
)

# CORS: permite que qualquer origem aceda à API (útil em desenvolvimento)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Serve os ficheiros estáticos do frontend ---
frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

# --- Inicialização dos componentes de IA ---
# São inicializados uma vez ao arrancar o servidor para reutilização
logger.info("A inicializar o motor RAG e o extractor de afirmações...")
rag_engine = FactCheckRAG()
claim_extractor = ClaimExtractor()
logger.info("Componentes de IA prontos.")


# ======================================================================
# ROTAS HTTP
# ======================================================================


@app.get("/", response_class=FileResponse)
async def serve_frontend():
    """Serve a página principal do frontend."""
    return FileResponse(str(frontend_dir / "index.html"))


@app.get("/health")
async def health_check():
    """Endpoint de verificação de saúde do servidor."""
    return {"status": "ok", "version": "1.0.0"}


# ======================================================================
# WEBSOCKET ENDPOINT
# ======================================================================


@app.websocket("/ws/factcheck")
async def websocket_factcheck(websocket: WebSocket):
    """
    WebSocket principal para o pipeline de fact-checking.

    Protocolo de mensagens (JSON):

    Cliente → Servidor:
      {"url": "https://www.youtube.com/watch?v=VIDEO_ID"}

    Servidor → Cliente:
      {"type": "status",     "message": "...", "video_id": "..."}
      {"type": "ready",      "total_chunks": N, "video_duration": T}
      {"type": "fact_check", "timestamp": T, "claim": "...", "verdict": "...",
                             "justification": "...", "confidence": 0.0, "sources": [...]}
      {"type": "progress",   "current": N, "total": M, "timestamp": T}
      {"type": "complete",   "message": "..."}
      {"type": "error",      "message": "..."}
    """
    await websocket.accept()
    logger.info("Novo cliente WebSocket ligado.")
    analysis_log = None

    try:
        # ----------------------------------------------------------------
        # PASSO 1 — Receber o URL do YouTube do cliente
        # ----------------------------------------------------------------
        raw_data = await websocket.receive_text()
        payload = json.loads(raw_data)
        url = payload.get("url", "").strip()

        if not url:
            await _send(websocket, {"type": "error", "message": "URL não fornecido."})
            return

        # Extrai e valida o video ID
        try:
            video_id = extract_video_id(url)
        except ValueError as err:
            await _send(websocket, {"type": "error", "message": str(err)})
            return

        logger.info(f"A processar vídeo: {video_id}")
        analysis_started_at = time.perf_counter()
        analysis_log = AnalysisRunLogger(video_id=video_id, url=url)
        analysis_log.log(
            "REQUEST_RECEIVED",
            url=url,
            video_id=video_id,
            config={
                "CHUNK_SIZE": CHUNK_SIZE,
                "CHUNK_OVERLAP": CHUNK_OVERLAP,
                "CHUNK_CONCURRENCY": CHUNK_CONCURRENCY,
                "FACT_CHECK_CONCURRENCY": FACT_CHECK_CONCURRENCY,
            },
        )

        # ----------------------------------------------------------------
        # PASSO 2 — Extrair metadados e transcrição
        # ----------------------------------------------------------------
        await _send(websocket, {
            "type": "status",
            "stage": "transcript",
            "message": "A obter metadados e transcrição...",
            "video_id": video_id,
        })

        try:
            # Extrai metadados (título, data)
            metadata = await asyncio.to_thread(get_video_metadata, url)
            v_date = metadata.get("upload_date", "")
            if len(v_date) == 8:
                fmt_date = f"{v_date[6:8]}/{v_date[4:6]}/{v_date[0:4]}"
            else:
                fmt_date = "Desconhecida"
            
            video_context = f"Título: {metadata.get('title')}, Data: {fmt_date}"
            logger.info(f"Contexto: {video_context}")
            analysis_log.log(
                "VIDEO_METADATA",
                metadata=metadata,
                formatted_date=fmt_date,
                video_context=video_context,
            )

            transcript = await asyncio.to_thread(get_transcript, url)
            analysis_log.log(
                "TRANSCRIPT_OBTAINED",
                segment_count=len(transcript),
                transcript=transcript,
            )
        except Exception as err:
            if analysis_log:
                analysis_log.log("EXTRACTION_ERROR", error=repr(err))
            await _send(websocket, {"type": "error", "message": f"Erro na extração: {err}"})
            return

        if not transcript:
            analysis_log.log("EMPTY_TRANSCRIPT")
            await _send(websocket, {
                "type": "error",
                "message": "Transcrição vazia. O vídeo pode não ter legendas.",
            })
            return

        # Agrupa segmentos em chunks para processamento
        chunks = _group_into_chunks(transcript, CHUNK_SIZE)
        video_duration = (
            transcript[-1]["start"] + transcript[-1].get("duration", 3.0)
            if transcript else 0.0
        )
        analysis_log.log(
            "CHUNKS_CREATED",
            chunk_count=len(chunks),
            video_duration=video_duration,
            chunks=[
                {
                    "index": idx,
                    "start_time": chunk["start_time"],
                    "end_time": chunk["end_time"],
                    "segment_count": len(chunk["segments"]),
                    "text": " ".join(seg["text"] for seg in chunk["segments"]),
                }
                for idx, chunk in enumerate(chunks)
            ],
        )

        await _send(websocket, {
            "type": "status",
            "stage": "chunking",
            "message": f"Transcrição obtida: {len(transcript)} segmentos. A iniciar análise...",
            "total_segments": len(transcript),
            "total_chunks": len(chunks),
        })

        # ----------------------------------------------------------------
        # PASSO 3 — Sinalizar ao cliente que a análise vai começar
        # ----------------------------------------------------------------
        await _send(websocket, {
            "type": "ready",
            "total_chunks": len(chunks),
            "total_segments": len(transcript),
            "video_duration": video_duration,
        })

        # ----------------------------------------------------------------
        # PASSO 4 — Processar cada chunk em paralelo (com limite)
        # ----------------------------------------------------------------
        # Semáforo para controlar quantos chunks são analisados ao mesmo tempo
        # (Aumentar se tiver muita GPU/CPU, diminuir se o Ollama crashar)
        extract_semaphore = asyncio.Semaphore(CHUNK_CONCURRENCY)
        fact_check_semaphore = asyncio.Semaphore(FACT_CHECK_CONCURRENCY)
        progress_lock = asyncio.Lock()
        seen_claims_lock = asyncio.Lock()
        seen_claims = []
        completed_chunks = 0
        chunk_texts = [" ".join(seg["text"] for seg in chunk["segments"]) for chunk in chunks]

        async def mark_chunk_complete(idx: int, timestamp: float, claims_count: int) -> None:
            nonlocal completed_chunks
            async with progress_lock:
                completed_chunks += 1
                if analysis_log:
                    analysis_log.log(
                        "CHUNK_COMPLETE",
                        chunk_index=idx,
                        timestamp=timestamp,
                        completed_chunks=completed_chunks,
                        total_chunks=len(chunks),
                        claims_verified=claims_count,
                    )
                await _send(websocket, {
                    "type": "progress",
                    "stage": "extract",
                    "chunk_index": idx,
                    "current": completed_chunks,
                    "total": len(chunks),
                    "timestamp": timestamp,
                    "claims_count": claims_count,
                    "status": (
                        f"Segmento {idx + 1}/{len(chunks)} concluído "
                        f"({_fmt_ts(timestamp)}) — {claims_count} afirmações verificadas."
                    ),
                })

        async def process_chunk(idx, chunk):
            async with extract_semaphore:
                chunk_text = chunk_texts[idx]
                timestamp = chunk["start_time"]
                
                # Janela deslizante: fornece os últimos 500 caracteres do bloco anterior para contexto
                contexto_anterior = chunk_texts[idx - 1][-500:] if idx > 0 else "Início do vídeo."
                if analysis_log:
                    analysis_log.log(
                        "CHUNK_START",
                        chunk_index=idx,
                        chunk_number=idx + 1,
                        total_chunks=len(chunks),
                        start_time=timestamp,
                        end_time=chunk["end_time"],
                        previous_context=contexto_anterior,
                        chunk_text=chunk_text,
                        segments=chunk["segments"],
                    )

                # 4a. Notifica o cliente do segmento em curso sem avançar a barra.
                await _send(websocket, {
                    "type": "status",
                    "stage": "extract",
                    "chunk_index": idx,
                    "total_chunks": len(chunks),
                    "timestamp": timestamp,
                    "status": f"A analisar segmento {idx + 1}/{len(chunks)} ({_fmt_ts(timestamp)})…",
                    "message": f"A analisar segmento {idx + 1}/{len(chunks)} ({_fmt_ts(timestamp)})…",
                })

                # 4b. Extrai afirmações factuais
                logger.info(f"[{idx+1}/{len(chunks)}] A extrair afirmações do bloco...")
                claims = await asyncio.to_thread(
                    claim_extractor.extract, 
                    chunk_text, 
                    previous_chunk=contexto_anterior, 
                    context=video_context
                )
                if analysis_log:
                    analysis_log.log(
                        "CLAIMS_EXTRACTED_RAW",
                        chunk_index=idx,
                        claim_count=len(claims),
                        claims=claims,
                    )
                # 'claims' agora é uma lista de dicionários {"text": ..., "query": ...}
                claims = [c for c in claims if len(c["text"].strip()) > 10]
                async with seen_claims_lock:
                    unique_claims = []
                    duplicate_claims = []
                    for claim in claims:
                        if _is_duplicate_claim(claim, seen_claims):
                            duplicate_claims.append(claim)
                            continue
                        seen_claims.append({
                            "text": claim.get("text", ""),
                            "query": claim.get("query", ""),
                            "evidence": claim.get("evidence", ""),
                        })
                        unique_claims.append(claim)
                    claims = unique_claims
                if analysis_log:
                    analysis_log.log(
                        "CLAIMS_AFTER_DEDUPE",
                        chunk_index=idx,
                        unique_count=len(claims),
                        duplicate_count=len(duplicate_claims),
                        unique_claims=claims,
                        duplicate_claims=duplicate_claims,
                    )
                
                logger.info(f"[{idx+1}/{len(chunks)}] Encontradas {len(claims)} afirmações.")

                if not claims:
                    await mark_chunk_complete(idx, timestamp, 0)
                    return {"idx": idx, "claims_count": 0, "messages": []}

                # Define uma função interna para processar e enviar cada verificação
                claims_with_ts = []
                for c_idx, c in enumerate(claims):
                    claim_text = c["text"]
                    exact_ts = _find_exact_timestamp(
                        claim_text,
                        chunk["segments"],
                        timestamp,
                        evidence_text=c.get("evidence", ""),
                    )
                    claims_with_ts.append((c_idx, c, exact_ts))
                    logger.info(f"[{idx+1}/{len(chunks)}][Claim {c_idx+1}] A verificar: '{claim_text[:50]}...' (TS: {exact_ts}s)")
                    if analysis_log:
                        analysis_log.log(
                            "CLAIM_CHECKING",
                            chunk_index=idx,
                            claim_index=c_idx,
                            claim=claim_text,
                            query=c.get("query", ""),
                            evidence=c.get("evidence", ""),
                            timestamp=exact_ts,
                        )
                async with fact_check_semaphore:
                    results = await asyncio.to_thread(
                        rag_engine.fact_check_many,
                        [c for _, c, _ in claims_with_ts],
                        fmt_date,
                        analysis_log,
                    )

                result_messages = []
                for (c_idx, c, exact_ts), res in zip(claims_with_ts, results):
                    logger.info(f"[{idx+1}/{len(chunks)}][Claim {c_idx+1}] Veredito: {res['verdict']} ({res['confidence']*100:.0f}%)")
                    if analysis_log:
                        analysis_log.log(
                            "FACT_CHECK_READY",
                            chunk_index=idx,
                            claim_index=c_idx,
                            timestamp=exact_ts,
                            claim=c["text"],
                            result=res,
                        )
                    result_messages.append({
                        "type": "fact_check",
                        "timestamp": exact_ts,
                        "claim": c["text"],
                        "query": c.get("query", ""),
                        "evidence": c.get("evidence", ""),
                        "verdict": res["verdict"],
                        "justification": res["justification"],
                        "confidence": res["confidence"],
                        "sources": res.get("sources", []),
                        "context_used": res.get("context_used", []),
                        "search_type": res.get("search_type", "web"),
                    })
                
                logger.info(f"[{idx+1}/{len(chunks)}] Bloco concluído.")
                await mark_chunk_complete(idx, timestamp, len(claims))
                result_messages.sort(key=lambda item: item.get("timestamp", 0))
                return {"idx": idx, "claims_count": len(claims), "messages": result_messages}

        # Dispara o processamento em paralelo, mas emite resultados por ordem do vídeo.
        tasks = [asyncio.create_task(process_chunk(idx, chunk)) for idx, chunk in enumerate(chunks)]
        pending_by_chunk = {}
        next_chunk_to_emit = 0
        total_claims_found = 0
        emitted_claims = []

        async def emit_ready_chunks() -> None:
            nonlocal next_chunk_to_emit, total_claims_found

            while next_chunk_to_emit in pending_by_chunk:
                chunk_result = pending_by_chunk.pop(next_chunk_to_emit)
                emitted_count = 0

                for message in chunk_result["messages"]:
                    claim_obj = {
                        "text": message.get("claim", ""),
                        "query": message.get("query", ""),
                        "evidence": message.get("evidence", ""),
                    }
                    if _is_duplicate_claim(claim_obj, emitted_claims):
                        if analysis_log:
                            analysis_log.log(
                                "FACT_CHECK_SUPPRESSED_DUPLICATE",
                                chunk_index=chunk_result["idx"],
                                timestamp=message.get("timestamp"),
                                claim=message.get("claim"),
                            )
                        continue

                    emitted_claims.append(claim_obj)
                    emitted_count += 1

                    await _send(websocket, {
                        "type": "checking",
                        "claim": message["claim"],
                        "timestamp": message["timestamp"],
                        "chunk_index": chunk_result["idx"],
                    })
                    public_message = {
                        key: value
                        for key, value in message.items()
                        if key not in {"query", "evidence"}
                    }
                    await _send(websocket, public_message)
                    if analysis_log:
                        analysis_log.log(
                            "FACT_CHECK_SENT",
                            chunk_index=chunk_result["idx"],
                            timestamp=message.get("timestamp"),
                            claim=message.get("claim"),
                            result={
                                "verdict": message.get("verdict"),
                                "confidence": message.get("confidence"),
                                "sources": message.get("sources", []),
                            },
                        )

                total_claims_found += emitted_count
                next_chunk_to_emit += 1

        for task in asyncio.as_completed(tasks):
            chunk_result = await task
            pending_by_chunk[chunk_result["idx"]] = chunk_result
            await emit_ready_chunks()

        # ----------------------------------------------------------------
        # PASSO 5 — Análise concluída
        # ----------------------------------------------------------------
        elapsed_seconds = round(time.perf_counter() - analysis_started_at, 1)
        await _send(websocket, {
            "type": "complete",
            "message": (
                f"Análise concluída em {elapsed_seconds:.1f}s! "
                f"{total_claims_found} afirmações verificadas."
            ),
            "total_claims": total_claims_found,
            "elapsed_seconds": elapsed_seconds,
        })
        logger.info(
            "Vídeo %s analisado: %s afirmações em %.1fs.",
            video_id,
            total_claims_found,
            elapsed_seconds,
        )
        if analysis_log:
            analysis_log.close(
                status="complete",
                total_claims=total_claims_found,
                elapsed_seconds=elapsed_seconds,
                log_file=str(analysis_log.path),
            )

    except WebSocketDisconnect:
        logger.info("Cliente desligado durante a análise.")
        if analysis_log:
            analysis_log.close(status="websocket_disconnect")

    except Exception as err:
        logger.error(f"Erro inesperado: {err}", exc_info=True)
        if analysis_log:
            analysis_log.close(status="unexpected_error", error=repr(err))
        try:
            await _send(websocket, {
                "type": "error",
                "message": f"Erro interno do servidor: {str(err)[:150]}",
            })
        except Exception:
            pass  # Cliente já pode estar desligado


# ======================================================================
# FUNÇÕES AUXILIARES
# ======================================================================


def _fmt_ts(seconds: float) -> str:
    """Formata segundos em MM:SS ou HH:MM:SS."""
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h}:{m:02}:{sec:02}" if h > 0 else f"{m}:{sec:02}"


def _find_exact_timestamp(
    claim_text: str,
    segments: list,
    fallback_ts: float,
    evidence_text: str = "",
) -> float:
    """Encontra o segundo exato do segmento onde a claim aparece."""
    evidence_ts = _find_timestamp_by_text(evidence_text, segments)
    if evidence_ts is not None:
        return evidence_ts

    claim_lower = claim_text.lower()
    
    # 1. Tenta correspondência direta (se a frase do segmento estiver na claim)
    for seg in segments:
        seg_text = seg["text"].lower()
        if seg_text in claim_lower or claim_lower in seg_text:
            return seg["start"]
            
    # 2. Busca aproximada (Fuzzy matching) caso o LLM tenha reescrito a frase ligeiramente
    best_match_ts = fallback_ts
    highest_ratio = 0.0
    
    for seg in segments:
        seg_text = seg["text"].lower()
        ratio = difflib.SequenceMatcher(None, claim_lower, seg_text).ratio()
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_match_ts = seg["start"]
            
    # Usa fuzzy matching apenas quando a claim e o segmento continuam parecidos.
    # Claims traduzidas devem cair no evidence_text ou no fallback, nao num match fraco.
    if highest_ratio >= 0.62:
        return best_match_ts
        
    token_ts = _find_timestamp_by_keywords(claim_text, segments)
    if token_ts is not None:
        return token_ts

    return fallback_ts


def _find_timestamp_by_text(text: str, segments: list) -> float | None:
    normalized = _normalize_for_similarity(text)
    if not normalized:
        return None

    for seg in segments:
        seg_norm = _normalize_for_similarity(seg.get("text", ""))
        if seg_norm and (seg_norm in normalized or normalized in seg_norm):
            return seg["start"]

    best_ts = None
    best_ratio = 0.0
    for seg in segments:
        seg_norm = _normalize_for_similarity(seg.get("text", ""))
        ratio = difflib.SequenceMatcher(None, normalized, seg_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_ts = seg["start"]

    return best_ts if best_ratio >= 0.62 else None


def _find_timestamp_by_keywords(claim_text: str, segments: list) -> float | None:
    claim_tokens = _important_tokens(claim_text)
    if not claim_tokens:
        return None

    best_ts = None
    best_score = 0.0
    for seg in segments:
        seg_tokens = _important_tokens(seg.get("text", ""))
        if not seg_tokens:
            continue
        score = len(claim_tokens & seg_tokens) / min(len(claim_tokens), len(seg_tokens))
        if score > best_score:
            best_score = score
            best_ts = seg["start"]

    return best_ts if best_score >= 0.45 else None


def _is_duplicate_claim(claim: dict, seen_claims: list[dict]) -> bool:
    """Deduplica claims vindas de chunks sobrepostos ou reescritas pelo LLM."""
    text = _normalize_for_similarity(claim.get("text", ""))
    query = _normalize_for_similarity(claim.get("query", ""))
    if not text and not query:
        return True

    for seen in seen_claims:
        seen_text = _normalize_for_similarity(seen.get("text", ""))
        seen_query = _normalize_for_similarity(seen.get("query", ""))

        if query and seen_query and query == seen_query:
            return True
        if query and seen_query and _similarity(query, seen_query) >= 0.86:
            return True
        if text and seen_text and _similarity(text, seen_text) >= 0.82:
            return True
        if text and seen_text and _token_overlap(text, seen_text) >= 0.78:
            return True

    return False


def _normalize_for_similarity(value: str) -> str:
    text = re.sub(r"\W+", " ", str(value).casefold()).strip()
    stopwords = {
        "a", "o", "os", "as", "um", "uma", "the", "an", "and", "or", "de", "da", "do",
        "das", "dos", "que", "that", "to", "of", "in", "em", "no", "na", "nos", "nas",
    }
    tokens = [token for token in text.split() if token not in stopwords]
    return " ".join(tokens[:28])


def _important_tokens(value: str) -> set[str]:
    normalized = _normalize_for_similarity(value)
    return {token for token in normalized.split() if len(token) >= 4}


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _token_overlap(a: str, b: str) -> float:
    left = set(a.split())
    right = set(b.split())
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


async def _send(websocket: WebSocket, data: dict) -> None:
    """Serializa e envia um dicionário como mensagem JSON pelo WebSocket."""
    await websocket.send_text(json.dumps(data, ensure_ascii=False))


def _group_into_chunks(transcript: list[dict], chunk_size: int) -> list[dict]:
    """
    Agrupa segmentos com sobreposição (overlap) para não cortar 'meias frases'.
    """
    if chunk_size <= 0:
        chunk_size = 1

    chunks = []
    # Mantem alguma sobreposicao para nao cortar frases, mas impede que o overlap
    # seja maior que o chunk e transforme o processamento numa janela segmento-a-segmento.
    overlap = max(0, min(CHUNK_OVERLAP, chunk_size - 1))
    step = max(1, chunk_size - overlap)
    
    for i in range(0, len(transcript), step):
        group = transcript[i : i + chunk_size]
        if not group:
            break
            
        chunks.append(
            {
                "segments": group,
                "start_time": group[0]["start"],
                "end_time": group[-1]["start"] + group[-1].get("duration", 3.0),
            }
        )
        # Pára se já chegou ao fim da transcrição
        if i + chunk_size >= len(transcript):
            break
            
    return chunks


# ======================================================================
# PONTO DE ENTRADA
# ======================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,  # Recarrega automaticamente ao alterar código
        log_level="info",
    )
