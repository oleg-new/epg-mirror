#!/usr/bin/env python3
"""
Зеркалирование EPG + иконок каналов (объединённая версия).

Переменные окружения:
  SRC_URL     - источник EPG (по умолчанию http://epg.one/epg2.xml.gz)
  ICON_BASE   - базовый URL, под которым будут доступны иконки
  ICON_SCOPE  - "channel" (иконки каналов) или "all" (ещё и иконки передач)
  OUT_FILE    - путь итогового .gz
  MIN_CHANNELS - минимум каналов в источнике (защита от пустого/битого файла)
"""
import gzip
import hashlib
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SRC_URL = os.environ.get("SRC_URL", "http://epg.one/epg2.xml.gz")
ICON_BASE = os.environ.get(
    "ICON_BASE",
    "https://raw.githubusercontent.com/oleg-new/epg-mirror/main/icons",
).rstrip("/")
ICON_SCOPE = os.environ.get("ICON_SCOPE", "channel")
OUT_FILE = os.environ.get("OUT_FILE", "out/epg2.xml.gz")
MIN_CHANNELS = int(os.environ.get("MIN_CHANNELS", "10"))
ICON_DIR = "icons"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
MAX_ICON_SIZE = 3 * 1024 * 1024          # 3 МБ на иконку
MAX_GZ_SIZE = 95 * 1024 * 1024           # лимит GitHub 100 МБ, оставляем запас
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


SESSION = make_session()


def download_epg(dst_xml: str) -> None:
    """Потоково скачивает .gz и распаковывает в dst_xml."""
    print(f"Downloading {SRC_URL}")
    with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
        tmp_gz = tmp.name
    try:
        with SESSION.get(SRC_URL, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            with open(tmp_gz, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        size = os.path.getsize(tmp_gz)
        print(f"Downloaded: {size / 1024 / 1024:.1f} MB")

        with open(tmp_gz, "rb") as f:
            magic = f.read(2)
        if magic == b"\x1f\x8b":
            with gzip.open(tmp_gz, "rb") as f_in, open(dst_xml, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        else:
            shutil.copyfile(tmp_gz, dst_xml)  # отдали несжатый XML
    finally:
        os.remove(tmp_gz)


def icon_filename(url: str) -> str:
    """Читаемое безопасное имя: <slug>_<sha1[:10]><ext>."""
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    stem, ext = os.path.splitext(os.path.basename(path))
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        ext = ".png"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:40] or "icon"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{slug}_{digest}{ext}"


def download_icon(url: str):
    """Возвращает (url, имя_файла | None)."""
    name = icon_filename(url)
    target = os.path.join(ICON_DIR, name)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return url, name
    try:
        r = SESSION.get(url, timeout=(10, 30))
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").lower()
        data = r.content
        if "text/html" in ctype:
            raise ValueError("server returned HTML instead of image")
        if not data or len(data) > MAX_ICON_SIZE:
            raise ValueError(f"bad size: {len(data)}")
        with open(target, "wb") as f:
            f.write(data)
        return url, name
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] icon failed: {url} ({e})", file=sys.stderr)
        return url, None


def get_icon_elements(root):
    if ICON_SCOPE == "all":
        return root.findall(".//icon")
    return root.findall("channel/icon")


def main() -> None:
    os.makedirs(ICON_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)

    work_dir = tempfile.mkdtemp(prefix="epg_")
    xml_path = os.path.join(work_dir, "epg.xml")
    try:
        # 1. Скачивание и распаковка
        download_epg(xml_path)
        print(f"Unpacked: {os.path.getsize(xml_path) / 1024 / 1024:.1f} MB")

        # 2. СТРОГИЙ парсинг: битый файл = ошибка, старое зеркало не трогаем
        parser = etree.XMLParser(huge_tree=True, recover=False)
        tree = etree.parse(xml_path, parser)
        root = tree.getroot()
        if root.tag != "tv":
            sys.exit(f"ERROR: root element is <{root.tag}>, expected <tv>")

        channels = len(root.findall("channel"))
        programmes = len(root.findall("programme"))
        print(f"Channels: {channels}, programmes: {programmes}")
        if channels < MIN_CHANNELS or programmes == 0:
            sys.exit("ERROR: source EPG looks empty or incomplete")

        # 3. Уникальные URL иконок (с учётом относительных ссылок)
        icon_elems = get_icon_elements(root)
        urls = set()
        for el in icon_elems:
            src = (el.get("src") or "").strip()
            if not src:
                continue
            absolute = urllib.parse.urljoin(SRC_URL, src)
            if urllib.parse.urlparse(absolute).scheme in ("http", "https"):
                urls.add(absolute)
        print(f"Icon tags: {len(icon_elems)}, unique URLs: {len(urls)} (scope={ICON_SCOPE})")

        # 4. Параллельное скачивание
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = dict(ex.map(download_icon, sorted(urls)))
        ok = sum(1 for v in results.values() if v)
        print(f"Icons OK: {ok}, failed: {len(results) - ok}")

        # 5. Замена ссылок (только для успешно скачанных)
        replaced = 0
        for el in icon_elems:
            src = (el.get("src") or "").strip()
            if not src:
                continue
            name = results.get(urllib.parse.urljoin(SRC_URL, src))
            if name:
                el.set("src", f"{ICON_BASE}/{name}")
                replaced += 1
        print(f"Links replaced: {replaced}")

        # 6. Запись (DOCTYPE сохраняется) и сжатие
        out_xml = os.path.join(work_dir, "out.xml")
        tree.write(out_xml, encoding="utf-8", xml_declaration=True, pretty_print=False)

        tmp_out = OUT_FILE + ".tmp"
        with open(out_xml, "rb") as f_in, gzip.open(tmp_out, "wb", compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)

        gz_size = os.path.getsize(tmp_out)
        print(f"Result: {gz_size / 1024 / 1024:.1f} MB (gz)")
        if gz_size > MAX_GZ_SIZE:
            os.remove(tmp_out)
            sys.exit("ERROR: result exceeds GitHub 100 MB limit")

        os.replace(tmp_out, OUT_FILE)   # атомарная замена
        print(f"Written {OUT_FILE}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
