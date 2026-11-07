
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from aiogram.types import (
    BufferedInputFile,
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv не обязателен
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ───────────────────────────── Настройки ─────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
# Один главный администратор задаётся через SUPERADMIN_ID. Для совместимости,
# если переменная не задана, используется минимальный ID из старого ADMIN_IDS.
SUPERADMIN_ID = int(os.getenv("SUPERADMIN_ID", "0") or 0) or (min(ADMIN_IDS) if ADMIN_IDS else 0)
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))  # хранилище, если DATABASE_URL не задан

# Хранилище в Postgres: офферы и настройки переживают перезапуски (на Render диск эфемерный)
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Webhook. На Render RENDER_EXTERNAL_URL задаётся автоматически; без него (локально) работает polling.
WEBHOOK_BASE = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:48]

# Отключённый предпросмотр для служебных сообщений админки и .help.
# Для экранов оффера с {v4} используется preview_options() ниже.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

FALLBACK_EMOJI = "⭐"  # обычный эмодзи-заглушка внутри премиум-эмодзи
MAX_REMEMBERED_MESSAGES = 5000  # сколько сообщений-офферов помнить (для имени на следующих экранах)
# Telegram строит предпросмотр по URL, даже если он задан как гиперссылка в HTML.
# show_above_text размещает карточку ссылки над текстом сообщения.
def preview_options(tpl: dict, screen: str) -> LinkPreviewOptions:
    # Просим Telegram показать карточку только на экранах, где {v4} действительно выводится.
    raw_text = get_block(tpl, SCREENS[screen]) if "SCREENS" in globals() else ""
    url = normalize_v4_url(tpl.get("v4_url") or "")
    if "{v4}" in raw_text and url.lower().startswith(("https://", "http://")):
        return LinkPreviewOptions(is_disabled=False, url=url, show_above_text=True)
    return LinkPreviewOptions(is_disabled=True)

# Языки постоянных офферов (можно менять/добавлять — пресеты создадутся сами)
LANGS = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English", "es": "zh Китайский"}

# Вариант переменной {v3}
CHOICE_TITLES = {"stars": "звёзды", "gram": "GRAM", "rub": "рубли", "usdt": "USDT"}

# Откуда брать имя для {v1}
NAME_MODES = [
    ("sender_first", "отправитель команды · имя"),
    ("sender_username", "отправитель команды · @username"),
    ("sender_full", "отправитель команды · имя и фамилия"),
    ("chat_first", "собеседник в чате · имя"),
    ("chat_username", "собеседник в чате · @username"),
    ("chat_full", "собеседник в чате · имя и фамилия"),
]

# Блоки постоянных офферов: ключ, название в админке, тип
#   html  — текст сообщения с форматированием
#   label — название кнопки
#   alert — текст всплывающего окна
#   plain — простой текст
BLOCKS = [
    ("main_text", "Экран 1 — текст", "html"),
    ("btn_accept", "Экран 1 — кнопка «Принять»", "label"),
    ("btn_reject", "Экран 1 — кнопка «Отклонить»", "label"),
    ("reject_text", "Экран «Отклонено» — текст", "html"),
    ("accept_alert", "Алерт после «Принять»", "alert"),
    ("accepted_text", "Экран 2 (после алерта) — текст", "html"),
    ("btn_link", "Экран 2 — кнопка-ссылка", "label"),
    ("btn_next", "Экран 2 — кнопка «дальше»", "label"),
    ("screen3_text", "Экран 3 — текст", "html"),
    ("btn_link2", "Экран 3 — кнопка-ссылка", "label"),
    ("btn_alert2", "Экран 3 — кнопка с алертом", "label"),
    ("alert2_text", "Алерт 2 — текст", "alert"),
    ("v3_stars", "Переменная {v3} — вариант «звёзды»", "plain"),
    ("v3_gram", "Переменная {v3} — вариант «GRAM»", "plain"),
    ("v3_rub", "Переменная {v3} — вариант «рубли»", "plain"),
    ("v3_usdt", "Переменная {v3} — вариант «USDT»", "plain"),
]
BLOCK_KIND = {k: kind for k, _, kind in BLOCKS}
BLOCK_TITLE = {k: title for k, title, _ in BLOCKS}
SCREENS = {"main": "main_text", "reject": "reject_text", "accepted": "accepted_text", "s3": "screen3_text"}

DEFAULT_HELP = "<b>Команды</b>\n\n.help — эта справка\n.shab &lt;номер&gt; — вставить оффер"

# Стартовые тексты. Всё это переписывается в админке (🗂 Постоянные офферы).
DEFAULT_PRESETS = {
    "ru": {
        "main_text": "{v1}, вам доступно:\n\n{v2} {v3}\n\n{v4}",
        "btn_accept": "Принять",
        "btn_reject": "Отклонить",
        "reject_text": "❌ {v1}, вы отклонили предложение.",
        "accept_alert": "Вы приняли: {v2} {v3}. Нажмите OK, чтобы продолжить.",
        "accepted_text": "✅ {v1}, предложение принято.\n\n{v4}",
        "btn_link": "Перейти",
        "btn_next": "Далее",
        "screen3_text": "{v4}\n\n{v5}",
        "btn_link2": "Перейти",
        "btn_alert2": "Подробнее",
        "alert2_text": "{v5}",
        "v3_stars": "звёзд",
        "v3_gram": "GRAM",
        "v3_rub": "рублей",
        "v3_usdt": "USDT",
    },
    "en": {
        "main_text": "{v1}, you have:\n\n{v2} {v3}\n\n{v4}",
        "btn_accept": "Accept",
        "btn_reject": "Decline",
        "reject_text": "❌ {v1}, you declined the offer.",
        "accept_alert": "You accepted: {v2} {v3}. Press OK to continue.",
        "accepted_text": "✅ {v1}, offer accepted.\n\n{v4}",
        "btn_link": "Open",
        "btn_next": "Next",
        "screen3_text": "{v4}\n\n{v5}",
        "btn_link2": "Open",
        "btn_alert2": "Details",
        "alert2_text": "{v5}",
        "v3_stars": "stars",
        "v3_gram": "GRAM",
        "v3_rub": "RUB",
        "v3_usdt": "USDT",
    },
    "es": {
        "main_text": "{v1}, tienes:\n\n{v2} {v3}\n\n{v4}",
        "btn_accept": "Aceptar",
        "btn_reject": "Rechazar",
        "reject_text": "❌ {v1}, has rechazado la oferta.",
        "accept_alert": "Has aceptado: {v2} {v3}. Pulsa OK para continuar.",
        "accepted_text": "✅ {v1}, oferta aceptada.\n\n{v4}",
        "btn_link": "Abrir",
        "btn_next": "Siguiente",
        "screen3_text": "{v4}\n\n{v5}",
        "btn_link2": "Abrir",
        "btn_alert2": "Detalles",
        "alert2_text": "{v5}",
        "v3_stars": "estrellas",
        "v3_gram": "GRAM",
        "v3_rub": "RUB",
        "v3_usdt": "USDT",
    },
}

# Постоянные значения для всех языков (редактируются в админке: 🔧 Постоянные значения)
DEFAULT_SETTINGS = {
    "v5_html": "@example",  # переменная {v5} (можно с форматированием и премиум-эмодзи)
    "link_url": "https://t.me/{v5}",  # ссылка кнопок; {v5} подставляется автоматически
    "emoji_v1": "",  # ID премиум-эмодзи после {v1}
    "emoji_v2_stars": "",  # после {v2}, если выбраны «звёзды»
    "emoji_v2_gram": "",  # после {v2}, если выбран «GRAM»
    "emoji_v2_rub": "",  # после {v2}, если выбраны рубли
    "emoji_v2_usdt": "",  # после {v2}, если выбран USDT
    "emoji_stars": "",  # после {v3}, если выбраны «звёзды»
    "emoji_gram": "",  # после {v3}, если выбран «GRAM»
    "emoji_rub": "",  # после {v3}, если выбраны рубли
    "emoji_usdt": "",  # после {v3}, если выбран USDT
    "name_mode": "sender_first",
    "notify_mode": "super",  # super — уведомления только суперадмину, all — всем админам (кроме менеджеров)
}


# ───────────────────────────── Хранилище (JSON) ─────────────────────────────
def new_template(lang: str) -> dict:
    return {
        "lang": lang,
        "amount": "",  # число для {v2}
        "v3_choice": "stars",  # "stars" | "gram" | "rub" | "usdt"
        "v4_text": "",  # текст для {v4}
        "v4_url": "",  # ссылка, в которую превращается {v4}
    }


def tpl_key(owner_id, n) -> str:
    """Ключ оффера в хранилище: офферы персональные, номер уникален внутри админа."""
    return f"{owner_id}:{n}"


def owner_templates(owner_id) -> dict:
    """Офферы конкретного админа в виде {"номер": оффер}."""
    prefix = f"{owner_id}:"
    return {
        key.split(":", 1)[1]: tpl
        for key, tpl in db.data["templates"].items()
        if key.startswith(prefix)
    }


def get_tpl(owner_id, n: str) -> dict | None:
    """Оффер по владельцу и номеру. Без владельца — первый попавшийся с таким номером."""
    if owner_id:
        return db.data["templates"].get(tpl_key(owner_id, n))
    for key, tpl in db.data["templates"].items():
        if key.split(":", 1)[1] == n:
            return tpl
    return None


def owner_of_bcid(bcid: str | None):
    """Владелец бизнес-подключения — по нему определяется, чьи офферы использовать."""
    if not bcid:
        return None
    return (db.data["connections"].get(bcid) or {}).get("user_id")


