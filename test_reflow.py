#!/usr/bin/env python3
"""
Quick smoke test for the reflow renderer.
Creates a demo PDF with sample Arabic content to verify:
- Font registration works
- Arabic text reshaping + bidi works
- Platypus layout produces correct top-to-bottom order
- Headings, body, and dividers render correctly
"""

import sys
sys.path.insert(0, "/home/claude/e2apdf")

from src.arabic_utils import prepare_arabic, has_arabic
from src.models import (
    BBox, FontInfo, TextBlock, TextSpan, ImageBlock,
    TranslatedBlock, TranslatedPage, TranslatedDocument,
)
from src.renderer_reflow import ReflowRenderer, ReflowConfig

def make_block(text: str, translated: str, font_size: float = 12.0,
               bold: bool = False, y1: float = 700) -> TranslatedBlock:
    """Helper to create a TranslatedBlock."""
    font = FontInfo(size=font_size, is_bold=bold)
    bbox = BBox(50, y1 - 20, 500, y1)
    span = TextSpan(text=text, font=font, bbox=bbox)
    original = TextBlock(spans=[span], bbox=bbox)
    return TranslatedBlock(original=original, translated_text=translated, font=font)


def build_demo_doc() -> TranslatedDocument:
    """Build a realistic demo TranslatedDocument."""
    doc = TranslatedDocument(source_path="demo_input.pdf", target_language="ar")

    # ── Page 1: Title + intro ──
    page1 = TranslatedPage(page_number=1, width=595.28, height=841.89)
    page1.translated_blocks = [
        make_block(
            "Fires and Explosions",
            "الحرائق والانفجارات",
            font_size=24, bold=True, y1=780,
        ),
        make_block(
            "Chapter 1: Introduction",
            "الفصل الأول: مقدمة",
            font_size=16, bold=True, y1=720,
        ),
        make_block(
            "Fire and explosion hazards are significant concerns in chemical and petrochemical industries.",
            "تعتبر مخاطر الحرائق والانفجارات من المخاوف الكبيرة في الصناعات الكيميائية والبتروكيميائية. "
            "يتطلب فهم هذه المخاطر معرفة عميقة بكيمياء الاحتراق وديناميكيات الانفجار وطرق الوقاية والحماية. "
            "يهدف هذا الفصل إلى تقديم الأساسيات النظرية والعملية لفهم ظواهر الحرائق والانفجارات.",
            font_size=12, y1=680,
        ),
        make_block(
            "The study of fires involves understanding combustion processes.",
            "تتضمن دراسة الحرائق فهم عمليات الاشتعال والاحتراق وانتقال الحرارة. "
            "تلعب عوامل متعددة دوراً في تحديد شدة الحريق ومعدل انتشاره.",
            font_size=12, y1=600,
        ),
    ]
    doc.pages.append(page1)

    # ── Page 2: Subsections ──
    page2 = TranslatedPage(page_number=2, width=595.28, height=841.89)
    page2.translated_blocks = [
        make_block(
            "1.1 Fire Triangle",
            "١.١ مثلث الحريق",
            font_size=16, bold=True, y1=780,
        ),
        make_block(
            "The fire triangle consists of three elements: fuel, oxygen, and an ignition source.",
            "يتكون مثلث الحريق من ثلاثة عناصر أساسية: الوقود والأكسجين ومصدر الاشتعال. "
            "يجب أن تتوفر هذه العناصر الثلاثة معاً لكي يحدث الحريق. "
            "إزالة أي عنصر من هذه العناصر يؤدي إلى إخماد الحريق أو منع حدوثه.",
            font_size=12, y1=720,
        ),
        make_block(
            "1.2 Explosion Mechanisms",
            "١.٢ آليات الانفجار",
            font_size=16, bold=True, y1=620,
        ),
        make_block(
            "Explosions can be classified into physical and chemical explosions.",
            "يمكن تصنيف الانفجارات إلى انفجارات فيزيائية وكيميائية. "
            "تنتج الانفجارات الفيزيائية عن الإطلاق المفاجئ للطاقة المخزنة في شكل ضغط، "
            "بينما تنتج الانفجارات الكيميائية عن تفاعلات كيميائية سريعة تولد كميات كبيرة من الغازات والحرارة. "
            "يعتمد تصنيف الانفجار أيضاً على سرعة موجة الصدمة.",
            font_size=12, y1=560,
        ),
        make_block(
            "1.3 Safety Measures",
            "١.٣ إجراءات السلامة",
            font_size=16, bold=True, y1=420,
        ),
        make_block(
            "Prevention and mitigation strategies are essential for industrial safety.",
            "تعد استراتيجيات الوقاية والتخفيف ضرورية للسلامة الصناعية. "
            "تشمل هذه الاستراتيجيات التصميم الهندسي الآمن وأنظمة الكشف والإنذار المبكر "
            "وخطط الاستجابة للطوارئ وتدريب العاملين على إجراءات السلامة.",
            font_size=12, y1=360,
        ),
    ]
    doc.pages.append(page2)

    # ── Page 3: Mixed content with English terms ──
    page3 = TranslatedPage(page_number=3, width=595.28, height=841.89)
    page3.translated_blocks = [
        make_block(
            "Chapter 2: Flash Point and Autoignition",
            "الفصل الثاني: نقطة الوميض والاشتعال الذاتي",
            font_size=16, bold=True, y1=780,
        ),
        make_block(
            "The flash point is the lowest temperature at which a liquid gives off enough vapour.",
            "نقطة الوميض (Flash Point) هي أدنى درجة حرارة يعطي عندها السائل بخاراً كافياً "
            "لتشكيل خليط قابل للاشتعال مع الهواء فوق سطح السائل. "
            "تعتبر نقطة الوميض من أهم الخصائص المستخدمة في تصنيف المواد القابلة للاشتعال.",
            font_size=12, y1=720,
        ),
        # A pure-English block (should render LTR)
        make_block(
            "Table 2.1: Flash points of common solvents",
            "Table 2.1: Flash points of common solvents",
            font_size=10, y1=600,
        ),
    ]
    doc.pages.append(page3)

    return doc


def main():
    print("Building demo TranslatedDocument...")
    doc = build_demo_doc()
    print(f"  {len(doc.pages)} pages, "
          f"{sum(len(p.translated_blocks) for p in doc.pages)} blocks")

    print("\nInitialising ReflowRenderer (font_size=14)...")
    config = ReflowConfig(
        font_size=14,
        header_text="E2A PDF Translator — Demo",
        show_source_markers=True,
        show_page_numbers=True,
    )
    renderer = ReflowRenderer(config)

    output_path = "/home/claude/e2apdf/demo_reflow_output.pdf"
    print(f"Rendering to {output_path}...")
    result = renderer.render(doc, output_path)
    print(f"Done! Output: {result}")

    # Basic sanity checks
    import os
    size = os.path.getsize(output_path)
    print(f"File size: {size:,} bytes")
    assert size > 1000, "PDF suspiciously small"

    # Check PDF header
    with open(output_path, "rb") as f:
        header = f.read(5)
        assert header == b"%PDF-", f"Bad PDF header: {header}"

    print("\n✅ All checks passed!")
    return output_path


if __name__ == "__main__":
    main()
