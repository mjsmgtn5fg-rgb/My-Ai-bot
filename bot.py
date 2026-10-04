import os
import io
import asyncio
import aiohttp
import urllib.parse
import threading
from flask import Flask
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.enums import ChatAction
from groq import Groq
from pypdf import PdfReader
from docx import Document

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY)

history = {}
MAX_HISTORY = 20

SYSTEM_PROMPT = (
    "Ты — дружелюбный и умный ассистент. Отвечай понятно, по делу, "
    "на русском языке (если пользователь не попросит иначе). "
    "Если не знаешь — честно скажи."
)


def get_history(user_id):
    if user_id not in history:
        history[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return history[user_id]


def trim_history(user_id):
    h = history[user_id]
    if len(h) > MAX_HISTORY + 1:
        history[user_id] = [h[0]] + h[-MAX_HISTORY:]


app = Flask(__name__)


@app.route("/")
@app.route("/health")
def health():
    return "Bot is running"


def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = (
        "👋 Привет! Я твой личный AI-бот.\n\n"
        "Что я умею:\n"
        "💬 Отвечаю на вопросы и общаюсь\n"
        "🎨 Генерирую картинки: /image описание\n"
        "📁 Читаю файлы: txt, pdf, docx\n"
        "🧹 Очищаю память: /reset\n\n"
        "Просто пиши мне сообщение!"
    )
    await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 Команды:\n"
        "/start — начало\n"
        "/help — справка\n"
        "/image <описание> — сгенерировать картинку\n"
        "/reset — очистить историю диалога\n\n"
        "Также можешь отправлять файлы (txt/pdf/docx)."
    )


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    history[message.from_user.id] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    await message.answer("🧹 История очищена. Начинаем заново!")


@dp.message(Command("image"))
async def cmd_image(message: types.Message):
    prompt = message.text.replace("/image", "", 1).strip()
    if not prompt:
        await message.answer("Напиши так: /image кот в космосе")
        return

    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    await message.answer("🎨 Генерирую картинку...")

    try:
        encoded = urllib.parse.quote(prompt)
        url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            "?width=1024&height=1024&nologo=true"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=120) as resp:
                if resp.status != 200:
                    await message.answer(f"❌ Ошибка генерации: {resp.status}")
                    return
                img_bytes = await resp.read()

        photo = BufferedInputFile(img_bytes, filename="image.png")
        await message.answer_photo(photo, caption=f"🎨 {prompt}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    h = get_history(user_id)
    h.append({"role": "user", "content": message.text})

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=h,
            temperature=0.7,
            max_tokens=2048,
        )
        answer = response.choices[0].message.content
        h.append({"role": "assistant", "content": answer})
        trim_history(user_id)

        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(F.document)
async def handle_document(message: types.Message):
    doc = message.document
    file_name = doc.file_name or "file"
    ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""

    if ext not in {"txt", "pdf", "docx"}:
        await message.answer("📁 Поддерживаю только txt, pdf, docx.")
        return

    await message.answer(f"📖 Читаю файл {file_name}...")

    try:
        file = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        buf.seek(0)

        if ext == "txt":
            content = buf.read().decode("utf-8", errors="ignore")
        elif ext == "pdf":
            reader = PdfReader(buf)
            content = "\n".join((p.extract_text() or "") for p in reader.pages)
        elif ext == "docx":
            d = Document(buf)
            content = "\n".join(p.text for p in d.paragraphs)

        content = content[:15000]

        prompt = (
            f"Пользователь прислал файл '{file_name}'. "
            f"Вот его содержимое:\n\n---\n{content}\n---\n\n"
            "Кратко перескажи, о чём этот файл."
        )

        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=2048,
        )
        answer = response.choices[0].message.content

        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
    except Exception as e:
        await message.answer(f"❌ Ошибка при чтении файла: {e}")


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer(
        "🖼 Вижу картинку! Пока не умею анализировать изображения.\n"
        "Могу сгенерировать новую: /image описание"
    )


async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    print("🤖 Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
