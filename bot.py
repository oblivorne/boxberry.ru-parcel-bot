import logging
import os
import json
import re
import asyncio
from typing import Optional, Dict, List, Tuple, Any
from functools import wraps, lru_cache
from contextlib import asynccontextmanager
from dataclasses import dataclass
import aiohttp
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)
from db import SessionLocal, init_db, User, Parcel
from sqlalchemy.exc import IntegrityError
from thefuzz import process, fuzz
import pymorphy2

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Состояния для обработчиков разговоров
REGISTER_LOGIN, REGISTER_PASSWORD, REGISTER_NAME, REGISTER_SURNAME = range(4)
LOGIN_LOGIN, LOGIN_PASSWORD = range(4, 6)
ADD_TRACKING = 6
CHANGE_OLD_PASSWORD, CHANGE_NEW_PASSWORD = range(7, 9)
CALC_STORAGE, CALC_CITY_SEARCH, CALC_CITY_SELECT, CALC_WEIGHT, CALC_DELIVERY = range(
    9, 14
)


# Класс конфигурации
@dataclass
class Config:
    TELEGRAM_TOKEN: str
    BASE_URL: str = "https://boxberry.ru"
    MAX_RETRIES: int = 3
    REQUEST_TIMEOUT: int = 30
    MAX_MESSAGE_LENGTH: int = 4000
    CACHE_TTL: int = 300  # 5 минут

    @classmethod
    def from_env(cls):
        return cls(
            TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN"),
            BASE_URL=os.getenv("BOT_BASE_URL", "https://boxberry.ru"),
        )


config = Config.from_env()


# Шаблон Singleton для управления данными
class DataManager:
    _instance = None
    _keywords = None
    _restrictions = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    @lru_cache(maxsize=1)
    def keywords(self) -> Dict:
        if self._keywords is None:
            try:
                with open("keywords_mapping.json", "r", encoding="utf-8") as f:
                    self._keywords = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                logger.error(f"Не удалось загрузить ключевые слова: {e}")
                self._keywords = {}
        return self._keywords

    @property
    @lru_cache(maxsize=1)
    def restrictions(self) -> Dict:
        if self._restrictions is None:
            try:
                with open("restrictions.json", "r", encoding="utf-8") as f:
                    self._restrictions = json.load(f)["countries"]
            except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
                logger.error(f"Не удалось загрузить ограничения: {e}")
                self._restrictions = {}
        return self._restrictions


data_manager = DataManager()

# Морфологический анализатор
morph = pymorphy2.MorphAnalyzer()

# Шаблон для трек-номеров
TRACKING_PATTERN = re.compile(r"^[A-Z0-9\-]{8,}$")


# Декораторы
def async_db_session(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        session = SessionLocal()
        try:
            result = await func(session, *args, **kwargs)
            session.commit()
            return result
        except Exception as e:
            logger.error(f"Ошибка базы данных в {func.__name__}: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    return wrapper


def handle_errors(send_error_message: bool = True):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Ошибка в {func.__name__}: {e}", exc_info=True)
                if send_error_message and len(args) >= 2:
                    update = args[0] if hasattr(args[0], "message") else args[1]
                    try:
                        await safe_send_message(
                            update, "❌ Произошла ошибка. Пожалуйста, попробуйте позже."
                        )
                    except:
                        pass
                return None

        return wrapper

    return decorator


# Менеджер HTTP-клиента
class HTTPManager:
    _session = None

    @classmethod
    @asynccontextmanager
    async def get_session(cls):
        if cls._session is None or cls._session.closed:
            timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)
            cls._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/xml",
                    "Origin": "https://bxbox.boxberry.ru",
                    "Referer": "https://bxbox.boxberry.ru/",
                },
            )
        try:
            yield cls._session
        finally:
            pass

    @classmethod
    async def close(cls):
        if cls._session and not cls._session.closed:
            await cls._session.close()


# Обработчик сообщений
class TextMessageHandler:
    @staticmethod
    def split_message(text: str, max_length: int = None) -> List[str]:
        max_length = max_length or config.MAX_MESSAGE_LENGTH
        if len(text) <= max_length:
            return [text]

        parts = []
        current = ""

        for line in text.split("\n"):
            if len(current + line + "\n") > max_length:
                if current:
                    parts.append(current.rstrip())
                    current = ""
                if len(line) > max_length:
                    words = line.split(" ")
                    temp = ""
                    for word in words:
                        if len(temp + word + " ") > max_length:
                            if temp:
                                parts.append(temp.rstrip())
                            temp = word + " "
                        else:
                            temp += word + " "
                    current = temp
                else:
                    current = line + "\n"
            else:
                current += line + "\n"

        if current:
            parts.append(current.rstrip())

        return parts


# Менеджер кэша
class CacheManager:
    _cache: Dict = {}

    @classmethod
    def get(cls, key: str) -> Optional[Any]:
        if key in cls._cache:
            data, timestamp = cls._cache[key]
            if asyncio.get_event_loop().time() - timestamp < config.CACHE_TTL:
                return data
            del cls._cache[key]
        return None

    @classmethod
    def set(cls, key: str, value: Any):
        cls._cache[key] = (value, asyncio.get_event_loop().time())


