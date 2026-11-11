# FactCheck — Verificação de Factos em Tempo Real

> Analisa automaticamente os factos afirmados em debates e entrevistas do YouTube, sincronizado com o vídeo, utilizando apenas LLMs locais gratuitos.

**Python · FastAPI · WebSockets · LangChain · Ollama · RAG**

Projeto académico da unidade curricular de Inteligência Artificial Aplicada (IAAPLI), Licenciatura em Engenharia Eletrotécnica e de Computadores, ISEP, 2026.

**Autores:** Henrique Correia e Guilherme Leão

---

## Como funciona

```
URL do YouTube
      │
      ▼
Extracção da transcrição
      │
      ▼
Divisão em chunks sobrepostos
      │
      ▼
Extracção de afirmações (LLM)
      │
      ▼
Pesquisa web em tempo real (DuckDuckGo)
      │
      ▼
Classificação RAG (LLM)
      │
      ▼
Resultados via WebSocket ──► Frontend sincronizado com o player
```

1. O utilizador cola um URL do YouTube.
2. O backend extrai a transcrição e divide-a em blocos com sobreposição para não cortar frases.
3. Para cada bloco, um LLM local (via Ollama) identifica afirmações factuais verificáveis.
4. O sistema pesquisa cada afirmação na web (DuckDuckGo, sem API key) em paralelo.
5. Um segundo passo de LLM classifica cada afirmação com base nas fontes encontradas.
6. Os resultados são transmitidos via WebSocket e exibidos sincronizados com o ponto do vídeo onde a afirmação foi feita.

---

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.12+, FastAPI, Uvicorn |
| Tempo real | WebSockets |
| LLM local | Ollama (qualquer modelo) |
| Orquestração LLM | LangChain + langchain-ollama |
| Pesquisa web | DuckDuckGo (ddgs) — sem API key |
| Transcrições | youtube-transcript-api |
| Frontend | HTML + CSS + JavaScript vanilla |

---

## Pré-requisitos

- **Python 3.12+**
- **[Ollama](https://ollama.com/)** instalado e a correr localmente
- Um modelo descarregado no Ollama (ex: `ollama pull llama3.2`)

---

## Instalação

```bash
# 1. Clonar o repositório
git clone https://github.com/henrique-correia-it/factcheck.git
cd factcheck

# 2. Criar e activar um ambiente virtual
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Instalar dependências
pip install -r requirements.txt

# 4. Configurar variáveis de ambiente
cp .env.example .env
# Editar .env e definir LLM_MODEL e, se necessário, OLLAMA_BASE_URL
```

---

## Configuração (`.env`)

| Variável | Predefinição | Descrição |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL do servidor Ollama |
| `LLM_MODEL` | — | Modelo Ollama a usar (ex: `llama3.2`, `phi4`) |
| `CHUNK_SIZE` | `16` | Segmentos de transcrição por bloco |
| `CHUNK_OVERLAP` | `3` | Segmentos repetidos entre blocos adjacentes |
| `CHUNK_CONCURRENCY` | `2` | Blocos analisados em simultâneo |
| `FACT_CHECK_CONCURRENCY` | `2` | Lotes de verificação em paralelo |
| `WEB_SEARCH_CONCURRENCY` | `4` | Pesquisas web paralelas por lote |
| `LLM_TIMEOUT_SECONDS` | `45` | Timeout máximo por chamada ao LLM |

> **Dica de performance:** Com um modelo maior (70B+) ou GPU limitada, reduza `CHUNK_CONCURRENCY` para `1`. Com hardware potente, aumente para `3` ou `4`.

---

## Executar

```bash
cd backend
python main.py
```

O servidor fica disponível em **[http://localhost:8000](http://localhost:8000)**.

---

## Interface

### Ecrã 1 — Introdução do URL

Cole qualquer URL do YouTube com legendas disponíveis e clique em **Iniciar Análise**.

### Ecrã 2 — Pré-processamento

Acompanhe o progresso em tempo real:

- Transcrição obtida
- Afirmações extraídas por segmento
- Pesquisa e verificação em curso
- Contadores de Verdadeiros / Falsos / Incertos a actualizar ao vivo

### Ecrã 3 — Player sincronizado

Quando a análise termina, o vídeo começa. As verificações aparecem no feed esquerdo no momento exacto em que cada afirmação é feita. A timeline no fundo mostra a distribuição dos resultados ao longo do vídeo.

---

## Vereditos

| Veredito | Significado |
|---|---|
| **Verdadeiro** | Facto confirmado explicitamente pelas fontes |
| **Falso** | Facto contradito explicitamente pelas fontes |
| **Mais ou menos** | Contexto insuficiente, fonte fraca, opinião ou afirmação ambígua |

Cada resultado inclui a afirmação, a justificação, a confiança (0–0.95) e as fontes web consultadas.

---

## Estrutura do projecto

```
Projeto_FactCheck/
├── backend/
│   ├── main.py              # Servidor FastAPI + WebSocket
│   ├── claim_extractor.py   # Extracção de afirmações via LLM
│   ├── rag_pipeline.py      # Verificação com pesquisa web + LLM
│   ├── transcript_handler.py# Obtenção e parsing da transcrição
│   ├── web_search.py        # Pesquisa DuckDuckGo
│   └── analysis_log.py      # Logger estruturado por sessão
├── frontend/
│   ├── index.html           # Interface única (3 ecrãs)
│   ├── style.css            # Estilos
│   └── script.js            # Lógica do cliente e player YouTube
├── requirements.txt
└── .env.example
```

---

## Protocolo WebSocket

O endpoint `/ws/factcheck` segue este protocolo:

**Cliente → Servidor**
```json
{ "url": "https://www.youtube.com/watch?v=VIDEO_ID" }
```

**Servidor → Cliente**
```json
{ "type": "status",     "message": "...", "stage": "transcript" }
{ "type": "ready",      "total_chunks": 12, "video_duration": 3600 }
{ "type": "progress",   "current": 3, "total": 12, "timestamp": 120.5 }
{ "type": "fact_check", "timestamp": 142.0, "claim": "...",
                        "verdict": "Verdadeiro", "justification": "...",
                        "confidence": 0.87, "sources": ["..."] }
{ "type": "complete",   "total_claims": 24, "elapsed_seconds": 87.3 }
{ "type": "error",      "message": "..." }
```

---

## Notas

- **Privacidade:** todo o processamento é local. Nenhum dado é enviado para serviços externos, excepto as pesquisas web ao DuckDuckGo.
- **Legendas:** o vídeo precisa de ter legendas (automáticas ou manuais) disponíveis na API do YouTube.
- **Idioma:** o sistema funciona em qualquer idioma. O modelo extrai afirmações e classifica-as no idioma do vídeo.
- **Modelos testados:** `llama3.2`, `phi4`, `deepseek-v3:cloud`, `minimax-m3:cloud` (via Ollama).
