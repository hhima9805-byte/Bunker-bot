"""Проверка bot.py на заглушке aiogram: разбор кнопок, маршрутизация, повтор при лимитах.
Совместимость с настоящим aiogram эта проверка не подтверждает."""
import asyncio
import logging
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
logging.disable(logging.CRITICAL)


# ---------- заглушка aiogram ----------
class _F:
    def __getattr__(self, name):
        return self

    def __eq__(self, other):
        return ("eq", other)

    def startswith(self, prefix):
        return ("startswith", prefix)

    def __call__(self, *a, **k):
        return self

    __hash__ = object.__hash__


class Router:
    def __init__(self):
        self.messages, self.callbacks = [], []

    def message(self, *filters):
        def deco(f):
            self.messages.append((filters, f)); return f
        return deco

    def callback_query(self, *filters):
        def deco(f):
            self.callbacks.append((filters, f)); return f
        return deco


class TelegramBadRequest(Exception):
    pass


class TelegramForbiddenError(Exception):
    pass


class TelegramRetryAfter(Exception):
    def __init__(self, retry_after):
        super().__init__(f"retry after {retry_after}"); self.retry_after = retry_after


class Message:
    pass


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def install():
    a = types.ModuleType("aiogram")
    a.Bot = object; a.Dispatcher = object; a.Router = Router; a.F = _F()
    client = types.ModuleType("aiogram.client"); default = types.ModuleType("aiogram.client.default")
    default.DefaultBotProperties = Obj
    enums = types.ModuleType("aiogram.enums")
    enums.ChatType = Obj(GROUP="group", SUPERGROUP="supergroup", PRIVATE="private")
    enums.ParseMode = Obj(HTML="HTML")
    exc = types.ModuleType("aiogram.exceptions")
    exc.TelegramBadRequest = TelegramBadRequest; exc.TelegramForbiddenError = TelegramForbiddenError
    exc.TelegramRetryAfter = TelegramRetryAfter
    filters = types.ModuleType("aiogram.filters")
    filters.Command = lambda *a, **k: ("command", a); filters.CommandStart = lambda *a, **k: ("start",)
    filters.CommandObject = Obj
    tps = types.ModuleType("aiogram.types")
    tps.BotCommand = Obj; tps.CallbackQuery = Obj; tps.InlineKeyboardButton = Obj
    tps.InlineKeyboardMarkup = Obj; tps.Message = Message
    tps.BufferedInputFile = lambda data, filename: Obj(data=data, filename=filename)
    sys.modules.update({"aiogram": a, "aiogram.client": client, "aiogram.client.default": default,
                        "aiogram.enums": enums, "aiogram.exceptions": exc, "aiogram.filters": filters,
                        "aiogram.types": tps})


install()
import bot as B  # noqa: E402
import game as G  # noqa: E402
from engine import Engine, Settings  # noqa: E402
from storage import Storage  # noqa: E402

n_ok = 0


def ok(cond, name):
    global n_ok
    print(("OK  " if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    n_ok += 1


# ---------- поддельный Bot API ----------
class FakeBot:
    def __init__(self):
        self.sent, self.edits = [], []
        self.next_id = 1
        self.forbidden = set()
        self.retry_once = set()
        self.admins = set()
        self.bot_can_delete = False
        self.deleted = []
        self.BOT_ID = 999

    async def me(self):
        return Obj(id=self.BOT_ID, username="bunker_test_bot")

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError("bot was blocked by the user")
        if chat_id in self.retry_once:
            self.retry_once.discard(chat_id)
            raise TelegramRetryAfter(0.01)
        assert len(text) <= 4096, "сообщение длиннее лимита Telegram"
        mid = self.next_id; self.next_id += 1
        self.sent.append((chat_id, text, reply_markup, mid))
        return Obj(message_id=mid)

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError("bot was blocked by the user")
        if chat_id in self.retry_once:
            self.retry_once.discard(chat_id)
            raise TelegramRetryAfter(0.01)
        mid = self.next_id; self.next_id += 1
        self.photos = getattr(self, "photos", []) + [(chat_id, photo, caption, reply_markup, mid)]
        return Obj(message_id=mid)

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))

    async def get_chat_member(self, chat_id, user_id):
        if user_id == self.BOT_ID:
            return Obj(status="administrator" if self.bot_can_delete else "member",
                       can_delete_messages=self.bot_can_delete)
        return Obj(status="administrator" if user_id in self.admins else "member")

    def texts(self, chat):
        return [t for c, t, _, _ in self.sent if c == chat]


