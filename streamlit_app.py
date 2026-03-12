"""
E2A PDF Translator — Streamlit App

Translate English PDF files to Arabic with full RTL support.
Supports mock (testing), Google Translate, DeepL, OpenAI, and Anthropic backends.
"""

import io
import os
import subprocess
import sys
import tempfile
import logging
import urllib.request
import zipfile
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Ensure an Arabic-capable font is available at startup (cached once per run)
# ---------------------------------------------------------------------------

_FONTS_DIR = Path(__file__).parent / "fonts"
_AMIRI_URL = (
    "https://github.com/aliftype/amiri/releases/download/1.000/Amiri-1.000.zip"
)


def _ensure_arabic_font() -> str | None:
    """
    Return a path to an Arabic-capable .ttf file, downloading one if needed.
    Returns None if auto-detection from system paths will suffice.
    """
    from src.renderer import FontConfig, RenderError

    # 1. Try the fonts that ship with the OS
    try:
        FontConfig().auto_detect()
        return None
    except RenderError:
        pass

    # 2. Try apt-get
    try:
        subprocess.run(
            ["apt-get", "install", "-y", "fonts-freefont-ttf"],
            capture_output=True, timeout=60,
        )
        FontConfig().auto_detect()
        return None
    except Exception:
        pass

    # 3. Last resort: download Amiri from GitHub releases.
    _FONTS_DIR.mkdir(exist_ok=True)
    amiri_path = _FONTS_DIR / "Amiri-Regular.ttf"
    if not amiri_path.exists():
        try:
            zip_data = urllib.request.urlopen(_AMIRI_URL, timeout=30).read()
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                for name in zf.namelist():
                    if name.endswith("Amiri-Regular.ttf"):
                        amiri_path.write_bytes(zf.read(name))
                        break
        except Exception:
            return None

    return str(amiri_path) if amiri_path.exists() else None


_ARABIC_FONT_PATH: str | None = _ensure_arabic_font()


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="E2A PDF Translator",
    page_icon="📄",
    layout="centered",
)

# ---------------------------------------------------------------------------
# Title & description
# ---------------------------------------------------------------------------
st.title("📄 E2A PDF Translator")
st.markdown(
    "Translate **English PDF** files into **Arabic** with full RTL layout support, "
    "correct letter shaping, and Bidi reordering."
)
st.divider()

# ---------------------------------------------------------------------------
# Sidebar — settings
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")

    backend = st.selectbox(
        "Translation backend",
        options=["free", "mock", "google", "deepl", "llm-openai", "llm-anthropic"],
        format_func=lambda x: {
            "free": "Free (Google Translate — no API key needed)",
            "mock": "Mock (pipeline test only — not a real translation)",
            "google": "Google Cloud Translation",
            "deepl": "DeepL",
            "llm-openai": "OpenAI GPT",
            "llm-anthropic": "Anthropic Claude",
        }[x],
        help="Choose the translation service. 'Free' uses Google Translate with no API key.",
    )

    api_key = ""
    model_name = ""

    _ENV_VAR_HINTS = {
        "google": "GOOGLE_TRANSLATE_API_KEY",
        "deepl": "DEEPL_API_KEY",
        "llm-openai": "OPENAI_API_KEY",
        "llm-anthropic": "ANTHROPIC_API_KEY",
    }

    if backend not in ("mock", "free"):
        env_hint = _ENV_VAR_HINTS.get(backend, "API_KEY")
        default_key = (
            st.secrets.get(env_hint, None)
            or st.secrets.get(env_hint.lower(), None)
            or os.environ.get(env_hint, "")
        )
        api_key = st.text_input(
            "API Key",
            value=default_key,
            type="password",
            help=(
                f"Your API key. Set `{env_hint}` in Streamlit secrets "
                f"or as an environment variable to avoid typing it here."
            ),
        )

    if backend in ("llm-openai", "llm-anthropic"):
        model_name = st.text_input(
            "Model name (optional)",
            value="",
            help="Override the default model, e.g. `claude-sonnet-4-6` or `gpt-4o`.",
        )

    # ── Rendering mode ──
    st.subheader("Rendering mode")

    render_mode = st.radio(
        "Output layout",
        options=["reflow", "positioned"],
        format_func=lambda x: {
            "reflow": "♻️ Reflow (clean A4, recommended)",
            "positioned": "📐 Positioned (preserve original layout)",
        }[x],
        help=(
            "**Reflow** produces clean A4 pages with consistent formatting — "
            "recommended for readable output.\n\n"
            "**Positioned** tries to match the original PDF layout "
            "(may have coordinate issues with some documents)."
        ),
    )

    # ── Reflow-specific settings ──
    if render_mode == "reflow":
        st.subheader("Reflow options")

        reflow_font_size = st.slider(
            "Font size (pt)", min_value=10, max_value=20, value=14, step=1,
            help="Body text size. Headings and titles scale proportionally.",
        )
        reflow_line_spacing = st.slider(
            "Line spacing", min_value=1.2, max_value=2.2, value=1.6, step=0.1,
        )
        reflow_show_markers = st.checkbox(
            "Show source page markers", value=True,
            help="Add dividers showing where each source page starts.",
        )
        reflow_header = st.text_input(
            "Running header (optional)", value="",
            help="Text shown at the top of every page.",
        )

    # ── Positioned-mode settings (original) ──
    if render_mode == "positioned":
        st.subheader("Layout options")

        mirror_layout = st.checkbox("Mirror layout for RTL", value=True)
        preserve_positions = st.checkbox("Preserve original positions", value=True)
        line_spacing = st.slider(
            "Line spacing", min_value=1.0, max_value=2.5, value=1.4, step=0.1
        )
        margin = st.slider(
            "Page margin (pts)", min_value=20, max_value=100, value=50, step=5
        )

    add_page_numbers = st.checkbox("Add page numbers", value=True)

    st.subheader("Cache")
    use_cache = st.checkbox(
        "Enable translation cache",
        value=True,
        help="Cache translations to avoid re-calling the API for identical text.",
    )

