import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Optional, Dict, List, Tuple, Any
import aiohttp
import pymorphy2
import redis.asyncio as redis
import xml.etree.ElementTree as ET
from aiolimiter import AsyncLimiter
from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    BigInteger,
    String,
    Text,
    DateTime,
    ForeignKey,
    func,
    update as sa_update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship, selectinload
from sqlalchemy.sql import select, and_
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
from thefuzz import process
from werkzeug.security import generate_password_hash, check_password_hash

# Load environment variables
load_dotenv()
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "name": "%(name)s", "level": "%(levelname)s", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)
# Database setup
DATABASE_URL = os.getenv("DATABASE_URL")
Base = declarative_base()
# Conversation states
REGISTER_LOGIN, REGISTER_PASSWORD, REGISTER_NAME, REGISTER_SURNAME = range(4)
LOGIN_LOGIN, LOGIN_PASSWORD = range(4, 6)
ADD_TRACKING = 6
CHANGE_OLD_PASSWORD, CHANGE_NEW_PASSWORD = range(7, 9)
CALC_STORAGE, CALC_CITY_SEARCH, CALC_CITY_SELECT, CALC_WEIGHT, CALC_DELIVERY = range(
    9, 14
)


# Configuration class
@dataclass
class Config:
    TELEGRAM_TOKEN: str
    BASE_URL: str = "https://boxberry.ru"
    MAX_RETRIES: int = 3
    REQUEST_TIMEOUT: int = 30
    MAX_MESSAGE_LENGTH: int = 4096
    CACHE_TTL: int = 60

    @classmethod
    def from_env(cls):
        return cls(TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN"))


config = Config.from_env()
TRACKING_PATTERN = re.compile(r"^[A-Z0-9\-]{8,}$")


# Database models
class User(Base):
    __tablename__ = "users"
    telegram_id = Column(BigInteger, primary_key=True, index=True)
    telegram_username = Column(String, nullable=True)
    username = Column(String, unique=True, index=True, nullable=True)
    password = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    parcels = relationship("Parcel", back_populates="user")


class Parcel(Base):
    __tablename__ = "parcels"
    id = Column(BigInteger, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"))
    tracking_number = Column(String, nullable=True)
    nickname = Column(String, nullable=True)
    last_status = Column(String, nullable=True)
    user = relationship("User", back_populates="parcels")


async def init_db():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# Utility classes and functions
_morph = None


def get_morph():
    global _morph
    if _morph is None:
        _morph = pymorphy2.MorphAnalyzer()
    return _morph


def clean_username(text: str) -> str:
    text = re.sub(
        r"[\s\u2000-\u200f\u2028-\u202f\u205f-\u206f\u3000\ufeff\u3164\u00a0]",
        "",
        text.strip(),
    ).lower()
    parsed = get_morph().parse(text)
    normal = parsed[0].normal_form if parsed else text
    if not re.match(r"^[a-z0-9_]+$", normal):
        raise ValueError("Invalid username format")
    return normal


def async_db_session():
    engine = create_async_engine(os.getenv("DATABASE_URL"), echo=False)
    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # DEBUG: Argüman sayısını kontrol et
            print(
                f"DEBUG {func.__name__}: args count = {len(args)}, args = {[type(arg).__name__ for arg in args]}"
            )

            async with AsyncSessionLocal() as session:
                try:
                    result = await func(session, *args, **kwargs)
                    await session.commit()
                    return result
                except Exception as e:
                    await session.rollback()
                    logger.error(f"Database error in {func.__name__}: {e}")
                    raise
                finally:
                    await session.close()

        return wrapper

    return decorator


def handle_errors(send_error_message: bool = True):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            limiter = AsyncLimiter(1, 1)
            async with limiter:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
                    if send_error_message:
                        # Find the Update object in args
                        update = None
                        for arg in args:
                            if hasattr(
                                arg, "effective_user"
                            ):  # This is likely an Update object
                                update = arg
                                break

                        if update:
                            try:
                                await safe_send_message(
                                    update,
                                    "❌ An error occurred. Please try again later.",
                                )
                            except:
                                pass
                    return None

        return wrapper

    return decorator


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


class CacheManager:
    _redis = None

    @classmethod
    async def init(cls):
        cls._redis = redis.Redis(host="redis", port=6379, db=0, decode_responses=True)

    @classmethod
    async def get(cls, key: str) -> Optional[Any]:
        if not cls._redis:
            return None
        data = await cls._redis.get(key)
        if data:
            ttl = await cls._redis.ttl(key)
            if ttl > 0:
                return json.loads(data)
        return None

    @classmethod
    async def set(cls, key: str, value: Any):
        if not cls._redis:
            return
        await cls._redis.setex(key, config.CACHE_TTL, json.dumps(value))

    @classmethod
    async def close(cls):
        if cls._redis:
            await cls._redis.aclose()


class DataManager:
    _instance = None
    _keywords = None
    _restrictions = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def keywords(self) -> Dict:
        if self._keywords is None:
            try:
                with open("keywords_mapping.json", "r", encoding="utf-8") as f:
                    self._keywords = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load keywords: {e}")
                self._keywords = {}
        return self._keywords

    @property
    def restrictions(self) -> Dict:
        if self._restrictions is None:
            try:
                with open("restrictions.json", "r", encoding="utf-8") as f:
                    self._restrictions = json.load(f)["countries"]
            except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
                logger.error(f"Failed to load restrictions: {e}")
                self._restrictions = {}
        return self._restrictions


data_manager = DataManager()


class BoxberryAPI:
    @staticmethod
    async def get_cities(city_name: str) -> List[Dict[str, str]]:
        if len(city_name) < 2:
            return []

        cache_key = f"cities_{city_name.lower()}"
        cached = await CacheManager.get(cache_key)
        if cached:
            return cached

        for attempt in range(config.MAX_RETRIES):
            try:
                async with HTTPManager.get_session() as session:
                    url = "https://lk.boxberry.ru/int-import-api/get-cities-list"
                    params = {"country_code": "RU", "q": city_name}

                    async with session.get(url, params=params) as response:
                        if response.status == 200:
                            text = await response.text()
                            root = ET.fromstring(text)
                            cities = [
                                {
                                    "code": item.find("id").text,
                                    "name": item.find("text").text,
                                }
                                for item in root.findall("item")
                                if item.find("id") is not None
                                and item.find("text") is not None
                            ]
                            await CacheManager.set(cache_key, cities)
                            return cities
                        elif attempt < config.MAX_RETRIES - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        else:
                            return []
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.error(f"Error fetching cities on attempt {attempt + 1}: {e}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
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
                                logger.error("API returned empty response")
                                return None
                            try:
                                root = ET.fromstring(data)
                                error_elem = root.find("error")
                                if error_elem is not None and error_elem.text == "true":
                                    error_msg = root.find("errorMessage")
                                    error_text = (
                                        error_msg.text
                                        if error_msg is not None
                                        else "Unknown error"
                                    )
                                    logger.error(f"Calculation error: {error_text}")
                                    return None
                                cost_elem = root.find("cost")
                                return (
                                    f"Cost: {cost_elem.text} ₽"
                                    if cost_elem is not None
                                    else None
                                )
                            except ET.ParseError as xml_err:
                                logger.error(f"XML parsing error: {xml_err}")
                                return None
                        elif attempt < config.MAX_RETRIES - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        return None
            except asyncio.TimeoutError:
                logger.error(f"Timeout on attempt {attempt + 1}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
            except Exception as e:
                logger.error(f"Calculation request error: {e}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
                    continue
        return "Calculation error"


# Telegram bot handlers
async def safe_send_message(
    update: Update, text: str, reply_markup=None, parse_mode=None
):
    try:
        parts = TextMessageHandler.split_message(text)
        if update.message:
            msg = await update.message.reply_text(
                parts[0], reply_markup=reply_markup, parse_mode=parse_mode
            )
            for part in parts[1:]:
                await msg.reply_text(part, parse_mode=parse_mode)
        elif update.callback_query:
            msg = await update.callback_query.message.reply_text(
                parts[0], reply_markup=reply_markup, parse_mode=parse_mode
            )
            for part in parts[1:]:
                await msg.reply_text(part, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"Error sending message: {e}")


async def safe_edit_message(query, text: str, reply_markup=None, parse_mode=None):
    try:
        if reply_markup is None:
            reply_markup = InlineKeyboardMarkup([])
        parts = TextMessageHandler.split_message(text)
        if len(parts) == 1:
            await query.edit_message_text(
                parts[0], reply_markup=reply_markup, parse_mode=parse_mode
            )
        else:
            await query.message.delete()
            await safe_send_message(
                query, text, reply_markup=reply_markup, parse_mode=parse_mode
            )
    except Exception as e:
        logger.error(f"Error editing message: {e}")
        try:
            await query.message.delete()
            await safe_send_message(
                query, text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except:
            pass


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📦 Мои посылки", "💰 Калькулятор"],
            ["📋 BxBox Правила", "🌍 Россия → СНГ , Международные → Россия"],
            ["🎫 Создать тикет", "❓ Помощь"],
            ["👤 Профиль"],
        ],
        resize_keyboard=True,
    )


def get_profile_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔑 Изменить пароль", "📍 Изменить адрес"],
            ["📋 Мои посылки", "🏠 Главное меню"],
        ],
        resize_keyboard=True,
    )


async def get_my_parcels_content(
    session: AsyncSession, user: Optional[User]
) -> Tuple[str, InlineKeyboardMarkup]:
    if not user or not user.username:
        text = (
            "📦 **Мои посылки**\n\n"
            "Для сохранения и управления трек-номерами войдите в аккаунт или зарегистрируйтесь.\n\n"
            "💡 Без регистрации вы можете:\n"
            "• Отслеживать любой трек-номер\n"
            "• Пользоваться калькулятором\n"
            "• Получать информацию о доставке\n\n"
            "🔐 С аккаунтом дополнительно:\n"
            "• Сохранение трек-номеров\n"
            "• История отслеживания\n"
            "• Уведомления об изменениях"
        )
        keyboard = [
            [
                InlineKeyboardButton("📝 Регистрация", callback_data="register"),
                InlineKeyboardButton("🔑 Войти", callback_data="login"),
            ],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
        return text, InlineKeyboardMarkup(keyboard)
    parcels = (
        (
            await session.execute(
                select(Parcel)
                .filter_by(user_id=user.telegram_id)
                .options(selectinload(Parcel.user))
            )
        )
        .scalars()
        .all()
    )
    if not parcels:
        text = "📦 У вас нет сохраненных посылок.\n\n💡 Добавьте трек-номер для отслеживания."
        keyboard = [
            [
                InlineKeyboardButton(
                    "➕ Добавить трек", callback_data="add_new_tracking"
                )
            ],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
        return text, InlineKeyboardMarkup(keyboard)
    text = "📦 **Ваши посылки:**\n\n"
    keyboard = []
    for parcel in parcels:
        display_name = parcel.nickname or parcel.tracking_number
        text += f"• `{display_name}` - {parcel.last_status or 'Неизвестно'}\n"
        keyboard.append(
            [
                InlineKeyboardButton(
                    display_name, callback_data=f"track_{parcel.tracking_number}"
                )
            ]
        )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    "➕ Добавить трек", callback_data="add_new_tracking"
                )
            ],
            [InlineKeyboardButton("🗑 Удалить посылку", callback_data="start_delete")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="refresh_parcels")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )
    return text, InlineKeyboardMarkup(keyboard)


@handle_errors()
@async_db_session()
async def my_parcels_cmd(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = await session.get(User, update.effective_user.id)
    if not user or not user.username:
        text, reply_markup = await get_my_parcels_content(session, user)
        await safe_send_message(
            update, text, reply_markup=reply_markup, parse_mode="Markdown"
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


@handle_errors()
@async_db_session()
async def profile_cmd(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = await session.get(User, update.effective_user.id)
    if not user or not user.username:
        text = (
            "👤 **Профиль**\n\n"
            "У вас пока нет аккаунта.\n\n"
            "🔐 Создайте аккаунт, чтобы:\n"
            "• Сохранять трек-номера\n"
            "• Получать уведомления\n"
            "• Вести историю отслеживания\n\n"
            "💡 Без аккаунта доступны все основные функции бота."
        )
        keyboard = [
            [
                InlineKeyboardButton("📝 Регистрация", callback_data="register"),
                InlineKeyboardButton("🔑 Войти", callback_data="login"),
            ],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
        await safe_send_message(
            update,
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return
    parcels_count = (
        await session.execute(
            select(func.count()).select_from(Parcel).filter_by(user_id=user.telegram_id)
        )
    ).scalar()
    text = (
        f"👤 **Ваш профиль**\n\n"
        f"**Имя:** {user.first_name or 'не указано'} {user.last_name or ''}\n"
        f"**Имя пользователя:** `{user.username or 'не указано'}`\n"
        f"**Посылок отслеживается:** {parcels_count}\n"
        f"**Дата регистрации:** {user.created_at.strftime('%d.%m.%Y') if user.created_at else 'не указана'}"
    )
    await safe_send_message(
        update, text, reply_markup=get_profile_keyboard(), parse_mode="Markdown"
    )


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
            logger.warning(f"Failed to delete message: {e}")
    try:
        msg = await (update.message or update.callback_query.message).reply_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        context.user_data["last_tracking_message_id"] = msg.message_id
    except Exception as e:
        logger.error(f"Failed to send tracking info: {e}")


async def db_get_or_create_user(
    session: AsyncSession, telegram_id: int, telegram_username: Optional[str] = None
) -> User:
    user = (
        await session.execute(select(User).filter_by(telegram_id=telegram_id))
    ).scalar_one_or_none()
    if user:
        if telegram_username and user.telegram_username != telegram_username:
            user.telegram_username = telegram_username
        return user
    user = User(telegram_id=telegram_id, telegram_username=telegram_username)
    session.add(user)
    await session.flush()
    return user


@handle_errors()
@async_db_session()
async def start(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data.clear()
    user = await session.get(User, update.effective_user.id)
    if user and user.username:
        text = (
            f"🌟 Добро пожаловать, {user.first_name or 'пользователь'}!\n\n"
            "Я помогу вам:\n"
            "📦 Отслеживать и сохранять посылки\n"
            "💰 Рассчитывать стоимость доставки\n"
            "❓ Получать информацию о доставке\n\n"
            "Выберите действие:"
        )
    else:
        text = (
            "🌟 Добро пожаловать в Boxberry Bot!\n\n"
            "Я помогу вам:\n"
            "📦 Отслеживать посылки\n"
            "💰 Рассчитывать стоимость доставки\n"
            "❓ Получать информацию о доставке\n\n"
            "💡 Все функции доступны без регистрации!\n"
            "🔐 Войдите в аккаунт для сохранения трек-номеров."
        )
    await safe_send_message(
        update, text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
    )


@handle_errors()
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_message(
        update, "Действие отменено.", reply_markup=get_main_menu_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = "🔐 Регистрация\n\nВведите ваше имя пользователя:"
    if update.message:
        await safe_send_message(update, text)
    elif update.callback_query:
        await safe_edit_message(update.callback_query, text)
    return REGISTER_LOGIN


@handle_errors()
@async_db_session()
async def register_login_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    try:
        username = clean_username(update.message.text)
    except ValueError:
        await safe_send_message(
            update,
            "❌ Имя пользователя должно содержать только буквы, цифры и подчеркивания. Попробуйте снова:",
        )
        return REGISTER_LOGIN
    existing_user = (
        await session.execute(select(User).filter_by(username=username))
    ).scalar_one_or_none()
    if existing_user:
        await safe_send_message(
            update, "❌ Это имя пользователя уже занято. Попробуйте другое:"
        )
        return REGISTER_LOGIN
    context.user_data["reg_username"] = username
    await safe_send_message(update, "🔒 Введите пароль (минимум 6 символов):")
    return REGISTER_PASSWORD


@handle_errors()
@async_db_session()
async def register_password_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    password = update.message.text.strip()
    if len(password) < 6:
        await safe_send_message(
            update, "❌ Пароль должен содержать минимум 6 символов. Попробуйте снова:"
        )
        return REGISTER_PASSWORD
    context.user_data["reg_password"] = generate_password_hash(password)
    await safe_send_message(update, "👤 Введите ваше имя:")
    return REGISTER_NAME


@handle_errors()
async def register_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await safe_send_message(
            update, "❌ Имя не может быть пустым. Попробуйте снова:"
        )
        return REGISTER_NAME
    context.user_data["reg_first"] = name
    await safe_send_message(update, "👤 Введите вашу фамилию:")
    return REGISTER_SURNAME


@handle_errors()
@async_db_session()
async def register_surname_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    surname = update.message.text.strip()
    if not surname:
        await safe_send_message(
            update, "❌ Фамилия не может быть пустой. Попробуйте снова:"
        )
        return REGISTER_SURNAME
    data = context.user_data
    try:
        user = await db_get_or_create_user(
            session, update.effective_user.id, update.effective_user.username
        )
        user.username = data["reg_username"]
        user.password = data["reg_password"]
        user.first_name = data["reg_first"]
        user.last_name = surname
        session.add(user)
        await session.commit()
        text = (
            f"✅ Регистрация завершена!\n\n"
            f"👤 Имя: {user.first_name} {user.last_name}\n"
            f"📧 Имя пользователя: {user.username}\n\n"
            f"Теперь вы можете сохранять трек-номера и пользоваться всеми функциями бота!"
        )
        await safe_send_message(
            update, text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
        )
    except IntegrityError:
        await safe_send_message(
            update, "❌ Ошибка при регистрации. Попробуйте другое имя пользователя."
        )
        return REGISTER_LOGIN
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = "🔑 Вход в аккаунт\n\nВведите ваше имя пользователя:"
    if update.message:
        await safe_send_message(update, text)
    elif update.callback_query:
        await safe_edit_message(update.callback_query, text)
    return LOGIN_LOGIN


@handle_errors()
@async_db_session()
async def login_login_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    try:
        username = clean_username(update.message.text)
    except ValueError:
        await safe_send_message(
            update,
            "❌ Имя пользователя должно содержать только буквы, цифры и подчеркивания. Попробуйте снова:",
        )
        return LOGIN_LOGIN
    user = (
        await session.execute(select(User).filter_by(username=username))
    ).scalar_one_or_none()
    if not user:
        await safe_send_message(
            update, "❌ Неверное имя пользователя. Попробуйте снова:"
        )
        return LOGIN_LOGIN
    context.user_data["login_username"] = username
    await safe_send_message(update, "🔒 Введите пароль:")
    return LOGIN_PASSWORD


@handle_errors()
@async_db_session()
async def login_password_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    username = context.user_data.get("login_username")
    if not username:
        await safe_send_message(update, "❌ Сессия истекла. Начните заново.")
        context.user_data.clear()
        return ConversationHandler.END
    user = (
        await session.execute(select(User).filter_by(username=username))
    ).scalar_one_or_none()
    if not user:
        await safe_send_message(update, "❌ Пользователь не найден.")
        context.user_data.clear()
        return ConversationHandler.END
    password = update.message.text.strip()
    if not check_password_hash(user.password, password):
        await safe_send_message(update, "❌ Неверный пароль. Попробуйте снова:")
        return LOGIN_PASSWORD
    # Update parcels user_id first to avoid foreign key violation
    old_telegram_id = user.telegram_id
    new_telegram_id = update.effective_user.id
    if old_telegram_id != new_telegram_id:
        await session.execute(
            sa_update(Parcel)
            .where(Parcel.user_id == old_telegram_id)
            .values(user_id=new_telegram_id)
        )
    # Now update user
    user.telegram_id = new_telegram_id
    user.telegram_username = update.effective_user.username
    await session.commit()
    text = (
        f"✅ Вход успешен!\n\n"
        f"👤 Добро пожаловать, {user.first_name or user.username}!\n\n"
        f"Вы можете использовать все функции аккаунта."
    )
    await safe_send_message(
        update, text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def add_tracking_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "➕ Введите трек-номер для добавления:"
    prompt_msg = await query.message.reply_text(text)
    context.user_data["add_prompt_id"] = prompt_msg.message_id
    return ADD_TRACKING


@handle_errors()
@async_db_session()
async def add_tracking_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    tracking = update.message.text.strip().upper()
    if not TRACKING_PATTERN.match(tracking):
        await safe_send_message(
            update,
            "❌ Некорректный формат трек-номера (минимум 8 символов, буквы/цифры/-). Попробуйте снова:",
        )
        return ADD_TRACKING
    user = await session.get(User, update.effective_user.id)
    if not user or not user.username:
        await safe_send_message(
            update, "❌ Для сохранения посылок зарегистрируйтесь или войдите в аккаунт."
        )
        if "add_prompt_id" in context.user_data:
            try:
                await context.bot.delete_message(
                    update.effective_chat.id, context.user_data["add_prompt_id"]
                )
            except:
                pass
        context.user_data.clear()
        return ConversationHandler.END
    existing = (
        await session.execute(
            select(Parcel).filter_by(user_id=user.telegram_id, tracking_number=tracking)
        )
    ).scalar_one_or_none()
    if existing:
        await safe_send_message(
            update, f"ℹ️ Трек-номер '{tracking}' уже сохранен в ваших посылках."
        )
        if "add_prompt_id" in context.user_data:
            try:
                await context.bot.delete_message(
                    update.effective_chat.id, context.user_data["add_prompt_id"]
                )
            except:
                pass
        context.user_data.clear()
        return ConversationHandler.END
    parcel = Parcel(
        user_id=user.telegram_id, tracking_number=tracking, last_status="Добавлено"
    )
    session.add(parcel)
    await safe_send_message(
        update, f"✅ Трек-номер '{tracking}' успешно добавлен в 'Мои посылки'!"
    )
    if "add_prompt_id" in context.user_data:
        try:
            await context.bot.delete_message(
                update.effective_chat.id, context.user_data["add_prompt_id"]
            )
        except:
            pass
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
@async_db_session()
async def change_password_start(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = await session.get(User, update.effective_user.id)
    if not user or not user.username:
        await safe_send_message(
            update,
            "❌ Для изменения пароля необходимо иметь аккаунт. Зарегистрируйтесь или войдите.",
        )
        return ConversationHandler.END
    text = "🔑 Изменение пароля\n\nВведите текущий пароль:"
    await safe_send_message(update, text)
    return CHANGE_OLD_PASSWORD


@handle_errors()
@async_db_session()
async def change_old_password_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = await session.get(User, update.effective_user.id)
    if not user or not user.password:
        await safe_send_message(update, "❌ Ошибка: аккаунт не настроен.")
        return ConversationHandler.END
    old_password = update.message.text.strip()
    if not check_password_hash(user.password, old_password):
        await safe_send_message(update, "❌ Текущий пароль неверный. Попробуйте снова:")
        return CHANGE_OLD_PASSWORD
    context.user_data["old_password_verified"] = True
    await safe_send_message(update, "🔒 Введите новый пароль (минимум 6 символов):")
    return CHANGE_NEW_PASSWORD


@handle_errors()
@async_db_session()
async def change_new_password_received(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not context.user_data.get("old_password_verified"):
        await safe_send_message(update, "❌ Сессия истекла. Начните заново.")
        context.user_data.clear()
        return ConversationHandler.END
    new_password = update.message.text.strip()
    if len(new_password) < 6:
        await safe_send_message(
            update,
            "❌ Новый пароль должен содержать минимум 6 символов. Попробуйте снова:",
        )
        return CHANGE_NEW_PASSWORD
    user = await session.get(User, update.effective_user.id)
    if not user:
        await safe_send_message(update, "❌ Ошибка аккаунта.")
        context.user_data.clear()
        return ConversationHandler.END
    user.password = generate_password_hash(new_password)
    text = "✅ Пароль успешно изменен!\n\nТеперь используйте новый пароль для входа."
    await safe_send_message(
        update, text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def calculator_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = "💰 Калькулятор доставки\n\nВыберите страну отправки:"
    keyboard = [
        [InlineKeyboardButton("🇺🇸 США", callback_data="calc_storage_usa")],
        [InlineKeyboardButton("🇨🇳 Китай", callback_data="calc_storage_china")],
        [InlineKeyboardButton("🇩🇪 Германия", callback_data="calc_storage_germany")],
        [InlineKeyboardButton("🇪🇸 Испания", callback_data="calc_storage_spain")],
        [InlineKeyboardButton("🇮🇳 Индия", callback_data="calc_storage_india")],
        [InlineKeyboardButton("🔙 Отмена", callback_data="calc_cancel")],
    ]
    if update.message:
        await safe_send_message(
            update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif update.callback_query:
        await safe_edit_message(
            update.callback_query, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return CALC_STORAGE


@handle_errors()
async def calculator_storage_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    country = parts[-1]
    storage_map = {
        "usa": {"id": "1", "name": "США"},
        "china": {"id": "2", "name": "Китай"},
        "germany": {"id": "3", "name": "Германия"},
        "spain": {"id": "4", "name": "Испания"},
        "india": {"id": "5", "name": "Индия"},
    }
    if country not in storage_map:
        text = "❌ Неизвестная страна. Попробуйте снова."
        keyboard = [
            [InlineKeyboardButton("🔙 Назад", callback_data="calc_back_to_country")]
        ]
        await safe_edit_message(
            query, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CALC_STORAGE
    storage_info = storage_map[country]
    context.user_data["storage_id"] = storage_info["id"]
    context.user_data["storage_name"] = storage_info["name"]
    text = f"📦 Страна выбрана: {storage_info['name']}\n\nВведите название города доставки (минимум 2 символа):"
    await safe_edit_message(query, text)
    return CALC_CITY_SEARCH


@handle_errors()
async def calculator_city_search_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    city_name = update.message.text.strip()
    if len(city_name) < 2:
        await safe_send_message(
            update, "❌ Введите минимум 2 символа для поиска города."
        )
        return CALC_CITY_SEARCH
    cities = await BoxberryAPI.get_cities(city_name)
    if not cities:
        text = "❌ Города не найдены. Попробуйте другой запрос."
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 Выбрать страну", callback_data="calc_back_to_country"
                )
            ]
        ]
        await safe_send_message(
            update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CALC_CITY_SEARCH
    text = f"📍 Результаты поиска для '{city_name}':"
    keyboard = [
        [
            InlineKeyboardButton(
                city["name"][:30] + "..." if len(city["name"]) > 30 else city["name"],
                callback_data=f"calc_city_{city['code']}",
            )
        ]
        for city in cities[:10]
    ]
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    "🔍 Новый поиск", callback_data="calc_city_new_search"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Выбрать страну", callback_data="calc_back_to_country"
                )
            ],
        ]
    )
    await safe_send_message(update, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return CALC_CITY_SELECT


@handle_errors()
async def calculator_city_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city_id = query.data.split("_", 2)[-1]
    button_text = [
        btn.text
        for row in query.message.reply_markup.inline_keyboard
        for btn in row
        if btn.callback_data == query.data
    ][0]
    context.user_data["city_id"] = city_id
    context.user_data["city_name"] = button_text
    text = f"🏙️ Город: {button_text}\n\nВыберите тип доставки:"
    keyboard = [
        [InlineKeyboardButton("📦 До пункта выдачи", callback_data="calc_delivery_0")],
        [InlineKeyboardButton("🚚 С курьером", callback_data="calc_delivery_1")],
    ]
    await safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    return CALC_DELIVERY


@handle_errors()
async def calculator_city_new_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    text = "Введите название города доставки:"
    await safe_edit_message(query, text)
    return CALC_CITY_SEARCH


@handle_errors()
async def calc_back_to_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    text = "Выберите страну отправки:"
    keyboard = [
        [InlineKeyboardButton("🇺🇸 США", callback_data="calc_storage_usa")],
        [InlineKeyboardButton("🇨🇳 Китай", callback_data="calc_storage_china")],
        [InlineKeyboardButton("🇩🇪 Германия", callback_data="calc_storage_germany")],
        [InlineKeyboardButton("🇪🇸 Испания", callback_data="calc_storage_spain")],
        [InlineKeyboardButton("🇮🇳 Индия", callback_data="calc_storage_india")],
        [InlineKeyboardButton("🔙 Отмена", callback_data="calc_cancel")],
    ]
    if query:
        await safe_edit_message(
            query, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await safe_send_message(
            update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return CALC_STORAGE


@handle_errors()
async def calculator_delivery_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    courier = query.data.split("_")[-1] == "1"
    context.user_data["courier"] = courier
    text = "Введите вес посылки в кг (0.1 - 31.5):"
    await safe_edit_message(query, text)
    return CALC_WEIGHT


@handle_errors()
async def calculator_weight_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    try:
        weight = float(update.message.text.replace(",", "."))
        if weight <= 0 or weight > 31.5:
            await safe_send_message(
                update, "❌ Вес должен быть от 0.1 до 31.5 кг. Попробуйте снова:"
            )
            return CALC_WEIGHT
    except ValueError:
        await safe_send_message(update, "❌ Некорректный вес. Попробуйте снова:")
        return CALC_WEIGHT
    storage_id = context.user_data["storage_id"]
    city_id = context.user_data["city_id"]
    courier = context.user_data["courier"]
    cost = await BoxberryAPI.calculate_delivery_cost(
        storage_id, city_id, weight, courier
    )
    text = f"💰 Результат расчета для {context.user_data['storage_name']}:\n\n{cost}\n\n💡 Это приблизительная стоимость. Точная зависит от габаритов и услуг."
    keyboard = [
        [InlineKeyboardButton("🔄 Новый расчет", callback_data="calc_new")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await safe_send_message(
        update, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def calculator_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.reply_text(
            "Расчет отменен.", reply_markup=get_main_menu_keyboard()
        )
    else:
        await safe_send_message(
            update, "Расчет отменен.", reply_markup=get_main_menu_keyboard()
        )
    context.user_data.clear()
    return ConversationHandler.END


@handle_errors()
async def keyword_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("kw_"):
        key = data[3:]
        choices = {
            f"{k} {' '.join(v.get('keywords', []))}": k
            for k, v in data_manager.keywords.items()
        }
        best_match = process.extractOne(
            query.message.text if query.message else "", choices.keys()
        )
        if best_match and best_match[1] > 80:
            selected_key = choices[best_match[0]]
            text = data_manager.keywords.get(selected_key, {}).get(
                "text", "Информация не найдена."
            )
            link = data_manager.keywords.get(selected_key, {}).get(
                "link", config.BASE_URL
            )
            keyboard = [[InlineKeyboardButton("Подробнее", url=link)]]
            await safe_edit_message(
                query, text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await safe_edit_message(
                query, "ℹ️ Информация не найдена. Попробуйте другой запрос."
            )


@handle_errors()
async def bxbox_rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
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
            query,
            "❌ Страна не найдена. Пожалуйста, выберите из списка.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Назад к правилам", callback_data="back_to_rules"
                        )
                    ]
                ]
            ),
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
    keyboard = [
        [InlineKeyboardButton("🔙 Назад к правилам", callback_data="back_to_rules")]
    ]
    await safe_edit_message(
        query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


@handle_errors()
async def back_to_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
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
    text = "Выберите страну для просмотра ограничений:"
    if query:
        await safe_edit_message(
            query, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await safe_send_message(
            update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )


@handle_errors()
@async_db_session()
async def start_delete_menu(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user = await session.get(User, query.from_user.id)
    if not user or not user.username:
        await query.answer(
            "❌ Для удаления посылок необходимо войти в аккаунт.", show_alert=True
        )
        return
    parcels = (
        (await session.execute(select(Parcel).filter_by(user_id=user.telegram_id)))
        .scalars()
        .all()
    )
    if not parcels:
        await query.answer("Нет посылок для удаления.", show_alert=True)
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"❌ {parcel.nickname or parcel.tracking_number}",
                callback_data=f"del_{parcel.tracking_number}",
            )
        ]
        for parcel in parcels
    ]
    keyboard.extend(
        [
            [InlineKeyboardButton("🔥🔥🔥 Удалить ВСЕ", callback_data="del_all")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_parcels")],
        ]
    )
    try:
        await safe_edit_message(
            query,
            "Выберите трек-номер для удаления:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


@handle_errors()
@async_db_session()
async def button_handler(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "main_menu":
        await query.message.reply_text(
            "Главное меню:", reply_markup=get_main_menu_keyboard()
        )
        return
    elif data == "register":
        return await register_cmd(update, context)
    elif data == "login":
        return await login_cmd(update, context)
    elif data == "bxbox_rules":
        await bxbox_rules_cmd(update, context)
    elif data == "refresh_parcels":
        user = await session.get(User, query.from_user.id)
        text, markup = await get_my_parcels_content(session, user)
        try:
            await safe_edit_message(
                query, text, reply_markup=markup, parse_mode="Markdown"
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise
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
    elif data == "add_new_tracking":
        return await add_tracking_start(update, context)
    elif data.startswith("track_"):
        tracking_id = data.split("_", 1)[1]
        await send_tracking_info(update, context, tracking_id)
    elif data == "start_delete":
        await start_delete_menu(update, context)
    elif data.startswith("del_"):
        user = await session.get(User, query.from_user.id)
        if not user or not user.username:
            await query.answer(
                "❌ Для удаления посылок необходимо войти в аккаунт.", show_alert=True
            )
            return
        if data == "del_all":
            await session.execute(
                Parcel.__table__.delete().where(Parcel.user_id == user.telegram_id)
            )
            await session.commit()
            await query.answer("✅ Все посылки удалены.", show_alert=True)
            text, markup = await get_my_parcels_content(session, user)
            try:
                await safe_edit_message(
                    query, text, reply_markup=markup, parse_mode="Markdown"
                )
            except Exception as e:
                if "Message is not modified" in str(e):
                    pass
                else:
                    raise
        else:
            tracking = data[4:]
            await session.execute(
                Parcel.__table__.delete().where(
                    and_(
                        Parcel.user_id == user.telegram_id,
                        Parcel.tracking_number == tracking,
                    )
                )
            )
            await session.commit()
            await query.answer(f"✅ Трек-номер {tracking} удален.", show_alert=True)
            text, markup = await get_my_parcels_content(session, user)
            try:
                await safe_edit_message(
                    query, text, reply_markup=markup, parse_mode="Markdown"
                )
            except Exception as e:
                if "Message is not modified" in str(e):
                    pass
                else:
                    raise
    elif data == "back_to_parcels":
        user = await session.get(User, query.from_user.id)
        text, markup = await get_my_parcels_content(session, user)
        try:
            await safe_edit_message(
                query, text, reply_markup=markup, parse_mode="Markdown"
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise
    elif data.startswith("rule_"):
        await bxbox_rules_country_selected(update, context)
    elif data == "back_to_rules":
        await back_to_rules(update, context)
    else:
        await safe_edit_message(query, "Неизвестное действие.")


@handle_errors()
@async_db_session()
async def handle_menu_selection(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text
    context.user_data.clear()
    if text == "📦 Мои посылки" or text == "📋 Мои посылки":
        await my_parcels_cmd(update, context)
    elif text == "💰 Калькулятор":
        await calculator_start(update, context)
    elif text == "📋 BxBox Правила":
        await bxbox_rules_cmd(update, context)
    elif text == "🌍 Россия → СНГ , Международные → Россия":
        boxberry_cis_text = (
            "🌍 Доставка в страны СНГ через Boxberry\n"
            "Куда вы хотите отправить посылку? Boxberry доставляет в Казахстан, Беларусь, Армению, Кыргызстан, Таджикистан и Узбекистан.\n"
            "Краткая информация: Boxberry — доставка из России в Россию (более 640 городов) и страны СНГ (Казахстан, Беларусь, Армения, Кыргызстан, Таджикистан, Узбекистан)."
        )
        await safe_send_message(
            update,
            boxberry_cis_text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Источник",
                            url="https://boxberry.ru/faq/chastnym-klientam-voprosy-i-otvety/posylki-chastnym-licam",
                        )
                    ]
                ]
            ),
        )
        bxbox_text = (
            "🌍 Доставка в Россию из стран мира через Bxbox\n"
            "Bxbox доставляет из США, Китая, Германии, Испании, Индии в Россию.\n"
            "Краткая информация: Bxbox — международная доставка в Россию из США, Китая, Германии, Испании, Индии (как часть ЕС и других партнеров)."
        )
        await safe_send_message(
            update,
            bxbox_text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Расчет стоимости доставки",
                            url="https://bxbox.boxberry.ru/#import-calculator",
                        )
                    ]
                ]
            ),
        )
    elif text == "🎫 Создать тикет":
        boxberry_ticket_text = "Boxberry — доставка из России в Россию (более 640 городов) и страны СНГ (Казахстан, Беларусь, Армения, Кыргызстан, Таджикистан, Узбекистан)."
        await safe_send_message(
            update,
            boxberry_ticket_text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Контакты Boxberry", url="https://boxberry.ru/kontakty"
                        )
                    ]
                ]
            ),
        )
        bxbox_ticket_text = "Bxbox — международная доставка в Россию из США, Китая, Германии, Испании, Индии (как часть ЕС и других партнеров)."
        await safe_send_message(
            update,
            bxbox_ticket_text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Создать тикет Bxbox",
                            url="https://bxbox.bxb.delivery/ru/new-ticket/1",
                        )
                    ]
                ]
            ),
        )
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
        await keyword_handler(session, update, context)


@handle_errors()
@async_db_session()
async def keyword_handler(
    session: AsyncSession, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user_input = update.message.text.strip()
    if TRACKING_PATTERN.match(user_input.upper()):
        tracking_number = user_input.upper()
        user = await session.get(User, update.effective_user.id)
        additional_text = ""
        if user and user.username:
            exists = (
                await session.execute(
                    select(Parcel).filter_by(
                        user_id=user.telegram_id, tracking_number=tracking_number
                    )
                )
            ).scalar_one_or_none()
            if not exists:
                parcel = Parcel(
                    user_id=user.telegram_id,
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
    choices = {
        f"{key} {' '.join(data.get('keywords', []))}": key
        for key, data in data_manager.keywords.items()
    }
    best_match = process.extractOne(user_input, choices.keys())
    if best_match and best_match[1] > 80:
        selected_key = choices[best_match[0]]
        text = data_manager.keywords.get(selected_key, {}).get(
            "text", "Информация не найдена."
        )
        link = data_manager.keywords.get(selected_key, {}).get("link", config.BASE_URL)
        keyboard = [[InlineKeyboardButton("Подробнее", url=link)]]
        await safe_send_message(
            update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await safe_send_message(
            update, "ℹ️ Информация не найдена. Попробуйте другой запрос."
        )


@handle_errors()
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ **Помощь**\n\n"
        "**Основные команды:**\n"
        "/start - Главное меню\n"
        "/help - Эта справка\n"
        "/profile - Просмотр профиля\n"
        "/myparcels - Список ваших посылок\n"
        "/calculator - Калькулятор доставки\n"
        "/register - Регистрация\n"
        "/login - Вход в аккаунт\n\n"
        "**Дополнительно:**\n"
        "• Введите трек-номер для быстрого отслеживания\n"
        "• Используйте ключевые слова для поиска информации\n"
        "• Без регистрации доступны все функции, кроме сохранения посылок"
    )
    await safe_send_message(
        update, text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown"
    )


@handle_errors()
async def create_ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    boxberry_ticket_text = "Boxberry — доставка из России в Россию (более 640 городов) и страны СНГ (Казахстан, Беларусь, Армения, Кыргызстан, Таджикистан, Узбекистан)."
    await safe_send_message(
        update,
        boxberry_ticket_text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Контакты Boxberry", url="https://boxberry.ru/kontakty"
                    )
                ]
            ]
        ),
    )
    bxbox_ticket_text = "Bxbox — международная доставка в Россию из США, Китая, Германии, Испании, Индии (как часть ЕС и других партнеров)."
    await safe_send_message(
        update,
        bxbox_ticket_text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Создать тикет Bxbox",
                        url="https://bxbox.bxb.delivery/ru/new-ticket/1",
                    )
                ]
            ]
        ),
    )


