#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twitter / X → Markdown
======================
Дополнение к «Архиватору ссылок»: принимает ссылку на пост (твит)
и сохраняет заметку .md по тем же правилам, что для Telegram и
Instagram: текст (включая длинные посты и «статьи» X), автор, дата,
статистика, фото рядом с заметкой, расшифровка речи из видео
(Whisper), опрос, цитируемый пост и список всех найденных ссылок.

Работает без платного API Твиттера: данные берутся через открытые
зеркала FxTwitter и VxTwitter. Если пост с видео не отдался —
запасной способ через yt-dlp (как у Instagram).

Отдельный запуск:   python twitter_archiver.py <ссылка> [папка]
Внутри программы:   просто вставьте ссылку вида
                    https://x.com/user/status/123 в общее поле —
                    archiver.py передаст её сюда автоматически.
"""

import os
import re
import shutil
import tempfile
import datetime
import urllib.parse

from archiver import (
    sanitize, unique_path, extract_links, links_section, now_str,
    try_analyze, maybe_translate, transcribe_file, download_file,
    compress_and_keep_video, video_file_section, process_video,
)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# домены, которые считаем «твиттерными» (включая зеркала)
TW_HOSTS = ("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com",
            "fixupx.com", "fixvx.com")

PATH_RE = re.compile(
    r"/(?:i/web/status|([A-Za-z0-9_]{1,15})/status(?:es)?)/(\d+)")


def parse_tweet_url(url: str):
    """Возвращает (имя_пользователя_или_None, id_поста) или бросает ошибку."""
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    for pref in ("www.", "mobile.", "m."):
        if host.startswith(pref):
            host = host[len(pref):]
    ok = host in TW_HOSTS or host.startswith("nitter.")
    m = PATH_RE.search(p.path)
    if not (ok and m):
        raise RuntimeError(
            "Не удалось разобрать ссылку Twitter/X. Нужна ссылка на "
            "конкретный пост, например https://x.com/user/status/123")
    return m.group(1), m.group(2)


# ----------------------------------------------------------------------
# Получение данных поста (FxTwitter → VxTwitter)
# ----------------------------------------------------------------------

def _fmt_date(epoch) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(epoch)) \
            .strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _strip_trailing_tco(text: str, has_media: bool) -> str:
    """Убирает «хвостовые» служебные ссылки t.co, указывающие на медиа."""
    if not has_media or not text:
        return text
    return re.sub(r"(?:\s*https?://t\.co/\w+)+\s*$", "", text).rstrip()


def _from_fx(data: dict) -> dict:
    """Приводит ответ api.fxtwitter.com к общему виду."""
    t = data["tweet"]
    a = t.get("author") or {}
    media = t.get("media") or {}
    photos, videos = [], []
    for ph in media.get("photos") or []:
        u = ph.get("url")
        if u:
            photos.append(u)
    for v in media.get("videos") or []:
        u = v.get("url")
        if u:
            videos.append({"url": u, "type": v.get("type", "video")})
    quote = None
    q = t.get("quote")
    if q:
        qa = q.get("author") or {}
        quote = {"text": q.get("text", ""),
                 "name": qa.get("name", ""),
                 "screen_name": qa.get("screen_name", ""),
                 "url": q.get("url", "")}
    poll = None
    p = t.get("poll")
    if p and p.get("choices"):
        poll = {"choices": [{"label": c.get("label", ""),
                             "count": c.get("count", 0),
                             "percentage": c.get("percentage")}
                            for c in p["choices"]],
                "total": p.get("total_votes")}
    art = t.get("article") or {}
    article = None
    if art.get("title") or art.get("text") or art.get("preview_text"):
        article = {"title": art.get("title", ""),
                   "text": art.get("text") or art.get("preview_text") or ""}
    return {
        "text": t.get("text", ""),
        "name": a.get("name", ""),
        "screen_name": a.get("screen_name", ""),
        "date": _fmt_date(t.get("created_timestamp")),
        "likes": t.get("likes"), "retweets": t.get("retweets"),
        "replies": t.get("replies"), "views": t.get("views"),
        "photos": photos, "videos": videos,
        "quote": quote, "poll": poll, "article": article,
        "quote_url": "",
    }


def _from_vx(data: dict) -> dict:
    """Приводит ответ api.vxtwitter.com к общему виду."""
    photos, videos = [], []
    for m in data.get("media_extended") or []:
        u = m.get("url")
        if not u:
            continue
        if m.get("type") == "image":
            photos.append(u)
        else:  # video / gif
            videos.append({"url": u, "type": m.get("type", "video")})
    return {
        "text": data.get("text", ""),
        "name": data.get("user_name", ""),
        "screen_name": data.get("user_screen_name", ""),
        "date": _fmt_date(data.get("date_epoch")),
        "likes": data.get("likes"), "retweets": data.get("retweets"),
        "replies": data.get("replies"), "views": None,
        "photos": photos, "videos": videos,
        "quote": None, "poll": None, "article": None,
        "quote_url": data.get("qrtURL") or "",
    }


def fetch_tweet(screen_name, tweet_id, log, _with_quote: bool = True) -> dict:
    """Пробует получить пост через FxTwitter, затем через VxTwitter."""
    import requests
    sn = screen_name or "i"
    errors = []

    try:
        r = requests.get(f"https://api.fxtwitter.com/{sn}/status/{tweet_id}",
                         headers=HEADERS, timeout=30)
        j = r.json()
        if r.status_code == 200 and j.get("tweet"):
            return _from_fx(j)
        errors.append(f"FxTwitter: {j.get('message') or r.status_code}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"FxTwitter: {e}")

    try:
        r = requests.get(f"https://api.vxtwitter.com/{sn}/status/{tweet_id}",
                         headers=HEADERS, timeout=30)
        j = r.json()
        if r.status_code == 200 and j.get("text") is not None:
            tw = _from_vx(j)
            # у VxTwitter цитата приходит только ссылкой — дотягиваем текст
            if _with_quote and tw["quote_url"]:
                try:
                    qsn, qid = parse_tweet_url(tw["quote_url"])
                    q = fetch_tweet(qsn, qid, log, _with_quote=False)
                    tw["quote"] = {"text": q["text"], "name": q["name"],
                                   "screen_name": q["screen_name"],
                                   "url": tw["quote_url"]}
                except Exception:  # noqa: BLE001
                    pass
            return tw
        errors.append(f"VxTwitter: статус {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"VxTwitter: {e}")

    raise RuntimeError("Пост недоступен (удалён, приватный аккаунт или "
                       "сервисы не отвечают). " + "; ".join(errors))


# ----------------------------------------------------------------------
# Вспомогательное
# ----------------------------------------------------------------------

def _orig_quality(img_url: str) -> str:
    """Для картинок pbs.twimg.com запрашивает исходное качество."""
    if "pbs.twimg.com" in img_url and "name=" not in img_url:
        sep = "&" if "?" in img_url else "?"
        return img_url + sep + "name=orig"
    return img_url


def _photo_ext(img_url: str) -> str:
    p = urllib.parse.urlparse(img_url)
    ext = os.path.splitext(p.path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return ext
    fmt = urllib.parse.parse_qs(p.query).get("format", [""])[0]
    return f".{fmt}" if fmt else ".jpg"


def _num(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return ""


# ----------------------------------------------------------------------
# Основная обработка
# ----------------------------------------------------------------------

def process_twitter(url: str, out_root: str, log, model_size: str = "base",
                    save_video: bool = True, target_lang: str = "") -> str:
    screen_name, tweet_id = parse_tweet_url(url)

    log(f"Загружаю пост {tweet_id}" +
        (f" от @{screen_name}..." if screen_name else "..."))
    try:
        tw = fetch_tweet(screen_name, tweet_id, log)
    except Exception as e:  # noqa: BLE001
        log(f"Не удалось получить пост через FxTwitter/VxTwitter: {e}")
        log("Пробую запасной способ (yt-dlp)...")
        last_err = None
        for browser in (None, "chrome", "firefox", "edge"):
            try:
                if browser:
                    log(f"...с куки из браузера {browser}")
                extra = {"cookiesfrombrowser": (browser,)} if browser else {}
                return process_video(url, out_root, log, model_size,
                                     ydl_extra=extra, save_video=save_video,
                                     target_lang=target_lang)
            except Exception as e2:  # noqa: BLE001
                last_err = e2
        raise RuntimeError(
            "Twitter/X не отдал пост. Что попробовать по порядку:\n"
            "  1) проверьте, что ссылка ведёт на существующий публичный "
            "пост;\n"
            "  2) обновите библиотеки:  pip install -U requests yt-dlp\n"
            "  3) откройте x.com в Chrome и войдите в аккаунт — тогда "
            "сработает запасной способ через куки браузера.\n"
            f"Ошибка сервисов: {e}\n"
            f"Ошибка запасного способа: {last_err}")

    screen_name = tw["screen_name"] or screen_name or "i"
    src_url = f"https://x.com/{screen_name}/status/{tweet_id}"
    has_media = bool(tw["photos"] or tw["videos"])
    post_text = _strip_trailing_tco(tw["text"], has_media)

    # заголовок файла — первая строка текста (как у Telegram)
    first_line = (post_text.splitlines() or [""])[0].strip()
    if not first_line and tw["article"]:
        first_line = tw["article"]["title"].strip()
    title = sanitize(first_line, 80) if first_line else f"Пост {tweet_id}"

    quote_text = tw["quote"]["text"] if tw["quote"] else ""
    body_for_ai = "\n\n".join(x for x in (
        post_text,
        tw["article"]["text"] if tw["article"] else "",
        quote_text) if x)

    analysis = try_analyze("пост", first_line or title,
                           f"{tw['name']} (@{screen_name})",
                           "", body_for_ai, log)
    if analysis:
        topic = sanitize(analysis.get("topic") or "Разное")
        sub = sanitize(analysis.get("subtopic") or "") \
            if (analysis.get("subtopic") or "").strip() else ""
        folder = os.path.join(out_root, topic, sub) if sub \
            else os.path.join(out_root, topic)
        if analysis.get("title"):
            title = sanitize(analysis["title"], 80)
    else:
        import analyzer
        kw_topic = analyzer.detect_topic_keywords(body_for_ai)
        folder = os.path.join(out_root, sanitize(kw_topic))
    os.makedirs(folder, exist_ok=True)
    md_path = unique_path(os.path.join(folder, f"{title}.md"))
    base_name = os.path.splitext(os.path.basename(md_path))[0]

    # ---------- фото ----------
    photo_md = []
    for i, img_url in enumerate(tw["photos"], start=1):
        try:
            log(f"Скачиваю фото {i}...")
            img_name = f"{base_name}_фото_{i}{_photo_ext(img_url)}"
            download_file(_orig_quality(img_url),
                          os.path.join(folder, img_name))
            photo_md.append(f"![Фото {i}]({img_name})")
        except Exception as e:  # noqa: BLE001
            log(f"Не удалось скачать фото {i}: {e}")

    # ---------- видео и GIF: скачиваем, расшифровываем, сохраняем ----------
    transcripts, saved_videos = [], []
    if tw["videos"]:
        tmpdir = tempfile.mkdtemp(prefix="archiver_tw_")
        try:
            for vi, v in enumerate(tw["videos"], start=1):
                try:
                    log(f"Скачиваю видео {vi}...")
                    vpath = os.path.join(tmpdir, f"video_{vi}.mp4")
                    download_file(v["url"], vpath)
                except Exception as e:  # noqa: BLE001
                    log(f"Не удалось скачать видео {vi}: {e}")
                    continue
                if v.get("type") != "gif":   # у GIF звука нет
                    log(f"Расшифровываю речь из видео {vi} (Whisper)...")
                    try:
                        t = transcribe_file(vpath, model_size, log)
                        transcripts.append((vi, t))
                    except Exception as e:  # noqa: BLE001
                        log(f"Расшифровка видео {vi} не удалась: {e}")
                if save_video:
                    try:
                        name = f"{base_name}_видео_{vi}" \
                            if len(tw["videos"]) > 1 else base_name
                        saved_videos.append(
                            compress_and_keep_video(vpath, folder, name, log))
                    except Exception as e:  # noqa: BLE001
                        log(f"Не удалось сохранить видео {vi}: {e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    all_transcript = "\n\n".join(t for _, t in transcripts)
    links = extract_links(post_text, quote_text,
                          tw["article"]["text"] if tw["article"] else "",
                          all_transcript)
    links = [l for l in links if "//t.co/" not in l] or links

    # ---------- сборка заметки ----------
    md = [f"# {first_line or 'Пост ' + tweet_id}", ""]
    md.append(f"**Источник:** {src_url}  ")
    md.append(f"**Автор:** {tw['name']} (@{screen_name})  ")
    if tw["date"]:
        md.append(f"**Дата публикации:** {tw['date']}  ")
    stats = [f"❤ {_num(tw['likes'])}" if _num(tw['likes']) else "",
             f"🔁 {_num(tw['retweets'])}" if _num(tw['retweets']) else "",
             f"💬 {_num(tw['replies'])}" if _num(tw['replies']) else "",
             f"👁 {_num(tw['views'])}" if _num(tw['views']) else ""]
    stats = [s for s in stats if s]
    if stats:
        md.append("**Статистика:** " + " · ".join(stats) + "  ")
    if analysis:
        theme = analysis.get("topic", "")
        if analysis.get("subtopic"):
            theme += f" / {analysis['subtopic']}"
        md.append(f"**Тема:** {theme}  ")
    md.append(f"**Сохранено:** {now_str()}")
    md.append("")
    if analysis and (analysis.get("summary_md") or "").strip():
        md += ["## Конспект", "",
               maybe_translate(analysis["summary_md"].strip(),
                               target_lang, log), ""]
    if photo_md:
        md += ["## Фото", ""] + photo_md + [""]
    md += ["## Текст поста", "",
           maybe_translate(post_text, target_lang, log)
           or "_Текст отсутствует._", ""]
    if tw["article"]:
        md += ["## Статья (длинный материал X)", ""]
        if tw["article"]["title"]:
            md.append(f"**Заголовок:** {tw['article']['title']}")
            md.append("")
        if tw["article"]["text"]:
            md.append(maybe_translate(tw["article"]["text"].strip(),
                                      target_lang, log))
            md.append("")
    if tw["poll"]:
        md += ["## Опрос", ""]
        for c in tw["poll"]["choices"]:
            pct = f" ({c['percentage']}%)" \
                if c.get("percentage") is not None else ""
            md.append(f"- {c['label']} — {_num(c['count'])} голосов{pct}")
        if tw["poll"].get("total") is not None:
            md.append(f"\nВсего голосов: {_num(tw['poll']['total'])}")
        md.append("")
    if tw["quote"]:
        q = tw["quote"]
        md += ["## Цитируемый пост", ""]
        who = f"**{q['name']} (@{q['screen_name']})**"
        md.append(who + (f" — {q['url']}" if q.get("url") else ""))
        md.append("")
        qt = maybe_translate(q["text"].strip(), target_lang, log)
        md += [f"> {line}" for line in (qt or "_Без текста._").splitlines()]
        md.append("")
    if transcripts:
        md += ["## Текст видео (расшифровка)", ""]
        for vi, t in transcripts:
            if len(transcripts) > 1:
                md += [f"### Видео {vi}", ""]
            md.append(t.strip() or "_Речь не обнаружена._")
            md.append("")
    if len(saved_videos) == 1:
        md.append(video_file_section(*saved_videos[0]))
    elif saved_videos:
        md += ["## Видео-файлы (архивные копии)", ""]
        md += [f"- `{n}` — {d}" for n, d in saved_videos]
        md.append("")
    md.append(links_section(links))

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return md_path


# ----------------------------------------------------------------------
# Запуск из командной строки
# ----------------------------------------------------------------------

def _cli():
    import sys
    if len(sys.argv) < 2:
        print("Использование:  python twitter_archiver.py <ссылка> [папка]")
        print("Пример:  python twitter_archiver.py "
              "https://x.com/user/status/123 Архив")
        return
    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 \
        else os.path.join(os.path.expanduser("~"), "Архив")
    path = process_twitter(url, out, print)
    print(f"\nГотово! Заметка: {path}")


if __name__ == "__main__":
    _cli()