# API Boxberry
class BoxberryAPI:
    @staticmethod
    async def get_cities(city_name: str) -> List[Dict[str, str]]:
        cache_key = f"cities_{city_name.lower()}"
        cached = CacheManager.get(cache_key)
        if cached:
            return cached

        for attempt in range(config.MAX_RETRIES):
            try:
                async with HTTPManager.get_session() as session:
                    url = "https://lk.boxberry.ru/int-import-api/get-cities-list/"
                    params = {"q": city_name}
                    async with session.get(url, params=params) as response:
                        if response.status == 200:
                            data = await response.text()
                            cities = BoxberryAPI._parse_cities_xml(data)
                            CacheManager.set(cache_key, cities)
                            return cities
                        elif attempt < config.MAX_RETRIES - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        else:
                            return []
            except asyncio.TimeoutError:
                logger.error(f"Тайм-аут на попытке {attempt + 1}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
            except Exception as e:
                logger.error(f"Ошибка API: {e}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
        return []

    @staticmethod
    def _parse_cities_xml(xml_data: str) -> List[Dict[str, str]]:
        try:
            root = ET.fromstring(xml_data)
            return [
                {"id": item.find("id").text, "text": item.find("text").text}
                for item in root.findall("item")
                if item.find("id") is not None
                and item.find("text") is not None
                and item.find("id").text
                and item.find("text").text
            ]
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML: {e}")
            return []

    @staticmethod
    async def calculate_delivery_cost(
        storage_id: str, city_id: str, weight: float, courier: bool
    ) -> Optional[str]:
        for attempt in range(config.MAX_RETRIES):
            try:
                async with HTTPManager.get_session() as session:
                    url = "https://lk.boxberry.ru/int-import-api/calculate/"
                    params = {
                        "cityCode": city_id,
                        "storage_id": storage_id,
                        "weight": str(weight),
                        "courier": "1" if courier else "0",
                    }
                    async with session.get(url, params=params) as response:
                        if response.status == 200:
                            data = await response.text()
                            if not data.strip():
                                logger.error("API вернул пустой ответ")
                                return None
                            try:
                                root = ET.fromstring(data)
                                error_elem = root.find("error")
                                if error_elem is not None and error_elem.text == "true":
                                    error_msg = root.find("errorMessage")
                                    error_text = (
                                        error_msg.text
                                        if error_msg is not None
                                        else "Неизвестная ошибка"
                                    )
                                    logger.error(f"Ошибка расчета: {error_text}")
                                    return None
                                cost_elem = root.find("cost")
                                return cost_elem.text if cost_elem is not None else None
                            except ET.ParseError as xml_err:
                                logger.error(f"Ошибка парсинга XML: {xml_err}")
                                return None
                        elif attempt < config.MAX_RETRIES - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        return None
            except asyncio.TimeoutError:
                logger.error(f"Тайм-аут на попытке {attempt + 1}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
            except Exception as e:
                logger.error(f"Ошибка запроса расчета: {e}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
        return None


# Функции обработки сообщений
async def safe_send_message(
    update, text: str, reply_markup=None, parse_mode="Markdown", **kwargs
):
    try:
        parts = TextMessageHandler.split_message(text)
        for i, part in enumerate(parts):
            reply_markup = reply_markup if i == len(parts) - 1 else None
            await update.message.reply_text(
                part, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
            )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")


async def safe_edit_message(
    query, text: str, reply_markup=None, parse_mode="Markdown", **kwargs
):
    try:
        current_text = getattr(query.message, "text", "") or ""
        if current_text == text:
            return
        parts = TextMessageHandler.split_message(text)
        if len(parts) == 1:
            await query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
            )
        else:
            await query.edit_message_text(
                parts[0], reply_markup=None, parse_mode=parse_mode, **kwargs
            )
            for i, part in enumerate(parts[1:], 1):
                reply_markup = reply_markup if i == len(parts) - 1 else None
                await query.message.reply_text(
                    part, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
                )
    except Exception as e:
        logger.error(f"Ошибка редактирования сообщения: {e}")
        try:
            await query.message.reply_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
            )
        except:
            pass


# Утилитные функции
def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["📦 Мои посылки", "💰 Калькулятор"],
        ["📋 BxBox Правила", "🌍 СНГ страны"],
        ["🎫 Создать тикет", "❓ Помощь"],
        ["👤 Профиль"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_profile_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["🔑 Изменить пароль", "📍 Изменить адрес"],
        ["📋 Мои посылки", "🏠 Главное меню"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def normalize_text(text: str) -> str:
    words = re.findall(r"\w+", text.lower())
    return " ".join([morph.parse(word)[0].normal_form for word in words])


# Обработчики
@handle_errors()
@async_db_session
async def start(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    text = (
        "🌟 Добро пожаловать в Boxberry Bot!\n\n"
        "Я помогу вам:\n"
        "📦 Отслеживать посылки\n"
        "💰 Рассчитывать стоимость доставки\n"
        "❓ Получать информацию о доставке\n\n"
        "Для доступа к 'Мои посылки' и 'Профиль' зарегистрируйтесь или войдите:"
    )
    keyboard = [
        ["📦 Мои посылки", "💰 Калькулятор"],
        ["📋 BxBox Правила", "🌍 СНГ страны"],
        ["🎫 Создать тикет", "❓ Помощь"],
        ["👤 Профиль"],
        [
            InlineKeyboardButton("📝 Регистрация", callback_data="register"),
            InlineKeyboardButton("🔑 Войти", callback_data="login"),
        ],
    ]
    await safe_send_message(
        update,
        text,
        reply_markup=ReplyKeyboardMarkup(keyboard[:4], resize_keyboard=True),
    )


@handle_errors()
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ **Помощь по Boxberry Bot**\n\n"
        "Я могу ответить на ваши вопросы. Просто напишите, что вас интересует, например:\n"
        "`Cколько стоит доставка?`\n"
        "`Kак упаковать посылку?`\n\n"
        "**Или воспользуйтесь кнопками ниже:**"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "📚 Частые вопросы (FAQ)",
                url="https://boxberry.ru/faq/chastnym-klientam-voprosy-i-otvety",
            ),
            InlineKeyboardButton(
                "☎️ Служба поддержки", url="https://boxberry.ru/kontakty"
            ),
        ]
    ]
    if update.message:
        await safe_send_message(
            update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif update.callback_query:
        await safe_edit_message(
            update.callback_query, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )


@handle_errors()
async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🔐 Регистрация\n\nВведите ваше имя пользователя (минимум 5 символов):"
    if update.message:
        await safe_send_message(update, text)
    elif update.callback_query:
        await safe_edit_message(update.callback_query, text)
    return REGISTER_LOGIN


@handle_errors()
@async_db_session
async def register_login_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    username = update.message.text.strip()
    if len(username) < 5:
        await safe_send_message(
            update,
            "❌ Имя пользователя должно содержать минимум 5 символов. Попробуйте снова:",
        )
        return REGISTER_LOGIN
    existing_user = session.query(User).filter_by(username=username).first()
    if existing_user:
        await safe_send_message(
            update,
            "❌ Это имя пользователя уже занято. Попробуйте другое или войдите с помощью /login.",
        )
        return ConversationHandler.END
    context.user_data["reg_username"] = username
    await safe_send_message(update, "🔒 Введите пароль (минимум 6 символов):")
    return REGISTER_PASSWORD


@handle_errors()
@async_db_session
async def register_password_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    password = update.message.text.strip()
    if len(password) < 6:
        await safe_send_message(
            update, "❌ Пароль должен содержать минимум 6 символов. Попробуйте снова:"
        )
        return REGISTER_PASSWORD
    if password.isdigit() or password.isalpha():
        await safe_send_message(
            update,
            "⚠️ Рекомендуется использовать пароль с цифрами и буквами для большей безопасности.",
        )
    context.user_data["reg_password"] = password
    await safe_send_message(update, "👤 Введите ваше имя:")
    return REGISTER_NAME


@handle_errors()
@async_db_session
async def register_name_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    name = update.message.text.strip()
    if not name:
        await safe_send_message(
            update, "❌ Имя не может быть пустым. Попробуйте снова:"
        )
        return REGISTER_NAME
    context.user_data["reg_first"] = name
    await safe_send_message(update, "👥 Введите вашу фамилию:")
    return REGISTER_SURNAME


@handle_errors()
@async_db_session
async def register_surname_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    surname = update.message.text.strip()
    if not surname:
        await safe_send_message(
            update, "❌ Фамилия не может быть пустой. Попробуйте снова:"
        )
        return REGISTER_SURNAME
    data = context.user_data
    try:
        user = db_get_or_create_user(
            session, update.effective_user.id, update.effective_user.username
        )
        user.username = data["reg_username"]
        user.password = data["reg_password"]
        user.first_name = data["reg_first"]
        user.last_name = surname
        session.add(user)
        text = (
            f"✅ Регистрация завершена!\n\n"
            f"👤 Имя: {user.first_name} {user.last_name}\n"
            f"📧 Имя пользователя: {user.username}\n\n"
            f"Теперь вы можете пользоваться всеми функциями бота!"
        )
        await safe_send_message(update, text, reply_markup=get_main_menu_keyboard())
    except IntegrityError:
        session.rollback()
        await safe_send_message(
            update, "❌ Произошла ошибка при регистрации. Попробуйте еще раз."
        )
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🔑 Вход\n\nВведите ваше имя пользователя:"
    if update.message:
        await safe_send_message(update, text)
    elif update.callback_query:
        await safe_edit_message(update.callback_query, text)
    return LOGIN_LOGIN


@handle_errors()
@async_db_session
async def login_login_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    username = update.message.text.strip()
    if not username:
        await safe_send_message(update, "❌ Введите корректное имя пользователя:")
        return LOGIN_LOGIN
    context.user_data["login_username"] = username
    await safe_send_message(update, "🔒 Введите пароль:")
    return LOGIN_PASSWORD


@handle_errors()
@async_db_session
async def login_password_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    username = context.user_data.get("login_username")
    password_text = update.message.text.strip()
    user = (
        session.query(User).filter_by(username=username, password=password_text).first()
    )
    if not user:
        await safe_send_message(
            update,
            "❌ Неверное имя пользователя или пароль. Попробуйте снова или зарегистрируйтесь /register.",
            reply_markup=get_main_menu_keyboard(),
        )
        context.user_data.clear()
        return ConversationHandler.END
    user.telegram_id = update.effective_user.id
    user.telegram_username = update.effective_user.username
    text = f"✅ Вход выполнен!\n\n👤 Добро пожаловать, {user.first_name}!"
    await safe_send_message(update, text, reply_markup=get_main_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
@async_db_session
async def profile_cmd(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user or not user.username:
        await safe_send_message(
            update,
            "❌ Для доступа к профилю необходимо войти в аккаунт. Пожалуйста, используйте /register или /login.",
            reply_markup=get_main_menu_keyboard(),
        )
        return
    parcels_count = session.query(Parcel).filter_by(user_id=user.id).count()
    text = (
        f"👤 **Ваш профиль**\n\n"
        f"**Имя:** {user.first_name or 'не указано'} {user.last_name or ''}\n"
        f"**Имя пользователя:** `{user.username}`\n"
        f"**Посылок отслеживается:** {parcels_count}\n"
        f"**Дата регистрации:** {user.created_at.strftime('%d.%m.%Y') if hasattr(user, 'created_at') and user.created_at else 'не указана'}"
    )
    await safe_send_message(update, text, reply_markup=get_profile_keyboard())


@handle_errors()
async def change_password_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_message(update, "Введите старый пароль:")
    return CHANGE_OLD_PASSWORD


@handle_errors()
@async_db_session
async def change_old_password_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    old_password = update.message.text.strip()
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user or user.password != old_password:
        await safe_send_message(update, "❌ Неверный старый пароль. Попробуйте снова.")
        return CHANGE_OLD_PASSWORD
    await safe_send_message(update, "Введите новый пароль (минимум 6 символов):")
    return CHANGE_NEW_PASSWORD


@handle_errors()
@async_db_session
async def change_new_password_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    new_password = update.message.text.strip()
    if len(new_password) < 6:
        await safe_send_message(
            update,
            "❌ Новый пароль должен содержать минимум 6 символов. Попробуйте снова:",
        )
        return CHANGE_NEW_PASSWORD
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user:
        await safe_send_message(update, "❌ Пользователь не найден.")
        return ConversationHandler.END
    user.password = new_password
    await safe_send_message(
        update, "✅ Пароль успешно изменен!", reply_markup=get_profile_keyboard()
    )
    return ConversationHandler.END


@handle_errors()
@async_db_session
async def my_parcels_cmd(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user or not user.username:
        keyboard = [
            [
                InlineKeyboardButton("📝 Регистрация", callback_data="register"),
                InlineKeyboardButton("🔑 Войти", callback_data="login"),
            ],
        ]
        await safe_send_message(
            update,
            "❌ Для доступа к посылкам необходимо войти в аккаунт.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    for msg_key in ["my_parcels_message_id", "last_tracking_message_id"]:
        if msg_key in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data[msg_key],
                )
                del context.user_data[msg_key]
            except:
                pass
    text, reply_markup = await get_my_parcels_content(session, user)
    message = await update.message.reply_text(
        text, reply_markup=reply_markup, parse_mode="Markdown"
    )
    context.user_data["my_parcels_message_id"] = message.message_id


async def get_my_parcels_content(session, user) -> Tuple[str, InlineKeyboardMarkup]:
    parcels = session.query(Parcel).filter_by(user_id=user.id).all()
    if not parcels:
        text = "У вас пока нет отслеживаемых посылок.\nИспользуйте кнопку ниже, чтобы добавить первую."
        keyboard = [
            [
                InlineKeyboardButton(
                    "➕ Добавить трек-номер", callback_data="add_new_tracking"
                )
            ]
        ]
    else:
        text = f"📦 Ваши посылки: {len(parcels)}\n\n"
        keyboard = []
        for i, parcel in enumerate(parcels, 1):
            status = parcel.last_status or "Статус не определен"
            text += f"**{i}.** `{parcel.tracking_number}`\n"
            text += f"📊 _{status}_\n\n"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"🔍 {parcel.tracking_number}",
                        callback_data=f"track_{parcel.tracking_number}",
                    )
                ]
            )
        keyboard.append(
            [
                InlineKeyboardButton("🗑️ Удалить", callback_data="start_delete"),
                InlineKeyboardButton(
                    "➕ Добавить новый", callback_data="add_new_tracking"
                ),
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🔄 Обновить статусы", callback_data="refresh_parcels"
                )
            ]
        )
    return text, InlineKeyboardMarkup(keyboard)


@handle_errors()
@async_db_session
async def add_tracking_received(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    code = update.message.text.strip().upper()
    if not TRACKING_PATTERN.match(code):
        await safe_send_message(
            update, "❌ Некорректный формат трек-номера. Попробуйте снова:"
        )
        return ADD_TRACKING
    user = db_get_or_create_user(session, update.effective_user.id)
    if not user.username:
        await safe_send_message(
            update,
            "❌ Для добавления посылки необходимо войти в аккаунт. Используйте /login или /register.",
            reply_markup=get_main_menu_keyboard(),
        )
        return ConversationHandler.END
    exists = (
        session.query(Parcel).filter_by(user_id=user.id, tracking_number=code).first()
    )
    if exists:
        await safe_send_message(update, "ℹ️ Этот трек-номер уже есть в вашем списке.")
    else:
        parcel = Parcel(user_id=user.id, tracking_number=code, last_status="Добавлено")
        session.add(parcel)
        await safe_send_message(update, "✅ Трек-номер успешно добавлен!")
    for msg_key in ["add_prompt_id"]:
        if msg_key in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data[msg_key],
                )
                del context.user_data[msg_key]
            except:
                pass
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=update.message.message_id
        )
    except:
        pass
    await my_parcels_cmd(session, update, context)
    return ConversationHandler.END


@async_db_session
async def start_delete_menu(
    session, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user = session.query(User).filter_by(telegram_id=query.from_user.id).first()
    if not user:
        await query.answer("❌ Пользователь не найден.", show_alert=True)
        return
    parcels = session.query(Parcel).filter_by(user_id=user.id).all()
    if not parcels:
        await query.answer("Нет посылок для удаления.", show_alert=True)
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"❌ {parcel.tracking_number}",
                callback_data=f"del_{parcel.tracking_number}",
            )
        ]
        for parcel in parcels
    ]
    keyboard.append(
        [InlineKeyboardButton("🔥🔥🔥 Удалить ВСЕ", callback_data="del_all")]
    )
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_parcels")])
    await safe_edit_message(
        query,
        "Выберите трек-номер для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@async_db_session
async def handle_delete(
    session,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tracking: Optional[str] = None,
    all: bool = False,
):
    query = update.callback_query
    user = session.query(User).filter_by(telegram_id=query.from_user.id).first()
    if not user:
        await query.answer("❌ Пользователь не найден.", show_alert=True)
        return
    if all:
        deleted_count = session.query(Parcel).filter_by(user_id=user.id).delete()
        await query.answer(f"🗑️ Удалено {deleted_count} посылок!", show_alert=True)
    elif tracking:
        parcel = (
            session.query(Parcel)
            .filter_by(user_id=user.id, tracking_number=tracking)
            .first()
        )
        if parcel:
            session.delete(parcel)
            await query.answer("🗑️ Посылка удалена!", show_alert=True)
        else:
            await query.answer("ℹ️ Посылка не найдена.", show_alert=True)
    text, reply_markup = await get_my_parcels_content(session, user)
    await safe_edit_message(query, text, reply_markup=reply_markup)


@handle_errors()
async def bxbox_rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🇺🇸 США", callback_data="rule_USA"),
            InlineKeyboardButton("🇨🇳 Китай", callback_data="rule_China"),
        ],
        [
            InlineKeyboardButton("🇩🇪 Германия", callback_data="rule_Germany"),
            InlineKeyboardButton("🇪🇸 Испания", callback_data="rule_Spain"),
        ],
        [InlineKeyboardButton("🇮🇳 Индия", callback_data="rule_India")],
    ]
    await safe_send_message(
        update,
        "Выберите страну для просмотра ограничений:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@handle_errors()
async def bxbox_rules_country_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    country_code = query.data.split("_", 1)[1]
    rules = data_manager.restrictions.get(country_code)
    if not rules:
        await safe_edit_message(
            query, "❌ Страна не найдена. Пожалуйста, выберите из списка."
        )
        return
    text = f"📋 **Правила для отправлений: {country_code}**\n\n"
    for category, details in rules.get("categories", {}).items():
        text += f"**{category}**\n"
        if details.get("standard"):
            text += (
                "🚚 **Стандартная доставка:**\n"
                + "\n".join(details["standard"])
                + "\n\n"
            )
        if details.get("alternative"):
            text += (
                "✈️ **Альтернативная доставка:**\n"
                + "\n".join(details["alternative"])
                + "\n\n"
            )
        if details.get("restricted"):
            text += "⚠️ **Ограничения:**\n" + "\n".join(details["restricted"]) + "\n\n"
        if details.get("prohibited"):
            text += (
                "🚫 **Запрещено к пересылке:**\n"
                + "\n".join(details["prohibited"])
                + "\n\n"
            )
        if details.get("details_link"):
            text += f"[🔗 Подробнее]({details['details_link']})\n\n"
    text += f"📏 **Максимальные параметры:**\n"
    text += f"• Вес: *{rules.get('max_weight', 'Нет данных')}*\n"
    text += f"• Размеры: *{rules.get('max_dimensions', 'Нет данных')}*"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_rules")]]
    await safe_edit_message(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


@handle_errors()
async def back_to_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("🇺🇸 США", callback_data="rule_USA"),
            InlineKeyboardButton("🇨🇳 Китай", callback_data="rule_China"),
        ],
        [
            InlineKeyboardButton("🇩🇪 Германия", callback_data="rule_Germany"),
            InlineKeyboardButton("🇪🇸 Испания", callback_data="rule_Spain"),
        ],
        [InlineKeyboardButton("🇮🇳 Индия", callback_data="rule_India")],
    ]
    await safe_edit_message(
        query,
        "Выберите страну для просмотра ограничений:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@handle_errors()
async def create_ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🎫 Для создания обращения (тикетов) или оформления заявки на выкуп, пожалуйста, перейдите на наш сайт."
    keyboard = [
        [
            InlineKeyboardButton(
                "📝 Открыть форму на сайте",
                url="https://bxbox.bxb.delivery/ru/new-ticket/2",
            )
        ]
    ]
    await safe_send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))


