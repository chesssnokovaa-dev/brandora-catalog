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


# Модели для названий по фото: сначала ANTHROPIC_MODEL (если задана), затем по очереди эти.
AI_MODELS = ['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-4-5-20251001']
AI_PHOTOS = 3        # сколько фото товара отправлять модели
AI_RENAME_LIMIT = 400  # сколько товаров с простым названием переименовывать за запуск
AI = {'models': None, 'off': False}

AI_PROMPT = (
    'Это фото одного товара из каталога люксовых вещей. Раздел каталога: {sec}. Подпись поста: {text}.\n'
    'Дай подробное название товара по-русски по схеме: тип + бренд + модель (только если уверен) + цвет/материал/деталь.\n'
    'Примеры правильного стиля: «Сумка Chanel Classic Flap бежевая», «Лоферы Loro Piana Summer Charms серые», '
    '«Пуховик Moncler чёрный с капюшоном», «Солнцезащитные очки Celine Triomphe черепаховые», '
    '«Ботильоны Hermes на каблуке с пряжкой Kelly (разные цвета)», «Костюм Miu Miu шерстяной (жакет + юбка) серый».\n'
    'Бренд бери из раздела, подписи поста или логотипа на фото. Если модель не узнаёшь — не выдумывай, пиши тип + бренд + цвет. '
    'Если на фото несколько расцветок — добавь «(разные цвета)» или перечисли цвета в скобках.\n'
    'Ответь только названием, без кавычек и пояснений.')


def ai_name(folder, text, sec_title):
    """Название по фото через Anthropic API. None — если ключа нет или API не ответило (тогда остаётся простое название)."""
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key or AI['off']:
        return None
    if AI['models'] is None:
        env = os.environ.get('ANTHROPIC_MODEL', '').strip()
        AI['models'] = ([env] if env else []) + [m for m in AI_MODELS if m != env]
    content = []
    for n in range(AI_PHOTOS):
        fn = os.path.join(folder, f'{n}.webp')
        if os.path.exists(fn):
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/webp',
                                                        'data': base64.b64encode(open(fn, 'rb').read()).decode()}})
    if not content:
        return None
    content.append({'type': 'text', 'text': AI_PROMPT.format(sec=sec_title, text=text or 'нет')})
    while AI['models']:
        model = AI['models'][0]
        for attempt in range(4):
            try:
                r = requests.post('https://api.anthropic.com/v1/messages', timeout=90, headers={
                    'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                    json={'model': model, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': content}]})
            except Exception as e:
                print(f'AI [{model}] сетевая ошибка: {e}', flush=True)
                time.sleep(2 + 3 * attempt)
                continue
            if r.status_code == 200:
                try:
                    t = ''.join(b.get('text', '') for b in r.json()['content']).strip().split('\n')[0].strip().strip('«»"\'')
                except Exception as e:
                    print(f'AI [{model}] непонятный ответ: {e} {r.text[:300]}', flush=True)
                    return None
                return t[:120] or None
            try:
                err = r.json().get('error', {})
            except Exception:
                err = {}
            msg = f"AI [{model}] HTTP {r.status_code} {err.get('type', '')}: {err.get('message', r.text[:300])}"
            print(msg, flush=True)
            if r.status_code in (401, 403):
                print('AI: ключ не принят — названия по фото отключены до следующего запуска', flush=True)
                AI['off'] = True
                return None
            if r.status_code == 404 or (r.status_code == 400 and 'model' in str(err.get('message', '')).lower()):
                break  # модель недоступна — пробуем следующую
            if r.status_code == 400:
                return None  # ошибка именно в этом запросе (например, фото) — оставляем простое название
            time.sleep(5 * (attempt + 1))  # 429 / 5xx / 529 — ждём и повторяем
        else:
            return None  # модель есть, но сейчас не отвечает — не сжигаем остальные модели
        print(f'AI: модель {model} недоступна, пробую следующую', flush=True)
        AI['models'].pop(0)
    print('AI: ни одна модель не доступна — названия по фото отключены до следующего запуска', flush=True)
    AI['off'] = True
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
        it = {'id': aid, 's': k, 'n': names.get(str(aid)), 'z': sizes(d['text']), 'd': d['date'], 'v': 1 if d['video'] else 0, 'k': n}
        if not it['n']:
            nm = 'Видеообзор' if k == 'reviews' else ai_name(os.path.join(SITE, 'p', str(aid)), d['text'], stitle[k])
            if nm:
                it['n'] = names[str(aid)] = nm
            else:
                # простое название из подписи; в names.json не пишем, чтобы потом переименовать по фото
                it['n'], it['f'] = fallback_name(d['text'], stitle[k]), 1
        new.append(it)
    data['items'] = [it for it in new + data['items'] if it['s'] in stitle]
    # Товары с простым названием (f=1) переименовываем по фото, если API доступно
    renamed = 0
    todo = sorted([it for it in data['items'] if it.get('f')], key=lambda it: -it['id'])[:AI_RENAME_LIMIT]
    if todo and os.environ.get('ANTHROPIC_API_KEY') and not AI['off']:
        def rename(it):
            h = parse(it['id'], fetch(it['id']))
            return it, ai_name(os.path.join(SITE, 'p', str(it['id'])), h.get('text', ''), stitle[it['s']])
        with ThreadPoolExecutor(4) as ex:
            for it, nm in ex.map(rename, todo):
                if nm:
                    print(f"переименован {it['id']}: {it['n']} → {nm}", flush=True)
                    it['n'] = names[str(it['id'])] = nm
                    it.pop('f', None)
                    renamed += 1
    flagged = sum(1 for it in data['items'] if it.get('f'))
    data['items'].sort(key=lambda it: -it['id'])
    data['maxid'] = maxid
    data['sections'] = [{'g': s['g'], 'k': s['k'], 't': s['t']} for s in SECTIONS]
    data['updated'] = datetime.datetime.utcnow().strftime('%d.%m.%Y %H:%M UTC')
    data['unknown_topics'] = sorted(unknown)
    json.dump(data, open(DATA, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    json.dump(names, open(NAMES, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    print(json.dumps({'added': len(new), 'renamed': renamed, 'still_simple': flagged, 'total': len(data['items']), 'maxid': maxid, 'unknown_topics': sorted(unknown)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
