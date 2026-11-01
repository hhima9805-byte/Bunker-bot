"""Telegram-бот «Бункер» (aiogram 3). Тонкий слой над engine.py."""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from engine import RULES_TEXT, DeliveryError, Engine, Settings, esc, mention_of
from storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bunker.bot")

try:   # карточки картинкой: нужен Pillow и шрифт из папки fonts
    import card_image
    CARD_RENDERER = card_image.render_card if card_image.available() else None
except Exception as exc:   # без Pillow бот работает, карточки идут текстом
    log.warning("Карточки картинкой недоступны: %r", exc)
    CARD_RENDERER = None

router = Router()
engine = None  # создаётся в main()

MAX_LEN = 4000  # лимит Telegram 4096 символов, оставляем запас


def load_token():
    token = (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'").strip()
    if not token:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана. Добавьте её в Railway: "
                           "сервис -> Variables -> New Variable.")
    return token


def env_int(name, default):
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def to_markup(buttons):
    if not buttons:
        return None
    rows = []
    for row in buttons:
        out = []
        for b in row:
            if b.url:
                out.append(InlineKeyboardButton(text=b.text, url=b.url))
            else:
                out.append(InlineKeyboardButton(text=b.text, callback_data=b.data))
        rows.append(out)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def split_text(text, limit=MAX_LEN):
    """Делит длинный текст по переводам строк"""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts


class TelegramMessenger:
    def __init__(self, bot):
        self.bot = bot

    async def _send_one(self, chat_id, text, markup):
        for attempt in range(3):
            try:
                msg = await self.bot.send_message(chat_id, text, reply_markup=markup)
                return msg.message_id
            except TelegramRetryAfter as e:          # превышен лимит сообщений: ждём и повторяем
                if attempt == 2:
                    raise DeliveryError(str(e))
                await asyncio.sleep(e.retry_after + 0.5)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                raise DeliveryError(str(e))

    async def send_photo(self, chat_id, png, caption, buttons=None):
        for attempt in range(3):
            try:
                msg = await self.bot.send_photo(chat_id, BufferedInputFile(png, filename="card.png"),
                                                caption=caption[:1024], reply_markup=to_markup(buttons))
                return msg.message_id
            except TelegramRetryAfter as e:
                if attempt == 2:
                    raise DeliveryError(str(e))
                await asyncio.sleep(e.retry_after + 0.5)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                raise DeliveryError(str(e))

    async def send(self, chat_id, text, buttons=None):
        parts = split_text(text)
        last_id = None
        for i, part in enumerate(parts):
            markup = to_markup(buttons) if i == len(parts) - 1 else None
            last_id = await self._send_one(chat_id, part, markup)
        return last_id

    async def edit(self, chat_id, message_id, text, buttons=None):
        try:
            await self.bot.edit_message_text(text=split_text(text)[0], chat_id=chat_id, message_id=message_id,
                                             reply_markup=to_markup(buttons))
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
            raise DeliveryError(str(e))
        except (TelegramForbiddenError, TelegramRetryAfter) as e:   # правки не критичны, пропускаем
            raise DeliveryError(str(e))


