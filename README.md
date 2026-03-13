# E2A PDF Translator

**English-to-Arabic PDF translation with full RTL support, letter shaping, and clean A4 reflow layout.**

E2A extracts text from English PDF documents, translates it to Arabic using your choice of translation backend, and renders the output onto properly formatted A4 pages with connected Arabic letter forms, right-to-left paragraph alignment, and automatic pagination.

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [How It Works](#how-it-works)
4. [Installation](#installation)
5. [Quick Start](#quick-start)
6. [Translation Backends](#translation-backends)
7. [Rendering Modes](#rendering-modes)
8. [Configuration Reference](#configuration-reference)
9. [Project Structure](#project-structure)
10. [Module Reference](#module-reference)
11. [Arabic Text Processing](#arabic-text-processing)
12. [Known Limitations](#known-limitations)
13. [Troubleshooting](#troubleshooting)
14. [Development Notes](#development-notes)
15. [Changelog](#changelog)
16. [License](#license)

---

## Features

- **Multiple translation backends**: Free (Google Translate, no API key), Google Cloud Translation, DeepL, OpenAI GPT, and Anthropic Claude
- **Clean A4 reflow rendering**: Translated text is reflowed onto fresh A4 pages with configurable font size (default 14pt), proper margins, and automatic pagination — no coordinate system bugs
- **Full Arabic text support**: Letter reshaping (positional forms), BiDi reordering (per-line, not per-paragraph), Lam-Alef ligatures, and diacritic preservation
- **Automatic heading detection**: Source PDF font sizes are analyzed to identify titles, headings, and body text, which are then styled proportionally in the output
- **Image reinsertion**: Extracted images are scaled and placed inline in the output PDF
- **Translation caching**: SHA-256-keyed persistent cache avoids re-translating identical text across runs
- **Skip detection**: Numbers, formulas, URLs, emails, and already-Arabic text are passed through without translation
- **Per-page error isolation**: If one page fails to extract or translate, the rest of the document still processes
- **Streamlit web interface**: Upload a PDF, choose your backend, configure settings, and download the translated PDF — all from a browser
- **CLI support**: `python -m src input.pdf --backend free` for scripted workflows
- **Self-contained Arabic processing**: Built-in reshaping and BiDi algorithms with zero external dependencies (falls back to `arabic-reshaper` and `python-bidi` if installed for higher fidelity)

---

## Architecture

```
┌─────────────┐    ┌──────────────┐    ┌────────────────────┐    ┌─────────────┐
│  Input PDF  │───>│  Extractor   │───>│    Translator      │───>│  Renderer   │
│  (English)  │    │  (pypdfium2) │    │  (free/API/LLM)    │    │  (Reflow)   │
└─────────────┘    └──────────────┘    └────────────────────┘    └─────────────┘
                          │                      │                       │
                   Text blocks with        Translated text         Clean A4 PDF
                   font info + bbox        per block               with Arabic RTL
                   + images                + cache                 + page numbers
```

The pipeline is orchestrated by `E2APipeline` which chains three stages:

1. **Extraction** (`extractor.py`): Reads the PDF with pypdfium2, extracts character-level spans with font metadata and bounding boxes, merges them into logical paragraphs, and extracts images.

2. **Translation** (`translator.py`): Sends extracted text blocks to the chosen backend. Manages caching, skip detection, batching, and retry logic. Each block is translated as continuous prose (visual line breaks from the PDF are stripped).

3. **Rendering** (`renderer_reflow.py`): Reflows translated Arabic text onto fresh A4 pages using ReportLab's Platypus layout engine. Applies Arabic letter reshaping, per-line BiDi reordering, automatic word wrapping, heading/title styling, image insertion, page numbers, and optional running headers.

---

## How It Works

### The Arabic Rendering Challenge

Arabic PDF rendering requires solving three problems that don't exist for Latin scripts:

**1. Letter Reshaping** — Arabic letters change form based on their position in a word (isolated, initial, medial, final). The letter "ب" has four forms: ب ﺑ ﺒ ﺐ. Without reshaping, Arabic text renders as disconnected isolated letters.

**2. BiDi (Bidirectional) Reordering** — Arabic reads right-to-left, but PDF engines render left-to-right. Text must be reordered from logical (storage) order to visual (display) order. The critical subtlety: this reordering must be applied **per wrapped line**, not per paragraph. Applying BiDi to an entire paragraph reverses the sentence order, putting the last sentence at the top.

**3. Line Wrapping** — Arabic words have different width characteristics than the Latin source. The translated text must be re-wrapped to fit A4 page margins, and the BiDi reordering must happen *after* wrapping so each visual line is self-contained.

E2A solves all three with a pipeline inside `renderer_reflow.py`:

```
Translated Arabic text (logical order)
        │
        ▼
  reshape_arabic()     ← Connect letters into positional forms
        │
        ▼
  _wrap_arabic_text()  ← Word-wrap to fit A4 margins (with font metrics)
        │
        ▼
  bidi_reorder()       ← Apply per-line, converting logical → visual order
        │
        ▼
  Join with <br/>      ← Feed to ReportLab Paragraph as pre-wrapped lines
        │
        ▼
  Platypus renders     ← Right-aligned, paginated, with page numbers
```

### Text Extraction Pipeline

PDFs store text as positioned glyphs, not logical paragraphs. The extractor reconstructs reading order through several steps:

1. **Character extraction**: pypdfium2 provides individual character bounding boxes
2. **Span assembly**: Adjacent characters with similar Y-coordinates and font sizes are grouped into spans
3. **Line clustering**: Spans are clustered into lines using median glyph height with generous tolerance (handles ascender/descender variation like 'g' vs 'a')
4. **Paragraph merging**: Vertically adjacent lines with similar indentation and small gaps are merged into paragraph blocks
5. **Prose joining**: Visual line breaks (`\r\n`) are stripped and broken words are rejoined, producing continuous flowing text suitable for translation

---

## Installation

### Requirements

- Python 3.10+
- System package: `fonts-freefont-ttf` (provides FreeSerif with Arabic glyphs)

### Install Dependencies

```bash
pip install pypdfium2 pypdf reportlab requests streamlit
```

Optional (higher-quality Arabic shaping):
```bash
pip install arabic-reshaper python-bidi
```

### System Font (Linux/Debian/Ubuntu)

```bash
sudo apt-get install -y fonts-freefont-ttf
```

On Streamlit Cloud, add `fonts-freefont-ttf` to your `packages.txt`.

### Project Files

```
e2apdf/
├── streamlit_app.py          # Web interface
├── requirements.txt          # Python dependencies
├── packages.txt              # System packages (for Streamlit Cloud)
└── src/
    ├── __init__.py
    ├── __main__.py            # CLI entry point
    ├── models.py              # Data models (TextBlock, TranslatedDocument, etc.)
    ├── arabic_utils.py        # Letter reshaping + BiDi reordering
    ├── extractor.py           # PDF text/image extraction (pypdfium2 + pypdf)
    ├── translator.py          # Translation backends + caching
    ├── renderer.py            # Original position-preserving renderer (legacy)
    ├── renderer_reflow.py     # A4 reflow renderer (recommended)
    └── pipeline.py            # Orchestrates extract → translate → render
```

---

## Quick Start

### Streamlit Web App

```bash
streamlit run streamlit_app.py
```

1. Open the browser at `http://localhost:8501`
2. Upload an English PDF
3. Select a translation backend (Free requires no API key)
4. Choose render mode (Reflow recommended)
5. Click **Translate to Arabic**
6. Download the translated PDF

### Command Line

```bash
# Quick test with mock translator (no API needed)
python -m src input.pdf --backend mock

# Real translation with free Google Translate
python -m src input.pdf --backend free -o output_ar.pdf

# High-quality with Anthropic Claude
python -m src input.pdf --backend llm-anthropic --api-key YOUR_KEY

# Specify font size and line spacing
python -m src input.pdf --backend free --font-size 14 --line-spacing 1.6
```

### Python API

```python
from src.pipeline import E2APipeline, PipelineConfig

config = PipelineConfig(
    translation_backend="free",
    render_mode="reflow",
    reflow_font_size=14,
)
pipeline = E2APipeline(config)
report = pipeline.translate("input.pdf", "output_ar.pdf")
print(report.summary())
```

---

## Translation Backends

| Backend | API Key Required | Quality | Speed | Cost |
|---------|:---:|:---:|:---:|:---:|
| `free` | No | Good | Fast | Free |
| `mock` | No | N/A (test only) | Instant | Free |
| `google` | Yes | Good | Fast | Pay-per-use |
| `deepl` | Yes | Very Good | Fast | Pay-per-use |
| `llm-openai` | Yes | Excellent | Slower | Pay-per-use |
| `llm-anthropic` | Yes | Excellent | Slower | Pay-per-use |

### Free Backend

Uses Google Translate's unofficial `gtx` endpoint (same as browser extensions). No API key needed. Suitable for moderate document volumes. Good general-purpose translation quality.

### LLM Backends (OpenAI / Anthropic)

Higher translation quality, especially for technical and academic content. The LLM receives batched text segments with instructions to preserve numbers, proper nouns, and formatting. Default models: `gpt-4o-mini` (OpenAI), `claude-sonnet-4-20250514` (Anthropic).

### API Key Configuration

Keys can be provided via (in priority order):

1. Streamlit secrets (`st.secrets["ANTHROPIC_API_KEY"]`)
2. Environment variables (`export ANTHROPIC_API_KEY=...`)
3. Manual entry in the Streamlit sidebar
4. CLI `--api-key` flag

---

## Rendering Modes

### Reflow Mode (Recommended)

`render_mode="reflow"`

Ignores original PDF coordinates entirely. Translated text is reflowed onto clean A4 pages with consistent formatting:

- **A4 page size** (595 × 842 points)
- **Configurable font size** (default 14pt body, headings/titles scale proportionally)
- **Right-aligned Arabic paragraphs** with proper RTL display
- **Automatic pagination** via ReportLab's Platypus engine
- **Source page markers** showing where each original page's content begins
- **Page numbers** at the footer
- **Optional running header**

This mode eliminates all coordinate-system bugs that plague position-preserving approaches.

### Positioned Mode (Legacy)

`render_mode="positioned"`

Attempts to place translated text at the original PDF coordinates (mirrored for RTL). Provided for backward compatibility but not recommended — Arabic text has different width characteristics than English, making position matching unreliable.

---

## Configuration Reference

### PipelineConfig

| Parameter | Default | Description |
|-----------|---------|-------------|
| `translation_backend` | `"mock"` | Backend: `mock`, `free`, `google`, `deepl`, `llm-openai`, `llm-anthropic` |
| `api_key` | `None` | API key for the chosen backend |
| `model` | `None` | Override default model for LLM backends |
| `render_mode` | `"reflow"` | `"reflow"` (recommended) or `"positioned"` |
| `reflow_font_size` | `14.0` | Body text size in points |
| `reflow_heading_scale` | `1.35` | Heading font = body × this |
| `reflow_title_scale` | `1.65` | Title font = body × this |
| `reflow_line_spacing` | `1.6` | Line height = font_size × this |
| `reflow_paragraph_spacing` | `8.0` | Extra space between paragraphs (points) |
| `reflow_show_source_markers` | `True` | Show "─── Source Page N ───" dividers |
| `reflow_header_text` | `""` | Running header text (Arabic supported) |
| `add_page_numbers` | `True` | Footer page numbers |
| `cache_path` | `".e2a_cache/translations.json"` | Translation cache file path |
| `merge_paragraphs` | `True` | Merge adjacent text lines into paragraphs |
| `font_path` | `None` | Custom Arabic .ttf font file path |
| `continue_on_error` | `True` | Continue processing if a page fails |
| `verbose` | `False` | Debug-level logging |

### Font Auto-Detection

The renderer searches for Arabic-capable fonts in this order:

1. Custom path (if `font_path` specified)
2. Amiri (beautiful Arabic naskh)
3. Noto Naskh Arabic
4. FreeSerif (most commonly available on Linux)
5. FreeSans
6. DejaVu Sans (partial Arabic)
7. Helvetica (fallback — Arabic will NOT render)

---

## Project Structure

### Module Reference

#### `models.py` (234 lines)

Data models for the entire pipeline. Key classes:

- **`TextBlock`**: A logical paragraph extracted from the PDF. Contains spans (character runs) with font info and bounding boxes. The `full_text` property reassembles spans into continuous prose by clustering characters into lines (using median height tolerance), sorting left-to-right, and stripping visual line breaks.

- **`TranslatedDocument`**: Container for all translated pages, passed from translator to renderer.

- **`FontInfo`**: Font metadata (name, size, bold, italic, color) extracted per span.

- **`BBox`**: Bounding box with `mirror_x()` for RTL layout conversion.

#### `arabic_utils.py` (299 lines)

Self-contained Arabic text processing with no mandatory external dependencies.

- **`reshape_arabic(text)`**: Converts base Arabic Unicode to positional presentation forms. Handles all 36 Arabic letters, Lam-Alef ligatures, diacritics (tashkeel), and right-join-only letters (Alef, Dal, Ra, Waw, etc.).

- **`bidi_reorder(text)`**: Simplified BiDi algorithm for RTL visual ordering. Splits text into directional runs (Arabic, Latin, digits), reverses run order, and mirrors brackets. Sufficient for translated text; for edge cases, install `python-bidi`.

- **`prepare_arabic(text)`**: Full pipeline (reshape + bidi). Use for **short single-line strings** only (headers, titles). For paragraphs, use `reshape_arabic()` followed by per-line `bidi_reorder()`.

- **`has_arabic(text)`**: Detection utility.

If `arabic-reshaper` and `python-bidi` are installed, all functions automatically delegate to them for higher-fidelity processing.

#### `extractor.py` (189 lines)

PDF content extraction using pypdfium2 (primary) with pypdf fallback.

- Extracts character-level bounding boxes and font sizes
- Clusters characters into lines using median-height tolerance (handles ascender/descender variation)
- Merges adjacent lines into paragraph blocks using configurable gap factor
- Joins visual line breaks into continuous prose (strips `\r\n`, rejoins hyphenated words)
- Extracts embedded images with position data
- Detects scanned PDFs (minimal text) and warns

#### `translator.py` (180 lines)

Translation orchestration with multiple backends.

- **`TranslationCache`**: SHA-256-keyed persistent JSON cache
- **`should_skip_translation()`**: Detects untranslatable content (numbers, URLs, already-Arabic)
- **Backend classes**: `MockTranslationBackend`, `FreeTranslationBackend`, `GoogleTranslationBackend`, `DeepLTranslationBackend`, `LLMTranslationBackend`
- **Batch translation**: Groups text blocks to minimize API calls
- **Per-page error isolation**: Failed pages fall back to original text

#### `renderer_reflow.py` (654 lines)

The A4 reflow renderer — the core of the output quality.

- **`ReflowRenderer.render(doc, output_path)`**: Main entry point, same signature as the legacy renderer for drop-in replacement
- **`_prepare_arabic_paragraph()`**: The critical Arabic rendering pipeline — reshape → wrap → per-line BiDi → join with `<br/>`
- **`_wrap_arabic_text()`**: Manual word wrapping using ReportLab font metrics, with 5% overflow tolerance and single-word orphan absorption
- **`_classify_block()`**: Heuristic heading/title detection based on source font size relative to document median
- **`_on_page()`**: Page decoration (footer numbers, running header)
- Uses 90% of usable width for wrapping to prevent Platypus from re-wrapping pre-formatted lines

#### `pipeline.py` (360 lines)

Orchestrates the three-stage pipeline.

- **`PipelineConfig`**: All configuration in one dataclass
- **`E2APipeline.translate()`**: Runs extract → translate → render with progress logging
- **`PipelineReport`**: Statistics (pages, blocks, cache hits, duration, warnings, errors)
- **`translate_pdf()`**: Convenience one-liner function
- Routes to `ReflowRenderer` (default) or legacy `PDFRenderer` based on `render_mode`

#### `streamlit_app.py` (422 lines)

Web interface with:

- Backend selection with API key management (secrets/env/manual)
- Render mode radio (Reflow vs Positioned)
- Reflow-specific controls (font size, line spacing, source markers, running header)
- Progress bar with stage indicators
- Log viewer (expandable)
- Statistics dashboard (pages, blocks, cache hits, duration)
- Download button for translated PDF
- Warning/error display

---

## Arabic Text Processing

### Why Per-Line BiDi Matters

This is the most important design decision in the renderer. Consider translating "First sentence. Second sentence." to Arabic:

```
Full paragraph BiDi (WRONG — reverses sentence order):
  ".ﺔﻴﻧﺎﺜﻟﺍ ﺔﻠﻤﺠﻟﺍ .ﻰﻟﻭﻷﺍ ﺔﻠﻤﺠﻟﺍ"
  → Second sentence appears FIRST when rendered

Per-line BiDi (CORRECT — each line self-contained):
  Line 1: ".ﻰﻟﻭﻷﺍ ﺔﻠﻤﺠﻟﺍ"    ← First sentence
  Line 2: ".ﺔﻴﻧﺎﺜﻟﺍ ﺔﻠﻤﺠﻟﺍ"    ← Second sentence
  → Correct reading order when rendered RTL
```

The renderer applies `reshape_arabic()` to the full paragraph (letter forms don't depend on line context), then word-wraps, then applies `bidi_reorder()` to each wrapped line individually.

### Font Requirements

Arabic rendering requires a font with:
- Full Arabic Unicode block coverage (U+0600–U+06FF)
- Arabic Presentation Forms-B (U+FE70–U+FEFF) for positional letter forms
- Lam-Alef ligature glyphs

FreeSerif (from `fonts-freefont-ttf`) meets all requirements and is the recommended default. Amiri and Noto Naskh Arabic produce more aesthetically pleasing output if available.

---

## Known Limitations

1. **Scanned/image-based PDFs**: The extractor requires selectable text. Scanned documents need OCR preprocessing (e.g., Tesseract) before translation.

2. **Tables**: Table content is extracted as text blocks without structural information. Complex tables may render as dense paragraphs rather than formatted grids.

3. **Mathematical formulas**: Inline equations and LaTeX-style notation may not translate or render correctly.

4. **Source page markers**: The "صفحة المصدر" divider text currently displays with reversed letters due to the BiDi processing path. This is a cosmetic issue.

5. **Column layouts**: Multi-column PDFs may have columns interleaved in the extracted text. The extractor sorts by Y-coordinate (top-to-bottom) which can mix columns.

6. **Encrypted PDFs**: Password-protected PDFs must be decrypted before processing.

7. **Very large documents**: Documents over ~100 pages may hit translation API rate limits. Use caching and consider processing in batches.

8. **Mixed RTL/LTR content**: Embedded English terms, chemical formulas, and abbreviations (like "BRAF V600E" or "BLEVE") pass through correctly, but complex mixed-direction paragraphs may have minor ordering artifacts.

---

## Troubleshooting

### "No Arabic-capable font found"

Install system fonts:
```bash
# Linux/Debian/Ubuntu
sudo apt-get install -y fonts-freefont-ttf

# Or specify a custom font
python -m src input.pdf --font /path/to/Amiri-Regular.ttf
```

### Arabic text appears as disconnected letters

This means reshaping isn't being applied. Check that:
- The `renderer_reflow.py` module is being used (not the legacy renderer)
- The font supports Arabic Presentation Forms-B (FreeSerif, Amiri, or Noto Naskh)

### Text appears reversed (last sentence first)

This indicates full-paragraph BiDi is being applied instead of per-line BiDi. Ensure you're using the latest `renderer_reflow.py` which calls `reshape_arabic()` + per-line `bidi_reorder()`.

### Orphaned single words on their own line

The word-wrap tolerance may need adjustment. The renderer uses 90% of usable width with 5% overflow tolerance to prevent Platypus from re-wrapping. If orphans appear, the font metrics measurement may differ from Platypus rendering — try adjusting `reflow_font_size` by ±1pt.

### Translation API errors

- **Free backend**: Google may rate-limit frequent requests. Add delays between documents.
- **LLM backends**: Check API key validity, model name, and account billing status.
- **All backends**: The cache saves successful translations, so re-running after fixing an API issue will skip already-translated blocks.

### Empty output PDF

The source PDF likely has no selectable text (scanned document). Check the pipeline log for "PDF appears scanned" warnings. Preprocess with OCR.

---

## Development Notes

### Running Tests

```bash
# Basic smoke test with mock backend
python -m src test_document.pdf --backend mock

# Test with real translation
python -m src test_document.pdf --backend free -o test_output.pdf

# Run the included test script
python test_reflow.py
```

### Adding a New Translation Backend

1. Create a class inheriting from `TranslationBackend` in `translator.py`
2. Implement `translate_texts(texts, source_lang, target_lang) -> list[str]` and `name() -> str`
3. Register it in `Translator._create_backend()`
4. Add the option to the Streamlit sidebar in `streamlit_app.py`

### Key Design Decisions

- **Platypus over Canvas**: The reflow renderer uses ReportLab's Platypus (Page Layout and Typography Using Scripts) rather than direct Canvas drawing. This avoids all coordinate-system bugs and provides automatic pagination, but requires manual word wrapping for Arabic (since Platypus doesn't understand RTL wrapping).

- **Per-line BiDi**: The single most critical decision. Full-paragraph BiDi reverses sentence order; per-line BiDi preserves it. The renderer word-wraps first, then applies BiDi to each line.

- **90% wrap width**: The renderer wraps text at 90% of the page's usable width, leaving a 10% buffer. This prevents Platypus from re-wrapping our pre-formatted `<br/>`-joined lines (which would create orphaned words).

- **Continuous prose extraction**: The extractor strips all `\r\n` from extracted text and joins visual lines into continuous paragraphs before sending to the translator. This prevents the translator from receiving fragmented sentences that it would translate independently and then reassemble incorrectly.

- **Self-contained Arabic processing**: The built-in reshaping and BiDi algorithms have zero external dependencies, making deployment on constrained environments (like Streamlit Cloud) reliable. If `arabic-reshaper` and `python-bidi` are installed, they're used automatically for higher fidelity.

---

## Changelog

### v0.2.0 — Reflow Renderer

- **New**: A4 reflow rendering mode (recommended default)
- **New**: Per-line BiDi reordering (fixes paragraph reversal)
- **New**: Manual word wrapping with orphan prevention
- **New**: Streamlit render mode selector (Reflow vs Positioned)
- **Fixed**: Y-coordinate inversion (bottom-to-top text)
- **Fixed**: Character-level span clustering (glyph height variation)
- **Fixed**: Visual line breaks in extracted text (sentence jumbling)
- **Fixed**: Full-paragraph BiDi reversal (last sentence first)
- **Fixed**: Single-word orphaned lines after wrapping

### v0.1.0 — Initial Release

- Position-preserving renderer with Canvas drawing
- Multiple translation backends (mock, free, Google, DeepL, OpenAI, Anthropic)
- pypdfium2 extraction with pypdf fallback
- Arabic reshaping and BiDi (built-in + external library support)
- Translation caching
- Streamlit web interface
- CLI interface

---

## License

MIT License. See LICENSE file for details.

---

## Acknowledgments

- [ReportLab](https://www.reportlab.com/) — PDF generation engine
- [pypdfium2](https://github.com/nicegist/pypdfium2) — PDF content extraction
- [arabic-reshaper](https://github.com/mpcabd/python-arabic-reshaper) — Arabic letter shaping (optional)
- [python-bidi](https://github.com/MichaelAqworthy/python-bidi) — BiDi algorithm (optional)
- [Streamlit](https://streamlit.io/) — Web interface framework

---

*Built by Abdol · [GitHub](https://github.com/abdoljh/e2apdf)*
