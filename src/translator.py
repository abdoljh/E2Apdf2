"""
Translation layer for E2A PDF translator.

Backends:
  - mock           — placeholder text (testing)
  - free           — unofficial Google Translate (gtx endpoint)
  - deep-google    — deep-translator GoogleTranslator (recommended free)
  - deep-mymemory  — deep-translator MyMemoryTranslator
  - google         — Google Cloud Translation API (paid)
  - deepl          — DeepL API (paid)
  - llm-openai     — OpenAI GPT
  - llm-anthropic  — Anthropic Claude

Features:
  - Translation caching (SHA-256 keyed)
  - Skip detection (numbers, URLs, already-Arabic)
  - Marker preservation (parentheses, brackets, special symbols)
  - Batch translation to minimize API calls
  - Retry with exponential backoff
"""
from __future__ import annotations
import hashlib, json, logging, os, re, time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from .arabic_utils import has_arabic
from .models import (DocumentContent, FontInfo, TextBlock,
                     TranslatedBlock, TranslatedDocument, TranslatedPage)

logger = logging.getLogger(__name__)
_BATCH_DELIM = "\n|||E2A_SPLIT|||\n"


class TranslationError(Exception):
    pass


# ═══════════════════════════════════════════════════════════════
# Translation Cache
# ═══════════════════════════════════════════════════════════════

class TranslationCache:
    def __init__(self, cache_path=None):
        self._cache = {}
        self._cache_path = Path(cache_path) if cache_path else None
        self._hits = self._misses = 0
        if self._cache_path and self._cache_path.exists():
            try:
                self._cache = json.loads(
                    self._cache_path.read_text(encoding="utf-8")
                )
            except Exception:
                pass

    def _key(self, text, tl="ar"):
        return hashlib.sha256(f"{tl}:{text}".encode()).hexdigest()[:16]

    def get(self, text, tl="ar"):
        r = self._cache.get(self._key(text, tl))
        if r is not None:
            self._hits += 1
        else:
            self._misses += 1
        return r

    def put(self, text, translation, tl="ar"):
        self._cache[self._key(text, tl)] = translation

    def save(self):
        if self._cache_path:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    @property
    def stats(self):
        return {
            "cache_size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
        }


# ═══════════════════════════════════════════════════════════════
# Marker Preservation
# ═══════════════════════════════════════════════════════════════

# Patterns that should be preserved (not translated)
_MARKER_PATTERNS = [
    r'\([^)]{0,5}\)',           # Short parenthesized markers: (A), (1), etc.
    r'\[[^\]]{0,5}\]',          # Short bracketed markers: [1], [a], etc.
    r'§\s*\d+',                 # Section markers: §1, § 2, etc.
    r'¢\s',                     # Bullet markers
    r'(?:Fig(?:ure)?|Table|Eq)\.\s*\d+[\-.]?\d*',  # Figure/Table refs
]
_MARKER_RE = re.compile('|'.join(_MARKER_PATTERNS))


def preserve_markers(text: str):
    """
    Extract markers from text, replace with placeholders, return
    (cleaned_text, list_of_markers) so markers can be reinserted
    after translation.
    """
    markers = []
    def _replace(m):
        markers.append(m.group())
        return f" __MRK{len(markers)-1}__ "
    cleaned = _MARKER_RE.sub(_replace, text)
    return cleaned, markers


def restore_markers(text: str, markers: list[str]) -> str:
    """Reinsert preserved markers into translated text."""
    for i, marker in enumerate(markers):
        text = text.replace(f"__MRK{i}__", marker)
    return text


# ═══════════════════════════════════════════════════════════════
# Skip Detection
# ═══════════════════════════════════════════════════════════════

def should_skip_translation(text: str) -> bool:
    text = text.strip()
    if not text or len(text) <= 1:
        return True
    if has_arabic(text):
        return True
    if re.match(r'^[\d\s.,;:/%$€£¥+\-*=<>()\[\]{}]+$', text):
        return True
    if re.match(r'^https?://', text):
        return True
    if re.match(r'^[\w.+-]+@[\w-]+\.[\w.-]+$', text):
        return True
    if re.match(r'^[A-Z][.\s]*$', text):
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# Backend Interface
# ═══════════════════════════════════════════════════════════════

