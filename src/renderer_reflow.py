"""
renderer_reflow.py — A4 Reflow Renderer for E2A PDF Translator.

Instead of placing translated text at the original PDF coordinates
(which causes the Y-flip bug because pypdfium2 uses top-left origin
while ReportLab uses bottom-left), this renderer:

1. Takes the TranslatedDocument produced by the existing pipeline
2. Extracts text in reading order (top→bottom per source page)
3. Reflows Arabic text onto fresh A4 pages using Platypus
4. Uses configurable font size (default 14pt) and proper margins

Benefits over the position-preserving renderer:
- No coordinate system bugs (Platypus handles all layout)
- Clean, readable output with consistent formatting
- Automatic word wrapping and pagination
- Proper paragraph spacing and heading detection
- Images inserted inline at appropriate scale

Integrates as a drop-in replacement: pipeline.py calls
ReflowRenderer.render(translated_doc, output_path) instead of
PDFRenderer.render(translated_doc, output_path).
"""

from __future__ import annotations

import io
import logging
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image as RLImage,
    HRFlowable,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .arabic_utils import prepare_arabic, has_arabic
from .models import (
    ImageBlock,
    TranslatedBlock,
    TranslatedDocument,
    TranslatedPage,
)

logger = logging.getLogger(__name__)


class ReflowRenderError(Exception):
    """Raised when reflow rendering fails."""
    pass


# ─────────────────────────────────────────────────────────────
#  Font Registration
# ─────────────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    # (regular_path, bold_path, family_name)
    ("/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
     "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
     "FreeSerif", "FreeSerifBold"),
    ("/usr/share/fonts/truetype/amiri/Amiri-Regular.ttf",
     "/usr/share/fonts/truetype/amiri/Amiri-Bold.ttf",
     "Amiri", "AmiriBold"),
    ("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
     "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
     "NotoNaskh", "NotoNaskhBold"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "DejaVuSans", "DejaVuSansBold"),
]

_font_regular: str | None = None
_font_bold: str | None = None


def _register_fonts(custom_path: str | None = None) -> tuple[str, str]:
    """Register an Arabic-capable font. Returns (regular_name, bold_name)."""
    global _font_regular, _font_bold
    if _font_regular is not None:
        return _font_regular, _font_bold

    # Custom font
    if custom_path and os.path.isfile(custom_path):
        try:
            pdfmetrics.registerFont(TTFont("CustomArabic", custom_path))
            _font_regular = "CustomArabic"
            _font_bold = "CustomArabic"
            logger.info(f"Registered custom font: {custom_path}")
            return _font_regular, _font_bold
        except Exception as e:
            logger.warning(f"Custom font failed: {e}")

    # Auto-detect
    for reg_path, bold_path, reg_name, bold_name in _FONT_CANDIDATES:
        if os.path.isfile(reg_path):
            try:
                pdfmetrics.registerFont(TTFont(reg_name, reg_path))
                _font_regular = reg_name
                if os.path.isfile(bold_path):
                    pdfmetrics.registerFont(TTFont(bold_name, bold_path))
                    _font_bold = bold_name
                else:
                    _font_bold = reg_name
                logger.info(f"Registered font: {reg_name}")
                return _font_regular, _font_bold
            except Exception as e:
                logger.warning(f"Font {reg_name} failed: {e}")

    # Fallback (Arabic won't render but won't crash)
    _font_regular = "Helvetica"
    _font_bold = "Helvetica-Bold"
    logger.warning("No Arabic font found — falling back to Helvetica")
    return _font_regular, _font_bold


# ─────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────

@dataclass
class ReflowConfig:
    """Configuration for the reflow renderer."""
    # Typography
    font_size: float = 14.0             # Body text size in points
    heading_scale: float = 1.35         # Heading = font_size × this
    title_scale: float = 1.65           # Title = font_size × this
    line_spacing: float = 1.6           # Leading = font_size × this
    paragraph_spacing: float = 8.0      # Extra space between paragraphs (pt)

    # Page layout
    page_size: tuple[float, float] = A4  # (width, height) in points
    margin_left: float = 2.0 * cm
    margin_right: float = 2.0 * cm
    margin_top: float = 2.5 * cm
    margin_bottom: float = 2.0 * cm

    # Features
    show_page_numbers: bool = True
    show_source_markers: bool = True     # "─── Source Page N ───" dividers
    header_text: str = ""                # Optional running header

    # Font
    custom_font_path: str | None = None

    # Heading detection thresholds (relative to median font size)
    heading_font_threshold: float = 1.15  # Block font > median × this → heading
    title_font_threshold: float = 1.5     # Block font > median × this → title


