import os
import re
import zipfile
from html import escape

# Matches tags and comments so we can process only text nodes
_TAG_RE = re.compile(
    r'(<(?:[^>"\']*|"[^"]*"|\'[^\']*\')*>|<!--[\s\S]*?-->)',
    re.DOTALL,
)

# Like _TAG_RE but also treats already-injected noteref spans as opaque units,
# preventing their text content from being re-matched by subsequent entity passes.
# Identifies our links by the curie- href pattern (no custom class needed).
_SPLIT_RE = re.compile(
    r'(<a\b[^>]*href="[^"]*curie-[^"]*"[^>]*>[\s\S]*?</a>'
    r'|<(?:[^>"\']*|"[^"]*"|\'[^\']*\')*>'
    r'|<!--[\s\S]*?-->)',
    re.DOTALL,
)

_EPUB_NS = 'xmlns:epub="http://www.idpf.org/2007/ops"'

# Matches any space-like separator as it may appear in raw HTML source.
_SPACE_PAT = '(?:[  ]|&#160;|&nbsp;)'

# Pattern matching any per-entity footnote file we create
_ENTITY_FILE_RE = re.compile(r'curie-(char|loc)-\d+\.xhtml$')
# Also matches the old single-file format for backward-compatible removal
_ANY_CURIE_RE = re.compile(r'curie-(?:footnotes|(?:char|loc)-\d+)\.xhtml$')


# ── Public API ────────────────────────────────────────────────────────────────

def inject_footnotes(epub_path, book_data, target_reader='koreader',
                     hint_density='every_10_paragraphs'):
    """
    Inject EPUB 3 popup footnotes for all characters and locations.
    Each entity gets its own dedicated XHTML spine document (linear=no) so that
    Kobo Nickel shows exactly one entry in its popup, not the whole notes file.
    Modifies the EPUB in-place. Returns {'chapters_modified': N, 'refs_injected': M}.
    """
    from calibre_plugins.curie.epub_utils import get_spine_items, _find_opf_path

    characters = book_data.get('characters', [])
    locations  = book_data.get('locations', [])

    with zipfile.ZipFile(epub_path, 'r') as zf:
        opf_path     = _find_opf_path(zf)
        old_curie = [n for n in zf.namelist() if _ANY_CURIE_RE.search(n)]
    opf_dir = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''

    spine_items           = get_spine_items(epub_path)
    chapter_modifications = {}
    asides_dict           = {}   # entity_id -> aside_html, deduplicated across chapters
    total_refs            = 0

    for chapter_num, zip_path, html_bytes in spine_items:
        new_bytes, ref_count, chapter_asides = _process_chapter(
            html_bytes, chapter_num, characters, locations,
            zip_path, opf_dir, target_reader, hint_density,
        )
        if new_bytes != html_bytes:
            chapter_modifications[zip_path] = new_bytes
        total_refs += ref_count
        asides_dict.update(chapter_asides)

    modifications = dict(chapter_modifications)
    # Exclude stale entity files from the old injection so they don't persist
    exclude = set(old_curie)

    if asides_dict:
        for entity_id, aside_html in asides_dict.items():
            entity_zip_path = opf_dir + f'{entity_id}.xhtml'
            modifications[entity_zip_path] = _build_entity_file(aside_html).encode('utf-8')
            exclude.discard(entity_zip_path)  # keep files we're actively writing

        modifications[opf_path] = _opf_add_entities(
            epub_path, opf_path, list(asides_dict.keys())
        )

    if modifications or exclude:
        _rewrite_epub(epub_path, modifications, exclude)

    return {'chapters_modified': len(chapter_modifications), 'refs_injected': total_refs}