fb = FakeBot()
B.engine = Engine(B.TelegramMessenger(fb), Storage(tempfile.mkdtemp()),
                  Settings(min_players=5, open_seconds=5, discuss_seconds=5, vote_seconds=5,
                           revote_seconds=5, edit_interval=0, notice_interval=60), bot_username="bunker_test_bot")
E = B.engine
GROUP = -100777


class Answers:
    def __init__(self):
        self.items = []

    async def __call__(self, text=None, **kw):
        self.items.append((text, kw.get("show_alert")))


_next_msg_id = [10000]


def msg(user_id, chat_id, chat_type="supergroup", name=None, username=None, sender_chat=None, service=False):
    m = Message()
    m.from_user = Obj(id=user_id, full_name=name or f"User{user_id}", username=username)
    m.chat = Obj(id=chat_id, type=chat_type, title="Тестовая группа")
    m.sender_chat = sender_chat
    m.new_chat_members = [m.from_user] if service else None
    m.left_chat_member = None
    m.answers = []
    _next_msg_id[0] += 1
    m.message_id = _next_msg_id[0]
    m.deleted = False

    async def delete():
        if not fb.bot_can_delete:
            raise TelegramBadRequest("not enough rights")
        m.deleted = True
    m.delete = delete

    async def answer(text, **kw):
        m.answers.append(text)
    m.answer = answer
    m.bot = fb
    return m


def cb(user_id, chat_id, data):
    c = Obj(from_user=Obj(id=user_id), data=data, bot=fb)
    c.answer = Answers()
    dm_msg = Message()
    dm_msg.chat = Obj(id=chat_id)
    dm_msg.markup_removed = False

    async def edit_reply_markup(reply_markup=None):
        dm_msg.markup_removed = True
    dm_msg.edit_reply_markup = edit_reply_markup
    c.message = dm_msg
    return c


def run(c):
    return asyncio.get_event_loop().run_until_complete(c)


asyncio.set_event_loop(asyncio.new_event_loop())

# ---------- служебные функции ----------
ok(B.split_text("\n".join(["a"] * 10), limit=6) == ["a\na\na", "a\na\na", "a\na\na", "a"], "длинный текст делится по строкам")
ok(B.split_text("коротко") == ["коротко"], "короткий текст не делится")
long_text = "\n".join(f"Строка номер {i}" for i in range(600))
ok(all(len(p) <= 4000 for p in B.split_text(long_text)) and "\n".join(B.split_text(long_text)) == long_text,
   "деление не теряет текст и укладывается в лимит")
os.environ["BOT_TOKEN"] = ' "123:abc" \n'
ok(B.load_token() == "123:abc", "токен очищается от кавычек и пробелов")
os.environ["BOT_TOKEN"] = ""
try:
    B.load_token(); ok(False, "пустой токен")
except RuntimeError as e:
    ok("BOT_TOKEN" in str(e), "без токена понятная ошибка")
os.environ["X_INT"] = "abc"
ok(B.env_int("X_INT", 7) == 7 and B.env_int("NO_SUCH", 9) == 9, "настройки из переменных с запасным значением")

# ---------- мессенджер ----------
mess = B.TelegramMessenger(fb)
fb.retry_once.add(55)
mid = run(mess.send(55, "текст"))
ok(mid is not None and fb.texts(55) == ["текст"], "при лимите Telegram сообщение отправляется повторно")
fb.forbidden.add(66)
try:
    run(mess.send(66, "x")); ok(False, "запрет")
