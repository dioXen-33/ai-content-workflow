"""Traitement media : telechargement, sondage, extraction de frame, filtres.

Tout passe par ffmpeg/ffprobe en sous-processus, sans dependance Python lourde.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageStat

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# Instagram et TikTok servent leurs binaires derriere un CDN qui refuse les
# clients sans en-tetes credibles.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@dataclass
class Probe:
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool

    @property
    def aspect_ratio(self) -> str:
        if not self.width or not self.height:
            return "9:16"
        r = self.width / self.height
        candidates = {
            "9:16": 9 / 16, "3:4": 3 / 4, "1:1": 1.0,
            "4:3": 4 / 3, "16:9": 16 / 9, "21:9": 21 / 9,
        }
        return min(candidates, key=lambda k: abs(candidates[k] - r))


async def _run(*args: str, capture_stderr: bool = False) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out or b"", err or b""


# ---------------------------------------------------------------------------
# Telechargement
# ---------------------------------------------------------------------------


async def download(url: str, dest: Path, referer: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer

    tmp = dest.with_suffix(dest.suffix + ".part")
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(120.0, connect=20.0)
    ) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
    tmp.replace(dest)
    return dest


DRYRUN_SCHEME = "dryrun://"

# Video fournie directement par l'utilisateur : le fichier est deja sur disque,
# il n'y a rien a telecharger.
UPLOAD_SCHEME = "upload://"

# Video TikTok : on ne telecharge pas via yt-dlp (bloque par signature), mais en
# rejouant la page de la video pour en extraire une URL de media fraiche. Voir
# app/clients/tiktok_browser.download.
TIKTOK_SCHEME = "tiktokdl://"

_TEST_PATTERNS = ("testsrc2", "smptebars", "rgbtestsrc", "testsrc")


async def generate_test_video(dest: Path, duration: float = 12.0, seed: int = 0) -> Path:
    """Fabrique une video de test avec ffmpeg, sans reseau.

    Utilise par le mode dry-run : le pipeline se valide de bout en bout sans
    dependre d'Internet ni consommer le moindre credit.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    pattern = _TEST_PATTERNS[seed % len(_TEST_PATTERNS)]
    # La duree passe par `-t` et non par l'option `duration=` du filtre : tous les
    # generateurs lavfi ne l'exposent pas (`mandelbrot`, par exemple, ne l'a pas).
    code, _, err = await _run(
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"{pattern}=size=720x1280:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency={220 + seed * 110}",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest",
        "-movflags", "+faststart", str(dest),
        capture_stderr=True,
    )
    if code != 0:
        raise RuntimeError(
            f"Generation de la video de test echouee : "
            f"{err.decode('utf-8', 'replace')[-300:]}"
        )
    return dest


async def remux_to_mp4(src: Path, dest: Path) -> Path:
    """Convertit un conteneur video quelconque (.mov/.webm/.mkv...) en .mp4.

    On tente d'abord une copie de flux (rapide, sans perte) ; si les codecs ne
    sont pas compatibles MP4, on re-encode.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    copy_args = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-c", "copy", "-movflags", "+faststart", str(dest),
    ]
    code, _, _ = await _run(*copy_args)
    if code == 0 and dest.exists() and dest.stat().st_size > 0:
        return dest

    encode_args = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest),
    ]
    code, _, err = await _run(*encode_args, capture_stderr=True)
    if code != 0:
        raise RuntimeError(
            f"Conversion en mp4 echouee : {err.decode('utf-8', 'replace')[-200:]}"
        )
    return dest


# ---------------------------------------------------------------------------
# Sondage
# ---------------------------------------------------------------------------


async def probe(path: Path) -> Probe:
    code, out, _ = await _run(
        FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    )
    if code != 0:
        raise RuntimeError(f"ffprobe a echoue sur {path.name}")

    data = json.loads(out.decode("utf-8", "replace"))
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError(f"Aucun flux video dans {path.name}")

    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)

    fps = 0.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

    return Probe(
        duration_s=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )




# ---------------------------------------------------------------------------
# Extraction de la premiere frame
# ---------------------------------------------------------------------------

# La frame a t=0 est inexploitable dans une bonne partie des cas : fondu depuis
# le noir, flash de transition, frame de compression. On en extrait plusieurs et
# on garde la meilleure.
CANDIDATE_OFFSETS = (0.0, 0.3, 0.8, 1.5)


def _score_frame(path: Path) -> float:
    """Score de qualite d'une frame candidate.

    Penalise le noir, le blanc satures et les images plates (faible ecart-type),
    qui sont les symptomes d'un fondu ou d'une transition.
    """
    try:
        with Image.open(path) as img:
            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            mean = stat.mean[0]
            stddev = stat.stddev[0]

            rgb = img.convert("RGB")
            cstat = ImageStat.Stat(rgb)
            colorfulness = sum(cstat.stddev) / 3.0
    except Exception:
        return -1.0

    if mean < 12 or mean > 245:
        return -1.0  # quasiment noir ou crame

    # Cible une luminance moyenne : 1.0 a 128, decroit vers les extremes.
    exposure = 1.0 - abs(mean - 128) / 128
    detail = min(stddev / 60.0, 1.0)
    color = min(colorfulness / 60.0, 1.0)
    return 0.30 * exposure + 0.45 * detail + 0.25 * color


async def extract_first_frame(video_path: Path, out_dir: Path) -> tuple[Path, float]:
    """Extrait la meilleure frame de debut. Renvoie (chemin, score)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[tuple[float, Path]] = []

    for offset in CANDIDATE_OFFSETS:
        cand = out_dir / f"_cand_{offset:.1f}.jpg"
        code, _, _ = await _run(
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(offset), "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2", str(cand),
        )
        if code == 0 and cand.exists() and cand.stat().st_size > 0:
            candidates.append((await asyncio.to_thread(_score_frame, cand), cand))

    if not candidates:
        raise RuntimeError(f"Aucune frame extractible de {video_path.name}")

    best_score, best_path = max(candidates, key=lambda t: t[0])
    final = out_dir / "first_frame.jpg"
    best_path.replace(final)

    for _, cand in candidates:
        cand.unlink(missing_ok=True)

    return final, best_score