class TranslationBackend(ABC):
    @abstractmethod
    def translate_texts(self, texts, source_lang="en", target_lang="ar"):
        ...
    @abstractmethod
    def name(self):
        ...


# ═══════════════════════════════════════════════════════════════
# Mock Backend
# ═══════════════════════════════════════════════════════════════

class MockTranslationBackend(TranslationBackend):
    def name(self):
        return "mock"

    def translate_texts(self, texts, source_lang="en", target_lang="ar"):
        return [f"[ترجمة تجريبية] {i+1}" for i, _ in enumerate(texts)]


# ═══════════════════════════════════════════════════════════════
# Free Backend (unofficial Google Translate — gtx)
# ═══════════════════════════════════════════════════════════════

class FreeTranslationBackend(TranslationBackend):
    def name(self):
        return "free"

    def translate_texts(self, texts, source_lang="en", target_lang="ar"):
        import requests
        results = []
        url = "https://translate.googleapis.com/translate_a/single"
        for text in texts:
            for attempt in range(3):
                try:
                    resp = requests.get(
                        url,
                        params={"client": "gtx", "sl": source_lang,
                                "tl": target_lang, "dt": "t", "q": text},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    translated = "".join(p[0] for p in data[0] if p[0])
                    results.append(translated)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(f"Free translation failed: {e}")
                    time.sleep(2 ** attempt)
        return results


# ═══════════════════════════════════════════════════════════════
# deep-translator Backends (GoogleTranslator, MyMemoryTranslator)
# ═══════════════════════════════════════════════════════════════

class DeepTranslatorBackend(TranslationBackend):
    """
    Uses the deep-translator library for translation.
    Supports GoogleTranslator (free, no API key) and
    MyMemoryTranslator (free, no API key).

    Install: pip install deep-translator

    Advantages over the gtx endpoint:
      - Well-maintained library with proper error handling
      - Automatic text chunking for long texts (>5000 chars)
      - Multiple service support through one interface
      - Better rate-limit handling
    """

    def __init__(self, provider: str = "google"):
        self._provider = provider
        # Validate the library is available
        try:
            if provider == "google":
                from deep_translator import GoogleTranslator
            elif provider == "mymemory":
                from deep_translator import MyMemoryTranslator
            else:
                raise TranslationError(
                    f"Unknown deep-translator provider: {provider}. "
                    f"Use 'google' or 'mymemory'."
                )
        except ImportError:
            raise TranslationError(
                "deep-translator library not installed. "
                "Install it with: pip install deep-translator"
            )

    def name(self):
        return f"deep-{self._provider}"

    def translate_texts(self, texts, source_lang="en", target_lang="ar"):
        if self._provider == "google":
            return self._translate_google(texts, source_lang, target_lang)
        elif self._provider == "mymemory":
            return self._translate_mymemory(texts, source_lang, target_lang)
        return texts

    def _translate_google(self, texts, source_lang, target_lang):
        from deep_translator import GoogleTranslator

        results = []
        translator = GoogleTranslator(source=source_lang, target=target_lang)

        for text in texts:
            for attempt in range(3):
                try:
                    # deep-translator handles chunking for long texts
                    translated = translator.translate(text)
                    results.append(translated or text)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(
                            f"deep-translator Google failed: {e}"
                        )
                    time.sleep(2 ** attempt)

        return results

    def _translate_mymemory(self, texts, source_lang, target_lang):
        from deep_translator import MyMemoryTranslator

        results = []
        translator = MyMemoryTranslator(
            source=source_lang, target=target_lang
        )

        for text in texts:
            for attempt in range(3):
                try:
                    translated = translator.translate(text)
                    results.append(translated or text)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(
                            f"deep-translator MyMemory failed: {e}"
                        )
                    time.sleep(2 ** attempt)

        return results


# ═══════════════════════════════════════════════════════════════
# Google Cloud Translation Backend
# ═══════════════════════════════════════════════════════════════

class GoogleCloudTranslationBackend(TranslationBackend):
    def __init__(self, api_key=None):
        self._api_key = api_key or os.environ.get("GOOGLE_TRANSLATE_API_KEY")
        if not self._api_key:
            raise TranslationError("Google Translate API key not found.")

    def name(self):
        return "google"

    def translate_texts(self, texts, source_lang="en", target_lang="ar"):
        import requests
        url = "https://translation.googleapis.com/language/translate/v2"
        results = []
        for i in range(0, len(texts), 128):
            batch = texts[i:i+128]
            body = {"q": batch, "source": source_lang,
                    "target": target_lang, "format": "text"}
            for attempt in range(3):
                try:
                    resp = requests.post(
                        url, params={"key": self._api_key},
                        json=body, timeout=30,
                    )
                    resp.raise_for_status()
                    translations = resp.json()["data"]["translations"]
                    results.extend(t["translatedText"] for t in translations)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(f"Google API failed: {e}")
                    time.sleep(2 ** attempt)
        return results


# ═══════════════════════════════════════════════════════════════
# DeepL Backend
# ═══════════════════════════════════════════════════════════════

class DeepLTranslationBackend(TranslationBackend):
    def __init__(self, api_key=None):
        self._api_key = api_key or os.environ.get("DEEPL_API_KEY")
        if not self._api_key:
            raise TranslationError("DeepL API key not found.")

    def name(self):
        return "deepl"

    def translate_texts(self, texts, source_lang="en", target_lang="ar"):
        import requests
        url = "https://api-free.deepl.com/v2/translate"
        results = []
        for i in range(0, len(texts), 50):
            batch = texts[i:i+50]
            for attempt in range(3):
                try:
                    resp = requests.post(
                        url,
                        json={"text": batch,
                              "source_lang": source_lang.upper(),
                              "target_lang": target_lang.upper()},
                        headers={"Authorization": f"DeepL-Auth-Key {self._api_key}"},
                        timeout=30,
                    )
                    resp.raise_for_status()
                    results.extend(t["text"] for t in resp.json()["translations"])
                    break
                except Exception as e:
                    if attempt == 2:
                        raise TranslationError(f"DeepL failed: {e}")
                    time.sleep(2 ** attempt)
        return results


# ═══════════════════════════════════════════════════════════════
# LLM Backend (OpenAI / Anthropic)
# ═══════════════════════════════════════════════════════════════

class LLMTranslationBackend(TranslationBackend):
    def __init__(self, provider="openai", api_key=None, model=None):
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
            raise TranslationError(f"No API key for {provider}")

    def name(self):
        return f"llm-{self._provider}"

    def translate_texts(self, texts, source_lang="en", target_lang="ar"):
        import requests
        combined = _BATCH_DELIM.join(texts)
        prompt = (
            f"Translate the following English text segments to Arabic. "
            f"Each segment is separated by '{_BATCH_DELIM.strip()}'. "
            f"Return ONLY the Arabic translations, separated by the same "
            f"delimiter. Preserve numbers, proper nouns, and formatting.\n\n"
            f"{combined}"
        )
        if self._provider == "openai":
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "messages": [
                    {"role": "system", "content": "You are a professional English-Arabic translator."},
                    {"role": "user", "content": prompt}],
                    "temperature": 0.1},
                timeout=60,
            )
            resp.raise_for_status()
            parts = resp.json()["choices"][0]["message"]["content"].split(
                _BATCH_DELIM.strip()
            )
        else:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self._api_key,
                          "anthropic-version": "2023-06-01",
                          "Content-Type": "application/json"},
                json={"model": self._model, "max_tokens": 4096,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=60,
            )
            resp.raise_for_status()
            parts = resp.json()["content"][0]["text"].split(
                _BATCH_DELIM.strip()
            )

        parts = [p.strip() for p in parts]
        while len(parts) < len(texts):
            parts.append(parts[-1] if parts else "")
        return parts[:len(texts)]


