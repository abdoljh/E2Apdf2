"""
PDF content extractor for E2A PDF Translator.

Extraction backends (in priority order):
  1. PyMuPDF (fitz) — block-level extraction, best text quality
  2. pypdfium2       — character-level with span merging
  3. pypdf           — basic paragraph-level fallback

PyMuPDF's get_text("blocks") returns pre-assembled text blocks with
bounding boxes, eliminating the character-level span clustering issues
that pypdfium2 requires.
"""
from __future__ import annotations
import io, logging
from pathlib import Path
from typing import Optional
from .models import (BBox, BlockType, DocumentContent, FontInfo,
                     ImageBlock, PageContent, TextBlock, TextSpan)

logger = logging.getLogger(__name__)
_PARA_GAP_FACTOR = 0.6
_MIN_TEXT_LENGTH = 1

class ExtractionError(Exception):
    pass

class PDFExtractor:
    def __init__(self, merge_paragraphs=True, min_text_length=_MIN_TEXT_LENGTH):
        self.merge_paragraphs = merge_paragraphs
        self.min_text_length = min_text_length
        self._current_pdf_path = None

    def extract(self, pdf_path):
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise ExtractionError(f"File not found: {pdf_path}")
        self._current_pdf_path = str(pdf_path)
        doc = DocumentContent(source_path=str(pdf_path))

        backends = [
            ("PyMuPDF", self._extract_with_pymupdf),
            ("pypdfium2", self._extract_with_pypdfium2),
            ("pypdf", self._extract_with_pypdf),
        ]
        last_err = None
        for name, method in backends:
            try:
                doc = method(pdf_path, doc)
                logger.info(f"Extraction succeeded with {name}")
                break
            except Exception as e:
                logger.warning(f"{name} failed: {e}")
                last_err = e
                doc = DocumentContent(source_path=str(pdf_path))
        else:
            raise ExtractionError(f"All backends failed. Last: {last_err}")

        total_text = sum(len(b.full_text) for p in doc.pages for b in p.text_blocks)
        if total_text < 50 and len(doc.pages) > 0:
            doc.is_scanned = True
            doc.extraction_warnings.append("PDF appears scanned.")
        return doc

    # ═══════════════════════════════════════════════════════════
    # Backend 1: PyMuPDF (fitz) — PREFERRED
    # ═══════════════════════════════════════════════════════════
    def _extract_with_pymupdf(self, pdf_path, doc):
        import fitz
        pdf = fitz.open(str(pdf_path))
        for page_idx in range(len(pdf)):
            try:
                page = pdf[page_idx]
                w, h = page.rect.width, page.rect.height
                pc = PageContent(page_number=page_idx+1, width=w, height=h)

                # Text: get_text("blocks") → (x0,y0,x1,y1,text,block_no,type)
                for b in page.get_text("blocks"):
                    x0, y0, x1, y1, text, block_no, block_type = b
                    if block_type != 0: continue  # skip image blocks
                    text = text.replace("\n", " ").strip()
                    if len(text) < self.min_text_length: continue

                    # PyMuPDF: top-left origin → convert to bottom-left
                    bbox = BBox(x0, h - y1, x1, h - y0)
                    line_count = max(1, b[4].count("\n") + 1)
                    est_fs = max(6.0, min((y1 - y0) / line_count * 0.7, 36.0))
                    font = FontInfo(size=est_fs, is_bold=est_fs > 14)
                    span = TextSpan(text=text, font=font, bbox=bbox)
                    pc.text_blocks.append(TextBlock(spans=[span], bbox=bbox))

                # Images via PyMuPDF
                try:
                    pc.image_blocks = self._images_pymupdf(pdf, page, page_idx, w, h)
                except Exception as e:
                    logger.warning(f"PyMuPDF image extraction failed p{page_idx+1}: {e}")

                if self.merge_paragraphs and pc.text_blocks:
                    pc.text_blocks = self._merge_blocks(pc.text_blocks)
                doc.pages.append(pc)
            except Exception as e:
                logger.error(f"PyMuPDF page {page_idx+1} failed: {e}")
                doc.extraction_warnings.append(f"Page {page_idx+1}: {e}")
                try:
                    p = pdf[page_idx]
                    doc.pages.append(PageContent(page_number=page_idx+1, width=p.rect.width, height=p.rect.height))
                except:
                    doc.pages.append(PageContent(page_number=page_idx+1, width=612, height=792))
        pdf.close()

        # ── Cross-page deduplication ────────────────────────────────────
        # Any image xref that appears on MORE THAN ONE page is a template
        # element (header logo, footer watermark, background graphic) that
        # was placed via a shared form XObject.  page.get_images(full=True)
        # surfaces these on every page, but they are not unique content and
        # should not flood the translated output.  Remove them from all pages.
        from collections import Counter
        xref_counts: Counter = Counter(
            img.xref
            for pc in doc.pages
            for img in pc.image_blocks
            if img.xref  # xref=0 are inline images; handled separately
        )
        repeated_xrefs = {x for x, c in xref_counts.items() if c > 1}
        if repeated_xrefs:
            logger.info(
                f"Removing {len(repeated_xrefs)} repeated template image(s) "
                f"(xrefs: {repeated_xrefs})"
            )
            for pc in doc.pages:
                pc.image_blocks = [
                    img for img in pc.image_blocks
                    if img.xref not in repeated_xrefs
                ]

        return doc

    def _images_pymupdf(self, pdf, page, page_idx, pw, ph):
        import fitz
        from collections import defaultdict

        # 1. Build xref→on-page-bbox map from get_image_info().
        #    Since PyMuPDF ≥ 1.18.11 this also covers images inside form
        #    XObjects, so we get accurate on-page positions for all images.
        bbox_map: dict[int, BBox] = {}
        try:
            for info in page.get_image_info():
                xref = info.get("xref", 0)
                if xref and xref not in bbox_map:
                    b = info["bbox"]
                    bbox_map[xref] = BBox(b[0], ph - b[3], b[2], ph - b[1])
        except Exception:
            pass

        # 2. Collect all images, dedup by xref, and bucket by referencer.
        #    The `referencer` field (img_info[9]) is the xref of the form
        #    XObject that directly contains the image.  referencer=0 means
        #    the image is painted directly on the page content stream.
        #
        #    Images that share a non-zero referencer are sub-elements of the
        #    same composite figure (e.g. logos inside a "Figure 2" XObject).
        #    We render such groups as a single region pixmap instead of
        #    extracting each sub-image individually.
        Entry = tuple  # (xref, img_bytes, ext, bbox_or_None)
        by_referencer: dict[int, list[Entry]] = defaultdict(list)
        seen_xrefs: set[int] = set()

        for img_info in page.get_images(full=True):
            xref      = img_info[0]
            referencer = img_info[9]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                base = pdf.extract_image(xref)
                if not base:
                    continue
                img_bytes = base["image"]
                ext = base.get("ext", "png")
                if len(img_bytes) < 500:
                    continue
                if len(img_bytes) > 10_000_000:
                    img_bytes, ext = self._downscale_image(img_bytes)
                    if not img_bytes:
                        continue
                bbox = bbox_map.get(xref)          # None if not positioned
                by_referencer[referencer].append((xref, img_bytes, ext, bbox))
            except Exception as e:
                logger.debug(f"Image xref={xref} failed: {e}")

        # 3. Produce final ImageBlock list.
        images: list[ImageBlock] = []

        for referencer, entries in by_referencer.items():
            # Single image OR directly-placed images → extract individually.
            if referencer == 0 or len(entries) == 1:
                for xref, img_bytes, ext, bbox in entries:
                    if bbox is None:
                        idx = len(images)
                        y1 = ph * (0.9 - 0.15 * idx)
                        y0 = max(0.0, y1 - ph * 0.4)
                        bbox = BBox(pw * 0.1, y0, pw * 0.9, y1)
                    images.append(ImageBlock(
                        image_bytes=img_bytes, bbox=bbox,
                        extension=ext, xref=xref,
                    ))
                    logger.info(
                        f"Extracted image xref={xref} p{page_idx+1} "
                        f"({len(img_bytes)/1000:.0f}KB)"
                    )
                continue

            # Multiple images inside the same form XObject.
            # If we have on-page positions for at least 2 of them, render
            # the union region as a single composite pixmap.  Use the
            # referencer's xref as the ImageBlock xref so that the
            # cross-page dedup correctly removes header/footer composites
            # (same form XObject used on every page) while keeping unique
            # figures (used only on one page).
            positioned = [(xref, bbox) for xref, _, _, bbox in entries
                          if bbox is not None]

            if len(positioned) >= 2:
                all_bboxes = [bbox for _, bbox in positioned]
                pad = 4.0
                x0 = max(0.0, min(b.x0 for b in all_bboxes) - pad)
                y0 = max(0.0, min(b.y0 for b in all_bboxes) - pad)
                x1 = min(pw,  max(b.x1 for b in all_bboxes) + pad)
                y1 = min(ph,  max(b.y1 for b in all_bboxes) + pad)
                # PyMuPDF clip uses top-left origin
                clip = fitz.Rect(x0, ph - y1, x1, ph - y0)
                try:
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip)
                    comp_bytes = pixmap.tobytes("png")
                    if len(comp_bytes) > 10_000_000:
                        comp_bytes, _ = self._downscale_image(comp_bytes)
                    images.append(ImageBlock(
                        image_bytes=comp_bytes,
                        bbox=BBox(x0, y0, x1, y1),
                        extension="png",
                        xref=referencer,   # ← key: enables cross-page dedup
                    ))
                    logger.info(
                        f"Rendered {len(entries)}-image form XObject "
                        f"xref={referencer} as composite p{page_idx+1}"
                    )
                    continue
                except Exception as e:
                    logger.warning(
                        f"Composite render failed (referencer={referencer}): {e}"
                    )
                    # Fall through to individual extraction below

            # Fallback: no position info or composite render failed →
            # extract individually with unique vertical fallback slots.
            for xref, img_bytes, ext, bbox in entries:
                if bbox is None:
                    idx = len(images)
                    y1 = ph * (0.9 - 0.15 * idx)
                    y0 = max(0.0, y1 - ph * 0.4)
                    bbox = BBox(pw * 0.1, y0, pw * 0.9, y1)
                images.append(ImageBlock(
                    image_bytes=img_bytes, bbox=bbox,
                    extension=ext, xref=xref,
                ))

        return images

    # ═══════════════════════════════════════════════════════════
    # Backend 2: pypdfium2
    # ═══════════════════════════════════════════════════════════
    def _extract_with_pypdfium2(self, pdf_path, doc):
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        for page_idx in range(len(pdf)):
            try:
                page = pdf[page_idx]
                w, h = page.get_width(), page.get_height()
                pc = PageContent(page_number=page_idx+1, width=w, height=h)
                tp = page.get_textpage()
                try: full_text = tp.get_text_bounded()
                except TypeError: full_text = tp.get_text()
                n = tp.count_chars()
                if n > 0:
                    spans = self._spans_pypdfium2(tp, n, h, full_text)
                    blocks = self._spans_to_blocks(spans)
                    if self.merge_paragraphs:
                        blocks = self._merge_blocks(blocks)
                    pc.text_blocks = [b for b in blocks if len(b.full_text.strip()) >= self.min_text_length]
                tp.close()
                try: pc.image_blocks = self._images_pypdf(page_idx, w, h)
                except Exception as e: logger.warning(f"Image extraction failed p{page_idx+1}: {e}")
                page.close()
                doc.pages.append(pc)
            except Exception as e:
                logger.error(f"pypdfium2 page {page_idx+1} failed: {e}")
                page = pdf[page_idx]
                doc.pages.append(PageContent(page_number=page_idx+1, width=page.get_width(), height=page.get_height()))
        pdf.close()
        return doc

    def _spans_pypdfium2(self, tp, n, page_h, full_text=""):
        spans = []
        cur_text, cur_fs, cur_bbox = [], None, None
        for i in range(n):
            try:
                ch = full_text[i] if full_text and i < len(full_text) else None
                if not ch or ch == "\x00": continue
                try:
                    x0, y0, x1, y1 = tp.get_charbox(i)
                except:
                    if cur_text:
                        spans.append(TextSpan(text="".join(cur_text), font=FontInfo(size=cur_fs or 12.0), bbox=BBox(*cur_bbox) if cur_bbox else BBox(0,0,0,0)))
                        cur_text, cur_bbox = [], None
                    continue
                if ch in (" ","\t") and abs(y1-y0)<0.5:
                    if cur_text and cur_bbox is not None: cur_text.append(ch)
                    continue
                fs = abs(y1-y0) if abs(y1-y0)>0.5 else 12.0
                if cur_fs is not None and abs(fs-cur_fs)<1.0 and cur_bbox is not None and abs(y0-cur_bbox[1])<fs*0.5:
                    cur_text.append(ch); cur_bbox[2]=max(cur_bbox[2],x1); cur_bbox[3]=max(cur_bbox[3],y1); cur_bbox[1]=min(cur_bbox[1],y0)
                else:
                    if cur_text:
                        spans.append(TextSpan(text="".join(cur_text), font=FontInfo(size=cur_fs or 12.0), bbox=BBox(*cur_bbox) if cur_bbox else BBox(0,0,0,0)))
                    cur_text, cur_fs, cur_bbox = [ch], fs, [x0,y0,x1,y1]
            except: continue
        if cur_text:
            spans.append(TextSpan(text="".join(cur_text), font=FontInfo(size=cur_fs or 12.0), bbox=BBox(*cur_bbox) if cur_bbox else BBox(0,0,0,0)))
        return spans

    # ═══════════════════════════════════════════════════════════
    # Backend 3: pypdf (fallback)
    # ═══════════════════════════════════════════════════════════
    def _extract_with_pypdf(self, pdf_path, doc):
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        if reader.is_encrypted: raise ExtractionError("PDF encrypted")
        for pi, page in enumerate(reader.pages):
            mb = page.mediabox; w, h = float(mb.width), float(mb.height)
            text = page.extract_text() or ""
            pc = PageContent(page_number=pi+1, width=w, height=h)
            if text.strip():
                y = h - 50
                for para in text.split("\n\n"):
                    para = para.strip()
                    if not para: continue
                    bbox = BBox(50, y-20, w-50, y)
                    pc.text_blocks.append(TextBlock(spans=[TextSpan(text=para, font=FontInfo(size=12.0), bbox=bbox)], bbox=bbox))
                    y -= 30
            try: pc.image_blocks = self._images_pypdf(pi, w, h)
            except: pass
            doc.pages.append(pc)
        return doc

    # ═══════════════════════════════════════════════════════════
    # Shared: pypdf image extraction
    # ═══════════════════════════════════════════════════════════
    def _images_pypdf(self, page_idx, pw, ph):
        """
        Extract images using pypdf's page.images API, with actual
        positions parsed from the PDF content stream.
        """
        if not self._current_pdf_path:
            return []
        images = []
        try:
            from pypdf import PdfReader
            reader = PdfReader(self._current_pdf_path)
            if page_idx >= len(reader.pages):
                return []

            page = reader.pages[page_idx]

            # Parse content stream to get image positions
            img_positions = self._parse_image_positions(page, pw, ph)

            for img_obj in page.images:
                try:
                    img_data = img_obj.data
                    if len(img_data) > 10_000_000:
                        img_data, _ = self._downscale_image(img_data)
                        if not img_data:
                            continue
                    if len(img_data) < 500:
                        continue

                    # Look up actual position from content stream parse
                    img_name = "/" + img_obj.name.split(".")[0]  # "/Img1.png" → "/Img1"
                    if img_name in img_positions:
                        bbox = img_positions[img_name]
                    else:
                        # Try without leading slash
                        alt_name = img_obj.name.split(".")[0]
                        bbox = img_positions.get(
                            alt_name,
                            BBox(pw * 0.1, ph * 0.25, pw * 0.9, ph * 0.75)
                        )

                    images.append(ImageBlock(
                        image_bytes=img_data, bbox=bbox, extension="png",
                    ))
                    logger.info(
                        f"Extracted image {img_obj.name} p{page_idx+1} "
                        f"({len(img_data)/1000:.0f}KB) "
                        f"at y={bbox.y0:.0f}-{bbox.y1:.0f}"
                    )
                except Exception as e:
                    logger.warning(f"Image failed: {e}")
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"pypdf image extraction failed: {e}")
        return images

    def _parse_image_positions(self, page, pw, ph) -> dict[str, BBox]:
        """
        Parse the PDF page content stream to find actual image
        placement coordinates.

        Tracks the graphics state (q/Q save/restore, cm transforms)
        and records where each image is placed via the Do operator.

        Returns dict mapping image name (e.g. "/Img1") to BBox
        in bottom-left origin coordinates.
        """
        import re
        positions = {}

        try:
            contents = page["/Contents"]
            if hasattr(contents, '__iter__') and not hasattr(contents, 'get_data'):
                raw = b""
                for c in contents:
                    raw += c.get_object().get_data()
            else:
                raw = contents.get_object().get_data()
            raw_str = raw.decode('latin-1', errors='replace')
        except Exception:
            return positions

        # Track graphics state
        ctm_stack = []
        current_ctm = [1, 0, 0, 1, 0, 0]  # identity matrix [a,b,c,d,e,f]

        for line in raw_str.split('\n'):
            line = line.strip()
            if not line:
                continue

            # Save graphics state
            if line == 'q':
                ctm_stack.append(list(current_ctm))
                continue

            # Restore graphics state
            if line == 'Q':
                if ctm_stack:
                    current_ctm = ctm_stack.pop()
                continue

            # cm operator: concatenate transformation matrix
            cm_match = re.match(
                r'([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+'
                r'([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+cm',
                line,
            )
            if cm_match:
                try:
                    a, b, c, d, e, f = [float(x) for x in cm_match.groups()]
                    ca, cb, cc, cd, ce, cf = current_ctm
                    current_ctm = [
                        a*ca + b*cc,
                        a*cb + b*cd,
                        c*ca + d*cc,
                        c*cb + d*cd,
                        e*ca + f*cc + ce,
                        e*cb + f*cd + cf,
                    ]
                except ValueError:
                    pass
                continue

            # Do operator: paint XObject (image)
            do_match = re.match(r'(/\w+)\s+Do', line)
            if do_match:
                name = do_match.group(1)
                a, b, c, d, e, f = current_ctm
                # Image maps (0,0)-(1,1) → page coords via CTM
                # Bottom-left of image: (e, f)
                # Width = |a|, Height = |d| (for non-rotated images)
                x0 = e
                y0 = f
                img_w = abs(a) if abs(a) > 1 else abs(c)
                img_h = abs(d) if abs(d) > 1 else abs(b)
                x1 = x0 + img_w
                y1 = y0 + img_h

                # Coordinates are already in PDF bottom-left origin
                positions[name] = BBox(x0, y0, x1, y1)
                continue

        return positions

    # ═══════════════════════════════════════════════════════════
    # Shared utilities
    # ═══════════════════════════════════════════════════════════
    def _downscale_image(self, img_bytes, max_dim=1200):
        try:
            from PIL import Image as PILImage
            pil = PILImage.open(io.BytesIO(img_bytes))
            if pil.width > max_dim or pil.height > max_dim:
                ratio = min(max_dim/pil.width, max_dim/pil.height)
                pil = pil.resize((int(pil.width*ratio), int(pil.height*ratio)), PILImage.LANCZOS)
            buf = io.BytesIO()
            pil.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "png"
        except Exception as e:
            logger.warning(f"Downscale failed: {e}")
            return None, None

    def _spans_to_blocks(self, spans):
        if not spans: return []
        spans.sort(key=lambda s: (-s.bbox.y1, s.bbox.x0))
        blocks, cur = [], [spans[0]]
        for s in spans[1:]:
            if abs(s.bbox.y1 - cur[-1].bbox.y1) < cur[-1].font.size * 0.6:
                cur.append(s)
            else:
                blocks.append(self._make_block(cur)); cur = [s]
        if cur: blocks.append(self._make_block(cur))
        return blocks

    def _make_block(self, spans):
        return TextBlock(
            spans=spans,
            bbox=BBox(min(s.bbox.x0 for s in spans), min(s.bbox.y0 for s in spans),
                      max(s.bbox.x1 for s in spans), max(s.bbox.y1 for s in spans)))

    def _merge_blocks(self, blocks):
        if len(blocks) <= 1: return blocks
        merged = []
        for block in blocks:
            cur_h = block.bbox.y1 - block.bbox.y0
            if cur_h < 6.0 and merged:
                prev = merged[-1]; prev.spans.extend(block.spans)
                prev.bbox = BBox(min(prev.bbox.x0,block.bbox.x0),min(prev.bbox.y0,block.bbox.y0),max(prev.bbox.x1,block.bbox.x1),max(prev.bbox.y1,block.bbox.y1))
                continue
            if not merged: merged.append(block); continue
            prev = merged[-1]; ref_h = max(cur_h, 1.0)
            if abs(block.bbox.x0-prev.bbox.x0)<ref_h*3 and abs(block.bbox.y1-prev.bbox.y0)<ref_h*_PARA_GAP_FACTOR:
                prev.spans.extend(block.spans)
                prev.bbox = BBox(min(prev.bbox.x0,block.bbox.x0),min(prev.bbox.y0,block.bbox.y0),max(prev.bbox.x1,block.bbox.x1),max(prev.bbox.y1,block.bbox.y1))
            else: merged.append(block)
        return merged
