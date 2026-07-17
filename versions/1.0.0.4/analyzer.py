#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Интеллектуальный анализ содержимого — ПОЛНОСТЬЮ ЛОКАЛЬНО через Ollama.
Определяет тему (для сортировки по папкам), делает чёткий конспект
без воды и находит моменты видео для скриншотов.

Ollama — бесплатная программа, запускающая ИИ на вашем компьютере
без интернета: https://ollama.com
После установки выполните один раз:  ollama pull qwen2.5:7b

Если Ollama не установлен, архиватор всё равно работает: темы
определяются по словарю ключевых слов, а вместо ИИ-конспекта
сохраняется очищенная расшифровка.
"""

import json
import re

OLLAMA_URL = "http://localhost:11434"

TOPICS = [
    "3D печать", "IT и программирование", "Технологии и гаджеты",
    "Игры", "Наука и образование", "Бизнес и финансы",
    "Здоровье и спорт", "Дом и быт", "Авто",
    "Творчество и дизайн", "Кулинария", "Путешествия",
    "Новости и общество", "Разное",
]

# словарь для определения темы БЕЗ ИИ (запасной вариант)
TOPIC_KEYWORDS = {
    "3D печать": ["3d принтер", "3д принтер", "3d печат", "3д печат",
                  "филамент", "слайсер", " pla", "petg", "экструдер",
                  "сопло", "печатн", "prusa", "bambu", "ender"],
    "IT и программирование": ["программирован", "python", "javascript",
                              "линукс", "linux", "сервер", "git",
                              "скрипт", "разработ", "терминал",
                              "командная строка", " api", "база данных",
                              "нейросет", "докер", "docker", "код"],
    "Технологии и гаджеты": ["смартфон", "гаджет", "процессор",
                             "видеокарт", "ноутбук", "steam deck",
                             "windows", "устройств", "прошивк",
                             "андроид", "iphone", "приложени"],
    "Игры": ["геймплей", "прохожден", "playstation", "xbox", "игров",
             "шутер", "рпг", "инди-игр", "катсцен"],
    "Наука и образование": ["физик", "математ", "истори", "биолог",
                            "исследован", "учён", "наук", "экспери"],
    "Бизнес и финансы": ["инвест", "бизнес", "акци", "крипт",
                         "маркетинг", "налог", "стартап", "доход"],
    "Здоровье и спорт": ["тренир", "здоров", "питани", "похуден",
                         "врач", "мышц", "бег ", "сон "],
    "Дом и быт": ["ремонт", "сад", "огород", "растен", "томат",
                  "удобрен", "дач", "уборк", "интерьер", "инструмент"],
    "Авто": ["автомобил", "двигател", "машин", "мотор", "кузов"],
    "Творчество и дизайн": ["дизайн", "рисован", "фотограф", "монтаж",
                            "иллюстрац", "музык", "blender", "figma"],
    "Кулинария": ["рецепт", "ингредиент", "духовк", "тесто", "блюдо",
                  "готовим", "маринад"],
    "Путешествия": ["путешеств", "виз", "отел", "маршрут", "туризм"],
}


def detect_topic_keywords(text: str) -> str:
    """Определяет тему по ключевым словам (без ИИ)."""
    low = (text or "").lower()
    best, score = "Разное", 0
    for topic, keys in TOPIC_KEYWORDS.items():
        s = sum(low.count(k) for k in keys)
        if s > score:
            best, score = topic, s
    return best if score >= 2 else "Разное"


# ---------------------- Ollama ----------------------

def status() -> tuple:
    """Точное состояние Ollama:
    ("ok", имя_модели)  — всё готово
    ("no_model", "")    — Ollama запущен, но модель не скачана
    ("offline", "")     — Ollama не запущен или не установлен
    """
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = [m.get("name", "") for m in r.json().get("models", [])]
        if not models:
            return ("no_model", "")
        for pref in ("qwen", "llama", "mistral", "gemma"):
            for m in models:
                if pref in m.lower():
                    return ("ok", m)
        return ("ok", models[0])
    except Exception:  # noqa: BLE001
        return ("offline", "")


def available() -> str:
    """Имя локальной модели Ollama или '' если не запущен."""
    st, model = status()
    return model if st == "ok" else ""


# совместимость со старым кодом
def get_api_key() -> str:
    return available()


# ---------------------- разбор ответа ----------------------

def parse_json_answer(text: str) -> dict:
    """Достаёт JSON из ответа модели (терпимо к ```json ... ```)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе модели нет JSON")
    return json.loads(text[start:end + 1])


