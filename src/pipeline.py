"""
E2A PDF Translation Pipeline.

Orchestrates the full workflow:
  Input PDF → Extract → Translate → Render → Output PDF

Supports two rendering modes:
  - "positioned" (original): preserves source layout coordinates
  - "reflow" (new): reflows text onto clean A4 pages (recommended)

Provides both programmatic API and CLI-friendly interface.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .extractor import PDFExtractor, ExtractionError
from .translator import Translator, TranslatorConfig, TranslationError
from .renderer import PDFRenderer, RendererConfig, RenderError, FontConfig
from .renderer_reflow import ReflowRenderer, ReflowConfig, ReflowRenderError
from .models import DocumentContent, TranslatedDocument

logger = logging.getLogger(__name__)


# ===========================================================================
# Pipeline Configuration
# ===========================================================================

@dataclass
class PipelineConfig:
    """Full pipeline configuration."""
    # Translation settings
    translation_backend: str = "mock"
    api_key: Optional[str] = None
    model: Optional[str] = None
    source_lang: str = "en"
    target_lang: str = "ar"
    cache_path: Optional[str] = ".e2a_cache/translations.json"

    # Extraction settings
    merge_paragraphs: bool = True
    min_text_length: int = 2

    # Rendering mode: "reflow" (new, recommended) or "positioned" (original)
    render_mode: str = "reflow"

    # ── Reflow renderer settings ──
    reflow_font_size: float = 14.0       # Body text size in points
    reflow_heading_scale: float = 1.35
    reflow_title_scale: float = 1.65
    reflow_line_spacing: float = 1.6
    reflow_paragraph_spacing: float = 8.0
    reflow_show_source_markers: bool = True
    reflow_header_text: str = ""

    # ── Positioned renderer settings (original) ──
    font_path: Optional[str] = None
    mirror_layout: bool = True
    preserve_positions: bool = True
    add_page_numbers: bool = True
    line_spacing: float = 1.4
    margin: float = 50.0

    # Pipeline behavior
    continue_on_error: bool = True
    verbose: bool = False


# ===========================================================================
# Pipeline Report
# ===========================================================================

@dataclass
class PipelineReport:
    """Report of a translation pipeline run."""
    input_path: str = ""
    output_path: str = ""
    success: bool = False
    total_pages: int = 0
    translated_pages: int = 0
    total_blocks: int = 0
    translated_blocks: int = 0
    cached_blocks: int = 0
    skipped_blocks: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    render_mode: str = ""

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "=" * 60,
            "E2A PDF Translation Report",
            "=" * 60,
            f"Input:            {self.input_path}",
            f"Output:           {self.output_path}",
            f"Render mode:      {self.render_mode}",
            f"Status:           {'SUCCESS' if self.success else 'FAILED'}",
            f"Duration:         {self.duration_seconds:.1f}s",
            f"Pages:            {self.translated_pages}/{self.total_pages}",
            f"Text blocks:      {self.translated_blocks} translated, "
            f"{self.cached_blocks} from cache, "
            f"{self.skipped_blocks} skipped",
        ]
        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            for w in self.warnings[:10]:
                lines.append(f"  - {w}")
            if len(self.warnings) > 10:
                lines.append(f"  ... and {len(self.warnings) - 10} more")
        if self.errors:
            lines.append(f"Errors ({len(self.errors)}):")
            for e in self.errors[:10]:
                lines.append(f"  - {e}")
        lines.append("=" * 60)
        return "\n".join(lines)


# ===========================================================================
# Main Pipeline
# ===========================================================================

class E2APipeline:
    """
    Main translation pipeline.

    Usage:
        config = PipelineConfig(render_mode="reflow", reflow_font_size=14)
        pipeline = E2APipeline(config)
        report = pipeline.translate("input.pdf", "output_ar.pdf")
        print(report.summary())
    """

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self._setup_logging()

    def _setup_logging(self):
        level = logging.DEBUG if self.config.verbose else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def translate(
        self,
        input_path: str | Path,
        output_path: Optional[str | Path] = None,
    ) -> PipelineReport:
        """
        Run the full translation pipeline.

        Args:
            input_path: Path to the English PDF.
            output_path: Path for the Arabic PDF output.
                         Defaults to input_name_ar.pdf.

        Returns:
            PipelineReport with statistics and any warnings/errors.
        """
        start_time = time.time()
        input_path = Path(input_path)

        if output_path is None:
            output_path = input_path.with_name(
                f"{input_path.stem}_ar{input_path.suffix}"
            )
        else:
            output_path = Path(output_path)

        report = PipelineReport(
            input_path=str(input_path),
            output_path=str(output_path),
            render_mode=self.config.render_mode,
        )

        # ---- Step 1: Extract ----
        logger.info(f"Step 1/3: Extracting content from {input_path.name}")
        try:
            extractor = PDFExtractor(
                merge_paragraphs=self.config.merge_paragraphs,
                min_text_length=self.config.min_text_length,
            )
            doc = extractor.extract(input_path)
            report.total_pages = len(doc.pages)
            report.warnings.extend(doc.extraction_warnings)

            if doc.is_scanned:
                report.warnings.append(
                    "Document appears to be scanned. "
                    "Text extraction may be incomplete."
                )

            total_blocks = sum(len(p.text_blocks) for p in doc.pages)
            logger.info(
                f"  Extracted {len(doc.pages)} pages, "
                f"{total_blocks} text blocks, "
                f"{sum(len(p.image_blocks) for p in doc.pages)} images"
            )

        except ExtractionError as e:
            report.errors.append(f"Extraction failed: {e}")
            report.duration_seconds = time.time() - start_time
            return report

        # ---- Step 2: Translate ----
        logger.info(f"Step 2/3: Translating ({self.config.translation_backend})")
        try:
            translator_config = TranslatorConfig(
                backend=self.config.translation_backend,
                api_key=self.config.api_key,
                model=self.config.model,
                cache_path=self.config.cache_path,
                source_lang=self.config.source_lang,
                target_lang=self.config.target_lang,
            )
            translator = Translator(translator_config)
            translated_doc = translator.translate_document(doc)

            report.warnings.extend(translated_doc.warnings)
            stats = translated_doc.stats
            report.total_blocks = stats.get("total_blocks", 0)
            report.translated_blocks = stats.get("translated", 0)
            report.cached_blocks = stats.get("cached", 0)
            report.skipped_blocks = stats.get("skipped", 0)

            logger.info(
                f"  Translated: {report.translated_blocks}, "
                f"Cached: {report.cached_blocks}, "
                f"Skipped: {report.skipped_blocks}"
            )

        except TranslationError as e:
            report.errors.append(f"Translation failed: {e}")
            report.duration_seconds = time.time() - start_time
            if not self.config.continue_on_error:
                return report
            # Create a minimal translated doc with original text
            translated_doc = TranslatedDocument(source_path=str(input_path))

        # ---- Step 3: Render ----
        mode = self.config.render_mode
        logger.info(
            f"Step 3/3: Rendering Arabic PDF ({mode} mode) → {output_path.name}"
        )

        try:
            if mode == "reflow":
                self._render_reflow(translated_doc, output_path)
            else:
                self._render_positioned(translated_doc, output_path)

            report.translated_pages = len(translated_doc.pages)
            report.success = True
            logger.info(f"  Output: {output_path}")

        except (RenderError, ReflowRenderError) as e:
            report.errors.append(f"Rendering failed: {e}")

        report.duration_seconds = time.time() - start_time
        return report

    def _render_reflow(
        self, doc: TranslatedDocument, output_path: Path
    ):
        """Render using the new A4 reflow renderer."""
        from reportlab.lib.units import cm

        cfg = self.config
        reflow_config = ReflowConfig(
            font_size=cfg.reflow_font_size,
            heading_scale=cfg.reflow_heading_scale,
            title_scale=cfg.reflow_title_scale,
            line_spacing=cfg.reflow_line_spacing,
            paragraph_spacing=cfg.reflow_paragraph_spacing,
            show_source_markers=cfg.reflow_show_source_markers,
            header_text=cfg.reflow_header_text,
            show_page_numbers=cfg.add_page_numbers,
            custom_font_path=cfg.font_path,
            margin_left=cfg.margin if cfg.margin != 50.0 else 2.0 * cm,
            margin_right=cfg.margin if cfg.margin != 50.0 else 2.0 * cm,
            margin_top=cfg.margin if cfg.margin != 50.0 else 2.5 * cm,
            margin_bottom=cfg.margin if cfg.margin != 50.0 else 2.0 * cm,
        )
        renderer = ReflowRenderer(reflow_config)
        renderer.render(doc, output_path)

    def _render_positioned(
        self, doc: TranslatedDocument, output_path: Path
    ):
        """Render using the original position-preserving renderer."""
        cfg = self.config

        font_config = None
        if cfg.font_path:
            font_config = FontConfig(regular=cfg.font_path)
        else:
            font_config = FontConfig().auto_detect()

        renderer_config = RendererConfig(
            font_config=font_config,
            mirror_layout=cfg.mirror_layout,
            preserve_positions=cfg.preserve_positions,
            add_page_numbers=cfg.add_page_numbers,
            line_spacing=cfg.line_spacing,
            margin_top=cfg.margin,
            margin_bottom=cfg.margin,
            margin_left=cfg.margin,
            margin_right=cfg.margin,
        )
        renderer = PDFRenderer(renderer_config)
        renderer.render(doc, output_path)


# ===========================================================================
# Convenience function
# ===========================================================================

def translate_pdf(
    input_path: str,
    output_path: Optional[str] = None,
    backend: str = "mock",
    api_key: Optional[str] = None,
    render_mode: str = "reflow",
    **kwargs,
) -> PipelineReport:
    """
    Convenience function for quick translation.

    Args:
        input_path: Path to English PDF.
        output_path: Path for Arabic PDF (optional).
        backend: Translation backend ("mock", "free", "google", "deepl", etc.).
        api_key: API key for the translation service.
        render_mode: "reflow" (recommended) or "positioned".
        **kwargs: Additional PipelineConfig options.

    Returns:
        PipelineReport with results.

    Example:
        report = translate_pdf("paper.pdf", backend="free", render_mode="reflow")
        print(report.summary())
    """
    config = PipelineConfig(
        translation_backend=backend,
        api_key=api_key,
        render_mode=render_mode,
        **kwargs,
    )
    pipeline = E2APipeline(config)
    return pipeline.translate(input_path, output_path)
