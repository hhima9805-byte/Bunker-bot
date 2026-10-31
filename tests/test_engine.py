import asyncio
import logging
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
logging.disable(logging.CRITICAL)
import game as G  # noqa: E402
from engine import DeliveryError, Engine, Settings  # noqa: E402
from storage import Storage  # noqa: E402


class FakeMessenger:
    def __init__(self):
        self.sent = []          # (chat_id, text, buttons, msg_id)
        self.edits = []         # (chat_id, msg_id, text, buttons, time)
        self.next_id = 1
        self.blocked = set()    # пользователи, которым нельзя писать

    async def send(self, chat_id, text, buttons=None):
        if chat_id in self.blocked:
            raise DeliveryError("Forbidden")
        mid = self.next_id; self.next_id += 1
        self.sent.append((chat_id, text, buttons, mid))
        return mid

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((chat_id, message_id, text, buttons, time.monotonic()))

    def texts(self, chat):
        return [t for c, t, _, _ in self.sent if c == chat]

    def has(self, chat, word):
        return any(word in t for t in self.texts(chat))

    def last_buttons(self, chat, prefix=None):
        for c, t, b, _ in reversed(self.sent):
            if c == chat and b and (prefix is None or any(x.data and x.data.startswith(prefix) for r in b for x in r)):
                return b
        return None


def fast(**kw):
    base = dict(min_players=3, open_seconds=0.15, discuss_seconds=0.15, vote_seconds=0.15,
                revote_seconds=0.15, edit_interval=0)
    base.update(kw)
    return Settings(**base)


def mk_engine(settings=None, seed=1):
    m = FakeMessenger()
    e = Engine(m, Storage(tempfile.mkdtemp()), settings or fast(), bot_username="bunker_test_bot",
               rng_factory=lambda: random.Random(seed))
    return e, m


GROUP = -1001
n_ok = 0


