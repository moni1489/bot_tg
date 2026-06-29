import asyncio
import logging
import os
import random
import string
import asyncpg
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, 
    KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiohttp import web

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в файле .env")
if not DATABASE_URL:
    raise ValueError("Не найден DATABASE_URL в файле .env (нужна ссылка на PostgreSQL)")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

pool = None

# --- WEB SERVER (Для Hugging Face и cron-job) ---
async def health_check(request):
    return web.Response(text="Bot is alive!")

async def start_webserver():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 7860)
    await site.start()
    logging.info("🌐 Веб-сервер запущен на порту 7860 (Hugging Face health check)")

# --- FSM ---
class CreateOrder(StatesGroup):
    waiting_for_client_id = State()
    waiting_for_items = State()
    waiting_for_total_price = State()
    waiting_for_paid_amount = State()
    waiting_for_photo = State()

class CheckStatus(StatesGroup):
    waiting_for_id = State()
    waiting_for_password = State()

class CheckArchive(StatesGroup):
    waiting_for_id = State()
    waiting_for_password = State()

class UpdatePayment(StatesGroup):
    waiting_for_new_paid = State()

# --- DATABASE ---
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                user_tg_id BIGINT,
                login_id TEXT UNIQUE,
                password TEXT,
                role TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                password TEXT,
                user_tg_id BIGINT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                client_id INTEGER REFERENCES clients(id),
                items TEXT,
                total_price INTEGER,
                paid_amount INTEGER,
                status TEXT,
                photo_id TEXT,
                archived BOOLEAN DEFAULT FALSE
            )
        """)
        
        # Admin check
        admin = await db.fetchrow("SELECT id FROM users WHERE role = 'admin'")
        if not admin:
            try:
                await db.execute(
                    "INSERT INTO users (login_id, password, role) VALUES ($1, $2, $3)",
                    "admin", "admin123", "admin"
                )
            except asyncpg.exceptions.UniqueViolationError:
                pass

async def is_admin(user_tg_id: int) -> bool:
    if user_tg_id in ADMIN_IDS:
        return True
    async with pool.acquire() as db:
        row = await db.fetchrow("SELECT id FROM users WHERE user_tg_id = $1 AND role = 'admin'", user_tg_id)
        return row is not None

async def authenticate_admin(login_id: str, password: str, user_tg_id: int) -> bool:
    async with pool.acquire() as db:
        admin = await db.fetchrow("SELECT id FROM users WHERE login_id = $1 AND password = $2 AND role = 'admin'", login_id, password)
        if admin:
            await db.execute("UPDATE users SET user_tg_id = $1 WHERE id = $2", user_tg_id, admin['id'])
            return True
        return False

async def create_client_db(password: str) -> int:
    async with pool.acquire() as db:
        return await db.fetchval("INSERT INTO clients (password) VALUES ($1) RETURNING id", password)

async def check_client(client_id: int) -> bool:
    async with pool.acquire() as db:
        row = await db.fetchrow("SELECT id FROM clients WHERE id = $1", client_id)
        return row is not None

async def create_order(client_id: int, items: str, total_price: int, paid_amount: int, photo_id: str) -> int:
    async with pool.acquire() as db:
        return await db.fetchval(
            "INSERT INTO orders (client_id, items, total_price, paid_amount, status, photo_id, archived) VALUES ($1, $2, $3, $4, $5, $6, FALSE) RETURNING id",
            client_id, items, total_price, paid_amount, "Заказ принят в обработку", photo_id
        )

async def get_all_orders():
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT id, items, status FROM orders WHERE archived = FALSE")
        return [tuple(r) for r in rows]

async def get_archived_orders():
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT id, items, status FROM orders WHERE archived = TRUE")
        return [tuple(r) for r in rows]

async def get_all_clients():
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT id, password FROM clients")
        return [tuple(r) for r in rows]

async def get_client_orders(client_id: int, password: str):
    async with pool.acquire() as db:
        user = await db.fetchrow("SELECT id FROM clients WHERE id = $1 AND password = $2", client_id, password)
        if not user:
            return None
        rows = await db.fetch("SELECT id, items, total_price, paid_amount, status, photo_id FROM orders WHERE client_id = $1 AND archived = FALSE", client_id)
        return [tuple(r) for r in rows]

async def get_client_archived_orders(client_id: int, password: str):
    async with pool.acquire() as db:
        user = await db.fetchrow("SELECT id FROM clients WHERE id = $1 AND password = $2", client_id, password)
        if not user:
            return None
        rows = await db.fetch("SELECT id, items, total_price, paid_amount, status, photo_id FROM orders WHERE client_id = $1 AND archived = TRUE", client_id)
        return [tuple(r) for r in rows]

async def update_order_status(order_id: int, new_status: str):
    async with pool.acquire() as db:
        archived = (new_status == "Выдано")
        await db.execute("UPDATE orders SET status = $1, archived = $2 WHERE id = $3", new_status, archived, order_id)

async def update_order_payment(order_id: int, paid_amount: int):
    async with pool.acquire() as db:
        await db.execute("UPDATE orders SET paid_amount = $1 WHERE id = $2", paid_amount, order_id)

async def bind_client_tg_id(client_id: int, user_tg_id: int):
    async with pool.acquire() as db:
        await db.execute("UPDATE clients SET user_tg_id = $1 WHERE id = $2", user_tg_id, client_id)

async def get_client_tg_id_by_order(order_id: int):
    async with pool.acquire() as db:
        res = await db.fetchrow("""
            SELECT c.user_tg_id 
            FROM orders o
            JOIN clients c ON o.client_id = c.id
            WHERE o.id = $1
        """, order_id)
        if res and res['user_tg_id']:
            return res['user_tg_id']
        return None

async def delete_order_db(order_id: int):
    async with pool.acquire() as db:
        await db.execute("DELETE FROM orders WHERE id = $1", order_id)

async def unarchive_order_db(order_id: int):
    async with pool.acquire() as db:
        await db.execute("UPDATE orders SET archived = FALSE WHERE id = $1", order_id)

# --- KEYBOARDS ---
def get_start_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Отследить заказы")],
            [KeyboardButton(text="🗃 Архив заказов")]
        ],
        resize_keyboard=True
    )

def get_admin_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Список клиентов")],
            [KeyboardButton(text="👤 Создать клиента"), KeyboardButton(text="➕ Добавить заказ")],
            [KeyboardButton(text="🔄 Изменить статус заказа")],
            [KeyboardButton(text="💰 Изменить оплату по заказу")],
            [KeyboardButton(text="🗃 Архив заказов (Админ)")]
        ],
        resize_keyboard=True
    )

def get_orders_kb(orders, action="status"):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for order in orders:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"Заказ #{order[0]} - {order[1][:15]}", callback_data=f"{action}_{order[0]}")
        ])
    return kb

STATUSES = [
    "Заказ принят в обработку",
    "Заказ ожидает отправки из магазина",
    "Заказ едет на склад США",
    "Заказ начал сортировку на складе США",
    "Заказ отправлен из США на наш склад в Россию",
    "Заказ проходит таможенное оформление",
    "Заказ прибыл в магазин и готов к выдаче",
    "Выдано"
]

def get_status_kb(order_id):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for i, status in enumerate(STATUSES):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=status, callback_data=f"setstatus_{order_id}_{i}")
        ])
    return kb

def get_admin_archive_kb(order_id):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Восстановить (Убрать из архива)", callback_data=f"unarchive_{order_id}")],
        [InlineKeyboardButton(text="🗑 Удалить навсегда", callback_data=f"delete_{order_id}")]
    ])
    return kb

def get_skip_photo_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить фото")]],
        resize_keyboard=True
    )

def generate_random_password(length=6):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

# --- HANDLERS ---
@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    if await is_admin(message.from_user.id):
        await message.answer("Добро пожаловать в панель администратора!", reply_markup=get_admin_kb())
    else:
        await message.answer("Добро пожаловать в Личный Кабинет! Выберите действие ниже.", reply_markup=get_start_kb())

@router.message(Command("admin_login"))
async def admin_login_start(message: Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    if len(args) == 3:
        login, password = args[1], args[2]
        if await authenticate_admin(login, password, message.from_user.id):
            await message.answer("Авторизация успешна. Вы добавлены как администратор.", reply_markup=get_admin_kb())
        else:
            await message.answer("Неверный логин или пароль администратора.")
    else:
        await message.answer("Использование: /admin_login [логин] [пароль]")

@router.message(Command("add_admin"))
async def add_new_admin(message: Message):
    if not await is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) == 3:
        new_login, new_pass = args[1], args[2]
        async with pool.acquire() as db:
            try:
                await db.execute("INSERT INTO users (login_id, password, role) VALUES ($1, $2, $3)", new_login, new_pass, "admin")
                await message.answer(f"✅ Новый админ успешно создан!\nЛогин: `{new_login}`\nПароль: `{new_pass}`\n\nПередайте эти данные вашему партнеру, чтобы он отправил команду:\n`/admin_login {new_login} {new_pass}`", parse_mode="Markdown")
            except asyncpg.exceptions.UniqueViolationError:
                await message.answer("❌ Админ с таким логином уже существует!")
    else:
        await message.answer("Использование: `/add_admin [новый_логин] [новый_пароль]`", parse_mode="Markdown")

# --- ADMIN: CREATE CLIENT ---
@router.message(F.text == "👤 Создать клиента")
async def add_client_start(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
        
    password = generate_random_password(6)
    client_id = await create_client_db(password)
    
    await message.answer(
        f"✅ Клиент успешно создан!\n\n"
        f"🆔 **Номер клиента:** `{client_id}`\n"
        f"🔑 **Пароль:** `{password}`\n\n"
        f"Теперь вы можете добавлять заказы на этот Номер клиента.",
        parse_mode="Markdown", reply_markup=get_admin_kb()
    )
    await state.clear()

# --- ADMIN: LIST CLIENTS ---
@router.message(F.text == "👥 Список клиентов")
async def list_clients(message: Message):
    if not await is_admin(message.from_user.id):
        return
    clients = await get_all_clients()
    if not clients:
        await message.answer("Клиентов пока нет.")
        return
        
    response = "👥 **Список всех клиентов:**\n\n"
    for client in clients:
        response += f"🆔 ID: `{client[0]}` | 🔑 Пароль: `{client[1]}`\n"
        
    await message.answer(response, parse_mode="Markdown")

# --- ADMIN: CREATE ORDER ---
@router.message(F.text == "➕ Добавить заказ")
async def add_order_start(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Введите Номер клиента (ID), к которому нужно привязать заказ:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CreateOrder.waiting_for_client_id)

@router.message(CreateOrder.waiting_for_client_id)
async def add_order_client_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID клиента должен быть числом.")
        return
    client_id = int(message.text)
    
    if not await check_client(client_id):
        await message.answer("❌ Клиент с таким ID не найден. Сначала создайте клиента.", reply_markup=get_admin_kb())
        await state.clear()
        return
        
    await state.update_data(client_id=client_id)
    await message.answer("Введите позиции заказа (что купили):")
    await state.set_state(CreateOrder.waiting_for_items)

@router.message(CreateOrder.waiting_for_items)
async def add_order_items(message: Message, state: FSMContext):
    await state.update_data(items=message.text)
    await message.answer("Введите общую стоимость заказа (число):")
    await state.set_state(CreateOrder.waiting_for_total_price)

@router.message(CreateOrder.waiting_for_total_price)
async def add_order_total(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число.")
        return
    await state.update_data(total_price=int(message.text))
    await message.answer("Сколько клиент уже оплатил? (число):")
    await state.set_state(CreateOrder.waiting_for_paid_amount)

@router.message(CreateOrder.waiting_for_paid_amount)
async def add_order_paid(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число.")
        return
    await state.update_data(paid_amount=int(message.text))
    await message.answer("Пришлите фото заказа (или нажмите кнопку 'Пропустить фото'):", reply_markup=get_skip_photo_kb())
    await state.set_state(CreateOrder.waiting_for_photo)

@router.message(CreateOrder.waiting_for_photo)
async def add_order_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.text != "Пропустить фото":
        await message.answer("Пожалуйста, пришлите фото или нажмите 'Пропустить фото'.")
        return
    
    order_id = await create_order(
        data['client_id'], 
        data['items'], 
        data['total_price'], 
        data['paid_amount'], 
        photo_id
    )
    
    msg = f"✅ Заказ успешно добавлен клиенту #{data['client_id']}!\n\n" \
          f"🆔 Номер заказа: {order_id}\n" \
          f"🛒 Позиции: {data['items']}\n" \
          f"💰 Стоимость: {data['total_price']} | Оплачено: {data['paid_amount']}"
          
    await message.answer(msg, reply_markup=get_admin_kb())
    await state.clear()
    
    # Notify user via bot if tg id is linked
    client_tg_id = await get_client_tg_id_by_order(order_id)
    if client_tg_id:
        try:
            notify_msg = f"🎉 **У вас новый заказ!**\n\n🆔 Заказ #{order_id}\n🛒 Позиции:\n{data['items']}\n\n💰 Стоимость: {data['total_price']}\n✅ Оплачено: {data['paid_amount']}"
            if photo_id:
                await bot.send_photo(chat_id=client_tg_id, photo=photo_id, caption=notify_msg, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=client_tg_id, text=notify_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя о новом заказе: {e}")

# --- ADMIN: UPDATE STATUS ---
@router.message(F.text == "🔄 Изменить статус заказа")
async def change_status_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    orders = await get_all_orders()
    if not orders:
        await message.answer("Нет активных заказов.")
        return
    await message.answer("Выберите заказ для изменения статуса (Архивные здесь не отображаются):", reply_markup=get_orders_kb(orders, "status"))

@router.callback_query(F.data.startswith("status_"))
async def select_order_for_status(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await callback.message.edit_text(f"Выберите новый статус для заказа #{order_id}:", reply_markup=get_status_kb(order_id))

@router.callback_query(F.data.startswith("setstatus_"))
async def set_order_status(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    parts = callback.data.split("_")
    order_id = int(parts[1])
    status_idx = int(parts[2])
    new_status = STATUSES[status_idx]
    
    await update_order_status(order_id, new_status)
    
    if new_status == "Выдано":
        await callback.message.edit_text(f"✅ Статус заказа #{order_id} изменен на:\n'{new_status}'.\n\n🗃 Заказ автоматически перемещен в Архив.")
    else:
        await callback.message.edit_text(f"✅ Статус заказа #{order_id} изменен на:\n'{new_status}'.")
        
    await callback.answer("Статус обновлен")
    
    client_tg_id = await get_client_tg_id_by_order(order_id)
    if client_tg_id:
        try:
            await bot.send_message(client_tg_id, f"🔔 **Обновление по заказу #{order_id}**\n\nНовый статус: _{new_status}_", parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление клиенту {client_tg_id}: {e}")

# --- ADMIN: UPDATE PAYMENT ---
@router.message(F.text == "💰 Изменить оплату по заказу")
async def change_payment_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    orders = await get_all_orders()
    if not orders:
        await message.answer("Нет активных заказов.")
        return
    await message.answer("Выберите заказ:", reply_markup=get_orders_kb(orders, "pay"))

@router.callback_query(F.data.startswith("pay_"))
async def select_order_for_payment(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await state.update_data(pay_order_id=order_id)
    await callback.message.answer(f"Введите новую сумму, которую клиент УЖЕ оплатил по заказу #{order_id}:")
    await state.set_state(UpdatePayment.waiting_for_new_paid)
    await callback.answer()

@router.message(UpdatePayment.waiting_for_new_paid)
async def update_payment_value(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число.")
        return
    data = await state.get_data()
    order_id = data['pay_order_id']
    new_paid = int(message.text)
    
    await update_order_payment(order_id, new_paid)
    await message.answer(f"✅ Сумма оплаты по заказу #{order_id} обновлена до {new_paid}.", reply_markup=get_admin_kb())
    await state.clear()

# --- ADMIN: ARCHIVE LIST ---
@router.message(F.text == "🗃 Архив заказов (Админ)")
async def admin_archive_list(message: Message):
    if not await is_admin(message.from_user.id):
        return
    
    archived_orders = await get_archived_orders()
    if not archived_orders:
        await message.answer("Архив пуст.")
        return
        
    await message.answer("🗃 Выберите архивный заказ для действий:", reply_markup=get_orders_kb(archived_orders, "archiveadmin"))

@router.callback_query(F.data.startswith("archiveadmin_"))
async def select_archived_order(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await callback.message.edit_text(
        f"🗃 **Архивный заказ #{order_id}**\n\nВыберите действие:",
        reply_markup=get_admin_archive_kb(order_id),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("unarchive_"))
async def action_unarchive_order(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await unarchive_order_db(order_id)
    await callback.message.edit_text(f"✅ Заказ #{order_id} успешно восстановлен из архива.")
    await callback.answer("Восстановлено")

@router.callback_query(F.data.startswith("delete_"))
async def action_delete_order(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    order_id = int(callback.data.split("_")[1])
    await delete_order_db(order_id)
    await callback.message.edit_text(f"🗑 Заказ #{order_id} был окончательно удален из базы.")
    await callback.answer("Удалено")

# --- CLIENT INTERFACE ---
@router.message(F.text == "📦 Отследить заказы")
async def check_status_start(message: Message, state: FSMContext):
    await message.answer("Введите ваш Номер клиента (ID):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CheckStatus.waiting_for_id)

@router.message(CheckStatus.waiting_for_id)
async def check_status_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Номер клиента должен быть числом.")
        return
    await state.update_data(client_id=int(message.text))
    await message.answer("Введите ваш Пароль:")
    await state.set_state(CheckStatus.waiting_for_password)

@router.message(CheckStatus.waiting_for_password)
async def check_status_password(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data['client_id']
    password = message.text
    
    orders = await get_client_orders(client_id, password)
    
    if orders is None:
        await message.answer("❌ Ошибка: Неверный ID клиента или пароль.", reply_markup=get_start_kb())
    elif len(orders) == 0:
        await bind_client_tg_id(client_id, message.from_user.id)
        await message.answer(f"Привет! Вы вошли в личный кабинет (ID: {client_id}).\n\nУ вас пока нет активных заказов.", reply_markup=get_start_kb())
    else:
        await bind_client_tg_id(client_id, message.from_user.id)
        await message.answer(f"✅ **Личный кабинет #{client_id}**\n\nАктивных заказов: {len(orders)}", parse_mode="Markdown")
        for order in orders:
            order_id, items, total_price, paid_amount, status, photo_id = order
            debt = total_price - paid_amount
            
            response = f"📦 **Заказ #{order_id}**\n\n"
            response += f"🛒 **Позиции:**\n{items}\n\n"
            response += f"💵 **Общая стоимость:** {total_price}\n"
            response += f"✅ **Оплачено:** {paid_amount}\n"
            response += f"❗️ **Осталось доплатить:** {debt if debt > 0 else 0}\n\n"
            response += f"🚚 **Текущий статус:**\n_{status}_"
            
            if photo_id:
                await message.answer_photo(photo=photo_id, caption=response, parse_mode="Markdown")
            else:
                await message.answer(response, parse_mode="Markdown")
                
        await message.answer("Все активные заказы загружены.", reply_markup=get_start_kb())
            
    await state.clear()

# CLIENT: ARCHIVE
@router.message(F.text == "🗃 Архив заказов")
async def check_archive_start(message: Message, state: FSMContext):
    await message.answer("Введите ваш Номер клиента (ID) для доступа к Архиву:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CheckArchive.waiting_for_id)

@router.message(CheckArchive.waiting_for_id)
async def check_archive_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Номер клиента должен быть числом.")
        return
    await state.update_data(client_id=int(message.text))
    await message.answer("Введите ваш Пароль:")
    await state.set_state(CheckArchive.waiting_for_password)

@router.message(CheckArchive.waiting_for_password)
async def check_archive_password(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data['client_id']
    password = message.text
    
    orders = await get_client_archived_orders(client_id, password)
    
    if orders is None:
        await message.answer("❌ Ошибка: Неверный ID клиента или пароль.", reply_markup=get_start_kb())
    elif len(orders) == 0:
        await message.answer(f"🗃 Ваш архив заказов пуст.", reply_markup=get_start_kb())
    else:
        await message.answer(f"🗃 **Архив заказов #{client_id}**\n\nВыданных заказов: {len(orders)}", parse_mode="Markdown")
        for order in orders:
            order_id, items, total_price, paid_amount, status, photo_id = order
            
            response = f"📦 **Архивный заказ #{order_id}**\n\n"
            response += f"🛒 **Позиции:**\n{items}\n\n"
            response += f"💵 **Общая стоимость:** {total_price}\n"
            response += f"✅ **Оплачено:** {paid_amount}\n\n"
            response += f"🚚 **Финальный статус:**\n_{status}_"
            
            if photo_id:
                await message.answer_photo(photo=photo_id, caption=response, parse_mode="Markdown")
            else:
                await message.answer(response, parse_mode="Markdown")
                
        await message.answer("Все архивные заказы загружены.", reply_markup=get_start_kb())
            
    await state.clear()

# --- MAIN ---
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    await init_db()
    
    await start_webserver()
    
    logging.info("Бот запущен. Ожидание сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
