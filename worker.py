import json
import os
import re
import threading
import time
import traceback

from qt.core import QThread, pyqtSignal

from calibre_plugins.curie.api_client import (
    generate_book_data, enrich_with_claude, calc_cost, wait_for_rate_limit,
    generate_book_data_ollama, enrich_with_ollama, discover_entities_from_chunk,
    OLLAMA_OVERHEAD_TOKENS,
)
from calibre_plugins.curie.epub_utils import extract_spine_texts, build_epub_chunks

MIN_OCCURRENCES = 3

_Cancelled = type('_Cancelled', (Exception,), {})

# Words that look like proper names in title case but aren't characters/places.
_REGEX_SEED_STOPWORDS = {
    'Chapter', 'Part', 'Book', 'Section', 'Volume', 'Prologue', 'Epilogue',
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
    'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday',
    'English', 'French', 'German', 'Spanish', 'Italian', 'Russian', 'Dutch',
    'North', 'South', 'East', 'West', 'Northern', 'Southern', 'Eastern', 'Western',
    'God', 'Lord', 'Lady', 'King', 'Queen', 'Prince', 'Princess',
}

# Place-name suffixes that help identify locations from plain text.
_PLACE_SUFFIX_RE = re.compile(
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+'
    r'(?:Street|Road|Lane|Avenue|Square|Park|Hall|House|Inn|Tavern|'
    r'Manor|Castle|Tower|Bridge|Gate|Market|Court|Place|Gardens?))\b'
)


def _regex_seed_names(book_data, chapters, include_characters, include_places):
    """Seed entity lists with candidates found via capitalization heuristics.

    Catches names the LLM discovery missed. False positives are removed later
    by _filter_by_occurrences and the enrichment pass.
    """
    full_text = ' '.join(text for _, text in chapters)

    if include_characters:
        known = {(c.get('full_name') or c.get('name', '')).lower()
                 for c in book_data.get('characters', [])}

        # ── Multi-word names: "John Smith", "Mrs Brown" ───────────────────
        multi = re.compile(r'\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})+)\b')
        counts: dict[str, int] = {}
        for m in multi.finditer(full_text):
            name = m.group(1)
            if not any(w in _REGEX_SEED_STOPWORDS for w in name.split()):
                counts[name] = counts.get(name, 0) + 1

        # ── Single capitalized words that appear mid-sentence ─────────────
        # A word is "mid-sentence" if the preceding char is not a period/newline.
        # We require higher frequency to offset the noisier signal.
        single = re.compile(r'(?<=[a-z,;:""\']\s)([A-Z][a-z]{2,})\b')
        single_counts: dict[str, int] = {}
        for m in single.finditer(full_text):
            name = m.group(1)
            if name not in _REGEX_SEED_STOPWORDS:
                single_counts[name] = single_counts.get(name, 0) + 1

        for name, count in counts.items():
            if count >= MIN_OCCURRENCES and name.lower() not in known:
                known.add(name.lower())
                book_data.setdefault('characters', []).append({
                    'full_name': name, 'nicknames': [],
                    'role': 'supporting character', 'description': '',
                })

        # Single-word names need a higher bar to suppress false positives
        for name, count in single_counts.items():
            if count >= max(5, MIN_OCCURRENCES * 2) and name.lower() not in known:
                known.add(name.lower())
                book_data.setdefault('characters', []).append({
                    'full_name': name, 'nicknames': [],
                    'role': 'supporting character', 'description': '',
                })

    if include_places:
        known_locs = {(l.get('name') or l.get('full_name', '')).lower()
                      for l in book_data.get('locations', [])}
        for m in _PLACE_SUFFIX_RE.finditer(full_text):
            name = m.group(1)
            if name.lower() not in known_locs:
                known_locs.add(name.lower())
                book_data.setdefault('locations', []).append({
                    'name': name, 'type': '', 'aliases': [], 'description': '',
                })


