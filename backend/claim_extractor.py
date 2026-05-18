"""
claim_extractor.py
------------------
Extrai afirmacoes factuais verificaveis de transcricoes de debates.

Este modulo deve funcionar para qualquer assunto e idioma. Por isso, a decisao
principal fica no LLM e nao em regras hardcoded para um video ou tema concreto.
"""

import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
logger = logging.getLogger(__name__)

EXTRACT_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """Voce e um verificador de factos especializado em debates, entrevistas e discursos.
O seu objetivo e extrair TODAS as afirmacoes factuais verificaveis que um espectador esperaria ver confirmadas.
Funcione em qualquer idioma e qualquer assunto.

Extraia afirmacoes sobre:
- numeros, percentagens, datas, prazos, custos, crescimento, desemprego, crimes ou resultados;
- leis, votos, tribunais, tratados, politicas publicas, programas e promessas ja feitas;
- citacoes atribuidas, documentos divulgados, emails, relatorios, estudos ou sondagens;
- acusacoes concretas sobre pessoas, empresas, governos, guerras, eleicoes ou acontecimentos passados.

NAO extraia:
1. Opinioes puras, insultos ou slogans sem conteudo factual verificavel.
2. Promessas futuras, previsoes e consequencias especulativas ("vai beneficiar", "sera um desastre") sem facto historico verificavel.
3. Generalizacoes vagas sobre comportamento ("sempre que perde diz que...", "nunca respeita...") sem evento, numero, data ou documento concreto.
4. Frases demasiado vagas que nao permitam uma pesquisa objetiva.

REGRAS:
- Mantenha a afirmacao no idioma original quando isso preservar melhor o significado.
- Se a transcricao estiver num idioma claro, mantenha as claims nesse idioma; nao traduza para portugues so porque estas instrucoes estao em portugues.
- Resolva pronomes e referencias usando o contexto anterior e o contexto do video.
- Use "X disse/afirmou" apenas quando o facto verificavel for uma citacao ou atribuicao concreta. Caso contrario, extraia o facto subjacente.
- Inclua "evidence_text" com a frase curta e literal do trecho que originou a claim. Nao traduza este campo.
- Nao transforme frases como "ha fontes que verificam..." numa claim sobre fontes. Extraia apenas o facto subjacente.
- Nao atribua uma acao a uma pessoa so porque essa pessoa foi mencionada numa pergunta, chamada pelo nome, ou como objeto da frase.
- Se a evidencia estiver em primeira pessoa ("I", "my", "we", "eu", "meu", "nos"), so atribua a uma pessoa nomeada quando o falante estiver claro no proprio trecho ou no contexto imediatamente anterior. Se nao estiver claro, extraia sem nomear o falante ou ignore.
- Se uma pessoa aparece como destinatario ou referencia externa ("Donald?", "as well Donald's", "senhor X"), essa pessoa NAO e o falante dessa frase.
- Nao corte frases a meio.
- Se houver varias afirmacoes verificaveis no trecho, devolva varias.
- Crie "search_query" no melhor idioma para pesquisar o facto. Use nomes proprios, datas e entidades.
- A query deve ter 4 a 10 termos, sem frases longas.

CONTEXTO DO VIDEO:
{context}

RESPONDA APENAS COM JSON:
{{
  "claims": [
    {{
      "texto": "afirmacao factual completa com referencias resolvidas",
      "search_query": "termos de pesquisa precisos",
      "evidence_text": "frase literal curta do trecho"
    }}
  ]
}}

Se nao houver factos verificaveis, responda: {{"claims": []}}
""",
        ),
        (
            "human",
            "Contexto anterior:\n{previous_chunk}\n\nTrecho a analisar:\n{text}",
        ),
    ]
)


