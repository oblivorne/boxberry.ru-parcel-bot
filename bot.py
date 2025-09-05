import logging
import os
import json
import re
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, ConversationHandler, CallbackQueryHandler
from db import SessionLocal, init_db, User, Parcel
from tracker import login_and_get_shipments
from sqlalchemy.exc import IntegrityError
from thefuzz import process, fuzz
import pymorphy2
from datetime import datetime
from functools import wraps

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BASE = os.getenv("BOT_BASE_URL", "https://boxberry.ru")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REGISTER_LOGIN, REGISTER_PASSWORD, REGISTER_NAME, REGISTER_SURNAME = range(4)
LOGIN_LOGIN, LOGIN_PASSWORD = range(4, 6)
ADD_TRACKING = 6
CHANGE_OLD_PASSWORD, CHANGE_NEW_PASSWORD = range(7, 9)
CALC_COUNTRY, CALC_CITY, CALC_WEIGHT = range(9, 12)

with open("keywords_mapping.json", "r", encoding="utf-8") as f:
    KEYWORDS = json.load(f)

with open("price_matrix.json", "r", encoding="utf-8") as f:
    PRICE_MATRIX = json.load(f)["countries"]

with open("restrictions.json", "r", encoding="utf-8") as f:
    BXBOX_RESTRICTIONS = json.load(f)["countries"]

morph = pymorphy2.MorphAnalyzer()
TRACKING_PATTERN = re.compile(r'^[A-Z0-9\-]{8,}$')