except B.DeliveryError:
    ok(True, "заблокированный бот: DeliveryError, игра решает, что делать")
big = "\n".join("x" * 50 for _ in range(200))
run(mess.send(77, big, [[B.Btn if False else type("Bt", (), {"text": "Кн", "data": "d", "url": None})()]]))
parts = [(t, mk) for c, t, mk, _ in fb.sent if c == 77]
ok(len(parts) == 3 and parts[0][1] is None and parts[-1][1] is not None, "кнопки прикрепляются только к последней части")

# ---------- команды ----------
m = msg(1, 1, "private"); run(B.cmd_bunker(m))
ok("групповом чате" in m.answers[0], "/bunker в личке объясняет, что нужно в группе")
m = msg(1, GROUP); run(B.cmd_bunker(m))
ok(GROUP in E.sessions and any("Набор игроков" in t for t in fb.texts(GROUP)), "/bunker в группе открывает набор")
ok(any("Удаление сообщений" in a for a in m.answers), "без прав на удаление бот сразу предупреждает")
lobby_kb = [mk for c, t, mk, _ in fb.sent if c == GROUP][-1]
ok(lobby_kb.inline_keyboard[0][0].url == f"https://t.me/bunker_test_bot?start=join_{GROUP}", "кнопка «Войти» — ссылка с кодом чата")
m = msg(1, GROUP); run(B.cmd_bunker(m)); ok("уже идёт" in m.answers[0], "второй /bunker в том же чате отклонён")

for uid in range(100, 105):
    m = msg(uid, uid, "private", name=f"Игрок{uid}", username=f"user_{uid}")
    run(B.cmd_start(m, Obj(args=f"join_{GROUP}")))
    ok("Вы записаны" in m.answers[0], f"/start join_ записывает игрока {uid}")
m = msg(200, 200, "private"); run(B.cmd_start(m, Obj(args="join_abc")))
ok("Некорректная ссылка" in m.answers[0], "битая ссылка входа")
m = msg(201, 201, "private"); run(B.cmd_start(m, Obj(args=None)))
ok("/bunker" in m.answers[0], "обычный /start показывает инструкцию")

c = cb(100, GROUP, "lobby:start"); run(B.cb_lobby_start(c))
ok(c.answer.items[0][1] is True and "Начать игру может только создатель" in c.answer.items[0][0], "чужой «Начать игру» — всплывающее предупреждение")
c = cb(104, GROUP, "lobby:leave"); run(B.cb_lobby_leave(c))
ok(c.answer.items[0] == ("Вы вышли из игры.", False), "«Выйти» работает")
c = cb(1, GROUP, "lobby:start"); run(B.cb_lobby_start(c))
ok("минимум 5" in c.answer.items[0][0], "4 игрока — старт отклонён с пояснением")
m = msg(104, 104, "private", name="Игрок104"); run(B.cmd_start(m, Obj(args=f"join_{GROUP}")))
fb.admins.add(100)
c = cb(100, GROUP, "lobby:start"); run(B.cb_lobby_start(c))
ok(c.answer.items[0] == ("Игра начинается.", False), "администратор чата может начать игру")
ok(any("Удаление сообщений" in t for t in fb.texts(GROUP)), "при старте без прав — предупреждение в чат")
ok(E.sessions[GROUP].game.players[100].username == "user_100", "username игрока передаётся в игру")
g = E.sessions[GROUP].game
ok(all(any("секретная карточка" in t for t in fb.texts(u)) for u in range(100, 105)), "карточки дошли всем в личку")


async def until(cond, timeout=3):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False

ok(run(until(lambda: g.phase == G.DISCUSSION)), "игра дошла до обсуждения")

