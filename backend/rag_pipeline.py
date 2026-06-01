"""
rag_pipeline.py
---------------
Pipeline RAG com pesquisa web em tempo real.

Fluxo:
  1. Pesquisa web para a query gerada pelo extractor.
  2. O contexto recuperado e anexado ao prompt.
  3. LLM classifica a claim com especial atencao a contexto temporal.
"""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama

from web_search import format_search_context, search_claim_variants

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
logger = logging.getLogger(__name__)

FACT_CHECK_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """Voce e um verificador de factos rigoroso e imparcial.

DATA/CONTEXTO TEMPORAL DO VIDEO: {video_date}

Avalie a afirmacao com base no trecho original do video e no contexto de pesquisa web abaixo.

CONTEXTO:
{context}

CATEGORIAS:
- "Verdadeiro": facto confirmado explicitamente pelo contexto.
- "Falso": facto contradito explicitamente pelo contexto.
- "Mais ou menos": contexto insuficiente, informacao desatualizada, fonte fraca, opiniao, previsao ou afirmacao demasiado vaga.

REGRAS CRITICAS:
1. Se o contexto nao falar diretamente do assunto, nao invente. Use "Mais ou menos".
2. Respeite a data do video. Para "agora", "atualmente", "right now", "never", "nunca" e linguagem temporal, avalie como era conhecido nessa data.
3. Fontes posteriores ao video so servem para confirmar factos ja conhecidos na data do video. Nao as use para contradizer afirmacoes temporais feitas no video.
4. Se a afirmacao for sobre alguem ter dito/afirmado uma frase, o trecho original do video pode confirmar essa atribuicao. Se a afirmacao for sobre o facto subjacente, o trecho original nao basta.
5. Previsoes, consequencias especulativas e juizos causais sobre o futuro devem ser "Mais ou menos", exceto quando o contexto prova que ja aconteceram.
6. Baixe a confianca quando as fontes forem editoriais, partidarias, agregadores ou divergentes.
7. Nunca devolva confianca 1.0. Use no maximo 0.95 mesmo com fontes fortes.
8. Use portugues europeu correto.

RESPONDA APENAS com este JSON:
{{
  "verdict": "Verdadeiro",
  "justification": "Justificacao curta, concreta e fiel ao contexto.",
  "confidence": 0.85
}}
""",
        ),
        ("human", "Afirmacao: {claim}"),
    ]
)

BATCH_FACT_CHECK_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """Voce e um verificador de factos rigoroso e imparcial.

DATA/CONTEXTO TEMPORAL DO VIDEO: {video_date}

Vai receber varias afirmacoes, cada uma com o seu contexto de pesquisa.
Classifique CADA afirmacao de forma independente.
Use apenas o contexto dentro do bloco da propria afirmacao. Nunca use o contexto
ou a conclusao de uma CLAIM para justificar outra CLAIM.

CATEGORIAS:
- "Verdadeiro": facto confirmado explicitamente pelo contexto.
- "Falso": facto contradito explicitamente pelo contexto.
- "Mais ou menos": contexto insuficiente, informacao desatualizada, fonte fraca, opiniao, previsao ou afirmacao demasiado vaga.

REGRAS CRITICAS:
1. Se o contexto de uma afirmacao nao falar diretamente do assunto, use "Mais ou menos".
2. Respeite a data do video. Para "agora", "atualmente", "right now", "never", "nunca" e linguagem temporal, avalie como era conhecido nessa data.
3. Fontes posteriores ao video so servem para confirmar factos ja conhecidos na data do video. Nao as use para contradizer afirmacoes temporais feitas no video.
4. Se a afirmacao for sobre alguem ter dito/afirmado uma frase, o trecho original do video pode confirmar essa atribuicao. Se a afirmacao for sobre o facto subjacente, o trecho original nao basta.
5. Previsoes, consequencias especulativas e juizos causais sobre o futuro devem ser "Mais ou menos", exceto quando o contexto prova que ja aconteceram.
6. Baixe a confianca quando as fontes forem editoriais, partidarias, agregadores ou divergentes.
7. Use portugues europeu correto.
8. A justificacao nao deve mencionar "Claim 0", "Claim 1" ou outra numeracao interna.
9. Nunca devolva confianca 1.0. Use no maximo 0.95 mesmo com fontes fortes.

RESPONDA APENAS com este JSON:
{{
  "results": [
    {{
      "index": 0,
      "verdict": "Verdadeiro",
      "justification": "Justificacao curta, concreta e fiel ao contexto.",
      "confidence": 0.85
    }}
  ]
}}
""",
        ),
        ("human", "{claims_with_context}"),
    ]
)


