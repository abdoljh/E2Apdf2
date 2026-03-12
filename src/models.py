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
        # Use the block's overall height to estimate line height.
        # With character-level spans, individual font.size values are
        # unreliable (descenders like 'g' report ~14pt, 'a' reports ~7pt
        # for the same 12pt line).  Instead, detect the actual line pitch
        # from the Y-coordinate clusters.
        all_spans = sorted(self.spans, key=lambda s: -s.bbox.y1)

        # --- Phase 1: cluster spans into lines by y1 proximity ---
        # Use a generous tolerance: the max y1-spread within a single
        # text line is about 1× the nominal font size (ascenders vs
        # descenders).  We estimate nominal font size as the median
        # span height, then allow 1.0× that as tolerance.
        heights = sorted(s.bbox.height for s in all_spans if s.bbox.height > 0.5)
        if heights:
            nominal_size = heights[len(heights) // 2]
        else:
            nominal_size = 12.0
        tolerance = max(nominal_size * 1.0, 8.0)

        lines: list[list[TextSpan]] = [[all_spans[0]]]
        for span in all_spans[1:]:
            # Compare against the MEAN y1 of the current line cluster
            # (not just the first span) to avoid drift.
            line_y1_mean = sum(s.bbox.y1 for s in lines[-1]) / len(lines[-1])
            if abs(span.bbox.y1 - line_y1_mean) <= tolerance:
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
                    if gap > nominal_size * 0.3:
                        parts.append(" ")
                parts.append(span.text)
            line_texts.append("".join(parts))

        # --- Phase 2: join visual lines into continuous prose ---
        # PDF visual lines are NOT logical line breaks — they're just
        # where text wraps on the page.  Strip \r\n, rejoin hyphenated
        # words, and collapse into a single flowing paragraph.
        joined_lines: list[str] = []
        for lt in line_texts:
            # Strip trailing/leading whitespace and CR/LF
            cleaned = lt.replace("\r\n", "").replace("\r", "").replace("\n", "").strip()
            if cleaned:
                joined_lines.append(cleaned)

        # Rejoin: if a line ends mid-word (last char is a letter, next
        # line starts with a lowercase letter), join without space.
        result_parts: list[str] = []
        for i, line_str in enumerate(joined_lines):
            if i > 0 and result_parts:
                prev = result_parts[-1]
                # Hyphenated word break: "over-\n whelming" → "overwhelming"
                if prev.endswith("-"):
                    result_parts[-1] = prev[:-1]  # remove trailing hyphen
                # If previous line ends with a letter and current starts
                # with a lowercase letter, the word was split across lines
                elif (prev and prev[-1].isalpha()
                      and line_str and line_str[0].islower()):
                    result_parts.append(" ")
                else:
                    result_parts.append(" ")
            result_parts.append(line_str)

        return "".join(result_parts)

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
