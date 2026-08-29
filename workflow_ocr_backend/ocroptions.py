"""
Typed, closed vocabulary of OCR options exposed over the REST API.

Design rules enforced here (see also test/test_ocroptions.py, which enforces them
mechanically so they cannot rot):

1. The API contract is THIS schema, not ``ocrmypdf.ocr()``'s signature.
   Nothing is derived by reflection from the dependency.
2. ``extra="forbid"`` - unknown fields are a 422, never a silent passthrough.
3. No field is path-typed, plugin-typed, or free-form argv. Structurally
   impossible to express "load this file" or "append this to a subprocess
   command line" in this schema.
4. Every scalar is bounded. Caller intent and operator policy are separate:
   resource limits (jobs, timeouts, image size caps) are NOT caller options.
5. Mapping to ocrmypdf kwargs is written out by hand, field by field. There is
   no ``**caller_data`` splat anywhere in this module.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class InvalidOcrOptionsError(ValueError):
    """Raised for a caller-supplied option that is well-typed but invalid at
    runtime (e.g. a language not installed on this backend) - a 400, not a 422."""


# --------------------------------------------------------------------------
# Server-side policy. Operator-controlled, never caller-controlled.
# --------------------------------------------------------------------------


class OcrPolicy(BaseModel):
    """Limits owned by whoever runs the backend, injected from config/env."""

    model_config = ConfigDict(frozen=True)

    jobs: int = Field(default=1, ge=1, le=16)
    max_image_mpixels: float = Field(default=250.0, gt=0, le=1000.0)
    # Hard ceiling. A caller-supplied tesseract_timeout is clamped to this.
    max_tesseract_timeout_s: float = Field(default=180.0, gt=0, le=3600.0)
    installed_languages: frozenset[str] = frozenset({"eng"})


# --------------------------------------------------------------------------
# Enumerations. Replace stringly-typed ocrmypdf options with closed sets.
# --------------------------------------------------------------------------


class TextMode(str, Enum):
    """Replaces the mutually exclusive skip_text/force_ocr/redo_ocr booleans.

    The legacy API let a caller set two of them at once and get a 500 out of
    ocrmypdf's own validation. As an enum the invalid state is unrepresentable.
    """

    SKIP = "skip-text"
    FORCE = "force-ocr"
    REDO = "redo-ocr"


class OutputType(str, Enum):
    PDFA = "pdfa"
    PDF = "pdf"
    PDFA_1 = "pdfa-1"
    PDFA_2 = "pdfa-2"
    PDFA_3 = "pdfa-3"


class PdfRenderer(str, Enum):
    AUTO = "auto"
    HOCR = "hocr"
    SANDWICH = "sandwich"


class Thresholding(str, Enum):
    OTSU = "otsu"
    ADAPTIVE_OTSU = "adaptive-otsu"
    SAUVOLA = "sauvola"


# --------------------------------------------------------------------------
# Constrained scalars. The constraint travels with the type.
# --------------------------------------------------------------------------

# Tesseract language / script code, e.g. "eng", "chi_sim", "script/Latin".
LanguageCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$|^script/[A-Za-z_]{1,31}$"),
]

# Page selection, e.g. "1-4,7,9-". Deliberately narrow: digits, comma, hyphen.
PageRange = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]+(-[0-9]*)?(,[0-9]+(-[0-9]*)?)*$", max_length=128),
]

MetadataText = Annotated[str, StringConstraints(max_length=512)]


# --------------------------------------------------------------------------
# The request schema.
# --------------------------------------------------------------------------


class OcrOptions(BaseModel):
    """Everything a caller is allowed to say about how to OCR a document."""

    model_config = ConfigDict(
        extra="forbid",  # unknown key -> 422, never forwarded
        frozen=True,
        use_enum_values=False,
        str_strip_whitespace=True,
    )

    # --- recognition -------------------------------------------------------
    languages: list[LanguageCode] = Field(default_factory=lambda: ["eng"], min_length=1, max_length=8)
    # None is a fourth, deliberate state distinct from the three enum values: it
    # means "don't override OCRmyPDF's own disposition", which is to raise if the
    # document already has text. Defaulting to None rather than TextMode.SKIP
    # keeps that conservative default - a caller has to opt into skipping,
    # forcing, or redoing OCR on already-processed pages.
    mode: TextMode | None = None
    pages: PageRange | None = None

    # --- preprocessing -----------------------------------------------------
    rotate_pages: bool = False
    rotate_pages_threshold: float | None = Field(default=None, ge=0.0, le=30.0)
    deskew: bool = False
    clean: bool = False
    clean_final: bool = False
    remove_background: bool = False
    remove_vectors: bool = False
    oversample_dpi: int | None = Field(default=None, ge=0, le=1200)
    image_dpi: int | None = Field(default=None, ge=1, le=5000)

    # --- output ------------------------------------------------------------
    output_type: OutputType = OutputType.PDFA
    optimize: int = Field(default=1, ge=0, le=3)
    jpeg_quality: int | None = Field(default=None, ge=0, le=100)
    png_quality: int | None = Field(default=None, ge=0, le=100)
    pdf_renderer: PdfRenderer = PdfRenderer.AUTO

    # --- tesseract tuning --------------------------------------------------
    tesseract_pagesegmode: int | None = Field(default=None, ge=0, le=13)
    tesseract_oem: int | None = Field(default=None, ge=0, le=3)
    tesseract_thresholding: Thresholding | None = None
    # Requested, not guaranteed: clamped to policy ceiling at map time.
    tesseract_timeout_s: float | None = Field(default=None, gt=0, le=3600.0)

    # --- document metadata -------------------------------------------------
    title: MetadataText | None = None
    author: MetadataText | None = None
    subject: MetadataText | None = None
    keywords: MetadataText | None = None

    @model_validator(mode="after")
    def _check_coherent(self) -> OcrOptions:
        if self.clean_final and not self.clean:
            raise ValueError("clean_final requires clean")
        if self.rotate_pages_threshold is not None and not self.rotate_pages:
            raise ValueError("rotate_pages_threshold requires rotate_pages")
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("languages must not contain duplicates")
        return self

    def validate_against_policy(self, policy: OcrPolicy) -> None:
        """Checks that depend on runtime state, so they can return 400 not 500."""
        unknown = [lang for lang in self.languages if lang not in policy.installed_languages]
        if unknown:
            raise InvalidOcrOptionsError(
                f"Language(s) not installed on this backend: {', '.join(sorted(unknown))}"
            )

    # ----------------------------------------------------------------------
    # Explicit mapping. Hand-written on purpose: adding an option to the API
    # is a deliberate edit here, not a side effect of upgrading a dependency.
    # ----------------------------------------------------------------------

    def to_ocrmypdf_kwargs(self, policy: OcrPolicy) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            # Operator policy - never caller-controlled.
            "jobs": policy.jobs,
            "max_image_mpixels": policy.max_image_mpixels,
            "progress_bar": False,
            # Caller intent.
            "language": list(self.languages),
            "output_type": self.output_type.value,
            "optimize": self.optimize,
            "pdf_renderer": self.pdf_renderer.value,
            "rotate_pages": self.rotate_pages,
            "deskew": self.deskew,
            "clean": self.clean,
            "clean_final": self.clean_final,
            "remove_background": self.remove_background,
            "remove_vectors": self.remove_vectors,
        }

        # Mode: at most one enum in, at most one legacy boolean out. None means
        # "no override" - all three stay unset, and ocrmypdf applies its own
        # (conservative) default disposition.
        if self.mode is not None:
            kwargs[
                {
                    TextMode.SKIP: "skip_text",
                    TextMode.FORCE: "force_ocr",
                    TextMode.REDO: "redo_ocr",
                }[self.mode]
            ] = True

        optional: list[tuple[str, Any]] = [
            ("pages", self.pages),
            ("rotate_pages_threshold", self.rotate_pages_threshold),
            ("oversample", self.oversample_dpi),
            ("image_dpi", self.image_dpi),
            # ocrmypdf.ocr()'s keyword is "jpg_quality" - "--jpeg-quality" is only the
            # documented *CLI* spelling; the Python signature never exposed it. Keep the
            # public field named after the CLI flag callers actually know, and translate
            # here so the public contract does not depend on which alias ocrmypdf's
            # argument parser happens to forward to the API.
            ("jpg_quality", self.jpeg_quality),
            ("png_quality", self.png_quality),
            ("tesseract_pagesegmode", self.tesseract_pagesegmode),
            ("tesseract_oem", self.tesseract_oem),
            ("title", self.title),
            ("author", self.author),
            ("subject", self.subject),
            ("keywords", self.keywords),
        ]
        for key, value in optional:
            if value is not None:
                kwargs[key] = value

        if self.tesseract_thresholding is not None:
            # ocrmypdf takes an IntEnum here. The API keeps a readable string so
            # the public contract survives upstream changing its encoding - which
            # is the whole point of not exposing the library's own types.
            kwargs["tesseract_thresholding"] = {
                Thresholding.OTSU: 0,
                Thresholding.ADAPTIVE_OTSU: 1,
                Thresholding.SAUVOLA: 2,
            }[self.tesseract_thresholding]

        # Clamp rather than reject: the caller asked for a timeout, the operator
        # decides the maximum. A caller can never extend it.
        kwargs["tesseract_timeout"] = min(
            self.tesseract_timeout_s or policy.max_tesseract_timeout_s,
            policy.max_tesseract_timeout_s,
        )

        return kwargs


# --------------------------------------------------------------------------
# The literal, frozen boundary. Asserted by tests.
# --------------------------------------------------------------------------

#: Every ocrmypdf kwarg this service is capable of producing. Written out as a
#: literal so it appears in code review whenever it changes.
EMITTABLE_OCRMYPDF_KWARGS: Final[frozenset[str]] = frozenset(
    {
        "author",
        "clean",
        "clean_final",
        "deskew",
        "force_ocr",
        "image_dpi",
        "jobs",
        "jpg_quality",
        "keywords",
        "language",
        "max_image_mpixels",
        "optimize",
        "output_type",
        "oversample",
        "pages",
        "pdf_renderer",
        "png_quality",
        "progress_bar",
        "redo_ocr",
        "remove_background",
        "remove_vectors",
        "rotate_pages",
        "rotate_pages_threshold",
        "skip_text",
        "subject",
        "tesseract_oem",
        "tesseract_pagesegmode",
        "tesseract_thresholding",
        "tesseract_timeout",
        "title",
    }
)

#: Parameters this service must never emit at all, with the reason. This is a
#: tripwire for review, not the security mechanism - the mechanism is that the
#: schema has no field capable of expressing any of them.
NEVER_EMITTED: Final[dict[str, str]] = {
    "plugins": "loads and executes arbitrary Python -> RCE",
    "plugin_manager": "same as plugins",
    "user_words": "caller-supplied path appended to the tesseract argv",
    "user_patterns": "caller-supplied path appended to the tesseract argv",
    "tesseract_config": "caller-supplied list extended directly into the tesseract argv",
    "unpaper_args": "caller-supplied argv for the unpaper subprocess",
    "keep_temporary_files": "leaves caller data on the backend filesystem",
    "invalidate_digital_signatures": "changes document trust semantics",
    "input_file": "owned by this service",
    "input_file_or_options": "owned by this service",
    "output_file": "owned by this service",
    "output_folder": "owned by this service",
    "sidecar": "owned by this service",
    "no_overwrite": "owned by this service",
}

#: Emitted, but sourced from OcrPolicy only. No request field may influence them.
#: Raising max_image_mpixels re-enables decompression bombs; raising jobs and
#: tesseract_timeout are the cheapest DoS vectors against a backend that can only
#: run one OCRmyPDF task per process.
OPERATOR_OWNED: Final[frozenset[str]] = frozenset(
    {"jobs", "max_image_mpixels", "progress_bar", "tesseract_timeout"}
)