class Storage:
    def __init__(self, path: Path, database_url: str = ""):
        self.path = path
        self.url = database_url
        self._conn = None
        self.data: dict = {
            "help_html": DEFAULT_HELP,
            "presets": {},
            "settings": {},
            "admins": [],
            "managers": [],  # могут выдавать админку, уведомления им не приходят
            "templates": {},  # "owner_id:номер" -> оффер
            "connections": {},  # business_connection_id -> {"user_id": ...}
            "business_chats": {},  # username lowercase -> {chat_id, business_connection_id, name}
            "messages": {},  # "chat_id:message_id" -> {"name": ...}
            "gift_cache": {},  # business_connection_id -> [{"id": ..., "take": ..., "conv": ...}]
            "gift_next": {},  # business_connection_id -> offset для следующей страницы
        }
        saved = self._load()
        if saved:
            self.data.update(saved)
        self.data.setdefault("business_chats", {})
        self.data.setdefault("gift_cache", {})
        self.data.setdefault("gift_next", {})
        self.data.setdefault("managers", [])
        old_settings = self.data.get("settings", {})
        self.data["settings"] = {**DEFAULT_SETTINGS, **old_settings}
        # Совместимость со старым единым эмодзи после {v2}.
        if "emoji_v2" in old_settings:
            self.data["settings"].setdefault("emoji_v2_stars", old_settings["emoji_v2"])
            self.data["settings"].setdefault("emoji_v2_gram", old_settings["emoji_v2"])
        for lang in LANGS:
            preset = self.data["presets"].setdefault(lang, {})
            for key, value in DEFAULT_PRESETS.get(lang, DEFAULT_PRESETS["ru"]).items():
                preset.setdefault(key, value)
        # Офферы стали персональными: старые общие (ключи без ":") отдаём суперадмину.
        migrated = {}
        for key, tpl in self.data["templates"].items():
            migrated[key if ":" in key else tpl_key(SUPERADMIN_ID, key)] = tpl
        self.data["templates"] = migrated
        for tpl in self.data["templates"].values():
            for key, value in new_template(tpl.get("lang", "ru")).items():
                tpl.setdefault(key, value)
        self.save()

    def _connect(self):
        import psycopg  # нужен только при заданном DATABASE_URL

        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.url, autocommit=True)
            # тип JSON (не JSONB) — сохраняет порядок ключей
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS bot_state (id INT PRIMARY KEY, data JSON NOT NULL)"
            )
        return self._conn

    def _load(self):
        if self.url:
            row = self._connect().execute("SELECT data FROM bot_state WHERE id = 1").fetchone()
            return row[0] if row else None
        if self.path.exists():
            return json.loads(self.path.read_text("utf-8"))
        return None

    def save(self) -> None:
        if self.url:
            from psycopg.types.json import Json

            sql = ("INSERT INTO bot_state (id, data) VALUES (1, %s) "
                   "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data")
            for attempt in (1, 2):
                try:
                    self._connect().execute(sql, (Json(self.data),))
                    return
                except Exception:  # noqa: BLE001 — соединение могло оборваться, переподключаемся
                    self._conn = None
                    if attempt == 2:
                        raise
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)


db = Storage(DATA_FILE, DATABASE_URL)
# Перенос старой конфигурации: первый/указанный супер-админ, остальные ADMIN_IDS — обычные.
db.data["managers"] = [int(x) for x in db.data.get("managers", [])
                       if str(x).isdigit() and int(x) != SUPERADMIN_ID]
if "admins" not in db.data:
    db.data["admins"] = sorted(ADMIN_IDS - ({SUPERADMIN_ID} if SUPERADMIN_ID else set())
                               - set(db.data["managers"]))
    db.save()
else:
    db.data["admins"] = [int(x) for x in db.data.get("admins", [])
                         if str(x).isdigit() and int(x) != SUPERADMIN_ID
                         and int(x) not in db.data["managers"]]


def get_admin_ids() -> set[int]:
    ids = {SUPERADMIN_ID} if SUPERADMIN_ID else set()
    ids.update(int(x) for x in db.data.get("admins", []))
    ids.update(int(x) for x in db.data.get("managers", []))
    return ids


def is_superadmin(user_id: int | None) -> bool:
    return bool(user_id and SUPERADMIN_ID and user_id == SUPERADMIN_ID)


def is_manager(user_id: int | None) -> bool:
    return bool(user_id and int(user_id) in db.data.get("managers", []))


def is_staff(user_id: int | None) -> bool:
    """Суперадмин или менеджер — те, кто управляет администраторами."""
    return is_superadmin(user_id) or is_manager(user_id)


# ───────────────────────────── Подключения и права ─────────────────────────────
RIGHT_KEYS = [
    ("can_reply", "отвечать в чатах"),
    ("can_read_messages", "читать переписку"),
    ("can_delete_sent_messages", "удалять свои сообщения"),
    ("can_delete_all_messages", "удалять любые сообщения"),
    ("can_edit_name", "менять имя"),
    ("can_edit_bio", "менять bio"),
    ("can_edit_profile_photo", "менять фото"),
    ("can_edit_username", "менять username"),
    ("can_change_gift_settings", "менять настройки подарков"),
    ("can_view_gifts_and_stars", "ВИДЕТЬ подарки и звёзды"),
    ("can_convert_gifts_to_stars", "конвертировать подарки в звёзды"),
    ("can_transfer_and_upgrade_gifts", "ЗАБИРАТЬ и апгрейдить подарки"),
    ("can_transfer_stars", "переводить звёзды"),
    ("can_manage_stories", "управлять историями"),
]
RIGHT_TITLE = dict(RIGHT_KEYS)
GIFTS_PAGE = 8
GIFTS_FETCH = 50  # сколько подарков тянем за раз, листаем локально


def rights_of(conn) -> dict:
    rights = getattr(conn, "rights", None)
    if rights is None:
        return {}
    return {k: bool(getattr(rights, k, False)) for k, _ in RIGHT_KEYS}


def mark(value) -> str:
    return "—" if value is None else ("✅" if value else "❌")


def admin_label(uid) -> str:
    if is_superadmin(uid):
        return "👑 суперадмин"
    if is_manager(uid):
        return "🧑‍💼 менеджер"
    return "🛠 админ" if uid in get_admin_ids() else "❔ не админ"


def conn_text(bcid: str, c: dict) -> str:
    r = c.get("rights")
    chats = sum(1 for k in db.data["business_chats"] if k.startswith(bcid + ":"))
    lines = [
        f"👤 <b>{html.escape(c.get('name') or '—')}</b> · {admin_label(c.get('user_id'))}",
        f"ID: <code>{c.get('user_id')}</code>",
        f"Состояние: {'🟢 активно' if c.get('enabled') else '⚪️ отключено'}",
        f"Известных чатов: {chats}",
        "",
    ]
    if r is None:
        lines.append("⚠️ Права не получены — нажмите «🔄 Обновить права».")
        return "\n".join(lines)

    lines.append("🧰 <b>РАБОЧИЕ ПРАВА</b>")
    for key in ("can_reply", "can_read_messages", "can_delete_all_messages"):
        lines.append(f"{mark(r.get(key))} {RIGHT_TITLE[key]}")
    if not r.get("can_reply"):
        lines.append("⚠️ Без «отвечать в чатах» офферы у него не работают.")

    lines.append("")
    lines.append("🎁 <b>ПОДАРКИ И ЗВЁЗДЫ</b>")
    for key in ("can_view_gifts_and_stars", "can_transfer_and_upgrade_gifts",
                "can_convert_gifts_to_stars", "can_transfer_stars",
                "can_change_gift_settings"):
        lines.append(f"{mark(r.get(key))} {RIGHT_TITLE[key]}")
    if r.get("can_transfer_and_upgrade_gifts") and not r.get("can_transfer_stars"):
        lines.append("\n⚠️ Забирать можно, но звёзд на комиссию нет — только star_count=0.")

    lines.append("")
    lines.append("🔧 <b>Остальное</b>")
    for key in ("can_edit_username", "can_edit_name", "can_edit_bio",
                "can_edit_profile_photo", "can_delete_sent_messages",
                "can_manage_stories"):
        lines.append(f"{mark(r.get(key))} {RIGHT_TITLE[key]}")
    return "\n".join(lines)


def conn_kb(bcid: str, c: dict) -> InlineKeyboardMarkup:
    r = c.get("rights") or {}
    rows = [[("🔄 Обновить права", f"b:{bcid}:r")]]
    if r.get("can_view_gifts_and_stars"):
        rows.append([("🎁 Подарки", f"b:{bcid}:g"), ("⭐ Баланс звёзд", f"b:{bcid}:s")])
    if r.get("can_transfer_stars"):
        rows.append([("💸 Вывести звёзды себе", f"b:{bcid}:ts")])
    rows.append([("⬅️ Назад", "a:conns")])
    return kb(*rows)


def gift_title(g) -> str:
    if getattr(g, "type", "") == "unique":
        gift = getattr(g, "gift", None)
        base = getattr(gift, "base_name", "") or getattr(gift, "name", "")
        num = getattr(gift, "number", "?")
        return f"{base} #{num}"
    return "обычный подарок"


def gift_line(idx: int, g) -> str:
    gift = getattr(g, "gift", None)
    flags = []
    if getattr(g, "type", "") == "unique":
        if getattr(gift, "is_from_blockchain", False):
            flags.append("⛓ on-chain (в TG не уйдёт)")
        if not getattr(g, "can_be_transferred", False):
            flags.append("🔒 не передаётся")
        nt = getattr(g, "next_transfer_date", None)
        if nt and nt > time.time():
            flags.append("⏳ разблокируется " + time.strftime("%d.%m %H:%M", time.localtime(nt)))
        fee = getattr(g, "transfer_star_count", None)
        if fee:
            flags.append(f"💫 комиссия {fee}")
    else:
        if getattr(g, "can_be_upgraded", False):
            flags.append("⬆️ можно апгрейдить")
        conv = getattr(g, "convert_star_count", None)
        if conv:
            flags.append(f"💥 в звёзды {conv}")
    tail = (" · " + " · ".join(flags)) if flags else ""
    return f"{idx}. {html.escape(gift_title(g))}{tail}"


def can_take(g) -> bool:
    if getattr(g, "type", "") != "unique":
        return False
    if getattr(getattr(g, "gift", None), "is_from_blockchain", False):
        return False
    if not getattr(g, "can_be_transferred", False):
        return False
    nt = getattr(g, "next_transfer_date", None)
    return not nt or nt <= time.time()


def gift_entries(gifts) -> list:
    return [{
        "id": g.owned_gift_id,
        "take": can_take(g),
        "conv": getattr(g, "convert_star_count", None) if getattr(g, "type", "") != "unique" else None,
    } for g in gifts]


def get_block(tpl: dict, key: str) -> str:
    return db.data["presets"][tpl["lang"]][key]


def remember(chat_id: int, message_id: int, name: str) -> None:
    """Запоминаем имя для сообщения: на следующих экранах (нажатия кнопок) оно нужно снова."""
    msgs = db.data["messages"]
    msgs[f"{chat_id}:{message_id}"] = {"name": name}
    while len(msgs) > MAX_REMEMBERED_MESSAGES:
        msgs.pop(next(iter(msgs)))
    db.save()


def recall_name(chat_id: int, message_id: int) -> str:
    return db.data["messages"].get(f"{chat_id}:{message_id}", {}).get("name", "")


def pick_name(mode: str, sender, chat) -> str:
    """Имя для {v1}: отправитель команды (владелец аккаунта) или собеседник в чате."""
    who = sender if mode.startswith("sender") else chat
    if who is None:
        return ""
    kind = mode.split("_", 1)[1]
    first = getattr(who, "first_name", None) or ""
    if kind == "username":
        username = getattr(who, "username", None)
        return f"@{username}" if username else first
    if kind == "full":
        return " ".join(p for p in (first, getattr(who, "last_name", None)) if p)
    return first


