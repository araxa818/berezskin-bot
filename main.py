import logging
import asyncio
import json
import os
from datetime import datetime, timedelta, time

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback

import config 

# --- СОСТОЯНИЯ ---
class BookingForm(StatesGroup):
    waiting_for_service = State()
    waiting_for_master = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_name = State()
    waiting_for_phone = State()
    confirm_booking = State()

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=config.LOCAL_TZ)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/calendar"]
creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
gc = gspread.authorize(creds)
sheet = gc.open(config.GOOGLE_SHEET_NAME).sheet1
cal_service = build('calendar', 'v3', credentials=creds)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def load_data():
    if not os.path.exists(config.DATA_FILE): return {}
    with open(config.DATA_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def save_data(data):
    with open(config.DATA_FILE, 'w', encoding='utf-8') as f: 
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_free_slots(target_date):
    now = datetime.now(config.LOCAL_TZ)
    start_of_day = config.LOCAL_TZ.localize(datetime.combine(target_date, time(10, 0)))
    end_of_day = config.LOCAL_TZ.localize(datetime.combine(target_date, time(20, 0)))
    res = cal_service.events().list(calendarId=config.SINGLE_CALENDAR_ID, timeMin=start_of_day.isoformat(), 
                                    timeMax=end_of_day.isoformat(), singleEvents=True).execute()
    busy = [(datetime.fromisoformat(e['start']['dateTime']).astimezone(config.LOCAL_TZ), 
             datetime.fromisoformat(e['end']['dateTime']).astimezone(config.LOCAL_TZ)) 
            for e in res.get('items', []) if 'dateTime' in e['start']]
    free = []
    curr = start_of_day
    while curr + timedelta(minutes=60) <= end_of_day:
        if not any(not (curr + timedelta(minutes=60) <= b[0] or curr >= b[1]) for b in busy) and curr > now:
            free.append(curr)
        curr += timedelta(minutes=30)
    return free

# --- HANDLERS ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    data = load_data(); uid = str(message.from_user.id)
    if uid not in data: data[uid] = 0; save_data(data)
    b = InlineKeyboardBuilder()
    b.button(text="📅 Записаться", callback_data="book_start")
    b.button(text=f"🎁 Баллы: {data[uid]}₽", callback_data="pts_info"); b.adjust(1)
    await message.answer("✨ Студия BERЁZSKIN: Мир красоты Montibello", reply_markup=b.as_markup())

@dp.callback_query(F.data == "pts_info")
async def show_loyalty_info(c: types.CallbackQuery):
    data = load_data()
    uid = str(c.from_user.id)
    balance = data.get(uid, 0)
    
    # Текст сообщения: Баланс + Правила из конфига
    text = f"💰 Ваш баланс: **{balance}₽**\n\n{config.LOYALTY_TEXT}"
    
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад в меню", callback_data="back_to_main")
    
    await c.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")

# Хендлер для возврата в главное меню
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(c: types.CallbackQuery, state: FSMContext):
    await cmd_start(c.message, state)

@dp.callback_query(F.data == "book_start")
async def book_start_direct(c: types.CallbackQuery, state: FSMContext):
    # Мы пропускаем выбор категории и сразу жестко задаем 'face'
    cat_id = "face"
    await state.update_data(category=cat_id)
    
    b = InlineKeyboardBuilder()
    # Берем услуги только из категории face
    for sid, srv in config.SERVICES[cat_id].items(): 
        b.button(text=f"{srv['name']} ({srv['price']}₽)", callback_data=f"srv_{sid}")
    
    b.adjust(1)
    # Кнопка назад теперь ведет в самое начало (к кнопке "Записаться / Баллы")
    b.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main"))
    
    await c.message.edit_text("Выберите процедуру по уходу за лицом:", reply_markup=b.as_markup())
    await state.set_state(BookingForm.waiting_for_service)

@dp.callback_query(F.data.startswith("cat_"))
async def book_srv(c: types.CallbackQuery, state: FSMContext):
    cat_id = c.data.replace("cat_", "")
    await state.update_data(category=cat_id)
    b = InlineKeyboardBuilder()
    for sid, srv in config.SERVICES[cat_id].items(): 
        b.button(text=f"{srv['name']} ({srv['price']}₽)", callback_data=f"srv_{sid}")
    b.adjust(1).row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data="book_start"))
    await c.message.edit_text("Выберите процедуру:", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("srv_"))
