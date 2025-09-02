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
with open("keywords_mapping.json", "r", encoding="utf-8") as f:
    KEYWORDS = json.load(f)
morph = pymorphy2.MorphAnalyzer()
TRACKING_PATTERN = re.compile(r'^[A-Z0-9\-]{8,}$')
def session_handler(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        session = SessionLocal()
        try:
            return await func(session, *args, **kwargs)
        finally:
            session.close()
    return wrapper
def db_get_or_create_user(session, telegram_id, username=None):
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    if user:
        return user
    user = User(telegram_id=telegram_id, username=username)
    session.add(user)
    session.commit()
    return user
def get_main_menu_keyboard():
    keyboard = [
        ["📦 Посылки", "📍 Трекинг"],
        ["💰 Калькулятор", "🌍 СНГ страны"],
        ["❓ Помощь", "👤 Профиль"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
def get_profile_keyboard():
    keyboard = [
        ["✏️ Изменить данные", "📍 Изменить адрес", "🔑 Изменить пароль"],
        ["📋 Мои посылки", "🏠 Главное меню"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    session.close()
   
    if user and user.first_name:
        text = f"Добро пожаловать, {user.first_name}! 👋\n\nВыберите действие из меню ниже:"
        await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
    else:
        text = ("🌟 Добро пожаловать в Boxberry Bot!\n\n"
                "Я помогу вам:\n"
                "📦 Отслеживать посылки\n"
                "💰 Рассчитывать стоимость доставки\n"
                "❓ Получать информацию о доставке\n\n"
                "Для начала нужно зарегистрироваться:")
        keyboard = [
            [InlineKeyboardButton("🔐 Регистрация", callback_data="register")],
            [InlineKeyboardButton("🔑 Уже есть аккаунт", callback_data="login")],
            [InlineKeyboardButton("❓ Помощь без регистрации", callback_data="help_guest")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
   
    if query.data == "register":
        context.user_data.clear()
        await query.message.reply_text("Для регистрации введите ваш логин (любой email):", reply_markup=ReplyKeyboardRemove())
        return REGISTER_LOGIN
    elif query.data == "login":
        context.user_data.clear()
        await query.message.reply_text("Для входа введите ваш логин (email):", reply_markup=ReplyKeyboardRemove())
        return LOGIN_LOGIN
    elif query.data == "help_guest":
        text = ("ℹ️ Помощь по Boxberry:\n\n"
                "📦 Отследить посылку: отправьте номер трека\n"
                "💰 Рассчитать стоимость: напишите 'калькулятор'\n"
                "🌍 СНГ страны: напишите 'снг'\n"
                "❓ FAQ: напишите 'вопросы'\n\n"
                "Для полного функционала используйте /start")
        await query.edit_message_text(text)
    elif query.data.startswith("track_"):
        tracking_id = query.data.split("_", 1)[1]
        tracking_url = f"https://boxberry.ru/tracking-page?id={tracking_id}"
        keyboard = [[InlineKeyboardButton("🔍 Отследить на сайте", url=tracking_url)]]
        await query.edit_message_text(
            f"📦 Трек-номер: `{tracking_id}`\n\nНажмите кнопку для отслеживания:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    elif query.data == "add_new_tracking":
        context.user_data['my_parcels_message_id'] = query.message.message_id
        prompt_msg = await query.message.reply_text("Введите новый трек-номер для отслеживания:")
        context.user_data['add_prompt_id'] = prompt_msg.message_id
        return ADD_TRACKING
    elif query.data == "start_delete":
        await start_delete_menu(query, context)
    elif query.data == "del_all":
        await handle_delete(query, context, all=True)
    elif query.data.startswith("del_"):
        tracking_id = query.data.split("_", 1)[1]
        await handle_delete(query, context, tracking=tracking_id)
async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("🔐 Регистрация в Boxberry Bot\n\n"
            "Введите ваш логин (email):")
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
    return REGISTER_LOGIN
async def register_login_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    login = update.message.text.strip()
    if len(login) < 3:
        await update.message.reply_text("❌ Логин должен содержать минимум 3 символа:")
        return REGISTER_LOGIN
    session = SessionLocal()
    existing_user = session.query(User).filter_by(login=login).first()
    session.close()
    if existing_user:
        await update.message.reply_text("❌ Этот логин уже зарегистрирован. Попробуйте другой или используйте /login:")
        return REGISTER_LOGIN
    context.user_data["login"] = login
    await update.message.reply_text("🔒 Введите ваш пароль (минимум 6 символов):")
    return REGISTER_PASSWORD
async def register_password_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    if len(password) < 6:
        await update.message.reply_text("❌ Пароль должен содержать минимум 6 символов. Попробуйте снова:")
        return REGISTER_PASSWORD
    context.user_data["password"] = password
    await update.message.reply_text("👤 Введите ваше имя:")
    return REGISTER_NAME
async def register_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["first_name"] = update.message.text.strip()
    await update.message.reply_text("👥 Введите вашу фамилию:")
    return REGISTER_SURNAME
@session_handler
async def register_surname_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_name"] = update.message.text.strip()
    try:
        user = db_get_or_create_user(session, update.effective_user.id, update.effective_user.username)
        user.login = context.user_data.get("login")
        user.password = context.user_data.get("password")
        user.first_name = context.user_data.get("first_name")
        user.last_name = context.user_data.get("last_name")
        session.add(user)
        session.commit()
        text = (f"✅ Регистрация завершена!\n\n"
                f"👤 Имя: {user.first_name} {user.last_name}\n"
                f"📧 Логин: {user.login}\n\n"
                f"Теперь вы можете пользоваться всеми функциями бота!")
        await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
    except IntegrityError:
        session.rollback()
        await update.message.reply_text("❌ Ошибка при сохранении. Попробуйте еще раз.")
    context.user_data.clear()
    return ConversationHandler.END
async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("🔑 Вход в аккаунт\n\n"
            "Введите ваш логин:")
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
    return LOGIN_LOGIN
async def login_login_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login"] = update.message.text.strip()
    await update.message.reply_text("🔒 Введите ваш пароль:")
    return LOGIN_PASSWORD
@session_handler
async def login_password_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    login = context.user_data.get("login")
    password = update.message.text.strip()
    user = session.query(User).filter_by(login=login, password=password).first()
    if not user:
        await update.message.reply_text(
            "❌ Неверный логин или пароль. Попробуйте снова или используйте /register для регистрации.",
            reply_markup=get_main_menu_keyboard()
        )
        context.user_data.clear()
        return ConversationHandler.END
    user.telegram_id = update.effective_user.id
    user.username = update.effective_user.username
    session.commit()
    text = (f"✅ Вход выполнен успешно!\n\n"
            f"👤 Добро пожаловать, {user.first_name} {user.last_name}!\n"
            f"📧 Логин: {user.login}")
    await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END
async def send_tracking_response(update, tracking_number, additional_text="", context=None):
    tracking_url = f"https://boxberry.ru/tracking-page?id={tracking_number}"
    keyboard = [[InlineKeyboardButton("🔍 Отследить на сайте", url=tracking_url)]]
    message = f"📦 Трек-номер: `{tracking_number}`{additional_text}"
    msg = await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    if context:
        context.user_data['tracking_response_id'] = msg.message_id
@session_handler
async def add_tracking_received(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracking_number = update.message.text.strip().upper()
    if not TRACKING_PATTERN.match(tracking_number):
        await update.message.reply_text(
            "❌ Неверный формат трек-номера. Попробуйте снова:"
        )
        return ADD_TRACKING
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user:
        await update.message.reply_text("❌ Пользователь не найден.")
        return ConversationHandler.END
    existing_parcel = session.query(Parcel).filter_by(user_id=user.id, tracking_number=tracking_number).first()
    if existing_parcel:
        await update.message.reply_text(
            f"ℹ️ Трек-номер `{tracking_number}` уже добавлен в ваш список.",
            parse_mode='Markdown'
        )
    else:
        new_parcel = Parcel(
            user_id=user.id,
            tracking_number=tracking_number,
            last_status="Добавлено для отслеживания"
        )
        session.add(new_parcel)
        session.commit()
    if 'my_parcels_message_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['my_parcels_message_id'])
        except:
            pass
    if 'add_prompt_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['add_prompt_id'])
        except:
            pass
    text, reply_markup = await get_my_parcels_content(session, user)
    new_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
    context.user_data['my_parcels_message_id'] = new_msg.message_id
    if not existing_parcel:
        await send_tracking_response(update, tracking_number, "\n\n✅ Добавлен! Теперь отслеживайте через 📋 Мои посылки.", context)
    else:
        await send_tracking_response(update, tracking_number, "\n(уже в вашем списке)", context)
    return ConversationHandler.END
async def handle_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📦 Посылки":
        await parcel_cmd(update, context)
    elif text == "📍 Трекинг":
        await update.message.reply_text(
            "📍 Отслеживание посылки\n\n"
            "Просто отправьте мне номер для отслеживания (трек-номер).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Открыть страницу отслеживания", url="https://boxberry.ru/tracking-page")
            ]])
        )
    elif text == "💰 Калькулятор":
        await update.message.reply_text(
            "💰 Калькулятор стоимости\n\n"
            "Рассчитайте стоимость и сроки доставки на нашем сайте:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🧮 Открыть калькулятор", url="https://boxberry.ru/#calculator")
            ]])
        )
    elif text == "🌍 СНГ страны":
        text_response = KEYWORDS.get("куда отправить", {}).get("text", "Boxberry доставляет в несколько стран СНГ. Узнайте больше на сайте.")
        await update.message.reply_text(
            f"🌍 Доставка в страны СНГ\n\n{text_response}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Подробнее о международной доставке", url=KEYWORDS.get("международная доставка", {}).get("link", "https://boxberry.ru"))]])
        )
    elif text == "❓ Помощь":
        await help_cmd(update, context)
    elif text == "👤 Профиль":
        await profile_cmd(update, context)
    elif text == "📋 Мои посылки":
        if 'tracking_response_id' in context.user_data:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['tracking_response_id'])
            except:
                pass
        await my_parcels_cmd(update, context)
    elif text == "🏠 Главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())
    elif text == "📍 Изменить адрес":
        await update.message.reply_text(
            "ℹ️ Изменить адрес доставки можно через личный кабинет или обратившись в службу поддержки, если посылка еще не передана курьеру.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 Подробнее о переадресации", url="https://boxberry.ru/faq/chastnym-klientam-voprosy-i-otvety/kak-pereadresovat-posylku-na-drugoi-punkt-vydachi-boxberry")
            ]])
        )
    elif text == "🔑 Изменить пароль":
        await change_password_start(update, context)
    else:
        await keyword_handler(update, context)
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
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("❓ **Помощь по Boxberry Bot**\n\n"
            "Я могу ответить на ваши вопросы. Просто напишите, что вас интересует, например:\n"
            "`Cколько стоит доставка в Минск?`\n"
            "`Kак упаковать посылку?`\n"
            "`Mожно ли отправить без паспорта?`\n\n"
            "**Основные команды:**\n"
            "🔹 /start - Главное меню\n"
            "🔹 /parcel - Проверить мои посылки (нужна регистрация)\n"
            "🔹 /help - Эта справка\n\n"
            "**Или воспользуйтесь кнопками ниже:**")
    keyboard = [[
        InlineKeyboardButton("📚 Частые вопросы (FAQ)", url="https://boxberry.ru/faq/chastnym-klientam-voprosy-i-otvety"),
        InlineKeyboardButton("☎️ Служба поддержки", url="https://boxberry.ru/kontakty")
    ]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
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
@session_handler
async def parcel_cmd(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
    if not user or not user.login:
        keyboard = [[
            InlineKeyboardButton("🔐 Регистрация", callback_data="register"),
            InlineKeyboardButton("🔑 Вход", callback_data="login")
        ]]
        await update.message.reply_text(
            "❌ Для доступа к посылкам необходимо войти в аккаунт.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    if 'tracking_response_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['tracking_response_id'])
        except:
            pass
    await my_parcels_cmd(update, context)
async def get_my_parcels_content(session, user):
    parcels = session.query(Parcel).filter_by(user_id=user.id).all()
    if not parcels:
        text = (f"📦 Добро пожаловать, {user.first_name}!\n\n"
                "У вас пока нет отслеживаемых посылок.\n"
                "Используйте кнопку 'Добавить трек-номер' для добавления посылки.")
        keyboard = [[InlineKeyboardButton("➕ Добавить трек-номер", callback_data="add_new_tracking")]]
    else:
        text = f"📦 Ваши посылки ({len(parcels)}):\n\n"
        keyboard = []
        for i, parcel in enumerate(parcels, 1):
            status = parcel.last_status or "Статус не определен"
            text += f"**{i}.** `{parcel.tracking_number}`\n"
            text += f" 📊 {status}\n\n"
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
        await update.message.reply_text(
            "❌ Для доступа к посылкам необходимо войти в аккаунт.",
            reply_markup=get_main_menu_keyboard()
        )
        return
    if 'my_parcels_message_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['my_parcels_message_id'])
        except:
            pass
    text, reply_markup = await get_my_parcels_content(session, user)
    message = await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    context.user_data['my_parcels_message_id'] = message.message_id
@session_handler
async def start_delete_menu(session, query, context):
    user = session.query(User).filter_by(telegram_id=query.from_user.id).first()
    parcels = session.query(Parcel).filter_by(user_id=user.id).all()
    if not parcels:
        await query.answer("Нет посылок для удаления.")
        return
    text = "Какой трек-номер удалить?\n\n"
    keyboard = []
    for i, parcel in enumerate(parcels, 1):
        text += f"{i}. {parcel.tracking_number}\n"
        keyboard.append([InlineKeyboardButton(str(i), callback_data=f"del_{parcel.tracking_number}")])
    keyboard.append([InlineKeyboardButton("Все", callback_data="del_all")])
    try:
        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except:
        pass
    delete_msg = await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['delete_menu_id'] = delete_msg.message_id
def normalize_text(text, morph_analyzer):
    words = re.findall(r'\w+', text.lower())
    return ' '.join([morph_analyzer.parse(word)[0].normal_form for word in words])
@session_handler
async def keyword_handler(session, update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    if TRACKING_PATTERN.match(user_input.upper()):
        tracking_number = user_input.upper()
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if user and user.login:
            existing_parcel = session.query(Parcel).filter_by(user_id=user.id, tracking_number=tracking_number).first()
            if not existing_parcel:
                new_parcel = Parcel(
                    user_id=user.id,
                    tracking_number=tracking_number,
                    last_status="Добавлено для отслеживания"
                )
                session.add(new_parcel)
                session.commit()
                additional_text = "\n\n✅ Добавлен! Используйте 📋 Мои посылки для просмотра."
            else:
                additional_text = "\n(уже в вашем списке)"
        else:
            additional_text = "\n\n💡 Зарегистрируйтесь для автоматического сохранения трек-номеров!"
        await send_tracking_response(update, tracking_number, additional_text, context)
        return
    normalized_input = normalize_text(user_input, morph)
    choices = {}
    key_mapping = {}
    for key, data in KEYWORDS.items():
        normalized_key_text = normalize_text(f"{key} {' '.join(data.get('keywords', []))}", morph)
        choices[normalized_key_text] = normalized_key_text
        key_mapping[normalized_key_text] = key
    best_matches = process.extract(normalized_input, choices, limit=3, scorer=fuzz.token_set_ratio)
    if not best_matches:
        await update.message.reply_text(
            "К сожалению, я не смог распознать ваш запрос. Пожалуйста, попробуйте переформулировать его или воспользуйтесь главным меню.",
            reply_markup=get_main_menu_keyboard()
        )
        return
    processed_matches = [(match[0], match[1]) for match in best_matches]
    best_match_normalized, best_score = processed_matches[0]
    best_match_key = key_mapping.get(best_match_normalized)
    if not best_match_key:
        await update.message.reply_text(
            "К сожалению, я не смог распознать ваш запрос. Пожалуйста, попробуйте переформулировать его или воспользуйтесь главным меню.",
            reply_markup=get_main_menu_keyboard()
        )
        return
    MATCH_THRESHOLD = 70
    if best_score >= MATCH_THRESHOLD:
        meta = KEYWORDS[best_match_key]
        keyboard = [[InlineKeyboardButton("🔗 Открыть подробности", url=meta['link'])]]
        await update.message.reply_text(
            f"ℹ️ {meta['text']}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    else:
        text_response = "🤔 Я не совсем уверен, что вы имеете в виду. Возможно, вас интересует один из этих вопросов?\n"
        keyboard = []
        for normalized_text, score in processed_matches:
            if score > 40:
                original_key = key_mapping.get(normalized_text)
                if original_key:
                    button_text = f"❓ {original_key.capitalize()}"
                    callback_data = f"kw_{original_key}"
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        if not keyboard:
            await update.message.reply_text(
                "К сожалению, я не смог распознать ваш запрос. Пожалуйста, попробуйте переформулировать его или воспользуйтесь главным меню.",
                reply_markup=get_main_menu_keyboard()
            )
        else:
            await update.message.reply_text(text_response, reply_markup=InlineKeyboardMarkup(keyboard))
async def keyword_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split('_', 1)[1]
    if key in KEYWORDS:
        meta = KEYWORDS[key]
        keyboard = [[InlineKeyboardButton("🔗 Открыть подробности", url=meta['link'])]]
        await query.edit_message_text(
            f"ℹ️ {meta['text']}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
@session_handler
async def handle_delete(session, query, context, tracking=None, all=False):
    user = session.query(User).filter_by(telegram_id=query.from_user.id).first()
    if not user:
        await query.answer("❌ Пользователь не найден.")
        return
    if all:
        session.query(Parcel).filter_by(user_id=user.id).delete()
        session.commit()
        await query.answer("🗑️ Все удалено!")
    else:
        parcel = session.query(Parcel).filter_by(user_id=user.id, tracking_number=tracking).first()
        if parcel:
            session.delete(parcel)
            session.commit()
            await query.answer("🗑️ Удалено!")
        else:
            await query.answer("ℹ️ Не найдено.")
    if 'delete_menu_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=context.user_data['delete_menu_id'])
        except:
            pass
    if 'my_parcels_message_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=context.user_data['my_parcels_message_id'])
        except:
            pass
    if 'tracking_response_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=context.user_data['tracking_response_id'])
        except:
            pass
    text, reply_markup = await get_my_parcels_content(session, user)
    new_msg = await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
    context.user_data['my_parcels_message_id'] = new_msg.message_id
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.", reply_markup=get_main_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END
async def handle_add_tracking_menu(update, context):
    return ADD_TRACKING
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler('register', register_cmd), CallbackQueryHandler(button_handler, pattern="^register$")],
        states={
            REGISTER_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_login_received)],
            REGISTER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_password_received)],
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name_received)],
            REGISTER_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=False
    )
    login_conv = ConversationHandler(
        entry_points=[CommandHandler('login', login_cmd), CallbackQueryHandler(button_handler, pattern="^login$")],
        states={
            LOGIN_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_login_received)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=False
    )
    add_tracking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(button_handler, pattern="^add_new_tracking$"),
            MessageHandler(filters.TEXT & filters.Regex(r'^➕ Добавить трек-номер$'), handle_add_tracking_menu)
        ],
        states={
            ADD_TRACKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_tracking_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=False
    )
    change_password_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex(r'^🔑 Изменить пароль$'), change_password_start)],
        states={
            CHANGE_OLD_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_old_password_received)],
            CHANGE_NEW_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_new_password_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True,
        per_chat=False
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(reg_conv)
    app.add_handler(login_conv)
    app.add_handler(add_tracking_conv)
    app.add_handler(change_password_conv)
    app.add_handler(CommandHandler("parcel", parcel_cmd))
    app.add_handler(CallbackQueryHandler(keyword_callback_handler, pattern=r"^(kw_|add_new_tracking)"))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(help_guest|track_|start_delete|del_)"))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r'^(📦 Посылки|📍 Трекинг|💰 Калькулятор|🌍 СНГ страны|❓ Помощь|👤 Профиль|📋 Мои посылки|➕ Добавить трек-номер|🏠 Главное меню|📍 Изменить адрес|🔑 Изменить пароль)$'),
        handle_menu_selection
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, keyword_handler))
    print("🤖 Boxberry Bot запущен...")
    app.run_polling()
if __name__ == "__main__":
    main()