def remove_injections(epub_path):
    """Strip all Curie-injected elements from every chapter and delete
    all per-entity footnote files. Returns chapters cleaned."""
    from calibre_plugins.curie.epub_utils import get_spine_items, _find_opf_path

    with zipfile.ZipFile(epub_path, 'r') as zf:
        opf_path      = _find_opf_path(zf)
        namelist      = zf.namelist()
    opf_dir = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''

    spine_items   = get_spine_items(epub_path)
    modifications = {}
    exclude       = set()

    for _, zip_path, html_bytes in spine_items:
        html_str = html_bytes.decode('utf-8', errors='replace')
        cleaned  = _strip_injections(html_str)
        if cleaned != html_str:
            modifications[zip_path] = cleaned.encode('utf-8')

    curie_files = [n for n in namelist if _ANY_CURIE_RE.search(n)]
    if curie_files:
        exclude.update(curie_files)
        modifications[opf_path] = _opf_remove_entities(epub_path, opf_path)

    if modifications or exclude:
        _rewrite_epub(epub_path, modifications, exclude)

    return {'chapters_cleaned': len(modifications)}


# ── Chapter processing ────────────────────────────────────────────────────────

def _process_chapter(html_bytes, chapter_num, characters, locations,
                     chapter_zip_path, opf_dir, target_reader='koreader',
                     hint_density='every_10_paragraphs'):
    """Returns (new_bytes, ref_count, asides_dict) where asides_dict maps entity_id -> aside_html."""
    html_str = html_bytes.decode('utf-8', errors='replace')

    if '<html' not in html_str.lower():
        return html_bytes, 0, {}

    # Remove any previous injections so re-running is safe
    html_str = _strip_injections(html_str)

    asides_dict = {}
    total_refs  = 0

    for i, char in enumerate(characters):
        if (char.get('chapter') or 0) >= chapter_num:
            continue
        primary = char.get('full_name') or char.get('name', '')
        if not primary:
            continue
        nicknames = [n for n in char.get('nicknames', []) if n]
        names     = [primary] + nicknames
        entity_id = f'curie-char-{i + 1}'
        href      = _entity_href(chapter_zip_path, opf_dir, entity_id)

        html_str, count = _inject_entity(html_str, names, entity_id, href, target_reader, hint_density)
        if count:
            total_refs += count
            asides_dict[entity_id] = _make_aside(
                entity_id, primary, nicknames,
                char.get('role', ''), char.get('description', ''), target_reader,
            )

    for i, loc in enumerate(locations):
        if (loc.get('chapter') or 0) >= chapter_num:
            continue
        primary = loc.get('name') or loc.get('full_name', '')
        if not primary:
            continue
        names     = [primary]
        entity_id = f'curie-loc-{i + 1}'
        href      = _entity_href(chapter_zip_path, opf_dir, entity_id)

        html_str, count = _inject_entity(html_str, names, entity_id, href, target_reader, hint_density)
        if count:
            total_refs += count
            asides_dict[entity_id] = _make_aside(
                entity_id, primary, [],
                loc.get('type', ''), loc.get('description', ''), target_reader,
            )

    if not asides_dict:
        return html_str.encode('utf-8'), 0, {}

    # Ensure epub namespace is declared on <html> (required for epub:type on <a> tags)
    if 'xmlns:epub=' not in html_str:
        html_str = re.sub(r'(<html\b[^>]*?)(>)', r'\1 ' + _EPUB_NS + r'\2', html_str, count=1)

    # Mark <body> as curie-enriched (used by _is_enriched in the UI)
    html_str = _set_body_attr(html_str, 'data-curie', 'true')

    return html_str.encode('utf-8'), total_refs, asides_dict


def _entity_href(chapter_zip_path, opf_dir, entity_id):
    """Relative href from a chapter file to an entity's dedicated footnote file."""
    entity_zip_path = opf_dir + f'{entity_id}.xhtml'
    return _relative_href(chapter_zip_path, entity_zip_path) + f'#{entity_id}'