# ---------------------------------------------------------------------------
# Active-backend indicator
# ---------------------------------------------------------------------------
_BACKEND_LABELS = {
    "free": ("Free — Google Translate, no API key", "🆓"),
    "mock": ("Mock — pipeline test only, not a real translation", "⚙️"),
    "google": ("Google Cloud Translation", "🔵"),
    "deepl": ("DeepL", "🟢"),
    "llm-openai": ("OpenAI GPT", "🟣"),
    "llm-anthropic": ("Anthropic Claude", "🟠"),
}
_label, _icon = _BACKEND_LABELS.get(backend, (backend, "🔧"))

if backend in ("mock", "free"):
    _note = " — produces placeholder text, not real Arabic." if backend == "mock" else " · real Arabic translation via Google Translate, no API key required."
    st.info(f"{_icon} **Active backend:** {_label}{_note}", icon="ℹ️")
else:
    _key_source = ""
    _env_hint = _ENV_VAR_HINTS.get(backend, "API_KEY")
    if st.secrets.get(_env_hint) or st.secrets.get(_env_hint.lower()):
        _key_source = " · API key loaded from Streamlit secrets"
    elif os.environ.get(_env_hint):
        _key_source = " · API key loaded from environment"
    elif api_key.strip():
        _key_source = " · API key entered manually"
    else:
        _key_source = " · ⚠️ no API key provided"
    st.info(f"{_icon} **Active backend:** {_label}{_key_source}", icon="ℹ️")

# Render mode indicator
_mode_label = "♻️ Reflow (A4)" if render_mode == "reflow" else "📐 Positioned"
if render_mode == "reflow":
    st.caption(f"Render: {_mode_label} · {reflow_font_size}pt · {reflow_line_spacing}× spacing")
else:
    st.caption(f"Render: {_mode_label}")

# ---------------------------------------------------------------------------
# Main area — file upload
# ---------------------------------------------------------------------------
uploaded_file = st.file_uploader(
    "Upload an English PDF",
    type=["pdf"],
    help="Upload the PDF you want translated to Arabic.",
)