# ---------------------- запросы к локальной модели ----------------------

def _ask(model: str, prompt: str, want_json: bool = False,
         temperature: float = 0.3, num_ctx: int = 12288) -> str:
    """Один запрос к Ollama."""
    import requests
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if want_json:
        payload["format"] = "json"
    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload,
                      timeout=1800)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def _meta_prompt(kind, title, author, body):
    topics = ", ".join(f"«{t}»" for t in TOPICS)
    return f"""Определи тему материала для сортировки по папкам.
Тип: {kind}. Название: {title}. Автор: {author}.
Начало содержимого:
{body[:4000]}

Верни ТОЛЬКО JSON:
{{"topic": "одна тема из списка: {topics} — или новая короткая",
"subtopic": "уточнение в 1-3 слова (программа, устройство, язык) или пустая строка",
"title": "чёткое информативное название заметки на русском; иностранные названия и термины в нём — латиницей в оригинале (Steam Deck, Docker), без транслитерации"}}"""


def _summary_prompt(kind, title, author, description, body):
    if kind == "видео":
        return f"""Ты пишешь конспект видео для личной базы знаний.
Название: {title}. Автор: {author}.
Описание автора: {(description or "(нет)")[:1500]}
Транскрипт с таймкодами [мм:сс]:
{body}

Напиши ПОДРОБНЫЙ структурированный конспект на русском в Markdown.
Обязательная структура:

## Суть
2-4 предложения: о чём видео и главный вывод.

## Главное
Маркированный список ключевых тезисов, советов и фактов — не менее 5-10 пунктов для видео длиннее 5 минут. У важных пунктов ставь таймкод [мм:сс] из транскрипта. Сохраняй конкретные цифры, значения, названия.

## Пошаговые действия
Если автор объясняет, что и куда нажимать/вводить/настраивать — нумерованные шаги. Каждую команду, кнопку, пункт меню и параметр оформи в `бэктиках`. Если пошаговых инструкций нет — напиши одну строку: «Пошаговых инструкций в видео нет».

## Команды и настройки
Все команды, параметры и конкретные значения из видео — в блоке кода ```. Если их нет — пропусти раздел целиком.

## Инструменты и ссылки
Упомянутые программы, устройства, сайты, ссылки.

Правила: строго по делу, без воды и слов-паразитов, ничего не выдумывай — только то, что есть в транскрипте. Иностранные слова, названия программ, брендов, сервисов, устройств и команды пиши в оригинале ЛАТИНИЦЕЙ (Steam Deck, Docker, Windows), не транслитерируй кириллицей; при первом упоминании можно дать русский перевод в скобках. Ответ — только Markdown конспекта, без вступлений."""
    if kind == "статья":
        return f"""Сделай конспект статьи «{title}» ({author}) для базы знаний.
Текст статьи:
{body}

Markdown-структура: ## Суть (2-3 предложения), ## Главное (тезисы и цифры списком), ## Команды и настройки (блок ``` если есть код/команды, иначе пропусти), ## Инструменты и ссылки. Подробно, но строго по делу, ничего не выдумывай. Ответ — только Markdown."""
    return f"""Перескажи суть поста в 1-3 предложениях (или верни пустую строку, если пост короче 300 знаков). Пост «{title}» от {author}:
{body[:4000]}
Ответ — только текст."""


