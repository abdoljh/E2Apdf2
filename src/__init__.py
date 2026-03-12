"""
E2A PDF Translator — English to Arabic PDF translation.

Usage:
    from e2apdf import translate_pdf
    report = translate_pdf("input.pdf", backend="mock")
    print(report.summary())

For more control:
    from e2apdf import E2APipeline, PipelineConfig
    config = PipelineConfig(translation_backend="google", api_key="...")
    pipeline = E2APipeline(config)
    report = pipeline.translate("input.pdf", "output_ar.pdf")
"""

from .pipeline import (
    E2APipeline,
    PipelineConfig,
    PipelineReport,
    translate_pdf,
)
from .extractor import PDFExtractor, ExtractionError
from .translator import Translator, TranslatorConfig, TranslationError
from .renderer import PDFRenderer, RendererConfig, RenderError, FontConfig
from .arabic_utils import prepare_arabic, has_arabic, reshape_arabic, bidi_reorder
from .models import (
    BBox,
    BlockType,
    DocumentContent,
    FontInfo,
    ImageBlock,
    PageContent,
    TextBlock,
    TextSpan,
    TranslatedBlock,
    TranslatedDocument,
    TranslatedPage,
)

__version__ = "0.1.0"
__all__ = [
    # Pipeline
    "E2APipeline",
    "PipelineConfig",
    "PipelineReport",
    "translate_pdf",
    # Components
    "PDFExtractor",
    "Translator",
    "TranslatorConfig",
    "PDFRenderer",
    "RendererConfig",
    "FontConfig",
    # Arabic utilities
    "prepare_arabic",
    "has_arabic",
    "reshape_arabic",
    "bidi_reorder",
    # Models
    "BBox",
    "BlockType",
    "DocumentContent",
    "FontInfo",
    "ImageBlock",
    "PageContent",
    "TextBlock",
    "TextSpan",
    "TranslatedBlock",
    "TranslatedDocument",
    "TranslatedPage",
    # Errors
    "ExtractionError",
    "TranslationError",
    "RenderError",
]
