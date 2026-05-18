"""
transcript_handler.py
---------------------
Extrai transcrições de vídeos do YouTube.

Compatível com youtube-transcript-api >= 1.0.0, onde a classe
passou a ser instanciada em vez de usar métodos estáticos.
"""

import re
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

def get_video_metadata(url: str) -> dict:
    """
    Extrai metadados do vídeo (título, data de carregamento).
    """
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title", "Desconhecido"),
            "upload_date": info.get("upload_date", "Desconhecida"), # YYYYMMDD
        }


def extract_video_id(url: str) -> str:
    """
    Extrai o ID de 11 caracteres de um URL do YouTube.

    Suporta:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://youtu.be/VIDEO_ID
      - https://www.youtube.com/embed/VIDEO_ID
      - ID direto (11 caracteres)
    """
    patterns = [
        r"(?:v=|\/videos\/|embed\/|youtu\.be\/|\/v\/|watch\?v=)([0-9A-Za-z_-]{11})",
        r"^([0-9A-Za-z_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url.strip())
        if match:
            return match.group(1)

    raise ValueError(f"Não foi possível extrair o video ID de: '{url}'")


def get_transcript(url: str, languages: list[str] | None = None) -> list[dict]:
    """
    Obtém a transcrição de um vídeo do YouTube.

    Compatível com youtube-transcript-api v1.x (API instanciada).

    Args:
        url: URL ou ID do vídeo.
        languages: Idiomas por ordem de preferência.

    Returns:
        Lista de segmentos: [{"text": "...", "start": 0.0, "duration": 3.0}, ...]
    """
    video_id = extract_video_id(url)

    if languages is None:
        languages = ["pt", "pt-BR", "pt-PT", "en", "en-US", "en-GB", "es", "fr", "de"]

    # Instancia a API (necessário em v1.x)
    api = YouTubeTranscriptApi()

    try:
        # --- Tentativa 1: idiomas preferidos ---
        fetched = api.fetch(video_id, languages=languages)
        return _to_list(fetched)

    except NoTranscriptFound:
        # --- Tentativa 2: qualquer transcrição disponível ---
        try:
            transcript_list = api.list(video_id)
            for transcript_obj in transcript_list:
                try:
                    fetched = transcript_obj.fetch()
                    return _to_list(fetched)
                except Exception:
                    continue
            raise Exception("Nenhuma transcrição utilizável encontrada.")
        except Exception as inner:
            raise Exception(
                f"Nenhuma transcrição encontrada para este vídeo. "
                f"Pode não ter legendas activas. Detalhe: {inner}"
            )

    except TranscriptsDisabled:
        raise Exception("As legendas estão desactivadas pelo autor deste vídeo.")

    except VideoUnavailable:
        raise Exception("Vídeo indisponível. Verifica se o link é público e correcto.")

    except Exception as err:
        raise Exception(f"Erro inesperado ao obter transcrição: {err}")


def _to_list(fetched) -> list[dict]:
    """
    Converte o objecto FetchedTranscript (v1.x) para uma lista de dicionários
    compatível com o resto do pipeline.
    """
    result = []
    for snippet in fetched:
        # Em v1.x os snippets são objectos com atributos .text, .start, .duration
        # Em v0.x eram dicionários — suporta ambos por segurança
        if hasattr(snippet, "text"):
            text  = snippet.text
            start = float(snippet.start)
            dur   = float(snippet.duration)
        else:
            text  = snippet.get("text", "")
            start = float(snippet.get("start", 0.0))
            dur   = float(snippet.get("duration", 3.0))

        # Limpa tags HTML residuais
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"').strip()

        if text:
            result.append({"text": text, "start": start, "duration": dur})

    return result