if uploaded_file is not None:
    st.success(f"File loaded: **{uploaded_file.name}** ({uploaded_file.size:,} bytes)")

    if st.button("Translate to Arabic", type="primary", use_container_width=True):
        if backend not in ("mock", "free") and not api_key.strip():
            st.error(
                f"Please enter your API key for the **{backend}** backend "
                "(or set the corresponding environment variable)."
            )
            st.stop()

        # ------------------------------------------------------------------
        # Run the pipeline
        # ------------------------------------------------------------------
        progress = st.progress(0, text="Starting translation pipeline…")
        status_area = st.empty()
        log_expander = st.expander("Pipeline log", expanded=False)
        log_box = log_expander.empty()
        log_lines: list[str] = []

        class StreamlitLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord):
                msg = self.format(record)
                log_lines.append(msg)
                log_box.code("\n".join(log_lines[-40:]), language=None)

        root_logger = logging.getLogger()
        ui_handler = StreamlitLogHandler()
        ui_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        root_logger.addHandler(ui_handler)
        root_logger.setLevel(logging.INFO)

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                input_path = Path(tmpdir) / uploaded_file.name
                output_path = Path(tmpdir) / (Path(uploaded_file.name).stem + "_ar.pdf")
                cache_path = (
                    str(Path(tmpdir) / "translations.json") if use_cache else None
                )

                input_path.write_bytes(uploaded_file.getvalue())

                progress.progress(10, text="Importing translation engine…")
                status_area.info("Importing modules…")

                try:
                    from src.pipeline import E2APipeline, PipelineConfig
                except ImportError as exc:
                    st.error(
                        f"Failed to import the translation engine: {exc}\n\n"
                        "Make sure all dependencies are installed:\n"
                        "```\npip install pypdfium2 pypdf reportlab arabic-reshaper python-bidi requests\n```"
                    )
                    st.stop()

                progress.progress(20, text="Configuring pipeline…")
                status_area.info("Configuring pipeline…")

                # Build config based on render mode
                config_kwargs = dict(
                    translation_backend=backend,
                    api_key=api_key.strip() or None,
                    model=model_name.strip() or None,
                    font_path=_ARABIC_FONT_PATH,
                    cache_path=cache_path,
                    render_mode=render_mode,
                    add_page_numbers=add_page_numbers,
                    verbose=False,
                )

                if render_mode == "reflow":
                    config_kwargs.update(
                        reflow_font_size=float(reflow_font_size),
                        reflow_line_spacing=reflow_line_spacing,
                        reflow_show_source_markers=reflow_show_markers,
                        reflow_header_text=reflow_header.strip(),
                    )
                else:
                    config_kwargs.update(
                        mirror_layout=mirror_layout,
                        preserve_positions=preserve_positions,
                        line_spacing=line_spacing,
                        margin=float(margin),
                    )

                config = PipelineConfig(**config_kwargs)

                progress.progress(30, text="Extracting text from PDF…")
                status_area.info("Step 1/3 — Extracting content…")

                pipeline = E2APipeline(config)

                progress.progress(40, text="Translating content…")
                status_area.info("Step 2/3 — Translating…  (this may take a while)")

                report = pipeline.translate(str(input_path), str(output_path))

                progress.progress(90, text="Finalising output PDF…")
                status_area.info("Step 3/3 — Rendering Arabic PDF…")

                # ----------------------------------------------------------
                # Show report
                # ----------------------------------------------------------
                progress.progress(100, text="Done!")
                status_area.empty()

                if report.success:
                    st.success(f"Translation complete! (render mode: {report.render_mode})")

                    if report.translated_blocks == 0 and report.total_blocks == 0:
                        st.warning(
                            "No text was extracted from the PDF. "
                            "The file may be scanned/image-based (OCR not supported), "
                            "encrypted, or have no selectable text. "
                            "Check the pipeline log and warnings below for details."
                        )

                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Pages", f"{report.translated_pages}/{report.total_pages}")
                    col2.metric("Translated blocks", report.translated_blocks)
                    col3.metric("From cache", report.cached_blocks)
                    col4.metric("Skipped", report.skipped_blocks)

                    st.metric("Duration", f"{report.duration_seconds:.1f} s")

                    translated_bytes = output_path.read_bytes()
                    st.download_button(
                        label="Download Arabic PDF",
                        data=translated_bytes,
                        file_name=output_path.name,
                        mime="application/pdf",
                        type="primary",
                        use_container_width=True,
                    )
                else:
                    st.error("Translation failed.")

                if report.warnings:
                    with st.expander(f"Warnings ({len(report.warnings)})", expanded=False):
                        for w in report.warnings:
                            st.warning(w)

                if report.errors:
                    with st.expander(f"Errors ({len(report.errors)})", expanded=True):
                        for e in report.errors:
                            st.error(e)

                with st.expander("Full pipeline report", expanded=False):
                    st.code(report.summary(), language=None)

        except Exception as exc:
            progress.empty()
            status_area.empty()
            st.error(f"Unexpected error: {exc}")
            st.exception(exc)
        finally:
            root_logger.removeHandler(ui_handler)

else:
    st.info("Upload a PDF file above to get started.")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.divider()
st.caption(
    "Backends: **free** (Google Translate, no key) · **Google Cloud** · **DeepL** · **OpenAI** · **Anthropic**  |  "
    "Modes: **reflow** (clean A4) · **positioned** (preserve layout)  |  "
    "Source: [abdoljh/e2apdf](https://github.com/abdoljh/e2apdf)"
)
