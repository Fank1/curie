import json
import re
import time
from datetime import datetime, timezone

try:
    import requests as _requests

    def _http_post(url, headers, payload):
        r = _requests.post(url, headers=headers, json=payload, timeout=180)
        r.raise_for_status()
        return r.json(), {k.lower(): v for k, v in r.headers.items()}

except ImportError:
    import urllib.request
    import urllib.error

    def _http_post(url, headers, payload):
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return json.loads(resp.read().decode('utf-8')), resp_headers
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode('utf-8', errors='replace')
            except Exception:
                body = ''
            raise Exception(f'HTTP {e.code}: {e.reason} — {body}') from e


API_URL     = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2023-06-01'

MODELS = {
    'claude-sonnet-4-6': {
        'label':          'Claude Sonnet 4.6 — Better quality',
        'input':          3.00,
        'output':         15.00,
        'cache_write':    3.75,
        'cache_read':     0.30,
        'estimate_step1': (0.20, 0.35),
        'estimate_step2': (0.08, 0.15),
    },
    'claude-haiku-4-5-20251001': {
        'label':          'Claude Haiku 4.5 — Faster & cheaper',
        'input':          0.80,
        'output':         4.00,
        'cache_write':    1.00,
        'cache_read':     0.08,
        'estimate_step1': (0.05, 0.10),
        'estimate_step2': (0.02, 0.05),
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers(api_key, betas=None):
    h = {
        'x-api-key':         api_key,
        'anthropic-version': API_VERSION,
        'content-type':      'application/json',
    }
    if betas:
        h['anthropic-beta'] = ','.join(betas)
    return h


def _usage_from(resp):
    u = resp.get('usage', {})
    return {
        'input_tokens':       u.get('input_tokens', 0),
        'output_tokens':      u.get('output_tokens', 0),
        'cache_write_tokens': u.get('cache_creation_input_tokens', 0),
        'cache_read_tokens':  u.get('cache_read_input_tokens', 0),
    }


def _accumulate(total, partial):
    for k in total:
        total[k] += partial.get(k, 0)


def _countdown_sleep(seconds, label, progress_cb=None):
    """Sleep with a live per-second countdown emitted via progress_cb."""
    for remaining in range(int(seconds), 0, -1):
        if progress_cb:
            progress_cb(f'{label} — {remaining}s remaining…')
        time.sleep(1)


def wait_for_rate_limit(resp_headers, tokens_needed, progress_cb=None):
    """Proactively wait if the remaining token budget won't cover the next request."""
    try:
        remaining = int(resp_headers.get('anthropic-ratelimit-tokens-remaining', 999_999))
    except (ValueError, TypeError):
        return

    if remaining >= tokens_needed:
        return

    reset_str = resp_headers.get('anthropic-ratelimit-tokens-reset', '')
    wait = 0
    if reset_str:
        try:
            reset_time = datetime.fromisoformat(reset_str.replace('Z', '+00:00'))
            wait = max(0, (reset_time - datetime.now(timezone.utc)).total_seconds()) + 1
        except Exception:
            wait = 60

    if wait > 0:
        _countdown_sleep(int(wait), 'Rate limit — pausing before Step 2', progress_cb)


def calc_cost(usage, model_id):
    if not usage:
        return 0.0
    p = MODELS.get(model_id, MODELS['claude-sonnet-4-6'])
    return (
        usage.get('input_tokens', 0)       * p['input'] +
        usage.get('output_tokens', 0)      * p['output'] +
        usage.get('cache_write_tokens', 0) * p['cache_write'] +
        usage.get('cache_read_tokens', 0)  * p['cache_read']
    ) / 1_000_000


def extract_json(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*\n?', '', text)
    text = re.sub(r'\n?```\s*$', '', text)
    text = text.strip()
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ── Step 1 ────────────────────────────────────────────────────────────────────

def generate_book_data(api_key, title, author, include_characters,
                       include_places, language, model, progress_cb=None):
    field_specs = []
    if include_characters:
        field_specs.append(
            f'characters: array of objects with:\n'
            f'  - full_name (string)\n'
            f'  - nicknames (array of strings, capital letters only)\n'
            f'  - role: protagonist | major character | supporting character | minor character\n'
            f'  - description: 2-3 sentences, SPOILER-FREE, written in {language}'
        )
    if include_places:
        field_specs.append(
            f'locations: array of objects with:\n'
            f'  - name (string)\n'
            f'  - type (string, e.g. shop / landmark / residential / street / workplace)\n'
            f'  - aliases (array of strings, capital letters only)\n'
            f'  - description: 2-3 sentences, SPOILER-FREE, written in {language}'
        )

    what = ' and '.join(
        (['characters'] if include_characters else []) +
        (['locations']  if include_places     else [])
    )
    skeleton = {'title': title, 'author': author}
    if include_characters:
        skeleton['characters'] = []
    if include_places:
        skeleton['locations'] = []

    prompt = (
        f'Research the book "{title}" by {author} and generate a JSON with {what}.\n\n'
        f'Return ONLY a raw JSON object (no markdown, no code fences) matching:\n'
        f'{json.dumps(skeleton, indent=2)}\n\n'
        f'Required fields:\n' + '\n'.join(field_specs) + '\n\n'
        f'Rules:\n'
        f'- All description content MUST be in {language}\n'
        f'- All JSON keys MUST be in English\n'
        f'- Descriptions must be SPOILER-FREE\n'
        f'- Cover all significant {what}\n'
        f'- Nicknames and aliases must be proper names in natural title case (e.g. "Mr. Nakano"), never all-caps\n'
        f'- Return ONLY the raw JSON'
    )

    messages     = [{'role': 'user', 'content': prompt}]
    total_usage  = {'input_tokens': 0, 'output_tokens': 0,
                    'cache_write_tokens': 0, 'cache_read_tokens': 0}
    last_headers = {}

    step1_attempt = 0
    while True:
        if progress_cb:
            progress_cb('Step 1: Searching online for book data…')

        try:
            resp, last_headers = _http_post(API_URL, _headers(api_key, ['web-search-2025-03-05']), {
                'model':      model,
                'max_tokens': 8000,
                'tools':      [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 5}],
                'messages':   messages,
            })
        except Exception as exc:
            if '429' in str(exc) and step1_attempt < 2:
                wait = 60 * (step1_attempt + 1)
                _countdown_sleep(wait, 'Step 1: Rate limit', progress_cb)
                step1_attempt += 1
                continue
            raise
        step1_attempt = 0

        _accumulate(total_usage, _usage_from(resp))

        stop_reason = resp.get('stop_reason')
        content     = resp.get('content', [])

        if stop_reason == 'end_turn':
            text_blocks = [
                b['text'] for b in content
                if b.get('type') == 'text' and b.get('text', '').strip()
            ]
            if text_blocks:
                return extract_json(text_blocks[-1]), total_usage, last_headers
            raise ValueError('Step 1: no text block in response')

        if stop_reason == 'tool_use':
            messages.append({'role': 'assistant', 'content': content})
            messages.append({'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': b['id'], 'content': ''}
                for b in content if b.get('type') == 'tool_use'
            ]})
        else:
            raise ValueError(f'Step 1: unexpected stop_reason "{stop_reason}"')


