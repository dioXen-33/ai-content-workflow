"""Enums et schemas partages."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Platform(StrEnum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    PINTEREST = "pinterest"
    # Video fournie directement par l'utilisateur : aucun scraping, le fichier
    # est deja sur disque. Elle rejoint la file au meme titre que les autres.
    UPLOAD = "upload"


class VideoState(StrEnum):
    """Etats d'une video dans le pipeline.

    La progression normale est lineaire. `SKIPPED` couvre les rejets
    automatiques (filtres de recevabilite) et manuels (deselection par
    l'utilisateur) ; `FAILED` couvre les erreurs techniques.
    """

    DISCOVERED = "discovered"          # metadonnees recuperees via Apify
    DOWNLOADED = "downloaded"          # fichier mp4 sur disque
    FRAMED = "framed"                  # premiere frame extraite
    EDIT_BATCHED = "edit_batched"      # inclus dans un batch Gemini en cours
    EDITED = "edited"                  # image Nano Banana Pro obtenue
    MOTION_SUBMITTED = "motion_submitted"  # tache Kling en cours
    DONE = "done"                      # video finale disponible
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_STATES = {VideoState.DONE, VideoState.FAILED, VideoState.SKIPPED}


class JobStatus(StrEnum):
    DRAFT = "draft"                # cree, pas encore scrape
    SCRAPING = "scraping"
    REVIEW = "review"              # scraping fini, attente validation utilisateur
    RUNNING = "running"
    PAUSED = "paused"              # arret sur plafond budget ou demande utilisateur
    COMPLETED = "completed"
    FAILED = "failed"


class FailureKind(StrEnum):
    """Distingue ce qui merite un retry de ce qui n'en merite pas.

    Retrier un blocage de securite a l'identique ne fait que bruler du credit :
    le modele repondra la meme chose. Seul `TRANSIENT` est rejoue.
    """

    TRANSIENT = "transient"        # reseau, 5xx, rate limit -> retry
    SAFETY_BLOCK = "safety_block"  # refus du modele -> abandon
    INVALID_INPUT = "invalid_input"  # 4xx, media non conforme -> abandon
    QUOTA = "quota"                # credits epuises -> pause du job
    UNKNOWN = "unknown"


class PipelineError(Exception):
    def __init__(
        self, kind: FailureKind, message: str, retry_after: float | None = None
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        # Delai conseille avant de reessayer, en secondes. Renseigne quand le
        # fournisseur indique une surcharge : on attend alors bien plus que le
        # backoff generique.
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Schemas d'entree de l'API
# ---------------------------------------------------------------------------


class ScrapeParams(BaseModel):
    """Parametres de scraping, choisis par l'utilisateur dans l'UI."""

    max_videos_per_account: int = Field(30, ge=1, le=500)
    posted_after: str | None = None          # ISO date, ex "2026-01-01"
    min_views: int = Field(0, ge=0)
    min_duration_s: float = Field(3.0, ge=0)
    max_duration_s: float = Field(30.0, ge=1)


class GenerationParams(BaseModel):
    """Parametres des deux etapes IA."""

    prompt: str = Field(..., min_length=1)
    reference_image_id: str

    # Nano Banana Pro
    aspect_ratio: str = "9:16"
    image_size: str = "2K"                   # 1K | 2K | 4K

    # Kling
    #
    # Motion Control n'expose AUCUN parametre de duree : la video generee fait
    # exactement la longueur de la video de reference. Le seul reglage possible
    # est un plafond, applique en tronquant la source avant l'envoi.
    #
    # L'orientation du personnage est toujours `video` (le personnage suit
    # l'orientation de la video de reference), ce qui autorise 30 s de reference.
    max_output_duration_s: int = Field(30, ge=3, le=30)
    kling_mode: str = "pro"                  # std | pro
    keep_audio: bool = True

    # Batch API Gemini : moitie prix, mais traitement asynchrone (cible 24 h).
    gemini_batch: bool = False

    def image_unit_cost(self) -> float:
        table = PRICING["gemini_image_batch"] if self.gemini_batch else PRICING["gemini_image"]
        return table.get(self.image_size, table["2K"])

    def duration_cap(self) -> float:
        """Plafond effectif : reglage utilisateur, borne aux 30 s de l'API."""
        return min(float(self.max_output_duration_s), 30.0)


class CreateJobRequest(BaseModel):
    name: str = Field(..., min_length=1)
    # Vide pour un job d'upload direct : les videos sont fournies par
    # l'utilisateur, sans passer par le scraping.
    accounts: list[str] = Field(default_factory=list)
    scrape: ScrapeParams = ScrapeParams()


class StartRunRequest(BaseModel):
    generation: GenerationParams


class Preferences(BaseModel):
    """Parametres par defaut, reutilises a chaque nouveau job.

    Evite de re-saisir le prompt et l'image de reference a chaque campagne.
    """

    # Generation
    prompt: str = ""
    reference_image_id: str = ""
    aspect_ratio: str = "9:16"
    image_size: str = "2K"
    max_output_duration_s: int = Field(30, ge=3, le=30)
    kling_mode: str = "pro"
    keep_audio: bool = True
    gemini_batch: bool = False

    # Scraping
    max_videos_per_account: int = Field(30, ge=1, le=500)
    min_views: int = Field(0, ge=0)
    min_duration_s: float = Field(3.0, ge=0)
    max_duration_s: float = Field(30.0, ge=1)

    # Budget applique a chaque nouveau job
    max_spend_usd: float = Field(50.0, gt=0)

    def to_scrape_params(self) -> ScrapeParams:
        return ScrapeParams(
            max_videos_per_account=self.max_videos_per_account,
            min_views=self.min_views,
            min_duration_s=self.min_duration_s,
            max_duration_s=self.max_duration_s,
        )


# ---------------------------------------------------------------------------
# Tarifs, pour l'estimation et le suivi de budget.
# Sources : tarif officiel Gemini API et tarif Kling / revendeurs, aout 2026.
# ---------------------------------------------------------------------------

# Tarif par modele d'image, en USD. Le Batch API applique -50 %.
GEMINI_IMAGE_PRICING = {
    "gemini-3-pro-image": {"1K": 0.134, "2K": 0.134, "4K": 0.24},
    "gemini-3-pro-image-preview": {"1K": 0.134, "2K": 0.134, "4K": 0.24},
    # Nano Banana 2 : deux fois moins cher, et nettement moins sature.
    "gemini-3.1-flash-image": {"1K": 0.067, "2K": 0.067, "4K": 0.151},
    "gemini-3.1-flash-image-preview": {"1K": 0.067, "2K": 0.067, "4K": 0.151},
    "gemini-2.5-flash-image": {"1K": 0.039, "2K": 0.039, "4K": 0.039},
}

_DEFAULT_IMAGE_PRICE = {"1K": 0.134, "2K": 0.134, "4K": 0.24}


def image_cost(model: str, image_size: str = "2K", batch: bool = False) -> float:
    """Cout d'une image pour un modele donne."""
    table = GEMINI_IMAGE_PRICING.get(model.split("/")[-1], _DEFAULT_IMAGE_PRICE)
    price = table.get(image_size, table.get("2K", 0.134))
    return price * (0.5 if batch else 1.0)


PRICING = {
    "gemini_image": {"1K": 0.134, "2K": 0.134, "4K": 0.24},
    # Batch API : 50 % du tarif interactif.
    "gemini_image_batch": {"1K": 0.067, "2K": 0.067, "4K": 0.12},
    # USD par seconde de video generee. `std` = 720p, `pro` = 1080p.
    "kling_motion_control": {"std": 0.07, "pro": 0.1134},
    # USD par resultat de scraping. Pinterest passe par yt-dlp et une video
    # importee ne coute rien a decouvrir : ni l'un ni l'autre n'est facture.
    "apify": {
        "instagram": 0.0005, "tiktok": 0.0017, "pinterest": 0.0, "upload": 0.0,
    },
}


def estimate_video_cost(
    gen: GenerationParams, duration_s: float, platform: str = "tiktok"
) -> float:
    """Cout theorique d'une video, sans retry.

    `duration_s` est la duree reelle de sortie, c'est-a-dire celle de la video
    source apres application du plafond -- Kling facture a la seconde produite.
    """
    kling = PRICING["kling_motion_control"].get(gen.kling_mode, 0.1134)
    scrape = PRICING["apify"].get(platform, 0.0017)
    return gen.image_unit_cost() + kling * min(duration_s, gen.duration_cap()) + scrape