@handle_errors()
async def calculator_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🇺🇸 США", callback_data="calc_storage_usa")],
        [InlineKeyboardButton("🇨🇳 Китай", callback_data="calc_storage_china")],
        [InlineKeyboardButton("🇩🇪 Германия", callback_data="calc_storage_germany")],
        [InlineKeyboardButton("🇪🇸 Испания", callback_data="calc_storage_spain")],
        [InlineKeyboardButton("🇮🇳 Индия", callback_data="calc_storage_india")],
        [InlineKeyboardButton("❌ Отмена", callback_data="calc_cancel")],
    ]
    text = "💰 **Калькулятор доставки**\n\nВыберите страну склада:"
    if update.callback_query:
        await safe_edit_message(
            update.callback_query, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await safe_send_message(
            update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return CALC_STORAGE


@handle_errors()
async def calculator_storage_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    storage_map = {
        "calc_storage_usa": {"id": "1", "name": "🇺🇸 США"},
        "calc_storage_china": {"id": "2", "name": "🇨🇳 Китай"},
        "calc_storage_germany": {"id": "3", "name": "🇩🇪 Германия"},
        "calc_storage_spain": {"id": "4", "name": "🇪🇸 Испания"},
        "calc_storage_india": {"id": "5", "name": "🇮🇳 Индия"},
    }
    storage_info = storage_map.get(query.data)
    if not storage_info:
        await safe_edit_message(query, "❌ Ошибка выбора склада. Начните заново.")
        return ConversationHandler.END
    context.user_data["calc_storage_id"] = storage_info["id"]
    context.user_data["calc_storage_name"] = storage_info["name"]
    await safe_edit_message(
        query,
        f"📦 Склад: {storage_info['name']}\n\n🏙️ Введите название города получателя (например: Москва):",
    )
    return CALC_CITY_SEARCH


@handle_errors()
async def calculator_city_search_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    city_name = update.message.text.strip()
    if len(city_name) < 2:
        await safe_send_message(
            update,
            "❌ Название города должно содержать минимум 2 символа. Попробуйте снова:",
        )
        return CALC_CITY_SEARCH
    search_msg = await update.message.reply_text("🔍 Поиск городов...")
    cities = await BoxberryAPI.get_cities(city_name)
    await search_msg.delete()
    if not cities:
        await safe_send_message(
            update, "❌ Города не найдены.\n\nПопробуйте другое название:"
        )
        return CALC_CITY_SEARCH
    keyboard = [
        [InlineKeyboardButton(city["text"], callback_data=f"calc_city_{city['id']}")]
        for city in cities[:15]
    ]
    keyboard.append(
        [InlineKeyboardButton("🔍 Новый поиск", callback_data="calc_city_new_search")]
    )
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="calc_cancel")])
    storage_name = context.user_data.get("calc_storage_name", "")
    await safe_send_message(
        update,
        f"📦 Склад: {storage_name}\n🏙️ Найденные города для '{city_name}':\n\nВыберите город:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CALC_CITY_SELECT


@handle_errors()
async def calculator_city_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city_id = query.data.replace("calc_city_", "")
    inline_keyboard = (
        query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
    )
    selected_city_name = "Выбранный город"
    for row in inline_keyboard:
        for button in row:
            if button.callback_data == query.data:
                selected_city_name = button.text
                break
    context.user_data["calc_city_id"] = city_id
    context.user_data["calc_city_name"] = selected_city_name
    storage_name = context.user_data.get("calc_storage_name", "")
    keyboard = [
        [
            InlineKeyboardButton(
                "🔙 Назад к стране", callback_data="calc_back_to_country"
            )
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="calc_cancel")],
    ]
    await safe_edit_message(
        query,
        f"📦 Склад: {storage_name}\n🏙️ Город: {selected_city_name}\n\n⚖️ Введите вес посылки в килограммах (например: 2.5):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CALC_WEIGHT


@handle_errors()
async def calculator_weight_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    try:
        weight_text = update.message.text.strip().replace(",", ".")
        weight = float(weight_text)
        if weight <= 0 or weight > 31.5:
            await safe_send_message(
                update,
                "❌ Вес должен быть от 0.01 до 31.5 кг.\n\nВведите число, например: 2.5",
            )
            return CALC_WEIGHT
    except ValueError:
        await safe_send_message(
            update, "❌ Неверный формат веса.\n\nВведите число, например: 2.5"
        )
        return CALC_WEIGHT
    context.user_data["calc_weight"] = weight
    storage_name = context.user_data.get("calc_storage_name", "")
    city_name = context.user_data.get("calc_city_name", "")
    keyboard = [
        [
            InlineKeyboardButton(
                "🚚 Курьерская доставка", callback_data="calc_delivery_courier"
            )
        ],
        [
            InlineKeyboardButton(
                "📍 Доставка в пункт выдачи", callback_data="calc_delivery_pickup"
            )
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="calc_cancel")],
    ]
    await safe_send_message(
        update,
        f"📦 Склад: {storage_name}\n🏙️ Город: {city_name}\n⚖️ Вес: {weight} кг\n\nВыберите способ доставки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CALC_DELIVERY


@handle_errors()
async def calculator_delivery_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    delivery_type = query.data.replace("calc_delivery_", "")
    courier = delivery_type == "courier"
    storage_id = context.user_data.get("calc_storage_id")
    city_id = context.user_data.get("calc_city_id")
    weight = context.user_data.get("calc_weight")
    await safe_edit_message(query, "⏳ Расчет стоимости...")
    cost = await BoxberryAPI.calculate_delivery_cost(
        storage_id, city_id, weight, courier
    )
    storage_name = context.user_data.get("calc_storage_name", "")
    city_name = context.user_data.get("calc_city_name", "")
    delivery_text = "Курьерская доставка" if courier else "Доставка в пункт выдачи"
    result_text = (
        f"💰 **Результат расчета**\n\n"
        f"📦 Склад: {storage_name}\n"
        f"🏙️ Город: {city_name}\n"
        f"⚖️ Вес: {weight} кг\n"
        f"🚚 Способ доставки: {delivery_text}\n\n"
    )
    if cost:
        result_text += f"💵 **Стоимость: {cost} ₽**"
        keyboard = [
            [InlineKeyboardButton("🔄 Новый расчет", callback_data="calc_new")],
            [InlineKeyboardButton("📋 Главное меню", callback_data="main_menu")],
        ]
    else:
        result_text += (
            f"❌ **Ошибка расчета**\n\n"
            f"Не удалось рассчитать стоимость доставки.\n"
            f"Попробуйте позже или обратитесь к администратору."
        )
        keyboard = [
            [InlineKeyboardButton("🔄 Попробовать снова", callback_data="calc_new")]
        ]
    await safe_edit_message(
        query, result_text, reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def calculator_city_new_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    storage_name = context.user_data.get("calc_storage_name", "")
    await safe_edit_message(
        query,
        f"📦 Склад: {storage_name}\n\n🏙️ Введите название города получателя (например: Москва):",
    )
    return CALC_CITY_SEARCH


@handle_errors()
async def calculator_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await safe_edit_message(update.callback_query, "❌ Расчет отменен.")
    else:
        await safe_send_message(
            update, "❌ Расчет отменен.", reply_markup=get_main_menu_keyboard()
        )
    return ConversationHandler.END


@handle_errors()
async def calc_back_to_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keys_to_keep = []
    user_data_copy = {k: v for k, v in context.user_data.items() if k in keys_to_keep}
    context.user_data.clear()
    context.user_data.update(user_data_copy)
    await calculator_start(update, context)
    return CALC_STORAGE


@handle_errors()
async def handle_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # Очистка активных разговоров
    context.user_data.clear()

    if text == "📦 Мои посылки":
        await my_parcels_cmd(update, context)
    elif text == "💰 Калькулятор":
        await calculator_start(update, context)
    elif text == "📋 BxBox Правила":
        await bxbox_rules_cmd(update, context)
    elif text == "🎫 Создать тикет":
        await create_ticket_cmd(update, context)
    elif text == "❓ Помощь":
        await help_cmd(update, context)
    elif text == "👤 Профиль":
        await profile_cmd(update, context)
    elif text == "🏠 Главное меню":
        await safe_send_message(
            update, "Главное меню:", reply_markup=get_main_menu_keyboard()
        )
    elif text == "🔑 Изменить пароль":
        await change_password_start(update, context)
    elif text == "🌍 СНГ страны":
        text_response = data_manager.keywords.get("куда отправить", {}).get(
            "text", "Информация о доставке в страны СНГ доступна на нашем сайте."
        )
        link = data_manager.keywords.get("международная доставка", {}).get(
            "link", "https://boxberry.ru"
        )
        await safe_send_message(
            update,
            f"🌍 Доставка в страны СНГ\n\n{text_response}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Подробнее о международной доставке", url=link)]]
            ),
        )
    elif text == "📍 Изменить адрес":
        await safe_send_message(
            update,
            "ℹ️ Изменить адрес доставки можно через личный кабинет или обратившись в службу поддержки, если посылка еще не передана курьеру.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔗 Подробнее о переадресации",
                            url="https://boxberry.ru/faq/chastnym-klientam-voprosy-i-otvety/kak-pereadresovat-posylku-na-drugoi-punkt-vydachi-boxberry",
                        )
                    ]
                ]
            ),
        )
    else:
        await keyword_handler(update, context)


