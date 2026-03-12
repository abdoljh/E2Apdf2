"""
RTL PDF renderer for translated Arabic content.

Takes TranslatedDocument and produces a properly formatted Arabic PDF:
- Right-to-left text layout with proper alignment
- Arabic text reshaping and BiDi reordering
- Image reinsertion at mirrored positions
- Font size and style preservation
- Page dimensions matching the original

Uses ReportLab for PDF generation.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import inch, mm
from reportlab.lib.colors import Color, black
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .arabic_utils import prepare_arabic, has_arabic
from .models import (
    BBox,
    ImageBlock,
    TranslatedBlock,
    TranslatedDocument,
    TranslatedPage,
)

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised when PDF rendering fails."""
    pass


# ===========================================================================
# Font Configuration
# ===========================================================================

@dataclass
class FontConfig:
    """
    Font configuration for Arabic PDF rendering.
    Specifies paths to Arabic-capable TrueType fonts.
    """
    regular: str = ""
    bold: str = ""
    italic: str = ""
    bold_italic: str = ""
    family_name: str = "ArabicFont"

    # Common Arabic font search paths
    _FONT_SEARCH_PATHS: list[str] = field(default_factory=lambda: [
        # Linux
        "/usr/share/fonts/truetype/freefont/",
        "/usr/share/fonts/truetype/dejavu/",
        "/usr/share/fonts/truetype/noto/",
        "/usr/share/fonts/opentype/noto/",
        # macOS
        "/Library/Fonts/",
        "/System/Library/Fonts/Supplemental/",
        # Project local
        "fonts/",
        "./fonts/",
    ])

    # Preferred fonts (in order) that support Arabic
    _PREFERRED_FONTS: list[tuple[str, str, str, str]] = field(
        default_factory=lambda: [
            # (regular, bold, italic, bold_italic)
            ("Amiri-Regular.ttf", "Amiri-Bold.ttf", "Amiri-Italic.ttf", "Amiri-BoldItalic.ttf"),
            ("NotoNaskhArabic-Regular.ttf", "NotoNaskhArabic-Bold.ttf", "", ""),
            ("FreeSerif.ttf", "FreeSerifBold.ttf", "FreeSerifItalic.ttf", "FreeSerifBoldItalic.ttf"),
            ("FreeSans.ttf", "FreeSansBold.ttf", "FreeSansOblique.ttf", "FreeSansBoldOblique.ttf"),
        ]
    )

    def auto_detect(self) -> FontConfig:
        """Find and configure the best available Arabic font."""
        for reg, bold, italic, bold_it in self._PREFERRED_FONTS:
            for search_path in self._FONT_SEARCH_PATHS:
                reg_path = os.path.join(search_path, reg)
                if os.path.exists(reg_path):
                    self.regular = reg_path
                    self.bold = os.path.join(search_path, bold) if bold else reg_path
                    self.italic = os.path.join(search_path, italic) if italic else reg_path
                    self.bold_italic = os.path.join(search_path, bold_it) if bold_it else self.bold
                    self.family_name = reg.replace("-Regular.ttf", "").replace(".ttf", "")

                    # Verify bold/italic exist, fallback to regular
                    if not os.path.exists(self.bold):
                        self.bold = self.regular
                    if not os.path.exists(self.italic):
                        self.italic = self.regular
                    if not os.path.exists(self.bold_italic):
                        self.bold_italic = self.bold

                    logger.info(f"Auto-detected font: {self.family_name} at {search_path}")
                    return self

        raise RenderError(
            "No Arabic-capable font found. Install one of: "
            "Amiri, Noto Naskh Arabic, FreeSerif, or FreeSans. "
            "Or specify font paths in FontConfig."
        )


# ===========================================================================
# Renderer
# ===========================================================================

@dataclass
class RendererConfig:
    """Rendering configuration."""
    font_config: Optional[FontConfig] = None
    margin_top: float = 50.0
    margin_bottom: float = 50.0
    margin_left: float = 50.0
    margin_right: float = 50.0
    line_spacing: float = 1.4  # Line height multiplier
    mirror_layout: bool = True  # Mirror x-positions for RTL
    preserve_positions: bool = True  # Try to match original positions
    add_page_numbers: bool = True
    page_number_text: str = "صفحة"  # "Page" in Arabic
    fallback_font_size: float = 12.0


