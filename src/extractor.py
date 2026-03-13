"""
PDF content extractor. Uses pypdfium2 as primary, pypdf as fallback.
"""
from __future__ import annotations
import io, logging, struct
from pathlib import Path
from typing import Optional
from .models import BBox, BlockType, DocumentContent, FontInfo, ImageBlock, PageContent, TextBlock, TextSpan

logger = logging.getLogger(__name__)
_PARA_GAP_FACTOR = 0.6
_MIN_TEXT_LENGTH = 1

class ExtractionError(Exception):
    pass

class PDFExtractor:
    def __init__(self, merge_paragraphs=True, min_text_length=_MIN_TEXT_LENGTH):
        self.merge_paragraphs = merge_paragraphs
        self.min_text_length = min_text_length

    def extract(self, pdf_path):
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise ExtractionError(f"File not found: {pdf_path}")
        doc = DocumentContent(source_path=str(pdf_path))
        try:
            doc = self._extract_with_pypdfium2(pdf_path, doc)
        except Exception as e:
            logger.warning(f"pypdfium2 failed: {e}. Trying pypdf.")
            try:
                doc = self._extract_with_pypdf(pdf_path, doc)
            except Exception as e2:
                raise ExtractionError(f"All backends failed. pypdfium2: {e}, pypdf: {e2}")
        total_text = sum(len(b.full_text) for p in doc.pages for b in p.text_blocks)
        if total_text < 50 and len(doc.pages) > 0:
            doc.is_scanned = True
            doc.extraction_warnings.append("PDF appears scanned.")
        return doc

    def _extract_with_pypdfium2(self, pdf_path, doc):
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        self._current_pdf_path = str(pdf_path)  # Store for image extraction
        for page_idx in range(len(pdf)):
            try:
                page_content = self._extract_page_pypdfium2(pdf, page_idx)
                doc.pages.append(page_content)
            except Exception as e:
                logger.error(f"Page {page_idx+1} failed: {e}")
                page = pdf[page_idx]
                doc.pages.append(PageContent(page_number=page_idx+1, width=page.get_width(), height=page.get_height()))
        pdf.close()
        return doc

    def _extract_page_pypdfium2(self, pdf, page_idx):
        import pypdfium2 as pdfium
        page = pdf[page_idx]
        w, h = page.get_width(), page.get_height()
        pc = PageContent(page_number=page_idx+1, width=w, height=h)
        tp = page.get_textpage()
        try:
            full_text = tp.get_text_bounded()
        except TypeError:
            full_text = tp.get_text()
        n = tp.count_chars()
        if n > 0:
            spans = self._build_spans(tp, n, h, full_text)
            blocks = self._spans_to_blocks(spans)
            if self.merge_paragraphs:
                blocks = self._merge_paragraphs_fn(blocks)
            pc.text_blocks = [b for b in blocks if len(b.full_text.strip()) >= self.min_text_length]
        tp.close()
        # Extract images
        try:
            pc.image_blocks = self._extract_images(pdf, page, page_idx, w, h)
        except Exception as e:
            logger.warning(f"Image extraction failed page {page_idx+1}: {e}")
        page.close()
        return pc

    def _build_spans(self, tp, n, page_h, full_text=""):
        spans = []
        cur_text, cur_fs, cur_bbox = [], None, None
        for i in range(n):
            try:
                ch = full_text[i] if full_text and i < len(full_text) else None
                if not ch or ch == "\x00": continue
                try:
                    rect = tp.get_charbox(i)
                    x0, y0, x1, y1 = rect
                except: 
                    if cur_text:
                        spans.append(TextSpan(text="".join(cur_text), font=FontInfo(size=cur_fs or 12.0), bbox=BBox(*cur_bbox) if cur_bbox else BBox(0,0,0,0)))
                        cur_text, cur_bbox = [], None
                    continue
                if ch in (" ", "\t") and abs(y1-y0) < 0.5:
                    if cur_text and cur_bbox is not None: cur_text.append(ch)
                    continue
                fs = abs(y1-y0) if abs(y1-y0) > 0.5 else 12.0
                if cur_fs is not None and abs(fs-cur_fs) < 1.0 and cur_bbox is not None and abs(y0-cur_bbox[1]) < fs*0.5:
                    cur_text.append(ch)
                    cur_bbox[2] = max(cur_bbox[2], x1)
                    cur_bbox[3] = max(cur_bbox[3], y1)
                    cur_bbox[1] = min(cur_bbox[1], y0)
                else:
                    if cur_text:
                        spans.append(TextSpan(text="".join(cur_text), font=FontInfo(size=cur_fs or 12.0), bbox=BBox(*cur_bbox) if cur_bbox else BBox(0,0,0,0)))
                    cur_text, cur_fs, cur_bbox = [ch], fs, [x0, y0, x1, y1]
            except: continue
        if cur_text:
            spans.append(TextSpan(text="".join(cur_text), font=FontInfo(size=cur_fs or 12.0), bbox=BBox(*cur_bbox) if cur_bbox else BBox(0,0,0,0)))
        return spans

    def _extract_images(self, pdf, page, page_idx, pw, ph):
        """
        Extract images from a PDF page.

        Uses pypdf's page.images API which reliably handles all image
        types (JPEG, PNG, CCITT, etc.) across PDF versions.  Falls back
        to pypdfium2 object enumeration if pypdf is unavailable.

        Note: Very large images (>10MB) are skipped to avoid memory
        issues and excessive output PDF size.
        """
        images = []
        pdf_path = None

        # Get the source PDF path for pypdf
        # pypdfium2 pdf object stores path internally
        try:
            # The DocumentContent stores the path
            if hasattr(pdf, '_orig_input'):
                pdf_path = str(pdf._orig_input)
        except:
            pass

        # Strategy 1: Use pypdf (most reliable for image extraction)
        try:
            from pypdf import PdfReader
            # We need the file path — get it from the pipeline context
            # Try to find the path from the pdfium document
            if pdf_path is None:
                # pypdfium2 PdfDocument doesn't expose path easily;
                # we'll store it during extraction and pass through
                pdf_path = getattr(self, '_current_pdf_path', None)

            if pdf_path:
                reader = PdfReader(str(pdf_path))
                if page_idx < len(reader.pages):
                    pypdf_page = reader.pages[page_idx]
                    try:
                        page_images = pypdf_page.images
                    except Exception:
                        page_images = []

                    for img_obj in page_images:
                        try:
                            img_data = img_obj.data
                            # Skip very large images (>10MB raw)
                            if len(img_data) > 10_000_000:
                                logger.info(
                                    f"Skipping large image {img_obj.name} "
                                    f"({len(img_data)/1_000_000:.1f}MB) on page {page_idx+1}"
                                )
                                # Still include but at reduced size
                                try:
                                    from PIL import Image as PILImage
                                    pil = PILImage.open(io.BytesIO(img_data))
                                    # Resize to max 1200px wide
                                    max_dim = 1200
                                    if pil.width > max_dim or pil.height > max_dim:
                                        ratio = min(max_dim/pil.width, max_dim/pil.height)
                                        new_size = (int(pil.width*ratio), int(pil.height*ratio))
                                        pil = pil.resize(new_size, PILImage.LANCZOS)
                                    buf = io.BytesIO()
                                    pil.save(buf, format="PNG", optimize=True)
                                    img_data = buf.getvalue()
                                except Exception as resize_err:
                                    logger.warning(f"Image resize failed: {resize_err}")
                                    continue

                            # Determine bbox from PDF page placement
                            # pypdf doesn't give us placement info directly,
                            # so use a centered default bbox
                            bbox = BBox(
                                pw * 0.1, ph * 0.25,
                                pw * 0.9, ph * 0.75,
                            )

                            images.append(ImageBlock(
                                image_bytes=img_data,
                                bbox=bbox,
                                extension="png",
                            ))
                            logger.info(
                                f"Extracted image {img_obj.name} from page {page_idx+1} "
                                f"({len(img_data)/1000:.0f}KB)"
                            )
                        except Exception as e:
                            logger.warning(f"Image extraction failed for {img_obj.name}: {e}")

        except ImportError:
            logger.warning("pypdf not available for image extraction")
        except Exception as e:
            logger.warning(f"pypdf image extraction failed on page {page_idx+1}: {e}")

        # Strategy 2: Fallback to pypdfium2 object enumeration
        if not images:
            try:
                for oi in range(page.count_objs()):
                    try:
                        obj = page.get_obj(oi)
                        if obj.type == 2:
                            try:
                                m = obj.get_matrix()
                                x, y, w, h = m.e, m.f, m.a, m.d
                                bbox = BBox(x, y, x+w, y+h)
                            except:
                                bbox = BBox(0, 0, pw, ph)
                            try:
                                bm = obj.get_bitmap()
                                pil = bm.to_pil()
                                buf = io.BytesIO()
                                pil.save(buf, format="PNG")
                                images.append(ImageBlock(
                                    image_bytes=buf.getvalue(),
                                    bbox=bbox, extension="png",
                                ))
                            except:
                                pass
                    except:
                        continue
            except:
                pass  # pypdfium2 5.x may not support count_objs

        return images

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
                    pc.text_blocks.append(TextBlock(spans=[TextSpan(text=para, font=FontInfo(size=12.0), bbox=BBox(50, y-20, w-50, y))], bbox=BBox(50, y-20, w-50, y)))
                    y -= 30
            doc.pages.append(pc)
        return doc

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
        x0 = min(s.bbox.x0 for s in spans); y0 = min(s.bbox.y0 for s in spans)
        x1 = max(s.bbox.x1 for s in spans); y1 = max(s.bbox.y1 for s in spans)
        return TextBlock(spans=spans, bbox=BBox(x0, y0, x1, y1))

    def _merge_paragraphs_fn(self, blocks):
        if len(blocks) <= 1: return blocks
        merged = []
        for block in blocks:
            cur_h = block.bbox.y1 - block.bbox.y0
            if cur_h < 6.0 and merged:
                prev = merged[-1]; prev.spans.extend(block.spans)
                prev.bbox = BBox(min(prev.bbox.x0, block.bbox.x0), min(prev.bbox.y0, block.bbox.y0), max(prev.bbox.x1, block.bbox.x1), max(prev.bbox.y1, block.bbox.y1))
                continue
            if not merged: merged.append(block); continue
            prev = merged[-1]
            ref_h = max(cur_h, 1.0)
            vgap = abs(block.bbox.y1 - prev.bbox.y0)
            x_ok = abs(block.bbox.x0 - prev.bbox.x0) < ref_h * 3
            gap_ok = vgap < ref_h * _PARA_GAP_FACTOR
            if x_ok and gap_ok:
                prev.spans.extend(block.spans)
                prev.bbox = BBox(min(prev.bbox.x0, block.bbox.x0), min(prev.bbox.y0, block.bbox.y0), max(prev.bbox.x1, block.bbox.x1), max(prev.bbox.y1, block.bbox.y1))
            else:
                merged.append(block)
        return merged