# ───────────────────────────── Рендер текста ─────────────────────────────
TAG_RE = re.compile(r"<[^>]+>")
VAR_RE = re.compile(r"\{(v[1-5])\}")
EMOJI_RE = re.compile(r"\{e:(\d+)\}")
URL_RE = re.compile(r"^(https?://|tg://)\S+$", re.I)


def normalize_v4_url(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("t.me/"):
        return "https://" + url
    return url


def valid_v4_url(url: str) -> bool:
    return bool(URL_RE.match(url) or re.match(r"^t\.me/\S+$", url, re.I))
AMOUNT_RE = re.compile(r"^\d[\d\s.,]*$")


def emoji_tag(emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{FALLBACK_EMOJI}</tg-emoji>'


def bold_var(text: str, emoji_id: str) -> str:
    """Жирный текст переменной + в конце один премиум-эмодзи."""
    out = f"<b>{html.escape(text)}</b>"
    return out + (" " + emoji_tag(emoji_id) if emoji_id else "")


def make_vars(tpl: dict, name: str):
    """Возвращает (html-значения, plain-значения) переменных {v1}..{v5}."""
    st = db.data["settings"]
    preset = db.data["presets"][tpl["lang"]]
    choice = tpl["v3_choice"]
    v3_text = preset["v3_" + choice]
    v4_text = tpl.get("v4_text", "")
    if tpl.get("v4_url"):
        v4_html = f'<a href="{html.escape(tpl["v4_url"], quote=True)}">{html.escape(v4_text)}</a>'
    else:
        v4_html = html.escape(v4_text)
    v5 = st["v5_html"]
    html_vars = {
        "v1": bold_var(name or "—", st["emoji_v1"]),
        "v2": bold_var(tpl.get("amount", ""), st.get("emoji_v2_" + choice, st.get("emoji_v2", ""))),
        "v3": bold_var(v3_text, st["emoji_" + choice]),
        "v4": v4_html,
        "v5": v5,
    }
    plain_vars = {
        "v1": name,
        "v2": tpl.get("amount", ""),
        "v3": v3_text,
        "v4": v4_text,
        "v5": html.unescape(TAG_RE.sub("", v5)),
    }
    return html_vars, plain_vars


def render_html(raw: str, variables: dict) -> str:
    """{v1}..{v5} -> значения, {e:ID} -> премиум-эмодзи. Работает с HTML-разметкой."""
    out = VAR_RE.sub(lambda m: variables.get(m.group(1), ""), raw)
    return EMOJI_RE.sub(lambda m: emoji_tag(m.group(1)), out)


def render_plain(raw: str, variables: dict) -> str:
    """Для алертов и кнопок: без разметки и без эмодзи-маркеров."""
    out = VAR_RE.sub(lambda m: variables.get(m.group(1), ""), raw)
    return EMOJI_RE.sub("", out).strip()


def parse_label(raw: str, variables: dict) -> tuple[str, str | None]:
    """Название кнопки + ID премиум-эмодзи (первый {e:ID}), если указан."""
    m = EMOJI_RE.search(raw)
    return render_plain(raw, variables), (m.group(1) if m else None)


def link_url() -> str:
    """Ссылка кнопок. {v5} в ней заменяется на значение переменной 5 —
    так ссылка меняется только вместе с {v5}."""
    st = db.data["settings"]
    v5 = html.unescape(TAG_RE.sub("", st["v5_html"]))
    v5 = EMOJI_RE.sub("", v5).strip().lstrip("@")
    return st["link_url"].replace("{v5}", urllib.parse.quote(v5, safe=""))


def build_screen(n: str, tpl: dict, screen: str, name: str, icons: bool = True):
    """Возвращает (html_текст, клавиатура) для экрана оффера."""
    html_vars, plain_vars = make_vars(tpl, name)

    def btn(key: str, **kw) -> InlineKeyboardButton:
        text, emoji_id = parse_label(get_block(tpl, key), plain_vars)
        extra = {"icon_custom_emoji_id": emoji_id} if (emoji_id and icons) else {}
        return InlineKeyboardButton(text=text or "•", **kw, **extra)

    def link_btn(key: str) -> InlineKeyboardButton:
        return btn(key, url=link_url())

    rows = None
    if screen == "main":
        rows = [[btn("btn_accept", callback_data=f"sh:{n}:acc"),
                 btn("btn_reject", callback_data=f"sh:{n}:rej")]]
    elif screen == "accepted":
        rows = [[link_btn("btn_link")], [btn("btn_next", callback_data=f"sh:{n}:s3")]]
    elif screen == "s3":
        rows = [[link_btn("btn_link2")], [btn("btn_alert2", callback_data=f"sh:{n}:al2")]]

    text = render_html(get_block(tpl, SCREENS[screen]), html_vars)
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return text, markup


def alert_text(tpl: dict, key: str, name: str) -> str:
    _, plain_vars = make_vars(tpl, name)
    return render_plain(get_block(tpl, key), plain_vars)[:200]


async def edit_screen(bot: Bot, *, chat_id: int, message_id: int, bcids: list,
                      n: str, tpl: dict, screen: str, name: str) -> None:
    """Правит сообщение (в бизнес-чате или обычном). Если Telegram не принял иконки
    премиум-эмодзи на кнопках — повторяет без них."""
    last_error = None
    for bcid in bcids:
        for icons in (True, False):
            text, markup = build_screen(n, tpl, screen, name, icons)
            try:
                await bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=message_id,
                    business_connection_id=bcid, parse_mode=ParseMode.HTML,
                    link_preview_options=preview_options(tpl, screen), reply_markup=markup,
                )
                return
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return
                last_error = e
                log.warning("edit failed (bcid=%s, icons=%s): %s", bcid, icons, e)
    raise last_error


async def send_screen(bot: Bot, chat_id: int, n: str, tpl: dict, name: str,
                      screen: str = "main", business_connection_id: str | None = None):
    for icons in (True, False):
        text, markup = build_screen(n, tpl, screen, name, icons)
        try:
            return await bot.send_message(
                chat_id, text, business_connection_id=business_connection_id,
                parse_mode=ParseMode.HTML,
                link_preview_options=preview_options(tpl, screen), reply_markup=markup,
            )
        except TelegramBadRequest as e:
            if not icons:
                raise
            log.warning("send with icons failed: %s", e)


# ───────────────────────────── /info ─────────────────────────────
INFO_TEXT = (
    "ℹ️ <b>Как работает бот</b>\n\n"
    "Бот подключается к вашему аккаунту через Telegram Business "
    "(Настройки → Telegram для бизнеса → Чат-боты) и умеет править ваши сообщения в любых чатах.\n\n"
    "<b>Команды в чатах</b> (пишете вы, бот заменяет сообщение):\n"
    "• <code>.help</code> — справка\n"
    "• <code>.shab номер</code> — отправляет оффер под нужным номером\n"
    "<b>Как идёт оффер</b>\n"
    "1. Оффер и кнопки «Принять» / «Отклонить».\n"
    "2. «Отклонить» → сообщение меняется на экран отказа.\n"
    
    "3. «Принять» → всплывающее уведомление с кнопкой OK, затем экран об успешной продаже с кнопкой «Передать нфт» и кнопкой «Как передать?».\n"
    "4. «Как передать?» → экран с инструкцией, с кнопкой «Передать нфт» и кнопкой «Подтвердить передачу», которая показывает уведомление, что нфт ещё не передан.\n\n"

    
    "<b>Язык</b>: у каждого оффера свой язык (ru / en / sh) — от него зависят все тексты кнопок и экранов."
)

INFO_ADMIN = (
    "\n\n<b>Админ-панель</b> — /admin\n"
    "• 📖 Сообщение .help\n"
    "• 🧩 Офферы — создаёте номер: язык, сумма, звёзды/GRAM/рубли/USDT, название нфт и ссылка на подарок\n"
    "• 📤 /dump_texts — выгрузка всех текстов по всем языкам файлом\n"
)


def info_text(is_admin: bool) -> str:
    return INFO_TEXT + (INFO_ADMIN if is_admin else "")


# ───────────────────────────── Business: .help и .shab ─────────────────────────────
public = Router()
SHAB_RE = re.compile(r"^\.shab\s+(\d+)\s*$", re.I)
CONF_RE = re.compile(r"^\+conf\s+(\d+)\s*$", re.I)


async def get_owner_id(bot: Bot, bcid: str) -> int:
    conn = db.data["connections"].get(bcid)
    if conn:
        return conn["user_id"]
    c = await bot.get_business_connection(bcid)
    db.data["connections"][bcid] = {
        "user_id": c.user.id,
        "name": c.user.full_name,
        "enabled": c.is_enabled,
        "rights": rights_of(c),
    }
    db.save()
    return c.user.id


async def notify_admins(bot: Bot, text: str) -> None:
    """Уведомления. По умолчанию — только суперадмину и всегда без звука.
    Менеджеры уведомлений не получают никогда."""
    mode = db.data["settings"].get("notify_mode", "super")
    if mode == "super":
        ids = {SUPERADMIN_ID} if SUPERADMIN_ID else set()
    else:
        ids = get_admin_ids() - {int(x) for x in db.data.get("managers", [])}
    for admin_id in ids:
        try:
            await bot.send_message(admin_id, text, disable_notification=True,
                                   link_preview_options=NO_PREVIEW)
        except Exception:  # noqa: BLE001
            pass


@public.business_connection()
async def on_business_connection(conn: BusinessConnection, bot: Bot):
    # rights.can_reply — актуальное поле Bot API; can_reply на верхнем уровне устарело.
    rights = getattr(conn, "rights", None)
    can_reply = getattr(rights, "can_reply", None) if rights else getattr(conn, "can_reply", None)
    db.data["connections"][conn.id] = {
        "user_id": conn.user.id,
        "name": conn.user.full_name,
        "enabled": conn.is_enabled,
        "can_reply": can_reply,
        "rights": rights_of(conn),
    }
    db.save()
    state = "подключён" if conn.is_enabled else "отключён"
    r = db.data["connections"][conn.id]["rights"]
    await notify_admins(
        bot,
        f"🔌 Бизнес-аккаунт {html.escape(conn.user.full_name)} {state} "
        f"(<code>{conn.user.id}</code> · {admin_label(conn.user.id)}).\n"
        f"подарки: видеть {mark(r.get('can_view_gifts_and_stars'))} · "
        f"забирать {mark(r.get('can_transfer_and_upgrade_gifts'))} · "
        f"звёзды {mark(r.get('can_transfer_stars'))}"
    )


@public.business_message()
async def on_business_message(message: Message, bot: Bot):
    # Запоминаем username и chat_id из обновлений Business, чтобы позже можно было
    # адресно отправить экран 1 по username. Искать произвольные username Telegram не даёт.
    bcid = message.business_connection_id
    if bcid and message.chat.type == "private":
        username = getattr(message.chat, "username", None)
        if username:
            db.data.setdefault("business_chats", {})[f"{bcid}:{username.lower()}"] = {
                "username": username.lower(),
                "chat_id": message.chat.id,
                "business_connection_id": bcid,
                "name": (" ".join(p for p in (
                    getattr(message.chat, "first_name", None),
                    getattr(message.chat, "last_name", None),
                ) if p) or ("@" + username)),
            }
            db.save()
    if not message.text:
        return
    text = message.text.strip()
    low = text.lower()
    conf_match = CONF_RE.match(text)
    # Диагностика: фиксируем только команды, чтобы не засорять лог обычной перепиской.
    if low == ".help" or low.startswith(".shab") or conf_match:
        log.info(
            "Business command received: text=%r chat_id=%s message_id=%s bcid=%s "
            "from_user_id=%s chat_type=%s",
            text, message.chat.id, message.message_id, message.business_connection_id,
            message.from_user.id if message.from_user else None, message.chat.type,
        )
    else:
        return
    # реагируем только на сообщения самого владельца бизнес-аккаунта
    owner_id = await get_owner_id(bot, message.business_connection_id)
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        log.warning(
            "Business command ignored: sender_id=%s owner_id=%s bcid=%s text=%r",
            sender_id, owner_id, message.business_connection_id, text,
        )
        return
    log.info("Business command owner check passed: owner_id=%s bcid=%s", owner_id, message.business_connection_id)

    bcid = message.business_connection_id
    if low == ".help":
        await bot.edit_message_text(
            text=render_html(db.data["help_html"], {}), chat_id=message.chat.id,
            message_id=message.message_id, business_connection_id=bcid,
            parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
        )
        return

    m = SHAB_RE.match(text)
    if not m and not conf_match:
        log.warning("Business command syntax did not match: text=%r bcid=%s", text, bcid)
        return
    n = str(int((m or conf_match).group(1)))
    # Офферы персональные: берём оффер владельца этого бизнес-аккаунта.
    tpl = get_tpl(owner_id, n)
    if not tpl:
        log.error("Business command template missing: owner=%s n=%s", owner_id, n)
        command = "+conf" if conf_match else ".shab"
        await notify_admins(bot, f"⚠️ Оффер №{n} не найден (команда {command} {n}).")
        return
    name = pick_name(db.data["settings"]["name_mode"], message.from_user, message.chat)
    remember(message.chat.id, message.message_id, name)
    if conf_match:
        # Промежуточное сообщение: экран 1 появится только после нажатия «Продолжить».
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Продолжить", callback_data=f"cf:{n}")
        ]])
        await bot.edit_message_text(
            text=".", chat_id=message.chat.id, message_id=message.message_id,
            business_connection_id=bcid, parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW, reply_markup=markup,
        )
        return
    try:
        await edit_screen(
            bot, chat_id=message.chat.id, message_id=message.message_id,
            bcids=[bcid], n=n, tpl=tpl, screen="main", name=name,
        )
        return
    except Exception as edit_exc:  # noqa: BLE001 — не оставляем ошибку без диагностики
        log.exception(
            "Не удалось заменить .shab на экран: chat_id=%s message_id=%s bcid=%s",
            message.chat.id, message.message_id, bcid,
        )

    # Если исходное сообщение нельзя редактировать, пробуем отправить экран отдельным сообщением.
    # Если Telegram запрещает и отправку, это будет видно в логах и администратору.
    try:
        sent = await send_screen(
            bot, message.chat.id, n, tpl, name,
            screen="main", business_connection_id=bcid,
        )
        remember(sent.chat.id, sent.message_id, name)
        log.info("Экран отправлен отдельным сообщением: chat_id=%s bcid=%s", message.chat.id, bcid)
    except Exception as send_exc:  # noqa: BLE001
        log.exception(
            "Не удалось и отредактировать, и отправить экран: chat_id=%s bcid=%s",
            message.chat.id, bcid,
        )
        await notify_admins(
            bot,
            "❌ Не удалось показать экран .shab.\n"
            f"chat_id={message.chat.id}; business_connection_id={bcid}\n"
            f"Ошибка редактирования: {str(edit_exc)[:700]}\n"
            f"Ошибка отправки: {str(send_exc)[:700]}\n\n"
            "Проверьте права Telegram Business и доступность чата: у Business-бота "
            "должно быть право отвечать в этом чате (обычно чат должен быть активен по входящему "
            "сообщению пользователя в последние 24 часа). Сам факт отсутствия пользователя в контактах "
            "не является отдельным универсальным запретом."
        )


