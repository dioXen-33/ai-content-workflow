"""Client Nano Banana Pro (Gemini 3 Pro Image) via l'API officielle Google.

Point d'attention specifique a ce projet : on envoie des frames de personnes
reelles filmees. Le modele oppose des refus de securite sur une partie d'entre
elles. Ces refus sont classes `SAFETY_BLOCK` et ne sont jamais rejoues : renvoyer
la meme requete produirait le meme refus en consommant du credit a chaque fois.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..config import settings
from ..models import PRICING, FailureKind, PipelineError, image_cost

_client = None
_client_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Frein global en cas de surcharge du modele
#
# Quand Gemini repond 503 "high demand", le pire reflexe est de laisser les N
# workers reessayer chacun de leur cote : on multiplie les appels sur une API
# deja saturee. On partage donc une fenetre de repos entre toutes les taches.
# ---------------------------------------------------------------------------

_overload_until: float = 0.0
_overload_streak: int = 0

# Paliers d'attente, en secondes, appliques aux echecs successifs.
_OVERLOAD_BACKOFF = (30, 90, 240, 600, 900)


def _note_overload() -> float:
    """Enregistre une surcharge et renvoie le delai d'attente retenu."""
    global _overload_until, _overload_streak
    import random

    delay = _OVERLOAD_BACKOFF[min(_overload_streak, len(_OVERLOAD_BACKOFF) - 1)]
    _overload_streak += 1
    # Un peu d'aleatoire pour que les taches ne repartent pas toutes ensemble.
    delay = delay * random.uniform(0.85, 1.25)
    _overload_until = max(_overload_until, time.monotonic() + delay)
    return delay


def _note_success() -> None:
    global _overload_streak
    _overload_streak = 0


async def _await_cooldown() -> None:
    """Attend la fin de la fenetre de repos, si une surcharge a ete detectee."""
    while True:
        remaining = _overload_until - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 15))


def overload_status() -> dict:
    remaining = max(0.0, _overload_until - time.monotonic())
    return {"cooling_down": remaining > 0, "seconds_left": round(remaining, 1)}


async def _get_client():
    global _client
    async with _client_lock:
        if _client is None:
            if not settings.gemini_api_key:
                raise PipelineError(
                    FailureKind.INVALID_INPUT, "GEMINI_API_KEY manquant."
                )
            from google import genai

            _client = genai.Client(api_key=settings.gemini_api_key)
        return _client


def _build_config(aspect_ratio: str, image_size: str):
    """Config de generation, tolerante aux variations du SDK.

    `image_size` n'existe que sur les versions recentes de google-genai ; on ne
    le passe que s'il est reellement supporte, pour ne pas casser sur une version
    plus ancienne.
    """
    from google.genai import types

    image_kwargs: dict = {"aspect_ratio": aspect_ratio}
    fields = getattr(types.ImageConfig, "model_fields", {})
    if "image_size" in fields:
        image_kwargs["image_size"] = image_size

    try:
        image_config = types.ImageConfig(**image_kwargs)
    except Exception:
        image_config = types.ImageConfig(aspect_ratio=aspect_ratio)

    return types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=image_config,
    )


def _part_from_file(path: Path):
    from google.genai import types

    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


# Motifs de finish_reason qui traduisent un refus, pas une panne.
_SAFETY_MARKERS = {
    "SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "BLOCKLIST",
    "SPII", "RECITATION", "IMAGE_PROHIBITED_CONTENT",
}


def _extract_image(response) -> bytes:
    """Recupere les octets de l'image, ou leve une erreur classifiee."""
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        reason = str(feedback.block_reason)
        # `OTHER` et `UNSPECIFIED` ne designent pas un refus de contenu : ils
        # apparaissent notamment quand le modele est sous tension. On les rejoue
        # au lieu d'abandonner la video, contrairement a un vrai blocage de
        # securite qui donnerait le meme resultat a chaque tentative.
        if "OTHER" in reason.upper() or "UNSPECIFIED" in reason.upper():
            raise PipelineError(
                FailureKind.TRANSIENT,
                f"Gemini a refuse la requete sans motif precis ({reason}), "
                f"souvent signe d'une surcharge passagere. Nouvel essai prevu.",
                retry_after=45.0,
            )
        raise PipelineError(
            FailureKind.SAFETY_BLOCK,
            f"Requete bloquee en amont par Gemini : {reason}",
        )

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        # Reponse vide sans motif : plus souvent un hoquet de service qu'un refus.
        raise PipelineError(
            FailureKind.TRANSIENT,
            "Gemini n'a renvoye aucun candidat (reponse vide). Nouvel essai prevu.",
            retry_after=45.0,
        )

    candidate = candidates[0]
    finish = str(getattr(candidate, "finish_reason", "") or "").upper()

    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline is not None and getattr(inline, "data", None):
            mime = getattr(inline, "mime_type", "") or ""
            if mime.startswith("image/"):
                return inline.data

    if any(marker in finish for marker in _SAFETY_MARKERS):
        raise PipelineError(
            FailureKind.SAFETY_BLOCK,
            f"Nano Banana Pro a refuse de generer l'image (finish_reason={finish}). "
            f"Un nouvel essai identique donnerait le meme resultat.",
        )

    texts = [t for t in (getattr(p, "text", None) for p in parts) if t]
    detail = f" Reponse texte : {texts[0][:200]}" if texts else ""
    raise PipelineError(
        FailureKind.UNKNOWN,
        f"Aucune image dans la reponse (finish_reason={finish or 'inconnu'}).{detail}",
    )