# ── Step 2 (Claude enrichment) ────────────────────────────────────────────────

def enrich_with_claude(api_key, book_data, epub_text, model, progress_cb=None, chunk_label=''):
    if progress_cb:
        suffix = f' ({chunk_label})' if chunk_label else ''
        progress_cb(f'Step 2 (EPUB spoiler analysis): Enriching with Claude{suffix}…')

    payload = {
        'model':      model,
        'max_tokens': 8000,
        'system': [
            {
                'type': 'text',
                'text': (
                    'You are an expert literary analyst. '
                    'Below is the book text (or an excerpt), organized by chapter.\n\n'
                    + epub_text
                ),
                'cache_control': {'type': 'ephemeral'},
            }
        ],
        'messages': [
            {
                'role': 'user',
                'content': (
                    f'Below is the character/location JSON for "{book_data["title"]}" '
                    f'by {book_data["author"]}, generated from web research.\n\n'
                    f'{json.dumps(book_data, indent=2, ensure_ascii=False)}\n\n'
                    'Using the book text, enrich this JSON by:\n'
                    '1. Correcting misspelled names to match actual spelling in the book\n'
                    '2. Adding missing nicknames/aliases (proper names in natural title case only, never all-caps)\n'
                    '3. Removing spoilers from descriptions — each entity has a "chapter" field indicating where it first appears; descriptions must only reflect what a reader would know up to and including that chapter, never revealing events or details from later chapters\n'
                    '4. Adding significant missing characters/locations, including the protagonist or first-person narrator if absent\n\n'
                    'Return ONLY the complete enriched JSON. No markdown, no explanation.\n'
                    'Do NOT add or modify chapter or occurrences fields.'
                ),
            }
        ],
    }

    for attempt in range(3):
        try:
            resp, _ = _http_post(API_URL, _headers(api_key), payload)
            break
        except Exception as exc:
            if '429' in str(exc) and attempt < 2:
                wait = 60 * (attempt + 1)
                _countdown_sleep(wait, 'Step 2: Rate limit', progress_cb)
            else:
                raise

    usage = _usage_from(resp)
    for block in resp.get('content', []):
        if block.get('type') == 'text' and block.get('text', '').strip():
            return extract_json(block['text']), usage

    raise ValueError('Step 2: no text block in Claude response')


