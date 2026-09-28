"""Brandora catalog builder for GitHub Pages.

Reads the public Telegram forum group t.me/brandora_all (via embed pages),
downloads photos, names items and writes site/data.json + site/p/<id>/<n>.webp.

First run (no site/data.json): full scan of the whole group.
Next runs: only messages after the last seen id.
Names: names.json (id -> name). New items get a name from the Anthropic API
(if ANTHROPIC_API_KEY is set) or from caption/hashtags/section as a fallback.
"""
import base64, datetime, html, io, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, 'site')
DATA = os.path.join(SITE, 'data.json')
NAMES = os.path.join(ROOT, 'names.json')
SECTIONS = json.load(open(os.path.join(ROOT, 'sections.json'), encoding='utf-8'))
CHANNEL = 'brandora_all'
MAX_PHOTOS = 12
IGNORED_TOPICS = {1, 26805}  # General, «Наши обзоры» — не показываем на сайте
WIDTH = 480
S = requests.Session()
S.headers['User-Agent'] = 'Mozilla/5.0 (catalog builder)'


def fetch(i):
    for a in range(6):
        try:
            r = S.get(f'https://t.me/{CHANNEL}/{i}?embed=1&mode=tme', timeout=25)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(1 + a)
    return None


def parse(i, h):
    if h is None:
        return {'id': i, 'err': 1}
    if 'Post not found' in h:
        return {'id': i, 'nf': 1}
    d = {'id': i}
    m = re.search(r'datetime="([^"]*)"', h)
    d['date'] = m.group(1)[:10] if m else ''
    m = re.search(r'class="tgme_widget_message_reply[^"]*" href="https://t.me/' + CHANNEL + r'/(\d+)', h)
    d['reply'] = int(m.group(1)) if m else None
    ph = [(int(b), a) for a, b in re.findall(r"background-image:url\('([^']+)'\)\"[^>]*href=\"https://t.me/" + CHANNEL + r"/(\d+)\?single", h)]
    if not ph:
        ph = [(i, a) for a in re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", h)]
    vid = re.findall(r"tgme_widget_message_video_thumb\" style=\"background-image:url\('([^']+)'\)", h)
    d['video'] = bool(vid)
    if not ph and vid:
        ph = [(i, vid[0])]
    d['photos'] = ph
    m = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_footer|<div class="tgme_widget_message_reactions)', h, re.S)
    d['text'] = html.unescape(re.sub(r'<br\s*/?>', '\n', re.sub(r'<(?!br)[^>]+>', '', m.group(1)))).strip() if m else ''
    return d


def scan(start, stop_after_missing):
    out, i, miss = {}, start, 0
    while miss < stop_after_missing:
        ids = list(range(i, i + 100))
        with ThreadPoolExecutor(12) as ex:
            res = list(ex.map(lambda k: parse(k, fetch(k)), ids))
        for d in res:
            out[d['id']] = d
            miss = miss + 1 if d.get('nf') else 0
        i += 100
        if i % 2000 == 0:
            print('scanned up to', i, flush=True)
    return out


def save_photo(url, fn):
    for a in range(5):
        try:
            im = Image.open(io.BytesIO(S.get(url, timeout=30).content)).convert('RGB')
            if im.width > WIDTH:
                im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
            im.save(fn, 'WEBP', quality=64, method=5)
            return True
        except Exception:
            time.sleep(1 + a)
    return False


def fallback_name(text, sec_title):
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    tags = re.findall(r'#(\w+)', text)
    brand = next((l for l in lines if not l.startswith('#') and not l.lower().startswith('размер') and not l.startswith('New 20')), '')
    kind = next((t for t in tags if re.match(r'^[а-яё]+$', t.lower()) and t.lower() not in ('одежда', 'обувь')), '')
    if not brand:
        brand = next((t.replace('_', ' ').title() for t in tags if re.match(r'^[a-z_&]+$', t.lower())), '')
    if kind and brand:
        return f'{kind.capitalize()} {brand}'
    if brand:
        return f'{sec_title} · {brand}'
    return sec_title


def ai_name(path, text, sec_title):
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        return None
    b64 = base64.b64encode(open(path, 'rb').read()).decode()
    prompt = ('Это фото товара из каталога люксовых вещей. Раздел: ' + sec_title + '. Подпись поста: ' + (text or 'нет') +
              '. Дай короткое название товара по-русски: тип + бренд + модель (если узнаёшь) + цвет, например '
              '«Сумка Chanel Classic Flap бежевая» или «Лоферы Loro Piana Summer Charms серые». '
              'Не выдумывай модель, если не уверен. Ответь только названием, без кавычек.')
    try:
        r = requests.post('https://api.anthropic.com/v1/messages', timeout=60, headers={
            'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
            json={'model': os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5'), 'max_tokens': 60, 'messages': [{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/webp', 'data': b64}},
                {'type': 'text', 'text': prompt}]}]})
        t = r.json()['content'][0]['text'].strip().strip('«»"')
        return t[:90] if t else None
    except Exception as e:
        print('ai name failed', e, flush=True)
        return None