def _is_overloaded(text: str) -> bool:
    """Surcharge temporaire du modele, distincte d'un quota epuise."""
    low = text.lower()
    return any(
        k in low
        for k in ("503", "unavailable", "high demand", "overloaded", "try again later")
    )


def _classify_exception(exc: Exception) -> FailureKind:
    text = f"{type(exc).__name__}: {exc}".lower()
    # La surcharge se teste avant le quota : le message 503 contient parfois
    # "resource" sans qu'il s'agisse d'un credit epuise.
    if _is_overloaded(text):
        return FailureKind.TRANSIENT
    if any(k in text for k in ("quota", "resource_exhausted", "billing", "insufficient")):
        return FailureKind.QUOTA
    if any(k in text for k in ("429", "rate limit", "deadline", "timeout",
                               "connection", "500")):
        return FailureKind.TRANSIENT
    if any(k in text for k in ("safety", "blocked", "prohibited")):
        return FailureKind.SAFETY_BLOCK
    if any(k in text for k in ("invalid", "400", "permission", "401", "403",
                               "not found", "404")):
        return FailureKind.INVALID_INPUT
    return FailureKind.UNKNOWN


async def edit_image(
    frame_path: Path,
    reference_path: Path,
    prompt: str,
    out_path: Path,
    aspect_ratio: str = "9:16",
    image_size: str = "2K",
) -> float:
    """Envoie frame + image de reference + prompt. Ecrit le resultat.

    Renvoie le cout estime en USD.
    """
    if settings.dry_run:
        # On recopie la frame : le pipeline reste testable de bout en bout.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(frame_path.read_bytes())
        return 0.0

    # Si tous les modeles sont sous tension, les taches patientent ensemble au
    # lieu de les marteler chacune de leur cote.
    await _await_cooldown()

    client = await _get_client()
    config = _build_config(aspect_ratio, image_size)

    # Ordre des images : reference d'abord, frame ensuite. Le prompt s'y refere
    # par position ("premiere image" = reference, "deuxieme image" = frame), donc
    # cet ordre doit rester identique ici et dans le mode batch.
    contents = [
        prompt,
        _part_from_file(reference_path),
        _part_from_file(frame_path),
    ]

    # Chaine de modeles : on bascule sur un repli des que le principal sature,
    # plutot que d'attendre passivement qu'il se libere.
    chain = settings.gemini_model_chain
    last_overload: Exception | None = None

    for index, model in enumerate(chain):
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
        except PipelineError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if _is_overloaded(message):
                last_overload = exc
                continue  # modele sature : on tente le suivant
            raise PipelineError(
                _classify_exception(exc),
                f"Appel {model} echoue : {exc}",
            ) from exc

        try:
            data = _extract_image(response)
        except PipelineError as exc:
            # Un refus sans motif precis sur le modele principal peut aussi
            # traduire une tension : on laisse sa chance au repli.
            if exc.kind == FailureKind.TRANSIENT and index < len(chain) - 1:
                last_overload = exc
                continue
            raise

        _note_success()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)

        if index > 0:
            print(f"[gemini] repli sur {model} (principal sature).", flush=True)
        return image_cost(model, image_size)

    # Toute la chaine est saturee : on impose une fenetre de repos partagee.
    delay = _note_overload()
    raise PipelineError(
        FailureKind.TRANSIENT,
        f"Tous les modeles image sont satures ({', '.join(chain)}). "
        f"Nouvel essai dans {delay / 60:.1f} min.",
        retry_after=delay,
    ) from last_overload