# ---------------------------------------------------------------------------
# Previsualisation navigateur
# ---------------------------------------------------------------------------

# Codecs que tous les navigateurs savent decoder en logiciel.
#
# HEVC en est volontairement absent : Instagram et TikTok servent beaucoup de
# videos en H.265, et une machine sans extension HEVC ni GPU (Windows Server,
# session RDP) ne rend alors aucune image -- le son passe, la video reste figee.
BROWSER_SAFE_CODECS = {"h264", "vp8", "vp9", "av1"}


async def video_codec(path: Path) -> str:
    """Nom du codec video, en minuscules. Chaine vide si indeterminable."""
    code, out, _ = await _run(
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path),
    )
    if code != 0:
        return ""
    return out.decode("utf-8", "replace").strip().lower()


async def needs_browser_transcode(path: Path) -> bool:
    codec = await video_codec(path)
    # Un codec indeterminable est traite comme lisible : mieux vaut tenter la
    # lecture directe que reencoder inutilement.
    return bool(codec) and codec not in BROWSER_SAFE_CODECS


async def transcode_for_browser(src: Path, dest: Path, max_height: int = 720) -> Path:
    """Reencode en H.264 pour la previsualisation.

    Definition volontairement reduite : cette copie ne sert qu'a regarder la
    video dans l'interface, jamais a la generation. Kling continue de recevoir
    la source d'origine, intacte.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.mp4")

    code, _, err = await _run(
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-vf", f"scale=-2:'min({max_height},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(tmp),
        capture_stderr=True,
    )
    if code != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"Conversion pour previsualisation echouee : "
            f"{err.decode('utf-8', 'replace')[-300:]}"
        )

    tmp.replace(dest)
    return dest


# ---------------------------------------------------------------------------
# Preparation de la source pour Kling
# ---------------------------------------------------------------------------


async def trim_for_kling(
    src: Path, dest: Path, max_duration_s: float, max_bytes: int = 95 * 1024 * 1024
) -> Path:
    """Prepare la video de reference selon les contraintes de Kling.

    Contraintes de l'API : .mp4/.mov, 100 Mo maximum, duree 3-30 s. On tronque
    au besoin, et on re-encode seulement si le fichier depasse la limite de
    taille -- une copie de flux est gratuite et sans perte.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = await probe(src)

    needs_trim = info.duration_s > max_duration_s
    too_big = src.stat().st_size > max_bytes

    if not needs_trim and not too_big:
        if src != dest:
            shutil.copy2(src, dest)
        return dest

    duration = min(info.duration_s, max_duration_s)

    if too_big:
        # Re-encodage cible pour tenir sous la limite.
        target_bitrate = int((max_bytes * 8 * 0.85) / max(duration, 1))
        target_bitrate = max(min(target_bitrate, 8_000_000), 800_000)
        args = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", str(target_bitrate), "-maxrate", str(target_bitrate),
            "-bufsize", str(target_bitrate * 2),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", str(dest),
        ]
    else:
        args = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-t", f"{duration:.3f}",
            "-c", "copy", "-movflags", "+faststart", str(dest),
        ]

    code, _, err = await _run(*args, capture_stderr=True)
    if code != 0:
        raise RuntimeError(
            f"Preparation de la video echouee : {err.decode('utf-8', 'replace')[-300:]}"
        )
    return dest


# ---------------------------------------------------------------------------
# Filtre de recevabilite
# ---------------------------------------------------------------------------


@dataclass
class Eligibility:
    ok: bool
    reason: str = ""


def check_eligibility(
    info: Probe,
    frame_score: float,
    min_duration_s: float,
    max_duration_s: float,
) -> Eligibility:
    """Verdict avant tout appel API payant.

    Chaque video ecartee ici, c'est environ 1,95 USD non depense.
    """
    if info.duration_s < 3.0:
        return Eligibility(False, "Duree < 3 s (minimum impose par Kling)")
    if info.duration_s < min_duration_s:
        return Eligibility(False, f"Duree {info.duration_s:.1f} s < minimum demande")
    if info.duration_s > max_duration_s:
        return Eligibility(False, f"Duree {info.duration_s:.1f} s > maximum demande")
    if frame_score < 0:
        return Eligibility(False, "Premiere frame inexploitable (noire ou saturee)")
    if frame_score < 0.20:
        return Eligibility(False, f"Premiere frame de trop faible qualite ({frame_score:.2f})")
    if not info.width or not info.height:
        return Eligibility(False, "Dimensions video illisibles")
    ratio = info.width / info.height
    if not (0.4 <= ratio <= 2.5):
        return Eligibility(False, f"Ratio {ratio:.2f} hors des bornes acceptees par Kling")
    return Eligibility(True)