def session_handler(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        session = SessionLocal()
        try:
            return await func(session, *args, **kwargs)
        except Exception as e:
            logger.error(f"Database error: {e}")
            session.rollback()
        finally:
            session.close()
    return wrapper

def db_get_or_create_user(session, telegram_id, username=None):
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    if user: return user
    user = User(telegram_id=telegram_id, username=username)
    session.add(user)
    session.commit()
    return user

async def send_tracking_info(update: Update, context: ContextTypes.DEFAULT_TYPE, tracking_number, additional_text=""):
    tracking_url = f"{BASE}/tracking-page?id={tracking_number}"
    keyboard = [[InlineKeyboardButton("🔍 Отследить на сайте", url=tracking_url)]]
    message_text = f"📦 Трек-номер: `{tracking_number}`{additional_text}"
    
    if 'last_tracking_message_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['last_tracking_message_id'])
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
            
    try:
        msg = await (update.message or update.callback_query.message).reply_text(
            messageomanip            message_text, 
            reply_markup=InlineKeyboardMarkup(keyboard), 
            parse_mode='Markdown'
        )
        context.user_data['last_tracking_message_id'] = msg.message_id
    except Exception as e:
        logger.error(f"Failed to send tracking info: {e}")

def get_main_menu_keyboard():
    keyboard = [
        ["📦 Мои посылки"],
        ["💰 Калькулятор", "📋 BxBox Правила"],
        ["🌍 СНГ страны", "🎫 Создать тикет"],
        ["❓ Помощь", "👤 Профиль"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_profile_keyboard():
    keyboard = [
        ["🔑 Изменить пароль", "📍 Изменить адрес"],
        ["📋 Мои посылки", "🏠 Главное меню"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if user and user.first_name:
            text = f"С возвращением, {user.first_name}! 👋\n\nВыберите действие из меню:"
            await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
        else:
            text = ("🌟 Добро пожаловать в Boxberry Bot!\n\n"
                    "Я помогу вам:\n"
                    "📦 Отслеживать посылки\n"
                    "💰 Рассчитывать стоимость доставки\n"
                    "❓ Получать информацию о доставке\n\n"
                    "Для начала нужно зарегистрироваться или войти:")
            keyboard = [
                [InlineKeyboardButton("📝 Регистрация", callback_data="register")],
                [InlineKeyboardButton("🔑 Войти", callback_data="login")],
                [InlineKeyboardButton("❓ Помощь без регистрации", callback_data="help_guest")]
            ]
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error in start: {e}")
    finally:
        session.close()

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = ("❓ **Помощь по Boxberry Bot**\n\n"
                "Я могу ответить на ваши вопросы. Просто напишите, что вас интересует, например:\n"
                "`Cколько стоит доставка?`\n"
                "`Kак упаковать посылку?`\n\n"
                "**Или воспользуйтесь кнопками ниже:**")
        keyboard = [[
            InlineKeyboardButton("📚 Частые вопросы (FAQ)", url="https://boxberry.ru/faq/chastnym-klientam-voprosy-i-otvety"),
            InlineKeyboardButton("☎️ Служба поддержки", url="https://boxberry.ru/kontakty")
        ]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in help_cmd: {e}")

async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🔐 Регистрация\n\nВведите ваш логин (email):", reply_markup=ReplyKeyboardRemove())
        return REGISTER_LOGIN
    except Exception as e:
        logger.error(f"Error in register_cmd: {e}")
        return ConversationHandler.END

@session_handler
async def register_login_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    login = update.message.text.strip()
    if len(login) < 5 or "@" not in login:
        await update.message.reply_text("❌ Логин должен быть корректным email адресом. Попробуйте снова:")
        return REGISTER_LOGIN
    existing_user = session.query(User).filter_by(login=login).first()
    if existing_user:
        await update.message.reply_text("❌ Этот логин уже занят. Попробуйте другой или войдите с помощью /login.")
        return ConversationHandler.END
    context.user_data['reg_login'] = login
    await update.message.reply_text("🔒 Введите пароль (минимум 6 символов):")
    return REGISTER_PASSWORD

@session_handler
async def register_password_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    if len(password) < 6:
        await update.message.reply_text("❌ Пароль должен содержать минимум 6 символов. Попробуйте снова:")
        return REGISTER_PASSWORD
    context.user_data['reg_password'] = password
    await update.message.reply_text("👤 Введите ваше имя:")
    return REGISTER_NAME

@session_handler
async def register_name_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_first'] = update.message.text.strip()
    await update.message.reply_text("👥 Введите вашу фамилию:")
    return REGISTER_SURNAME

@session_handler
async def register_surname_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    try:
        user = db_get_or_create_user(session, update.effective_user.id, update.effective_user.username)
        user.login = data['reg_login']
        user.password = data['reg_password']
        user.first_name = data['reg_first']
        user.last_name = update.message.text.strip()
        session.add(user)
        session.commit()
        text = (f"✅ Регистрация завершена!\n\n"
                f"👤 Имя: {user.first_name} {user.last_name}\n"
                f"📧 Логин: {user.login}\n\n"
                f"Теперь вы можете пользоваться всеми функциями бота!")
        await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
    except IntegrityError:
        session.rollback()
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте еще раз.")
    except Exception as e:
        logger.error(f"Error in register_surname_received: {e}")
    context.user_data.clear()
    return ConversationHandler.END

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🔑 Вход\n\nВведите ваш логин (email):", reply_markup=ReplyKeyboardRemove())
        return LOGIN_LOGIN
    except Exception as e:
        logger.error(f"Error in login_cmd: {e}")
        return ConversationHandler.END

@session_handler
async def login_login_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['login_login'] = update.message.text.strip()
    await update.message.reply_text("🔒 Введите пароль:")
    return LOGIN_PASSWORD

@session_handler
async def login_password_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    login_text = context.user_data.get('login_login')
    password_text = update.message.text.strip()
    user = session.query(User).filter_by(login=login_text, password=password_text).first()
    if not user:
        await update.message.reply_text(
            "❌ Неверный логин или пароль. Попробуйте снова или зарегистрируйтесь /register.",
            reply_markup=get_main_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END
    user.telegram_id = update.effective_user.id
    user.username = update.effective_user.username
    session.commit()
    text = f"✅ Вход выполнен!\n\n👤 Добро пожаловать, {user.first_name}!"
    await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

@session_handler
async def profile_cmd(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user or not user.login:
        await update.message.reply_text("Вы не вошли в аккаунт. Пожалуйста, используйте /register или /login.", reply_markup=get_main_menu_keyboard())
        return

    text = (f"👤 **Ваш профиль**\n\n"
            f"**Имя:** {user.first_name or 'не указано'} {user.last_name or ''}\n"
            f"**Логин:** `{user.login}`")
    await update.message.reply_text(text, reply_markup=get_profile_keyboard(), parse_mode='Markdown')

async def change_password_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите старый пароль:", reply_markup=ReplyKeyboardRemove())
    return CHANGE_OLD_PASSWORD

@session_handler
async def change_old_password_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_password = update.message.text.strip()
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user or user.password != old_password:
        await update.message.reply_text("❌ Неверный старый пароль. Попробуйте снова.")
        return CHANGE_OLD_PASSWORD
    await update.message.reply_text("Введите новый пароль (минимум 6 символов):")
    return CHANGE_NEW_PASSWORD

@session_handler
async def change_new_password_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_password = update.message.text.strip()
    if len(new_password) < 6:
        await update.message.reply_text("❌ Новый пароль должен содержать минимум 6 символов. Попробуйте снова:")
        return CHANGE_NEW_PASSWORD
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user:
        await update.message.reply_text("❌ Пользователь не найден.")
        return ConversationHandler.END
    user.password = new_password
    session.commit()
    await update.message.reply_text("✅ Пароль успешно изменен!", reply_markup=get_profile_keyboard())
    return ConversationHandler.END

async def get_my_parcels_content(session, user):
    parcels = session.query(Parcel).filter_by(user_id=user.id).all()
    if not parcels:
        text = ("У вас пока нет отслеживаемых посылок.\n"
                "Используйте кнопку ниже, чтобы добавить первую.")
        keyboard = [[InlineKeyboardButton("➕ Добавить трек-номер", callback_data="add_new_tracking")]]
    else:
        text = f"📦 Ваши посылки ({len(parcels)}):\n\n"
        keyboard = []
        for i, parcel in enumerate(parcels, 1):
            status = parcel.last_status or "Статус не определен"
            text += f"**{i}.** `{parcel.tracking_number}`\n"
            text += f" 📊 _{status}_\n\n"
            keyboard.append([
                InlineKeyboardButton(f"🔍 {parcel.tracking_number}", callback_data=f"track_{parcel.tracking_number}")
            ])
        keyboard.append([
            InlineKeyboardButton("🗑️ Удалить", callback_data="start_delete"),
            InlineKeyboardButton("➕ Добавить новый", callback_data="add_new_tracking")
        ])
    return text, InlineKeyboardMarkup(keyboard)

@session_handler
async def my_parcels_cmd(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user or not user.login:
        keyboard = [[
            InlineKeyboardButton("📝 Регистрация", callback_data="register"),
            InlineKeyboardButton("🔑 Войти", callback_data="login")
        ]]
        await update.message.reply_text(
            "❌ Для доступа к посылкам необходимо войти в аккаунт.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if 'my_parcels_message_id' in context.user_data:
        try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['my_parcels_message_id'])
        except Exception: pass
    if 'last_tracking_message_id' in context.user_data:
        try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['last_tracking_message_id'])
        except Exception: pass

    text, reply_markup = await get_my_parcels_content(session, user)
    message = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    context.user_data['my_parcels_message_id'] = message.message_id

@session_handler
async def add_tracking_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    if not TRACKING_PATTERN.match(code):
        await update.message.reply_text("Некорректный формат. Попробуйте снова:")
        return ADD_TRACKING
        
    user = db_get_or_create_user(session, update.effective_user.id)
    exists = session.query(Parcel).filter_by(user_id=user.id, tracking_number=code).first()
    
    if not exists:
        p = Parcel(user_id=user.id, tracking_number=code, last_status="Добавлено", created_at=datetime.utcnow())
        session.add(p)
        session.commit()

    if 'add_prompt_id' in context.user_data:
        try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['add_prompt_id'])
        except Exception: pass
    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except Exception: pass

    await my_parcels_cmd(update, context)
    return ConversationHandler.END

@session_handler
async def start_delete_menu(session, query, context):
    user = session.query(User).filter_by(telegram_id=query.from_user.id).first()
    if not user:
        await query.answer("❌ Пользователь не найден.", show_alert=True)
        return

    parcels = session.query(Parcel).filter_by(user_id=user.id).all()
    if not parcels:
        await query.answer("Нет посылок для удаления.", show_alert=True)
        return

    keyboard = []
    for parcel in parcels:
        keyboard.append([InlineKeyboardButton(f"❌ {parcel.tracking_number}", callback_data=f"del_{parcel.tracking_number}")])
    keyboard.append([InlineKeyboardButton("🔥🔥🔥 Удалить ВСЕ", callback_data="del_all")])
    keyboard.append([InlineKeyboardButton("Назад", callback_data="back_to_parcels")])
    
    await query.edit_message_text("Выберите трек-номер для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))

@session_handler
async def handle_delete(session, query, context, tracking=None, all=False):
    user = session.query(User).filter_by(telegram_id=query.from_user.id).first()
    if not user:
        await query.answer("❌ Пользователь не найден.", show_alert=True)
        return
        
    if all:
        deleted_count = session.query(Parcel).filter_by(user_id=user.id).delete()
        session.commit()
        await query.answer(f"🗑️ Удалено {deleted_count} посылок!", show_alert=True)
    elif tracking:
        parcel = session.query(Parcel).filter_by(user_id=user.id, tracking_number=tracking).first()
        if parcel:
            session.delete(parcel)
            session.commit()
            await query.answer("🗑️ Удалено!", show_alert=True)
        else:
            await query.answer("ℹ️ Не найдено.", show_alert=True)
    
    text, reply_markup = await get_my_parcels_content(session, user)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def bxbox_rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("США", callback_data="rule_USA"), InlineKeyboardButton("Китай", callback_data="rule_China")],
        [InlineKeyboardButton("Германия", callback_data="rule_Germany"), InlineKeyboardButton("Испания", callback_data="rule_Spain")],
        [InlineKeyboardButton("Индия", callback_data="rule_India")]
    ]
    await update.message.reply_text("Выберите страну для просмотра ограничений:", reply_markup=InlineKeyboardMarkup(keyboard))

async def bxbox_rules_country_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    country_code = query.data.split("_", 1)[1]
    rules = BXBOX_RESTRICTIONS.get(country_code)
    if not rules:
        await query.edit_message_text("❌ Страна не найдена. Пожалуйста, выберите из списка.")
        return
    
    text = f"📋 **Правила для отправлений в {country_code}**\n\n"
    for category, details in rules["categories"].items():
        text += f"**{category}**\n"
        if details.get("standard"):
            text += "🚚 **Стандартная доставка:**\n" + "\n".join(details["standard"]) + "\n"
        if details.get("alternative"):
            text += "✈️ **Альтернативная доставка:**\n" + "\n".join(details["alternative"]) + "\n"
        if details.get("restricted"):
            text += "⚠️ **Ограничения:**\n" + "\n".join(details["restricted"]) + "\n"
        if details.get("prohibited"):
            text += "🚫 **Запрещено к пересылке:**\n" + "\n".join(details["prohibited"]) + "\n"
        if details.get("details_link"):
            text += f"[🔗 Подробнее]({details['details_link']})\n\n"
    
    text += f"📏 **Максимальные параметры:**\n"
    text += f"• Вес: *{rules.get('max_weight', 'Нет данных')}*\n"
    text += f"• Размеры: *{rules.get('max_dimensions', 'Нет данных')}*"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_rules")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)

async def back_to_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("США", callback_data="rule_USA"), InlineKeyboardButton("Китай", callback_data="rule_China")],
        [InlineKeyboardButton("Германия", callback_data="rule_Germany"), InlineKeyboardButton("Испания", callback_data="rule_Spain")],
        [InlineKeyboardButton("Индия", callback_data="rule_India")]
    ]
    await query.edit_message_text("Выберите страну для просмотра ограничений:", reply_markup=InlineKeyboardMarkup(keyboard))

async def create_ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("🎫 Для создания обращения (тикетов) или оформления заявки на выкуп, пожалуйста, перейдите на наш сайт.")
    kb = [[InlineKeyboardButton("📝 Открыть форму на сайте", url="https://bxbox.bxb.delivery/ru/new-ticket/2")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def calculator_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    countries = list(PRICE_MATRIX.keys())
    keyboard = [[InlineKeyboardButton(country, callback_data=f"calc_country_{country}")] for country in countries]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="calc_cancel")])
    await update.message.reply_text("Выберите страну отправления:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CALC_COUNTRY

async def calculator_country_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    country = query.data.replace("calc_country_", "")
    context.user_data['calc_country'] = country
    cities = list(PRICE_MATRIX[country]["cities"].keys())
    kb = [[InlineKeyboardButton(city, callback_data=f"calc_city_{city}")] for city in cities]
    kb.append([InlineKeyboardButton("Назад", callback_data="calc_back_country"), InlineKeyboardButton("Отмена", callback_data="calc_cancel")])
    await query.edit_message_text(f"Страна: {country}\nВыберите город:", reply_markup=InlineKeyboardMarkup(kb))
    return CALC_CITY

async def calculator_city_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city = query.data.replace("calc_city_", "")
    context.user_data['calc_city'] = city
    await query.edit_message_text(f"Город: {city}\nВведите вес посылки в кг (например: 2.5):")
    return CALC_WEIGHT

async def calculator_weight_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight_text = update.message.text.strip().replace(',', '.')
        w = float(weight_text)
        if not (0 < w <= 31.5):
            await update.message.reply_text("Вес должен быть от 0.1 до 31.5 кг. Попробуйте снова:")
            return CALC_WEIGHT
        country = context.user_data.get('calc_country')
        city = context.user_data.get('calc_city')
        price_data = PRICE_MATRIX.get(country, {}).get("cities", {}).get(city, {}).get("weights", {})
        price = None
        if w <= 1: price = price_data.get("0-1")
        elif w <= 5: price = price_data.get("1-5")
        elif w <= 10: price = price_data.get("5-10")
        elif w <= 20: price = price_data.get("10-20")
        elif w <= 31.5: price = price_data.get("20-31.5")

        if price is None:
            await update.message.reply_text("Не удалось рассчитать. Проверьте данные и попробуйте снова.")
        else:
            days = PRICE_MATRIX[country]["cities"][city].get("delivery_days", "N/A")
            await update.message.reply_text(
                f"**Расчет стоимости**\n\n"
                f"🌍 **Маршрут:** {country} → {city}\n"
                f"⚖️ **Вес:** {w} кг\n"
                f"💰 **Стоимость:** *{price} руб.*\n"
                f"⏳ **Примерный срок:** {days} дней",
                parse_mode='Markdown'
            )
    except (ValueError, KeyError) as e:
        logger.error(f"Error in calculator_weight_received: {e}")
        await update.message.reply_text("Некорректный ввод. Введите число, например: 5.5")
        return CALC_WEIGHT
    context.user_data.clear()
    return ConversationHandler.END

def normalize_text(text: str) -> str:
    words = re.findall(r'\w+', text.lower())
    return ' '.join([morph.parse(word)[0].normal_form for word in words])

@session_handler
async def keyword_handler(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    
    if TRACKING_PATTERN.match(user_input.upper()):
        tracking_number = user_input.upper()
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        additional_text = ""
        if user and user.login:
            exists = session.query(Parcel).filter_by(user_id=user.id, tracking_number=tracking_number).first()
            if not exists:
                p = Parcel(user_id=user.id, tracking_number=tracking_number, last_status="Добавлено")
                session.add(p)
                session.commit()
                additional_text = "\n\n✅ Трек-номер сохранен в 'Мои посылки'!"
            else:
                additional_text = "\n\nℹ️ Этот трек-номер уже есть в вашем списке."
        else:
            additional_text = "\n\n💡 Войдите или зарегистрируйтесь, чтобы сохранять трек-номера."

        await send_tracking_info(update, context, tracking_number, additional_text)
        return

    normalized_input = normalize_text(user_input)
    choices = {normalize_text(f"{key} {' '.join(data.get('keywords', []))}"): key for key, data in KEYWORDS.items()}
    results = process.extract(normalized_input, choices.keys(), limit=3, scorer=fuzz.token_set_ratio)
    best_match, best_score = results[0]

    if best_score > 70:
        key = choices[best_match]
        meta = KEYWORDS[key]
        text = meta.get("text", "Информация не найдена.")
        kb = [[InlineKeyboardButton("🔗 Подробнее на сайте", url=meta.get("link"))]] if meta.get("link") else None
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    else:
        keyboard = []
        for match, score in results:
            if score > 45:
                key = choices[match]
                button_text = f"❓ {key.capitalize()}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"kw_{key}")])
        
        if keyboard:
            await update.message.reply_text("🤔 Я не совсем уверен, что вы имеете в виду. Возможно, вас интересует:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text("К сожалению, я не смог распознать ваш запрос. Пожалуйста, попробуйте переформулировать его или воспользуйтесь главным меню.")

async def keyword_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split('_', 1)[1]
    if key in KEYWORDS:
        meta = KEYWORDS[key]
        text = meta.get("text", "Информация не найдена.")
        kb = [[InlineKeyboardButton("🔗 Подробнее на сайте", url=meta.get("link"))]] if meta.get("link") else None
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None)

@session_handler
async def button_handler(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "register":
        await query.message.reply_text("Введите логин (email):", reply_markup=ReplyKeyboardRemove())
        context.application.create_task(register_cmd(query.message, context))
    elif data == "login":
        await query.message.reply_text("Введите логин (email):", reply_markup=ReplyKeyboardRemove())
        context.application.create_task(login_cmd(query.message, context))
    elif data == "help_guest":
        await help_cmd(query.message, context)

    elif data == "add_new_tracking":
        prompt_msg = await query.message.reply_text("Введите трек-номер для добавления:")
        context.user_data['add_prompt_id'] = prompt_msg.message_id
    elif data.startswith("track_"):
        tracking_id = data.split("_", 1)[1]
        await send_tracking_info(update, context, tracking_id)
    elif data == "start_delete":
        await start_delete_menu(query, context)
    elif data == "back_to_parcels":
        user = db_get_or_create_user(session, query.from_user.id)
        text, markup = await get_my_parcels_content(session, user)
        await query.edit_message_text(text, reply_markup=markup, parse_mode='Markdown')
    elif data.startswith("del_"):
        if data == "del_all":
            await handle_delete(query, context, all=True)
        else:
            tracking_id = data.split("_", 1)[1]
            await handle_delete(query, context, tracking=tracking_id)
            
async def handle_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📦 Мои посылки": await my_parcels_cmd(update, context)
    elif text == "💰 Калькулятор": await calculator_start(update, context)
    elif text == "📋 BxBox Правила": await bxbox_rules_cmd(update, context)
    elif text == "🎫 Создать тикет": await create_ticket_cmd(update, context)
    elif text == "❓ Помощь": await help_cmd(update, context)
    elif text == "👤 Профиль": await profile_cmd(update, context)
    elif text == "🏠 Главное меню": await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())
    elif text == "🔑 Изменить пароль": await change_password_start(update, context)
    elif text == "🌍 СНГ страны":
        text_response = KEYWORDS.get("куда отправить", {}).get("text", "Информация о доставке в страны СНГ доступна на нашем сайте.")
        link = KEYWORDS.get("международная доставка", {}).get("link", "https://boxberry.ru")
        await update.message.reply_text(
            f"🌍 Доставка в страны СНГ\n\n{text_response}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Подробнее о международной доставке", url=link)]])
        )
    elif text == "📍 Изменить адрес":
        await update.message.reply_text(
            "ℹ️ Изменить адрес доставки можно через личный кабинет или обратившись в службу поддержки, если посылка еще не передана курьеру.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Подробнее о переадресации", url="https://boxberry.ru/faq/chastnym-klientam-voprosy-i-otvety/kak-pereadresovat-posylku-na-drugoi-punkt-vydachi-boxberry")]])
        )
    else:
        await keyword_handler(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.", reply_markup=get_main_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

def main():
    init_db()
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN переменная окружения не определена. Пожалуйста, укажите правильный токен в файле .env.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    reg_conv = ConversationHandler(
        entry_points=[CommandHandler('register', register_cmd), CallbackQueryHandler(register_cmd, pattern="^register$")],
        states={
            REGISTER_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_login_received)],
            REGISTER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_password_received)],
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name_received)],
            REGISTER_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_user=True
    )

    login_conv = ConversationHandler(
        entry_points=[CommandHandler('login', login_cmd), CallbackQueryHandler(login_cmd, pattern="^login$")],
        states={
            LOGIN_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_login_received)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_user=True
    )
    
    add_tracking_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^add_new_tracking$")],
        states={ADD_TRACKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_tracking_received)]},
        fallbacks=[CommandHandler('cancel', cancel)], per_user=True
    )
    
    change_password_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^🔑 Изменить пароль$'), change_password_start)],
        states={
            CHANGE_OLD_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_old_password_received)],
            CHANGE_NEW_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_new_password_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_user=True
    )
    
    calc_conv = ConversationHandler(
        entry_points=[CommandHandler('calculator', calculator_start), MessageHandler(filters.Regex(r'^💰 Калькулятор$'), calculator_start)],
        states={
            CALC_COUNTRY: [CallbackQueryHandler(calculator_country_selected, pattern="^calc_country_")],
            CALC_CITY: [CallbackQueryHandler(calculator_city_selected, pattern="^calc_city_")],
            CALC_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, calculator_weight_received)]
        },
        fallbacks=[CallbackQueryHandler(cancel, pattern="^calc_cancel$"), CommandHandler('cancel', cancel)], per_user=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("myparcels", my_parcels_cmd))
    
    app.add_handler(reg_conv)
    app.add_handler(login_conv)
    app.add_handler(add_tracking_conv)
    app.add_handler(change_password_conv)
    app.add_handler(calc_conv)
    
    app.add_handler(CallbackQueryHandler(keyword_callback_handler, pattern=r"^kw_"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CallbackQueryHandler(bxbox_rules_country_selected, pattern="^rule_"))
    app.add_handler(CallbackQueryHandler(back_to_rules, pattern="^back_to_rules$"))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_selection))

    print("Boxberry Hybrid Bot запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()