class FactCheckRAG:
    """Motor de verificacao de factos com retrieval web e classificacao LLM."""

    def __init__(self):
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        llm_model = os.getenv("LLM_MODEL", "minimax-m3:cloud")
        self.web_results = int(os.getenv("WEB_MAX_RESULTS", "3"))
        self.web_search_concurrency = int(os.getenv("WEB_SEARCH_CONCURRENCY", "4"))

        self.llm = ChatOllama(
            model=llm_model,
            base_url=base_url,
            temperature=0,
            format="json",
            num_predict=int(os.getenv("FACT_CHECK_NUM_PREDICT", "900")),
            sync_client_kwargs={"timeout": float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))},
        )
        self.chain = FACT_CHECK_PROMPT | self.llm
        self.batch_chain = BATCH_FACT_CHECK_PROMPT | self.llm

    def fact_check(self, claim_obj: dict, video_date: str = "", analysis_log=None) -> dict:
        """Verifica uma afirmacao usando pesquisa web em tempo real."""
        return self.fact_check_many([claim_obj], video_date=video_date, analysis_log=analysis_log)[0]

    def fact_check_many(self, claim_objs: list[dict], video_date: str = "", analysis_log=None) -> list[dict]:
        """Verifica varias afirmacoes com uma chamada LLM em lote."""
        if not claim_objs:
            return []

        if analysis_log:
            analysis_log.log(
                "FACT_CHECK_BATCH_START",
                video_date=video_date or "Desconhecida",
                claims=claim_objs,
            )

        prepared = self._prepare_claim_contexts(claim_objs, video_date, analysis_log=analysis_log)
        if len(prepared) == 1:
            return [self._classify_single(prepared[0], video_date, analysis_log=analysis_log)]

        claims_with_context = "\n\n".join(
            f"### CLAIM {idx}\n"
            f"Afirmação: {item['claim_text']}\n"
            f"Trecho original do video: {item.get('evidence') or 'Nao disponivel.'}\n"
            f"Contexto:\n{item['context']}"
            for idx, item in enumerate(prepared)
        )

        try:
            if analysis_log:
                analysis_log.log(
                    "LLM_BATCH_CLASSIFICATION_START",
                    claim_count=len(prepared),
                    prompt_payload=claims_with_context,
                )
            response = self.batch_chain.invoke(
                {
                    "claims_with_context": claims_with_context,
                    "video_date": video_date or "Desconhecida",
                }
            )
            if analysis_log:
                analysis_log.log("LLM_BATCH_RAW_RESPONSE", response=response.content)
            parsed = _parse_json_safe(response.content.strip())
            raw_results = parsed.get("results", []) if isinstance(parsed, dict) else []

            by_index = {}
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                by_index[idx] = item

            results = []
            for idx, prepared_item in enumerate(prepared):
                result = by_index.get(idx, {})
                if _result_needs_single_recheck(result):
                    if analysis_log:
                        analysis_log.log(
                            "BATCH_RESULT_RECHECK",
                            index=idx,
                            reason="batch justification referenced another claim",
                            raw_result=result,
                        )
                    results.append(self._classify_single(
                        prepared_item,
                        video_date,
                        analysis_log=analysis_log,
                    ))
                else:
                    results.append(self._build_result(result, prepared_item))
            if analysis_log:
                analysis_log.log("FACT_CHECK_BATCH_RESULTS", results=results)
            return results

        except Exception as err:
            logger.error(f"Erro no batch fact-check: {err}")
            if analysis_log:
                analysis_log.log("LLM_BATCH_ERROR", error=repr(err))
            return [self._classify_single(item, video_date, analysis_log=analysis_log) for item in prepared]

    def _prepare_claim_context(self, claim_obj: dict, video_date: str = "", analysis_log=None) -> dict:
        """Pesquisa e monta contexto para uma claim."""
        claim_text = claim_obj["text"]
        original_search_query = claim_obj["query"]
        evidence = str(claim_obj.get("evidence", "")).strip()
        search_query = original_search_query

        use_video_year = os.getenv("USE_VIDEO_YEAR_IN_SEARCH", "true").lower() == "true"
        if use_video_year and video_date and video_date != "Desconhecida":
            year = video_date.split("/")[-1] if "/" in video_date else video_date
            if year and year not in search_query:
                search_query = f"{search_query} {year}"

        logger.info(f"RAG: Pesquisar '{search_query}'")
        if analysis_log:
            analysis_log.log(
                "WEB_SEARCH_START",
                claim=claim_text,
                original_query=original_search_query,
                final_query=search_query,
                max_results=self.web_results,
            )

        search_queries = _build_search_variants(search_query, evidence)
        web_results = search_claim_variants(search_queries, max_results=self.web_results)
        if analysis_log:
            analysis_log.log(
                "WEB_SEARCH_RESULTS",
                query=search_query,
                query_variants=search_queries,
                result_count=len(web_results),
                results=web_results,
            )
        context_parts, sources = format_search_context(web_results)
        source_qualities = [r.get("source_quality", "media") for r in web_results]
        search_type = "web"

        if web_results:
            logger.info(f"RAG: {len(web_results)} resultados web encontrados.")

        context = "\n\n---\n\n".join(context_parts) if context_parts else "Sem contexto disponivel."
        if analysis_log:
            analysis_log.log(
                "CLAIM_CONTEXT_PREPARED",
                claim=claim_text,
                search_query=search_query,
                search_type=search_type,
                sources=sources,
                evidence=evidence,
                context=context,
            )

        return {
            "claim_text": claim_text,
            "search_query": search_query,
            "evidence": evidence,
            "context": context,
            "context_parts": context_parts,
            "sources": sources,
            "source_qualities": source_qualities,
            "search_type": search_type,
        }

    def _prepare_claim_contexts(self, claim_objs: list[dict], video_date: str = "", analysis_log=None) -> list[dict]:
        """Prepara contextos em paralelo, preservando a ordem original."""
        if len(claim_objs) <= 1 or self.web_search_concurrency <= 1:
            return [
                self._prepare_claim_context(claim_obj, video_date, analysis_log=analysis_log)
                for claim_obj in claim_objs
            ]

        max_workers = min(self.web_search_concurrency, len(claim_objs))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(
                lambda claim_obj: self._prepare_claim_context(
                    claim_obj,
                    video_date,
                    analysis_log=analysis_log,
                ),
                claim_objs,
            ))

    def _classify_single(self, prepared: dict, video_date: str = "", analysis_log=None) -> dict:
        """Classifica uma claim ja enriquecida com contexto."""
        try:
            if analysis_log:
                analysis_log.log(
                    "LLM_SINGLE_CLASSIFICATION_START",
                    claim=prepared["claim_text"],
                    context=prepared["context"],
                )
            response = self.chain.invoke(
                {
                    "context": prepared["context"],
                    "claim": (
                        f"{prepared['claim_text']}\n"
                        f"Trecho original do video: {prepared.get('evidence') or 'Nao disponivel.'}"
                    ),
                    "video_date": video_date or "Desconhecida",
                }
            )
            if analysis_log:
                analysis_log.log("LLM_SINGLE_RAW_RESPONSE", response=response.content)
            result = _parse_json_safe(response.content.strip())
            built = self._build_result(result, prepared)
            if analysis_log:
                analysis_log.log("FACT_CHECK_SINGLE_RESULT", result=built)
            return built

        except Exception as err:
            logger.error(f"Erro no fact-check de '{prepared['claim_text'][:40]}': {err}")
            if analysis_log:
                analysis_log.log(
                    "LLM_SINGLE_ERROR",
                    claim=prepared["claim_text"],
                    error=repr(err),
                )
            return {
                "verdict": "Mais ou menos",
                "justification": f"Erro na verificacao: {str(err)[:80]}",
                "confidence": 0.0,
                "sources": [],
                "context_used": [],
                "search_type": prepared["search_type"],
            }

    def _build_result(self, result: dict, prepared: dict) -> dict:
        verdict = result.get("verdict", "Mais ou menos") if isinstance(result, dict) else "Mais ou menos"
        if verdict not in ("Verdadeiro", "Falso", "Mais ou menos"):
            verdict = "Mais ou menos"

        try:
            raw_confidence = float(result.get("confidence", 0.5)) if isinstance(result, dict) else 0.5
        except (TypeError, ValueError):
            raw_confidence = 0.5

        attributed_statement_override = _is_attributed_statement_with_evidence(
            prepared.get("claim_text", ""),
            prepared.get("evidence", ""),
        )
        if verdict == "Falso" and attributed_statement_override:
            verdict = "Verdadeiro"
            result = {
                **(result if isinstance(result, dict) else {}),
                "justification": "O trecho original do vídeo confirma que esta afirmação foi feita no debate.",
                "confidence": min(raw_confidence, 0.9),
            }
            raw_confidence = min(raw_confidence, 0.9)

        return {
            "verdict": verdict,
            "justification": (
                result.get("justification", "Sem justificacao disponivel.")
                if isinstance(result, dict)
                else "Sem justificacao disponivel."
            ),
            "confidence": _adjust_confidence(
                verdict,
                raw_confidence,
                prepared["sources"],
                prepared.get("source_qualities", []),
            ),
            "sources": prepared["sources"],
            "context_used": prepared["context_parts"][:3],
            "search_type": prepared["search_type"],
        }