# ═══════════════════════════════════════════════════════════════
# Translator Config & Main Class
# ═══════════════════════════════════════════════════════════════

@dataclass
class TranslatorConfig:
    backend: str = "mock"
    api_key: Optional[str] = None
    model: Optional[str] = None
    cache_path: Optional[str] = None
    source_lang: str = "en"
    target_lang: str = "ar"
    batch_size: int = 20
    max_retries: int = 3
    preserve_markers: bool = True  # NEW: protect markers from translation


class Translator:
    """
    Main translator orchestrating backend calls, caching, and
    marker preservation.
    """

    def __init__(self, config: TranslatorConfig):
        self.config = config
        self.cache = TranslationCache(config.cache_path)
        self._backend = self._create_backend(config)
        self._stats = {
            "total_blocks": 0, "translated": 0,
            "cached": 0, "skipped": 0,
        }

    def _create_backend(self, c: TranslatorConfig) -> TranslationBackend:
        backend = c.backend
        if backend == "mock":
            return MockTranslationBackend()
        elif backend in ("free", "mymemory"):
            return FreeTranslationBackend()
        elif backend == "deep-google":
            return DeepTranslatorBackend(provider="google")
        elif backend == "deep-mymemory":
            return DeepTranslatorBackend(provider="mymemory")
        elif backend == "google":
            return GoogleCloudTranslationBackend(api_key=c.api_key)
        elif backend == "deepl":
            return DeepLTranslationBackend(api_key=c.api_key)
        elif backend.startswith("llm-"):
            provider = backend.split("-", 1)[1]
            return LLMTranslationBackend(
                provider=provider, api_key=c.api_key, model=c.model,
            )
        else:
            raise TranslationError(f"Unknown backend: {backend}")

    def translate_document(self, doc):
        result = TranslatedDocument(
            source_path=doc.source_path,
            target_language=self.config.target_lang,
        )
        for page in doc.pages:
            try:
                tp = self._translate_page(page)
                result.pages.append(tp)
            except Exception as e:
                logger.error(f"Page {page.page_number} failed: {e}")
                result.warnings.append(f"Page {page.page_number}: {e}")
                result.pages.append(TranslatedPage(
                    page_number=page.page_number,
                    width=page.width, height=page.height,
                ))
        result.stats = dict(self._stats)
        result.stats.update(self.cache.stats)
        self.cache.save()
        return result

    def _translate_page(self, page):
        tp = TranslatedPage(
            page_number=page.page_number,
            width=page.width, height=page.height,
            image_blocks=page.image_blocks,
        )
        to_translate = []
        for idx, block in enumerate(page.text_blocks):
            self._stats["total_blocks"] += 1
            text = block.full_text.strip()
            if should_skip_translation(text):
                self._stats["skipped"] += 1
                tp.translated_blocks.append(TranslatedBlock(
                    original=block, translated_text=text,
                    font=block.primary_font,
                ))
            else:
                to_translate.append((idx, block))

        if to_translate:
            texts = [b.full_text.strip() for _, b in to_translate]
            translations = self._batch_translate(texts)
            for (_, block), tr in zip(to_translate, translations):
                tp.translated_blocks.append(TranslatedBlock(
                    original=block, translated_text=tr,
                    font=block.primary_font,
                ))
        return tp

    def _batch_translate(self, texts):
        results = [None] * len(texts)
        uncached_i, uncached_t = [], []

        for i, t in enumerate(texts):
            cached = self.cache.get(t, self.config.target_lang)
            if cached is not None:
                results[i] = cached
                self._stats["cached"] += 1
            else:
                uncached_i.append(i)
                uncached_t.append(t)

        if uncached_t:
            for bs in range(0, len(uncached_t), self.config.batch_size):
                batch = uncached_t[bs:bs + self.config.batch_size]
                batch_idx = uncached_i[bs:bs + self.config.batch_size]

                # Marker preservation: extract markers before translation
                if self.config.preserve_markers:
                    cleaned_batch = []
                    markers_batch = []
                    for text in batch:
                        cleaned, markers = preserve_markers(text)
                        cleaned_batch.append(cleaned)
                        markers_batch.append(markers)
                    translate_batch = cleaned_batch
                else:
                    translate_batch = batch
                    markers_batch = [[] for _ in batch]

                try:
                    translations = self._backend.translate_texts(
                        translate_batch,
                        source_lang=self.config.source_lang,
                        target_lang=self.config.target_lang,
                    )

                    for idx, orig_text, tr, markers in zip(
                        batch_idx, batch, translations, markers_batch
                    ):
                        # Restore markers
                        if markers:
                            tr = restore_markers(tr, markers)
                        results[idx] = tr
                        self.cache.put(orig_text, tr, self.config.target_lang)
                        self._stats["translated"] += 1

                except TranslationError as e:
                    logger.error(f"Batch translation failed: {e}")
                    for idx, t in zip(batch_idx, batch):
                        results[idx] = f"[TRANSLATION FAILED] {t}"

        return [
            r if r is not None else texts[i]
            for i, r in enumerate(results)
        ]
