# homework_bot.py
# -*- coding: utf-8 -*-
"""
Telegram bot: bitta ustoz va ko‘plab o‘quvchilar.
O‘quvchilar uyga vazifani yuboradi; ustoz 0/1/2 ball bilan baholaydi.

Texnologiyalar:
- aiogram v3 (async)
- aiosqlite (SQLite DB)
- python-dotenv (.env dan token/TEACHER_ID)

Ishga tushirish:
1) `pip install aiogram>=3.5.0 aiosqlite python-dotenv`
2) .env fayl yarating (shu papkada):
   BOT_TOKEN=123456:ABC...
   TEACHER_ID=123456789
3) `python homework_bot.py`

Asosiy komandalar:
- /start — ro‘l va yo‘riqnoma
- /submit — vazifa topshirish jarayonini boshlash (nom → fayl/matn)
- /my — o‘quvchining o‘z topshiriqlari va ballari
- /pending — (faqat ustoz) baholanmaganlarni ko‘rish va baholash
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# --- Qo'shimcha importlar (Windows/IPv4/timeout/retry) ---
import sys
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiohttp import web

# Windowsda ba'zi soket muammolari uchun Selector event loop siyosati
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# --------------------------------------------------
# Konfiguratsiya
# --------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TEACHER_ID = int(os.getenv("TEACHER_ID", "0"))  # bitta ustozning Telegram ID si
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. .env faylga qo‘ying: BOT_TOKEN=...")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("homework_bot")

# --- Bot sessiyasini sozlash (IPv4 majburiy + timeout) ---
def build_bot() -> Bot:
    # Aiogram AiohttpSession timeout parametri sekunlarda int/float bo'lishi kerak
    session = AiohttpSession(timeout=90)
    return Bot(BOT_TOKEN, session=session)

# --------------------------------------------------
# DB yordamchi funksiyalar
# --------------------------------------------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    username TEXT,
    assignment TEXT NOT NULL,
    student_desc TEXT,
    content_type TEXT NOT NULL,
    file_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    score INTEGER,
    teacher_desc TEXT,
    graded_by INTEGER,
    graded_at TEXT
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        # mavjud jadvalda ustunlar bo'lmasa qo'shib olish
        try:
            await db.execute("ALTER TABLE submissions ADD COLUMN student_desc TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE submissions ADD COLUMN teacher_desc TEXT")
        except Exception:
            pass
        await db.commit()

async def save_submission(student_id: int, username: Optional[str], assignment: str, content_type: str, file_id: str, student_desc: Optional[str] = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow().isoformat()
        cur = await db.execute(
            "INSERT INTO submissions (student_id, username, assignment, student_desc, content_type, file_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (student_id, username, assignment, student_desc, content_type, file_id, now),
        )
        await db.commit()
        return cur.lastrowid

async def fetch_pending(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM submissions WHERE score IS NULL ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return rows

async def fetch_one(submission_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,))
        return await cur.fetchone()

async def grade_submission(submission_id: int, score: int, teacher_id: int) -> Optional[Tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow().isoformat()
        cur = await db.execute(
            "UPDATE submissions SET score = ?, graded_by = ?, graded_at = ? WHERE id = ? AND score IS NULL",
            (score, teacher_id, now, submission_id),
        )
        await db.commit()
        if cur.rowcount == 0:
            return None
        # fetch student_id for notification
        db.row_factory = aiosqlite.Row
        cur2 = await db.execute("SELECT student_id FROM submissions WHERE id = ?", (submission_id,))
        row = await cur2.fetchone()
        return (submission_id, row["student_id"]) if row else None

async def fetch_my(student_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM submissions WHERE student_id = ? ORDER BY id DESC LIMIT ?",
            (student_id, limit),
        )
        return await cur.fetchall()

# --------------------------------------------------
# FSM
# --------------------------------------------------
class SubmitStates(StatesGroup):
    waiting_for_assignment_name = State()
    waiting_for_description = State()
    waiting_for_content = State()

# --------------------------------------------------
# Router
# --------------------------------------------------
router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == TEACHER_ID:
        text = (
            "Assalomu alaykum, ustoz!\n\n"
            "Buyruqlar:\n"
            "• /pending — baholanmagan topshiriqlar ro‘yxati\n"
            "— Har bir topshiriqda [0] [1] [2] tugmalari orqali baho qo‘yishingiz mumkin.\n"
        )
    else:
        text = (
            "Salom! Bu bot orqali uy vazifangizni topshirishingiz mumkin.\n\n"
            "Qanday ishlaydi:\n"
            "1) /submit buyrug‘ini bosing.\n"
            "2) Vazifa nomini yozing (masalan: ‘Algebra-1’).\n"
            "3) Izoh (ixtiyoriy) yozing.
4) Keyin faylni yuboring.\n"
            "— Ustoz bahosi: 0, 1 yoki 2 ball.\n"
            "• /my — o‘zingizning so‘nggi topshiriqlar va ballar.\n"
        )
    await message.answer(text)

@router.message(Command("submit"))
async def cmd_submit(message: types.Message, state: FSMContext):
    await state.set_state(SubmitStates.waiting_for_assignment_name)
    await message.answer("Vazifa nomini kiriting (masalan: ‘Algebra-1’):")

@router.message(SubmitStates.waiting_for_assignment_name, F.text)
async def get_assignment_name(message: types.Message, state: FSMContext):
    assignment = (message.text or "").strip()
    if not assignment:
        return await message.answer("Bo‘sh nom bo‘ldi. Iltimos, vazifa nomini kiriting.")
    await state.update_data(assignment=assignment)
    await state.set_state(SubmitStates.waiting_for_description)
    await message.answer("Izoh (ixtiyoriy) yuboring yoki /skip bosing.")

@router.message(SubmitStates.waiting_for_description, F.text)
async def get_description(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() in {"/skip", "skip", "o'tkazish", "otkazish"}:
        text = ""
    await state.update_data(student_desc=text)
    await state.set_state(SubmitStates.waiting_for_content)
    await message.answer("Endi vazifa faylini yuboring (document yoki rasm/video ham mumkin).")

async def _extract_payload(msg: types.Message) -> Optional[Tuple[str, str]]:(msg: types.Message) -> Optional[Tuple[str, str]]:
    """(content_type, file_id_or_text) qaytaradi yoki None.
    Qo‘llab-quvvatlanadigan turlar: document, photo, text, video, audio, voice
    """
    if msg.document:
        return ("document", msg.document.file_id)
    if msg.photo:  # eng katta rasm
        return ("photo", msg.photo[-1].file_id)
    if msg.video:
        return ("video", msg.video.file_id)
    if msg.audio:
        return ("audio", msg.audio.file_id)
    if msg.voice:
        return ("voice", msg.voice.file_id)
    if msg.text and msg.text.strip():
        return ("text", msg.text)
    return None

@router.message(SubmitStates.waiting_for_content)
async def receive_content(message: types.Message, state: FSMContext, bot: Bot):
    payload = await _extract_payload(message)
    if not payload:
        return await message.answer("Fayl/photo/matn topilmadi. Iltimos, qaytadan yuboring.")

    data = await state.get_data()
    assignment = data.get("assignment") or "Vazifa"

    content_type, file_id = payload
    submission_id = await save_submission(
        student_id=message.from_user.id,
        username=message.from_user.username,
        assignment=assignment,
        student_desc=data.get("student_desc"),
        content_type=content_type,
        file_id=file_id,
    )

    await state.clear()
    await message.answer(f"✅ Qabul qilindi! ID: {submission_id}\nVazifa: {assignment}\nBaholanishini kuting.")

    # Ustozga xabar
    if TEACHER_ID:
        kb = InlineKeyboardBuilder()
        kb.button(text="Ko‘rish", callback_data=f"show:{submission_id}")
        kb.button(text="0", callback_data=f"grade:{submission_id}:0")
        kb.button(text="1", callback_data=f"grade:{submission_id}:1")
        kb.button(text="2", callback_data=f"grade:{submission_id}:2")
        kb.adjust(1, 3)
        try:
            await bot.send_message(
                TEACHER_ID,
                (
                    "🆕 Yangi topshiriq!\n"
                    f"ID: {submission_id}\n"
                    f"O‘quvchi: @{message.from_user.username or message.from_user.id}\n"
                    f"Vazifa: {assignment}
"
                    f"Izoh (o‘quvchi): { (data.get('student_desc') or '—') }
"
                    f"Turi: {content_type}"
                ),
                reply_markup=kb.as_markup(),
            )
        except Exception as e:
            logger.warning("Ustozga xabar yuborilmadi: %s", e)

@router.message(Command("my"))
async def cmd_my(message: types.Message):
    rows = await fetch_my(message.from_user.id, limit=10)
    if not rows:
        return await message.answer("Hali topshirig‘ingiz yo‘q.")
    lines = ["So‘nggi topshiriqlaringiz:"]
    for r in rows:
        score_txt = "—" if r["score"] is None else str(r["score"])
        lines.append(f"#{r['id']} | {r['assignment']} | baho: {score_txt}")
    await message.answer("\n".join(lines))

# --------------- USTOZ UCHUN ---------------
@router.message(Command("pending"))
async def cmd_pending(message: types.Message, bot: Bot):
    if message.from_user.id != TEACHER_ID:
        return await message.answer("Bu buyruq faqat ustoz uchun.")

    rows = await fetch_pending(limit=10)
    if not rows:
        return await message.answer("Baholanmagan topshiriqlar yo‘q.")

    for r in rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="Ko‘rish", callback_data=f"show:{r['id']}")
        kb.button(text="0", callback_data=f"grade:{r['id']}:0")
        kb.button(text="1", callback_data=f"grade:{r['id']}:1")
        kb.button(text="2", callback_data=f"grade:{r['id']}:2")
        kb.adjust(1, 3)
        caption = (
        f"ID: {row['id']}
"
        f"O‘quvchi: @{row['username'] or row['student_id']}
"
        f"Vazifa: {row['assignment']}
"
        f"Izoh (o‘quvchi): { (row['student_desc'] or '—') }"
    )
    ct, fid = row["content_type"], row["file_id"]
    try:
        if ct == "photo":
            await bot.send_photo(TEACHER_ID, fid, caption=caption)
        elif ct == "document":
            await bot.send_document(TEACHER_ID, fid, caption=caption)
        elif ct == "video":
            await bot.send_video(TEACHER_ID, fid, caption=caption)
        elif ct == "audio":
            await bot.send_audio(TEACHER_ID, fid, caption=caption)
        elif ct == "voice":
            await bot.send_voice(TEACHER_ID, fid, caption=caption)
        elif ct == "text":
            await bot.send_message(TEACHER_ID, f"[MATN]\n{caption}\n\n{fid}")
        else:
            await call.answer("Qo‘llab-quvvatlanmagan tur.", show_alert=True)
    except Exception as e:
        logger.error("Ko‘rish jo‘natishda xato: %s", e)
        await call.answer("Xatolik yuz berdi.", show_alert=True)

@router.callback_query(F.data.startswith("grade:"))
async def on_grade(call: types.CallbackQuery, bot: Bot):
    if call.from_user.id != TEACHER_ID:
        return await call.answer("Faqat ustoz uchun.", show_alert=True)
    try:
        _, sid, sc = call.data.split(":")
        submission_id = int(sid)
        score = int(sc)
        if score not in (0, 1, 2):
            raise ValueError
    except Exception:
        return await call.answer("Noto‘g‘ri ma’lumot.", show_alert=True)

    res = await grade_submission(submission_id, score, TEACHER_ID)
    if not res:
        return await call.answer("Allaqachon baholangan yoki topilmadi.", show_alert=True)

    _, student_id = res
    await call.answer(f"Baho qo‘yildi: {score}")

    # o‘quvchiga xabar berish (ustoz izohi bo'lsa qo'shamiz)
    try:
        row = await fetch_one(submission_id)
        teacher_desc = row["teacher_desc"] if row else None
        text = f"📢 Sizning topshirig‘ingiz baholandi. ID: {submission_id} — baho: {score}"
        if teacher_desc:
            text += f"
Izoh: {teacher_desc}"
        await bot.send_message(student_id, text)
    except Exception as e:
        logger.warning("O‘quvchiga xabar yuborib bo‘lmadi: %s", e)

# --------------- USTOZ IZOH QO'SHISH (/comment) ---------------
async def update_teacher_comment(submission_id: int, teacher_desc: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE submissions SET teacher_desc = ? WHERE id = ?",
            (teacher_desc, submission_id),
        )
        await db.commit()
        return cur.rowcount > 0

@router.message(Command("comment"))
async def cmd_comment(message: types.Message, command: CommandObject, bot: Bot):
    if message.from_user.id != TEACHER_ID:
        return await message.answer("Bu buyruq faqat ustoz uchun.")
    args = (command.args or "").strip()
    if not args or " " not in args:
        return await message.answer("Foydalanish: /comment <ID> <izoh>")
    sid_str, comment = args.split(" ", 1)
    try:
        sid = int(sid_str)
    except ValueError:
        return await message.answer("ID butun son bo‘lishi kerak.")

    ok = await update_teacher_comment(sid, comment.strip())
    if not ok:
        return await message.answer("Topshiriq topilmadi.")

    row = await fetch_one(sid)
    if row:
        try:
            await bot.send_message(row["student_id"], f"📌 Ustoz izohi (ID {sid}):
{comment}")
        except Exception:
            pass
    await message.answer("Izoh saqlandi.")

# --------------------------------------------------
# Healthcheck uchun minimal web server (Render/Vercel kabi platformalar $PORT talab qiladi)
# --------------------------------------------------
async def start_health_server():
    async def ok(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)

    port = int(os.getenv("PORT", "8000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server listening on :%s", port)

# --------------------------------------------------
# main
# --------------------------------------------------
async def main():
    await init_db()
    # Health serverni ishga tushiramiz (PORT talab qiladigan hostinglar uchun)
    await start_health_server()
    dp = Dispatcher()
    dp.include_router(router)

    retry_delay = 5
    while True:
        bot = build_bot()
        try:
            logger.info("Bot ishga tushmoqda...")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except TelegramNetworkError as e:
            logger.error("TelegramNetworkError: %s", e)
        except Exception as e:
            logger.error("Polling xatosi: %s", e)
        finally:
            # sessiyani toza yopish
            try:
                await bot.session.close()
            except Exception:
                pass
            logger.info("%s soniyadan so'ng qaytadan urinish...", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)  # maksimal 60s backoff


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot to‘xtatildi")
