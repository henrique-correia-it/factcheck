"""
web_search.py
-------------
Pesquisa web em tempo real usando DuckDuckGo via pacote `ddgs`.
"""

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from ddgs import DDGS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
logger = logging.getLogger(__name__)

_CACHE: dict[tuple[str, int], list[dict]] = {}
_CACHE_LOCK = threading.Lock()

_TRUSTED_DOMAINS = (
    "politifact.com",
    "factcheck.org",
    "snopes.com",
    "fullfact.org",
    "afp.com",
    "reuters.com",
    "apnews.com",
    "npr.org",
    "pbs.org",
    "bbc.com",
    "nytimes.com",
    "washingtonpost.com",
    "cnn.com",
    "c-span.org",
    "archives.gov",
    "bea.gov",
    "congress.gov",
    "govinfo.gov",
    "ontheissues.org",
    "ballotpedia.org",
    "publico.pt",
    "observador.pt",
    "poligrafo.sapo.pt",
    "lupa.uol.com.br",
    "aosfatos.org",
    "checamos.afp.com",
    "maldita.es",
    "newtral.es",
    "lemonde.fr",
)

_LOW_QUALITY_DOMAINS = (
    "conservativefiringline.com",
    "alternet.org",
    "dailykos.com",
    "breitbart.com",
    "occupydemocrats.com",
    "naturalnews.com",
    "beforeitsnews.com",
    "commondreams.org",
)


def search_claim(claim: str, max_results: int = 3) -> list[dict]:
    """
    Pesquisa informacoes sobre uma afirmacao usando DuckDuckGo.

    Atrasos e retries sao configuraveis porque o pipeline ja limita
    concorrencia no backend. A versao anterior somava 1.5-3s aqui e mais
    1.5s por claim no backend, o que tornava o video visivelmente lento.
    """
    query = _clean_query(claim)
    cache_key = (query.lower(), max_results)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    min_delay = float(os.getenv("SEARCH_MIN_DELAY", "0.25"))
    max_delay = float(os.getenv("SEARCH_MAX_DELAY", "0.75"))
    retries = int(os.getenv("SEARCH_RETRIES", "2"))
    region = os.getenv("SEARCH_REGION", "wt-wt")

    for attempt in range(retries):
        try:
            if max_delay > 0:
                time.sleep(random.uniform(min_delay, max_delay))

            with DDGS() as ddgs:
                results = list(
                    ddgs.text(
                        query,
                        max_results=max_results * 4,
                        region=region,
                        safesearch="off",
                    )
                )

            ranked = _rank_and_select_results(results, query, max_results)
            with _CACHE_LOCK:
                _CACHE[cache_key] = ranked
            logger.info(
                "Web search '%s': %s resultados (tentativa %s)",
                query[:80],
                len(ranked),
                attempt + 1,
            )
            return ranked

        except Exception as err:
            logger.warning(f"Pesquisa web falhou (tentativa {attempt + 1}): {err}")
            if attempt == retries - 1:
                with _CACHE_LOCK:
                    _CACHE[cache_key] = []
                return []
            time.sleep(0.5 * (attempt + 1))
    return []


def search_claim_variants(queries: list[str], max_results: int = 3) -> list[dict]:
    """Pesquisa varias variantes de uma query e combina os melhores resultados."""
    cleaned_queries = []
    seen_queries = set()
    for query in queries:
        cleaned = _clean_query(query)
        key = cleaned.casefold()
        if cleaned and key not in seen_queries:
            cleaned_queries.append(cleaned)
            seen_queries.add(key)

    if not cleaned_queries:
        return []
    if len(cleaned_queries) == 1:
        return search_claim(cleaned_queries[0], max_results=max_results)

    max_workers = min(len(cleaned_queries), int(os.getenv("SEARCH_VARIANT_CONCURRENCY", "2")))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        result_sets = list(executor.map(
            lambda query: search_claim(query, max_results=max_results),
            cleaned_queries,
        ))

    combined = []
    seen_hrefs = set()
    for results in result_sets:
        for result in results:
            href = result.get("href", "")
            if not href or href in seen_hrefs:
                continue
            combined.append(result)
            seen_hrefs.add(href)

    return _rank_and_select_results(combined, " ".join(cleaned_queries), max_results)


