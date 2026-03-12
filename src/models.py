"""
Data models for E2A PDF translation pipeline.

Defines structured representations of PDF content at page and block level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class BlockType(Enum):
    TEXT = auto()
    IMAGE = auto()
    TABLE = auto()


@dataclass
class FontInfo:
    """Font metadata extracted from a text span."""
    name: str = "Unknown"
    size: float = 12.0
    is_bold: bool = False
    is_italic: bool = False
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)  # RGB 0-1

    @property
    def style_key(self) -> str:
        """Unique key for grouping spans with same styling."""
        bold = "B" if self.is_bold else ""
        italic = "I" if self.is_italic else ""
        return f"{self.name}_{self.size:.1f}_{bold}{italic}"


@dataclass
class BBox:
    """Bounding box in PDF coordinates (origin = bottom-left for ReportLab)."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2

    def mirror_x(self, page_width: float) -> BBox:
        """Mirror horizontally for RTL layout."""
        return BBox(
            x0=page_width - self.x1,
            y0=self.y0,
            x1=page_width - self.x0,
            y1=self.y1,
        )


@dataclass
class TextSpan:
    """A contiguous run of text with uniform styling."""
    text: str
    font: FontInfo
    bbox: BBox


@dataclass
class TextBlock:
    """
    A logical paragraph or text region.
    May contain multiple spans with different styles.
    """
    spans: list[TextSpan] = field(default_factory=list)
    bbox: BBox = field(default_factory=lambda: BBox(0, 0, 0, 0))
    block_type: BlockType = BlockType.TEXT

    @property
    def full_text(self) -> str:
        if not self.spans:
            return ""
        # Group spans into lines by similar y1 (top edge), then sort each
        # line left-to-right and insert spaces where there is a visible gap.
        # This handles character-level extraction where every glyph is its
        # own span and uppercase/descender glyphs have slightly different y1.
        line_height = max(s.font.size for s in self.spans)
        tolerance = line_height * 0.5

        all_spans = sorted(self.spans, key=lambda s: -s.bbox.y1)
        lines: list[list[TextSpan]] = [[all_spans[0]]]
        for span in all_spans[1:]:
            if abs(span.bbox.y1 - lines[-1][0].bbox.y1) <= tolerance:
                lines[-1].append(span)
            else:
                lines.append([span])

        line_texts: list[str] = []
        for line in lines:
            line.sort(key=lambda s: s.bbox.x0)
            parts: list[str] = []
            for i, span in enumerate(line):
                if i > 0:
                    gap = span.bbox.x0 - line[i - 1].bbox.x1
                    if gap > span.font.size * 0.4:
                        parts.append(" ")
                parts.append(span.text)
            line_texts.append("".join(parts))

        return " ".join(line_texts)

    @property
    def primary_font(self) -> FontInfo:
        """Most common font in this block (by text length)."""
        if not self.spans:
            return FontInfo()
        font_lengths: dict[str, tuple[int, FontInfo]] = {}
        for span in self.spans:
            key = span.font.style_key
            cur_len, _ = font_lengths.get(key, (0, span.font))
            font_lengths[key] = (cur_len + len(span.text), span.font)
        return max(font_lengths.values(), key=lambda x: x[0])[1]


@dataclass
class ImageBlock:
    """An image extracted from a PDF page."""
    image_bytes: bytes
    bbox: BBox
    extension: str = "png"  # png, jpeg, etc.
    xref: int = 0  # PDF cross-reference ID


@dataclass
class PageContent:
    """All content from a single PDF page."""
    page_number: int
    width: float
    height: float
    text_blocks: list[TextBlock] = field(default_factory=list)
    image_blocks: list[ImageBlock] = field(default_factory=list)
    rotation: int = 0


@dataclass
class DocumentContent:
    """Complete document extraction result."""
    pages: list[PageContent] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    source_path: str = ""
    is_scanned: bool = False
    extraction_warnings: list[str] = field(default_factory=list)


@dataclass
class TranslatedBlock:
    """A text block with its translation."""
    original: TextBlock
    translated_text: str
    font: FontInfo


@dataclass
class TranslatedPage:
    """A page with translated content ready for rendering."""
    page_number: int
    width: float
    height: float
    translated_blocks: list[TranslatedBlock] = field(default_factory=list)
    image_blocks: list[ImageBlock] = field(default_factory=list)
    untranslated_blocks: list[TextBlock] = field(default_factory=list)  # fallbacks


@dataclass
class TranslatedDocument:
    """Full translated document."""
    pages: list[TranslatedPage] = field(default_factory=list)
    source_path: str = ""
    target_language: str = "ar"
    stats: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