@public.callback_query(F.data.startswith("cf:"))
async def on_conf_callback(cq: CallbackQuery, bot: Bot):
    """Кнопка «Продолжить» из +conf №: открывает экран 1 оффера в том же Business-чате."""
    try:
        n = str(int(cq.data.split(":", 1)[1]))
    except (ValueError, IndexError):
        return await cq.answer("Некорректный номер оффера", show_alert=True)
    msg = cq.message
    bcid = getattr(msg, "business_connection_id", None) if msg else None
    if not msg or not bcid:
        return await cq.answer("Не найдено Business-подключение для этого сообщения", show_alert=True)
    tpl = get_tpl(owner_of_bcid(bcid), n)
    if not tpl:
        return await cq.answer("Оффер не найден", show_alert=True)
    name = recall_name(msg.chat.id, msg.message_id)
    try:
        await edit_screen(bot, chat_id=msg.chat.id, message_id=msg.message_id,
                          bcids=[bcid], n=n, tpl=tpl, screen="main", name=name)
        await cq.answer()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось открыть экран 1 по +conf для оффера %s", n)
        await cq.answer("Не удалось открыть экран 1", show_alert=True)


@public.callback_query(F.data.startswith("sh:"))
async def on_shab_callback(cq: CallbackQuery, bot: Bot):
    try:
        _, n, action = cq.data.split(":")
    except ValueError:
        return await cq.answer()
    msg = cq.message
    if not msg:
        return await cq.answer("Оффер не найден", show_alert=True)

    bcid = getattr(msg, "business_connection_id", None)
    # если у сообщения нет business_connection_id — пробуем обычное редактирование,
    # затем известные бизнес-подключения
    bcids = [bcid] if bcid else [None, *db.data["connections"]]
    tpl = get_tpl(owner_of_bcid(bcid), n)
    if not tpl:
        return await cq.answer("Оффер не найден", show_alert=True)
    name = recall_name(msg.chat.id, msg.message_id)

    async def go(screen: str):
        await edit_screen(bot, chat_id=msg.chat.id, message_id=msg.message_id,
                          bcids=bcids, n=n, tpl=tpl, screen=screen, name=name)

    try:
        if action == "rej":
            await go("reject")
            await cq.answer()
        elif action == "acc":
            # Сначала меняем экран; затем отвечаем на callback, чтобы ошибка редактирования
            # не оставляла кнопку в бесконечной загрузке.
            await go("accepted")
            await cq.answer(alert_text(tpl, "accept_alert", name) or None, show_alert=True)
        elif action == "s3":
            await go("s3")
            await cq.answer()
        elif action == "al2":
            await cq.answer(alert_text(tpl, "alert2_text", name) or None, show_alert=True)
        else:
            await cq.answer()
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "Ошибка callback sh: chat_id=%s message_id=%s bcid=%s action=%s",
            msg.chat.id, msg.message_id, bcid, action,
        )
        try:
            await cq.answer("Telegram не разрешил изменить сообщение. Проверьте права Business.", show_alert=True)
        except Exception:
            pass


@public.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("Бот работает через Telegram Business. Как это устроено — /info")


@public.message(Command("info"))
async def cmd_info_public(message: Message):
    await message.answer(info_text(False))


# ───────────────────────────── Админ-панель ─────────────────────────────
admin = Router()
admin.message.filter(F.chat.type == "private", F.from_user.id.func(lambda uid: uid in get_admin_ids()))
admin.callback_query.filter(F.from_user.id.func(lambda uid: uid in get_admin_ids()))


