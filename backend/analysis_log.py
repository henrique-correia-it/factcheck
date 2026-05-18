"""
analysis_log.py
---------------
Human-readable per-run analysis logs written to the project codex folder.
"""

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class AnalysisRunLogger:
    """Thread-safe text logger for one fact-check analysis run."""

    def __init__(self, video_id: str, url: str, base_dir: Path | None = None):
        self.started_at = time.perf_counter()
        self.lock = threading.Lock()
        self.video_id = video_id
        self.url = url

        project_root = Path(__file__).resolve().parent.parent
        log_dir = base_dir or project_root / "codex"
        log_dir.mkdir(parents=True, exist_ok=True)

        safe_video_id = re.sub(r"[^0-9A-Za-z_-]+", "_", video_id or "unknown")
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        self.path = log_dir / f"analysis_log_{safe_video_id}_{stamp}.txt"

        self._write_header()

    def log(self, event: str, **fields: Any) -> None:
        elapsed = time.perf_counter() - self.started_at
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        lines = [f"[{now}] +{elapsed:8.3f}s {event}"]

        for key, value in fields.items():
            formatted = self._format_value(value)
            if "\n" in formatted:
                lines.append(f"  {key}:")
                lines.extend(f"    {line}" for line in formatted.splitlines())
            else:
                lines.append(f"  {key}: {formatted}")

        self._append("\n".join(lines) + "\n\n")

    def close(self, **fields: Any) -> None:
        self.log("LOG_CLOSED", **fields)

    def _write_header(self) -> None:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        header = (
            "FACTCHECK ANALYSIS LOG\n"
            "======================\n"
            f"Started at: {now}\n"
            f"Video ID: {self.video_id}\n"
            f"URL: {self.url}\n\n"
        )
        self.path.write_text(header, encoding="utf-8")

    def _append(self, text: str) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(text)

    def _format_value(self, value: Any) -> str:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        return str(value)
