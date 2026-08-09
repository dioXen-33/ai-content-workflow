"""Recupere les images des batchs Gemini encore stockes chez Google.

Utile si la base locale a ete perdue : les resultats de batch restent
disponibles cote Google et contiennent les images generees par Nano Banana Pro.

    python recover_batches.py
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime
from pathlib import Path

import app  # noqa: F401  (active truststore)
from app.config import settings

OUT = settings.data_path / "recuperation"


def main() -> int:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    OUT.mkdir(parents=True, exist_ok=True)

    total_images = 0
    for batch in client.batches.list(config={"page_size": 50}):
        state = getattr(batch.state, "name", None) or str(batch.state)
        if state != "JOB_STATE_SUCCEEDED":
            continue

        dest = getattr(batch, "dest", None)
        file_name = getattr(dest, "file_name", None) if dest else None
        if not file_name:
            continue

        created = getattr(batch, "create_time", None)
        stamp = created.strftime("%Y%m%d-%H%M") if created else "inconnu"
        folder = OUT / f"batch-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)

        raw = client.files.download(file=file_name)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")

        found = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            key = payload.get("key") or f"item{found}"
            response = payload.get("response") or {}
            for cand in response.get("candidates") or []:
                parts = (cand.get("content") or {}).get("parts") or []
                for part in parts:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if not inline or not inline.get("data"):
                        continue
                    mime = inline.get("mimeType") or inline.get("mime_type") or ""
                    if not mime.startswith("image/"):
                        continue
                    ext = ".png" if "png" in mime else ".jpg"
                    (folder / f"{key}{ext}").write_bytes(
                        base64.b64decode(inline["data"])
                    )
                    found += 1

        total_images += found
        print(f"  {stamp} -> {found} image(s) dans {folder}")

    print(f"\n{total_images} image(s) recuperee(s) dans {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