class AdminCallbackPermissions(BaseMiddleware):
    """Обычным админам и менеджерам разрешены создание и редактирование полей своих офферов.
    Управление администраторами — только менеджерам и суперадмину."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and is_superadmin(user.id):
            return await handler(event, data)
        payload = getattr(event, "data", "") or ""
        allowed = (
            payload in {"a:menu", "a:tpls", "a:new", "a:set", "a:syntax",
                        "s:e:v5_html", "s:e:link_url", "d:dump"}
            or re.fullmatch(r"t:\d+(?::(?:card|prev|del|delok|v3|lang|e:(?:amount|v4_text|v4_url)|sl:(?:ru|en|es)))?", payload)
            or re.fullmatch(r"a:wlang:(ru|en|es)", payload)
            or re.fullmatch(r"a:wchoice:(stars|gram|rub|usdt)", payload)
        )
        if not allowed and user and is_manager(user.id):
            allowed = (
                payload in {"a:admins", "a:admin_add"}
                or payload.startswith("a:admin_del:")
            )
        if not allowed:
            if hasattr(event, "answer"):
                await event.answer("Недостаточно прав для этого действия.", show_alert=True)
            return None
        return await handler(event, data)


admin.callback_query.middleware(AdminCallbackPermissions())


class S(StatesGroup):
    value = State()  # ждём значение; data: target, key
    wiz = State()  # мастер создания оффера; data: i, draft
    admin_add = State()  # ввод Telegram ID нового админа/менеджера; data: role


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


MAIN_KB = kb(
    [("🔌 Подключения и права", "a:conns")],
    [("📖 Сообщение .help", "a:help")],
    [("🧩 Офферы (.shab)", "a:tpls")],
    [("📝 Тексты и кнопки", "a:pres")],
    [("⚙️ Настройки", "a:set")],
    [("❓ Синтаксис", "a:syntax")],
    [("👥 Управление админами", "a:admins")],
)
USER_MAIN_KB = kb(
    [("🧩 Создать оффер", "a:tpls")],
    [("🔧 Изменить юзернейм и ссылки кнопок", "a:set")],
)
MANAGER_MAIN_KB = kb(
    [("🧩 Создать оффер", "a:tpls")],
    [("🔧 Изменить юзернейм и ссылки кнопок", "a:set")],
    [("👥 Управление админами", "a:admins")],
)


def main_kb_for(user_id: int) -> InlineKeyboardMarkup:
    if is_superadmin(user_id):
        return MAIN_KB
    if is_manager(user_id):
        return MANAGER_MAIN_KB
    return USER_MAIN_KB

SYNTAX_TEXT = (
    "❓ <b>Синтаксис</b>\n\n"
    "• <b>Форматирование</b>: отправляйте боту сообщение с жирным, курсивом, ссылками, "
    "спойлерами, цитатами — всё сохранится.\n"
    "• <b>Премиум-эмодзи</b>: <code>{e:ID}</code> в любом месте текста (или вставьте сам "
    "премиум-эмодзи). В названии кнопки <code>{e:ID}</code> задаёт иконку кнопки "
    "(Telegram показывает её слева от текста).\n"
    "• <b>Переменные</b> (работают в любых текстах, кнопках и алертах):\n"
    "  {v1} имя · {v2} число · {v3} звёзды/GRAM/рубли/USDT · {v4} текст-ссылка · {v5} константа\n"
    "  {v1}–{v3} выводятся жирным и с премиум-эмодзи в конце.\n"
    "• <b>Алерты</b>: до 200 символов, без форматирования.\n\n"
    "Чтобы узнать ID премиум-эмодзи — просто отправьте его боту в личку."
)

HINT_HTML = (
    "Отправьте юзернейм кому нужно будет передавать нфт. Он будет указан в инструкции."
)
HINT_LABEL = ("Отправьте название кнопки. Премиум-эмодзи: добавьте <code>{e:ID}</code> в конец. "
              "Можно использовать {v1}…{v5}.")
HINT_ALERT = "Отправьте текст алерта (до 200 символов, без форматирования). Можно использовать {v1}…{v5}."
HINT_PLAIN = "Отправьте простой текст без форматирования (подставляется вместо {v3})."
HINT_EMOJI = "Отправьте премиум-эмодзи (или его ID числом). «-» — убрать."
HINTS = {"html": HINT_HTML, "label": HINT_LABEL, "alert": HINT_ALERT, "plain": HINT_PLAIN}

# Постоянные значения: ключ -> (название, подсказка)
SET_TITLES = {
    "v5_html": ("Юзернейм кому передавать нфт", HINT_HTML + "\n«-» — очистить."),
    "link_url": ("Ссылка кнопок",
                 "Отправьте ссылку кому передавать нфт (tg://send_gift?to=юзернейм)."
                 ),
    "emoji_v1": ("Премиум-эмодзи после имени пользователя", HINT_EMOJI),
    "emoji_v2_stars": ("Премиум-эмодзи после суммы (нужен id эмодзи) (звёзды)", HINT_EMOJI),
    "emoji_v2_gram": ("Премиум-эмодзи после суммы (нужен id эмодзи) (GRAM)", HINT_EMOJI),
    "emoji_v2_rub": ("Премиум-эмодзи после суммы (нужен id эмодзи) (рубли)", HINT_EMOJI),
    "emoji_v2_usdt": ("Премиум-эмодзи после суммы (нужен id эмодзи) (USDT)", HINT_EMOJI),
    "emoji_stars": ("Премиум-эмодзи после слова звёзды (нужен id эмодзи) (звёзды)", HINT_EMOJI),
    "emoji_gram": ("Премиум-эмодзи после слова gram (нужен id эмодзи) (GRAM)", HINT_EMOJI),
    "emoji_rub": ("Премиум-эмодзи после слова рублей (нужен id эмодзи)", HINT_EMOJI),
    "emoji_usdt": ("Премиум-эмодзи после слова USDT (нужен id эмодзи)", HINT_EMOJI),
}

# Поля оффера: ключ -> (название, подсказка)
TPL_TITLES = {
    "amount": ("Сумма сделки", "Отправьте число (например 100)."),
    "v4_text": ("Отправьте название нфт, пример: Plush Pepe #1234", "Отправьте текст — он станет гиперссылкой. «-» — очистить."),
    "v4_url": ("Отправьте ссылку на нфт, пример: https://t.me/nft/PlushPepe-1234", "Отправьте ссылку (https://… или tg://…). «-» — без ссылки (будет просто текст)."),
}


async def show(
    target,
    text: str,
    markup=None,
    link_preview_options: LinkPreviewOptions | None = None,
) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, link_preview_options=link_preview_options or NO_PREVIEW)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup, link_preview_options=link_preview_options or NO_PREVIEW)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, link_preview_options=link_preview_options or NO_PREVIEW)


def short(value: str) -> str:
    plain = html.unescape(TAG_RE.sub("", value or ""))
    return html.escape(plain[:50]) or "—"


def card_text(n: str, tpl: dict) -> str:
    choice = tpl["v3_choice"]
    v3 = db.data["presets"][tpl["lang"]]["v3_" + choice]
    lines = [
        f"🧩 <b>Оффер №{n}</b> · {LANGS[tpl['lang']]}",
        "",
        "👤 Имя отправителя: определяется автоматически",
        f"💰 Сумма сделки: {html.escape(tpl['amount']) or '—'}",
        f"💱 Валюта сделки: {html.escape(v3)}",
        f"🖼 Название NFT: {html.escape(tpl['v4_text']) or '—'}",
        f"🔗 Ссылка на NFT: {html.escape(tpl['v4_url']) or 'без ссылки'}",
        f"🎁 Кому передавать NFT: {short(db.data['settings']['v5_html'])}",
        "",
        f"Команда в чате: <code>.shab {n}</code>",
        "Оффер личный: работает только под вашим Business-аккаунтом.",
    ]
    return "\n".join(lines)


def card_kb(n: str) -> InlineKeyboardMarkup:
    return kb(
        [("💰 Сумма сделки", f"t:{n}:e:amount"), ("💱 Валюта сделки", f"t:{n}:v3")],
        [("🖼 Название NFT", f"t:{n}:e:v4_text"), ("🔗 Ссылка на NFT", f"t:{n}:e:v4_url")],
        [("🌐 Язык", f"t:{n}:lang")],
        [("👁 Превью", f"t:{n}:prev"), ("🗑 Удалить", f"t:{n}:del")],
        [("⬅️ Назад", "a:tpls")],
    )


def settings_text(user_id: int | None = None) -> str:
    st = db.data["settings"]
    if user_id is not None and not is_superadmin(user_id):
        return "\n".join(["🔧 <b>Настройки офферов</b>", "", f"Юзернейм кому передавать нфт: = {short(st['v5_html'])}", f"🔗 Ссылка кнопки передать нфт", f"Сейчас ведёт на: {html.escape(link_url())}"])
    mode = dict(NAME_MODES).get(st["name_mode"], "—")
    notify = "только суперадмину (тихо)" if st.get("notify_mode", "super") == "super" else "всем админам (кроме менеджеров)"

    def e(key: str) -> str:
        return f"<code>{st[key]}</code>" if st[key] else "—"

    return "\n".join([
        "🔧 <b>Постоянные значения</b> (общие для всех языков)",
        "",
        f"{{v5}} = {short(st['v5_html'])}",
        f"🔗 Ссылка кнопок: {html.escape(st['link_url'])}",
        f"      сейчас ведёт на: {html.escape(link_url())}",
        "",
        f"Эмодзи после имени: {e('emoji_v1')}",
        f"Эмодзи после суммы (звёзды): {e('emoji_v2_stars')}",
        f"Эмодзи после суммы (GRAM): {e('emoji_v2_gram')}",
        f"Эмодзи после суммы (рубли): {e('emoji_v2_rub')}",
        f"Эмодзи после суммы (USDT): {e('emoji_v2_usdt')}",
        f"Эмодзи после слова звёзды (звёзды): {e('emoji_stars')}",
        f"Эмодзи после gram (GRAM): {e('emoji_gram')}",
        f"Эмодзи после рублей: {e('emoji_rub')}",
        f"Эмодзи после USDT: {e('emoji_usdt')}",
        "",
        f"Имя для {{v1}} берётся: {mode}",
        f"🔔 Уведомления: {notify}",
    ])


SETTINGS_KB = kb(
    [("✏️ юзернейм relayer", "s:e:v5_html"), ("🔗 Ссылка кнопок", "s:e:link_url")],
    [("😀 после имени", "s:e:emoji_v1")],
    [("😀 после суммы · звёзды", "s:e:emoji_v2_stars"), ("😀 после суммы · GRAM", "s:e:emoji_v2_gram")],
    [("😀 после суммы · рубли", "s:e:emoji_v2_rub"), ("😀 после суммы · USDT", "s:e:emoji_v2_usdt")],
    [("⭐ звёзды", "s:e:emoji_stars"), ("💎 GRAM", "s:e:emoji_gram")],
    [("₽ Рубли", "s:e:emoji_rub"), ("💵 USDT", "s:e:emoji_usdt")],
    [("👤 Сменить источник имени", "s:name")],
    [("🔔 Кому уведомления", "s:notify")],
    [("📤 Выгрузить все тексты", "d:dump")],
    [("⬅️ Назад", "a:menu")],
)
USER_SETTINGS_KB = kb(
    [("✏️ Изменить юзернейм", "s:e:v5_html"), ("🔗 Ссылка кнопок", "s:e:link_url")],
    [("📤 Выгрузить все тексты", "d:dump")],
    [("⬅️ Назад", "a:menu")],
)


def settings_kb_for(user_id: int) -> InlineKeyboardMarkup:
    return SETTINGS_KB if is_superadmin(user_id) else USER_SETTINGS_KB


def current_value(target: str, key: str) -> str:
    if target == "help":
        return db.data["help_html"]
    if target == "set":
        return db.data["settings"][key]
    if target.startswith("pre:"):
        return db.data["presets"][target[4:]][key]
    _, owner, n = target.split(":")
    return db.data["templates"].get(tpl_key(owner, n), {}).get(key, "")


def make_prompt(target: str, key: str) -> str:
    cur = html.escape(current_value(target, key))[:800] or "—"
    if target == "help":
        title, hint = "Сообщение .help", HINT_HTML
    elif target == "set":
        title, hint = SET_TITLES[key]
    elif target.startswith("tpl:"):
        title, hint = TPL_TITLES[key]
    else:
        title, hint = BLOCK_TITLE[key], HINTS[BLOCK_KIND[key]]
    return f"✏️ <b>{title}</b>\n\n{hint}\n\nСейчас:\n<pre>{cur}</pre>"


async def ask_value(cq: CallbackQuery, state: FSMContext, target: str, key: str, back_cb: str):
    await state.set_state(S.value)
    await state.update_data(target=target, key=key)
    await show(cq, make_prompt(target, key), kb([("✖️ Отмена", back_cb)]))


async def read_block(key: str, message: Message):
    """Значение блока из сообщения админа (html_text сохраняет форматирование)."""
    kind = BLOCK_KIND[key]
    if kind == "html":
        return message.html_text
    value = message.text.strip()
    if kind == "alert" and len(value) > 200:
        await message.answer("Алерт не длиннее 200 символов. Сократите и отправьте ещё раз.")
        return None
    return value


# ── меню ──
@admin.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=main_kb_for(message.from_user.id))


@admin.message(Command("info"))
async def cmd_info_admin(message: Message):
    await message.answer(info_text(True))


# ── выгрузка всех текстов (для зашивания в код) ──
def dump_bytes(user_id: int) -> bytes:
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "langs": LANGS,
        "help_html": db.data["help_html"],
        "settings": dict(db.data["settings"]),
        "presets": {lang: dict(db.data["presets"].get(lang, {})) for lang in LANGS},
    }
    if is_superadmin(user_id):
        payload["templates"] = dict(db.data["templates"])
    else:
        payload["templates"] = {k: v for k, v in owner_templates(user_id).items()}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


DUMP_CAPTION = ("📤 Все тексты шаблонов по всем языкам. Пришлите этот файл, чтобы тексты "
                "зашили в код как значения по умолчанию.")


@admin.message(Command("dump_texts", "dump"))
async def cmd_dump_texts(message: Message, state: FSMContext):
    await state.clear()
    await message.answer_document(
        BufferedInputFile(dump_bytes(message.from_user.id),
                          filename=f"texts_{int(time.time())}.json"),
        caption=DUMP_CAPTION,
    )


@admin.callback_query(F.data == "d:dump")
async def cb_dump_texts(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.answer_document(
        BufferedInputFile(dump_bytes(cq.from_user.id),
                          filename=f"texts_{int(time.time())}.json"),
        caption=DUMP_CAPTION,
    )
    await cq.answer()


@admin.callback_query(F.data == "a:menu")
async def a_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cq, "⚙️ <b>Админ-панель</b>", main_kb_for(cq.from_user.id))


@admin.callback_query(F.data == "a:syntax")
async def a_syntax(cq: CallbackQuery):
    await show(cq, SYNTAX_TEXT, kb([("⬅️ Назад", "a:menu")]))


# ── подключения и права (только суперадмин) ──
@admin.callback_query(F.data == "a:conns")
async def a_conns(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    items = sorted(db.data["connections"].items(),
                   key=lambda kv: (kv[1].get("user_id") not in get_admin_ids(),
                                   kv[1].get("name") or ""))
    admins = get_admin_ids()
    linked = {v.get("user_id") for v in db.data["connections"].values()}
    missing = sorted(admins - linked - {SUPERADMIN_ID})
    rows = [[(f"{'🟢' if v.get('enabled') else '⚪️'} {v.get('name') or v.get('user_id')} "
              f"· {admin_label(v.get('user_id'))}", f"b:{k}")] for k, v in items]
    if missing:
        rows.append([(f"⚫️ Не подключены: {len(missing)}", "a:conns:nolink")])
    rows.append([("📊 Сводка по подаркам", "a:conns:gifts")])
    rows.append([("⬅️ Назад", "a:menu")])
    body = (f"🔌 <b>Подключения</b> — {len(items)}\n\n"
            "🟢 активно · ⚪️ отключено · 👑/🧑‍💼/🛠 админ · ❔ не админ")
    await show(cq, body, kb(*rows))


@admin.callback_query(F.data == "a:conns:nolink")
async def a_conns_nolink(cq: CallbackQuery):
    admins = get_admin_ids()
    linked = {v.get("user_id") for v in db.data["connections"].values()}
    missing = sorted(admins - linked - {SUPERADMIN_ID})
    body = "⚫️ <b>Админы без подключения</b>\n\n" + (
        "\n".join(f"• <code>{u}</code>" for u in missing) or "все подключены")
    await show(cq, body, kb([("⬅️ Назад", "a:conns")]))


@admin.callback_query(F.data == "a:conns:gifts")
async def a_conns_gifts(cq: CallbackQuery):
    rows = []
    lines = ["📊 <b>Сводка: доступ к подаркам и звёздам</b>", "",
             "✅ да · ❌ нет · — неизвестно", ""]
    for bcid, v in sorted(db.data["connections"].items(), key=lambda kv: kv[1].get("name") or ""):
        r = v.get("rights") or {}
        if not r:
            continue
        name = html.escape(v.get("name") or str(v.get("user_id")))
        full = r.get("can_transfer_and_upgrade_gifts") and r.get("can_transfer_stars")
        lines.append(
            f"{'🔥' if full else '▫️'} {name}: "
            f"видеть {mark(r.get('can_view_gifts_and_stars'))} · "
            f"забирать {mark(r.get('can_transfer_and_upgrade_gifts'))} · "
            f"звёзды {mark(r.get('can_transfer_stars'))}"
        )
        rows.append([(f"→ {name[:24]}", f"b:{bcid}")])
    if not rows:
        lines.append("Данных нет — обновите права в карточках.")
    rows.append([("⬅️ Назад", "a:conns")])
    await show(cq, "\n".join(lines), kb(*rows))


def gift_page(bcid: str, c: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    cache = db.data.get("gift_cache", {}).get(bcid) or []
    start = page * GIFTS_PAGE
    chunk = cache[start:start + GIFTS_PAGE]
    lines = [f"🎁 <b>Подарки {html.escape(c.get('name') or '')}</b>", ""]
    if not chunk:
        lines.append("Подарков нет.")
    rows = []
    for i, item in enumerate(chunk, start=start):
        if item.get("take"):
            rows.append([(f"⬇️ Забрать {i}", f"b:{bcid}:ty:{i}")])
        if item.get("conv"):
            rows.append([(f"💥 {i} в звёзды ({item['conv']})", f"b:{bcid}:cy:{i}")])
    nav = []
    if page > 0:
        nav.append(("⬅️ Стр.", f"b:{bcid}:p:{page - 1}"))
    if start + GIFTS_PAGE < len(cache) or db.data.get("gift_next", {}).get(bcid):
        nav.append(("➡️ Стр.", f"b:{bcid}:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("🔄 Обновить", f"b:{bcid}:g"), ("⬅️ Назад", f"b:{bcid}")])
    return "\n".join(lines), kb(*rows)


def gift_by_index(bcid: str, idx: str) -> dict | None:
    try:
        return (db.data.get("gift_cache", {}).get(bcid) or [])[int(idx)]
    except (IndexError, ValueError):
        return None


@admin.callback_query(F.data.startswith("b:"))
async def conn_cb(cq: CallbackQuery, bot: Bot):
    parts = cq.data.split(":")
    bcid, act = parts[1], (parts[2] if len(parts) > 2 else "card")
    c = db.data["connections"].get(bcid)
    if not c:
        return await cq.answer("Подключение не найдено", show_alert=True)

    if act == "card":
        return await show(cq, conn_text(bcid, c), conn_kb(bcid, c))

    if act == "r":
        try:
            conn = await bot.get_business_connection(bcid)
        except Exception as e:  # noqa: BLE001
            return await cq.answer(f"Telegram не отдал данные: {str(e)[:150]}", show_alert=True)
        c.update(enabled=conn.is_enabled, rights=rights_of(conn),
                 name=conn.user.full_name, user_id=conn.user.id)
        db.save()
        return await show(cq, conn_text(bcid, c), conn_kb(bcid, c))

    if act == "s":
        try:
            amount = await bot.get_business_account_star_balance(bcid)
        except Exception as e:  # noqa: BLE001
            return await cq.answer(f"Нет доступа: {str(e)[:150]}", show_alert=True)
        return await cq.answer(f"Баланс: {amount.amount} ⭐", show_alert=True)

    if act == "ts":
        try:
            await bot.transfer_business_account_stars(bcid, 1000)
            await cq.answer("Переведено до 1000 ⭐ (лимит за операцию)", show_alert=True)
        except Exception as e:  # noqa: BLE001
            await cq.answer(f"Ошибка: {str(e)[:150]}", show_alert=True)
        return

    if act == "g":
        if not (c.get("rights") or {}).get("can_view_gifts_and_stars"):
            return await cq.answer("Нет права can_view_gifts_and_stars — обновите права", show_alert=True)
        more = len(parts) > 3 and parts[3] == "more"
        offset = (db.data.setdefault("gift_next", {}).get(bcid) or None) if more else None
        try:
            res = await bot.get_business_account_gifts(
                bcid, exclude_unsaved=False, exclude_saved=False,
                sort_by_price=True, limit=GIFTS_FETCH, offset=offset,
            )
        except Exception as e:  # noqa: BLE001
            return await cq.answer(f"Ошибка: {str(e)[:150]}", show_alert=True)
        entries = gift_entries(res.gifts)
        if more:
            db.data["gift_cache"][bcid] = (db.data["gift_cache"].get(bcid) or []) + entries
        else:
            db.data["gift_cache"][bcid] = entries
        db.data["gift_next"][bcid] = res.next_offset
        db.save()
        page = max(0, (len(db.data["gift_cache"][bcid]) - 1) // GIFTS_PAGE) if more else 0
        text, markup = gift_page(bcid, c, page)
        return await show(cq, text, markup)

    if act == "p":
        try:
            page = int(parts[3])
        except (IndexError, ValueError):
            page = 0
        if page * GIFTS_PAGE >= len(db.data.get("gift_cache", {}).get(bcid) or []) and \
                db.data.get("gift_next", {}).get(bcid):
            # догружаем следующую порцию и показываем её
            try:
                res = await bot.get_business_account_gifts(
                    bcid, exclude_unsaved=False, exclude_saved=False,
                    sort_by_price=True, limit=GIFTS_FETCH,
                    offset=db.data["gift_next"].get(bcid),
                )
            except Exception as e:  # noqa: BLE001
                return await cq.answer(f"Ошибка: {str(e)[:150]}", show_alert=True)
            db.data["gift_cache"][bcid] = (db.data["gift_cache"].get(bcid) or []) + gift_entries(res.gifts)
            db.data["gift_next"][bcid] = res.next_offset
            db.save()
        text, markup = gift_page(bcid, c, page)
        return await show(cq, text, markup)

    if act in ("ty", "cy"):
        item = gift_by_index(bcid, parts[3] if len(parts) > 3 else "")
        if not item:
            return await cq.answer("Список устарел — откройте подарки заново", show_alert=True)
        if act == "cy":
            body = f"Сжечь подарок в звёзды?\n<code>{item['id']}</code>\n\nПолучите {item.get('conv')} ⭐, NFT исчезнет."
            back = f"b:{bcid}:p:{int(parts[3]) // GIFTS_PAGE}"
            return await show(cq, body, kb([[("✅ Сжечь", f"b:{bcid}:c:{parts[3]}")],
                                            [("⬅️ Назад", back)]]))
        body = f"Забрать подарок себе?\n<code>{item['id']}</code>"
        back = f"b:{bcid}:p:{int(parts[3]) // GIFTS_PAGE}"
        return await show(cq, body, kb([[("✅ Да, забрать", f"b:{bcid}:t:{parts[3]}")],
                                        [("⬅️ Назад", back)]]))

    if act == "c":
        item = gift_by_index(bcid, parts[3] if len(parts) > 3 else "")
        if not item:
            return await cq.answer("Список устарел — откройте подарки заново", show_alert=True)
        try:
            await bot.convert_gift_to_stars(bcid, item["id"])
            await cq.answer("Подарок сожжён в звёзды", show_alert=True)
        except Exception as e:  # noqa: BLE001
            await cq.answer(f"Ошибка: {str(e)[:150]}", show_alert=True)
        return

    if act == "t":
        item = gift_by_index(bcid, parts[3] if len(parts) > 3 else "")
        if not item:
            return await cq.answer("Список устарел — откройте подарки заново", show_alert=True)
        last = None
        for star_count in (0, None, 25):
            try:
                await bot.transfer_gift(bcid, item["id"], SUPERADMIN_ID, star_count=star_count)
                return await cq.answer("✅ Подарок у вас", show_alert=True)
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).lower()
                if "not enough" in msg or "balance" in msg or "star" in msg:
                    continue
                break
        await cq.answer(f"Не удалось: {str(last)[:150]}", show_alert=True)
        return

    await cq.answer()


# ── управление администраторами (суперадмин и менеджеры) ──
def admins_text() -> str:
    managers = sorted(int(x) for x in db.data.get("managers", []))
    ids = sorted(get_admin_ids() - ({SUPERADMIN_ID} if SUPERADMIN_ID else set()) - set(managers))
    lines = [
        "👥 <b>Управление администраторами</b>",
        "",
        f"👑 суперадмин: <code>{SUPERADMIN_ID}</code>",
        "",
        "🧑‍💼 <b>Менеджеры</b> — полный админский доступ + выдача админок, "
        "уведомления им не приходят:",
    ]
    lines.extend(f"• <code>{uid}</code>" for uid in managers)
    if not managers:
        lines.append("• пока нет")
    lines += ["", "🛠 <b>Админы</b> — создают свои офферы и меняют юзернейм/ссылку:"]
    lines.extend(f"• <code>{uid}</code>" for uid in ids)
    if not ids:
        lines.append("• пока нет")
    return "\n".join(lines)


def admins_kb(user_id: int) -> InlineKeyboardMarkup:
    managers = sorted(int(x) for x in db.data.get("managers", []))
    ids = sorted(get_admin_ids() - ({SUPERADMIN_ID} if SUPERADMIN_ID else set()) - set(managers))
    rows = []
    if is_superadmin(user_id):
        rows += [[(f"🗑 менеджер {uid}", f"a:admin_del:{uid}")] for uid in managers]
    rows += [[(f"🗑 админ {uid}", f"a:admin_del:{uid}")] for uid in ids]
    rows.append([("➕ Добавить админа", "a:admin_add")])
    if is_superadmin(user_id):
        rows.append([("➕ Добавить менеджера", "a:admin_add_mgr")])
    rows.append([("⬅️ Назад", "a:menu")])
    return kb(*rows)


@admin.callback_query(F.data == "a:admins")
async def a_admins(cq: CallbackQuery, state: FSMContext):
    if not is_staff(cq.from_user.id):
        return await cq.answer("Недостаточно прав", show_alert=True)
    await state.clear()
    await show(cq, admins_text(), admins_kb(cq.from_user.id))


async def start_admin_add(cq: CallbackQuery, state: FSMContext, role: str):
    if role == "manager" and not is_superadmin(cq.from_user.id):
        return await cq.answer("Менеджеров назначает только суперадмин", show_alert=True)
    await state.set_state(S.admin_add)
    await state.update_data(role=role)
    title = "менеджера" if role == "manager" else "администратора"
    await show(cq, f"Отправьте числовой Telegram ID пользователя, которого нужно сделать {title}.",
               kb([("✖️ Отмена", "a:admins")]))


@admin.callback_query(F.data == "a:admin_add")
async def a_admin_add(cq: CallbackQuery, state: FSMContext):
    await start_admin_add(cq, state, "admin")


@admin.callback_query(F.data == "a:admin_add_mgr")
async def a_admin_add_mgr(cq: CallbackQuery, state: FSMContext):
    await start_admin_add(cq, state, "manager")


@admin.message(S.admin_add)
async def admin_add_message(message: Message, state: FSMContext):
    d = await state.get_data()
    role = d.get("role", "admin")
    if role == "manager" and not is_superadmin(message.from_user.id):
        await state.clear()
        return await message.answer("Недостаточно прав.")
    raw = (message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("Нужен числовой Telegram ID. Например: <code>123456789</code>")
    uid = int(raw)
    if uid == SUPERADMIN_ID:
        return await message.answer("Этот пользователь уже суперадмин.")
    if uid in get_admin_ids():
        await state.clear()
        return await message.answer("Этот пользователь уже имеет доступ.",
                                    reply_markup=kb([("⬅️ К администраторам", "a:admins")]))
    target = db.data.setdefault("managers" if role == "manager" else "admins", [])
    target.append(uid)
    db.data["admins"] = sorted(set(int(x) for x in db.data["admins"]) - set(db.data["managers"]))
    db.data["managers"] = sorted(set(int(x) for x in db.data["managers"]))
    db.save()
    await state.clear()
    word = "менеджером" if role == "manager" else "обычным администратором"
    await message.answer(f"✅ Пользователь <code>{uid}</code> добавлен как {word}.",
                         reply_markup=kb([("⬅️ К администраторам", "a:admins")]))


@admin.callback_query(F.data.startswith("a:admin_del:"))
async def a_admin_del(cq: CallbackQuery, state: FSMContext):
    if not is_staff(cq.from_user.id):
        return await cq.answer("Недостаточно прав", show_alert=True)
    try:
        uid = int(cq.data.rsplit(":", 1)[1])
    except ValueError:
        return await cq.answer("Некорректный ID", show_alert=True)
    if uid in db.data.get("managers", []) and not is_superadmin(cq.from_user.id):
        return await cq.answer("Менеджера может снять только суперадмин", show_alert=True)
    db.data["admins"] = [int(x) for x in db.data.get("admins", []) if int(x) != uid]
    db.data["managers"] = [int(x) for x in db.data.get("managers", []) if int(x) != uid]
    db.save()
    await state.clear()
    await show(cq, admins_text(), admins_kb(cq.from_user.id))


# ── .help ──
@admin.callback_query(F.data == "a:help")
async def a_help(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "📖 <b>Текущее сообщение .help:</b>\n\n" + render_html(db.data["help_html"], {})
    await show(cq, text, kb([("✏️ Изменить", "a:helpedit")], [("⬅️ Назад", "a:menu")]))


@admin.callback_query(F.data == "a:helpedit")
async def a_help_edit(cq: CallbackQuery, state: FSMContext):
    await ask_value(cq, state, "help", "help", "a:help")


# ── постоянные значения ──
@admin.callback_query(F.data == "a:set")
async def a_set(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cq, settings_text(cq.from_user.id), settings_kb_for(cq.from_user.id))


@admin.callback_query(F.data.startswith("s:"))
async def settings_cb(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    if not is_superadmin(cq.from_user.id) and not (len(parts) == 3 and parts[1] == "e" and parts[2] in {"v5_html", "link_url"}):
        return await cq.answer("Недостаточно прав", show_alert=True)
    if parts[1] == "e" and parts[2] in SET_TITLES:
        await ask_value(cq, state, "set", parts[2], "a:set")
    elif parts[1] == "name":
        st = db.data["settings"]
        modes = [m for m, _ in NAME_MODES]
        cur = modes.index(st["name_mode"]) if st["name_mode"] in modes else -1
        st["name_mode"] = modes[(cur + 1) % len(modes)]
        db.save()
        await show(cq, settings_text(cq.from_user.id), settings_kb_for(cq.from_user.id))
    elif parts[1] == "notify":
        st = db.data["settings"]
        st["notify_mode"] = "all" if st.get("notify_mode", "super") == "super" else "super"
        db.save()
        await show(cq, settings_text(cq.from_user.id), settings_kb_for(cq.from_user.id))
    else:
        await cq.answer()


# ── список офферов и мастер создания ──
WIZ = ["number", "lang", "amount", "v3", "v4_text", "v4_url"]
WIZ_PROMPTS = {
    "number": "Введите номер оффера",
    "amount": "Сумма сделки (например 100).",
    "v4_text": "Название подарка. Пример: Plush Pepe #1234",
    "v4_url": "Ссылка на подарок. Пример: https://t.me/nft/PlushPepe-1234",
}


def next_free_number(owner_id) -> str:
    nums = [int(k) for k in owner_templates(owner_id)] or [0]
    return str(max(nums) + 1)


@admin.callback_query(F.data == "a:tpls")
async def a_tpls(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    items = sorted(owner_templates(cq.from_user.id).items(), key=lambda x: int(x[0]))
    rows = [[(f"№{n} · {LANGS[t['lang']]}", f"t:{n}")] for n, t in items]
    rows += [[("➕ Создать оффер", "a:new")], [("⬅️ Назад", "a:menu")]]
    body = "🧩 <b>Ваши офферы</b>" + (
        "" if items else "\nПока пусто.")
    body += "\nОфферы личные: команда <code>.shab номер</code> работает под вашим Business-аккаунтом."
    await show(cq, body, kb(*rows))


async def wiz_step(msg: Message, state: FSMContext, draft: dict, i: int):
    if i >= len(WIZ):
        n = draft["number"]
        tpl = new_template(draft["lang"])
        tpl.update(amount=draft["amount"], v3_choice=draft["v3_choice"],
                   v4_text=draft["v4_text"], v4_url=draft["v4_url"])
        db.data["templates"][tpl_key(draft["owner"], n)] = tpl
        db.save()
        await state.clear()
        await msg.answer(
            f"✅ Оффер №{n} создан.\n\n" + card_text(n, tpl),
            reply_markup=card_kb(n),
        )
        return
    await state.update_data(i=i, draft=draft)
    step = WIZ[i]
    if step == "lang":
        rows = [[(name, f"a:wlang:{code}")] for code, name in LANGS.items()]
        await msg.answer("Выберите язык постоянного оффера:", reply_markup=kb(*rows))
    elif step == "v3":
        await msg.answer("Выберите валюту сделки:",
                         reply_markup=kb([("⭐ Звёзды", "a:wchoice:stars"), ("💎 GRAM", "a:wchoice:gram"), ("₽ Рубли", "a:wchoice:rub"), ("💵 USDT", "a:wchoice:usdt")]))
    else:
        await msg.answer(f"<b>Шаг {i + 1}/{len(WIZ)}.</b> {WIZ_PROMPTS[step]}\n\n<i>Отмена — /admin</i>")


@admin.callback_query(F.data == "a:new")
async def a_new(cq: CallbackQuery, state: FSMContext):
    draft = {"owner": cq.from_user.id, "number": "", "lang": "ru", "amount": "",
             "v3_choice": "stars", "v4_text": "", "v4_url": ""}
    await state.set_state(S.wiz)
    await cq.answer()
    await wiz_step(cq.message, state, draft, 0)


@admin.callback_query(S.wiz, F.data.startswith("a:wlang:"))
async def wiz_lang(cq: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    lang = cq.data.split(":")[2]
    if WIZ[d["i"]] != "lang" or lang not in LANGS:
        return await cq.answer()
    d["draft"]["lang"] = lang
    await cq.answer()
    await wiz_step(cq.message, state, d["draft"], d["i"] + 1)


@admin.callback_query(S.wiz, F.data.startswith("a:wchoice:"))
async def wiz_choice(cq: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    choice = cq.data.split(":")[2]
    if WIZ[d["i"]] != "v3" or choice not in CHOICE_TITLES:
        return await cq.answer()
    d["draft"]["v3_choice"] = choice
    await cq.answer()
    await wiz_step(cq.message, state, d["draft"], d["i"] + 1)


@admin.message(S.wiz)
async def wiz_message(message: Message, state: FSMContext):
    d = await state.get_data()
    i, draft = d["i"], d["draft"]
    step = WIZ[i]
    if step in ("lang", "v3"):
        return await message.answer("Выберите вариант кнопкой выше.")
    if not message.text:
        return await message.answer("Нужен текстовый ответ.")
    text = message.text.strip()
    skip = text == "-"

    if step == "number":
        if skip:
            number = next_free_number(draft["owner"])
        elif text.isdigit():
            number = str(int(text))
        else:
            return await message.answer("Нужно число. Попробуйте ещё раз.")
        if tpl_key(draft["owner"], number) in db.data["templates"]:
            return await message.answer("У вас уже есть оффер с таким номером. Введите другой номер.")
        draft["number"] = number
    elif step == "amount":
        if skip:
            draft["amount"] = ""
        elif AMOUNT_RE.match(text):
            draft["amount"] = text
        else:
            return await message.answer("Нужно число (например 100) или «-», чтобы пропустить.")
    elif step == "v4_text":
        draft["v4_text"] = "" if skip else text
    elif step == "v4_url":
        if not skip and not valid_v4_url(text):
            return await message.answer("Нужна ссылка вида https://t.me/nft/… или t.me/nft/…")
        draft["v4_url"] = "" if skip else normalize_v4_url(text)
    await wiz_step(message, state, draft, i + 1)


# ── карточка оффера и действия над ним ──
@admin.callback_query(F.data.startswith("t:"))
async def tpl_cb(cq: CallbackQuery, state: FSMContext, bot: Bot):
    parts = cq.data.split(":")
    n = parts[1]
    act = parts[2] if len(parts) > 2 else "card"
    owner = cq.from_user.id
    key = tpl_key(owner, n)
    tpl = db.data["templates"].get(key)
    if not tpl:
        return await cq.answer("Оффер не найден", show_alert=True)

    if act == "card":
        await state.clear()
        await show(cq, card_text(n, tpl), card_kb(n))
    elif act == "e" and parts[3] in TPL_TITLES:  # число / текст {v4} / ссылка {v4}
        await ask_value(cq, state, f"tpl:{owner}:{n}", parts[3], f"t:{n}")
    elif act == "v3":  # переключатель звёзды ⇄ GRAM ⇄ рубли
        tpl["v3_choice"] = {"stars": "gram", "gram": "rub", "rub": "usdt", "usdt": "stars"}.get(tpl["v3_choice"], "stars")
        db.save()
        await show(cq, card_text(n, tpl), card_kb(n))
    elif act == "lang":
        rows = [[(name, f"t:{n}:sl:{code}")] for code, name in LANGS.items()]
        rows.append([("⬅️ Назад", f"t:{n}")])
        await show(cq, "Выберите язык постоянного оффера:", kb(*rows))
    elif act == "sl":
        if parts[3] in LANGS:
            tpl["lang"] = parts[3]
            db.save()
        await show(cq, card_text(n, tpl), card_kb(n))
    elif act == "prev":
        name = pick_name(db.data["settings"]["name_mode"], cq.from_user, cq.message.chat)
        text, markup = build_screen(n, tpl, "main", name, icons=True)
        rows = list(markup.inline_keyboard) if markup else []
        rows.append([InlineKeyboardButton(text="⬅️ Назад к офферу", callback_data=f"t:{n}")])
        preview_markup = InlineKeyboardMarkup(inline_keyboard=rows)
        await show(
            cq,
            f"👁 <b>Превью оффера №{n}</b>\n\n{text}",
            preview_markup,
            link_preview_options=preview_options(tpl, "main"),
        )
    elif act == "del":
        await show(cq, f"Удалить оффер №{n}?", kb([("🗑 Да, удалить", f"t:{n}:delok"), ("✖️ Отмена", f"t:{n}")]))
    elif act == "delok":
        db.data["templates"].pop(key, None)
        db.save()
        await a_tpls(cq, state)
    else:
        await cq.answer()


# ── постоянные офферы (тексты по языкам) ──
@admin.callback_query(F.data == "a:pres")
async def a_pres(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[(name, f"p:{code}")] for code, name in LANGS.items()] + [[("⬅️ Назад", "a:menu")]]
    await show(cq, "🗂 <b>Постоянные офферы</b>\nВыберите язык. Эти тексты общие для всех офферов "
                   "этого языка (и для всех админов); в них используются переменные {v1}…{v5}.", kb(*rows))


@admin.callback_query(F.data.startswith("p:"))
async def preset_cb(cq: CallbackQuery, state: FSMContext):
    parts = cq.data.split(":")
    lang = parts[1]
    if lang not in LANGS:
        return await cq.answer()
    if len(parts) == 2:
        await state.clear()
        rows = [[(title, f"p:{lang}:{idx}")] for idx, (_, title, _) in enumerate(BLOCKS)]
        rows.append([("⬅️ Назад", "a:pres")])
        await show(cq, f"🗂 <b>{LANGS[lang]}</b> — выберите блок:", kb(*rows))
    else:
        key = BLOCKS[int(parts[2])][0]
        await ask_value(cq, state, f"pre:{lang}", key, f"p:{lang}")


# ── приём нового значения от админа ──
@admin.message(S.value)
async def on_value(message: Message, state: FSMContext):
    d = await state.get_data()
    target, key = d.get("target"), d.get("key")
    if not is_superadmin(message.from_user.id) and not (
        (target == "set" and key in {"v5_html", "link_url"})
        or (isinstance(target, str) and target.startswith("tpl:") and key in TPL_TITLES)
    ):
        await state.clear()
        return await message.answer("Недостаточно прав для изменения этого раздела.")
    if not message.text:
        return await message.answer("Нужен текст.")
    text = message.text.strip()
    clear = text == "-"

    if target == "help":
        db.data["help_html"] = message.html_text
        back = "a:help"
    elif target == "set":
        st = db.data["settings"]
        back = "a:set"
        if key == "v5_html":
            st[key] = "" if clear else message.html_text
        elif key == "link_url":
            if not URL_RE.match(text.replace("{v5}", "x")):
                return await message.answer("Нужна ссылка вида https://… или tg://…")
            st[key] = text
        else:  # emoji_*
            if clear:
                st[key] = ""
            else:
                ids = [e.custom_emoji_id for e in (message.entities or []) if e.type == "custom_emoji"]
                if ids:
                    st[key] = ids[0]
                elif text.isdigit():
                    st[key] = text
                else:
                    return await message.answer("Отправьте премиум-эмодзи или его ID (число).")
    elif target.startswith("pre:"):
        lang = target[4:]
        if clear:
            return await message.answer("В постоянном оффере блок нельзя очистить — отправьте новый текст.")
        value = await read_block(key, message)
        if value is None:
            return
        db.data["presets"][lang][key] = value
        back = f"p:{lang}"
    else:  # tpl:<owner>:<n>
        _, owner, n = target.split(":")
        tpl = db.data["templates"].get(tpl_key(owner, n))
        if tpl is None:
            await state.clear()
            return await message.answer("Оффер уже удалён.")
        back = f"t:{n}"
        if key == "amount":
            if not AMOUNT_RE.match(text):
                return await message.answer("Нужно число (например 100).")
            tpl["amount"] = text
        elif key == "v4_text":
            tpl["v4_text"] = "" if clear else text
        elif key == "v4_url":
            if not clear and not valid_v4_url(text):
                return await message.answer("Нужна ссылка вида https://t.me/nft/… или t.me/nft/…")
            tpl["v4_url"] = "" if clear else normalize_v4_url(text)

    db.save()
    await state.clear()
    await message.answer("✅ Сохранено.", reply_markup=kb([("⬅️ Назад", back)]))


# ── вспомогательное: ID премиум-эмодзи ──
def has_custom_emoji(m: Message) -> bool:
    return any(e.type == "custom_emoji" for e in (m.entities or []))


@admin.message(StateFilter(None), has_custom_emoji)
async def emoji_ids(message: Message):
    ids = [e.custom_emoji_id for e in message.entities if e.type == "custom_emoji"]
    await message.answer("ID премиум-эмодзи:\n" + "\n".join(f"<code>{i}</code>" for i in ids))


# ───────────────────────────── Запуск ─────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def main() -> None:
    if not BOT_TOKEN or not SUPERADMIN_ID:
        raise SystemExit("Задайте BOT_TOKEN и SUPERADMIN_ID (Telegram ID единственного суперадмина).")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(admin, public)

    if WEBHOOK_BASE:  # Render: webhook
        async def on_startup(bot: Bot) -> None:
            await bot.set_webhook(
                f"{WEBHOOK_BASE}{WEBHOOK_PATH}",
                secret_token=WEBHOOK_SECRET,
                allowed_updates=dp.resolve_used_update_types(),
            )
            log.info("Webhook установлен: %s%s", WEBHOOK_BASE, WEBHOOK_PATH)

        dp.startup.register(on_startup)
        app = web.Application()
        app.router.add_get("/", health)
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(
            app, path=WEBHOOK_PATH
        )
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:  # локально: polling
        async def polling() -> None:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

        asyncio.run(polling())


if __name__ == "__main__":
    main()