def _inject_entity(html_str, names, entity_id, href, target_reader='koreader',
                   hint_density='every_10_paragraphs'):
    """Wrap occurrences of any name in text nodes with a noteref link,
    subject to the hint_density policy. Each entity independently tracks
    its own paragraph cooldown."""
    if target_reader == 'nickel':
        attrs = 'epub:type="noteref" role="doc-noteref" style="-webkit-text-fill-color:inherit"'
    else:
        attrs = 'epub:type="noteref" role="doc-noteref"'

    sorted_names = sorted(names, key=len, reverse=True)

    # Mutable state shared across the closure
    para_num    = [0]     # counts <p> openings seen so far in this chapter
    last_tagged = [None]  # paragraph where this entity was last tagged (None = never)
    total       = [0]

    def _should_tag():
        if hint_density == 'every_mention':
            return True
        if hint_density == 'once_per_chapter':
            return last_tagged[0] is None
        # every_10_paragraphs
        return last_tagged[0] is None or (para_num[0] - last_tagged[0]) >= 10

    parts  = _SPLIT_RE.split(html_str)
    result = []

    for i, part in enumerate(parts):
        if i % 2 == 1:  # tag or comment — track paragraphs, leave untouched
            if re.match(r'<p[\s>]', part, re.IGNORECASE):
                para_num[0] += 1
            result.append(part)
            continue

        segments = [(True, part)]

        for name in sorted_names:
            pat = re.compile(
                r'\b' + _SPACE_PAT.join(re.escape(w) for w in name.split()) + r'\b',
                re.IGNORECASE,
            )
            new_segments = []
            for is_text, seg in segments:
                if not is_text:
                    new_segments.append((False, seg))
                    continue

                out      = ''
                last_end = 0
                replaced = False
                for m in pat.finditer(seg):
                    out += seg[last_end:m.start()]
                    if _should_tag():
                        out += _make_link(m.group(0), href, attrs)
                        last_tagged[0] = para_num[0]
                        total[0] += 1
                        replaced = True
                    else:
                        out += m.group(0)
                    last_end = m.end()
                out += seg[last_end:]

                if replaced:
                    sub = _TAG_RE.split(out)
                    for k, s in enumerate(sub):
                        new_segments.append((k % 2 == 0, s))
                else:
                    new_segments.append((True, seg))
            segments = new_segments

        result.append(''.join(s for _, s in segments))

    return ''.join(result), total[0]


def _make_link(text, href, attrs):
    return f'<a href="{href}" {attrs}>{text}</a>'


def _make_aside(entity_id, name, aliases, role_or_type, description, target_reader='koreader'):
    if target_reader == 'nickel':
        # Lean format: primary name only (no aliases), no role/type.
        # Nickel popup renders plain text — styling tags have no effect.
        return (
            f'<aside id="{entity_id}" epub:type="footnote" '
            f'role="doc-footnote" class="curie-footnote">'
            f'<p><strong>{escape(name)}:</strong> {escape(description)}</p>'
            '</aside>'
        )

    name_part = escape(name)
    if aliases:
        name_part += ' (' + ', '.join(escape(a) for a in aliases) + ')'
    html = (
        f'<aside id="{entity_id}" epub:type="footnote" '
        f'role="doc-footnote" class="curie-footnote">'
        f'<p style="font-size:1.125em;font-weight:bold;margin:0 0 0.1em"><strong>{name_part}</strong></p>'
    )
    if role_or_type:
        html += f'<p style="font-size:1em;font-style:italic;margin:0 0 0.4em"><em>{escape(role_or_type.capitalize())}</em></p>'
    html += f'<p style="font-size:1em;margin:0">{escape(description)}</p>'
    html += '</aside>'
    return html


