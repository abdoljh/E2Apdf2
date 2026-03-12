#!/usr/bin/env python3
"""
E2A PDF Translator — Command Line Interface.

Usage:
    python -m e2apdf input.pdf [-o output.pdf] [--backend mock|google|deepl|llm-openai]

Examples:
    # Quick test with mock translator
    python -m e2apdf document.pdf

    # Production with Google Translate
    python -m e2apdf document.pdf --backend google --api-key YOUR_KEY

    # LLM-based translation
    python -m e2apdf document.pdf --backend llm-openai --api-key YOUR_KEY
"""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="e2apdf",
        description="Translate English PDF files to Arabic with RTL support.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input.pdf                           # Mock translator (testing)
  %(prog)s input.pdf -o output_ar.pdf          # Specify output path
  %(prog)s input.pdf --backend google           # Google Cloud Translation
  %(prog)s input.pdf --backend deepl            # DeepL
  %(prog)s input.pdf --backend llm-openai       # OpenAI GPT
  %(prog)s input.pdf --backend llm-anthropic    # Anthropic Claude
        """,
    )

    parser.add_argument(
        "input",
        type=str,
        help="Path to the input English PDF file.",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Output path for translated PDF. Default: <input>_ar.pdf",
    )
    parser.add_argument(
        "--backend",
        type=str,
        default="mock",
        choices=["mock", "google", "deepl", "llm-openai", "llm-anthropic"],
        help="Translation backend (default: mock).",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key for translation service. Can also use env vars: "
             "GOOGLE_TRANSLATE_API_KEY, DEEPL_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model name for LLM backends (default: auto-selected).",
    )
    parser.add_argument(
        "--font",
        type=str,
        default=None,
        help="Path to Arabic TrueType font file.",
    )
    parser.add_argument(
        "--no-mirror",
        action="store_true",
        help="Don't mirror x-positions for RTL layout.",
    )
    parser.add_argument(
        "--no-page-numbers",
        action="store_true",
        help="Don't add Arabic page numbers.",
    )
    parser.add_argument(
        "--flowing",
        action="store_true",
        help="Use flowing text layout instead of preserving positions.",
    )
    parser.add_argument(
        "--line-spacing",
        type=float,
        default=1.4,
        help="Line spacing multiplier (default: 1.4).",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=50.0,
        help="Page margins in points (default: 50).",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=".e2a_cache",
        help="Directory for translation cache (default: .e2a_cache).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable translation caching.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging.",
    )

    args = parser.parse_args()

    # Validate input
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    if not input_path.suffix.lower() == ".pdf":
        print(f"Warning: Input file may not be a PDF: {input_path}", file=sys.stderr)

    # Build config
    from .pipeline import PipelineConfig, E2APipeline

    config = PipelineConfig(
        translation_backend=args.backend,
        api_key=args.api_key,
        model=args.model,
        font_path=args.font,
        mirror_layout=not args.no_mirror,
        preserve_positions=not args.flowing,
        add_page_numbers=not args.no_page_numbers,
        line_spacing=args.line_spacing,
        margin=args.margin,
        cache_path=(
            f"{args.cache_dir}/translations.json" if not args.no_cache else None
        ),
        verbose=args.verbose,
    )

    # Run pipeline
    pipeline = E2APipeline(config)
    report = pipeline.translate(args.input, args.output)

    # Print report
    print(report.summary())

    if not report.success:
        sys.exit(1)


if __name__ == "__main__":
    main()