# тишина в чате
m = msg(100, GROUP); run(B.group_guard(m)); ok(not m.deleted, "без прав бот ничего не удаляет и не падает")
fb.bot_can_delete = True
m = msg(100, GROUP); run(B.group_guard(m)); ok(not m.deleted, "сообщение игрока остаётся")
m = msg(500, GROUP, name="Владелец", username="owner_x"); run(B.group_guard(m))
ok(m.deleted and any("сейчас идёт игра" in t for t in fb.texts(500)), "сообщение зрителя (владельца) удалено, в личку пришло уведомление")
n_before = len(fb.texts(500))
m = msg(500, GROUP); run(B.group_guard(m))
ok(m.deleted and len(fb.texts(500)) == n_before, "повторное уведомление не шлётся чаще раза в минуту")
fb.forbidden.add(501)
m = msg(501, GROUP, name="Админ", username="admin_y"); run(B.group_guard(m))
note = [(c, t, mid) for c, t, _, mid in fb.sent if c == GROUP and "@admin_y, сейчас идёт игра" in t]
ok(m.deleted and note, "личка закрыта: короткое уведомление в чат с упоминанием")
run(asyncio.sleep(0))
m = msg(0, GROUP, sender_chat=Obj(id=GROUP)); run(B.group_guard(m)); ok(m.deleted, "анонимный админ: сообщение удалено")
m = msg(502, GROUP, service=True); run(B.group_guard(m)); ok(not m.deleted, "служебные сообщения о входе не трогаются")
m = msg(503, -42); run(B.group_guard(m)); ok(not m.deleted, "в группе без игры ничего не удаляется")
ok(run(until(lambda: any("Стол" in t for t in fb.texts(GROUP)))), "стол показан")
m = msg(5, GROUP); run(B.cmd_table(m)); ok("<b>Стол</b>" in m.answers[0], "/table показывает стол")

for uid in (100, 101, 102):
    c = cb(uid, GROUP, "ready"); run(B.cb_ready(c))
    ok(c.answer.items[0][0] == "Ваша готовность учтена.", f"«Готов» от {uid}")
ok(run(until(lambda: g.phase == G.VOTING)), "большинство готово — голосование")

c = cb(100, GROUP, "vote:abc"); run(B.cb_vote(c)); ok(c.answer.items[0][1] is True, "битая кнопка голосования не роняет бота")
c = cb(100, GROUP, "vote:100"); run(B.cb_vote(c)); ok("за себя" in c.answer.items[0][0], "голос за себя отклонён")
c = cb(999, GROUP, "vote:100"); run(B.cb_vote(c)); ok(c.answer.items[0][1] is True, "посторонний не может голосовать")
for uid in (100, 101, 102, 103):
    c = cb(uid, GROUP, "vote:104"); run(B.cb_vote(c))
    ok(c.answer.items[0][0] == "Ваш голос: Игрок104.", f"голос игрока {uid} принят")
c = cb(104, GROUP, "vote:100"); run(B.cb_vote(c))
ok(run(until(lambda: g.round == 2 and g.phase == G.OPENING)), "все проголосовали — раунд 2")
ok(not g.players[104].alive, "исключён тот, за кого голосовало большинство")

c = cb(100, 100, "open:1:health"); run(B.cb_open(c))
ok("прошлому раунду" in c.answer.items[0][0] and not c.message.markup_removed, "кнопка прошлого раунда отклонена, кнопки остаются")
c = cb(100, 100, "open:2:health"); run(B.cb_open(c))
ok(c.answer.items[0][1] is False and c.message.markup_removed and "health" in g.players[100].opened,
   "раскрытие из лички: принято, кнопки убраны")
c = cb(101, 101, "open:x:health"); run(B.cb_open(c)); ok(c.answer.items[0][1] is True, "битая кнопка раскрытия не роняет бота")
for uid in (101, 102, 103):
    run(B.cb_open(cb(uid, uid, "open:2:fact")))
ok(run(until(lambda: g.phase == G.DISCUSSION)), "все раскрыли — обсуждение раунда 2")

