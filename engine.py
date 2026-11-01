"""Движок игры «Бункер»: ведёт игру по фазам, таймерам и сообщениям.

Не зависит от aiogram: общается с Telegram через объект-мессенджер
(методы send и edit), поэтому его можно полностью проверить тестами.
"""
import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass

import game as G
from content import PACK_NAMES, PACK_ORDER, TAG_NAMES

log = logging.getLogger("bunker")

USERNAME_RE = re.compile(r"[A-Za-z0-9_]{4,32}")


class DeliveryError(Exception):
    """Сообщение не удалось доставить (например, человек не нажал «Старт» у бота)"""


@dataclass
class Btn:
    text: str
    data: str = None
    url: str = None


@dataclass
class Settings:
    min_players: int = 5
    max_players: int = 12
    open_seconds: int = 90
    discuss_seconds: int = 180
    vote_seconds: int = 60
    revote_seconds: int = 45
    edit_interval: float = 1.5     # не чаще одной правки сообщения за это время (лимиты Telegram)
    notice_interval: float = 60.0  # как часто напоминать зрителю, что идёт игра
    timer_tick: float = 10.0       # как часто обновлять отсчёт времени в сообщениях
    fast_factor: float = 1 / 3     # во сколько раз короче этапы в быстрой игре


TEMPO_NAMES = {"normal": "обычный", "fast": "быстрый"}