# ─────────────────────────────────────────────────────────────
#  Reflow Renderer
# ─────────────────────────────────────────────────────────────

class ReflowRenderer:
    """
    Renders a TranslatedDocument onto clean A4 pages using Platypus.

    This is a drop-in alternative to PDFRenderer. The pipeline calls:
        renderer.render(translated_doc, output_path)
    with the same signature.
    """

    def __init__(self, config: ReflowConfig | None = None):
        self.config = config or ReflowConfig()
        self._font_reg, self._font_bold = _register_fonts(
            self.config.custom_font_path
        )
        self._styles = self._build_styles()
        self._median_font_size: float | None = None

    def _build_styles(self) -> dict[str, ParagraphStyle]:
        """Create paragraph styles for the various content types."""
        cfg = self.config
        styles = {}

        # ── Arabic body (right-aligned, RTL) ──
        styles["ar_body"] = ParagraphStyle(
            "ArabicBody",
            fontName=self._font_reg,
            fontSize=cfg.font_size,
            leading=cfg.font_size * cfg.line_spacing,
            alignment=TA_RIGHT,
            spaceAfter=cfg.paragraph_spacing,
            wordWrap="RTL",
        )

        # ── Arabic heading ──
        styles["ar_heading"] = ParagraphStyle(
            "ArabicHeading",
            parent=styles["ar_body"],
            fontName=self._font_bold,
            fontSize=cfg.font_size * cfg.heading_scale,
            leading=cfg.font_size * cfg.heading_scale * 1.35,
            spaceBefore=cfg.paragraph_spacing * 2.5,
            spaceAfter=cfg.paragraph_spacing * 1.2,
            alignment=TA_RIGHT,
        )

        # ── Arabic title ──
        styles["ar_title"] = ParagraphStyle(
            "ArabicTitle",
            parent=styles["ar_body"],
            fontName=self._font_bold,
            fontSize=cfg.font_size * cfg.title_scale,
            leading=cfg.font_size * cfg.title_scale * 1.3,
            spaceBefore=cfg.paragraph_spacing * 3,
            spaceAfter=cfg.paragraph_spacing * 2,
            alignment=TA_CENTER,
        )

        # ── English / Latin fallback (left-aligned) ──
        styles["en_body"] = ParagraphStyle(
            "EnglishBody",
            fontName=self._font_reg,
            fontSize=cfg.font_size,
            leading=cfg.font_size * cfg.line_spacing,
            alignment=TA_LEFT,
            spaceAfter=cfg.paragraph_spacing,
        )

        # ── Source-page divider ──
        styles["divider"] = ParagraphStyle(
            "PageDivider",
            fontName=self._font_reg,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor=colors.Color(0.55, 0.55, 0.55),
            spaceBefore=10,
            spaceAfter=6,
        )

        # ── Image caption ──
        styles["caption"] = ParagraphStyle(
            "Caption",
            parent=styles["ar_body"],
            fontSize=cfg.font_size * 0.82,
            leading=cfg.font_size * 0.82 * 1.4,
            alignment=TA_CENTER,
            textColor=colors.Color(0.35, 0.35, 0.35),
            spaceAfter=cfg.paragraph_spacing * 1.5,
        )

        return styles

    # ──────────────────────────────────────────────────────────
    #  Public API — same signature as PDFRenderer.render()
    # ──────────────────────────────────────────────────────────

    def render(
        self,
        doc: TranslatedDocument,
        output_path: str | Path,
    ) -> Path:
        """
        Render the translated document to a reflowed A4 PDF.

        Args:
            doc: TranslatedDocument from the translation step.
            output_path: Where to write the PDF.

        Returns:
            Path to the generated PDF.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Compute median font size across the whole document for
        # heading/title detection heuristics.
        self._median_font_size = self._compute_median_font(doc)

        # Build the Platypus story (list of flowables)
        story = self._build_story(doc)

        # Create the PDF document
        pdf_doc = SimpleDocTemplate(
            str(output_path),
            pagesize=self.config.page_size,
            leftMargin=self.config.margin_left,
            rightMargin=self.config.margin_right,
            topMargin=self.config.margin_top,
            bottomMargin=self.config.margin_bottom,
            title="E2A Translated Document",
            author="E2A PDF Translator — Reflow Renderer",
        )

        try:
            pdf_doc.build(
                story,
                onFirstPage=self._on_page,
                onLaterPages=self._on_page,
            )
        except Exception as e:
            raise ReflowRenderError(f"PDF build failed: {e}") from e

        logger.info(
            f"Reflow render complete: {len(doc.pages)} source pages → {output_path}"
        )
        return output_path

    # ──────────────────────────────────────────────────────────
    #  Story construction
    # ──────────────────────────────────────────────────────────

    def _build_story(self, doc: TranslatedDocument) -> list:
        """Convert the full TranslatedDocument into a Platypus story."""
        story: list = []

        for page_idx, page in enumerate(doc.pages):
            # Source-page divider (skip for first page)
            if self.config.show_source_markers and page_idx > 0:
                story.append(Spacer(1, 4))
                story.append(HRFlowable(
                    width="50%", thickness=0.5,
                    color=colors.Color(0.75, 0.75, 0.75),
                    spaceBefore=6, spaceAfter=2,
                ))
                marker = f"─── صفحة المصدر {page.page_number} ───"
                story.append(Paragraph(
                    self._esc(marker), self._styles["divider"]
                ))

            # Text blocks — already in reading order from the extractor
            # (sorted top-to-bottom, which is how they were extracted)
            sorted_blocks = self._sort_blocks_reading_order(page)

            for tblock in sorted_blocks:
                para = self._block_to_paragraph(tblock)
                if para is not None:
                    story.append(para)

            # Images — after text for this source page
            for img_block in page.image_blocks:
                flowable = self._image_to_flowable(img_block)
                if flowable is not None:
                    story.append(Spacer(1, 6))
                    story.append(flowable)
                    story.append(Spacer(1, 6))

        # Empty document guard
        if not story:
            story.append(Paragraph(
                self._esc("(لا يوجد محتوى للترجمة)"),
                self._styles["ar_body"],
            ))

        return story

    def _sort_blocks_reading_order(
        self, page: TranslatedPage
    ) -> list[TranslatedBlock]:
        """
        Sort translated blocks in top-to-bottom reading order.
        The extractor already sorts by -y1, but after translation the
        order might have been altered. Re-sort by descending y1 (top first).
        """
        return sorted(
            page.translated_blocks,
            key=lambda tb: -tb.original.bbox.y1,
        )

    def _block_to_paragraph(
        self, tblock: TranslatedBlock
    ) -> Paragraph | None:
        """Convert a TranslatedBlock into a Platypus Paragraph."""
        text = tblock.translated_text.strip()
        text = text.replace("\x00", "")  # strip null bytes
        if not text:
            return None

        # Detect content type
        is_ar = has_arabic(text)
        block_role = self._classify_block(tblock)

        if is_ar:
            display_text = prepare_arabic(text)
            style_key = {
                "title": "ar_title",
                "heading": "ar_heading",
                "body": "ar_body",
            }[block_role]
        else:
            display_text = text
            style_key = "en_body"

        escaped = self._esc(display_text)

        try:
            return Paragraph(escaped, self._styles[style_key])
        except Exception as e:
            logger.warning(f"Paragraph creation failed: {e}")
            # Strip control characters and retry
            safe = "".join(
                ch for ch in escaped
                if unicodedata.category(ch)[0] != "C" or ch in "\n\r\t"
            )
            try:
                return Paragraph(safe, self._styles[style_key])
            except Exception:
                return None

    def _classify_block(self, tblock: TranslatedBlock) -> str:
        """
        Classify a block as 'title', 'heading', or 'body' based on
        the original font size relative to the document median.

        This is a heuristic — it uses the source PDF's font metrics
        (which the extractor preserved) to decide whether the block
        was a heading/title in the original document.
        """
        cfg = self.config
        median = self._median_font_size or 12.0
        block_size = tblock.font.size

        # Short + large font → title candidate
        text = tblock.translated_text.strip()
        is_short = len(text) < 120

        if block_size > median * cfg.title_font_threshold and is_short:
            return "title"
        if block_size > median * cfg.heading_font_threshold and is_short:
            return "heading"
        if tblock.font.is_bold and is_short and len(text) < 200:
            return "heading"
        return "body"

    def _compute_median_font(self, doc: TranslatedDocument) -> float:
        """Compute the median font size across all translated blocks."""
        sizes: list[float] = []
        for page in doc.pages:
            for tb in page.translated_blocks:
                sizes.append(tb.font.size)
        if not sizes:
            return 12.0
        sizes.sort()
        mid = len(sizes) // 2
        return sizes[mid]

    def _image_to_flowable(self, img_block: ImageBlock) -> RLImage | None:
        """Convert an ImageBlock to a Platypus Image flowable."""
        if not img_block.image_bytes:
            return None

        try:
            img_io = io.BytesIO(img_block.image_bytes)
            # Determine natural dimensions
            try:
                from PIL import Image as PILImage
                with PILImage.open(io.BytesIO(img_block.image_bytes)) as pil:
                    nat_w, nat_h = pil.size
            except Exception:
                nat_w = img_block.bbox.width or 200
                nat_h = img_block.bbox.height or 200

            if nat_w <= 0 or nat_h <= 0:
                return None

            # Scale to fit 80% of usable width, max 55% of page height
            usable_w = (
                self.config.page_size[0]
                - self.config.margin_left
                - self.config.margin_right
            ) * 0.8
            usable_h = (
                self.config.page_size[1]
                - self.config.margin_top
                - self.config.margin_bottom
            ) * 0.55

            aspect = nat_h / nat_w
            disp_w = min(nat_w, usable_w)
            disp_h = disp_w * aspect
            if disp_h > usable_h:
                disp_h = usable_h
                disp_w = disp_h / aspect

            return RLImage(img_io, width=disp_w, height=disp_h)
        except Exception as e:
            logger.warning(f"Image render failed: {e}")
            return None

    # ──────────────────────────────────────────────────────────
    #  Page decoration (header / footer / page number)
    # ──────────────────────────────────────────────────────────

    def _on_page(self, canvas_obj, doc):
        """Called by Platypus for each page — draws header and footer."""
        canvas_obj.saveState()
        w, h = self.config.page_size

        # ── Page number (footer center) ──
        if self.config.show_page_numbers:
            canvas_obj.setFont(self._font_reg, 9)
            canvas_obj.setFillColor(colors.Color(0.45, 0.45, 0.45))
            page_num_text = f"— {doc.page} —"
            canvas_obj.drawCentredString(
                w / 2, self.config.margin_bottom * 0.45, page_num_text
            )

        # ── Running header ──
        if self.config.header_text:
            canvas_obj.setFont(self._font_reg, 8)
            canvas_obj.setFillColor(colors.Color(0.5, 0.5, 0.5))
            hdr = self.config.header_text
            if has_arabic(hdr):
                hdr = prepare_arabic(hdr)
            canvas_obj.drawCentredString(
                w / 2, h - self.config.margin_top * 0.55, hdr
            )
            # Subtle separator line
            canvas_obj.setStrokeColor(colors.Color(0.85, 0.85, 0.85))
            canvas_obj.setLineWidth(0.4)
            y_line = h - self.config.margin_top * 0.7
            canvas_obj.line(
                self.config.margin_left, y_line,
                w - self.config.margin_right, y_line,
            )

        canvas_obj.restoreState()

    # ──────────────────────────────────────────────────────────
    #  Utilities
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _esc(text: str) -> str:
        """Escape text for ReportLab's XML-based Paragraph parser."""
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        text = text.replace('"', "&quot;")
        return text
