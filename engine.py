"""Движок игры «Бункер»: ведёт игру по фазам, таймерам и сообщениям.

Не зависит от aiogram: общается с Telegram через объект-мессенджер
(методы send и edit), поэтому его можно полностью проверить тестами.
"""
import asyncio
import html
import logging
from dataclasses import dataclass

import game as G
from content import TAG_NAMES

log = logging.getLogger("bunker")


class DeliveryError(Exception):
    """Сообщение не удалось доставить (например, игрок не нажал «Старт» у бота)"""


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
    edit_interval: float = 1.5   # не чаще одной правки сообщения за это время (лимиты Telegram)


def esc(value):
    return html.escape(str(value))


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
    "1. Каждый получает секретную карточку: профессия, биология, здоровье, хобби, фобия, багаж, факт. "
    "Плюс одну особую карту.\n"
    "2. В первом раунде все раскрывают профессию. В следующих раундах каждый выбирает одну характеристику "
    "для раскрытия.\n"
    "3. После раскрытия идёт обсуждение: доказывайте свою пользу и ищите слабые места у других.\n"
    "4. Затем голосование. Исключённый игрок показывает все карточки и выбывает.\n"
    "5. Игра идёт, пока не останется столько человек, сколько мест в бункере.\n\n"
    "<b>Особые карты</b> применяются один раз во время обсуждения или голосования: двойной голос, "
    "иммунитет, замена характеристики, принудительное раскрытие, разведка.\n\n"
    "<b>Итог.</b> Бот оценивает шансы выживших: хватает ли им нужных навыков, есть ли будущее у группы, "
    "каково здоровье."
)


class Session:
    def __init__(self, chat_id, creator_id, game):
        self.chat_id = chat_id
        self.creator_id = creator_id
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


