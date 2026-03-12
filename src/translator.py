"""
Translation layer for E2A PDF translator.

Supports multiple backends:
- Google Cloud Translation API (recommended for production)
- DeepL API
- OpenAI / Anthropic for LLM-based translation
- Mock translator for testing

Features:
- Batch translation to minimize API calls
- Translation caching (avoids re-translating identical text)
- Skip detection (numbers, formulas, short non-translatable content)
- Rate limiting with exponential backoff
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .arabic_utils import has_arabic
from .models import (
    DocumentContent,
    FontInfo,
    TextBlock,
    TranslatedBlock,
    TranslatedDocument,
    TranslatedPage,
)

logger = logging.getLogger(__name__)

# Delimiter for batching multiple texts in one API call
_BATCH_DELIM = "\n|||E2A_SPLIT|||\n"

# Max chars per API call (most APIs have limits around 5000-10000)
_MAX_BATCH_CHARS = 4000


class TranslationError(Exception):
    """Raised when translation fails."""
    pass


# ===========================================================================
# Translation Cache
# ===========================================================================

class TranslationCache:
    """
    Simple persistent cache for translations.
    Keyed by SHA-256 hash of source text + target language.
    """

    def __init__(self, cache_path: Optional[str | Path] = None):
        self._cache: dict[str, str] = {}
        self._cache_path = Path(cache_path) if cache_path else None
        self._hits = 0
        self._misses = 0

        if self._cache_path and self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
                logger.info(f"Loaded {len(self._cache)} cached translations.")
            except Exception as e:
                logger.warning(f"Cache load failed: {e}. Starting fresh.")

    def _key(self, text: str, target_lang: str = "ar") -> str:
        h = hashlib.sha256(f"{target_lang}:{text}".encode()).hexdigest()
        return h[:16]  # 16 hex chars = 64 bits, plenty for dedup

    def get(self, text: str, target_lang: str = "ar") -> Optional[str]:
        key = self._key(text, target_lang)
        result = self._cache.get(key)
        if result is not None:
            self._hits += 1
        else:
            self._misses += 1
        return result

    def put(self, text: str, translation: str, target_lang: str = "ar"):
        key = self._key(text, target_lang)
        self._cache[key] = translation

    def save(self):
        if self._cache_path:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                f"Saved {len(self._cache)} translations. "
                f"Hits: {self._hits}, Misses: {self._misses}"
            )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "cache_size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
        }


# ===========================================================================
# Skip Detection
# ===========================================================================

def should_skip_translation(text: str) -> bool:
    """
    Determine if text should be skipped (not translated).
    Returns True for: empty text, pure numbers, single chars,
    already-Arabic text, formulas, URLs, emails.
    """
    text = text.strip()
    if not text:
        return True
    if len(text) <= 1:
        return True
    if has_arabic(text):
        return True  # Already Arabic
    if re.match(r'^[\d\s.,;:/%$€£¥+\-*=<>()[\]{}]+$', text):
        return True  # Pure numbers/symbols
    if re.match(r'^https?://', text):
        return True  # URL
    if re.match(r'^[\w.+-]+@[\w-]+\.[\w.-]+$', text):
        return True  # Email
    if re.match(r'^[A-Z][.\s]*$', text):
        return True  # Single letter label
    return False


# ===========================================================================
# Backend Interface
# ===========================================================================

class TranslationBackend(ABC):
    """Abstract base for translation backends."""

    @abstractmethod
    def translate_texts(
        self, texts: list[str], source_lang: str = "en", target_lang: str = "ar"
    ) -> list[str]:
        """Translate a list of texts. Returns list of same length."""
        ...

    @abstractmethod
    def name(self) -> str:
        ...


# ===========================================================================
# Mock Backend (for testing and offline use)
# ===========================================================================

class MockTranslationBackend(TranslationBackend):
    """
    Mock translator for pipeline testing (no API key needed).
    Returns pure Arabic placeholder text so the rendering path can be
    verified without mixing Arabic/Latin glyphs (which confuses BiDi).
    """

    def name(self) -> str:
        return "mock"

    def translate_texts(
        self, texts: list[str], source_lang: str = "en", target_lang: str = "ar"
    ) -> list[str]:
        # Pure Arabic placeholder — no embedded Latin so BiDi stays clean.
        # "ترجمة تجريبية" = "test translation"
        return [f"[ترجمة تجريبية] {i + 1}" for i, _ in enumerate(texts)]


# ===========================================================================
# Free Backend — unofficial Google Translate (gtx client, no API key)
# ===========================================================================

class FreeTranslationBackend(TranslationBackend):
    """
    Free translation using the same unofficial Google Translate endpoint
    that browser extensions and tools like googletrans use (client=gtx).
    No API key required. Suitable for moderate document volumes.
    """

    def name(self) -> str:
        return "free"

    def translate_texts(
        self, texts: list[str], source_lang: str = "en", target_lang: str = "ar"
    ) -> list[str]:
        try:
            import requests
        except ImportError:
            raise TranslationError("requests library required")

        results: list[str] = []
        url = "https://translate.googleapis.com/translate_a/single"

        for text in texts:
            for attempt in range(3):
                try:
                    resp = requests.get(
                        url,
                        params={
                            "client": "gtx",
                            "sl": source_lang,
                            "tl": target_lang,
                            "dt": "t",
                            "q": text,
                        },
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    # Response: [[["translated", "source", ...], ...], ...]
                    translated = "".join(
                        part[0] for part in data[0] if part[0]
                    )
                    results.append(translated)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(
                            f"Free translation failed: {e}"
                        )
                    time.sleep(2 ** attempt)

        return results


# ===========================================================================
# Google Cloud Translation Backend
# ===========================================================================

class GoogleTranslationBackend(TranslationBackend):
    """
    Google Cloud Translation API v2 backend.

    Requires: GOOGLE_TRANSLATE_API_KEY environment variable,
    or google-cloud-translate package with service account.
    """

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("GOOGLE_TRANSLATE_API_KEY")
        if not self._api_key:
            raise TranslationError(
                "Google Translate API key not found. "
                "Set GOOGLE_TRANSLATE_API_KEY environment variable."
            )

    def name(self) -> str:
        return "google"

    def translate_texts(
        self, texts: list[str], source_lang: str = "en", target_lang: str = "ar"
    ) -> list[str]:
        try:
            import requests
        except ImportError:
            raise TranslationError("requests library required for Google Translate")

        url = "https://translation.googleapis.com/language/translate/v2"
        results = []

        # Google v2 REST API: key must be a URL query param, not in the body
        for i in range(0, len(texts), 128):  # Max 128 segments per call
            batch = texts[i : i + 128]
            body = {
                "q": batch,
                "source": source_lang,
                "target": target_lang,
                "format": "text",
            }

            for attempt in range(3):
                try:
                    resp = requests.post(
                        url,
                        params={"key": self._api_key},
                        json=body,
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    translations = data["data"]["translations"]
                    results.extend(t["translatedText"] for t in translations)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(
                            f"Google API failed after 3 retries: {e}"
                        )
                    time.sleep(2 ** attempt)

        return results


# ===========================================================================
# DeepL Backend
# ===========================================================================

class DeepLTranslationBackend(TranslationBackend):
    """
    DeepL API backend. Requires DEEPL_API_KEY environment variable.
    """

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("DEEPL_API_KEY")
        if not self._api_key:
            raise TranslationError(
                "DeepL API key not found. Set DEEPL_API_KEY environment variable."
            )

    def name(self) -> str:
        return "deepl"

    def translate_texts(
        self, texts: list[str], source_lang: str = "en", target_lang: str = "ar"
    ) -> list[str]:
        try:
            import requests
        except ImportError:
            raise TranslationError("requests library required for DeepL")

        # DeepL uses 'AR' for Arabic, 'EN' for English
        url = "https://api-free.deepl.com/v2/translate"
        results = []

        for i in range(0, len(texts), 50):
            batch = texts[i : i + 50]
            payload = {
                "text": batch,
                "source_lang": source_lang.upper(),
                "target_lang": target_lang.upper(),
            }
            headers = {"Authorization": f"DeepL-Auth-Key {self._api_key}"}

            for attempt in range(3):
                try:
                    resp = requests.post(
                        url, json=payload, headers=headers, timeout=30
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    results.extend(t["text"] for t in data["translations"])
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(f"DeepL API failed: {e}")
                    time.sleep(2 ** attempt)

        return results


# ===========================================================================
# LLM Backend (OpenAI / Anthropic)
# ===========================================================================

class LLMTranslationBackend(TranslationBackend):
    """
    LLM-based translation using OpenAI or Anthropic APIs.
    Higher quality for nuanced text, but slower and more expensive.
    """

    def __init__(
        self,
        provider: str = "openai",
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._provider = provider
        if provider == "openai":
            self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
            self._model = model or "gpt-4o-mini"
        elif provider == "anthropic":
            self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            self._model = model or "claude-sonnet-4-20250514"
        else:
            raise TranslationError(f"Unknown LLM provider: {provider}")

        if not self._api_key:
            raise TranslationError(f"API key not found for {provider}")

    def name(self) -> str:
        return f"llm-{self._provider}"

    def translate_texts(
        self, texts: list[str], source_lang: str = "en", target_lang: str = "ar"
    ) -> list[str]:
        try:
            import requests
        except ImportError:
            raise TranslationError("requests library required")

        # Batch texts with delimiter
        combined = _BATCH_DELIM.join(texts)
        prompt = (
            f"Translate the following English text segments to Arabic. "
            f"Each segment is separated by '{_BATCH_DELIM.strip()}'. "
            f"Return ONLY the Arabic translations, separated by the same delimiter. "
            f"Preserve any numbers, proper nouns, and formatting.\n\n{combined}"
        )

        if self._provider == "openai":
            return self._call_openai(prompt, len(texts))
        else:
            return self._call_anthropic(prompt, len(texts))

    def _call_openai(self, prompt: str, expected_count: int) -> list[str]:
        import requests

        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": "You are a professional English-Arabic translator."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
            timeout=60,
        )
        resp.raise_for_status()
        result_text = resp.json()["choices"][0]["message"]["content"]
        parts = result_text.split(_BATCH_DELIM.strip())
        return self._align_parts(parts, expected_count)

    def _call_anthropic(self, prompt: str, expected_count: int) -> list[str]:
        import requests

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        result_text = resp.json()["content"][0]["text"]
        parts = result_text.split(_BATCH_DELIM.strip())
        return self._align_parts(parts, expected_count)

    def _align_parts(self, parts: list[str], expected: int) -> list[str]:
        """Ensure we have the right number of translations."""
        parts = [p.strip() for p in parts]
        if len(parts) == expected:
            return parts
        if len(parts) > expected:
            return parts[:expected]
        # Pad with last translation repeated
        while len(parts) < expected:
            parts.append(parts[-1] if parts else "")
        return parts


# ===========================================================================
# Main Translator
# ===========================================================================

@dataclass
class TranslatorConfig:
    """Configuration for the translator."""
    backend: str = "mock"  # mock, google, deepl, llm-openai, llm-anthropic
    api_key: Optional[str] = None
    model: Optional[str] = None
    cache_path: Optional[str] = None
    source_lang: str = "en"
    target_lang: str = "ar"
    batch_size: int = 20  # texts per API call
    max_retries: int = 3


class Translator:
    """
    Main translator that orchestrates extraction → translation → packaging.

    Usage:
        translator = Translator(TranslatorConfig(backend="mock"))
        result = translator.translate_document(extracted_doc)
    """

    def __init__(self, config: TranslatorConfig):
        self.config = config
        self.cache = TranslationCache(config.cache_path)
        self._backend = self._create_backend(config)
        self._stats = {"total_blocks": 0, "translated": 0, "cached": 0, "skipped": 0}

    def _create_backend(self, config: TranslatorConfig) -> TranslationBackend:
        if config.backend == "mock":
            return MockTranslationBackend()
        elif config.backend in ("free", "mymemory"):   # "mymemory" kept for back-compat
            return FreeTranslationBackend()
        elif config.backend == "google":
            return GoogleTranslationBackend(config.api_key)
        elif config.backend == "deepl":
            return DeepLTranslationBackend(config.api_key)
        elif config.backend.startswith("llm-"):
            provider = config.backend.split("-", 1)[1]
            return LLMTranslationBackend(
                provider=provider, api_key=config.api_key, model=config.model
            )
        else:
            raise TranslationError(f"Unknown backend: {config.backend}")

    def translate_document(self, doc: DocumentContent) -> TranslatedDocument:
        """
        Translate an entire extracted document.

        Per-page error isolation: if a page fails, original text is kept
        and the error is recorded as a warning.
        """
        result = TranslatedDocument(
            source_path=doc.source_path,
            target_language=self.config.target_lang,
        )

        for page in doc.pages:
            try:
                translated_page = self._translate_page(page)
                result.pages.append(translated_page)
            except Exception as e:
                logger.error(f"Page {page.page_number} translation failed: {e}")
                result.warnings.append(f"Page {page.page_number}: {e}")
                # Fallback: keep original text
                result.pages.append(TranslatedPage(
                    page_number=page.page_number,
                    width=page.width,
                    height=page.height,
                    untranslated_blocks=page.text_blocks,
                    image_blocks=page.image_blocks,
                ))

        result.stats = dict(self._stats)
        result.stats.update(self.cache.stats)

        # Save cache
        self.cache.save()

        return result

    def _translate_page(self, page: PageContent) -> TranslatedPage:
        """Translate all text blocks in a page."""
        translated_page = TranslatedPage(
            page_number=page.page_number,
            width=page.width,
            height=page.height,
            image_blocks=page.image_blocks,
        )

        # Separate blocks into translatable and skip
        to_translate: list[tuple[int, TextBlock]] = []
        for idx, block in enumerate(page.text_blocks):
            self._stats["total_blocks"] += 1
            text = block.full_text.strip()

            if should_skip_translation(text):
                self._stats["skipped"] += 1
                # Keep as untranslated
                translated_page.translated_blocks.append(
                    TranslatedBlock(
                        original=block,
                        translated_text=text,
                        font=block.primary_font,
                    )
                )
            else:
                to_translate.append((idx, block))

        # Batch translate
        if to_translate:
            texts = [block.full_text.strip() for _, block in to_translate]
            translations = self._batch_translate(texts)

            for (_, block), translated_text in zip(to_translate, translations):
                translated_page.translated_blocks.append(
                    TranslatedBlock(
                        original=block,
                        translated_text=translated_text,
                        font=block.primary_font,
                    )
                )

        return translated_page

    def _batch_translate(self, texts: list[str]) -> list[str]:
        """
        Translate a batch of texts, using cache where possible.
        Only sends uncached texts to the API.
        """
        results: list[Optional[str]] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        # Check cache first
        for i, text in enumerate(texts):
            cached = self.cache.get(text, self.config.target_lang)
            if cached is not None:
                results[i] = cached
                self._stats["cached"] += 1
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        # Translate uncached texts in batches
        if uncached_texts:
            for batch_start in range(0, len(uncached_texts), self.config.batch_size):
                batch = uncached_texts[batch_start : batch_start + self.config.batch_size]
                batch_indices = uncached_indices[
                    batch_start : batch_start + self.config.batch_size
                ]

                try:
                    translations = self._backend.translate_texts(
                        batch,
                        source_lang=self.config.source_lang,
                        target_lang=self.config.target_lang,
                    )

                    for idx, text, translated in zip(
                        batch_indices, batch, translations
                    ):
                        results[idx] = translated
                        self.cache.put(text, translated, self.config.target_lang)
                        self._stats["translated"] += 1

                except TranslationError as e:
                    logger.error(f"Batch translation failed: {e}")
                    # Fallback: use original text with warning marker
                    for idx, text in zip(batch_indices, batch):
                        results[idx] = f"[TRANSLATION FAILED] {text}"

        # Replace any remaining None values
        return [r if r is not None else texts[i] for i, r in enumerate(results)]