class ClaimExtractor:
    """Extractor geral de afirmacoes factuais para fact-checking."""

    def __init__(self):
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        llm_model = os.getenv("LLM_MODEL", "gemma4:31b-cloud")

        self.llm = ChatOllama(
            model=llm_model,
            base_url=base_url,
            temperature=0,
            format="json",
            num_predict=int(os.getenv("CLAIM_EXTRACT_NUM_PREDICT", "800")),
            sync_client_kwargs={
                "timeout": float(os.getenv("CLAIM_EXTRACT_TIMEOUT_SECONDS", os.getenv("LLM_TIMEOUT_SECONDS", "45")))
            },
        )
        self.chain = EXTRACT_PROMPT | self.llm

    def extract(self, text: str, previous_chunk: str = "", context: str = "") -> list[dict]:
        """
        Extrai afirmacoes factuais de um trecho.
        Retorna uma lista de dicionarios {"text": "...", "query": "..."}.
        """
        if not text or len(text.strip()) < 30:
            return []

        try:
            return self._extract_once(text, previous_chunk, context)
        except Exception as err:
            logger.warning(f"Extractor falhou no trecho completo; a tentar janelas menores: {err}")
            return self._extract_with_smaller_windows(text, previous_chunk, context)

    def _extract_once(self, text: str, previous_chunk: str, context: str) -> list[dict]:
        response = self.chain.invoke(
            {
                "text": text,
                "previous_chunk": previous_chunk,
                "context": context,
            }
        )
        parsed = _extract_json(response.content.strip())

        valid_claims = []
        source_text = f"{previous_chunk}\n{text}"
        if isinstance(parsed, dict) and "claims" in parsed:
            for item in parsed["claims"]:
                if not isinstance(item, dict):
                    continue

                claim_text = str(item.get("texto", "")).strip()
                query = str(item.get("search_query", "")).strip()
                evidence = str(item.get("evidence_text", "")).strip()

                if (
                    claim_text
                    and query
                    and self._is_substantive(claim_text)
                    and self._attribution_matches_evidence(claim_text, evidence, source_text)
                ):
                    valid_claims.append({
                        "text": claim_text,
                        "query": query,
                        "evidence": evidence,
                    })

        return _dedupe_claims(valid_claims)

    def _extract_with_smaller_windows(self, text: str, previous_chunk: str, context: str) -> list[dict]:
        """Retry geral para quando o modelo fica preso num trecho grande ou ambíguo."""
        valid_claims = []
        rolling_context = previous_chunk
        max_parts = int(os.getenv("CLAIM_EXTRACT_RETRY_PARTS", "4"))
        for part in _split_text_windows(text)[:max_parts]:
            try:
                valid_claims.extend(self._extract_once(part, rolling_context[-500:], context))
            except Exception as err:
                logger.warning(f"Extractor falhou numa janela menor: {err}")
            rolling_context = f"{rolling_context} {part}"
        return _dedupe_claims(valid_claims)

    def _is_substantive(self, claim_text) -> bool:
        """Remove respostas demasiado curtas ou narrativas em vez de claims."""
        c = str(claim_text).strip()
        if len(c) < 28:
            return False

        lower = c.lower()
        narrative_prefixes = (
            "o candidato disse",
            "a candidata disse",
            "o orador afirmou",
            "a oradora afirmou",
            "ele disse",
            "ela disse",
            "he said",
            "she said",
            "the candidate said",
            "the speaker said",
        )
        if lower.startswith(narrative_prefixes):
            return False

        meta_patterns = (
            r"\b(afirma|afirmou|disse|says|said|claims|claimed)\b.{0,80}\b(fontes|sources|google|verificam|verify)\b",
            r"\b(go google it|procurem no google|pesquisem no google)\b",
            r"\b(afirma|afirmou|disse|says|said|claims|claimed)\b.{0,100}\b(declarou|afirmou|disse|stated|said|claimed)\b",
            r"\b(fontes|sources)\b.{0,80}\b(verificam|verify|confirmam|confirm)\b",
        )
        if any(re.search(pattern, lower, re.IGNORECASE) for pattern in meta_patterns):
            return False

        speculative_patterns = (
            r"\b(will|would|going to|vai|irao|irá|ser[aá]|dever[aá])\b.{0,80}\b(benefit|beneficiar|winner|vencedor|ganhar|destroy|destruir|rip|ruin|arruinar)\b",
            r"\b(the beneficiary|o beneficiario|o beneficiário|big winner|grande vencedor)\b",
        )
        if any(re.search(pattern, lower, re.IGNORECASE) for pattern in speculative_patterns):
            return False

        vague_behavior_patterns = (
            r"\b(every time|whenever|sempre que|todas as vezes)\b.{0,100}\b(claims?|says?|afirma|diz|alega)\b",
            r"\b(when|quando)\b.{0,100}\b(direction|dire)\w*\b.{0,100}\b(rigged|manipulad|viciado)\b",
            r"\b(rigged|manipulad|viciado)\b.{0,120}\b(when|quando)\b.{0,100}\b(direction|dire)\w*\b",
            r"\b(rigged|manipulad|viciado)\b.{0,120}\b(favor|correm|direction|dire)\w*\b",
            r"\b(nobody|no one|ninguem|ningu[eé]m)\b.{0,80}\b(more|mais|less|menos|better|worse|melhor|pior)\b",
            r"\b(always|never|sempre|nunca)\b.{0,80}\b(respect|respeit|admits?|admite|accepts?|aceita)\b",
        )
        if any(re.search(pattern, lower, re.IGNORECASE) for pattern in vague_behavior_patterns):
            return False

        return True

    def _attribution_matches_evidence(self, claim_text: str, evidence: str, source_text: str) -> bool:
        """Evita trocar a pessoa mencionada pela pessoa que fez uma frase em primeira pessoa."""
        if not evidence:
            return True

        evidence_lower = evidence.casefold()
        first_person = re.search(
            r"\b(i|i'm|i've|my|we|we're|we've|our|eu|meu|minha|nos|n[óo]s|nossa|nosso)\b",
            evidence_lower,
            re.IGNORECASE,
        )
        if not first_person:
            return True

        names = _candidate_person_names(claim_text)
        if not names:
            return True

        window = _evidence_window(source_text, evidence).casefold()
        for name in names:
            parts = name.split()
            first = re.escape(parts[0].casefold())
            last = re.escape(parts[-1].casefold())
            target = rf"(?:{first}|{last})"

            conflict_patterns = (
                rf"\b{target}\s*(?:'s|’s)\b",
                rf"\b(?:how long is it|is it|is that|sir|madam|senhor|senhora)\W+{target}\b",
                rf"\b{target}\W+(?:,|\?)",
                rf"\b(?:as well|tal como|assim como)\W+{target}\b",
            )
            if any(re.search(pattern, window, re.IGNORECASE) for pattern in conflict_patterns):
                logger.info(
                    "Claim descartada por conflito de atribuicao: %s | evidencia=%s",
                    claim_text,
                    evidence,
                )
                return False

        return True