# ===========================================================================
# Batch API : 50 % du tarif interactif, traitement asynchrone (cible 24 h).
#
# Le pipeline traite des lots de plusieurs centaines de videos scrapees et n'a
# aucun besoin de reponse immediate : c'est exactement le profil vise par le
# Batch API. Le nom du batch est persiste en base des la soumission, car un
# batch soumis est deja facture -- un redemarrage doit reprendre le polling,
# jamais resoumettre.
# ===========================================================================

_TERMINAL_BATCH_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}


@dataclass
class BatchItem:
    key: str          # identifiant de correlation (= video_id)
    frame_path: Path


@dataclass
class BatchOutcome:
    images: dict[str, bytes]                  # key -> octets de l'image
    failures: dict[str, tuple[FailureKind, str]]  # key -> (nature, message)


def _encoded_image(path: Path, max_px: int) -> tuple[str, str]:
    """Redimensionne et encode une image en base64.

    Les frames sortent en 720x1280 ou plus ; les reduire divise la taille du
    JSONL par 3 a 5 sans rien changer a ce que le modele percoit, et evite de
    frôler la limite de 2 Go par fichier sur les gros lots.
    """
    with Image.open(path) as img:
        img = img.convert("RGB")
        if max(img.size) > max_px:
            ratio = max_px / max(img.size)
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _build_request(
    item: BatchItem,
    prompt: str,
    reference_b64: str,
    reference_mime: str,
    aspect_ratio: str,
    image_size: str,
) -> dict:
    """Une ligne du JSONL, au format GenerateContentRequest (proto JSON)."""
    frame_b64, frame_mime = _encoded_image(item.frame_path, settings.gemini_input_max_px)
    return {
        "key": item.key,
        "request": {
            "contents": [
                {
                    "role": "user",
                    # Meme ordre qu'en mode interactif : reference, puis frame.
                    "parts": [
                        {"text": prompt},
                        {"inlineData": {"mimeType": reference_mime, "data": reference_b64}},
                        {"inlineData": {"mimeType": frame_mime, "data": frame_b64}},
                    ],
                }
            ],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                    "imageSize": image_size,
                },
            },
        },
    }


async def submit_batch(
    items: list[BatchItem],
    reference_path: Path,
    prompt: str,
    workdir: Path,
    aspect_ratio: str = "9:16",
    image_size: str = "2K",
) -> str:
    """Construit le JSONL, l'envoie et cree le batch. Renvoie son nom Gemini."""
    if not items:
        raise PipelineError(FailureKind.INVALID_INPUT, "Batch vide.")

    client = await _get_client()
    workdir.mkdir(parents=True, exist_ok=True)

    reference_b64, reference_mime = await asyncio.to_thread(
        _encoded_image, reference_path, settings.gemini_input_max_px
    )

    jsonl_path = workdir / f"batch_{int(time.time())}.jsonl"

    def _write() -> None:
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for item in items:
                req = _build_request(
                    item, prompt, reference_b64, reference_mime,
                    aspect_ratio, image_size,
                )
                fh.write(json.dumps(req, ensure_ascii=False) + "\n")

    await asyncio.to_thread(_write)

    try:
        from google.genai import types

        uploaded = await client.aio.files.upload(
            file=str(jsonl_path),
            config=types.UploadFileConfig(
                display_name=jsonl_path.name, mime_type="jsonl"
            ),
        )
        job = await client.aio.batches.create(
            model=settings.gemini_image_model,
            src=uploaded.name,
            config={"display_name": f"workflow-ia-{jsonl_path.stem}"},
        )
    except Exception as exc:
        raise PipelineError(
            _classify_exception(exc), f"Soumission du batch Gemini echouee : {exc}"
        ) from exc

    return job.name


async def batch_state(name: str) -> str:
    client = await _get_client()
    try:
        job = await client.aio.batches.get(name=name)
    except Exception as exc:
        raise PipelineError(
            _classify_exception(exc), f"Lecture du batch {name} echouee : {exc}"
        ) from exc
    state = getattr(job.state, "name", None) or str(job.state)
    return state


async def wait_for_batch(name: str, on_tick=None) -> str:
    """Attend qu'un batch atteigne un etat terminal. Renvoie cet etat."""
    waited = 0.0
    limit = settings.gemini_batch_max_wait_h * 3600
    interval = max(settings.gemini_batch_poll_interval, 10)

    while waited < limit:
        state = await batch_state(name)
        if state in _TERMINAL_BATCH_STATES:
            return state
        if on_tick:
            on_tick(state, waited)
        await asyncio.sleep(interval)
        waited += interval

    raise PipelineError(
        FailureKind.TRANSIENT,
        f"Batch {name} toujours en cours apres "
        f"{settings.gemini_batch_max_wait_h:.0f} h. Il continue cote Google : son "
        f"nom est enregistre, une relance du job reprendra le polling sans "
        f"resoumettre ni repayer.",
    )


