from flask import Flask, jsonify, send_from_directory, request
from bs4 import BeautifulSoup
import requests
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Canal principal + canal relais statistique (fallback si le principal échoue)
CHANNELS = [
    {
        'name': 'jokerwcbnn11280',
        'url': 'https://t.me/s/jokerwcbnn11280',
        'public_url': 'https://t.me/jokerwcbnn11280',
        'id': '-1002699763359',
        'label': 'principal',
    },
    {
        'name': 'statistika_baccara',
        'url': 'https://t.me/s/statistika_baccara',
        'public_url': 'https://t.me/statistika_baccara',
        'id': '-1001352009817',
        'label': 'relais',
    },
]

USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'


def _headers_for(channel):
    return {
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
        'Referer': channel['public_url'],
    }


def _get(url, channel, timeout=20):
    try:
        res = requests.get(url, headers=_headers_for(channel), timeout=timeout)
    except requests.exceptions.Timeout:
        raise RuntimeError('Le serveur Telegram n\'a pas répondu à temps (timeout).')
    except requests.exceptions.ConnectionError:
        raise RuntimeError('Impossible de joindre Telegram (connexion refusée ou réseau indisponible).')
    if res.status_code in (403, 429):
        raise RuntimeError(
            f'Telegram a bloqué la requête (HTTP {res.status_code}) — '
            'probablement un blocage temporaire de l\'IP du serveur. Réessaie dans quelques minutes.'
        )
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


def _parse_latest_from_channel(channel):
    res = _get(channel['url'], channel)
    soup = BeautifulSoup(res.text, 'html.parser')
    messages = soup.select('.tgme_widget_message_wrap')
    if not messages:
        raise RuntimeError(f'Aucun message Telegram trouvé sur @{channel["name"]}')

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
        raise RuntimeError(f'Aucun jeu exploitable trouvé sur @{channel["name"]}')

    text_el = target.select_one('.tgme_widget_message_text')
    raw_text = text_el.get_text(' ', strip=True)
    normalized = normalize_message_text(raw_text)
    game_match = GAME_RE.search(normalized)
    if not game_match:
        raise RuntimeError(f'Le dernier message de @{channel["name"]} ne contient pas de numéro de jeu valide')

    date_el = target.select_one('time')
    link_el = target.select_one('a.tgme_widget_message_date')
    msg_wrap_id = target.get('data-post') or (link_el.get('href') if link_el else '')

    return {
        'channel_url': channel['url'],
        'channel_name': channel['name'],
        'channel_id': channel['id'],
        'channel_label': channel['label'],
        'game_number': int(game_match.group(1)),
        'raw_text': raw_text,
        'normalized': normalized,
        'published_at': date_el.get('datetime') if date_el else None,
        'source_url': link_el.get('href') if link_el else channel['public_url'],
        'message_id': msg_wrap_id,
    }


def fetch_latest_game():
    """Essaie le canal principal, puis le relais en cas d'échec."""
    errors = []
    for channel in CHANNELS:
        try:
            data = _parse_latest_from_channel(channel)
            data['fallback_used'] = channel['label'] != 'principal'
            data['tried_channels'] = [c['name'] for c in CHANNELS]
            return data
        except Exception as e:
            errors.append(f"@{channel['name']} ({channel['label']}): {e}")
            continue
    raise RuntimeError('Tous les canaux ont échoué — ' + ' | '.join(errors))


@app.get('/api/latest-game')
def api_latest_game():
    try:
        return jsonify(fetch_latest_game())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _fetch_history_from_channel(channel, limit=150):
    """Récupère jusqu'à `limit` jeux passés en paginant sur t.me/s/<canal>?before=<id>."""
    games = []
    seen_ids = set()
    before = None
    pages_guard = 0

    while len(games) < limit and pages_guard < 30:
        pages_guard += 1
        url = channel['url'] if before is None else f"{channel['url']}?before={before}"
        res = _get(url, channel)
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
                'channel_name': channel['name'],
                'channel_label': channel['label'],
            })

        if not page_numeric_ids:
            break
        oldest_on_page = min(page_numeric_ids)
        if before is not None and oldest_on_page >= before:
            break
        before = oldest_on_page
        if len(messages) < 20:
            break

    games.sort(key=lambda g: g['message_numeric_id'])
    if len(games) > limit:
        games = games[-limit:]
    return games


def fetch_history(limit=150):
    """Essaie le canal principal, puis le relais si pas assez de jeux ou erreur."""
    errors = []
    for channel in CHANNELS:
        try:
            games = _fetch_history_from_channel(channel, limit)
            if len(games) >= 2:
                return games, channel
            errors.append(f"@{channel['name']}: seulement {len(games)} jeu(x)")
        except Exception as e:
            errors.append(f"@{channel['name']} ({channel['label']}): {e}")
            continue
    raise RuntimeError('Tous les canaux ont échoué pour l\'historique — ' + ' | '.join(errors))


@app.get('/api/history')
def api_history():
    limit = request.args.get('limit', default=150, type=int)
    limit = max(1, min(limit, 200))
    try:
        games, channel = fetch_history(limit)
        return jsonify({
            'games': games,
            'count': len(games),
            'channel_name': channel['name'],
            'channel_id': channel['id'],
            'channel_label': channel['label'],
            'fallback_used': channel['label'] != 'principal',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.get('/api/channels')
def api_channels():
    return jsonify({
        'channels': [
            {
                'name': c['name'],
                'id': c['id'],
                'label': c['label'],
                'public_url': c['public_url'],
            }
            for c in CHANNELS
        ]
    })


@app.get('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.get('/health')
def health():
    return jsonify({'ok': True, 'channels': [c['name'] for c in CHANNELS]})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
