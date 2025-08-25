

# 📦 Boxberry Parcel Telegram Bot

> **Проект:** Telegram-бот для отслеживания и управления посылками Boxberry, с асинхронной архитектурой, кешированием запросов и интеграцией с API Boxberry.

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.0-orange.svg)](https://aiogram.dev)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-green.svg)](https://sqlalchemy.org)
[![Docker](https://img.shields.io/badge/Docker-поддерживается-blue.svg)](https://docker.com)
[![License](https://img.shields.io/badge/Лицензия-Apache%202.0-yellow.svg)](LICENSE)

---

## 🎯 Обзор проекта

Boxberry Parcel Telegram Bot — это асинхронный бот для управления посылками Boxberry, позволяющий:

* Отслеживать посылки по трек-номеру
* Получать уведомления о статусах доставки
* Управлять адресами и способами получения
* Интегрироваться с API Boxberry для актуальной информации
* Хранить и кешировать данные в базе данных PostgreSQL

Проект разработан с акцентом на **надежность, производительность и масштабируемость**.

---

## 📈 Основные возможности

* Асинхронное взаимодействие с Telegram и API Boxberry
* Кеширование статусов посылок для снижения нагрузки
* История треков и уведомления о смене статуса
* Поддержка нескольких пользователей и сессий
* Логирование через logfire для мониторинга ошибок
* Контейнеризация с Docker и конфигурация через `.env`

---

## 🏗️ Структура репозитория

```
boxberry-telegram-bot/
├── bot/                       # Основной код бота
│   ├── handlers/              # Обработчики команд и событий
│   ├── services/              # Сервисы работы с API Boxberry и БД
│   ├── models.py              # ORM модели SQLAlchemy
│   ├── main.py                # Точка входа бота
├── tests/                     # Юнит и интеграционные тесты
├── docs/                      # Техническая документация
├── Dockerfile
├── docker-compose.yaml
├── requirements.txt
├── .env.example
├── README.md
└── LICENSE
```

---

## 🚀 Быстрый старт

### Требования

* Python 3.12+
* PostgreSQL
* Telegram Bot Token
* Docker (опционально)

### Установка

1. Клонировать репозиторий:

```bash
git clone https://github.com/oblivorne/boxberry-telegram-bot.git
cd boxberry-telegram-bot
```

2. Установить зависимости:

```bash
pip install -r requirements.txt
```

3. Настроить переменные окружения:

```bash
cp .env.example .env
# Указать BOT_TOKEN, DATABASE_URL и другие параметры
```

4. Запуск бота локально:

```bash
python bot/main.py
```

5. Или с Docker:

```bash
docker-compose build --no-cache
docker-compose up -d
```

---

## 🔧 Конфигурация `.env`

```env
BOT_TOKEN=ваш_токен_бота
DATABASE_URL=postgresql+asyncpg://user:password@localhost/db
CACHE_TTL=3600
LOGFIRE_TOKEN=ваш_логгер_токен
```

---

## 🧪 Тестирование

```bash
pytest tests/
pytest --cov=bot tests/
```

---

## 🔒 Безопасность

* Все чувствительные данные через `.env`
* Валидация входящих данных через Pydantic
* Защита от SQL-инъекций
* Ограничение скорости запросов

---

## 📄 Лицензия

Apache License 2.0 - см. [LICENSE](LICENSE)

---

## 🙏 Благодарности

* Boxberry API за предоставление данных о посылках
* Сообщество aiogram за асинхронный фреймворк
* SQLAlchemy за надежную ORM

---