async def cleanup():
    await HTTPManager.close()
    await CacheManager.close()


async def main():
    await init_db()
    if not config.TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN not found in environment variables")
    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
    await CacheManager.init()
    menu_regex = filters.Regex(
        r"^(📦 Мои посылки|💰 Калькулятор|📋 BxBox Правила|🌍 Россия → СНГ , Международные → Россия|📋 Мои посылки|🎫 Создать тикет|❓ Помощь|👤 Профиль|🏠 Главное меню|🔑 Изменить пароль|📍 Изменить адрес)$"
    )

    async def menu_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        return ConversationHandler.END

    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("register", register_cmd),
            CallbackQueryHandler(register_cmd, pattern="^register$"),
        ],
        states={
            REGISTER_LOGIN: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    register_login_received,
                )
            ],
            REGISTER_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    register_password_received,
                )
            ],
            REGISTER_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    register_name_received,
                )
            ],
            REGISTER_SURNAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    register_surname_received,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(menu_regex, menu_cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_cmd),
            CommandHandler("profile", profile_cmd),
            CommandHandler("myparcels", my_parcels_cmd),
            CommandHandler("calculator", calculator_start),
        ],
        per_user=True,
        per_chat=True,
    )
    login_conv = ConversationHandler(
        entry_points=[
            CommandHandler("login", login_cmd),
            CallbackQueryHandler(login_cmd, pattern="^login$"),
        ],
        states={
            LOGIN_LOGIN: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex, login_login_received
                )
            ],
            LOGIN_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    login_password_received,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(menu_regex, menu_cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_cmd),
            CommandHandler("profile", profile_cmd),
            CommandHandler("myparcels", my_parcels_cmd),
            CommandHandler("calculator", calculator_start),
        ],
        per_user=True,
        per_chat=True,
    )
    add_tracking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_tracking_start, pattern="^add_new_tracking$")
        ],
        states={
            ADD_TRACKING: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex, add_tracking_received
                )
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(menu_regex, menu_cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_cmd),
            CommandHandler("profile", profile_cmd),
            CommandHandler("myparcels", my_parcels_cmd),
            CommandHandler("calculator", calculator_start),
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
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    change_old_password_received,
                )
            ],
            CHANGE_NEW_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    change_new_password_received,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(menu_regex, menu_cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_cmd),
            CommandHandler("profile", profile_cmd),
            CommandHandler("myparcels", my_parcels_cmd),
            CommandHandler("calculator", calculator_start),
        ],
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
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    calculator_city_search_received,
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
                    filters.TEXT & ~filters.COMMAND & ~menu_regex,
                    calculator_weight_received,
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
            CommandHandler("cancel", cancel),
            MessageHandler(menu_regex, menu_cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_cmd),
            CommandHandler("profile", profile_cmd),
            CommandHandler("myparcels", my_parcels_cmd),
        ],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(reg_conv)
    app.add_handler(login_conv)
    app.add_handler(add_tracking_conv)
    app.add_handler(change_password_conv)
    app.add_handler(calc_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("myparcels", my_parcels_cmd))
    app.add_handler(CallbackQueryHandler(keyword_callback_handler, pattern=r"^kw_"))
    app.add_handler(
        CallbackQueryHandler(bxbox_rules_country_selected, pattern="^rule_")
    )
    app.add_handler(CallbackQueryHandler(back_to_rules, pattern="^back_to_rules$"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_selection)
    )
    logger.info("Boxberry Bot successfully started")
    print("Boxberry Bot started.")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await cleanup()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        if loop.is_running():
            loop.create_task(main())
        else:
            loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        if not loop.is_closed():
            loop.close()