g.players[101].card = "reveal"; g.players[101].card_used = False
c = cb(101, 101, "card:use"); run(B.cb_card_use(c))
ok("Выберите цель" in c.answer.items[0][0] and any("Выберите игрока" in t for t in fb.texts(101)), "особая карта с целью: бот прислал выбор")
c = cb(101, 101, "card:t:zz"); run(B.cb_card_target(c)); ok(c.answer.items[0][1] is True, "битая кнопка цели не роняет бота")
c = cb(101, 101, "card:t:102"); run(B.cb_card_target(c))
ok(c.answer.items[0] == ("Карта использована.", False) and c.message.markup_removed, "карта применена через кнопку")
g.players[102].card = "reroll"; g.players[102].card_used = False
run(B.cb_card_use(cb(102, 102, "card:use")))
hidden = [k for k in G.TRAIT_KEYS if k not in g.players[102].opened][0]
c = cb(102, 102, f"card:k:{hidden}"); run(B.cb_card_key(c))
ok(c.answer.items[0] == ("Карта использована.", False), "замена характеристики через кнопку")

m = msg(55, GROUP); run(B.cmd_stop(m)); ok("создатель или администратор" in m.answers[0], "/stop от постороннего отклонён")
m = msg(1, GROUP); run(B.cmd_stop(m))
ok(GROUP not in E.sessions and any("Игра остановлена" in t for t in fb.texts(GROUP)), "/stop от создателя останавливает игру")
m = msg(5, GROUP); run(B.cmd_table(m)); ok("нет идущей игры" in m.answers[0], "/table без игры")

for cmd in (B.cmd_help, B.cmd_rules, B.cmd_profile, B.cmd_top, B.cmd_history):
    m = msg(100, 100, "private"); run(cmd(m))
    ok(m.answers and m.answers[0], f"{cmd.__name__} отвечает")

cb_lost = Obj(from_user=Obj(id=1), data="ready", bot=fb, message=None); cb_lost.answer = Answers()
run(B.cb_ready(cb_lost)); ok(cb_lost.answer.items[0][1] is True, "кнопка из недоступного старого сообщения не роняет бота")

# режим ведущего через кнопки
m = msg(7, GROUP); run(B.cmd_bunker(m))
c = cb(100, GROUP, "lobby:host"); run(B.cb_lobby_host(c)); ok(c.answer.items[0][1] is True, "режим ведущего: только создатель")
c = cb(7, GROUP, "lobby:host"); run(B.cb_lobby_host(c))
ok(c.answer.items[0][1] is False and E.sessions[GROUP].host_mode, "создатель включил режим ведущего кнопкой")
for uid in range(100, 105):
    run(B.cmd_start(msg(uid, uid, "private", name=f"Игрок{uid}", username=f"user_{uid}"), Obj(args=f"join_{GROUP}")))
m = msg(7, 7, "private"); run(B.cmd_start(m, Obj(args=f"join_{GROUP}")))
ok("Вы ведущий" in m.answers[0], "ведущий не может записаться игроком")
run(B.cb_lobby_start(cb(7, GROUP, "lobby:start")))
g = E.sessions[GROUP].game
ok(any("Панель ведущего" in t for t in fb.texts(7)), "ведущему пришла панель")
ok(run(until(lambda: g.phase == G.DISCUSSION)), "игра с ведущим идёт")
m = msg(7, GROUP); run(B.group_guard(m)); ok(m.deleted, "сообщение ведущего в чате удаляется")
c = cb(7, 7, "host:cards"); run(B.cb_host(c))
ok(c.answer.items[0] == ("Карточки отправлены.", False) and not c.message.markup_removed, "главная панель: кнопки остаются")
c = cb(7, 7, "host:kc:101"); run(B.cb_host(c)); ok(c.message.markup_removed, "выбор игрока: кнопки убраны")
c = cb(7, 7, "host:ko:101"); run(B.cb_host(c)); ok(not g.players[101].alive, "исключение через кнопку подтверждения")
c = cb(100, 100, "host:cards"); run(B.cb_host(c)); ok(c.answer.items[0][1] is True, "игрок не может нажимать кнопки ведущего")
m = msg(101, GROUP); run(B.group_guard(m)); ok(m.deleted, "исключённый не может писать в чат")
run(B.cb_host(cb(7, 7, "host:stop"))); c = cb(7, 7, "host:so"); run(B.cb_host(c))
ok(GROUP not in E.sessions, "ведущий остановил игру кнопкой")
m = msg(500, GROUP); run(B.group_guard(m)); ok(not m.deleted, "после игры писать снова могут все")