def _extract_from_batch_line(payload: dict) -> tuple[bytes | None, FailureKind, str]:
    """Analyse une ligne de resultat. Renvoie (image, nature, message)."""
    if "error" in payload and payload["error"]:
        err = payload["error"]
        message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return None, _classify_exception(Exception(message)), message

    response = payload.get("response") or {}

    feedback = response.get("promptFeedback") or response.get("prompt_feedback") or {}
    blocked = feedback.get("blockReason") or feedback.get("block_reason")
    if blocked:
        # Meme regle qu'en mode interactif : `OTHER` et `UNSPECIFIED` traduisent
        # le plus souvent une tension passagere, pas un refus de contenu. On les
        # rend rejouables, contrairement a un vrai blocage de securite.
        reason = str(blocked).upper()
        if "OTHER" in reason or "UNSPECIFIED" in reason:
            return None, FailureKind.TRANSIENT, (
                f"Gemini a refuse sans motif precis ({blocked}), souvent signe "
                f"d'une surcharge passagere."
            )
        return None, FailureKind.SAFETY_BLOCK, f"Requete bloquee en amont : {blocked}"

    candidates = response.get("candidates") or []
    if not candidates:
        return None, FailureKind.TRANSIENT, "Aucun candidat renvoye (reponse vide)."

    candidate = candidates[0]
    finish = str(candidate.get("finishReason") or candidate.get("finish_reason") or "").upper()
    parts = (candidate.get("content") or {}).get("parts") or []

    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or ""
            if mime.startswith("image/"):
                return base64.b64decode(inline["data"]), FailureKind.UNKNOWN, ""

    if any(marker in finish for marker in _SAFETY_MARKERS):
        return None, FailureKind.SAFETY_BLOCK, (
            f"Nano Banana Pro a refuse de generer l'image (finish_reason={finish})."
        )

    texts = [p.get("text") for p in parts if p.get("text")]
    detail = f" Reponse texte : {texts[0][:200]}" if texts else ""
    return None, FailureKind.UNKNOWN, (
        f"Aucune image dans la reponse (finish_reason={finish or 'inconnu'}).{detail}"
    )


async def collect_batch(name: str) -> BatchOutcome:
    """Telecharge et decode les resultats d'un batch termine."""
    client = await _get_client()

    try:
        job = await client.aio.batches.get(name=name)
    except Exception as exc:
        raise PipelineError(
            _classify_exception(exc), f"Lecture du batch {name} echouee : {exc}"
        ) from exc

    state = getattr(job.state, "name", None) or str(job.state)
    if state != "JOB_STATE_SUCCEEDED":
        raise PipelineError(
            FailureKind.UNKNOWN,
            f"Batch {name} termine en {state} : "
            f"{getattr(job, 'error', None) or 'sans detail'}",
        )

    dest = getattr(job, "dest", None)
    file_name = getattr(dest, "file_name", None) if dest else None

    lines: list[str] = []
    if file_name:
        raw = await client.aio.files.download(file=file_name)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
    else:
        # Les petits batchs peuvent revenir en reponses inline.
        for inlined in (getattr(dest, "inlined_responses", None) or []):
            lines.append(
                inlined if isinstance(inlined, str) else json.dumps(inlined, default=str)
            )

    images: dict[str, bytes] = {}
    failures: dict[str, tuple[FailureKind, str]] = {}

    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = payload.get("key") or payload.get("custom_id")
        if not key:
            continue
        data, kind, message = _extract_from_batch_line(payload)
        if data:
            images[key] = data
        else:
            failures[key] = (kind, message)

    return BatchOutcome(images=images, failures=failures)


def batch_unit_cost(image_size: str) -> float:
    table = PRICING["gemini_image_batch"]
    return table.get(image_size, table["2K"])


async def check_credentials() -> tuple[bool, str]:
    """Diagnostic : la cle est-elle valide et le modele accessible ?"""
    if not settings.gemini_api_key:
        return False, "GEMINI_API_KEY manquant"
    try:
        client = await _get_client()
        models = await client.aio.models.list()
        names = []
        async for m in models:
            names.append(getattr(m, "name", "") or "")
            if len(names) > 200:
                break
        target = settings.gemini_image_model
        found = any(target in n for n in names)
        if found:
            return True, f"Cle valide, modele {target} accessible"
        return True, (
            f"Cle valide, mais {target} n'apparait pas dans la liste des modeles "
            f"de ce compte. Il reste peut-etre appelable (les modeles preview ne "
            f"sont pas tous listes)."
        )
    except Exception as exc:
        return False, f"{exc}"