def _shots_prompt(body):
    return f"""Ниже транскрипт видео с таймкодами [мм:сс]. Найди моменты, где автор показывает что-то НА ЭКРАНЕ и говорит «нажмите сюда», «вот здесь», «открываем», «видите это меню», «вводим» и т.п.

{body}

Верни ТОЛЬКО JSON вида:
{{"screenshots": [{{"n": 1, "timecode": "мм:сс", "caption": "что видно на кадре и зачем", "target": "конкретный элемент на экране, который нужно обвести (кнопка, поле, строка меню)"}}]}}
ВАЖНО: timecode — ТОЧНЫЙ таймкод из транскрипта той фразы, где автор показывает это на экране; скопируй его из квадратных скобок как есть, ничего не пересчитывай. Максимум 8 моментов, только реально полезные (интерфейс, настройки, команды, схемы). Если таких нет — {{"screenshots": []}}"""


def analyze(kind: str, title: str, author: str, description: str,
            body: str, log) -> dict | None:
    """Возвращает dict {topic, subtopic, title, summary_md, screenshots}
    или None, если Ollama не запущен. Три простых запроса — так локальные
    модели отвечают подробнее и надёжнее, чем одним большим JSON."""
    model = available()
    if not model:
        return None

    body_cut = body[:20000]
    data = {"topic": "", "subtopic": "", "title": title,
            "summary_md": "", "screenshots": []}

    # 1. тема / подтема / название
    try:
        meta = parse_json_answer(
            _ask(model, _meta_prompt(kind, title, author, body_cut),
                 want_json=True, temperature=0.2))
        for k in ("topic", "subtopic", "title"):
            if str(meta.get(k) or "").strip():
                data[k] = str(meta[k]).strip()
    except Exception as e:  # noqa: BLE001
        log(f"ИИ не определил тему ({e}) — возьму по словарю.")
    if not data["topic"]:
        data["topic"] = detect_topic_keywords(f"{title}\n{body_cut[:5000]}")
    log(f"Тема: {data['topic']}"
        + (f" / {data['subtopic']}" if data["subtopic"] else ""))

    # 2. подробный конспект (свободный Markdown — без JSON-ограничений)
    log("Пишу подробный конспект... (на слабом ПК — несколько минут)")
    try:
        summary = _ask(model, _summary_prompt(kind, title, author,
                                              description, body_cut))
        summary = re.sub(r"^```(?:markdown|md)?\s*", "", summary)
        summary = re.sub(r"\s*```$", "", summary)
        data["summary_md"] = summary.strip()
    except Exception as e:  # noqa: BLE001
        log(f"Конспект через ИИ не получился: {e}")

    # 3. моменты для скриншотов (только видео)
    if kind == "видео":
        log("Выбираю моменты для скриншотов...")
        try:
            js = parse_json_answer(_ask(model, _shots_prompt(body_cut),
                                        want_json=True, temperature=0.2))
            shots = js.get("screenshots") or []
            data["screenshots"] = [s for s in shots
                                   if isinstance(s, dict)][:8]
            log(f"ИИ предложил кадров: {len(data['screenshots'])}.")
        except Exception as e:  # noqa: BLE001
            log(f"ИИ не выбрал кадры ({e}) — возьму по ключевым словам.")
    return data


# ---------------------- vision-модель (кадры, OCR) ----------------------

_VISION_HINTS = ("llava", "vision", "moondream", "minicpm", "-vl", "vl:",
                 "bakllava", "qwen2-vl", "qwen2.5vl")