# настройки и фото
fb.retry_once.add(88)
mid = run(mess.send_photo(88, b"\x89PNGdata", "подпись" * 300, None))
ph = fb.photos[-1]
ok(mid and ph[0] == 88 and ph[1].data == b"\x89PNGdata" and len(ph[2]) == 1024, "фото: повтор при лимите, подпись обрезана до 1024")
fb.forbidden.add(89)
try:
    run(mess.send_photo(89, b"x", "c")); ok(False, "фото заблокированному")
except B.DeliveryError:
    ok(True, "фото заблокированному: DeliveryError")
ok(B.CARD_RENDERER is not None, "рисовальщик карточек найден (Pillow и шрифт на месте)")
E.card_renderer = B.CARD_RENDERER
m = msg(8, GROUP); run(B.cmd_bunker(m))
c = cb(100, GROUP, "lobby:settings"); run(B.cb_lobby_settings(c)); ok(c.answer.items[0][1] is True, "настройки: только создатель")
c = cb(8, GROUP, "lobby:settings"); run(B.cb_lobby_settings(c))
kb = [mk for ch, t, mk, _ in fb.sent if ch == GROUP][-1]
ok(c.answer.items[0][1] is False and [b.callback_data for row in kb.inline_keyboard for b in row] ==
   ["set:pack", "set:tempo", "set:image", "set:outside", "set:done"], "меню настроек с кнопками")
for d in ("set:pack", "set:image", "set:outside", "set:tempo"):
    c = cb(8, GROUP, d); run(B.cb_setting(c)); ok(c.answer.items[0][1] is False, f"{d} переключается")
st = E.sessions[GROUP]
ok(st.pack == "fantasy" and st.image_cards and st.outside and st.tempo == "fast", "настройки применены")
for uid in range(100, 105):
    run(B.cmd_start(msg(uid, uid, "private", name=f"Игрок{uid}", username=f"user_{uid}"), Obj(args=f"join_{GROUP}")))
run(B.cb_lobby_start(cb(8, GROUP, "lobby:start")))
cards = [p for p in fb.photos if 100 <= p[0] <= 104]
ok(len(cards) == 5 and all(p[1].data[:4] == b"\x89PNG" for p in cards), "игрокам пришли карточки картинкой через Telegram-слой")
c = cb(8, GROUP, "set:pack"); run(B.cb_setting(c)); ok(c.answer.items[0][1] is True, "после старта настройки не меняются")
m = msg(8, GROUP); run(B.cmd_stop(m)); ok(GROUP not in E.sessions, "игра остановлена")

registered = {f.__name__ for _, f in B.router.messages} | {f.__name__ for _, f in B.router.callbacks}
expected = {"cmd_start", "cmd_help", "cmd_rules", "cmd_bunker", "cmd_stop", "cmd_table", "cmd_profile", "cmd_top",
            "cmd_history", "cb_lobby_leave", "cb_lobby_start", "cb_lobby_cancel", "cb_ready", "cb_vote", "cb_open",
            "cb_card_use", "cb_card_target", "cb_card_key", "cb_lobby_host", "cb_host", "group_guard",
            "cb_lobby_settings", "cb_setting"}
ok(expected <= registered, "все обработчики зарегистрированы в роутере")
ok(B.router.messages[-1][1].__name__ == "group_guard", "охрана чата зарегистрирована последней, после команд")
datas = set()
for c, t, mk, _ in fb.sent:
    if mk:
        for row in mk.inline_keyboard:
            for b in row:
                if getattr(b, "callback_data", None):
                    datas.add(b.callback_data)
ok(all(len(d.encode()) <= 64 for d in datas), "все callback_data укладываются в 64 байта (лимит Telegram)")
print(f"\nВСЕ {n_ok} ПРОВЕРОК СЛОЯ TELEGRAM ПРОЙДЕНЫ")