def is_group(message):
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def is_chat_admin(bot, chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


RIGHTS_WARNING = ("Чтобы во время игры писать в чат могли только игроки, сделайте бота администратором "
                  "с правом «Удаление сообщений». Пока этого нет, сообщения остальных не удаляются.")


async def bot_can_delete(bot, chat_id):
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status == "creator":
            return True
        return member.status == "administrator" and bool(getattr(member, "can_delete_messages", False))
    except Exception:
        return False


async def delete_later(bot, chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ---------- команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    payload = (command.args or "").strip()
    if payload.startswith("join_"):
        try:
            chat_id = int(payload[len("join_"):])
        except ValueError:
            await message.answer("Некорректная ссылка. Нажмите «Войти» в игровом чате ещё раз.")
            return
        _, text = await engine.join(chat_id, message.from_user.id, message.from_user.full_name,
                                    message.from_user.username)
        await message.answer(text)
        return
    await message.answer(
        "Это бот для игры «Бункер».\n\n"
        "Добавьте меня в групповой чат и отправьте там команду /bunker, чтобы открыть набор игроков. "
        "Правила: /rules.")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>\n"
        "/bunker: открыть набор игроков (в группе)\n"
        "/table: показать, что уже раскрыто\n"
        "/stop: остановить игру (создатель или администратор)\n"
        "В наборе создатель может включить «Режим ведущего»: он не играет, видит все карточки "
        "и управляет игрой через панель в личных сообщениях.\n"
        "Кнопка «Настройки игры» в наборе: набор сценариев (реализм, фантастика, абсурд, смешанный), "
        "быстрая игра, карточки картинкой, финал «снаружи».\n"
        "/rules: правила\n"
        "/profile: ваша статистика\n"
        "/top: лучшие игроки\n"
        "/history: последние игры")


@router.message(Command("rules"))
async def cmd_rules(message: Message):
    await message.answer(RULES_TEXT)


@router.message(Command("bunker"))
async def cmd_bunker(message: Message):
    if not is_group(message):
        await message.answer("Игру нужно создавать в групповом чате. Добавьте меня в группу и отправьте /bunker там.")
        return
    ok, err = await engine.create_lobby(message.chat.id, message.from_user.id,
                                        message.from_user.full_name, message.from_user.username)
    if not ok:
        await message.answer(err)
        return
    if not await bot_can_delete(message.bot, message.chat.id):
        await message.answer(RIGHTS_WARNING)


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    if not is_group(message):
        return
    admin = await is_chat_admin(message.bot, message.chat.id, message.from_user.id)
    ok, text = await engine.cancel(message.chat.id, message.from_user.id, admin)
    if not ok:
        await message.answer(text)


@router.message(Command("table"))
async def cmd_table(message: Message):
    text = engine.show_table(message.chat.id)
    await message.answer(text or "Сейчас в этом чате нет идущей игры.")


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    await message.answer(engine.profile_text(message.from_user.id, message.from_user.full_name))


@router.message(Command("top"))
async def cmd_top(message: Message):
    await message.answer(engine.top_text())


@router.message(Command("history"))
async def cmd_history(message: Message):
    await message.answer(engine.history_text())


# ---------- кнопки ----------

async def reply(callback: CallbackQuery, ok, text, remove_markup=False):
    """Всплывающий ответ на кнопку; при успехе можно убрать кнопки у личного сообщения"""
    try:
        await callback.answer(text, show_alert=not ok)
    except TelegramBadRequest:
        pass
    if ok and remove_markup and isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


def chat_of(callback: CallbackQuery):
    return callback.message.chat.id if callback.message else None


@router.callback_query(F.data == "lobby:leave")
async def cb_lobby_leave(callback: CallbackQuery):
    ok, text = await engine.leave(chat_of(callback), callback.from_user.id)
    await reply(callback, ok, text)


@router.callback_query(F.data == "lobby:start")
async def cb_lobby_start(callback: CallbackQuery):
    chat_id = chat_of(callback)
    admin = await is_chat_admin(callback.bot, chat_id, callback.from_user.id)
    ok, text = await engine.start(chat_id, callback.from_user.id, admin)
    await reply(callback, ok, text if not ok else "Игра начинается.")
    if ok and not await bot_can_delete(callback.bot, chat_id):
        try:
            await callback.bot.send_message(chat_id, RIGHTS_WARNING)
        except Exception:
            pass


@router.callback_query(F.data == "lobby:cancel")
async def cb_lobby_cancel(callback: CallbackQuery):
    chat_id = chat_of(callback)
    admin = await is_chat_admin(callback.bot, chat_id, callback.from_user.id)
    ok, text = await engine.cancel(chat_id, callback.from_user.id, admin)
    await reply(callback, ok, text)


@router.callback_query(F.data == "lobby:host")
async def cb_lobby_host(callback: CallbackQuery):
    ok, text = await engine.toggle_host(chat_of(callback), callback.from_user.id)
    await reply(callback, ok, text)


@router.callback_query(F.data == "lobby:settings")
async def cb_lobby_settings(callback: CallbackQuery):
    ok, text = await engine.open_settings(chat_of(callback), callback.from_user.id)
    await reply(callback, ok, text)


@router.callback_query(F.data.startswith("set:"))
async def cb_setting(callback: CallbackQuery):
    ok, text = await engine.on_setting(chat_of(callback), callback.from_user.id, callback.data.split(":", 1)[1])
    await reply(callback, ok, text)


@router.callback_query(F.data.startswith("host:"))
async def cb_host(callback: CallbackQuery):
    ok, text = await engine.on_host(callback.from_user.id, callback.data)
    # кнопки главной панели остаются, кнопки выбора игрока и подтверждения убираются после нажатия
    submenu = callback.data.count(":") >= 2 or callback.data == "host:so"
    await reply(callback, ok, text, remove_markup=submenu)


@router.callback_query(F.data == "ready")
async def cb_ready(callback: CallbackQuery):
    ok, text = await engine.on_ready(chat_of(callback), callback.from_user.id)
    await reply(callback, ok, text)


@router.callback_query(F.data.startswith("vote:"))
async def cb_vote(callback: CallbackQuery):
    try:
        target = int(callback.data.split(":", 1)[1])
    except ValueError:
        await reply(callback, False, "Некорректная кнопка.")
        return
    ok, text = await engine.on_vote(chat_of(callback), callback.from_user.id, target)
    await reply(callback, ok, text)


@router.callback_query(F.data.startswith("open:"))
async def cb_open(callback: CallbackQuery):
    try:
        _, round_no, key = callback.data.split(":", 2)
        round_no = int(round_no)
    except ValueError:
        await reply(callback, False, "Некорректная кнопка.")
        return
    ok, text = await engine.on_open(callback.from_user.id, key, round_no)
    await reply(callback, ok, text, remove_markup=True)


@router.callback_query(F.data == "card:use")
async def cb_card_use(callback: CallbackQuery):
    ok, text = await engine.on_card_use(callback.from_user.id)
    await reply(callback, ok, text)


@router.callback_query(F.data.startswith("card:t:"))
async def cb_card_target(callback: CallbackQuery):
    try:
        target = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await reply(callback, False, "Некорректная кнопка.")
        return
    ok, text = await engine.on_card_target(callback.from_user.id, target)
    await reply(callback, ok, text, remove_markup=True)


@router.callback_query(F.data.startswith("card:k:"))
async def cb_card_key(callback: CallbackQuery):
    key = callback.data.split(":")[2]
    ok, text = await engine.on_card_key(callback.from_user.id, key)
    await reply(callback, ok, text, remove_markup=True)


# ---------- тишина в чате во время игры ----------
# Регистрируется последним: команды выше обрабатываются как обычно.

@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_guard(message: Message):
    if message.new_chat_members or message.left_chat_member:
        return
    user = message.from_user
    user_id = user.id if (user and not message.sender_chat) else None   # анонимный админ или канал
    if not engine.is_silenced(message.chat.id, user_id):
        return
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        return   # у бота нет права удалять сообщения
    if user_id is None or not engine.should_notify(message.chat.id, user_id):
        return
    title = esc(message.chat.title or "группа")
    try:
        await message.bot.send_message(
            user_id, f"В чате «{title}» сейчас идёт игра «Бункер». Писать туда могут только игроки, "
                     f"которые ещё в игре, поэтому ваше сообщение удалено. Дождитесь окончания игры.")
    except (TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter):
        # личка с ботом не открыта: короткое уведомление в чате, которое само исчезнет
        try:
            note = await message.bot.send_message(
                message.chat.id, f"{mention_of(user.id, user.full_name, user.username)}, сейчас идёт игра. "
                                 f"Писать в чат могут только игроки, которые ещё в игре.")
            asyncio.create_task(delete_later(message.bot, message.chat.id, note.message_id, 15))
        except Exception:
            pass


COMMANDS = [
    BotCommand(command="bunker", description="Открыть набор игроков"),
    BotCommand(command="table", description="Что уже раскрыто"),
    BotCommand(command="rules", description="Правила игры"),
    BotCommand(command="stop", description="Остановить игру"),
    BotCommand(command="profile", description="Моя статистика"),
    BotCommand(command="top", description="Лучшие игроки"),
    BotCommand(command="history", description="Последние игры"),
    BotCommand(command="help", description="Список команд"),
]


async def main():
    global engine
    token = load_token()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    settings = Settings(
        min_players=env_int("MIN_PLAYERS", 5),
        max_players=env_int("MAX_PLAYERS", 12),
        open_seconds=env_int("OPEN_SECONDS", 90),
        discuss_seconds=env_int("DISCUSS_SECONDS", 180),
        vote_seconds=env_int("VOTE_SECONDS", 60),
        revote_seconds=env_int("REVOTE_SECONDS", 45),
        timer_tick=env_int("TIMER_TICK", 10),
    )
    engine = Engine(TelegramMessenger(bot), Storage(), settings, bot_username=me.username,
                    card_renderer=CARD_RENDERER)
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as e:
        log.warning("Не удалось установить список команд: %r", e)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Бот @%s запущен", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