class CurieWorker(QThread):
    progress     = pyqtSignal(str)
    step1_done   = pyqtSignal(dict, dict)                    # book_data, usage1
    analysis_done = pyqtSignal(dict, dict, dict, str, dict)  # book_data, usage1, usage2, output_path, inject_stats
    error        = pyqtSignal(str)

    def __init__(self, api_key, title, author, epub_path, include_characters,
                 include_places, language, model, output_path, inject_only=False,
                 target_reader='koreader', hint_density='every_10_paragraphs',
                 provider='anthropic', ollama_host='http://localhost:11434',
                 ollama_model='', ollama_context_size=8192):
        super().__init__()
        self.api_key             = api_key
        self.title               = title
        self.author              = author
        self.epub_path           = epub_path
        self.include_characters  = include_characters
        self.include_places      = include_places
        self.language            = language
        self.model               = model
        self.output_path         = output_path
        self.inject_only         = inject_only
        self.target_reader       = target_reader
        self.hint_density        = hint_density
        self.provider            = provider
        self.ollama_host         = ollama_host
        self.ollama_model        = ollama_model
        self.ollama_context_size = ollama_context_size
        self._stop               = threading.Event()

    def cancel(self):
        self._stop.set()

    def _progress(self, msg):
        if self._stop.is_set():
            raise _Cancelled()
        self.progress.emit(msg)

    def run(self):
        try:
            if self.inject_only:
                self._progress('Loading existing analysis…')
                with open(self.output_path, 'r', encoding='utf-8') as f:
                    book_data = json.load(f)
                self._progress('Injecting footnote hints into EPUB…')
                from calibre_plugins.curie.epub_injector import inject_footnotes
                inject_stats = inject_footnotes(self.epub_path, book_data, self.target_reader, self.hint_density)
                self.analysis_done.emit(dict(book_data), {}, {}, self.output_path, inject_stats or {})
                return

            if self.provider == 'ollama':
                self._run_ollama()
            else:
                self._run_anthropic()

        except _Cancelled:
            pass  # clean exit — QThread.finished fires naturally

        except Exception as exc:
            self.error.emit(f'{exc}\n\n{traceback.format_exc()}')

    def _run_anthropic(self):
        # ── Step 1 ────────────────────────────────────────────────────────
        book_data, usage1, rate_headers = generate_book_data(
            self.api_key, self.title, self.author,
            self.include_characters, self.include_places,
            self.language, self.model,
            progress_cb=self._progress,
        )
        self.step1_done.emit(dict(book_data), dict(usage1))

        # ── Step 2a: programmatic counting ────────────────────────────────
        self._progress('Step 2 (EPUB spoiler analysis): Parsing EPUB…')
        chapters = extract_spine_texts(self.epub_path)

        self._progress('Step 2 (EPUB spoiler analysis): Counting occurrences…')
        _add_chapter_and_occurrences(book_data, chapters)

        # ── Proactive rate-limit pause before Step 2 Claude calls ─────────
        epub_total_chars = sum(len(text) for _, text in chapters)
        tokens_needed    = epub_total_chars // 4 + 5_000
        wait_for_rate_limit(rate_headers, tokens_needed=tokens_needed,
                            progress_cb=self._progress)

        # ── Step 2b: Claude enrichment (chunked for large books) ──────────
        epub_chunks  = build_epub_chunks(chapters)
        n_chunks     = len(epub_chunks)
        usage2       = {'input_tokens': 0, 'output_tokens': 0,
                        'cache_write_tokens': 0, 'cache_read_tokens': 0}
        current_data = book_data

        for i, chunk_text in enumerate(epub_chunks):
            chunk_label = f'part {i + 1}/{n_chunks}' if n_chunks > 1 else ''
            current_data, chunk_usage = enrich_with_claude(
                self.api_key, current_data, chunk_text, self.model,
                progress_cb=self._progress,
                chunk_label=chunk_label,
            )
            for k in usage2:
                usage2[k] += chunk_usage.get(k, 0)

        self._finish(book_data_enriched=current_data, usage1=usage1, usage2=usage2, chapters=chapters)

    def _run_ollama(self):
        # ── Step 1 ────────────────────────────────────────────────────────
        book_data, usage1 = generate_book_data_ollama(
            self.ollama_host, self.ollama_model, self.ollama_context_size,
            self.title, self.author,
            self.include_characters, self.include_places,
            self.language,
            progress_cb=self._progress,
        )
        self.step1_done.emit(dict(book_data), {})

        # ── Step 2a: programmatic counting ────────────────────────────────
        self._progress('Step 2 (EPUB analysis): Parsing EPUB…')
        chapters = extract_spine_texts(self.epub_path)

        self._progress('Step 2 (EPUB analysis): Counting occurrences…')
        _add_chapter_and_occurrences(book_data, chapters)

        # ── Step 2b: Name discovery (one pass per chunk, names only) ─────
        # Asking the model only for names is a much simpler task than
        # extraction + description combined, so local LLMs do it reliably.
        chunk_budget = max(500, self.ollama_context_size - OLLAMA_OVERHEAD_TOKENS)
        epub_chunks  = build_epub_chunks(chapters, chunk_token_budget=chunk_budget)
        n_chunks     = len(epub_chunks)

        known_char_names = {
            (c.get('full_name') or c.get('name', '')).lower()
            for c in book_data.get('characters', [])
        }
        known_loc_names = {
            (l.get('name') or l.get('full_name', '')).lower()
            for l in book_data.get('locations', [])
        }

        for i, chunk_text in enumerate(epub_chunks):
            chunk_label = f'part {i + 1}/{n_chunks}' if n_chunks > 1 else ''
            discovered = discover_entities_from_chunk(
                self.ollama_host, self.ollama_model, self.ollama_context_size,
                chunk_text,
                self.include_characters, self.include_places,
                progress_cb=self._progress,
                chunk_label=chunk_label,
            )
            for name in discovered.get('characters', []):
                if name and name.lower() not in known_char_names:
                    known_char_names.add(name.lower())
                    book_data.setdefault('characters', []).append({
                        'full_name': name, 'nicknames': [],
                        'role': 'supporting character', 'description': '',
                    })
            for name in discovered.get('locations', []):
                if name and name.lower() not in known_loc_names:
                    known_loc_names.add(name.lower())
                    book_data.setdefault('locations', []).append({
                        'name': name, 'type': '', 'aliases': [], 'description': '',
                    })

        # Regex seeding: catch names the LLM missed (single-word names, multi-word
        # sequences). False positives are removed by _filter_by_occurrences later.
        self._progress('Step 2 (EPUB analysis): Regex name seeding…')
        _regex_seed_names(book_data, chapters, self.include_characters, self.include_places)

        # Re-count occurrences now that we have the full entity list
        _add_chapter_and_occurrences(book_data, chapters, only_missing=False)

        # ── Step 2c: Ollama enrichment (add descriptions per chunk) ──────
        current_data = book_data
        for i, chunk_text in enumerate(epub_chunks):
            chunk_label = f'part {i + 1}/{n_chunks}' if n_chunks > 1 else ''
            current_data, _ = enrich_with_ollama(
                self.ollama_host, self.ollama_model, self.ollama_context_size,
                current_data, chunk_text,
                progress_cb=self._progress,
                chunk_label=chunk_label,
            )

        self._finish(book_data_enriched=current_data, usage1=usage1, usage2={}, chapters=chapters)

    def _finish(self, book_data_enriched, usage1, usage2, chapters):
        """Shared post-processing, save, inject, and signal emission."""
        # ── Post-processing ───────────────────────────────────────────────
        _fix_schema(book_data_enriched)
        _filter_aliases(book_data_enriched)
        _add_chapter_and_occurrences(book_data_enriched, chapters, only_missing=True)
        _filter_by_occurrences(book_data_enriched)

        # ── Save sidecar JSON ─────────────────────────────────────────────
        self._progress('Saving output…')
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        with open(self.output_path, 'w', encoding='utf-8') as f:
            json.dump(book_data_enriched, f, indent=2, ensure_ascii=False)

        # ── Step 3: Inject footnotes into EPUB ────────────────────────────
        self._progress('Step 3: Injecting footnote hints into EPUB…')
        from calibre_plugins.curie.epub_injector import inject_footnotes
        inject_stats = inject_footnotes(
            self.epub_path, book_data_enriched, self.target_reader, self.hint_density
        )

        self.analysis_done.emit(
            dict(book_data_enriched),
            dict(usage1),
            dict(usage2),
            self.output_path,
            inject_stats or {},
        )