def ok(cond, name):
    global n_ok
    print(("OK  " if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    n_ok += 1


def run(c):
    return asyncio.run(c)


async def lobby(e, n=5, chat=GROUP, creator=1, base=100, names=None):
    await e.create_lobby(chat, creator)
    for i in range(n):
        r = await e.join(chat, base + i, (names or {}).get(base + i, f"Игрок{i + 1}"))
        assert r[0], r
    return e.sessions[chat]


async def wait_phase(g, phase, timeout=3):
    t0 = time.time()
    while g.phase != phase:
        await asyncio.sleep(0.005)
        if time.time() - t0 > timeout:
            raise TimeoutError(f"фаза {phase}, сейчас {g.phase}")


# ---------- лобби ----------
async def t_lobby():
    e, m = mk_engine()
    r = await e.create_lobby(GROUP, 1); ok(r[0] and m.has(GROUP, "Набор игроков"), "лобби создано")
    link = m.sent[0][2][0][0].url
    ok(link == f"https://t.me/bunker_test_bot?start=join_{GROUP}", "кнопка «Войти» ведёт к боту с кодом чата")
    ok(not (await e.create_lobby(GROUP, 2))[0], "второй набор в том же чате запрещён")
    ok((await e.join(GROUP, 10, "Аня"))[0] and not (await e.join(GROUP, 10, "Аня"))[0], "повторный вход отклонён")
    await asyncio.sleep(0.05)
    ok(any("1. Аня" in t for _, _, t, _, _ in m.edits), "лобби обновляется при входе")
    await e.create_lobby(-5, 3)
    ok(not (await e.join(-5, 10, "Аня"))[0], "нельзя участвовать в двух играх сразу")
    ok((await e.leave(GROUP, 10))[0] and 10 not in e.user_chat, "выход из лобби освобождает игрока")
    for i in range(12):
        await e.join(GROUP, 200 + i, f"П{i}")
    ok(not (await e.join(GROUP, 999, "лишний"))[0], "13-й игрок не принимается")
    ok(not (await e.join(777, 5, "x"))[0], "вход в несуществующий набор отклонён")
    ok(not (await e.cancel(GROUP, 55))[0] and (await e.cancel(GROUP, 1))[0] and GROUP not in e.sessions,
       "отменить набор может только создатель")
    ok(not e.user_chat.get(200), "после отмены игроки свободны")
    ok((await e.create_lobby(GROUP, 1))[0], "после отмены можно открыть новый набор")
    e3, _ = mk_engine(); await e3.create_lobby(GROUP, 1)
    ok((await e3.cancel(GROUP, 99, is_admin=True))[0], "администратор чата может отменить набор")
run(t_lobby())


# ---------- троттлинг правок ----------
async def t_throttle():
    e, m = mk_engine(fast(edit_interval=0.3))
    await e.create_lobby(GROUP, 1)
    for i in range(12):
        await e.join(GROUP, 400 + i, f"T{i}")
    await asyncio.sleep(0.9)
    edits = [x for x in m.edits if x[0] == GROUP]
    ok(1 <= len(edits) <= 3, f"12 входов подряд дали {len(edits)} правки лобби, а не 12")
    ok("12. T11" in edits[-1][2], "последняя правка показывает актуальный состав")
    gaps = [b[4] - a[4] for a, b in zip(edits, edits[1:])]
    ok(all(gp >= 0.28 for gp in gaps), "правки не чаще заданного интервала")
run(t_throttle())


# ---------- старт ----------
async def t_start():
    e, m = mk_engine(fast(min_players=5, discuss_seconds=5, open_seconds=5, vote_seconds=5))
    await lobby(e, 4)
    r = await e.start(GROUP, 1)
    ok(not r[0] and "минимум 5" in r[1], "мало игроков — старт отклонён")
    await e.join(GROUP, 300, "Пятый")
    ok(not (await e.start(GROUP, 55))[0], "начать может только создатель")
    ok((await e.start(GROUP, 1))[0], "игра стартовала")
    for uid in (100, 101, 102, 103, 300):
        dm = [t for t in m.texts(uid) if "секретная карточка" in t]
        ok(len(dm) == 1 and all(G.TRAIT_TITLES[k] in dm[0] for k in G.TRAIT_KEYS) and "Особая карта" in dm[0],
           f"карточка с 7 характеристиками и особой картой у игрока {uid}")
    ok(m.has(GROUP, "Катастрофа") and m.has(GROUP, "Мест в бункере: <b>2</b>") and m.has(GROUP, "Нужно исключить: <b>3</b>"),
       "объявление: катастрофа, бункер, 2 места, исключить 3")
    ok(m.last_buttons(100, "card:use") is not None, "в карточке есть кнопка особой карты")
    ok(any("Набор закрыт" in t for _, _, t, _, _ in m.edits), "лобби помечено как закрытое")
    await e.cancel(GROUP, 1)
    ok(m.has(GROUP, "Игра остановлена") and not e.user_chat, "остановка идущей игры")

    e, m = mk_engine(fast(min_players=3)); await lobby(e, 3); m.blocked.add(101)
    r = await e.start(GROUP, 1)
    ok(not r[0] and m.has(GROUP, "Не удалось отправить карточки") and "Игрок2" in "".join(m.texts(GROUP))
       and GROUP not in e.sessions, "игрок без «Старт» у бота: игра отменяется с понятным сообщением")
run(t_start())


# ---------- полный ход раунда ----------
async def t_flow():
    e, m = mk_engine(fast(min_players=5, open_seconds=5, discuss_seconds=5, vote_seconds=5, revote_seconds=5))
    s = await lobby(e, 5, names={102: "<b>Хакер</b> & Co"}); g = s.game
    await e.start(GROUP, 1)
    await wait_phase(g, G.DISCUSSION)
    ok(m.has(GROUP, "Раунд 1") and m.has(GROUP, "Первый раунд: все раскрывают профессию"), "раунд 1: профессии раскрыты автоматически")
    ok(m.has(GROUP, "&lt;b&gt;Хакер&lt;/b&gt; &amp; Co") and not m.has(GROUP, "<b>Хакер</b>"), "имена экранируются (нет HTML-инъекции)")
    ok(m.has(GROUP, "<b>Стол</b>"), "показан стол")
    t0 = time.time()
    for uid in (100, 101, 102):
        await e.on_ready(GROUP, uid)
    await wait_phase(g, G.VOTING, 2)
    ok(time.time() - t0 < 2, "большинство нажало «Готов»: обсуждение закончилось досрочно")
    ok(m.last_buttons(GROUP, "vote:") is not None, "голосование: кнопки с игроками")
    ok(not (await e.on_vote(GROUP, 100, 100))[0], "за себя голосовать нельзя")
    for v in (100, 101, 102, 103):
        await e.on_vote(GROUP, v, 104)
    ok(g.phase == G.VOTING, "пока не все проголосовали — голосование идёт")
    await asyncio.sleep(0.05)
    ok(any("Проголосовали: 4/5" in t for _, _, t, _, _ in m.edits), "счётчик голосов обновляется")
    await e.on_vote(GROUP, 104, 100)
    await wait_phase(g, G.OPENING, 2)
    ok(m.has(GROUP, "Итоги голосования") and m.has(GROUP, "Из бункера исключён: <b>Игрок5</b>") and 104 not in e.user_chat,
       "исключён Игрок5, итоги с составом проголосовавших")
    ok(m.has(104, "Вы исключены из бункера") and m.has(GROUP, "Все характеристики исключённого игрока"),
       "исключённому личное сообщение, всем показаны его карточки")
    ok(m.has(GROUP, "Раунд 2") and m.last_buttons(100, "open:") is not None, "раунд 2: в личке кнопки выбора характеристики")
    btns = [x.data for r in m.last_buttons(100, "open:") for x in r]
    ok(all(d.startswith("open:2:") for d in btns) and not any(d.endswith(":profession") for d in btns),
       "кнопки привязаны к раунду, раскрытой профессии среди них нет")
    ok(not (await e.on_open(100, "health", round_no=1))[0], "кнопка прошлого раунда не срабатывает")
    ok((await e.on_open(100, "health", round_no=2))[0], "раскрытие через кнопку")
    ok(not (await e.on_open(100, "hobby", round_no=2))[0], "второй выбор за раунд отклонён")
    for uid in (101, 102, 103):
        await e.on_open(uid, "fact")
    await wait_phase(g, G.DISCUSSION, 2)
    ok(m.has(GROUP, "Раскрыто в раунде 2") and m.has(GROUP, "Здоровье:") and m.has(GROUP, "Факт:"),
       "все раскрыли досрочно: итог раунда в группе")
    await e.cancel(GROUP, 1)
run(t_flow())


# ---------- не успели раскрыть ----------
async def t_auto_open():
    e, m = mk_engine(fast(min_players=5, open_seconds=0.1, discuss_seconds=0.05, vote_seconds=0.05), seed=9)
    s = await lobby(e, 6); g = s.game
    await e.start(GROUP, 1)
    while g.round < 2 or g.phase == G.OPENING:
        await asyncio.sleep(0.01)
    ok(any("Время на выбор вышло" in t for uid in range(100, 106) for t in m.texts(uid)), "не успевшим сообщили о случайном раскрытии")
    ok(m.has(GROUP, "Раскрыто в раунде 2"), "раунд продолжился без них")
    await e.cancel(GROUP, 1)
run(t_auto_open())


# ---------- ничья ----------
async def t_tie():
    e, m = mk_engine(fast(min_players=5, open_seconds=5, discuss_seconds=0.05, vote_seconds=5, revote_seconds=5), seed=2)
    s = await lobby(e, 6); g = s.game
    await e.start(GROUP, 1); await wait_phase(g, G.VOTING, 2)
    for v, tg in {100: 101, 101: 100, 102: 103, 103: 102, 104: 100, 105: 102}.items():
        await e.on_vote(GROUP, v, tg)
    for _ in range(100):
        if g.candidates:
            break
        await asyncio.sleep(0.02)
    ok(m.has(GROUP, "Голоса разделились поровну") and set(g.candidates) == {100, 102}, "ничья: повторное голосование между кандидатами")
    vb = [x.data for r in m.last_buttons(GROUP, "vote:") for x in r]
    ok(sorted(vb) == ["vote:100", "vote:102"], "в повторном голосовании кнопки только кандидатов")
    ok(not (await e.on_vote(GROUP, 101, 104))[0], "за не-кандидата голосовать нельзя")
    for v in (100, 101, 103, 104, 105):
        await e.on_vote(GROUP, v, 102 if v != 102 else 100)
    await e.on_vote(GROUP, 102, 100)
    await wait_phase(g, G.OPENING, 2); ok(len(g.alive()) == 5, "после повторного голосования выбыл один игрок")
    await e.cancel(GROUP, 1)
run(t_tie())


# ---------- особые карты ----------
async def t_cards():
    e, m = mk_engine(fast(min_players=5, open_seconds=5, discuss_seconds=5, vote_seconds=5), seed=3)
    s = await lobby(e, 6); g = s.game
    await e.start(GROUP, 1); await wait_phase(g, G.DISCUSSION)

    def give(uid, code):
        g.players[uid].card = code; g.players[uid].card_used = False
    give(100, "immunity"); give(101, "reveal"); give(102, "peek"); give(103, "double"); give(104, "reroll")
    r = await e.on_card_use(100); ok(r[0] and m.has(GROUP, "Иммунитет"), "иммунитет: объявлен в группе")
    ok(not (await e.on_card_use(100))[0], "повторно картой воспользоваться нельзя")
    r = await e.on_card_use(101); ok(r[0] and m.last_buttons(101, "card:t:") is not None, "раскрытие: бот присылает кнопки с целями")
    r = await e.on_card_target(101, 103)
    ok(r[0] and m.has(GROUP, "Принудительное раскрытие") and len(g.players[103].opened) == 2, "раскрытие: цель раскрыла характеристику")
    r = await e.on_card_target(102, 101); ok(r[0] and m.has(102, "Разведка:") and not m.has(GROUP, "Разведка:"), "разведка: результат только в личке")
    r = await e.on_card_use(103); ok(r[0] and 103 in g.double, "двойной голос активирован")
    r = await e.on_card_use(104); ok(r[0] and m.last_buttons(104, "card:k:") is not None, "замена: бот присылает выбор характеристики")
    old = g.players[104].traits['hobby'].text
    r = await e.on_card_key(104, "hobby")
    ok(r[0] and g.players[104].traits['hobby'].text != old and m.has(104, "Было:"), "замена выполнена, результат в личке")
    for uid in range(100, 106):
        await e.on_ready(GROUP, uid)
    await wait_phase(g, G.VOTING, 2)
    ok(not any(x.data == "vote:100" for r in m.last_buttons(GROUP, "vote:") for x in r), "у игрока с иммунитетом нет кнопки голосования")
    ok(not (await e.on_vote(GROUP, 101, 100))[0], "за игрока с иммунитетом проголосовать нельзя")
    give(105, "immunity")
    await e.on_card_use(105); await asyncio.sleep(0.05)
    last_vote_edit = [x for x in m.edits if x[0] == GROUP and x[3]][-1]
    ok(not any(b.data == "vote:105" for r in last_vote_edit[3] for b in r), "иммунитет во время голосования убирает кнопку")
    give(103, "double"); g.phase = G.OPENING
    ok(not (await e.on_card_use(103))[0], "вне обсуждения карта недоступна")
    g.phase = G.VOTING
    await e.cancel(GROUP, 1)
run(t_cards())


# ---------- игра целиком ----------
async def t_full():
    e, m = mk_engine(fast(min_players=5, open_seconds=0.05, discuss_seconds=0.05, vote_seconds=0.05, revote_seconds=0.05), seed=4)
    s = await lobby(e, 7)
    await e.start(GROUP, 1)
    await asyncio.wait_for(s.task, 15)
    ok(m.has(GROUP, "Игра окончена") and m.has(GROUP, "Шансы группы на выживание") and m.has(GROUP, "Карточки выживших"),
       "итог игры: состав, оценка, карточки")
    ok(GROUP not in e.sessions and not e.user_chat, "после игры чат и игроки свободны")
    p = e.storage.profile(100); ok(p["games"] == 1 and p["survived"] + p["eliminated"] == 1, "статистика записана")
    ok(sum(1 for uid in range(100, 107) if e.storage.profile(uid)["survived"]) == 3, "в бункере ровно 3 места из 7")
    ok("Профиль" in e.profile_text(100, "A") and "Лучшие игроки" in e.top_text() and "Последние игры" in e.history_text(),
       "профиль, топ и история формируются")
    ok((await e.create_lobby(GROUP, 1))[0], "можно сразу начать новую игру")
run(t_full())


# ---------- сбой внутри игры ----------
async def t_error():
    e, m = mk_engine(fast(min_players=3)); s = await lobby(e, 3)

    def boom(g):
        raise RuntimeError("тест")
    e.table_text = boom
    await e.start(GROUP, 1)
    await asyncio.wait_for(s.task, 5)
    ok(m.has(GROUP, "внутренней ошибки") and GROUP not in e.sessions, "при сбое игра останавливается с сообщением и очищается")
run(t_error())


# ---------- 30 игр одновременно ----------
async def t_stress():
    rng = random.Random(7)
    m = FakeMessenger()
    e = Engine(m, Storage(tempfile.mkdtemp()),
               fast(min_players=5, open_seconds=0.12, discuss_seconds=0.12, vote_seconds=0.12, revote_seconds=0.12,
                    edit_interval=0.05), bot_username="b")

    async def one(idx):
        chat = -2000 - idx; base = idx * 100
        n = rng.randint(5, 12)
        s = await lobby(e, n, chat=chat, creator=base, base=base)
        await e.start(chat, base)
        g = s.game
        while g.phase != G.ENDED:
            await asyncio.sleep(0.01)
            alive = g.alive()
            if g.phase == G.OPENING:
                for p in alive:
                    if rng.random() < 0.6 and g.unopened_keys(p.user_id):
                        await e.on_open(p.user_id, rng.choice(g.unopened_keys(p.user_id)), g.round)
            elif g.phase == G.DISCUSSION:
                for p in alive:
                    if rng.random() < 0.3:
                        await e.on_ready(chat, p.user_id)
                    if rng.random() < 0.05:
                        await e.on_card_use(p.user_id)
            elif g.phase == G.VOTING:
                for p in alive:
                    if rng.random() < 0.7:
                        others = [o for o in alive if o.user_id != p.user_id]
                        await e.on_vote(chat, p.user_id, rng.choice(others).user_id)
                    if rng.random() < 0.03:
                        await e.on_card_use(p.user_id)
        if not s.task.done():
            await asyncio.wait_for(s.task, 10)
        return len(g.alive()) == g.seats

    t0 = time.time()
    results = await asyncio.wait_for(asyncio.gather(*[one(i) for i in range(30)]), 180)
    ok(all(results), f"30 одновременных игр (5-12 игроков, случайные действия) доиграны за {time.time() - t0:.1f} с")
    ok(not e.sessions and not e.user_chat, "после всех игр состояние очищено")
    ok(not any("внутренней ошибки" in t for _, t, _, _ in m.sent), "ни одна игра не упала с внутренней ошибкой")
    longest = max(len(t) for _, t, _, _ in m.sent)
    ok(longest < 4096, f"самое длинное сообщение {longest} символов, лимит Telegram 4096")
run(t_stress())
print(f"\nВСЕ {n_ok} ПРОВЕРОК ДВИЖКА ПРОЙДЕНЫ")
