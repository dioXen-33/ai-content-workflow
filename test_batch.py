"""Tests hors-ligne du Batch API Gemini.

Verifie ce qui ne depend pas du reseau : format du JSONL envoye, taille des
images apres redimensionnement, et decodage de toutes les formes de reponse
possibles (succes, blocage securite, erreur, absence d'image).

    python test_batch.py
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

from app.clients.gemini import (
    BatchItem,
    _build_request,
    _encoded_image,
    _extract_from_batch_line,
)
from app.models import FailureKind

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK  " if condition else "ECHEC"
    print(f"  [{status}] {label}{(' -> ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def test_encoding(tmp: Path) -> None:
    print("\nRedimensionnement et encodage")
    big = tmp / "frame.png"
    Image.new("RGB", (2160, 3840), (200, 120, 60)).save(big)

    data, mime = _encoded_image(big, max_px=1280)
    raw = base64.b64decode(data)
    out = tmp / "decoded.jpg"
    out.write_bytes(raw)
    with Image.open(out) as img:
        w, h = img.size

    check("mime JPEG", mime == "image/jpeg", mime)
    check("plus grand cote plafonne a 1280", max(w, h) == 1280, f"{w}x{h}")
    check("ratio preserve", abs((w / h) - (2160 / 3840)) < 0.01, f"{w/h:.3f}")
    check(
        "poids reduit sous 400 Ko",
        len(raw) < 400_000,
        f"{len(raw) // 1024} Ko pour une source 2160x3840",
    )


def test_jsonl(tmp: Path) -> None:
    print("\nFormat du JSONL")
    frame = tmp / "f.png"
    ref = tmp / "r.png"
    Image.new("RGB", (720, 1280), (30, 60, 90)).save(frame)
    Image.new("RGB", (800, 800), (180, 40, 40)).save(ref)

    ref_b64, ref_mime = _encoded_image(ref, max_px=1280)
    req = _build_request(
        BatchItem(key="vid123", frame_path=frame),
        prompt="Remplace le personnage",
        reference_b64=ref_b64,
        reference_mime=ref_mime,
        aspect_ratio="9:16",
        image_size="2K",
    )

    line = json.dumps(req)          # doit etre serialisable tel quel
    parsed = json.loads(line)
    parts = parsed["request"]["contents"][0]["parts"]
    cfg = parsed["request"]["generationConfig"]

    def _shape(part: dict) -> tuple[int, int]:
        """Dimensions de l'image encodee dans une part, pour l'identifier."""
        raw = base64.b64decode(part["inlineData"]["data"])
        with Image.open(io.BytesIO(raw)) as img:
            return img.size

    check("cle de correlation = video_id", parsed["key"] == "vid123")
    check("role user present", parsed["request"]["contents"][0]["role"] == "user")
    check("3 parts : prompt + reference + frame", len(parts) == 3, str(len(parts)))
    check("part 1 = texte du prompt", parts[0]["text"] == "Remplace le personnage")
    check("part 2 et 3 en inlineData",
          "inlineData" in parts[1] and "inlineData" in parts[2])

    # Verification stricte de l'ordre : la reference est carree (800x800), la
    # frame est verticale (720x1280). Les dimensions les distinguent sans
    # ambiguite, donc une inversion serait detectee.
    ref_shape, frame_shape = _shape(parts[1]), _shape(parts[2])
    check("part 2 = IMAGE DE REFERENCE (carree)",
          ref_shape[0] == ref_shape[1], f"{ref_shape[0]}x{ref_shape[1]}")
    check("part 3 = FRAME de la video (verticale)",
          frame_shape[1] > frame_shape[0], f"{frame_shape[0]}x{frame_shape[1]}")

    check("mimeType renseigne", parts[1]["inlineData"]["mimeType"] == "image/jpeg")
    check("base64 decodable", bool(base64.b64decode(parts[1]["inlineData"]["data"])))
    check("responseModalities = IMAGE", cfg["responseModalities"] == ["IMAGE"])
    check("aspectRatio transmis", cfg["imageConfig"]["aspectRatio"] == "9:16")
    check("imageSize transmis", cfg["imageConfig"]["imageSize"] == "2K")


def test_parsing() -> None:
    print("\nDecodage des reponses")
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()

    cases = [
        (
            "succes : image extraite",
            {"key": "a", "response": {"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": png}}]}}]}},
            lambda d, k, m: d is not None,
        ),
        (
            "snake_case accepte aussi",
            {"key": "b", "response": {"candidates": [{"content": {"parts": [
                {"inline_data": {"mime_type": "image/png", "data": png}}]}}]}},
            lambda d, k, m: d is not None,
        ),
        (
            "blocage amont -> SAFETY_BLOCK",
            {"key": "c", "response": {"promptFeedback": {"blockReason": "SAFETY"}}},
            lambda d, k, m: d is None and k == FailureKind.SAFETY_BLOCK,
        ),
        (
            "finish_reason IMAGE_SAFETY -> SAFETY_BLOCK",
            {"key": "d", "response": {"candidates": [
                {"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]}},
            lambda d, k, m: d is None and k == FailureKind.SAFETY_BLOCK,
        ),
        (
            "erreur de quota -> QUOTA",
            {"key": "e", "error": {"message": "RESOURCE_EXHAUSTED: quota exceeded"}},
            lambda d, k, m: d is None and k == FailureKind.QUOTA,
        ),
        (
            "reponse texte sans image -> UNKNOWN",
            {"key": "f", "response": {"candidates": [
                {"finishReason": "STOP", "content": {"parts": [
                    {"text": "Je ne peux pas faire cela."}]}}]}},
            lambda d, k, m: d is None and k == FailureKind.UNKNOWN and "cela" in m,
        ),
        (
            "aucun candidat -> TRANSIENT (rejouable)",
            {"key": "g", "response": {"candidates": []}},
            lambda d, k, m: d is None and k == FailureKind.TRANSIENT,
        ),
        (
            "blockReason OTHER -> TRANSIENT (pas un refus de contenu)",
            {"key": "h", "response": {"promptFeedback": {"blockReason": "OTHER"}}},
            lambda d, k, m: d is None and k == FailureKind.TRANSIENT,
        ),
    ]

    for label, payload, predicate in cases:
        data, kind, message = _extract_from_batch_line(payload)
        check(label, predicate(data, kind, message), f"{kind}")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_encoding(tmp)
        test_jsonl(tmp)
        test_parsing()

    print()
    if failures:
        print(f"{len(failures)} echec(s) : {', '.join(failures)}")
        return 1
    print("Tous les tests passent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
