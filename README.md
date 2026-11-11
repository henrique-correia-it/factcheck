# FactCheck — Real-Time Fact-Checking of YouTube Videos with Local LLMs

**Paste a YouTube link. FactCheck pulls out the factual claims, checks them against web sources with a local LLM (RAG) and shows each verdict at the moment the claim is made in the video.**

![Python](https://img.shields.io/badge/Python_3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![LangChain](https://img.shields.io/badge/LangChain-1C3C3C?logo=langchain&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-local_LLM-000000)
![WebSockets](https://img.shields.io/badge/WebSockets-real--time-4B32C3)

> Academic project for the Applied Artificial Intelligence course (IAAPLI), BSc in Electrical and Computer Engineering, ISEP, 2026.
> **Authors:** Henrique Correia and Guilherme Leão.

![Synchronised results view](docs/images/ecra_resultados.png)

## Highlights

- **RAG pipeline with a local LLM:**
  - one LLM pass extracts claims that can be checked;
  - a second pass judges each claim using live web results;
  - runs on **Ollama**, with no paid API and no data sent to an LLM provider.
- **Streaming results:**
  - a **FastAPI WebSocket** sends status, progress and each verdict as soon as it is ready;
  - the frontend plays the video and shows each verdict at its timestamp, with a timeline of results.
- **Concurrency without overloading the model:**
  - claim extraction and verification run in parallel under `asyncio` semaphores;
  - web searches run in a thread pool;
  - repeated claims are filtered out.
- **Context-aware chunking:** the transcript is split into overlapping chunks, so claims are not cut in half at the edges.
- **Calibrated output:** each verdict comes with a justification, sources and a confidence capped at 0.95.

## How it works

```text
YouTube URL
   │
   ▼
Transcript (youtube-transcript-api) ──► overlapping chunks
   │
   ▼
Claim extraction (LLM via LangChain + Ollama)          ┐
   │                                                   │ parallel, limited
   ▼                                                   │ by semaphores
Web search per claim (DuckDuckGo, query variants)      │
   │                                                   │
   ▼                                                   │
Verdict with RAG (LLM + retrieved sources)             ┘
   │
   ▼
WebSocket stream ──► frontend synchronised with the YouTube player
```

| 1. Paste the link | 2. Live analysis |
|---|---|
| ![URL form](docs/images/formulario_url_youtube.png) | ![Pre-processing](docs/images/ecra_pre_processamento.png) |

### Verdicts

| Verdict | Meaning |
|---|---|
| **True** (*Verdadeiro*) | Explicitly confirmed by the sources |
| **False** (*Falso*) | Explicitly contradicted by the sources |
| **Uncertain** (*Mais ou menos*) | Not enough context, weak sources, opinion or an ambiguous claim |

The interface and verdicts are in Portuguese. The model analyses each video in its own language.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn, asyncio |
| Real time | WebSockets |
| LLM | Ollama (any local model, e.g. `llama3.2`, `phi4`) |
| Orchestration | LangChain, `langchain-ollama` |
| Retrieval | DuckDuckGo search (`ddgs`, no API key) |
| Transcripts | `youtube-transcript-api` |
| Frontend | HTML, CSS, JavaScript, YouTube IFrame Player |

## Getting started

Requires Python 3.12+ and **[Ollama](https://ollama.com/)** running locally with a model pulled, for example `ollama pull llama3.2`.

```bash
git clone https://github.com/henrique-correia-it/factcheck.git
cd factcheck
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # set LLM_MODEL (and OLLAMA_BASE_URL if needed)
cd backend
python main.py
```

Open <http://localhost:8000>. The video needs captions (manual or automatic) available on YouTube.

### Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `LLM_MODEL` | — | Ollama model to use |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `16` / `3` | Transcript segments per chunk and overlap between chunks |
| `CHUNK_CONCURRENCY` | `2` | Chunks analysed at the same time |
| `FACT_CHECK_CONCURRENCY` | `2` | Verification batches in parallel |
| `WEB_SEARCH_CONCURRENCY` | `4` | Parallel web searches per batch |
| `LLM_TIMEOUT_SECONDS` | `45` | Timeout per LLM call |

With large models or a small GPU, lower `CHUNK_CONCURRENCY` to `1`.

## WebSocket protocol

Endpoint: `/ws/factcheck`

```json
// client → server
{ "url": "https://www.youtube.com/watch?v=VIDEO_ID" }

// server → client
{ "type": "status",     "message": "...", "stage": "transcript" }
{ "type": "ready",      "total_chunks": 12, "video_duration": 3600 }
{ "type": "progress",   "current": 3, "total": 12, "timestamp": 120.5 }
{ "type": "fact_check", "timestamp": 142.0, "claim": "...", "verdict": "Verdadeiro",
  "justification": "...", "confidence": 0.87, "sources": ["..."] }
{ "type": "complete",   "total_claims": 24, "elapsed_seconds": 87.3 }
{ "type": "error",      "message": "..." }
```

## Project structure

```text
backend/
├── main.py                FastAPI app, WebSocket endpoint, concurrency control
├── transcript_handler.py  transcript download and parsing
├── claim_extractor.py     claim extraction with the LLM
├── web_search.py          DuckDuckGo search
├── rag_pipeline.py        verification: retrieval + LLM verdict
└── analysis_log.py        structured per-session log
frontend/
├── index.html             single page, three screens
├── script.js              WebSocket client and synchronised player
└── style.css
```

## Limitations

- Verdicts depend on what the web search returns and on the model used. They support human judgement; they do not replace it.
- Videos without captions cannot be analysed.
- Analysis time grows with video length and model size.