# ── Post-processing helpers ───────────────────────────────────────────────────

def _scan(names, chapters):
    first_chapter = None
    total = 0
    for chapter_num, text in chapters:
        count = sum(
            len(re.findall(r'\b' + re.escape(n) + r'\b', text, re.IGNORECASE))
            for n in names if n
        )
        total += count
        if count > 0 and first_chapter is None:
            first_chapter = chapter_num
    return first_chapter, total


def _add_chapter_and_occurrences(book_data, chapters, only_missing=False):
    for char in book_data.get('characters', []):
        if only_missing and char.get('chapter') is not None:
            continue
        primary = char.get('full_name') or char.get('name', '')
        if not primary:
            continue
        names = [primary] + char.get('nicknames', [])
        char['chapter'], char['occurrences'] = _scan(names, chapters)

    for loc in book_data.get('locations', []):
        if only_missing and loc.get('chapter') is not None:
            continue
        primary = loc.get('name') or loc.get('full_name', '')
        if not primary:
            continue
        loc['chapter'], loc['occurrences'] = _scan([primary], chapters)


def _fix_schema(book_data):
    """Drop entries where Claude returned the wrong field names; remove spurious top-level fields."""
    book_data.pop('note', None)
    book_data['characters'] = [
        c for c in book_data.get('characters', [])
        if c.get('full_name') or c.get('name')
    ]
    book_data['locations'] = [
        loc for loc in book_data.get('locations', [])
        if loc.get('name') or loc.get('full_name')
    ]


def _filter_aliases(book_data):
    for char in book_data.get('characters', []):
        char['nicknames'] = [n for n in char.get('nicknames', []) if n and n[0].isupper()]
    for loc in book_data.get('locations', []):
        primary_words = set((loc.get('name') or loc.get('full_name', '')).lower().split())
        loc['aliases'] = [
            a for a in loc.get('aliases', [])
            if a and a[0].isupper()
            and len(a.split()) >= 2                                    # single words are too broad
            and not set(a.lower().split()).issubset(primary_words)     # must add at least one new word
        ]


def _filter_by_occurrences(book_data, min_occurrences=MIN_OCCURRENCES):
    for key in ('characters', 'locations'):
        book_data[key] = [
            e for e in book_data.get(key, [])
            if (e.get('occurrences') or 0) >= min_occurrences
        ]