def sizes(t):
    for l in t.split('\n'):
        if l.strip().lower().startswith('размер'):
            return l.strip()
    return ''


def main():
    tmap = {t: s['k'] for s in SECTIONS for t in s['topics']}
    stitle = {s['k']: s['t'] for s in SECTIONS}
    names = json.load(open(NAMES, encoding='utf-8')) if os.path.exists(NAMES) else {}
    data = json.load(open(DATA, encoding='utf-8')) if os.path.exists(DATA) else {'items': [], 'maxid': 0}
    full = not data['items'] or '--full' in sys.argv
    raw = scan(1 if full else data['maxid'] + 1, 400 if full else 150)
    have = {it['id'] for it in data['items']}
    albums, maxid = {}, data['maxid']
    unknown = set()
    for d in raw.values():
        if d.get('nf') or d.get('err'):
            continue
        pids = [p for p, _ in d['photos']] or [d['id']]
        maxid = max([maxid, d['id']] + pids)
        aid = min(pids)
        if aid in have or (not full and aid <= data['maxid']):
            continue
        if d['reply'] not in tmap:
            if d['reply'] and d['photos'] and d['reply'] not in IGNORED_TOPICS:
                unknown.add(d['reply'])
            continue
        if not d['photos']:
            continue
        d['aid'] = aid
        albums[aid] = d
    new = []
    os.makedirs(os.path.join(SITE, 'p'), exist_ok=True)

    def process(d):
        aid = d['aid']
        folder = os.path.join(SITE, 'p', str(aid))
        os.makedirs(folder, exist_ok=True)
        n = 0
        for _, u in d['photos'][:MAX_PHOTOS]:
            if save_photo(u, os.path.join(folder, f'{n}.webp')):
                n += 1
        return d, n

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(process, sorted(albums.values(), key=lambda x: x['aid'])))
    for d, n in results:
        if n == 0:
            continue
        aid, k = d['aid'], tmap[d['reply']]
        nm = names.get(str(aid))
        if not nm:
            nm = ('Видеообзор' if k == 'reviews' else
                  ai_name(os.path.join(SITE, 'p', str(aid), '0.webp'), d['text'], stitle[k]) or fallback_name(d['text'], stitle[k]))
            names[str(aid)] = nm
        new.append({'id': aid, 's': k, 'n': nm, 'z': sizes(d['text']), 'd': d['date'], 'v': 1 if d['video'] else 0, 'k': n})
    data['items'] = [it for it in new + data['items'] if it['s'] in stitle]
    data['items'].sort(key=lambda it: -it['id'])
    data['maxid'] = maxid
    data['sections'] = [{'g': s['g'], 'k': s['k'], 't': s['t']} for s in SECTIONS]
    data['updated'] = datetime.datetime.utcnow().strftime('%d.%m.%Y %H:%M UTC')
    data['unknown_topics'] = sorted(unknown)
    json.dump(data, open(DATA, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    json.dump(names, open(NAMES, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    print(json.dumps({'added': len(new), 'total': len(data['items']), 'maxid': maxid, 'unknown_topics': sorted(unknown)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