# ── Ollama support ────────────────────────────────────────────────────────────

# Tokens reserved for system prompt overhead + user message + expected response.
# Available book-text tokens per chunk = context_size - OLLAMA_OVERHEAD_TOKENS.
OLLAMA_OVERHEAD_TOKENS = 4000


def _http_get(url):
    try:
        import requests as _req
        r = _req.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except ImportError:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))


def list_ollama_models(host):
    """Return a list of model name strings from a running Ollama instance."""
    try:
        data = _http_get(host.rstrip('/') + '/api/tags')
        return [m['name'] for m in data.get('models', [])]
    except Exception:
        return []


def _ollama_chat(host, model, messages, context_size):
    """POST to Ollama /api/chat with a long timeout; return the response dict."""
    url     = host.rstrip('/') + '/api/chat'
    payload = {
        'model':    model,
        'stream':   False,
        'format':   'json',
        'options':  {'num_ctx': context_size},
        'messages': messages,
    }
    headers = {'content-type': 'application/json'}
    try:
        import requests as _req
        r = _req.post(url, headers=headers, json=payload, timeout=600)
        r.raise_for_status()
        return r.json()
    except ImportError:
        import urllib.request
        import urllib.error
        data = json.dumps(payload).encode('utf-8')
        req  = urllib.request.Request(url, data=data, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8', errors='replace')
            except Exception:
                pass
            raise Exception(f'Ollama HTTP {e.code}: {e.reason} — {body}') from e


def generate_book_data_ollama(host, model, context_size, title, author,
                               include_characters, include_places, language,
                               progress_cb=None):
    """Step 1 for Ollama: ask local model for character/location data.

    Falls back to an empty skeleton if the model has no knowledge of the book;
    Step 2 (enrichment from EPUB text) will fill in the gaps.
    """
    if progress_cb:
        progress_cb('Step 1: Asking local model for book data…')

    field_specs = []
    if include_characters:
        field_specs.append(
            f'characters: array of objects with:\n'
            f'  - full_name (string)\n'
            f'  - nicknames (array of strings)\n'
            f'  - role: protagonist | major character | supporting character | minor character\n'
            f'  - description: 2-3 sentences, SPOILER-FREE, written in {language}'
        )
    if include_places:
        field_specs.append(
            f'locations: array of objects with:\n'
            f'  - name (string)\n'
            f'  - type (string, e.g. shop / landmark / residential / street / workplace)\n'
            f'  - aliases (array of strings)\n'
            f'  - description: 2-3 sentences, SPOILER-FREE, written in {language}'
        )

    what = ' and '.join(
        (['characters'] if include_characters else []) +
        (['locations']  if include_places     else [])
    )
    skeleton = {'title': title, 'author': author}
    if include_characters:
        skeleton['characters'] = []
    if include_places:
        skeleton['locations'] = []

    prompt = (
        f'Generate a JSON object listing {what} for the book "{title}" by {author}.\n\n'
        f'Use this exact JSON structure:\n'
        f'{json.dumps(skeleton, indent=2)}\n\n'
        f'Field requirements:\n' + '\n'.join(field_specs) + '\n\n'
        f'Rules:\n'
        f'- All description content MUST be in {language}\n'
        f'- All JSON keys MUST be in English\n'
        f'- Descriptions must be SPOILER-FREE — no plot revelations\n'
        f'- Nicknames and aliases in natural title case (e.g. "Mr. Smith"), never all-caps\n'
        f'- If you have little or no reliable knowledge of this book, '
        f'return the skeleton with empty arrays rather than inventing data\n'
        f'- Return ONLY the JSON object, no explanation'
    )

    try:
        resp = _ollama_chat(host, model, [{'role': 'user', 'content': prompt}], context_size)
        text = resp.get('message', {}).get('content', '')
        return extract_json(text), {}
    except Exception:
        return skeleton, {}


def discover_entities_from_chunk(host, model, context_size, epub_text,
                                  include_characters, include_places,
                                  progress_cb=None, chunk_label=''):
    """Ask the LLM to list entity names found in a text chunk.

    This is intentionally a narrow task (names only, no descriptions) so that
    local models handle it reliably. Descriptions are filled in by the
    subsequent enrich_with_ollama pass.
    Returns {'characters': [...names...], 'locations': [...names...]}.
    """
    if progress_cb:
        suffix = f' ({chunk_label})' if chunk_label else ''
        progress_cb(f'Step 1b (name discovery): Scanning{suffix}…')

    char_example = '"characters": ["Alice Smith", "Bob"]' if include_characters else ''
    loc_example  = '"locations": ["The Red Tavern", "Thornfield Hall"]' if include_places else ''
    skeleton     = '{' + ', '.join(filter(None, [char_example, loc_example])) + '}'

    parts = []
    if include_characters:
        parts.append('named characters (people) — any named person including minor ones')
    if include_places:
        parts.append('named locations (places) — any named place, building, or area')

    system_content = (
        'You are a literary analyst. Extract named entities from the following book excerpt.\n\n'
        + epub_text
    )
    user_content = (
        f'List all {" and ".join(parts)} mentioned in the book text above.\n\n'
        f'Return ONLY this JSON structure:\n{skeleton}\n\n'
        'Rules:\n'
        '- Natural title case only (e.g. "Mrs. Hudson", "John Watson")\n'
        '- Include nicknames and short names as separate entries\n'
        '- Do NOT include unnamed references like "the man" or "a woman"\n'
        '- If none found for a category, return an empty array\n'
        '- Return ONLY the JSON object'
    )

    messages = [
        {'role': 'system', 'content': system_content},
        {'role': 'user',   'content': user_content},
    ]
    try:
        resp = _ollama_chat(host, model, messages, context_size)
        text = resp.get('message', {}).get('content', '')
        return extract_json(text)
    except Exception:
        return {}


def enrich_with_ollama(host, model, context_size, book_data, epub_text,
                        progress_cb=None, chunk_label=''):
    """Step 2 for Ollama: enrich entity data using a chunk of actual EPUB text."""
    if progress_cb:
        suffix = f' ({chunk_label})' if chunk_label else ''
        progress_cb(f'Step 2 (EPUB analysis): Enriching with local model{suffix}…')

    system_content = (
        'You are an expert literary analyst. '
        'Analyze the following book text (organized by chapter) to enrich character and location data.\n\n'
        + epub_text
    )
    user_content = (
        f'Here is the current character/location JSON for "{book_data.get("title", "")}" '
        f'by {book_data.get("author", "")}:\n\n'
        f'{json.dumps(book_data, indent=2, ensure_ascii=False)}\n\n'
        'Update this JSON using ONLY the book text provided in the system message. '
        'Do NOT use any knowledge from your training data about this book or its characters.\n\n'
        '1. Correct misspelled names to match the actual spelling in the text\n'
        '2. Add missing nicknames/aliases found in the text (natural title case, never all-caps)\n'
        '3. For entities whose description is empty (""): write a 1-3 sentence SPOILER-FREE '
        'description based strictly on what is shown in these chapters. '
        'If the entity barely appears, one short factual sentence is enough.\n'
        '4. For entities with an existing description: rewrite it only if it contains '
        'information that is NOT present in the provided text, or reveals events from '
        'chapters after the "chapter" field value. Otherwise keep it unchanged.\n'
        '5. Descriptions must never reveal what happens after the chapter in the "chapter" field.\n\n'
        'Return ONLY the complete updated JSON. No markdown, no explanation.\n'
        'Do NOT modify "chapter" or "occurrences" fields.'
    )

    messages = [
        {'role': 'system', 'content': system_content},
        {'role': 'user',   'content': user_content},
    ]
    resp = _ollama_chat(host, model, messages, context_size)
    text = resp.get('message', {}).get('content', '')
    return extract_json(text), {}