def _dedupe_claims(claims: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for claim in claims:
        key = re.sub(r"\W+", " ", claim.get("text", "").casefold()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(claim)
    return deduped


def _extract_json(text: str):
    """Extrai o primeiro objeto JSON valido de uma string."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _split_text_windows(text: str) -> list[str]:
    max_words = int(os.getenv("CLAIM_EXTRACT_RETRY_WORDS", "85"))
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if not sentences:
        return [text]

    windows = []
    current = []
    current_words = 0
    for sentence in sentences:
        word_count = len(sentence.split())
        if current and current_words + word_count > max_words:
            windows.append(" ".join(current))
            current = []
            current_words = 0
        current.append(sentence)
        current_words += word_count
    if current:
        windows.append(" ".join(current))
    return windows


def _candidate_person_names(text: str) -> list[str]:
    matches = re.findall(
        r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç'-]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç'-]+){1,3}",
        text,
    )
    blocked = {
        "Estados Unidos",
        "Produto Interno",
        "Segurança Social",
        "Social Security",
        "Supreme Court",
        "Second Amendment",
    }
    names = []
    for match in matches:
        cleaned = match.strip()
        if cleaned in blocked:
            continue
        if cleaned not in names:
            names.append(cleaned)
    return names[:3]


def _evidence_window(source_text: str, evidence: str) -> str:
    source_norm = " ".join(source_text.split())
    evidence_norm = " ".join(evidence.split())
    idx = source_norm.casefold().find(evidence_norm.casefold())
    if idx == -1:
        return source_norm[:400]
    start = max(0, idx - 180)
    end = min(len(source_norm), idx + len(evidence_norm) + 220)
    return source_norm[start:end]
