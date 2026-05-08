import re
import zipfile

from lxml import etree

_NS_CONTAINER = 'urn:oasis:names:tc:opendocument:xmlns:container'
_NS_OPF       = 'http://www.idpf.org/2007/opf'
_NS_XHTML     = 'http://www.w3.org/1999/xhtml'

# epub:type values that identify non-chapter front/back matter.
_FRONT_MATTER_RE = re.compile(
    r'epub:type\s*=\s*["\'][^"\']*\b('
    r'cover|title-page|titlepage|toc|landmarks|frontmatter|'
    r'halftitlepage|copyright-page|dedication|colophon|'
    r'index|loi|lot|seriespage'
    r')\b',
    re.IGNORECASE,
)


def get_spine_items(epub_path):
    """Return list of (chapter_number, zip_path, html_bytes) for each content spine item."""
    items       = []
    chapter_num = 0

    with zipfile.ZipFile(epub_path, 'r') as zf:
        opf_path = _find_opf_path(zf)
        opf_dir  = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''
        opf      = etree.fromstring(zf.read(opf_path))

        manifest = {
            item.get('id'): item.get('href')
            for item in opf.findall(f'.//{{{_NS_OPF}}}item')
        }
        spine = opf.find(f'.//{{{_NS_OPF}}}spine')

        for itemref in spine.findall(f'{{{_NS_OPF}}}itemref'):
            # Skip items explicitly marked as non-linear (covers, TOC pages, etc.)
            if itemref.get('linear', 'yes').lower() == 'no':
                continue

            sid  = itemref.get('idref', '')
            href = manifest.get(sid, '')
            if not href:
                continue
            full_path = _resolve(opf_dir, href)
            try:
                content = zf.read(full_path)
            except KeyError:
                continue

            # Skip front/back matter identified by epub:type on body or section
            raw = content[:2000].decode('utf-8', errors='replace')
            if _FRONT_MATTER_RE.search(raw):
                continue

            text = _to_text(content)
            if len(text.strip()) < 100:
                continue
            chapter_num += 1
            items.append((chapter_num, full_path, content))

    return items


def extract_spine_texts(epub_path):
    """Return list of (chapter_number, plain_text) for each content spine item."""
    return [(n, _to_text(html)) for n, _, html in get_spine_items(epub_path)]


# Token budget per chunk: 160k tokens for epub text, leaving ~40k for user message
# and system prompt overhead. Uses bytes//3 as the token estimate so the limit
# is accurate for both ASCII (1 byte/char) and Japanese/CJK (3 bytes/char).
CHUNK_TOKEN_BUDGET = 160_000


def build_epub_chunks(chapters, chunk_token_budget=CHUNK_TOKEN_BUDGET):
    """Split chapters into context-window-sized strings for Claude enrichment."""
    chunks, current, current_tokens = [], [], 0
    for chapter_num, text in chapters:
        piece = f'\n=== CHAPTER {chapter_num} ===\n{text}\n'
        piece_tokens = len(piece.encode('utf-8')) // 3
        if current and current_tokens + piece_tokens > chunk_token_budget:
            chunks.append(''.join(current))
            current, current_tokens = [], 0
        current.append(piece)
        current_tokens += piece_tokens
    if current:
        chunks.append(''.join(current))
    return chunks


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_opf_path(zf):
    container = etree.fromstring(zf.read('META-INF/container.xml'))
    return container.find(f'.//{{{_NS_CONTAINER}}}rootfile').get('full-path')


def _resolve(base_dir, href):
    href = href.split('#')[0]
    return href.lstrip('/') if href.startswith('/') else base_dir + href


def _to_text(html_bytes):
    try:
        root = etree.fromstring(html_bytes)
    except etree.XMLSyntaxError:
        try:
            from lxml import html as lhtml
            root = lhtml.fromstring(html_bytes)
        except Exception:
            return ''

    for tag in root.iter(
        f'{{{_NS_XHTML}}}script', f'{{{_NS_XHTML}}}style', 'script', 'style'
    ):
        parent = tag.getparent()
        if parent is not None:
            parent.remove(tag)

    return ' '.join(' '.join(root.itertext()).split())
