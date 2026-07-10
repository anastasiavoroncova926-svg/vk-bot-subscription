import os
import vk_api
import time
import re
import sqlite3
from datetime import datetime
from requests.exceptions import ReadTimeout, ConnectionError
from langchain_openai import ChatOpenAI
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from collections import defaultdict
import requests
import json
from langchain_community.llms import YandexGPT
from dotenv import load_dotenv
from dotenv import load_dotenv

load_dotenv()  # загружает .env, если он есть

DATA_DIR = os.getenv('DATA_DIR')
os.makedirs(DATA_DIR, exist_ok=True)
db_path = os.path.join(DATA_DIR, 'bot.db')


def init_db(db_path):
    """
    Инициализирует БД, если её нет, и приводит схему к актуальному виду.
    Ничего не удаляет и не сбрасывает данные.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Создаём таблицу users, если её ещё нет
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vk_user_id INTEGER NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            status TEXT DEFAULT 'free_day',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 2. Добавляем поле status, если его ещё нет (на случай старых БД)
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'status' not in columns:
        cursor.execute('ALTER TABLE users ADD COLUMN status TEXT DEFAULT "free_day"')
        # Заполняем NULL-значения для старых записей
        cursor.execute('UPDATE users SET status = "free_day" WHERE status IS NULL')

    # 3. Создаём useranswers, если её нет
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS useranswers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            servings TEXT,
            meals_count TEXT,
            restrictions TEXT,
            cooking_time TEXT,
            budget TEXT,
            preferences TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
                ON DELETE CASCADE
                ON UPDATE CASCADE
        )
    ''')

    # Включаем проверку внешних ключей
    cursor.execute("PRAGMA foreign_keys = ON")

    conn.commit()
    conn.close()



# Кэш для хранения результатов проверки подписок с TTL
class SubscriptionCache:
    def __init__(self, ttl_seconds=3600):  # TTL по умолчанию — 1 час
        self.cache = defaultdict(dict)
        self.ttl_seconds = ttl_seconds

    def get(self, user_id):
        """Получить статус подписки из кэша, если он ещё актуален"""
        if user_id in self.cache:
            cache_entry = self.cache[user_id]
            if time.time() - cache_entry['timestamp'] < self.ttl_seconds:
                print(f"Кэш попал: пользователь {user_id} — {cache_entry['is_subscriber']}")
                return cache_entry['is_subscriber']
            else:
                # Удаляем просроченный кэш
                del self.cache[user_id]
        return None

    def set(self, user_id, is_subscriber):
        """Сохранить результат проверки в кэш"""
        self.cache[user_id] = {
            'is_subscriber': is_subscriber,
            'timestamp': time.time()
        }
        print(f"Кэш сохранён: пользователь {user_id} — {is_subscriber}")

# Создаём экземпляр кэша (TTL = 1 час)
subscription_cache = SubscriptionCache(ttl_seconds=3600)

# Проверяет, является ли пользователь подписчиком VK Donut для сообщества. Использует кэш для снижения нагрузки на API.
def check_donut_subscription(vk, user_id, group_id):

    # Сначала проверяем кэш
    cached_result = subscription_cache.get(user_id)
    if cached_result is not None:
        return cached_result

    # Основная проверка через donut.isDon
    try:
        response = vk.donut.isDon(
            owner_id=group_id,
            user_id=user_id
        )
        is_subscriber = response.get('is_don', False)
        print(f"Ответ API VK Donut (основной метод): {response}")

        # Сохраняем результат в кэш
        subscription_cache.set(user_id, is_subscriber)
        return is_subscriber

    except vk_api.exceptions.VkApiError as e:
        print(f"Ошибка API VK при проверке подписки (основной метод): {e}")
        if e.code == 5:
            print("Ошибка 5: право 'donut' недоступно. Переходим к альтернативным методам...")

        # Переходим к запасным проверкам
        is_subscriber = check_donut_subscription_alt_v1(vk, user_id, group_id)
        if is_subscriber:
            subscription_cache.set(user_id, True)
            print(f"Пользователь {user_id} найден в подписчиках Donut (метод 1)")
            return True

        is_subscriber = check_donut_subscription_alt_v2(vk, user_id, group_id)
        if is_subscriber:
            subscription_cache.set(user_id, True)
            print(f"Пользователь {user_id} — подписчик Donut (метод 2)")
            return True

        subscription_cache.set(user_id, False)
        print(f"Ни один метод не подтвердил подписку пользователя {user_id}")
        return False

    except Exception as e:
        print(f"Неожиданная ошибка при основной проверке: {e}")
        # Пробуем запасные методы при любой неожиданной ошибке
        is_subscriber = check_donut_subscription_alt_v1(vk, user_id, group_id)
        if is_subscriber:
            subscription_cache.set(user_id, True)
            print(f"Пользователь {user_id} найден в подписчиках Donut (метод 1 — запасной)")
            return True

        is_subscriber = check_donut_subscription_alt_v2(vk, user_id, group_id)
        if is_subscriber:
            subscription_cache.set(user_id, True)
            print(f"Пользователь {user_id} — подписчик Donut (метод 2 — запасной)")
            return True

        subscription_cache.set(user_id, False)
        return False

def check_donut_subscription_alt_v1(vk, user_id, group_id):
    """Альтернативная проверка через groups.getMembers с фильтром 'donut'"""
    try:
        donut_members = vk.groups.getMembers(
            group_id=group_id,
            filter='donut',
            count=1000
        )
        return user_id in donut_members.get('items', [])
    except vk_api.exceptions.VkApiError as e:
        print(f"Ошибка при проверке через groups.getMembers: {e}")
        return False
    except Exception as e:
        print(f"Неожиданная ошибка в alt_v1: {e}")
        return False

# Альтернативная проверка через users.get с полем donut
def check_donut_subscription_alt_v2(vk, user_id, group_id):
    try:
        user_info = vk.users.get(
            user_ids=user_id,
            fields='donut'
        )
        if user_info and len(user_info) > 0:
            user_data = user_info[0]
            if 'donut' in user_data:
                return user_data['donut']
        return False
    except vk_api.exceptions.VkApiError as e:
        print(f"Ошибка при проверке через users.get: {e}")
        return False
    except Exception as e:
        print(f"Неожиданная ошибка в alt_v2: {e}")
        return False
        
# Сохраняет связь VK ID и внутреннего user_id в БД
def save_user_mapping(vk_user_id, user_id, db_path):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR IGNORE INTO users (vk_user_id, user_id) VALUES (?, ?)',
            (vk_user_id, user_id)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as e:
        print(f"Ошибка при сохранении в БД: {e}")
        return False

    
# Сохраняет ответы пользователя в таблицу useranswers
def save_user_answers(vk_user_id, answers, db_path):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Сначала получаем internal_user_id из таблицы users
        cursor.execute(
            'SELECT user_id FROM users WHERE vk_user_id = ?',
            (vk_user_id,)
        )
        result = cursor.fetchone()
        if not result:
            print(f"Пользователь с VK ID {vk_user_id} не найден в таблице users")
            return False
        internal_user_id = result[0]

        # Вставляем ответы в таблицу useranswers
        cursor.execute('''
            INSERT INTO useranswers
            (user_id, servings, meals_count, restrictions, cooking_time, budget, preferences, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            internal_user_id,
            answers.get('servings'),
            answers.get('meals_count'),
            answers.get('restrictions'),
            answers.get('cooking_time'),
            answers.get('budget'),
            answers.get('preferences'),
            datetime.now().isoformat()  # время завершения сбора ответов
        ))
        conn.commit()
        conn.close()
        print(f"Ответы пользователя {vk_user_id} успешно сохранены в БД")
        return True
    except sqlite3.Error as e:
        print(f"Ошибка при сохранении ответов в БД: {e}")
        return False

        
# Очистка текста   
def clean_response(text):
    text = re.sub(r'^#{2,}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    return text


# Настройки
GROUP_TOKEN = os.environ["VK_GROUP_TOKEN"]
GROUP_ID = int(os.environ["VK_GROUP_ID"])


# Словарь для хранения ответов пользователей (в реальном проекте используйте БД)
user_data = {}


# Вопросы и ключи для сохранения ответов
questions = [
    ("На сколько дней составить план питания (от 1 до 5 дней)?", "days"),
    ("На сколько человек нужно составить план питания?", "servings"),
    ("Сколько приёмов пищи в день вы планируете?", "meals_count"),
    ("Расскажите, пожалуйста, есть ли у вас аллергии или продукты, которые вы не едите (например: глютен, молоко и т.д.)?", "restrictions"),
    ("Сколько времени в среднем вы готовы тратить на приготовление одного блюда?", "cooking_time"),
    ("Какой у вас примерный бюджет на продукты: экономный, средний или без ограничений?", "budget"),
    ("Есть ли блюда или продукты, которые вы уже давно хотите попробовать?", "preferences")
]


# Создаём объект для работы с моделью 
load_dotenv()

YANDEX_CLOUD_API_KEY = os.getenv('YANDEX_CLOUD_API_KEY')
YANDEX_CLOUD_FOLDER = os.getenv('YANDEX_CLOUD_FOLDER')
YANDEX_CLOUD_MODEL = "aliceai-llm/latest"

model=f"gpt://{YANDEX_CLOUD_FOLDER}/{YANDEX_CLOUD_MODEL}"

llm = YandexGPT(
    iam_token = YANDEX_CLOUD_API_KEY,
    model_uri = model
)

def generate_meal_plan(answers, days=1):
    """Генерирует план питания с помощью LLM на основе собранных ответов"""
    prompt = (
        f"Составь план питания на {answers['days']} дней для {answers['servings']} человек, {answers['meals_count']} приёмов пищи в день. "
        f"Учитывай: ограничения — {answers['restrictions']}, макс. время на блюдо — {answers['cooking_time']} мин, бюджет — {answers['budget']}, предпочтения — {answers['preferences']}.\n\n"
        "Требования: баланс БЖУ, 2000–2500 ккал на взрослого в день.\n\n"
        "Формат по дням:\n"
        "📝 План питания\n"
        "\n"
        "🗓 День X\n"
        "🍳 Завтрак: [блюдо]."    
        "🍲 Обед: [блюдо 1]. [блюдо 2].\n"    
        "🥗 Ужин: [блюдо]."
        "Список покупок (по категориям: овощи, фрукты, мясо/рыба, молочное, бакалея, прочее):\n"
        "• [Продукт] — [количество с единицей, напр. 1.2 кг]\n\n"
        "Без пояснений, только план и список."
    )
    try:
        response_text = llm.invoke(prompt)  # это уже строка
        cleaned_response = clean_response(response_text)
        return cleaned_response
    except Exception as e:
        return f"Ошибка при генерации плана питания: {e}"

def edit_meal_plan(answers, current_plan, change_request):
    """Редактирует существующий план питания с учётом запроса пользователя"""
    prompt = (
        f"Есть текущий план питания:\n\n{current_plan}\n\n"
        f"Запрос на изменения: {change_request}.\n\n"
        f"Исходные параметры: {answers['meals_count']} приёмов пищи, {answers['servings']} человек, "
        f"ограничения — {answers['restrictions']}, макс. время — {answers['cooking_time']} мин, "
        f"бюджет — {answers['budget']}, предпочтения — {answers['preferences']}.\n\n"
        "Внеси только запрошенные изменения, остальные блюда не трогай. Сохрани структуру плана и формат вывода.\n"
        "Выдай только обновлённый план без пояснений.\n\n"
        "Формат:\n"
        "📝 План питания\n"
        "\n"
        "🗓 День X\n"
        "🍳 Завтрак: [блюдо]. "
        "🍲 Обед: [блюдо 1]. [блюдо 2].\n"
        "🥗 Ужин: [блюдо]."   
    )
    try:
        response_text = llm.invoke(prompt)
        cleaned_response = clean_response(response_text)        
        return cleaned_response
    except Exception as e:
        return f"Ошибка при редактировании плана питания: {e}"

def send_meal_plan_with_options(vk, user_id, meal_plan):
    """Отправляет план питания и варианты действий пользователю"""
    message = f"{meal_plan}\n\n---\nЧто хотите сделать дальше?\n- Напишите «изменить [что изменить]», чтобы внести правки (например: «изменить понедельник» или «убрать суп»).\n- Напишите «все хорошо», если всё устраивает.\n- Напишите «составить новое меню», чтобы начать заново."
    send_message(vk, user_id, message)

def ask_next_question(vk, user_id):
    """Отправляет следующий вопрос пользователю"""
    current_index = user_data[user_id]['current_question_index']
    question_text = questions[current_index][0]
    send_message(vk, user_id, question_text)

def send_message(vk, user_id, message):
    """Отправляет сообщение пользователю"""
    # Разбиваем длинное сообщение на части, если оно превышает лимит ВК (4096 символов)
    max_length = 4096
    if len(message) > max_length:
        for i in range(0, len(message), max_length):
            part = message[i:i + max_length]
            vk.messages.send(
                user_id=user_id,
                message=part,
                random_id=0
            )
    else:
        vk.messages.send(
            user_id=user_id,
            message=message,
            random_id=0  # random_id обязателен для защиты от повторной отправки
        )

def get_user_status(user_id, db_path):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT status FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None
    except sqlite3.Error as e:
        print(f"Ошибка при получении статуса пользователя: {e}")
        return None

# Меняет статус пользователя на 'no_free_day' в БД после завершения цикла free_day
def update_user_status_to_no_free_day(user_id, db_path):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Обновляем статус пользователя
        cursor.execute(
            'UPDATE users SET status = ? WHERE user_id = ? AND status = ?',
            ('no_free_day', user_id, 'free_day')
        )

        # Проверяем, была ли произведена какая‑либо модификация
        if cursor.rowcount == 0:
            print(f"Статус не изменён для пользователя {user_id}. Возможно, статус уже не 'free_day'.")
            conn.close()
            return False

        conn.commit()
        conn.close()
        print(f"Статус пользователя {user_id} успешно изменён на 'no_free_day'")
        return True

    except sqlite3.Error as e:
        print(f"Ошибка при обновлении статуса пользователя {user_id}: {e}")
        return False



def main():
    db_path = os.environ.get("DB_PATH", "bot.db")
    init_db(db_path)  # Гарантированно создаём/обновляем схему БД при старте
   
    vk_session = vk_api.VkApi(token=GROUP_TOKEN)
    longpoll = VkBotLongPoll(vk_session, GROUP_ID, wait=60)
    vk = vk_session.get_api()

    print("Бот запущен...")

    while True:
        try:
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    user_id = event.object.message['from_id']
                    message_text = event.object.message['text'].strip().lower()

                    # Проверяем подписку Donut (с использованием кэша)
                    is_subscriber = check_donut_subscription(vk, user_id, GROUP_ID)
                    
                    if is_subscriber:
                        if user_id not in user_data:
                            internal_user_id = str(user_id)  # генерируем user_id на основе VK ID
                            save_user_mapping(user_id, internal_user_id, db_path)  # сохраняем в БД
    
                            user_data[user_id] = {
                                'current_question_index': 0,
                                'answers': {},
                                'state': 'collecting_answers',  # состояния: collecting_answers, awaiting_edit, ready_for_new
                                'last_meal_plan': None
                            }
                            # Приветственное сообщение
                            welcome_message = (
                                "Давайте составим план питания для вас. Для этого ответьте, пожалуйста, на несколько вопросов."
                            )
                            send_message(vk, user_id, welcome_message)
                            ask_next_question(vk, user_id)
                            continue
    
                        state = user_data[user_id]['state']
    
                        if state == 'collecting_answers':
                            current_index = user_data[user_id]['current_question_index']
                            answers = user_data[user_id]['answers']
    
                            # Сохраняем ответ на текущий вопрос
                            question_key = questions[current_index][1]
                            answers[question_key] = message_text
    
                            # Переходим к следующему вопросу
                            current_index += 1
                            user_data[user_id]['current_question_index'] = current_index
    
                            if current_index < len(questions):
                                ask_next_question(vk, user_id)
                            else:
                                # Все вопросы заданы — генерируем план питания (в любом случае)
                                final_message = "Спасибо за ответы! Сейчас сформирую для вас меню с учётом всех пожеланий. Подождите, пожалуйста, пару минут…"
                                send_message(vk, user_id, final_message)
    
                                # Пытаемся сохранить ответы в БД 
                                save_success = save_user_answers(user_id, answers, db_path)
                                if not save_success:
                                    print(f"Предупреждение: не удалось сохранить ответы пользователя {user_id} в БД")
                             
                                meal_plan = generate_meal_plan(answers)
                                user_data[user_id]['last_meal_plan'] = meal_plan
                                send_meal_plan_with_options(vk, user_id, meal_plan)
                                user_data[user_id]['state'] = 'awaiting_edit'
    
                        elif state == 'awaiting_edit':
                            if message_text in ['все хорошо', 'ок', 'готово', 'хорошо', 'нормально']:
                                send_message(vk, user_id, "Отлично! Если захотите составить новое меню - просто напишите «меню», и я помогу.")
                                user_data[user_id]['state'] = 'ready_for_new'
                            elif message_text.startswith('изменить '):
                                change_request = message_text[len('изменить '):].strip()
                                updated_plan = edit_meal_plan(user_data[user_id]['answers'], user_data[user_id]['last_meal_plan'], change_request)
                                user_data[user_id]['last_meal_plan'] = updated_plan
                                send_meal_plan_with_options(vk, user_id, updated_plan)
                            elif message_text == 'составить новое меню':
                                user_data[user_id] = {
                                    'current_question_index': 0,
                                    'answers': {},
                                    'state': 'collecting_answers',
                                    'last_meal_plan': None
                                }
                                welcome_message = (
                                    "Отлично! Давайте составим новое меню. Ответьте, пожалуйста, на несколько вопросов."
                                )
                                send_message(vk, user_id, welcome_message)
                                ask_next_question(vk, user_id)
                            else:
                                send_message(vk, user_id, "Пожалуйста, укажите, что именно нужно изменить в меню, либо напишите «все хорошо», если всё устраивает.")
    
                        elif state == 'ready_for_new':
                            if message_text in ['меню', 'составить меню', 'новое меню', 'составить новое меню']:
                                user_data[user_id] = {
                                    'current_question_index': 0,
                                    'answers': {},
                                    'state': 'collecting_answers',
                                    'last_meal_plan': None
                                }
                                welcome_message = (
                                    "Давайте составим для вас новое меню. Ответьте, пожалуйста, на несколько вопросов."
                                )
                                send_message(vk, user_id, welcome_message)
                                ask_next_question(vk, user_id)
                            else:
                                send_message(vk, user_id, "Чтобы составить новое меню, напишите «меню». Если у вас есть другой вопрос — расскажите, чем ещё могу помочь.")



                    
                    elif not is_subscriber:
                        user_status = get_user_status(user_id, db_path)
                        print(f"Статус пользователя {user_id}: {user_status}")
                        
                        if user_status == 'free_day':
                            if user_id not in user_data:
                                internal_user_id = str(user_id)  # генерируем user_id на основе VK ID
                                save_user_mapping(user_id, internal_user_id, db_path)  # сохраняем в БД
        
                                user_data[user_id] = {
                                    'current_question_index': 1,
                                    'answers': {'days': 1},  # days = 1 для free_day
                                    'state': 'collecting_answers',  # состояния: collecting_answers, awaiting_edit, ready_for_new
                                    'last_meal_plan': None
                                }
                                # Приветственное сообщение
                                welcome_message = (
                                    "Давайте составим план питания для вас. Для этого ответьте, пожалуйста, на несколько вопросов."
                                )
                                send_message(vk, user_id, welcome_message)
                                ask_next_question(vk, user_id)
                                continue
            
                            state = user_data[user_id]['state']
                
                            if state == 'collecting_answers':
                                current_index = user_data[user_id]['current_question_index']
                                answers = user_data[user_id]['answers']
                
                                # Сохраняем ответ на текущий вопрос
                                question_key = questions[current_index][1]
                                answers[question_key] = message_text
                
                                # Переходим к следующему вопросу
                                current_index += 1
                                user_data[user_id]['current_question_index'] = current_index
                
                                if current_index < len(questions):
                                    ask_next_question(vk, user_id)
                                else:
                                    # Все вопросы заданы — генерируем план питания (в любом случае)
                                    final_message = "Спасибо за ответы! Сейчас сформирую для вас меню с учётом всех пожеланий. Подождите, пожалуйста, пару минут…"
                                    send_message(vk, user_id, final_message)
                
                                    # Пытаемся сохранить ответы в БД 
                                    save_success = save_user_answers(user_id, answers, db_path)
                                    if not save_success:
                                        print(f"Предупреждение: не удалось сохранить ответы пользователя {user_id} в БД")
                                         
                                    meal_plan = generate_meal_plan(answers)
                                    user_data[user_id]['last_meal_plan'] = meal_plan
                                    send_meal_plan_with_options(vk, user_id, meal_plan)
                                    user_data[user_id]['state'] = 'awaiting_edit'

                                     # Если пользователь был на free_day, меняем статус после завершения цикла
                                    if user_status == 'free_day':
                                        update_success = update_user_status_to_no_free_day(user_id, db_path)
                                        if update_success:
                                            print(f"Пользователь {user_id} завершил бесплатный период, статус изменён.")
                                        else:
                                            print(f"Не удалось обновить статус для пользователя {user_id}")
                      

                        elif user_status == 'no_free_day':
                            donation_message = (
                                "Для использования бота необходима подписка VK Donut.\n\n"
                                "[https://vk.ru/tasty__stories?analytics_screen=group&levelId=3340&source=donut_banner&w=donut_payment-234208989|Оформить подписку]\n\n"  # замените на ID вашей группы
                                "После оформления напишите любое сообщение снова."
                            )
                            send_message(vk, user_id, donation_message)
                            continue  # прерываем выполнение, так как доступ запрещён

        except ReadTimeout:
            print("Таймаут соединения. Повторяю попытку через 5 сек...")
            time.sleep(5)
            continue
        except ConnectionError as e:
            print(f"Ошибка соединения: {e}. Повторяю через 10 сек...")
            time.sleep(10)
            continue
        except Exception as e:
            print(f"Непредвиденная ошибка: {e}")
            time.sleep(10)
            continue

if __name__ == '__main__':
    main()