class Engine:
    def __init__(self, messenger, storage, settings=None, bot_username="bot", rng_factory=None):
        self.m = messenger
        self.storage = storage
        self.cfg = settings or Settings()
        self.bot_username = bot_username
        self.sessions = {}
        self.user_chat = {}
        self.rng_factory = rng_factory

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

    async def _wait(self, event, seconds):
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

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
            elif kind == "ready":
                if g.phase != G.DISCUSSION:
                    return
                await self._edit(s.chat_id, s.ready_msg_id, self._ready_text(g),
                                 [[Btn("Готов к голосованию", "ready")]])
            elif kind == "vote":
                if g.phase != G.VOTING:
                    return
                await self._edit(s.chat_id, s.vote_msg_id, self._vote_text(g), self._vote_buttons(g))
            s.last_edit[kind] = loop.time()
        except Exception:
            log.exception("Не удалось обновить сообщение (%s)", kind)
        finally:
            s.refresh_pending[kind] = False

    # ---------- лобби ----------

    def _lobby_text(self, s):
        g = s.game
        lines = ["<b>Бункер. Набор игроков</b>",
                 f"Игроков: {len(g.players)}/{self.cfg.max_players}. "
                 f"Для старта нужно минимум {self.cfg.min_players}.", ""]
        if g.players:
            for i, p in enumerate(g.players.values(), 1):
                lines.append(f"{i}. {esc(p.name)}")
        else:
            lines.append("Пока никого нет.")
        lines += ["", "Чтобы войти, нажмите «Войти», затем в личных сообщениях бота нажмите «Старт». "
                      "Это нужно, чтобы бот мог отправить вам секретную карточку. "
                      "Создатель набора тоже должен войти этим способом."]
        return "\n".join(lines)

    def _lobby_buttons(self, s):
        link = f"https://t.me/{self.bot_username}?start=join_{s.chat_id}"
        return [
            [Btn("Войти", url=link)],
            [Btn("Выйти", "lobby:leave"), Btn("Начать игру", "lobby:start")],
            [Btn("Отменить", "lobby:cancel")],
        ]

    async def create_lobby(self, chat_id, user_id):
        if chat_id in self.sessions:
            return False, "В этом чате уже идёт игра или набор игроков."
        game = G.BunkerGame(self.rng_factory() if self.rng_factory else None)
        s = Session(chat_id, user_id, game)
        self.sessions[chat_id] = s
        s.lobby_msg_id = await self._send(chat_id, self._lobby_text(s), self._lobby_buttons(s))
        return True, ""

    async def join(self, chat_id, user_id, name):
        s = self.sessions.get(chat_id)
        if not s or s.game.phase != G.LOBBY:
            return False, "Набор игроков в этом чате уже закрыт или не открыт."
        other = self.user_chat.get(user_id)
        if other is not None and other != chat_id:
            return False, "Вы уже участвуете в другой игре."
        async with s.lock:
            ok, err = s.game.add_player(user_id, name, self.cfg.max_players)
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

    async def cancel(self, chat_id, user_id, is_admin=False):
        s = self.sessions.get(chat_id)
        if not s:
            return False, "Здесь нет активной игры."
        if user_id != s.creator_id and not is_admin:
            return False, "Остановить игру может создатель или администратор чата."
        was_running = s.started
        if s.task and not s.task.done():
            s.task.cancel()
        self._cleanup(s)
        if was_running:
            await self._send(chat_id, "Игра остановлена.")
        else:
            await self._edit(chat_id, s.lobby_msg_id, "<b>Бункер.</b> Набор игроков отменён.")
        return True, "Игра остановлена."

    def _cleanup(self, s):
        for uid in list(s.game.players):
            if self.user_chat.get(uid) == s.chat_id:
                self.user_chat.pop(uid, None)
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

    async def start(self, chat_id, user_id, is_admin=False):
        s = self.sessions.get(chat_id)
        if not s or s.game.phase != G.LOBBY:
            return False, "Набор игроков не открыт."
        if user_id != s.creator_id and not is_admin:
            return False, "Начать игру может только создатель."
        g = s.game
        async with s.lock:
            try:
                g.start(self.cfg.min_players)
            except ValueError as e:
                return False, str(e)
            s.started = True

        await self._edit(chat_id, s.lobby_msg_id, self._lobby_text(s).replace(
            "<b>Бункер. Набор игроков</b>", "<b>Бункер. Набор закрыт</b>", 1))

        failed = []
        for p in g.players.values():
            mid = await self._dm(p.user_id, self._card_text(p), [[Btn("Использовать особую карту", "card:use")]])
            if mid is None:
                failed.append(p)
        if failed:
            names = ", ".join(esc(p.name) for p in failed)
            self._cleanup(s)
            await self._send(chat_id, f"Не удалось отправить карточки: {names}. Этим игрокам нужно открыть бота "
                                      f"и нажать «Старт». Начните набор заново командой /bunker.")
            return False, "Не удалось отправить карточки."

        need = ", ".join(TAG_NAMES.get(t, t) for t in g.needs)
        to_remove = len(g.players) - g.seats
        text = (f"<b>Игра началась</b>\n\n"
                f"<b>Катастрофа: {esc(g.catastrophe['name'])}.</b> {esc(g.catastrophe['text'])}\n\n"
                f"<b>Бункер: {esc(g.bunker['name'])}.</b> {esc(g.bunker['text'])} "
                f"Припасов хватит на {g.bunker['years']} {G._age_word(g.bunker['years'])}.\n\n"
                f"Мест в бункере: <b>{g.seats}</b>. Игроков: <b>{len(g.players)}</b>. "
                f"Нужно исключить: <b>{to_remove}</b>.\n"
                f"Что понадобится выжившим: {esc(need)}.\n\n"
                f"Секретные карточки отправлены в личные сообщения.")
        await self._send(chat_id, text)
        s.task = asyncio.create_task(self._run(s))
        return True, ""

    # ---------- основной цикл ----------

    async def _run(self, s):
        g = s.game
        try:
            while True:
                async with s.lock:
                    revealed = g.begin_round()
                alive = len(g.alive())
                header = (f"<b>Раунд {g.round}.</b> В игре {alive}, мест в бункере {g.seats}. "
                          f"В этом раунде будет исключён один игрок.")
                await self._send(s.chat_id, header)

                if g.round == 1:
                    lines = ["Первый раунд: все раскрывают профессию.", ""]
                    for p, _, trait in revealed:
                        lines.append(f"{esc(p.name)}: {esc(trait.text)}")
                    await self._send(s.chat_id, "\n".join(lines))
                else:
                    await self._opening_phase(s)

                await self._send(s.chat_id, self.table_text(g))
                await self._discussion_phase(s)
                await self._voting_phase(s)
                if g.finished():
                    break
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
        await self._send(s.chat_id, f"Раскрытие характеристик. Выберите в личных сообщениях бота, какую "
                                    f"характеристику показать остальным. Время: {self.cfg.open_seconds} сек.")
        for p in g.alive():
            buttons = [Btn(G.TRAIT_TITLES[k], f"open:{g.round}:{k}") for k in g.unopened_keys(p.user_id)]
            await self._dm(p.user_id, f"Раунд {g.round}. Выберите характеристику, которую вы раскроете:",
                           rows_of(buttons))
        await self._wait(s.open_event, self.cfg.open_seconds)
        async with s.lock:
            auto = g.auto_open_pending()
            g.end_opening()
        for p, key, trait in auto:
            await self._dm(p.user_id, f"Время на выбор вышло. Раскрыта случайная характеристика: "
                                      f"{G.TRAIT_TITLES[key]}.")
        lines = [f"<b>Раскрыто в раунде {g.round}</b>"]
        for p in g.alive():
            key = g.round_keys.get(p.user_id)
            if key:
                lines.append(f"{esc(p.name)}. {G.TRAIT_TITLES[key]}: {esc(p.traits[key].text)}")
        await self._send(s.chat_id, "\n".join(lines))

    def _ready_text(self, g):
        return (f"<b>Обсуждение.</b> Приводите аргументы и задавайте вопросы. Время: {self.cfg.discuss_seconds} сек. "
                f"Когда большинство нажмёт «Готов к голосованию», обсуждение закончится раньше. "
                f"Особую карту можно применить сейчас или во время голосования.\n\n"
                f"Готовы: {len(g.ready)}/{g.ready_needed()}")

    async def _discussion_phase(self, s):
        g = s.game
        s.ready_event.clear()
        s.ready_msg_id = await self._send(s.chat_id, self._ready_text(g), [[Btn("Готов к голосованию", "ready")]])
        await self._wait(s.ready_event, self.cfg.discuss_seconds)
        await self._edit(s.chat_id, s.ready_msg_id, "<b>Обсуждение завершено.</b>")

    def _vote_buttons(self, g):
        pool = g.alive() if g.candidates is None else [g.players[u] for u in g.candidates]
        buttons = [Btn(p.name[:30], f"vote:{p.user_id}") for p in pool if p.user_id not in g.immune]
        return rows_of(buttons)

    def _vote_text(self, g):
        revote = g.candidates is not None
        title = "Повторное голосование." if revote else "Голосование."
        seconds = self.cfg.revote_seconds if revote else self.cfg.vote_seconds
        voted = sum(1 for p in g.alive() if p.user_id in g.votes)
        return (f"<b>{title}</b> Выберите, кого исключить из бункера. Время: {seconds} сек. "
                f"Голос можно изменить до конца голосования.\n\nПроголосовали: {voted}/{len(g.alive())}")

    async def _voting_phase(self, s):
        g = s.game
        async with s.lock:
            g.begin_voting()
        s.vote_event.clear()
        s.vote_msg_id = await self._send(s.chat_id, self._vote_text(g), self._vote_buttons(g))
        await self._wait(s.vote_event, self.cfg.vote_seconds)
        await self._edit(s.chat_id, s.vote_msg_id, "<b>Голосование завершено.</b>")
        async with s.lock:
            result = g.tally()

        if result.kind == "tie":
            names = ", ".join(esc(p.name) for p in result.tied)
            await self._send(s.chat_id, f"Голоса разделились поровну между: {names}. "
                                        f"Проводится повторное голосование только между ними.")
            async with s.lock:
                g.start_revote(result.tied)
            s.vote_event.clear()
            s.vote_msg_id = await self._send(s.chat_id, self._vote_text(g), self._vote_buttons(g))
            await self._wait(s.vote_event, self.cfg.revote_seconds)
            await self._edit(s.chat_id, s.vote_msg_id, "<b>Повторное голосование завершено.</b>")
            async with s.lock:
                result = g.tally()

        lines = ["<b>Итоги голосования</b>"]
        for uid, count in sorted(result.counts.items(), key=lambda kv: -kv[1]):
            who = ", ".join(esc(n) for n in result.voters.get(uid, []))
            lines.append(f"{esc(g.players[uid].name)}: {count} ({who})")
        if not result.counts:
            lines.append("Голоса не поданы.")
        if result.note:
            lines.append(result.note)
        await self._send(s.chat_id, "\n".join(lines))

        async with s.lock:
            out = g.eliminate(result.player.user_id)
        self.user_chat.pop(out.user_id, None)
        card = ["<b>Все характеристики исключённого игрока</b>"]
        for key in G.TRAIT_KEYS:
            card.append(f"{G.TRAIT_TITLES[key]}: {esc(out.traits[key].text)}")
        await self._send(s.chat_id, f"Из бункера исключён: <b>{esc(out.name)}</b>.\n\n" + "\n".join(card))
        await self._dm(out.user_id, "Вы исключены из бункера. Можете наблюдать за игрой в групповом чате.")

    # ---------- итог ----------

    def _tag_list(self, tags):
        return ", ".join(TAG_NAMES.get(t, t) for t in tags) or "нет"

    async def _finish(self, s):
        g = s.game
        g.finish()
        survivors = g.alive()
        v = g.verdict()
        lines = ["<b>Игра окончена</b>", "", "<b>В бункер попали:</b>"]
        for p in survivors:
            lines.append(f"{esc(p.name)}: {esc(p.traits['profession'].text)}")
        lines += ["", f"Срок пребывания в бункере: {v['years']} {G._age_word(v['years'])}.",
                  f"Нужные навыки закрыты: {self._tag_list(v['covered'])}.",
                  f"Не закрыты: {self._tag_list(v['missing'])}."]
        lines.append("Перспективы продолжения рода: " +
                     ("есть." if v["future_ok"] else "под вопросом: в группе нет подходящей пары."))
        if v["severe"]:
            lines.append("Тяжёлые состояния здоровья: " + ", ".join(esc(p.name) for p in v["severe"]) + ".")
        if v["contagious"]:
            lines.append("Заразные заболевания: " + ", ".join(esc(p.name) for p in v["contagious"]) + ".")
        lines += ["", f"Шансы группы на выживание: <b>{v['rating']}</b> ({v['score']} из 100)."]
        await self._send(s.chat_id, "\n".join(lines))

        cards = ["<b>Карточки выживших</b>"]
        for p in survivors:
            cards.append(f"\n<b>{esc(p.name)}</b>")
            for key in G.TRAIT_KEYS:
                cards.append(f"{G.TRAIT_TITLES[key]}: {esc(p.traits[key].text)}")
        await self._send(s.chat_id, "\n".join(cards))

        try:
            self.storage.record_game(
                [(p.user_id, p.name, p.alive, p.traits["profession"].text) for p in g.players.values()],
                g.catastrophe["name"], g.seats, v["rating"])
        except Exception:
            log.exception("Не удалось сохранить статистику")
        self._cleanup(s)

    # ---------- обработчики действий игроков ----------

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
            if not s.game.pending_openers():
                s.open_event.set()
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
        if res.public:
            await self._send(s.chat_id, esc(res.public))
        if res.private:
            await self._dm(user_id, esc(res.private))
        if in_voting:
            self._schedule_refresh(s, "vote")   # иммунитет убирает кнопку и меняет счётчик
        return True, "Карта использована."

    # ---------- справочные тексты ----------

    def table_text(self, g):
        lines = ["<b>Стол</b>"]
        for p in g.alive():
            lines.append(f"\n<b>{esc(p.name)}</b>")
            shown = [k for k in G.TRAIT_KEYS if k in p.opened]
            if not shown:
                lines.append("пока ничего не раскрыто")
            for key in shown:
                lines.append(f"{G.TRAIT_TITLES[key]}: {esc(p.traits[key].text)}")
        out = [g.players[u].name for u in g.elimination_order]
        if out:
            lines += ["", "Исключены: " + ", ".join(esc(n) for n in out)]
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
                f"Игр: {p['games']}\nПопал в бункер: {p['survived']} ({rate}%)\n"
                f"Исключён: {p['eliminated']}\nЧаще всего выпадала профессия: {esc(fav)}")

    def top_text(self):
        rows = self.storage.top(10)
        if not rows:
            return "Статистики пока нет. Сыграйте первую игру."
        lines = ["<b>Лучшие игроки по числу попаданий в бункер</b>"]
        for i, (_, p) in enumerate(rows, 1):
            lines.append(f"{i}. {esc(p['name'])}: {p['survived']} из {p['games']}")
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
