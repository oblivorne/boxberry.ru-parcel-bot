📦 Boxberry Parcel Telegram Bot

Проект: Telegram-бот для отслеживания и управления посылками Boxberry, с асинхронной архитектурой, JSON-конфигурациями и интеграцией с API Boxberry.



🎯 Обзор проекта
Boxberry Parcel Telegram Bot — это асинхронный бот для управления посылками Boxberry, позволяющий:

Отслеживать посылки по трек-номеру
Рассчитывать стоимость доставки
Просматривать ограничения на отправку по странам
Управлять профилями пользователей
Обрабатывать запросы через естественный язык
Создавать тикеты для поддержки

Проект разработан с акцентом на надежность, производительность и масштабируемость, с использованием JSON-конфигураций и асинхронной архитектуры.

📈 Основные возможности

Асинхронное взаимодействие с Telegram и API Boxberry
JSON-конфигурации для ограничений (restrictions.json) и цен (price_matrix.json)
Поддержка регистрации, входа и управления профилем
Обработка запросов с использованием pymorphy2 и thefuzz
Логирование через встроенный модуль logging
Контейнеризация с Docker и конфигурация через .env


🏗️ Структура репозитория
boxberry-telegram-bot/
├── .env                      # Конфигурация окружения (не включена)
├── bot.py                    # Основная логика бота
├── db.py                    # Модели и конфигурация базы данных
├── docker-compose.yml       # Docker Compose для продакшена
├── Dockerfile               # Конфигурация Docker
├── init_db.sql              # SQL-скрипт для инициализации базы данных
├── keywords_mapping.json    # Ответы на ключевые слова
├── price_matrix.json        # Данные о стоимости доставки
├── requirements.txt         # Зависимости Python
├── restrictions.json        # Ограничения на отправку по странам
├── tracker.py               # Интеграция с отслеживанием посылок
├── .gitignore               # Файл игнорирования для Git
├── README.md                # Этот файл
└── LICENSE                  # Файл лицензии


🚀 Быстрый старт
Требования

Python 3.10+
PostgreSQL/MySQL/SQLite
Telegram Bot Token
Docker (опционально)

Установка

Клонировать репозиторий:

git clone https://github.com/oblivorne/boxberry.ru-parcel-bot.git
cd boxberry-telegram-bot


Установить зависимости:

pip install -r requirements.txt


Настроить переменные окружения:

cp .env.example .env
# Указать TELEGRAM_TOKEN, DATABASE_URL и BOT_BASE_URL


Инициализировать базу данных:

psql -U your_user -d your_db -f init_db.sql


Запуск бота локально:

python bot.py


Или с Docker:

docker-compose build --no-cache
docker-compose up -d


🔧 Конфигурация .env
TELEGRAM_TOKEN=ваш_токен_бота
DATABASE_URL=postgresql+asyncpg://user:password@localhost/db
BOT_BASE_URL=https://boxberry.ru


🧪 Тестирование
pytest tests/
pytest --cov=. tests/

Примечание: Добавьте файлы тестов в директорию tests/ для модульного и интеграционного тестирования.

🔒 Безопасность

Все чувствительные данные через .env
Валидация ввода для регистрации и трек-номеров
Защита от SQL-инъекций через SQLAlchemy
Корректное управление сессиями базы данных
Поддержка Unicode для эмодзи и текста


📄 Лицензия
Apache License 2.0 — см. LICENSE

🙏 Благодарности

Boxberry за предоставление API и данных
Сообщество aiogram за асинхронный фреймворк
SQLAlchemy за надежную ORM