class PDFRenderer:
    """
    Renders a TranslatedDocument to an Arabic RTL PDF.

    Usage:
        renderer = PDFRenderer(RendererConfig())
        renderer.render(translated_doc, "output.pdf")
    """

    def __init__(self, config: Optional[RendererConfig] = None):
        self.config = config or RendererConfig()
        self._font_registered = False
        self._font_names = {
            "regular": "",
            "bold": "",
            "italic": "",
            "bold_italic": "",
        }

    def render(
        self, doc: TranslatedDocument, output_path: str | Path
    ) -> Path:
        """
        Render the translated document to a PDF file.

        Args:
            doc: Translated document with all pages.
            output_path: Where to save the output PDF.

        Returns:
            Path to the generated PDF.

        Raises:
            RenderError: If rendering fails.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        self._register_fonts()

        try:
            self._render_document(doc, output_path)
        except Exception as e:
            raise RenderError(f"PDF rendering failed: {e}") from e

        logger.info(f"Rendered {len(doc.pages)} pages to {output_path}")
        return output_path

    def _register_fonts(self):
        """Register Arabic fonts with ReportLab."""
        if self._font_registered:
            return

        font_config = self.config.font_config
        if font_config is None:
            font_config = FontConfig().auto_detect()
            self.config.font_config = font_config

        try:
            base = font_config.family_name
            pdfmetrics.registerFont(TTFont(f"{base}", font_config.regular))
            pdfmetrics.registerFont(TTFont(f"{base}-Bold", font_config.bold))
            pdfmetrics.registerFont(TTFont(f"{base}-Italic", font_config.italic))
            pdfmetrics.registerFont(TTFont(f"{base}-BoldItalic", font_config.bold_italic))

            # Register font family for automatic style selection
            from reportlab.pdfbase.pdfmetrics import registerFontFamily
            registerFontFamily(
                base,
                normal=base,
                bold=f"{base}-Bold",
                italic=f"{base}-Italic",
                boldItalic=f"{base}-BoldItalic",
            )

            self._font_names = {
                "regular": base,
                "bold": f"{base}-Bold",
                "italic": f"{base}-Italic",
                "bold_italic": f"{base}-BoldItalic",
            }
            self._font_registered = True
            logger.info(f"Registered font family: {base}")

        except Exception as e:
            raise RenderError(f"Font registration failed: {e}") from e

    def _get_font_name(self, is_bold: bool = False, is_italic: bool = False) -> str:
        """Get the appropriate font name for the style."""
        if is_bold and is_italic:
            return self._font_names["bold_italic"]
        elif is_bold:
            return self._font_names["bold"]
        elif is_italic:
            return self._font_names["italic"]
        return self._font_names["regular"]

    def _render_document(self, doc: TranslatedDocument, output_path: Path):
        """Render all pages to a PDF file."""
        # Use first page dimensions or default to A4
        if doc.pages:
            first = doc.pages[0]
            pagesize = (first.width, first.height)
        else:
            pagesize = A4

        c = canvas.Canvas(str(output_path), pagesize=pagesize)
        c.setTitle("E2A Translated Document")
        c.setAuthor("E2A PDF Translator")

        for page in doc.pages:
            try:
                self._render_page(c, page)
            except Exception as e:
                logger.error(f"Page {page.page_number} render failed: {e}")
                # Draw error message on page
                self._render_error_page(c, page, str(e))
            c.showPage()

        c.save()

    def _render_page(self, c: canvas.Canvas, page: TranslatedPage):
        """Render a single translated page."""
        c.setPageSize((page.width, page.height))

        # --- Render images first (background layer) ---
        for img_block in page.image_blocks:
            self._render_image(c, img_block, page.width)

        # --- Render translated text ---
        if self.config.preserve_positions:
            self._render_positioned_text(c, page)
        else:
            self._render_flowing_text(c, page)

        # --- Page number ---
        if self.config.add_page_numbers:
            self._render_page_number(c, page)

    def _render_positioned_text(self, c: canvas.Canvas, page: TranslatedPage):
        """
        Render text blocks at their original (mirrored) positions.
        Best fidelity to original layout.
        """
        # Sort top-to-bottom (highest y1 first in PDF bottom-up coords)
        # so overlapping blocks render in the correct visual reading order.
        blocks = sorted(
            page.translated_blocks,
            key=lambda b: -b.original.bbox.y1,
        )
        for tblock in blocks:
            text = tblock.translated_text.strip()
            # Strip null bytes that can sneak in from figure label extraction
            text = text.replace("\x00", "")
            if not text:
                continue

            font = tblock.font
            font_name = self._get_font_name(font.is_bold, font.is_italic)
            font_size = max(font.size, 6.0)  # Minimum readable size

            # Prepare Arabic text (reshape + bidi)
            if has_arabic(text):
                display_text = prepare_arabic(text)
            else:
                display_text = text

            # Calculate position
            bbox = tblock.original.bbox

            # Place the first-line baseline at bbox.y1 - font_size.
            # For single-line blocks this equals bbox.y0 (exact match).
            # For multi-line merged paragraphs bbox.y0 is the bottom of
            # the last line, so the old code placed translated text
            # ~block_height below where the original paragraph started.
            y = bbox.y1 - font_size

            c.setFont(font_name, font_size)
            c.setFillColor(Color(*font.color))

            # Word wrap for long text
            max_width = page.width - self.config.margin_left - self.config.margin_right
            lines = self._wrap_text(c, display_text, font_name, font_size, max_width)

            for i, line in enumerate(lines):
                line_y = y - (i * font_size * self.config.line_spacing)
                # In positioned mode we respect original coordinates, so
                # allow content right down to 0 (the page edge).  The
                # margin_bottom guard is kept only to avoid going negative.
                if line_y < 0:
                    break

                # Right-align for Arabic (draw from right side)
                text_width = c.stringWidth(line, font_name, font_size)
                line_x = page.width - self.config.margin_right - text_width

                # Clamp to page bounds
                line_x = max(self.config.margin_left, line_x)

                c.drawString(line_x, line_y, line)

    def _render_flowing_text(self, c: canvas.Canvas, page: TranslatedPage):
        """
        Render text in a simple flowing layout (top-to-bottom, right-aligned).
        Used when position preservation isn't needed.
        """
        y = page.height - self.config.margin_top
        max_width = page.width - self.config.margin_left - self.config.margin_right

        for tblock in page.translated_blocks:
            text = tblock.translated_text.strip()
            if not text:
                continue

            font = tblock.font
            font_name = self._get_font_name(font.is_bold, font.is_italic)
            font_size = max(font.size, 6.0)

            if has_arabic(text):
                display_text = prepare_arabic(text)
            else:
                display_text = text

            c.setFont(font_name, font_size)
            c.setFillColor(Color(*font.color))

            lines = self._wrap_text(c, display_text, font_name, font_size, max_width)

            for line in lines:
                if y < self.config.margin_bottom:
                    break

                text_width = c.stringWidth(line, font_name, font_size)
                x = page.width - self.config.margin_right - text_width
                x = max(self.config.margin_left, x)

                c.drawString(x, y, line)
                y -= font_size * self.config.line_spacing

            # Paragraph spacing
            y -= font_size * 0.5

    def _render_image(
        self, c: canvas.Canvas, img: ImageBlock, page_width: float
    ):
        """Render an image block onto the canvas."""
        try:
            img_reader = ImageReader(io.BytesIO(img.image_bytes))
            bbox = img.bbox

            if self.config.mirror_layout:
                bbox = bbox.mirror_x(page_width)

            c.drawImage(
                img_reader,
                bbox.x0,
                bbox.y0,
                width=bbox.width,
                height=bbox.height,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception as e:
            logger.warning(f"Failed to render image: {e}")

    def _render_page_number(self, c: canvas.Canvas, page: TranslatedPage):
        """Add Arabic page number at the bottom center."""
        font_name = self._font_names["regular"]
        c.setFont(font_name, 10)
        c.setFillColor(black)

        page_text = prepare_arabic(f"{self.config.page_number_text} {page.page_number}")
        text_width = c.stringWidth(page_text, font_name, 10)
        x = (page.width - text_width) / 2
        y = self.config.margin_bottom / 2

        c.drawString(x, y, page_text)

    def _render_error_page(
        self, c: canvas.Canvas, page: TranslatedPage, error: str
    ):
        """Render an error message on a page that failed."""
        c.setPageSize((page.width, page.height))
        c.setFont("Helvetica", 14)
        c.setFillColor(Color(0.8, 0, 0))
        c.drawString(50, page.height - 50, f"Translation Error on Page {page.page_number}")
        c.setFont("Helvetica", 10)
        c.setFillColor(black)
        c.drawString(50, page.height - 80, f"Error: {error[:200]}")
        c.drawString(50, page.height - 100, "Original content could not be translated.")

    def _wrap_text(
        self,
        c: canvas.Canvas,
        text: str,
        font_name: str,
        font_size: float,
        max_width: float,
    ) -> list[str]:
        """
        Word-wrap text to fit within max_width.
        Handles both Arabic and Latin text.
        """
        if not text:
            return []

        # Check if text fits on one line
        if c.stringWidth(text, font_name, font_size) <= max_width:
            return [text]

        words = text.split()
        lines: list[str] = []
        current_line: list[str] = []

        for word in words:
            test_line = " ".join(current_line + [word])
            if c.stringWidth(test_line, font_name, font_size) <= max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                current_line = [word]

                # If single word is wider than max, force it onto a line
                if c.stringWidth(word, font_name, font_size) > max_width:
                    lines.append(word)
                    current_line = []

        if current_line:
            lines.append(" ".join(current_line))

        return lines