@handle_errors()
@async_db_session
async def keyword_handler(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    menu_options = [
        "📦 Мои посылки",
        "💰 Калькулятор",
        "📋 BxBox Правила",
        "🌍 СНГ страны",
        "🎫 Создать тикет",
        "❓ Помощь",
        "👤 Профиль",
        "🏠 Главное меню",
        "🔑 Изменить пароль",
        "📍 Изменить адрес",
    ]
    if user_input in menu_options:
        await safe_send_message(
            update,
            "🤔 Пожалуйста, выберите действие из меню.",
            reply_markup=get_main_menu_keyboard(),
        )
        return
    if TRACKING_PATTERN.match(user_input.upper()):
        tracking_number = user_input.upper()
        user = (
            session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        )
        additional_text = ""
        if user and user.username:
            exists = (
                session.query(Parcel)
                .filter_by(user_id=user.id, tracking_number=tracking_number)
                .first()
            )
            if not exists:
                parcel = Parcel(
                    user_id=user.id,
                    tracking_number=tracking_number,
                    last_status="Добавлено",
                )
                session.add(parcel)
                additional_text = "\n\n✅ Трек-номер сохранен в 'Мои посылки'!"
            else:
                additional_text = "\n\nℹ️ Этот трек-номер уже есть в вашем списке."
        else:
            additional_text = (
                "\n\n💡 Войдите или зарегистрируйтесь, чтобы сохранять трек-номера."
            )
        await send_tracking_info(update, context, tracking_number, additional_text)
        return
    normalized_input = normalize_text(user_input)
    choices = {
        normalize_text(f"{key} {' '.join(data.get('keywords', []))}"): key
        for key, data in data_manager.keywords.items()
    }
    results = process.extract(
        normalized_input, choices.keys(), limit=3, scorer=fuzz.token_set_ratio
    )
    if results:
        best_match, best_score = results[0]
        if best_score > 70:
            key = choices[best_match]
            meta = data_manager.keywords[key]
            text = meta.get("text", "Информация не найдена.")
            keyboard = (
                [[InlineKeyboardButton("🔗 Подробнее на сайте", url=meta.get("link"))]]
                if meta.get("link")
                else None
            )
            await safe_send_message(
                update,
                text,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
            )
        else:
            keyboard = [
                [
                    InlineKeyboardButton(
                        f"❓ {choices[match].capitalize()}",
                        callback_data=f"kw_{choices[match]}",
                    )
                ]
                for match, score in results
                if score > 45
            ]
            if keyboard:
                await safe_send_message(
                    update,
                    "🤔 Я не совсем уверен, что вы имеете в виду. Возможно, вас интересует:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await safe_send_message(
                    update,
                    "К сожалению, я не смог распознать ваш запрос. Пожалуйста, попробуйте переформулировать его или воспользуйтесь главным меню.",
                    reply_markup=get_main_menu_keyboard(),
                )


@handle_errors()
async def keyword_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split("_", 1)[1]
    if key in data_manager.keywords:
        meta = data_manager.keywords[key]
        text = meta.get("text", "Информация не найдена.")
        keyboard = (
            [[InlineKeyboardButton("🔗 Подробнее на сайте", url=meta.get("link"))]]
            if meta.get("link")
            else None
        )
        await safe_edit_message(
            query,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        )


@handle_errors()
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Очистка состояния разговора для всех кнопок, кроме register и login
    if data not in ["register", "login"]:
        conv_keys = [
            k
            for k in context.user_data.keys()
            if k.startswith(("reg_", "login_", "calc_"))
        ]
        for key in conv_keys:
            context.user_data.pop(key, None)

    if data == "register":
        return await register_cmd(update, context)
    elif data == "login":
        return await login_cmd(update, context)
    elif data == "help_guest":
        await help_cmd(update, context)
    elif data == "add_new_tracking":
        prompt_msg = await query.message.reply_text(
            "Введите трек-номер для добавления:"
        )
        context.user_data["add_prompt_id"] = prompt_msg.message_id
        return ADD_TRACKING
    elif data.startswith("track_"):
        tracking_id = data.split("_", 1)[1]
        await send_tracking_info(update, context, tracking_id)
    elif data == "start_delete":
        await start_delete_menu(update, context)
    elif data == "back_to_parcels":
        session = SessionLocal()
        try:
            user = db_get_or_create_user(session, query.from_user.id)
            text, markup = await get_my_parcels_content(session, user)
            await safe_edit_message(query, text, reply_markup=markup)
            session.commit()
        finally:
            session.close()
    elif data.startswith("del_"):
        if data == "del_all":
            await handle_delete(update, context, all=True)
        else:
            tracking_id = data.split("_", 1)[1]
            await handle_delete(update, context, tracking=tracking_id)
    elif data == "main_menu":
        await safe_edit_message(query, "Главное меню:")
        await query.message.reply_text(
            "Выберите действие:", reply_markup=get_main_menu_keyboard()
        )
    elif data == "refresh_parcels":
        session = SessionLocal()
        try:
            user = db_get_or_create_user(session, query.from_user.id)
            text, markup = await get_my_parcels_content(session, user)
            await safe_edit_message(query, text, reply_markup=markup)
            session.commit()
        finally:
            session.close()
    elif data.startswith("calc_"):
        if data.startswith("calc_storage_"):
            return await calculator_storage_selected(update, context)
        elif data.startswith("calc_city_"):
            return await calculator_city_selected(update, context)
        elif data == "calc_city_new_search":
            return await calculator_city_new_search(update, context)
        elif data == "calc_back_to_country":
            return await calc_back_to_country(update, context)
        elif data == "calc_new":
            return await calculator_start(update, context)
        elif data == "calc_cancel":
            return await calculator_cancel(update, context)
        elif data.startswith("calc_delivery_"):
            return await calculator_delivery_selected(update, context)


async def send_tracking_info(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tracking_number: str,
    additional_text: str = "",
):
    tracking_url = f"{config.BASE_URL}/tracking-page?id={tracking_number}"
    keyboard = [[InlineKeyboardButton("🔍 Отследить на сайте", url=tracking_url)]]
    message_text = f"📦 Трек-номер: `{tracking_number}` {additional_text}"
    if "last_tracking_message_id" in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data["last_tracking_message_id"],
            )
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение: {e}")
    try:
        msg = await (update.message or update.callback_query.message).reply_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        context.user_data["last_tracking_message_id"] = msg.message_id
    except Exception as e:
        logger.error(f"Не удалось отправить информацию о треке: {e}")


