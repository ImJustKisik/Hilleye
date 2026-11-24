"""Browse and download files from the IP-restricted SierraBay log directory."""

from contextlib import asynccontextmanager
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
import re
from time import monotonic
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from fastapi import HTTPException

from core.log_operations import finish_thread

ROOT_URL = "https://celadon.pro/game_servers/sierra_logs/"
ROOT_PATH = urlsplit(ROOT_URL).path
FILE_SUFFIXES = (".log", ".txt", ".zip", ".tar", ".tar.gz", ".tgz")


def _validate_path(path, *, directory):
    if not path and directory:
        return
    parts = path.removesuffix("/").split("/")
    if (
        len(path) > 1000
        or path.endswith("/") != directory
        or any(part in ("", ".", "..") for part in parts)
        or any(char in path for char in "%\\:")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or (not directory and not path.lower().endswith(FILE_SUFFIXES))
    ):
        raise ValueError("Недопустимый путь в каталоге Celadon.")


class _DirectoryLinks(HTMLParser):
    def __init__(self, directory_url, directory):
        super().__init__()
        self.directory_url = directory_url
        self.directory = directory
        self.entries = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        try:
            url = urlsplit(urljoin(self.directory_url, href))
            if url.scheme != "https" or url.netloc != "celadon.pro" or url.query or url.fragment:
                return
            path = unquote(url.path, errors="strict")
            prefix = ROOT_PATH + self.directory
            if not path.startswith(prefix):
                return
            name = path[len(prefix):]
            if "/" in name.removesuffix("/"):
                return
            _validate_path(self.directory + name, directory=name.endswith("/"))
        except ValueError:
            return
        self.entries.add((name, self.directory + name))


@asynccontextmanager
async def _open(path):
    # Celadon allowlists the Hilleye host's IPv4 address.
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", trust_env=False)
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=10) as client:
            async with client.stream(
                "GET", ROOT_URL + quote(path, safe="/"), headers={"Accept-Encoding": "identity"}
            ) as response:
                if response.status_code == 403:
                    raise HTTPException(502, "Celadon отклонил запрос. Проверьте IPv4 сервера Hilleye в белом списке.")
                if response.status_code == 404:
                    raise HTTPException(404, "Этот файл или папка больше не доступны на Celadon.")
                if response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise HTTPException(502, "Celadon вернул неожиданный ответ.")
                yield response
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Celadon не ответил вовремя.") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "Не удалось подключиться к Celadon.") from exc


async def _bounded_chunks(response, limit, too_large):
    size = 0
    deadline = monotonic() + 30
    length = response.headers.get("content-length", "")
    if length.isascii() and length.isdecimal() and len(length) <= 20 and int(length) > limit:
        raise too_large
    async for chunk in response.aiter_bytes():
        if monotonic() > deadline:
            raise HTTPException(504, "Celadon не успел передать данные.")
        size += len(chunk)
        if size > limit:
            raise too_large
        yield chunk


async def list_directory(path):
    _validate_path(path, directory=True)
    async with _open(path) as response:
        if not response.headers.get("content-type", "").lower().startswith("text/html"):
            raise HTTPException(502, "Celadon не вернул страницу каталога.")
        too_large = HTTPException(502, "Список файлов в этой папке слишком большой.")
        page = bytearray()
        async for chunk in _bounded_chunks(response, 1024 * 1024, too_large):
            page.extend(chunk)
        parser = _DirectoryLinks(str(response.url), path)
        parser.feed(page.decode("utf-8", errors="replace"))
        parser.close()
    return sorted(parser.entries, key=lambda item: (not item[0].endswith("/"), item[0].casefold()))


def round_location(path: str) -> tuple[str, str]:
    year, month, day, folder, *_ = path.split("/", 4) + [""] * 4
    if not re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}", f"{year}/{month}/{day}"):
        return "", ""
    try:
        day = date.fromisoformat(f"{year}-{month}-{day}").isoformat()
    except ValueError:
        return "", ""
    return day, folder if folder.startswith("round-") else ""


async def latest_day() -> str:
    path = ""
    for pattern in (r"[0-9]{4}/", r"(?:0[1-9]|1[0-2])/", r"(?:0[1-9]|[12][0-9]|3[01])/"):
        entries = await list_directory(path)
        path = max((entry for name, entry in entries if re.fullmatch(pattern, name)), default="")
        if not path:
            return ""
    return round_location(path)[0]


async def list_rounds(day: str):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        raise ValueError("Введите дату в формате ГГГГ-ММ-ДД.")
    date.fromisoformat(day)
    try:
        entries = await list_directory(day.replace("-", "/") + "/")
    except HTTPException as exc:
        if exc.status_code == 404:
            return []
        raise
    return [
        {"round_id": name.removesuffix("/").removeprefix("round-"), "folder": name.rstrip("/"), "path": path}
        for name, path in entries
        if name.startswith("round-") and name.endswith("/") and len(name) > len("round-/")
    ]


async def download(path: str, destination: Path, *, max_mb: int) -> str:
    _validate_path(path, directory=False)
    async with _open(path) as response:
        if response.headers.get("content-type", "").lower().startswith("text/html"):
            raise HTTPException(502, "Celadon вернул веб-страницу вместо файла.")
        too_large = HTTPException(413, f"Файл превышает максимальный размер ({max_mb} МБ).")
        with destination.open("wb") as stream:
            async for chunk in _bounded_chunks(response, max_mb * 1024 * 1024, too_large):
                await finish_thread(stream.write, chunk)
    return path.rsplit("/", 1)[-1]
