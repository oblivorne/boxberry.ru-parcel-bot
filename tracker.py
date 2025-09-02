import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
import logging

BASE = "https://boxberry.ru"
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
})

def login_and_get_shipments(login: str, password: str):
    """
    Вход в личный кабинет Boxberry и получение списка отправлений, адаптировано под текущую структуру сайта.
    """
    login_url = urljoin(BASE, "/private-office/")
    
    try:
        # Открываем страницу входа для получения cookies и формы
        r = session.get(login_url, timeout=15)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Не удалось загрузить страницу входа: {e}")
        return None  # Возвращаем None в случае ошибки

    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", {"class": "lk-auth__form"})
    
    if not form:
        logging.error("Форма входа не найдена на странице.")
        return None

    # Собираем данные формы, включая скрытые поля
    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        
        # Присваиваем значения для логина и пароля
        if "login" in name.lower() or "email" in name.lower():
            payload[name] = login
        elif "pass" in name.lower():
            payload[name] = password
        else:
            # Все остальные поля (например, CSRF-токен) берем как есть
            payload[name] = inp.get("value", "")
    
    # Получаем URL действия формы, если отсутствует — используем login_url
    action_url = form.get("action") or login_url
    if not action_url.startswith('http'):
        action_url = urljoin(BASE, action_url)

    try:
        # Отправляем запрос на вход
        post_response = session.post(action_url, data=payload, timeout=15, allow_redirects=True)
        post_response.raise_for_status()
        
        # Проверяем успешность входа
        if not is_login_successful(post_response):
            logging.warning(f"Вход не выполнен для {login} — неверные учетные данные")
            return False  # Вход неуспешен
        
        # Анализируем содержимое страницы после входа
        lk_soup = BeautifulSoup(post_response.text, "html.parser")
        
        shipments = []
        # Селекторы для списка отправлений
        order_items = lk_soup.select(".lk-o-item")

        if not order_items:
            # Вход успешен, но отправления не найдены
            logging.info(f"Вход успешен для {login}, но отправления не найдены.")
            return []

        for item in order_items:
            tracking_el = item.select_one(".lk-o-item__number a")
            status_el = item.select_one(".lk-o-item__status-text")
            
            tracking = tracking_el.text.strip() if tracking_el else "Трек-номер не найден"
            status = status_el.text.strip() if status_el else "Статус не определен"
            
            shipments.append({
                "tracking": tracking,
                "recipient_name": "",
                "recipient_surname": "",
                "status": status,
                "raw": item.get_text(" ", strip=True)
            })
        
        return shipments

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка при отправке запроса на вход или последующих запросах: {e}")
        return None

def is_login_successful(response):
    """
    Проверяет успешность входа различными методами
    """
    # 1. Проверка URL — успешный вход обычно перенаправляет на /private-office/ или /lk/
    if "/private-office/" not in response.url and "/lk/" not in response.url:
        return False
    
    # 2. Проверка содержимого
    soup = BeautifulSoup(response.text, "html.parser")
    
    # Признаки неуспешного входа
    error_indicators = [
        ".error",
        ".alert-danger", 
        ".login-error",
        "[data-error]",
        ".field-error"
    ]
    
    for indicator in error_indicators:
        if soup.select(indicator):
            error_text = soup.select_one(indicator).get_text(strip=True)
            if any(word in error_text.lower() for word in ['ошибка', 'неверный', 'неправильный', 'error', 'invalid']):
                logging.warning(f"Обнаружена ошибка входа: {error_text}")
                return False
    
    # Признаки успешного входа
    success_indicators = [
        ".lk-header",  # Заголовок личного кабинета
        ".user-profile",  # Профиль пользователя
        ".lk-menu",  # Меню личного кабинета
        ".logout",  # Ссылка на выход
        "[data-user]"  # Данные пользователя
    ]
    
    for indicator in success_indicators:
        if soup.select(indicator):
            return True
    
    # 3. Проверка наличия формы входа — если форма все еще есть, вход неуспешен
    login_form = soup.find("form", {"class": "lk-auth__form"})
    if login_form:
        return False
    
    # 4. Проверка заголовка страницы
    title = soup.find("title")
    if title:
        title_text = title.get_text().lower()
        if "личный кабинет" in title_text or "профиль" in title_text:
            return True
        elif "вход" in title_text or "авторизация" in title_text:
            return False
    
    # Если явных признаков нет, решение принимается по URL
    return "/private-office/" in response.url or "/lk/" in response.url

def search_tracking_by_name(name, surname):
    """
    Эта функция вряд ли будет работать с текущей структурой сайта,
    так как общий поиск по имени больше не поддерживается.
    В целях безопасности возвращается пустой список.
    """
    logging.info(f"Вызвана функция search_tracking_by_name для {name} {surname}, но она отключена.")
    return []