def db_get_or_create_user(
    session, telegram_id: int, username: Optional[str] = None
) -> User:
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    if user:
        if username and user.username != username:
            user.username = username
        return user
    user = User(telegram_id=telegram_id, username=username)
    session.add(user)
    return user


@handle_errors()
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_message(
        update, "Действие отменено.", reply_markup=get_main_menu_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cleanup():
    await HTTPManager.close()


def main():
    init_db()
    if not config.TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не найден в переменных окружения")

    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()

    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("register", register_cmd),
            CallbackQueryHandler(button_handler, pattern="^register$"),
        ],
        states={
            REGISTER_LOGIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_login_received)
            ],
            REGISTER_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, register_password_received
                )
            ],
            REGISTER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_name_received)
            ],
            REGISTER_SURNAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, register_surname_received
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    login_conv = ConversationHandler(
        entry_points=[
            CommandHandler("login", login_cmd),
            CallbackQueryHandler(button_handler, pattern="^login$"),
        ],
        states={
            LOGIN_LOGIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_login_received)
            ],
            LOGIN_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    add_tracking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(button_handler, pattern="^add_new_tracking$")
        ],
        states={
            ADD_TRACKING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_tracking_received)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(
                filters.Regex(
                    r"^(📦 Мои посылки|💰 Калькулятор|📋 BxBox Правила|🌍 СНГ страны|🎫 Создать тикет|❓ Помощь|👤 Профиль|🏠 Главное меню)$"
                ),
                cancel,
            ),
        ],
        per_user=True,
        per_chat=True,
    )

    change_password_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r"^🔑 Изменить пароль$"), change_password_start
            )
        ],
        states={
            CHANGE_OLD_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, change_old_password_received
                )
            ],
            CHANGE_NEW_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, change_new_password_received
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    calc_conv = ConversationHandler(
        entry_points=[
            CommandHandler("calculator", calculator_start),
            MessageHandler(filters.Regex(r"^💰 Калькулятор$"), calculator_start),
            CallbackQueryHandler(calculator_start, pattern="^calc_new$"),
        ],
        states={
            CALC_STORAGE: [
                CallbackQueryHandler(
                    calculator_storage_selected, pattern="^calc_storage_"
                )
            ],
            CALC_CITY_SEARCH: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, calculator_city_search_received
                )
            ],
            CALC_CITY_SELECT: [
                CallbackQueryHandler(calculator_city_selected, pattern="^calc_city_"),
                CallbackQueryHandler(
                    calculator_city_new_search, pattern="^calc_city_new_search$"
                ),
                CallbackQueryHandler(
                    calc_back_to_country, pattern="^calc_back_to_country$"
                ),
            ],
            CALC_WEIGHT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, calculator_weight_received
                )
            ],
            CALC_DELIVERY: [
                CallbackQueryHandler(
                    calculator_delivery_selected, pattern="^calc_delivery_"
                )
            ],
        },
        fallbacks=[
            CallbackQueryHandler(calculator_cancel, pattern="^calc_cancel$"),
            CommandHandler("cancel", calculator_cancel),
        ],
        per_user=True,
        per_chat=True,
    )

    # Сначала добавляем ConversationHandlers
    app.add_handler(reg_conv)
    app.add_handler(login_conv)
    app.add_handler(add_tracking_conv)
    app.add_handler(change_password_conv)
    app.add_handler(calc_conv)

    # Затем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("myparcels", my_parcels_cmd))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(keyword_callback_handler, pattern=r"^kw_"))
    app.add_handler(
        CallbackQueryHandler(bxbox_rules_country_selected, pattern="^rule_")
    )
    app.add_handler(CallbackQueryHandler(back_to_rules, pattern="^back_to_rules$"))
    app.add_handler(CallbackQueryHandler(button_handler))

    # В конце - общий обработчик текста
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_selection)
    )

    logger.info("Boxberry Bot успешно запущен")
    print("Boxberry Bot запущен.")

    try:
        app.run_polling()
    finally:
        asyncio.run(cleanup())


if __name__ == "__main__":
    main()