def fmt_time(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def on_off(flag):
    return "включено" if flag else "выключено"


def esc(value):
    return html.escape(str(value))


def mention_of(user_id, name, username=None):
    """Упоминание: @username, а если его нет — имя-ссылка на профиль"""
    if username and USERNAME_RE.fullmatch(username):
        return "@" + username
    return f'<a href="tg://user?id={int(user_id)}">{esc(name)}</a>'


def M(p):
    return mention_of(p.user_id, p.name, p.username)


def rows_of(buttons, per_row=2):
    rows, row = [], []
    for b in buttons:
        row.append(b)
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


RULES_TEXT = (
    "<b>Бункер: правила</b>\n\n"
    "Мир погиб, и вы все хотите попасть в бункер. Но мест в нём вдвое меньше, чем людей. "
    "Нужно убедить остальных, что именно вы должны остаться.\n\n"
    "<b>Как проходит игра</b>\n"
    "1. Каждый получает секретную карточку: профессия, биология, здоровье, хобби, фобия, характер, багаж, факт. "
    "Плюс одну особую карту.\n"
    "2. В первом раунде все раскрывают профессию. В следующих раундах каждый выбирает одну характеристику "
    "для раскрытия.\n"
    "3. После раскрытия идёт обсуждение: доказывайте свою пользу и ищите слабые места у других.\n"
    "4. Затем голосование. Исключённый игрок показывает все карточки и выбывает.\n"
    "5. Игра идёт, пока не останется столько человек, сколько мест в бункере.\n\n"
    "<b>Особые карты</b> применяются один раз во время обсуждения или голосования: двойной голос, "
    "иммунитет, замена характеристики, принудительное раскрытие, разведка.\n\n"
    "<b>Чат во время игры.</b> Писать могут только игроки, которые ещё в игре. Сообщения остальных, "
    "в том числе выбывших и ведущего, бот удаляет.\n\n"
    "<b>Ведущий.</b> Создатель набора может не играть, а вести игру: он видит все карточки и может "
    "заменять и раскрывать характеристики, исключать игроков, запускать события, ставить игру на паузу, "
    "добавлять время и завершать этапы досрочно.\n\n"
    "<b>Настройки.</b> Перед началом создатель выбирает набор сценариев (реализм, фантастика, абсурд, "
    "смешанный), темп, карточки картинкой и финал «снаружи», в котором исключённые образуют вторую группу "
    "и тоже могут победить.\n\n"
    "<b>Итог.</b> Бот оценивает шансы выживших: хватает ли им нужных навыков, есть ли будущее у группы, "
    "каково здоровье."
)

HOST_PANEL = [
    [Btn("Карточки игроков", "host:cards")],
    [Btn("Заменить характеристику", "host:replace"), Btn("Раскрыть характеристику", "host:reveal")],
    [Btn("Исключить игрока", "host:kick"), Btn("Событие", "host:event")],
    [Btn("Пауза / продолжить", "host:pause"), Btn("Добавить 30 секунд", "host:add")],
    [Btn("Завершить этап", "host:skip"), Btn("Остановить игру", "host:stop")],
]


class Session:
    def __init__(self, chat_id, creator_id, game, creator_name="", creator_username=None):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.creator_name = creator_name
        self.creator_username = creator_username
        self.host_mode = False
        self.game = game
        self.lobby_msg_id = None
        self.lock = asyncio.Lock()
        self.task = None
        self.open_event = asyncio.Event()
        self.ready_event = asyncio.Event()
        self.vote_event = asyncio.Event()
        self.ready_msg_id = None
        self.vote_msg_id = None
        self.started = False
        self.refresh_pending = {}
        self.last_edit = {}
        # настройки набора
        self.pack = "realism"
        self.tempo = "normal"
        self.image_cards = False
        self.outside = False
        self.settings_msg_id = None
        self.timings = {}
        # таймер этапа
        self.open_msg_id = None
        self.phase_kind = None
        self.deadline = None
        self.paused = False
        self.pause_left = 0.0
        self.resume_event = asyncio.Event()
        self.resume_event.set()

    def creator_mention(self):
        return mention_of(self.creator_id, self.creator_name or "Создатель", self.creator_username)


class Engine:
    def __init__(self, messenger, storage, settings=None, bot_username="bot", rng_factory=None,
                 card_renderer=None):
        self.m = messenger
        self.storage = storage
        self.cfg = settings or Settings()
        self.bot_username = bot_username
        self.sessions = {}
        self.user_chat = {}     # игрок -> чат его игры
        self.host_chat = {}     # ведущий -> чат игры, которую он ведёт
        self.rng_factory = rng_factory
        self.card_renderer = card_renderer   # функция (игрок, игра) -> PNG; None, если картинки недоступны
        self._notices = {}

    # ---------- отправка сообщений ----------

    async def _send(self, chat_id, text, buttons=None):
        try:
            return await self.m.send(chat_id, text, buttons)
        except DeliveryError as e:
            log.warning("Не удалось отправить сообщение в %s: %s", chat_id, e)
            return None

    async def _edit(self, chat_id, message_id, text, buttons=None):
        if message_id is None:
            return
        try:
            await self.m.edit(chat_id, message_id, text, buttons)
        except DeliveryError as e:
            log.warning("Не удалось изменить сообщение %s: %s", message_id, e)

    async def _dm(self, user_id, text, buttons=None):
        return await self._send(user_id, text, buttons)

    async def _timed_wait(self, s, event, seconds, kind):
        """Ожидание этапа с отсчётом, паузой и добавлением времени.
        Заканчивается по таймеру или когда событие установлено (все сходили, ведущий завершил этап)."""
        loop = asyncio.get_running_loop()
        s.phase_kind = kind
        if s.paused:
            s.pause_left = seconds
            s.deadline = None
        else:
            s.deadline = loop.time() + seconds
        try:
            while not event.is_set():
                if s.paused:
                    resume = asyncio.ensure_future(s.resume_event.wait())
                    done = asyncio.ensure_future(event.wait())
                    _, pending = await asyncio.wait({resume, done}, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    continue
                remaining = s.deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(self.cfg.timer_tick, remaining))
                except asyncio.TimeoutError:
                    pass
                if not event.is_set() and not s.paused:
                    self._schedule_refresh(s, kind)   # живой отсчёт
        finally:
            s.phase_kind = None
            s.deadline = None

    def _remaining(self, s):
        if s.paused:
            return s.pause_left
        if s.deadline is None:
            return None
        return max(0.0, s.deadline - asyncio.get_running_loop().time())

    def _time_line(self, s, seconds):
        """Строка с оставшимся временем для сообщений этапа"""
        if s.paused:
            return "Игра на паузе."
        left = self._remaining(s) if s.phase_kind else None
        return f"Осталось: {fmt_time(seconds if left is None else left)}"

    def _schedule_refresh(self, s, kind):
        """Отложенное обновление сообщения (лобби, готовность, голосование).
        Частые нажатия сливаются в одну правку, чтобы не упереться в лимиты Telegram."""
        if s.refresh_pending.get(kind):
            return
        s.refresh_pending[kind] = True
        asyncio.create_task(self._refresh_worker(s, kind))

    async def _refresh_worker(self, s, kind):
        loop = asyncio.get_running_loop()
        try:
            wait = s.last_edit.get(kind, 0) + self.cfg.edit_interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            g = s.game
            s.refresh_pending[kind] = False   # новые нажатия после этой точки попадут в следующую правку
            if kind == "lobby":
                if g.phase != G.LOBBY or self.sessions.get(s.chat_id) is not s:
                    return
                await self._edit(s.chat_id, s.lobby_msg_id, self._lobby_text(s), self._lobby_buttons(s))
            elif kind == "settings":
                if g.phase != G.LOBBY or self.sessions.get(s.chat_id) is not s:
                    return
                await self._edit(s.chat_id, s.settings_msg_id, self._settings_text(s), self._settings_buttons(s))
            elif kind == "open":
                if g.phase != G.OPENING:
                    return
                await self._edit(s.chat_id, s.open_msg_id, self._open_text(s))
            elif kind == "ready":
                if g.phase != G.DISCUSSION:
                    return
                await self._edit(s.chat_id, s.ready_msg_id, self._ready_text(s),
                                 [[Btn("Готов к голосованию", "ready")]])
            elif kind == "vote":
                if g.phase != G.VOTING:
                    return
                await self._edit(s.chat_id, s.vote_msg_id, self._vote_text(s), self._vote_buttons(g))
            s.last_edit[kind] = loop.time()
        except Exception:
            log.exception("Не удалось обновить сообщение (%s)", kind)
        finally:
            s.refresh_pending[kind] = False

    # ---------- тишина в чате ----------

    def is_silenced(self, chat_id, user_id):
        """Во время идущей игры писать в чат могут только игроки, которые ещё в игре"""
        s = self.sessions.get(chat_id)
        if not s or not s.started or s.game.phase in (G.LOBBY, G.ENDED):
            return False
        p = s.game.players.get(user_id) if user_id is not None else None
        return not (p and p.alive)

    def should_notify(self, chat_id, user_id):
        """Не чаще раза в notice_interval напоминаем человеку, что идёт игра"""
        now = time.monotonic()
        key = (chat_id, user_id)
        if now - self._notices.get(key, -1e9) < self.cfg.notice_interval:
            return False
        self._notices[key] = now
        return True

    # ---------- лобби ----------

    def _lobby_text(self, s):
        g = s.game
        lines = ["<b>Бункер. Набор игроков</b>",
                 f"Игроков: {len(g.players)}/{self.cfg.max_players}. "
                 f"Для старта нужно минимум {self.cfg.min_players}."]
        if s.host_mode:
            lines.append(f"Ведущий: {s.creator_mention()}. Не играет, управляет игрой.")
        lines.append(self._settings_summary(s))
        lines.append("")
        if g.players:
            for i, p in enumerate(g.players.values(), 1):
                lines.append(f"{i}. {M(p)}")
        else:
            lines.append("Пока никого нет.")
        lines += ["", "Чтобы войти, нажмите «Войти», затем в личных сообщениях бота нажмите «Старт». "
                      "Это нужно, чтобы бот мог отправить вам секретную карточку."]
        if not s.host_mode:
            lines.append("Создатель набора тоже входит этим способом, если играет. "
                         "Если он хочет только вести игру, нужно нажать «Режим ведущего».")
        return "\n".join(lines)

    def _lobby_buttons(self, s):
        link = f"https://t.me/{self.bot_username}?start=join_{s.chat_id}"
        host_label = "Режим ведущего: включён" if s.host_mode else "Режим ведущего: выключен"
        return [
            [Btn("Войти", url=link)],
            [Btn("Выйти", "lobby:leave"), Btn("Начать игру", "lobby:start")],
            [Btn(host_label, "lobby:host"), Btn("Настройки игры", "lobby:settings")],
            [Btn("Отменить", "lobby:cancel")],
        ]

    def _settings_summary(self, s):
        return (f"Набор: {PACK_NAMES[s.pack]}. Темп: {TEMPO_NAMES[s.tempo]}. "
                f"Карточки картинкой: {on_off(s.image_cards)}. Финал «снаружи»: {on_off(s.outside)}.")

    def _settings_text(self, s):
        return ("<b>Настройки игры</b>\n"
                "Менять настройки может только создатель набора, до начала игры.\n\n"
                f"<b>Набор сценариев:</b> {PACK_NAMES[s.pack]}. Реализм, фантастика, абсурд "
                f"или всё вместе.\n"
                f"<b>Темп:</b> {TEMPO_NAMES[s.tempo]}. В быстрой игре все этапы втрое короче.\n"
                f"<b>Карточки картинкой:</b> {on_off(s.image_cards)}. Личная карточка приходит изображением.\n"
                f"<b>Финал «снаружи»:</b> {on_off(s.outside)}. Исключённые образуют вторую группу, "
                f"в конце бот оценивает обе, и победить могут выгнанные.")

    def _settings_buttons(self, s):
        return [
            [Btn(f"Набор: {PACK_NAMES[s.pack]}", "set:pack"), Btn(f"Темп: {TEMPO_NAMES[s.tempo]}", "set:tempo")],
            [Btn(f"Картинки: {'да' if s.image_cards else 'нет'}", "set:image"),
             Btn(f"Снаружи: {'да' if s.outside else 'нет'}", "set:outside")],
            [Btn("Готово", "set:done")],
        ]

    async def open_settings(self, chat_id, user_id):
        s = self.sessions.get(chat_id)
        if not s or s.game.phase != G.LOBBY:
            return False, "Набор игроков не открыт."
        if user_id != s.creator_id:
            return False, "Настройки может менять только создатель набора."
        if s.settings_msg_id is not None:
            await self._edit(chat_id, s.settings_msg_id, "Настройки открыты ниже.")
        s.settings_msg_id = await self._send(chat_id, self._settings_text(s), self._settings_buttons(s))
        return True, "Настройки открыты."

    async def on_setting(self, chat_id, user_id, what):
        s = self.sessions.get(chat_id)
        if not s or s.game.phase != G.LOBBY:
            return False, "Игра уже началась, настройки менять нельзя."
        if user_id != s.creator_id:
            return False, "Настройки может менять только создатель набора."
        if what == "pack":
            s.pack = PACK_ORDER[(PACK_ORDER.index(s.pack) + 1) % len(PACK_ORDER)]
            note = f"Набор: {PACK_NAMES[s.pack]}."
        elif what == "tempo":
            s.tempo = "fast" if s.tempo == "normal" else "normal"
            note = f"Темп: {TEMPO_NAMES[s.tempo]}."
        elif what == "image":
            if not s.image_cards and self.card_renderer is None:
                return False, "Карточки картинкой сейчас недоступны на сервере."
            s.image_cards = not s.image_cards
            note = f"Карточки картинкой: {on_off(s.image_cards)}."
        elif what == "outside":
            s.outside = not s.outside
            note = f"Финал «снаружи»: {on_off(s.outside)}."
        elif what == "done":
            await self._edit(chat_id, s.settings_msg_id, "<b>Настройки сохранены.</b>\n" + self._settings_summary(s))
            s.settings_msg_id = None
            return True, "Настройки сохранены."
        else:
            return False, "Неизвестная настройка."
        self._schedule_refresh(s, "settings")
        self._schedule_refresh(s, "lobby")
        return True, note

    async def create_lobby(self, chat_id, user_id, name="", username=None):
        if chat_id in self.sessions:
            return False, "В этом чате уже идёт игра или набор игроков."
        game = G.BunkerGame(self.rng_factory() if self.rng_factory else None)
        s = Session(chat_id, user_id, game, name, username)
        self.sessions[chat_id] = s
        s.lobby_msg_id = await self._send(chat_id, self._lobby_text(s), self._lobby_buttons(s))
        return True, ""

    def _busy_elsewhere(self, user_id, chat_id):
        for where in (self.user_chat.get(user_id), self.host_chat.get(user_id)):
            if where is not None and where != chat_id:
                return True
        return False

    async def join(self, chat_id, user_id, name, username=None):
        s = self.sessions.get(chat_id)
        if not s or s.game.phase != G.LOBBY:
            return False, "Набор игроков в этом чате уже закрыт или не открыт."
        if s.host_mode and user_id == s.creator_id:
            return False, "Вы ведущий и не участвуете в игре. Чтобы играть, выключите режим ведущего в наборе."
        if self._busy_elsewhere(user_id, chat_id):
            return False, "Вы уже участвуете в другой игре."
        async with s.lock:
            ok, err = s.game.add_player(user_id, name, self.cfg.max_players, username)
            if not ok:
                return False, err
            self.user_chat[user_id] = chat_id
        self._schedule_refresh(s, "lobby")
        return True, "Вы записаны в игру. Дождитесь начала в групповом чате."

    async def leave(self, chat_id, user_id):
        s = self.sessions.get(chat_id)
        if not s:
            return False, "Набор игроков не открыт."
        async with s.lock:
            ok, err = s.game.remove_player(user_id)
            if not ok:
                return False, err
            self.user_chat.pop(user_id, None)
        self._schedule_refresh(s, "lobby")
        return True, "Вы вышли из игры."

    async def toggle_host(self, chat_id, user_id):
        s = self.sessions.get(chat_id)
        if not s or s.game.phase != G.LOBBY:
            return False, "Набор игроков не открыт."
        if user_id != s.creator_id:
            return False, "Режим ведущего может включить только создатель набора."
        if s.host_mode:
            s.host_mode = False
            self.host_chat.pop(user_id, None)
            self._schedule_refresh(s, "lobby")
            return True, "Режим ведущего выключен. Чтобы играть, нажмите «Войти»."
        if self._busy_elsewhere(user_id, chat_id):
            return False, "Вы уже участвуете в другой игре."
        mid = await self._dm(user_id, "Вы ведущий игры «Бункер». Вы не играете. Когда игра начнётся, "
                                      "здесь появится панель ведущего: карточки всех игроков и управление игрой.")
        if mid is None:
            return False, ("Сначала откройте бота в личных сообщениях и нажмите «Старт». "
                           "Без этого бот не сможет прислать вам панель ведущего.")
        async with s.lock:
            if user_id in s.game.players:
                s.game.remove_player(user_id)
                self.user_chat.pop(user_id, None)
            s.host_mode = True
        self.host_chat[user_id] = chat_id
        self._schedule_refresh(s, "lobby")
        return True, "Режим ведущего включён. Вы не играете и получите панель управления в личных сообщениях."

    async def cancel(self, chat_id, user_id, is_admin=False):
        s = self.sessions.get(chat_id)
        if not s:
            return False, "Здесь нет активной игры."
        if user_id != s.creator_id and not is_admin:
            return False, "Остановить игру может создатель или администратор чата."
        was_running = s.started
        if s.task and not s.task.done() and s.task is not asyncio.current_task():
            s.task.cancel()
        self._cleanup(s)
        if was_running:
            await self._send(chat_id, "Игра остановлена. Писать в чат снова могут все.")
        else:
            await self._edit(chat_id, s.lobby_msg_id, "<b>Бункер.</b> Набор игроков отменён.")
        return True, "Игра остановлена."

    def _cleanup(self, s):
        for uid in list(s.game.players):
            if self.user_chat.get(uid) == s.chat_id:
                self.user_chat.pop(uid, None)
        if self.host_chat.get(s.creator_id) == s.chat_id:
            self.host_chat.pop(s.creator_id, None)
        if self.sessions.get(s.chat_id) is s:
            del self.sessions[s.chat_id]
        s.game.phase = G.ENDED

    # ---------- старт ----------

    def _card_text(self, p):
        lines = ["<b>Ваша секретная карточка</b>", ""]
        for key in G.TRAIT_KEYS:
            lines.append(f"<b>{G.TRAIT_TITLES[key]}:</b> {esc(p.traits[key].text)}")
        title, desc, _, _ = G.CARDS[p.card]
        lines += ["", f"<b>Особая карта: {title}</b>", desc,
                  "", "Использовать её можно один раз во время обсуждения или голосования."]
        return "\n".join(lines)

    async def _send_card(self, s, p):
        """Личная карточка: картинкой, если включено, иначе текстом. Возвращает id сообщения или None."""
        buttons = [[Btn("Использовать особую карту", "card:use")]]
        if s.image_cards and self.card_renderer is not None:
            try:
                png = await asyncio.to_thread(self.card_renderer, p, s.game)
            except Exception:
                log.exception("Не удалось нарисовать карточку, отправляю текстом")
            else:
                title, desc, _, _ = G.CARDS[p.card]
                caption = (f"<b>Ваша секретная карточка</b>\n\n<b>Особая карта: {title}</b>\n{desc}\n\n"
                           f"Использовать её можно один раз во время обсуждения или голосования.")
                try:
                    return await self.m.send_photo(p.user_id, png, caption, buttons)
                except DeliveryError as e:
                    log.warning("Не удалось отправить карточку-картинку %s: %s", p.user_id, e)
                    return None
        return await self._dm(p.user_id, self._card_text(p), buttons)

    async def start(self, chat_id, user_id, is_admin=False):
        s = self.sessions.get(chat_id)
        if not s or s.game.phase != G.LOBBY:
            return False, "Набор игроков не открыт."
        if user_id != s.creator_id and not is_admin:
            return False, "Начать игру может только создатель."
        g = s.game
        async with s.lock:
            g.pack = s.pack
            g.outside = s.outside
            try:
                g.start(self.cfg.min_players)
            except ValueError as e:
                return False, str(e)
            s.started = True
            factor = self.cfg.fast_factor if s.tempo == "fast" else 1
            s.timings = {
                "open": self.cfg.open_seconds * factor,
                "discuss": self.cfg.discuss_seconds * factor,
                "vote": self.cfg.vote_seconds * factor,
                "revote": self.cfg.revote_seconds * factor,
            }
        if s.settings_msg_id is not None:
            await self._edit(chat_id, s.settings_msg_id, "<b>Настройки игры.</b>\n" + self._settings_summary(s))
            s.settings_msg_id = None

        await self._edit(chat_id, s.lobby_msg_id, self._lobby_text(s).replace(
            "<b>Бункер. Набор игроков</b>", "<b>Бункер. Набор закрыт</b>", 1))

        failed = []
        for p in g.players.values():
            mid = await self._send_card(s, p)
            if mid is None:
                failed.append(p)
        if failed:
            names = ", ".join(M(p) for p in failed)
            self._cleanup(s)
            await self._send(chat_id, f"Не удалось отправить карточки: {names}. Этим игрокам нужно открыть бота "
                                      f"и нажать «Старт». Начните набор заново командой /bunker.")
            return False, "Не удалось отправить карточки."

        need = ", ".join(TAG_NAMES.get(t, t) for t in g.needs)
        to_remove = len(g.players) - g.seats
        host_line = f"Ведущий: {s.creator_mention()}.\n" if s.host_mode else ""
        outside_line = ("Финал «снаружи»: исключённые образуют вторую группу. В конце бот оценит обе группы, "
                        "и победить могут те, кого выгнали.\n" if s.outside else "")
        text = (f"<b>Игра началась</b>\n\n"
                f"<b>Катастрофа: {esc(g.catastrophe['name'])}.</b> {esc(g.catastrophe['text'])}\n\n"
                f"<b>Бункер: {esc(g.bunker['name'])}.</b> {esc(g.bunker['text'])} "
                f"Припасов хватит на {g.bunker['years']} {G._age_word(g.bunker['years'])}.\n\n"
                f"Мест в бункере: <b>{g.seats}</b>. Игроков: <b>{len(g.players)}</b>. "
                f"Нужно исключить: <b>{to_remove}</b>.\n"
                f"Что понадобится выжившим: {esc(need)}.\n"
                f"Набор: {PACK_NAMES[s.pack]}. Темп: {TEMPO_NAMES[s.tempo]}.\n{host_line}{outside_line}\n"
                f"Секретные карточки отправлены в личные сообщения. До конца игры писать в чат могут "
                f"только игроки, которые ещё в игре.")
        await self._send(chat_id, text)

        if s.host_mode:
            mid = await self._dm(s.creator_id, "<b>Панель ведущего</b>\nИгра началась. Все действия ведущего "
                                               "объявляются в групповом чате.", HOST_PANEL)
            if mid is None:
                await self._send(chat_id, "Не удалось отправить панель ведущему. Ведущему нужно открыть бота и "
                                          "нажать «Старт». Игра продолжается без панели.")
        s.task = asyncio.create_task(self._run(s))
        return True, ""

    # ---------- основной цикл ----------

    async def _run(self, s):
        g = s.game
        try:
            while not g.finished():
                async with s.lock:
                    revealed = g.begin_round()
                alive = len(g.alive())
                header = (f"<b>Раунд {g.round}.</b> В игре {alive}, мест в бункере {g.seats}. "
                          f"В этом раунде будет исключён один игрок.")
                await self._send(s.chat_id, header)

                if g.round == 1:
                    lines = ["Первый раунд: все раскрывают профессию.", ""]
                    for p, _, trait in revealed:
                        lines.append(f"{M(p)}: {esc(trait.text)}")
                    await self._send(s.chat_id, "\n".join(lines))
                else:
                    await self._opening_phase(s)
                if g.finished():
                    break

                await self._send(s.chat_id, self.table_text(g))
                await self._discussion_phase(s)
                if g.finished():
                    break
                await self._voting_phase(s)
            await self._finish(s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в игре %s", s.chat_id)
            await self._send(s.chat_id, "Игра остановлена из-за внутренней ошибки.")
            self._cleanup(s)

    async def _opening_phase(self, s):
        g = s.game
        s.open_event.clear()
        s.open_msg_id = await self._send(s.chat_id, self._open_text(s))
        for p in g.alive():
            buttons = [Btn(G.TRAIT_TITLES[k], f"open:{g.round}:{k}") for k in g.unopened_keys(p.user_id)]
            await self._dm(p.user_id, f"Раунд {g.round}. Выберите характеристику, которую вы раскроете:",
                           rows_of(buttons))
        await self._timed_wait(s, s.open_event, s.timings["open"], "open")
        async with s.lock:
            auto = g.auto_open_pending()
            g.end_opening()
        await self._edit(s.chat_id, s.open_msg_id, "<b>Раскрытие завершено.</b>")
        for p, key, trait in auto:
            await self._dm(p.user_id, f"Время на выбор вышло. Раскрыта случайная характеристика: "
                                      f"{G.TRAIT_TITLES[key]}.")
        lines = [f"<b>Раскрыто в раунде {g.round}</b>"]
        for p in g.alive():
            key = g.round_keys.get(p.user_id)
            if key:
                lines.append(f"{M(p)}. {G.TRAIT_TITLES[key]}: {esc(p.traits[key].text)}")
        await self._send(s.chat_id, "\n".join(lines))

    def _open_text(self, s):
        g = s.game
        chosen = sum(1 for p in g.alive() if p.user_id in g.opened_this_round)
        return (f"<b>Раскрытие характеристик.</b> Выберите в личных сообщениях бота, какую характеристику "
                f"показать остальным. Кто не успеет, раскроет случайную.\n\n"
                f"Выбрали: {chosen}/{len(g.alive())}\n{self._time_line(s, s.timings['open'])}")

    def _ready_text(self, s):
        g = s.game
        return (f"<b>Обсуждение.</b> Приводите аргументы и задавайте вопросы. "
                f"Когда большинство нажмёт «Готов к голосованию», обсуждение закончится раньше. "
                f"Особую карту можно применить сейчас или во время голосования.\n\n"
                f"Готовы: {len(g.ready)}/{g.ready_needed()}\n{self._time_line(s, s.timings['discuss'])}")

    async def _discussion_phase(self, s):
        g = s.game
        s.ready_event.clear()
        s.ready_msg_id = await self._send(s.chat_id, self._ready_text(s), [[Btn("Готов к голосованию", "ready")]])
        await self._timed_wait(s, s.ready_event, s.timings["discuss"], "ready")
        await self._edit(s.chat_id, s.ready_msg_id, "<b>Обсуждение завершено.</b>")

    def _vote_buttons(self, g):
        pool = g.alive() if g.candidates is None else [g.players[u] for u in g.candidates if g.players[u].alive]
        buttons = [Btn(p.name[:30], f"vote:{p.user_id}") for p in pool if p.user_id not in g.immune]
        return rows_of(buttons)

    def _vote_text(self, s):
        g = s.game
        revote = g.candidates is not None
        title = "Повторное голосование." if revote else "Голосование."
        seconds = s.timings["revote"] if revote else s.timings["vote"]
        voted = sum(1 for p in g.alive() if p.user_id in g.votes)
        return (f"<b>{title}</b> Выберите, кого исключить из бункера. "
                f"Голос можно изменить до конца голосования.\n\n"
                f"Проголосовали: {voted}/{len(g.alive())}\n{self._time_line(s, seconds)}")

    async def _voting_phase(self, s):
        g = s.game
        async with s.lock:
            g.begin_voting()
        s.vote_event.clear()
        s.vote_msg_id = await self._send(s.chat_id, self._vote_text(s), self._vote_buttons(g))
        await self._timed_wait(s, s.vote_event, s.timings["vote"], "vote")
        await self._edit(s.chat_id, s.vote_msg_id, "<b>Голосование завершено.</b>")
        if g.finished():          # ведущий исключил игрока и мест стало достаточно
            return
        async with s.lock:
            result = g.tally()

        if result.kind == "tie":
            names = ", ".join(M(p) for p in result.tied)
            await self._send(s.chat_id, f"Голоса разделились поровну между: {names}. "
                                        f"Проводится повторное голосование только между ними.")
            async with s.lock:
                g.start_revote(result.tied)
            s.vote_event.clear()
            s.vote_msg_id = await self._send(s.chat_id, self._vote_text(s), self._vote_buttons(g))
            await self._timed_wait(s, s.vote_event, s.timings["revote"], "vote")
            await self._edit(s.chat_id, s.vote_msg_id, "<b>Повторное голосование завершено.</b>")
            if g.finished():
                return
            async with s.lock:
                result = g.tally()

        lines = ["<b>Итоги голосования</b>"]
        for uid, count in sorted(result.counts.items(), key=lambda kv: -kv[1]):
            who = ", ".join(M(g.players[v]) + (" (x2)" if w == 2 else "") for v, w in result.voters.get(uid, []))
            lines.append(f"{M(g.players[uid])}: {count} ({who})")
        if not result.counts:
            lines.append("Голоса не поданы.")
        if result.note:
            lines.append(result.note)
        await self._send(s.chat_id, "\n".join(lines))

        async with s.lock:
            # пока объявлялись итоги, ведущий мог исключить этого игрока или закончить игру
            if not result.player.alive or g.finished():
                return
            out = g.eliminate(result.player.user_id)
        await self._announce_out(s, out, f"Из бункера исключён: {M(out)}.")

    async def _announce_out(self, s, out, headline):
        self.user_chat.pop(out.user_id, None)
        card = ["<b>Все характеристики исключённого игрока</b>"]
        for key in G.TRAIT_KEYS:
            card.append(f"{G.TRAIT_TITLES[key]}: {esc(out.traits[key].text)}")
        await self._send(s.chat_id, f"{headline}\n\n" + "\n".join(card))
        if s.game.outside:
            await self._dm(out.user_id, "Вы исключены из бункера и теперь в группе снаружи. В финале бот оценит "
                                        "и её шансы: если они окажутся выше, победите вы. Писать в групповой чат "
                                        "до конца игры нельзя.")
        else:
            await self._dm(out.user_id, "Вы исключены из бункера. Вы можете наблюдать за игрой, "
                                        "но писать в групповой чат до конца игры нельзя.")

    # ---------- итог ----------

    def _tag_list(self, tags):
        return ", ".join(TAG_NAMES.get(t, t) for t in tags) or "нет"

    def _group_report(self, title, members, v, outside):
        lines = [f"<b>{title}</b>"]
        if members:
            for p in members:
                lines.append(f"{M(p)}: {esc(p.traits['profession'].text)}")
        else:
            lines.append("никого")
        if not outside:
            lines.append(f"Срок пребывания в бункере: {v['years']} {G._age_word(v['years'])}.")
        lines += [f"Нужные навыки закрыты: {self._tag_list(v['covered'])}.",
                  f"Не закрыты: {self._tag_list(v['missing'])}.",
                  "Перспективы продолжения рода: " +
                  ("есть." if v["future_ok"] else "под вопросом: в группе нет подходящей пары.")]
        if v["severe"]:
            lines.append("Тяжёлые состояния здоровья: " + ", ".join(M(p) for p in v["severe"]) + ".")
        if v["contagious"]:
            lines.append("Заразные заболевания: " + ", ".join(M(p) for p in v["contagious"]) + ".")
        lines.append(f"Шансы на выживание: <b>{v['rating']}</b> ({v['score']} из 100).")
        return lines

    async def _finish(self, s):
        g = s.game
        g.finish()
        survivors = g.alive()
        outsiders = [g.players[u] for u in g.elimination_order]
        inside, outside, winner = g.final_result()
        lines = ["<b>Игра окончена</b>", ""]
        lines += self._group_report("В бункер попали:", survivors, inside, outside=False)
        if outside is not None:
            lines += [""] + self._group_report("Группа снаружи:", outsiders, outside, outside=True)
            lines += ["", "<b>Победила группа в бункере.</b>" if winner == "inside" else
                      "<b>Победила группа снаружи.</b> Исключённые оказались приспособлены к выживанию лучше."]
        lines += ["", "Писать в чат снова могут все."]
        await self._send(s.chat_id, "\n".join(lines))

        cards = ["<b>Карточки выживших</b>"]
        for p in survivors:
            cards.append(f"\n<b>{esc(p.name)}</b>")
            for key in G.TRAIT_KEYS:
                cards.append(f"{G.TRAIT_TITLES[key]}: {esc(p.traits[key].text)}")
        await self._send(s.chat_id, "\n".join(cards))

        try:
            self.storage.record_game(
                [(p.user_id, p.name, p.alive, p.traits["profession"].text, p.alive == (winner == "inside"))
                 for p in g.players.values()],
                g.catastrophe["name"], g.seats, inside["rating"], winner)
        except Exception:
            log.exception("Не удалось сохранить статистику")
        self._cleanup(s)

    # ---------- действия игроков ----------

    def _session_of_user(self, user_id):
        chat_id = self.user_chat.get(user_id)
        return self.sessions.get(chat_id) if chat_id is not None else None

    async def on_open(self, user_id, key, round_no=None):
        s = self._session_of_user(user_id)
        if not s or not s.started:
            return False, "У вас нет активной игры."
        if round_no is not None and round_no != s.game.round:
            return False, "Эти кнопки относятся к прошлому раунду."
        async with s.lock:
            ok, err = s.game.open_trait(user_id, key)
            if not ok:
                return False, err
            all_done = not s.game.pending_openers()
        if all_done:
            s.open_event.set()
        else:
            self._schedule_refresh(s, "open")
        return True, f"Вы раскрываете: {G.TRAIT_TITLES[key]}. Результат появится в групповом чате."

    async def on_ready(self, chat_id, user_id):
        s = self.sessions.get(chat_id)
        if not s or not s.started:
            return False, "Игра не идёт."
        async with s.lock:
            ok, err = s.game.set_ready(user_id)
            if not ok:
                return False, err
            done = s.game.discussion_done()
        if done:
            s.ready_event.set()
        else:
            self._schedule_refresh(s, "ready")
        return True, "Ваша готовность учтена."

    async def on_vote(self, chat_id, user_id, target_id):
        s = self.sessions.get(chat_id)
        if not s or not s.started:
            return False, "Игра не идёт."
        async with s.lock:
            ok, err = s.game.cast_vote(user_id, target_id)
            if not ok:
                return False, err
            all_done = s.game.all_voted()
            name = s.game.players[target_id].name
        if all_done:
            s.vote_event.set()
        else:
            self._schedule_refresh(s, "vote")
        return True, f"Ваш голос: {name}."

    # ---------- особые карты ----------

    async def on_card_use(self, user_id):
        s = self._session_of_user(user_id)
        if not s or not s.started:
            return False, "У вас нет активной игры."
        g = s.game
        p = g.players.get(user_id)
        if not p or not p.alive:
            return False, "Вы не участвуете в игре."
        if p.card_used:
            return False, "Особая карта уже использована."
        if g.phase not in (G.DISCUSSION, G.VOTING):
            return False, "Карту можно использовать во время обсуждения и голосования."
        _, _, needs_target, needs_key = G.CARDS[p.card]
        if needs_target:
            buttons = [Btn(o.name[:30], f"card:t:{o.user_id}") for o in g.alive()
                       if o.user_id != user_id and g.unopened_keys(o.user_id)]
            if not buttons:
                return False, "Нет подходящих целей."
            await self._dm(user_id, "Выберите игрока:", rows_of(buttons))
            return True, "Выберите цель в сообщении ниже."
        if needs_key:
            buttons = [Btn(G.TRAIT_TITLES[k], f"card:k:{k}") for k in g.unopened_keys(user_id)]
            if not buttons:
                return False, "Все ваши характеристики уже раскрыты."
            await self._dm(user_id, "Выберите характеристику для замены:", rows_of(buttons))
            return True, "Выберите характеристику в сообщении ниже."
        return await self._play(s, user_id)

    async def on_card_target(self, user_id, target_id):
        s = self._session_of_user(user_id)
        if not s or not s.started:
            return False, "У вас нет активной игры."
        return await self._play(s, user_id, target_id=target_id)

    async def on_card_key(self, user_id, key):
        s = self._session_of_user(user_id)
        if not s or not s.started:
            return False, "У вас нет активной игры."
        return await self._play(s, user_id, key=key)

    async def _play(self, s, user_id, target_id=None, key=None):
        g = s.game
        async with s.lock:
            res = g.play_card(user_id, target_id=target_id, key=key)
            in_voting = res.ok and g.phase == G.VOTING
        if not res.ok:
            return False, res.error
        actor = g.players[user_id]
        if res.code == "immunity":
            await self._send(s.chat_id, f"{M(actor)} использует карту «Иммунитет»: голоса против него "
                                        f"в этом раунде не учитываются.")
        elif res.code == "reveal":
            target = g.players[res.target_id]
            await self._send(s.chat_id, f"{M(actor)} использует карту «Принудительное раскрытие». "
                                        f"{M(target)} раскрывает характеристику «{G.TRAIT_TITLES[res.key]}»: "
                                        f"{esc(target.traits[res.key].text)}")
        if res.private:
            await self._dm(user_id, esc(res.private))
        if in_voting:
            self._schedule_refresh(s, "vote")   # иммунитет убирает кнопку и меняет счётчик
        return True, "Карта использована."

    # ---------- панель ведущего ----------

    def _host_session(self, user_id):
        s = self.sessions.get(self.host_chat.get(user_id))
        if not s or not s.host_mode or s.creator_id != user_id or not s.started or s.game.phase == G.ENDED:
            return None
        return s

    def _host_cards_text(self, g):
        lines = ["<b>Карточки игроков</b>", "Отмечено, что уже раскрыто."]
        for p in sorted(g.players.values(), key=lambda x: not x.alive):
            status = "в игре" if p.alive else "исключён"
            lines.append(f"\n<b>{esc(p.name)}</b>" + (f" (@{p.username})" if p.username else "") + f", {status}")
            for key in G.TRAIT_KEYS:
                mark = " [раскрыто]" if key in p.opened else ""
                lines.append(f"{G.TRAIT_TITLES[key]}: {esc(p.traits[key].text)}{mark}")
            title = G.CARDS[p.card][0]
            lines.append(f"Особая карта: {title}" + (" (использована)" if p.card_used else ""))
        return "\n".join(lines)

    def _player_buttons(self, g, prefix, only=None):
        players = [p for p in g.alive() if only is None or only(p)]
        return rows_of([Btn(p.name[:30], f"host:{prefix}:{p.user_id}") for p in players])

    async def on_host(self, user_id, data):
        """Обработка кнопок панели ведущего. Возвращает (успех, всплывающий текст)."""
        s = self._host_session(user_id)
        if not s:
            return False, "Панель доступна только ведущему идущей игры, который в ней не участвует."
        g = s.game
        parts = data.split(":")
        cmd = parts[1] if len(parts) > 1 else ""

        if cmd == "event":
            buttons = [[Btn(name, f"host:ev:{code}")] for code, (name, _) in G.EVENTS.items()]
            text = "<b>События</b>\n" + "\n".join(f"<b>{name}.</b> {desc}" for name, desc in G.EVENTS.values())
            await self._dm(user_id, text, buttons)
            return True, "Выберите событие."
        if cmd == "ev":
            return await self._host_event(s, parts[2] if len(parts) > 2 else "")
        if cmd == "pause":
            return await self._host_pause(s)
        if cmd == "add":
            return await self._host_add_time(s, 30)

        try:
            args = [int(parts[2])] if len(parts) > 2 else []
        except ValueError:
            return False, "Некорректная кнопка."
        key = parts[3] if len(parts) > 3 else None

        if cmd == "cards":
            await self._dm(user_id, self._host_cards_text(g))
            return True, "Карточки отправлены."

        if cmd == "replace":
            await self._dm(user_id, "Кому заменить характеристику?", self._player_buttons(g, "rp"))
            return True, "Выберите игрока."
        if cmd == "rp" and args:
            p = g.players.get(args[0])
            if not p or not p.alive:
                return False, "Игрок уже не участвует."
            buttons = [Btn(G.TRAIT_TITLES[k] + (" (раскрыта)" if k in p.opened else ""), f"host:rk:{p.user_id}:{k}")
                       for k in G.TRAIT_KEYS]
            await self._dm(user_id, f"Какую характеристику заменить у {esc(p.name)}?", rows_of(buttons))
            return True, "Выберите характеристику."
        if cmd == "rk" and args and key:
            async with s.lock:
                p = g.players.get(args[0])
                try:
                    old, new = g.replace_trait(args[0], key)
                except ValueError as e:
                    return False, str(e)
                was_open = key in p.opened
            title = G.TRAIT_TITLES[key]
            if was_open:
                await self._send(s.chat_id, f"Ведущий заменил характеристику «{title}» у {M(p)}. "
                                            f"Новое значение: {esc(new)}")
            else:
                await self._send(s.chat_id, f"Ведущий заменил одну из нераскрытых характеристик у {M(p)}.")
            await self._dm(p.user_id, f"Ведущий заменил вашу характеристику «{title}».\n"
                                      f"Было: {esc(old)}\nСтало: {esc(new)}")
            return True, "Характеристика заменена."

        if cmd == "reveal":
            buttons = self._player_buttons(g, "rv", only=lambda p: g.unopened_keys(p.user_id))
            if not buttons:
                return False, "У всех игроков всё уже раскрыто."
            await self._dm(user_id, "Чью характеристику раскрыть?", buttons)
            return True, "Выберите игрока."
        if cmd == "rv" and args:
            p = g.players.get(args[0])
            if not p or not p.alive:
                return False, "Игрок уже не участвует."
            buttons = [Btn(G.TRAIT_TITLES[k], f"host:rvk:{p.user_id}:{k}") for k in g.unopened_keys(p.user_id)]
            if not buttons:
                return False, "У этого игрока всё уже раскрыто."
            await self._dm(user_id, f"Какую характеристику раскрыть у {esc(p.name)}?", rows_of(buttons))
            return True, "Выберите характеристику."
        if cmd == "rvk" and args and key:
            async with s.lock:
                p = g.players.get(args[0])
                try:
                    value = g.force_open(args[0], key)
                except ValueError as e:
                    return False, str(e)
            await self._send(s.chat_id, f"Ведущий раскрыл характеристику «{G.TRAIT_TITLES[key]}» у {M(p)}: "
                                        f"{esc(value)}")
            return True, "Характеристика раскрыта."

        if cmd == "kick":
            await self._dm(user_id, "Кого исключить из игры?", self._player_buttons(g, "kc"))
            return True, "Выберите игрока."
        if cmd == "kc" and args:
            p = g.players.get(args[0])
            if not p or not p.alive:
                return False, "Игрок уже не участвует."
            await self._dm(user_id, f"Исключить {esc(p.name)} из игры? Это нельзя отменить.",
                           [[Btn("Да, исключить", f"host:ko:{p.user_id}")]])
            return True, "Подтвердите исключение."
        if cmd == "ko" and args:
            async with s.lock:
                if g.finished():
                    return False, "Игра уже завершается."
                try:
                    out = g.kick(args[0])
                except ValueError as e:
                    return False, str(e)
                if g.phase == G.OPENING and not g.pending_openers():
                    s.open_event.set()
                if g.phase == G.DISCUSSION and g.discussion_done():
                    s.ready_event.set()
                if g.phase == G.VOTING and g.all_voted():
                    s.vote_event.set()
                ended = g.finished()
            await self._announce_out(s, out, f"Ведущий исключил из игры: {M(out)}.")
            if ended:
                s.open_event.set(); s.ready_event.set(); s.vote_event.set()
            elif g.phase == G.VOTING:
                self._schedule_refresh(s, "vote")
            return True, "Игрок исключён."

        if cmd == "skip":
            stages = {G.OPENING: (s.open_event, "раскрытие"), G.DISCUSSION: (s.ready_event, "обсуждение"),
                      G.VOTING: (s.vote_event, "голосование")}
            if g.phase not in stages:
                return False, "Сейчас нечего завершать."
            event, name = stages[g.phase]
            if event.is_set():
                return False, "Этап уже завершается."
            event.set()
            await self._send(s.chat_id, f"Ведущий завершил {name} досрочно.")
            return True, "Этап завершён."

        if cmd == "stop":
            await self._dm(user_id, "Остановить игру? Итоги подводиться не будут.",
                           [[Btn("Да, остановить", "host:so")]])
            return True, "Подтвердите остановку."
        if cmd == "so":
            ok, text = await self.cancel(s.chat_id, user_id)
            return ok, text

        return False, "Неизвестное действие."

    async def _host_pause(self, s):
        loop = asyncio.get_running_loop()
        if s.paused:
            s.paused = False
            if s.phase_kind:
                s.deadline = loop.time() + s.pause_left
            s.resume_event.set()
            await self._send(s.chat_id, "Ведущий снял игру с паузы.")
            if s.phase_kind:
                self._schedule_refresh(s, s.phase_kind)
            return True, "Игра продолжается."
        s.pause_left = max(0.0, s.deadline - loop.time()) if s.deadline is not None else 0.0
        s.paused = True
        s.resume_event.clear()
        await self._send(s.chat_id, "Ведущий поставил игру на паузу. Отсчёт времени остановлен.")
        if s.phase_kind:
            self._schedule_refresh(s, s.phase_kind)
        return True, "Игра на паузе."

    async def _host_add_time(self, s, seconds):
        if not s.phase_kind:
            return False, "Сейчас нет этапа с отсчётом времени."
        if s.paused:
            s.pause_left += seconds
        elif s.deadline is not None:
            s.deadline += seconds
        await self._send(s.chat_id, f"Ведущий добавил {seconds} секунд к текущему этапу.")
        self._schedule_refresh(s, s.phase_kind)
        return True, f"Добавлено {seconds} секунд."

    async def _host_event(self, s, code):
        g = s.game
        async with s.lock:
            if g.finished():
                return False, "Игра уже завершается."
            try:
                r = g.apply_event(code)
            except ValueError as e:
                return False, str(e)
            ended = g.finished()
        name = G.EVENTS[code][0]
        head = f"<b>Событие: {name}.</b> "
        if code == "fire":
            text = head + f"Сгорела часть припасов. Мест в бункере теперь: <b>{r['seats']}</b>."
        elif code == "room":
            text = head + f"Мест в бункере теперь: <b>{r['seats']}</b>."
            if ended:
                text += " Мест хватает всем, кто остался в игре."
        elif code in ("quake", "stash"):
            text = head + f"Припасов хватит на {r['years']} {G._age_word(r['years'])}."
        elif code == "outbreak":
            victim = r["player"]
            if r["public"]:
                text = head + f"У {M(victim)} ухудшилось здоровье: {esc(r['new'])}."
            else:
                text = head + "У одного из игроков ухудшилось здоровье. Кто это, знает только он."
            await self._dm(victim.user_id, f"Событие «{name}». Ваше здоровье изменилось.\n"
                                           f"Было: {esc(r['old'])}\nСтало: {esc(r['new'])}")
        elif code in ("medcheck", "search"):
            title = G.TRAIT_TITLES[r["key"]]
            rows = [f"{M(p)}: {esc(p.traits[r['key']].text)}" for p in r["players"]]
            text = head + f"Все раскрывают характеристику «{title}».\n" + "\n".join(rows)
        elif code == "mixup":
            a, b = r["players"]
            text = head + f"{M(a)} и {M(b)} поменялись багажом."
            if r["public"]:
                text += f"\n{M(a)}: {esc(a.traits['baggage'].text)}\n{M(b)}: {esc(b.traits['baggage'].text)}"
            for p in (a, b):
                await self._dm(p.user_id, f"Событие «{name}». Ваш новый багаж: {esc(p.traits['baggage'].text)}")
        else:
            text = head
        await self._send(s.chat_id, text)
        if ended:
            s.open_event.set(); s.ready_event.set(); s.vote_event.set()
        return True, "Событие применено."

    # ---------- справочные тексты ----------

    def table_text(self, g):
        lines = ["<b>Стол</b>"]
        for p in g.alive():
            lines.append(f"\n{M(p)}")
            shown = [k for k in G.TRAIT_KEYS if k in p.opened]
            if not shown:
                lines.append("пока ничего не раскрыто")
            for key in shown:
                lines.append(f"{G.TRAIT_TITLES[key]}: {esc(p.traits[key].text)}")
        out = [g.players[u] for u in g.elimination_order]
        if out:
            lines += ["", "Исключены: " + ", ".join(M(p) for p in out)]
        return "\n".join(lines)

    def show_table(self, chat_id):
        s = self.sessions.get(chat_id)
        if not s or not s.started:
            return None
        return self.table_text(s.game)

    def profile_text(self, user_id, name):
        p = self.storage.profile(user_id)
        if not p["games"]:
            return f"У игрока {esc(name)} пока нет сыгранных игр."
        rate = round(100 * p["survived"] / p["games"])
        fav = max(p["professions"], key=p["professions"].get) if p["professions"] else "нет"
        return (f"<b>Профиль: {esc(name)}</b>\n"
                f"Игр: {p['games']}\nПобед: {p['wins']}\nПопал в бункер: {p['survived']} ({rate}%)\n"
                f"Исключён: {p['eliminated']}\nЧаще всего выпадала профессия: {esc(fav)}")

    def top_text(self):
        rows = self.storage.top(10)
        if not rows:
            return "Статистики пока нет. Сыграйте первую игру."
        lines = ["<b>Лучшие игроки по числу побед</b>"]
        for i, (_, p) in enumerate(rows, 1):
            lines.append(f"{i}. {esc(p['name'])}: {p['wins']} из {p['games']}")
        return "\n".join(lines)

    def history_text(self):
        rows = self.storage.history(5)
        if not rows:
            return "История пока пуста."
        lines = ["<b>Последние игры</b>"]
        for h in rows:
            date = str(h["date"]).replace("T", " ")
            survivors = ", ".join(esc(p["name"]) for p in h["players"] if p["survived"]) or "никто"
            lines.append(f"\n{date}. {esc(h['scenario'])}. Шансы: {h['rating']}.\nВ бункере: {survivors}")
        return "\n".join(lines)