def vision_model() -> str:
    """Имя локальной модели Ollama, умеющей смотреть картинки, или ''."""
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = [m.get("name", "") for m in r.json().get("models", [])]
        for m in models:
            if any(h in m.lower() for h in _VISION_HINTS):
                return m
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _ask_image(model: str, prompt: str, image_path: str,
               want_json: bool = False) -> str:
    """Запрос к vision-модели с картинкой."""
    import base64
    import requests
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "images": [b64],
        "options": {"temperature": 0.1},
    }
    if want_json:
        payload["format"] = "json"
    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload,
                      timeout=600)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def locate_on_image(image_path: str, what: str):
    """Координаты элемента на кадре [x1,y1,x2,y2] в процентах, или None."""
    model = vision_model()
    if not model or not what:
        return None
    prompt = f"""На изображении найди: {what}.
Верни ТОЛЬКО JSON {{"found": true, "box": [x1, y1, x2, y2]}} — координаты
прямоугольника вокруг этого элемента в ПРОЦЕНТАХ от ширины и высоты
изображения (числа 0-100, x1<x2, y1<y2). Если элемента нет —
{{"found": false, "box": []}}"""
    try:
        d = parse_json_answer(_ask_image(model, prompt, image_path,
                                         want_json=True))
        box = d.get("box") or []
        if not d.get("found") or len(box) != 4:
            return None
        vals = [float(v) for v in box]
        # некоторые модели отвечают долями 0-1 вместо процентов
        if max(vals) <= 1.5:
            vals = [v * 100.0 for v in vals]
        vals = [max(0.0, min(100.0, v)) for v in vals]
        # вырожденная рамка = модель не нашла
        if abs(vals[2] - vals[0]) < 1.0 or abs(vals[3] - vals[1]) < 1.0:
            return None
        return vals
    except Exception:  # noqa: BLE001
        return None


def ocr_image(image_path: str) -> str:
    """Дословный текст с кадра (надписи, названия, ссылки)."""
    model = vision_model()
    if not model:
        return ""
    prompt = ("Выпиши ДОСЛОВНО весь текст, видимый на изображении: "
              "надписи, заголовки, названия, команды, ссылки. Каждый "
              "элемент с новой строки, ничего не переводи и не описывай "
              "картинку словами. Если текста нет — верни пустую строку.")
    try:
        return _ask_image(model, prompt, image_path)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------- выжимка по ссылке ----------------------

def link_note(link: str, context: str, page_title: str,
              page_text: str) -> str:
    """Выжимает из страницы по ссылке то, ради чего её упомянули."""
    model = available()
    if not model:
        return ""
    prompt = f"""В материале упомянута ссылка {link}.
Контекст упоминания: «{context[:400]}»
Название страницы: {page_title}
Текст страницы:
{page_text[:6000]}

Извлеки в 2-8 строках Markdown именно то, ради чего эту ссылку дали: команду, инструкцию, характеристики, цену, файл, таблицу. Конкретика важнее пересказа. Если страница не по делу — одно предложение, о чём она. Ответ — только текст выжимки, без вступлений."""
    try:
        return _ask(model, prompt, temperature=0.2).strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------- перевод ----------------------

def translate_text(text: str, target_lang: str) -> str:
    """Очень точный перевод на любой язык (локальной моделью), кусками.
    Разметка Markdown, таймкоды и код сохраняются."""
    model = available()
    if not model or not (text or "").strip():
        return ""
    # режем по абзацам на куски ~2500 знаков — так точнее и надёжнее
    chunks, buf, size = [], [], 0
    for para in text.split("\n\n"):
        if size + len(para) > 2500 and buf:
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para)
    if buf:
        chunks.append("\n\n".join(buf))

    out = []
    for ch in chunks:
        prompt = f"""Переведи текст на язык: {target_lang}.
Требования: МАКСИМАЛЬНО точный перевод; ничего не добавлять, не сокращать и не пропускать; сохранить разметку Markdown, заголовки, списки, таймкоды [мм:сс] и структуру абзацев; код, команды и содержимое `бэктиков` и блоков ``` НЕ переводить; имена собственные, названия программ, брендов и сервисов оставить в оригинале латиницей.
Текст:
{ch}

Ответ — только перевод, без пояснений."""
        out.append(_ask(model, prompt, temperature=0.1))
    return "\n\n".join(p for p in out if p).strip()