def format_search_context(results: list[dict]) -> tuple[list[str], list[str]]:
    """
    Converte resultados de pesquisa em contexto formatado para o LLM.

    Returns:
        (context_parts, sources)
    """
    context_parts = []
    sources = []

    for r in results:
        title = r.get("title", "").strip()
        body = r.get("body", "").strip()
        href = r.get("href", "").strip()
        quality = r.get("source_quality", "media")

        if title and body:
            context_parts.append(
                f"Fonte: {href}\n"
                f"Confiabilidade da fonte: {quality}\n"
                f"Titulo: {title}\n"
                f"Excerto: {body}"
            )

        if href and href not in sources:
            sources.append(href)

    return context_parts, sources


def _clean_query(query: str) -> str:
    query = " ".join(str(query).split())
    suffix = os.getenv("SEARCH_QUERY_SUFFIX", "").strip()
    if suffix and suffix.lower() not in query.lower():
        query = f"{query} {suffix}"
    return query


def _rank_and_select_results(results: list[dict], query: str, max_results: int) -> list[dict]:
    ranked = sorted(results, key=lambda result: _result_score(result, query), reverse=True)
    selected = []
    selected_domains = set()

    for result in ranked:
        href = result.get("href", "") or ""
        domain = _domain_from_url(href)
        if not domain:
            continue
        if domain in selected_domains and len(ranked) > max_results:
            continue

        item = dict(result)
        item["source_domain"] = domain
        item["source_quality"] = _source_quality(domain)
        selected.append(item)
        selected_domains.add(domain)

        if len(selected) >= max_results:
            return selected

    if len(selected) < max_results:
        selected_hrefs = {item.get("href", "") for item in selected}
        for result in ranked:
            href = result.get("href", "") or ""
            if not href or href in selected_hrefs:
                continue
            domain = _domain_from_url(href)
            item = dict(result)
            item["source_domain"] = domain
            item["source_quality"] = _source_quality(domain)
            selected.append(item)
            selected_hrefs.add(href)
            if len(selected) >= max_results:
                break

    return selected


def _result_score(result: dict, query: str) -> int:
    href = result.get("href", "") or ""
    domain = _domain_from_url(href)
    score = 0
    if any(domain.endswith(d) for d in _TRUSTED_DOMAINS):
        score += 100
    if any(domain.endswith(d) for d in _LOW_QUALITY_DOMAINS):
        score -= 80
    title_body = f"{result.get('title', '')} {result.get('body', '')}".lower()
    if "fact check" in title_body or "fact-check" in title_body:
        score += 20
    if "transcript" in title_body or "debate" in title_body:
        score += 8
    query_terms = {term for term in re_split_words(query) if len(term) > 3}
    text_terms = set(re_split_words(title_body))
    if query_terms:
        score += int(20 * (len(query_terms & text_terms) / len(query_terms)))
        ordered_hits = _ordered_query_hits(query_terms, title_body)
        score += min(30, ordered_hits * 6)
    if domain.endswith(".gov") or ".gov." in domain:
        score += 60
    if "wikipedia.org" in domain:
        score -= 20
    if "youtube.com" in domain:
        score -= 30
    return score


def _domain_from_url(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _source_quality(domain: str) -> str:
    if any(domain.endswith(d) for d in _TRUSTED_DOMAINS):
        return "alta"
    if any(domain.endswith(d) for d in _LOW_QUALITY_DOMAINS):
        return "baixa"
    return "media"


def re_split_words(text: str) -> list[str]:
    return [part for part in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split() if part]


def _ordered_query_hits(query_terms: set[str], text: str) -> int:
    hits = 0
    words = re_split_words(text)
    for idx in range(len(words) - 1):
        if words[idx] in query_terms and words[idx + 1] in query_terms:
            hits += 1
    return hits