def _parse_json_safe(raw: str) -> dict:
    """Tenta extrair JSON valido de uma string, mesmo com texto extra."""
    raw = re.sub(r"```(?:json)?\n?", "", raw).strip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    json_text = _extract_first_json_object(raw)
    if json_text:
        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            pass
    return {}


def _build_search_variants(search_query: str, evidence: str = "") -> list[str]:
    """Cria poucas variantes gerais para melhorar recall sem multiplicar latencia."""
    variants = [search_query]

    quote_variant = _quoted_evidence_variant(search_query, evidence)
    if quote_variant:
        variants.append(quote_variant)

    evidence_terms = _important_search_terms(evidence)
    query_lower = search_query.casefold()
    missing_terms = [term for term in evidence_terms if term.casefold() not in query_lower]
    if missing_terms:
        variants.append(f"{search_query} {' '.join(missing_terms[:4])}")

    if "fact check" not in query_lower and "fact-check" not in query_lower:
        variants.append(f"{search_query} fact check")

    deduped = []
    seen = set()
    for variant in variants:
        cleaned = " ".join(variant.split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            deduped.append(cleaned)
            seen.add(key)
    return deduped[: int(os.getenv("SEARCH_VARIANTS", "2"))]


def _quoted_evidence_variant(search_query: str, evidence: str) -> str:
    quoted = re.findall(r'"([^"]{8,120})"|\'([^\']{8,120})\'', evidence)
    quote = ""
    if quoted:
        quote = next((left or right for left, right in quoted if (left or right)), "")
    elif len(evidence.split()) <= 14 and any(mark in evidence for mark in [":", ";"]):
        quote = evidence

    if not quote:
        return ""

    quote_words = [word for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", quote) if word.casefold() not in {"the", "and", "that", "this"}]
    if len(quote_words) < 3:
        return ""

    entity_terms = []
    for word in re.findall(r"[A-Z][A-Za-z'-]{2,}", search_query):
        if word not in entity_terms:
            entity_terms.append(word)
        if len(entity_terms) >= 2:
            break

    compact_quote = " ".join(quote_words[:8])
    return f"{' '.join(entity_terms)} \"{compact_quote}\"".strip()


def _important_search_terms(text: str) -> list[str]:
    stopwords = {
        "the", "and", "that", "this", "with", "from", "have", "has", "for", "you",
        "your", "about", "their", "there", "were", "was", "are", "uma", "uns", "com",
        "para", "que", "por", "dos", "das", "de", "em", "na", "no", "as", "os",
        "said", "last", "year", "quote", "secretary", "first", "would", "could",
    }
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", text)
    useful = []
    for word in words:
        lower = word.casefold().strip("'")
        if lower in stopwords or len(lower) < 4:
            continue
        if word[:1].isupper() or any(ch.isdigit() for ch in word) or len(lower) >= 5:
            useful.append(word.strip("'"))
    return useful


def _extract_first_json_object(text: str) -> str | None:
    """Extrai o primeiro objeto JSON completo, incluindo objetos aninhados."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    return None


def _adjust_confidence(
    verdict: str,
    value: float,
    sources: list[str],
    source_qualities: list[str] | None = None,
) -> float:
    confidence = max(0.0, min(0.95, value))
    if verdict == "Mais ou menos":
        confidence = min(confidence, 0.7)
    if not sources:
        confidence = min(confidence, 0.5)
    qualities = source_qualities or []
    if verdict != "Mais ou menos" and qualities:
        if "alta" not in qualities:
            confidence = min(confidence, 0.85)
        if "baixa" in qualities:
            confidence = min(confidence, 0.75)
    return confidence


def _is_attributed_statement_with_evidence(claim_text: str, evidence: str) -> bool:
    """Identifica claims cujo facto verificavel e a propria atribuicao de uma frase."""
    if not evidence:
        return False
    claim_lower = claim_text.casefold()
    if not re.search(r"\b(afirma|afirmou|disse|says|said|claims|claimed)\b", claim_lower):
        return False
    return len(evidence.split()) >= 3


def _result_needs_single_recheck(result: dict) -> bool:
    if not isinstance(result, dict):
        return True
    justification = str(result.get("justification", ""))
    return bool(re.search(r"\bclaim\s*\d+\b|\bcontexto da claim\b", justification, re.IGNORECASE))
