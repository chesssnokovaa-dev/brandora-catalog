# Brandora — каталог

Сайт-каталог по Telegram-группе [t.me/brandora_all](https://t.me/brandora_all).
Каждый день GitHub Actions забирает новые посты, скачивает фото и обновляет сайт.

- `site/` — сам сайт (index.html, data.json, фото в `site/p/<id>/`)
- `scripts/catalog.py` — сборщик
- `sections.json` — разделы сайта и соответствующие им ветки Telegram (id темы)
- `names.json` — названия товаров (можно править вручную)
- Секрет `ANTHROPIC_API_KEY` (необязательно) — чтобы новые товары получали названия по фото.