async def book_mst(c: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    srv_id = c.data.replace("srv_", "")
    cat_id = d.get('category')
    
    if not cat_id or srv_id not in config.SERVICES[cat_id]:
        return await c.answer("Ошибка: выберите категорию заново.", show_alert=True)
    
    srv = config.SERVICES[cat_id][srv_id]
    await state.update_data(service=srv['name'], price=srv['price'], duration=srv['duration'], current_srv_id=srv_id)
    
    txt = f"**{srv['name']}**\n\n{srv['desc']}\n🕒 {srv['duration']} мин | 💰 {srv['price']}₽"
    b = InlineKeyboardBuilder()
    for k, v in config.MASTERS.items(): b.button(text=v, callback_data=f"mst_{k}")
    b.adjust(2).row(types.InlineKeyboardButton(text="⬅️ Назад к услугам", callback_data=f"cat_{cat_id}"))
    
    await c.message.edit_text(txt, reply_markup=b.as_markup(), parse_mode="Markdown")
    await state.set_state(BookingForm.waiting_for_master)

@dp.callback_query(F.data.startswith("mst_"))
async def book_date(c: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(master_name=config.MASTERS[c.data.replace("mst_", "")])
    b = InlineKeyboardBuilder().button(text="⬅️ Назад к мастерам", callback_data=f"srv_{d['current_srv_id']}")
    await c.message.answer("Выберите дату:", reply_markup=await SimpleCalendar().start_calendar())
    await c.message.answer("Или вернитесь назад:", reply_markup=b.as_markup())
    await state.set_state(BookingForm.waiting_for_date)

@dp.callback_query(SimpleCalendarCallback.filter())
async def book_time(c: types.CallbackQuery, callback_data: dict, state: FSMContext):
    sel, date = await SimpleCalendar().process_selection(c, callback_data)
    if sel:
        await state.update_data(date=date.strftime("%Y-%m-%d"))
        slots = get_free_slots(date.date())
        if not slots: return await c.message.answer("Свободных мест нет. Выберите другой день.")
        b = InlineKeyboardBuilder()
        for s in slots: b.button(text=s.strftime("%H:%M"), callback_data=f"tm_{s.strftime('%H:%M')}")
        b.adjust(4).row(types.InlineKeyboardButton(text="⬅️ Изменить дату", callback_data="back_to_cal"))
        await c.message.answer("Выберите время:", reply_markup=b.as_markup())
        await state.set_state(BookingForm.waiting_for_time)

@dp.callback_query(F.data == "back_to_cal")
async def back_to_cal(c: types.CallbackQuery, state: FSMContext):
    await c.message.answer("Выберите дату:", reply_markup=await SimpleCalendar().start_calendar())

@dp.callback_query(F.data.startswith("tm_"))
async def book_name(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(time=c.data.replace("tm_", ""))
    await c.message.answer("Как вас зовут?")
    await state.set_state(BookingForm.waiting_for_name)

@dp.message(BookingForm.waiting_for_name)
async def book_phone(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text); await m.answer("Ваш телефон?")
    await state.set_state(BookingForm.waiting_for_phone)

@dp.message(BookingForm.waiting_for_phone)
async def book_confirm(m: types.Message, state: FSMContext):
    await state.update_data(phone=m.text); d = await state.get_data()
    b = InlineKeyboardBuilder().button(text="✅ Подтвердить", callback_data="confirm_final")
    b.button(text="❌ Начать заново", callback_data="book_start")
    await m.answer(f"🌸 {d['service']}\n👩‍🎨 Мастер: {d['master_name']}\n📅 {d['date']} в {d['time']}", reply_markup=b.as_markup())
    await state.set_state(BookingForm.confirm_booking)

@dp.callback_query(F.data == "confirm_final", BookingForm.confirm_booking)
async def finalize(c: types.CallbackQuery, state: FSMContext):
    d = await state.get_data(); user_id = c.from_user.id
    st = config.LOCAL_TZ.localize(datetime.strptime(f"{d['date']} {d['time']}", "%Y-%m-%d %H:%M"))
    en = st + timedelta(minutes=d['duration'])
    ev = {'summary': f"{d['master_name']} | {d['service']}", 'description': f"Клиент: {d['name']}\nID:{user_id}", 'start': {'dateTime': st.isoformat()}, 'end': {'dateTime': en.isoformat()}}
    cal_service.events().insert(calendarId=config.SINGLE_CALENDAR_ID, body=ev).execute()
    sheet.append_row([datetime.now(config.LOCAL_TZ).strftime("%d.%m %H:%M"), d['name'], d['phone'], d['service'], f"{d['date']} {d['time']}", "Ожидает"])
    await bot.send_message(config.REAL_ADMIN_GROUP, f"🔔 ЗАПИСЬ: {d['name']} - {d['service']} - {d['date']} {d['time']}")
    await c.message.edit_text("✨ Вы успешно записаны!"); await state.clear()

@dp.message(Command("my_bookings"))
async def my_bookings(message: types.Message):
    uid = str(message.from_user.id); now = datetime.now(config.LOCAL_TZ)
    res = cal_service.events().list(calendarId=config.SINGLE_CALENDAR_ID, timeMin=now.isoformat(), singleEvents=True, orderBy='startTime').execute()
    events = [e for e in res.get('items', []) if f"ID:{uid}" in e.get('description', '')]
    if not events: return await message.answer("У вас нет активных записей.")
    for e in events:
        dt = datetime.fromisoformat(e['start']['dateTime']).astimezone(config.LOCAL_TZ)
        b = InlineKeyboardBuilder().button(text="❌ Отменить", callback_data=f"cancel_{e['id']}")
        await message.answer(f"📅 {dt.strftime('%d.%m %H:%M')}\n🔹 {e['summary']}", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_ev(c: types.CallbackQuery):
    eid = c.data.replace("cancel_", "")
    try:
        ev = cal_service.events().get(calendarId=config.SINGLE_CALENDAR_ID, eventId=eid).execute()
        dt = datetime.fromisoformat(ev['start'].get('dateTime')).astimezone(config.LOCAL_TZ)
        cal_service.events().delete(calendarId=config.SINGLE_CALENDAR_ID, eventId=eid).execute()
        await c.message.edit_text(f"❌ Запись на {dt.strftime('%d.%m %H:%M')} отменена.")
        await bot.send_message(config.REAL_ADMIN_GROUP, f"⚠️ ОТМЕНА: {dt.strftime('%d.%m %H:%M')}")
    except: await c.answer("Ошибка.")

async def main():
    await bot.set_my_commands([types.BotCommand(command="/start", description="Меню"), types.BotCommand(command="/my_bookings", description="Записи")])
    scheduler.start(); await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO); asyncio.run(main())