def _build_entity_file(aside_html):
    """Build a minimal XHTML document containing a single footnote aside."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" {_EPUB_NS}>\n'
        '<head><title>Footnote</title></head>\n'
        '<body>\n'
        f'{aside_html}\n'
        '</body>\n'
        '</html>'
    )


def _set_body_attr(html_str, attr, value):
    """Add or overwrite an attribute on the <body> opening tag."""
    attr_pat = re.compile(rf'\s+{re.escape(attr)}="[^"]*"')

    def replacer(m):
        tag = attr_pat.sub('', m.group(0))
        return tag[:-1] + f' {attr}="{value}">'

    return re.sub(r'<body\b[^>]*>', replacer, html_str, count=1)


def _strip_injections(html_str):
    """Remove all Curie-injected noterefs and entity files' asides, and the body flag.
    Identified by the curie- href pattern and the curie-footnote class."""
    while True:
        prev     = html_str
        html_str = re.sub(
            r'<a\b(?=[^>]*\bhref="[^"]*curie-[^"]*")[^>]*>(.*?)</a>',
            r'\1', html_str, flags=re.DOTALL,
        )
        html_str = re.sub(
            r'<aside\b(?=[^>]*\bclass="curie-footnote")[^>]*>[\s\S]*?</aside>',
            '', html_str, flags=re.DOTALL,
        )
        html_str = re.sub(r'\s+data-curie="[^"]*"', '', html_str)
        if html_str == prev:
            break
    return html_str


def _relative_href(from_zip_path, to_zip_path):
    """Compute a relative URL from one zip entry path to another."""
    from_parts = from_zip_path.split('/')
    to_parts   = to_zip_path.split('/')

    from_dirs = from_parts[:-1]
    to_dirs   = to_parts[:-1]

    common = 0
    for a, b in zip(from_dirs, to_dirs):
        if a == b:
            common += 1
        else:
            break

    up   = len(from_dirs) - common
    down = to_parts[common:]

    return '../' * up + '/'.join(down)


# ── OPF manipulation ──────────────────────────────────────────────────────────

def _opf_add_entities(epub_path, opf_path, entity_ids):
    """Add one manifest item + spine itemref per entity (idempotent: clears old entries first)."""
    with zipfile.ZipFile(epub_path, 'r') as zf:
        opf_str = zf.read(opf_path).decode('utf-8', errors='replace')

    # Always clean existing curie entries first so re-injection stays idempotent
    opf_str = _strip_curie_from_opf(opf_str)

    manifest_items = ''
    spine_items    = ''
    for entity_id in entity_ids:
        rel_href = f'{entity_id}.xhtml'
        manifest_items += (
            f'\n<item id="{entity_id}" href="{rel_href}" '
            f'media-type="application/xhtml+xml"/>'
        )
        spine_items += f'\n<itemref idref="{entity_id}" linear="no"/>'

    opf_str = re.sub(r'(</manifest>)', manifest_items + r'\1', opf_str, count=1)
    opf_str = re.sub(r'(</spine>)',    spine_items    + r'\1', opf_str, count=1)

    return opf_str.encode('utf-8')


def _opf_remove_entities(epub_path, opf_path):
    """Remove all Curie manifest items and spine itemrefs from the OPF."""
    with zipfile.ZipFile(epub_path, 'r') as zf:
        opf_str = zf.read(opf_path).decode('utf-8', errors='replace')
    return _strip_curie_from_opf(opf_str).encode('utf-8')


def _strip_curie_from_opf(opf_str):
    """Remove all curie-* manifest items and spine itemrefs."""
    opf_str = re.sub(
        r'\s*<item\b[^>]*id="curie-[^"]*"[^>]*/>', '', opf_str,
    )
    opf_str = re.sub(
        r'\s*<itemref\b[^>]*idref="curie-[^"]*"[^>]*/?>',  '', opf_str,
    )
    return opf_str


# ── EPUB rewriting ────────────────────────────────────────────────────────────

def _rewrite_epub(epub_path, modifications, exclude=None):
    """Rewrite the EPUB ZIP, replacing/adding specified files and skipping excluded ones."""
    exclude  = exclude or set()
    tmp_path = epub_path + '.curie_tmp'
    try:
        with zipfile.ZipFile(epub_path, 'r') as zin:
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                # mimetype must be first and stored uncompressed per EPUB spec
                if 'mimetype' in zin.namelist():
                    zout.writestr(
                        zipfile.ZipInfo('mimetype'),
                        zin.read('mimetype'),
                        compress_type=zipfile.ZIP_STORED,
                    )
                existing_names = set()
                for item in zin.infolist():
                    if item.filename == 'mimetype':
                        continue
                    if item.filename in exclude:
                        continue
                    existing_names.add(item.filename)
                    data = modifications.get(item.filename)
                    zout.writestr(item, data if data is not None else zin.read(item.filename))

                # Write new files not present in the original EPUB
                for path, data in modifications.items():
                    if path not in existing_names and path not in exclude:
                        zout.writestr(path, data)

        os.replace(tmp_path, epub_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
