from flask import Flask, jsonify, send_from_directory, request
from bs4 import BeautifulSoup
import requests
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHANNEL_URL = 'https://t.me/s/statistika_baccara'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'
REQUEST_HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
    'Referer': 'https://t.me/statistika_baccara',
}


def _get(url, timeout=20):
    try:
        res = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    except requests.exceptions.Timeout:
        raise RuntimeError('Le serveur Telegram n\'a pas répondu à temps (timeout).')
    except requests.exceptions.ConnectionError:
        raise RuntimeError('Impossible de joindre Telegram (connexion refusée ou réseau indisponible).')
    if res.status_code in (403, 429):
        raise RuntimeError(f'Telegram a bloqué la requête (HTTP {res.status_code}) — '
                            'probablement un blocage temporaire de l\'IP du serveur. Réessaie dans quelques minutes.')
    if res.status_code >= 400:
        raise RuntimeError(f'Telegram a répondu une erreur HTTP {res.status_code}.')
    return res

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path='')

CARD_SUIT_RE = re.compile(r'(10|[2-9AJQK])\s*([♠♥♦♣])\s*')
GAME_RE = re.compile(r'#N\s*(\d+)', re.IGNORECASE)


def normalize_message_text(text: str) -> str:
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'#N\s*(\d+)\s*\.', r'#N\1.', text, flags=re.IGNORECASE)
    text = re.sub(r'\(\s*', '(', text)
    text = re.sub(r'\s*\)', ')', text)
    text = re.sub(r'\s*-\s*', ' - ', text)

    def card_fix(match):
        return f"{match.group(1)}{match.group(2)}"

    text = CARD_SUIT_RE.sub(card_fix, text)
    text = re.sub(r'\)\s*-', ') -', text)
    text = re.sub(r'-\s*(\d+\()', r'- \1', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def fetch_latest_game():
    res = _get(CHANNEL_URL)
    soup = BeautifulSoup(res.text, 'html.parser')
    messages = soup.select('.tgme_widget_message_wrap')
    if not messages:
        raise RuntimeError('Aucun message Telegram trouvé')

    target = None
    for msg in reversed(messages):
        text_el = msg.select_one('.tgme_widget_message_text')
        if not text_el:
            continue
        raw_text = text_el.get_text(' ', strip=True)
        if '#N' in raw_text:
            target = msg
            break

    if target is None:
        raise RuntimeError('Aucun jeu exploitable trouvé dans les messages récents')

    text_el = target.select_one('.tgme_widget_message_text')
    raw_text = text_el.get_text(' ', strip=True)
    normalized = normalize_message_text(raw_text)
    game_match = GAME_RE.search(normalized)
    if not game_match:
        raise RuntimeError('Le dernier message ne contient pas de numéro de jeu valide')

    date_el = target.select_one('time')
    link_el = target.select_one('a.tgme_widget_message_date')
    msg_wrap_id = target.get('data-post') or (link_el.get('href') if link_el else '')

    return {
        'channel_url': CHANNEL_URL,
        'game_number': int(game_match.group(1)),
        'raw_text': raw_text,
        'normalized': normalized,
        'published_at': date_el.get('datetime') if date_el else None,
        'source_url': link_el.get('href') if link_el else CHANNEL_URL,
        'message_id': msg_wrap_id,
    }


@app.get('/api/latest-game')
def api_latest_game():
    try:
        return jsonify(fetch_latest_game())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def fetch_history(limit=150):
    """Récupère jusqu'à `limit` jeux passés en paginant sur t.me/s/<canal>?before=<id>."""
    games = []
    seen_ids = set()
    before = None
    pages_guard = 0

    while len(games) < limit and pages_guard < 30:
        pages_guard += 1
        url = CHANNEL_URL if before is None else f'{CHANNEL_URL}?before={before}'
        res = _get(url)
        soup = BeautifulSoup(res.text, 'html.parser')
        messages = soup.select('.tgme_widget_message_wrap')
        if not messages:
            break

        page_numeric_ids = []
        for msg in messages:
            post_id = msg.get('data-post')
            if not post_id:
                continue
            try:
                numeric_id = int(post_id.split('/')[-1])
            except ValueError:
                continue
            page_numeric_ids.append(numeric_id)

            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            text_el = msg.select_one('.tgme_widget_message_text')
            if not text_el:
                continue
            raw_text = text_el.get_text(' ', strip=True)
            if '#N' not in raw_text:
                continue
            normalized = normalize_message_text(raw_text)
            game_match = GAME_RE.search(normalized)
            if not game_match:
                continue
            date_el = msg.select_one('time')
            games.append({
                'message_id': post_id,
                'message_numeric_id': numeric_id,
                'game_number': int(game_match.group(1)),
                'raw_text': raw_text,
                'normalized': normalized,
                'published_at': date_el.get('datetime') if date_el else None,
            })

        if not page_numeric_ids:
            break
        oldest_on_page = min(page_numeric_ids)
        if before is not None and oldest_on_page >= before:
            break  # plus de progression possible, on arrête pour éviter une boucle infinie
        before = oldest_on_page
        if len(messages) < 20:
            break  # dernière page atteinte

    games.sort(key=lambda g: g['message_numeric_id'])  # chronologique : plus ancien -> plus récent
    if len(games) > limit:
        games = games[-limit:]
    return games


@app.get('/api/history')
def api_history():
    limit = request.args.get('limit', default=150, type=int)
    limit = max(1, min(limit, 200))
    try:
        games = fetch_history(limit)
        return jsonify({'games': games, 'count': len(games)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.get('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.get('/health')
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
