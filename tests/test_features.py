"""Проверки наборов сценариев, событий, финала «снаружи», таймера, настроек и карточек картинкой."""
import asyncio
import logging
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
logging.disable(logging.CRITICAL)
import card_image  # noqa: E402
import content as C  # noqa: E402
import game as G  # noqa: E402
from engine import DeliveryError, Engine, Settings  # noqa: E402
from storage import Storage  # noqa: E402

n_ok = 0


def ok(cond, name):
    global n_ok
    print(("OK  " if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    n_ok += 1


def run(c):
    return asyncio.run(c)


# =====================================================================
# Логика
# =====================================================================

def mk(n, seed=1, pack="realism", outside=False):
    g = G.BunkerGame(random.Random(seed), pack=pack, outside=outside)
    for i in range(1, n + 1):
        g.add_player(i, f"p{i}", 12)
    g.start(3)
    return g


def texts_of(pool):
    return {x[0] if isinstance(x, tuple) else x for x in pool}


fantasy_texts = set()
absurd_texts = set()
for name in dir(C):
    if name.startswith("FANTASY_") and isinstance(getattr(C, name), list):
        fantasy_texts |= {x[0] if isinstance(x, tuple) else x.get("name") if isinstance(x, dict) else x
                          for x in getattr(C, name)}
    if name.startswith("ABSURD_") and isinstance(getattr(C, name), list):
        absurd_texts |= {x[0] if isinstance(x, tuple) else x.get("name") if isinstance(x, dict) else x
                         for x in getattr(C, name)}
fantasy_texts |= C.FANTASY_IN_BASE


def stats(pack, games=300):
    special = total = 0
    cats = set()
    for seed in range(games):
        g = mk(8, seed, pack)
        cats.add(g.catastrophe["name"])
        for p in g.players.values():
            for k in ("profession", "health", "hobby", "baggage", "fact"):
                total += 1
                t = p.traits[k].text
                if t in fantasy_texts or t in absurd_texts:
                    special += 1
    return special / total, cats


share, cats = stats("realism")
ok(share == 0 and not (cats & (fantasy_texts | absurd_texts)), "реализм: нет фантастики и абсурда")
share, cats = stats("fantasy")
ok(0.3 < share < 0.6 and cats <= fantasy_texts and "Инопланетное вторжение" in cats, f"фантастика: доля особых характеристик {share:.0%}, катастрофы только фантастические")
share, cats = stats("absurd")
ok(0.3 < share < 0.6 and cats <= absurd_texts, f"абсурд: доля особых характеристик {share:.0%}, катастрофы абсурдные")
share, cats = stats("mixed")
ok(cats & fantasy_texts and cats & absurd_texts and cats - fantasy_texts - absurd_texts, "смешанный: катастрофы всех видов")
ok(G.BunkerGame(pack="непонятно").pack == "realism", "неизвестный набор заменяется реализмом")
g = mk(12, 3, "absurd")
ok(len({p.traits["profession"].text for p in g.players.values()}) == 12, "в наборе профессии тоже не повторяются")

# события
g = mk(8, 4); g.begin_round()
seats, years = g.seats, g.years
r = g.apply_event("fire"); ok(r["seats"] == seats - 1 == g.seats, "пожар: мест на одно меньше")
r = g.apply_event("room"); ok(g.seats == seats, "комната: мест на одно больше")
r = g.apply_event("stash"); ok(g.years == years + 1, "тайник: припасов на год больше")
r = g.apply_event("quake"); ok(g.years == years, "землетрясение: на год меньше")
g.years = 1
try:
    g.apply_event("quake"); ok(False, "землетрясение при 1 годе")
except ValueError:
    ok(True, "землетрясение не уменьшает срок ниже года")
g.seats = 1
try:
    g.apply_event("fire"); ok(False, "пожар при 1 месте")
except ValueError:
    ok(True, "пожар не уменьшает места ниже одного")
g = mk(8, 5); g.begin_round()
r = g.apply_event("outbreak")
ok(r["player"].traits["health"].severity >= 2 and r["new"] != r["old"] and r["public"] is False, "вспышка болезни ухудшает здоровье одного игрока")
r = g.apply_event("medcheck"); ok(all("health" in p.opened for p in g.alive()), "медосмотр раскрывает здоровье всем")
r = g.apply_event("search"); ok(all("baggage" in p.opened for p in g.alive()), "обыск раскрывает багаж всем")
a_bag = {p.user_id: p.traits["baggage"].text for p in g.alive()}
r = g.apply_event("mixup"); a, b = r["players"]
ok(a.traits["baggage"].text == a_bag[b.user_id] and b.traits["baggage"].text == a_bag[a.user_id] and r["public"], "перепутанный багаж: двое поменялись")
try:
    g.apply_event("nope"); ok(False, "неизвестное событие")
except ValueError:
    ok(True, "неизвестное событие отклонено")
g = mk(6, 6); g.begin_round()
for u in (1, 2):
    g.eliminate(u)
g.apply_event("room"); ok(g.finished(), "лишняя комната может завершить игру")

# финал снаружи
g = mk(6, 7, outside=True); g.begin_round()
for u in (1, 2, 3):
    g.eliminate(u)
inside, outside, winner = g.final_result()
ok(outside is not None and outside["size"] == 3 and inside["size"] == 3, "две группы по 3 человека")
ok(set(G.OUTSIDE_NEEDS) <= set(outside["covered"]) | set(outside["missing"]), "снаружи нужны защита, еда и вода")
ok(winner == ("outside" if outside["score"] > inside["score"] else "inside"), "победитель по оценке, при равенстве бункер")
g2 = mk(6, 7); g2.begin_round()
for u in (1, 2, 3):
    g2.eliminate(u)
ok(g2.final_result()[1] is None and g2.final_result()[2] == "inside", "без режима «снаружи» побеждает бункер")
g3 = mk(4, 8, outside=True)
ok(g3.verdict(outside=True)["score"] == 0, "пустая группа снаружи получает 0")
# одинаковые люди: снаружи хуже
g4 = mk(4, 9, outside=True); g4.begin_round()
for p in g4.players.values():
    for k in ("profession", "hobby", "character", "baggage", "fact"):
        p.traits[k] = G.Trait("x", ("medicine", "defense", "food", "water", "energy", "engineering",
                                    "construction", "agriculture", "science", "psychology", "tech",
                                    "chemistry", "mechanics", "communication", "logistics", "education", "morale"))
g4.eliminate(1); g4.eliminate(2)
i, o, w = g4.final_result()
ok(o["score"] < i["score"] and w == "inside", "при равных группах снаружи шансы ниже")

# статистика побед
st = Storage(tempfile.mkdtemp())
st.record_game([(1, "A", True, "Врач", False), (2, "B", False, "Повар", True)], "Ядерная война", 1, "низкие", "outside")
ok(st.profile(1)["wins"] == 0 and st.profile(1)["survived"] == 1 and st.profile(2)["wins"] == 1, "победа снаружи засчитывается исключённым")
ok(st.top()[0][0] == 2 and st.history(1)[0]["winner"] == "outside", "топ по победам, итог в истории")

# карточка картинкой
g = mk(6, 10, "fantasy")
png = card_image.render_card(g.players[1], g)
ok(png[:8] == b"\x89PNG\r\n\x1a\n" and 20_000 < len(png) < 2_000_000 and card_image.available(), "карточка рисуется в PNG")
g.players[2].name = "Очень" * 40
g.players[2].traits["fact"] = G.Trait("слово " * 200)
ok(card_image.render_card(g.players[2], g)[:4] == b"\x89PNG", "длинные имя и тексты не ломают рисование")


# =====================================================================
# Движок
# =====================================================================

class FakeMessenger:
    def __init__(self):
        self.sent, self.edits, self.photos = [], [], []
        self.next_id = 1
        self.blocked = set()
        self.photo_blocked = set()

    async def send(self, chat_id, text, buttons=None):
        if chat_id in self.blocked:
            raise DeliveryError("Forbidden")
        mid = self.next_id; self.next_id += 1
        self.sent.append((chat_id, text, buttons, mid))
        return mid

    async def send_photo(self, chat_id, png, caption, buttons=None):
        if chat_id in self.blocked or chat_id in self.photo_blocked:
            raise DeliveryError("Forbidden")
        mid = self.next_id; self.next_id += 1
        self.photos.append((chat_id, png, caption, buttons, mid))
        return mid

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((chat_id, message_id, text, buttons, time.monotonic()))

    def texts(self, chat):
        return [t for c, t, _, _ in self.sent if c == chat]

    def has(self, chat, word):
        return any(word in t for t in self.texts(chat))

    def edited(self, word):
        return any(word in t for _, _, t, _, _ in self.edits)


GROUP = -1001


def fast(**kw):
    base = dict(min_players=5, open_seconds=5, discuss_seconds=5, vote_seconds=5, revote_seconds=5,
                edit_interval=0, timer_tick=0.05)
    base.update(kw)
    return Settings(**base)


def mk_engine(settings=None, seed=1, renderer=None):
    m = FakeMessenger()
    e = Engine(m, Storage(tempfile.mkdtemp()), settings or fast(), bot_username="b",
               rng_factory=lambda: random.Random(seed), card_renderer=renderer)
    return e, m


async def lobby(e, n=6, host=False):
    await e.create_lobby(GROUP, 1, "Ведущий", "host_name")
    if host:
        await e.toggle_host(GROUP, 1)
    for i in range(n):
        await e.join(GROUP, 100 + i, f"Игрок{i + 1}", f"pl_{i + 1}")
    return e.sessions[GROUP]


async def wait_phase(g, phase, timeout=3):
    t0 = time.time()
    while g.phase != phase:
        await asyncio.sleep(0.005)
        if time.time() - t0 > timeout:
            raise TimeoutError(f"фаза {phase}, сейчас {g.phase}")


# ---------- настройки ----------
async def t_settings():
    e, m = mk_engine()
    s = await lobby(e)
    ok(not (await e.open_settings(GROUP, 100))[0], "настройки открывает только создатель")
    ok((await e.open_settings(GROUP, 1))[0] and s.settings_msg_id is not None, "меню настроек открыто")
    ok(not (await e.on_setting(GROUP, 100, "pack"))[0], "чужой не может менять настройки")
    seen = []
    for _ in range(4):
        await e.on_setting(GROUP, 1, "pack"); seen.append(s.pack)
    ok(seen == ["fantasy", "absurd", "mixed", "realism"], "набор переключается по кругу")
    await e.on_setting(GROUP, 1, "pack")
    r = await e.on_setting(GROUP, 1, "tempo"); ok(r[0] and s.tempo == "fast", "быстрый темп включён")
    r = await e.on_setting(GROUP, 1, "image"); ok(not r[0] and not s.image_cards, "без рисовальщика картинки не включаются")
    r = await e.on_setting(GROUP, 1, "outside"); ok(r[0] and s.outside, "финал «снаружи» включён")
    ok(not (await e.on_setting(GROUP, 1, "что-то"))[0], "неизвестная настройка отклонена")
    await asyncio.sleep(0.05)
    ok(m.edited("Набор: Фантастика") and m.edited("Финал «снаружи»: включено"), "лобби и меню показывают настройки")
    r = await e.on_setting(GROUP, 1, "done"); ok(r[0] and s.settings_msg_id is None and m.edited("Настройки сохранены"), "«Готово» закрывает меню")
    await e.open_settings(GROUP, 1)
    ok((await e.start(GROUP, 1))[0], "старт с настройками")
    g = s.game
    ok(g.pack == "fantasy" and g.outside and abs(s.timings["discuss"] - 5 / 3) < 1e-9, "игра получила набор, финал и короткие этапы")
    ok(m.has(GROUP, "Набор: Фантастика. Темп: быстрый") and m.has(GROUP, "Финал «снаружи»"), "настройки объявлены при старте")
    ok(not (await e.on_setting(GROUP, 1, "pack"))[0] and s.settings_msg_id is None, "после старта настройки закрыты")
    await e.cancel(GROUP, 1)
run(t_settings())


# ---------- карточки картинкой ----------
async def t_images():
    e, m = mk_engine(renderer=card_image.render_card)
    s = await lobby(e)
    await e.on_setting(GROUP, 1, "image")
    ok(s.image_cards, "картинки включаются при наличии рисовальщика")
    await e.start(GROUP, 1)
    ok(len(m.photos) == 6 and all(p[1][:4] == b"\x89PNG" for p in m.photos), "все игроки получили карточку картинкой")
    ok(all("Особая карта" in p[2] and len(p[2]) <= 1024 and p[3] for p in m.photos), "подпись с особой картой и кнопкой, не длиннее лимита")
    ok(not any("секретная карточка" in t for c, t, _, _ in m.sent if c >= 100), "текстовые карточки не дублируются")
    await e.cancel(GROUP, 1)

    def broken(p, g):
        raise RuntimeError("нет шрифта")
    e, m = mk_engine(renderer=broken); s = await lobby(e); await e.on_setting(GROUP, 1, "image")
    ok((await e.start(GROUP, 1))[0] and not m.photos and len([1 for c, t, _, _ in m.sent if "секретная карточка" in t]) == 6,
       "если рисование сломалось, карточки уходят текстом")
    await e.cancel(GROUP, 1)

    e, m = mk_engine(renderer=card_image.render_card); s = await lobby(e); await e.on_setting(GROUP, 1, "image")
    m.photo_blocked.add(102)
    r = await e.start(GROUP, 1)
    ok(not r[0] and m.has(GROUP, "Не удалось отправить карточки"), "фото не дошло — игра не начинается, как с текстом")
run(t_images())


# ---------- таймер ----------
async def t_timer():
    e, m = mk_engine(fast(discuss_seconds=0.6, timer_tick=0.05))
    s = await lobby(e, host=True); g = s.game
    await e.start(GROUP, 1)
    await wait_phase(g, G.DISCUSSION)
    await asyncio.sleep(0.25)
    countdowns = [t for _, mid, t, _, _ in m.edits if mid == s.ready_msg_id and "Осталось:" in t]
    ok(len(countdowns) >= 2, f"живой отсчёт обновляется ({len(countdowns)} правок за 0.25 с)")
    r = await e.on_host(1, "host:pause"); ok(r[0] and s.paused and m.has(GROUP, "поставил игру на паузу"), "пауза включена")
    await asyncio.sleep(0.05)
    ok(m.edited("Игра на паузе."), "в сообщении этапа видно паузу")
    await asyncio.sleep(0.8)
    ok(g.phase == G.DISCUSSION, "на паузе этап не заканчивается, хотя время вышло бы")
    left = s.pause_left
    r = await e.on_host(1, "host:add"); ok(r[0] and abs(s.pause_left - left - 30) < 1e-6, "на паузе добавление увеличивает остаток")
    s.pause_left = 0.2
    r = await e.on_host(1, "host:pause"); ok(r[0] and not s.paused and m.has(GROUP, "снял игру с паузы"), "пауза снята")
    t0 = time.time()
    await wait_phase(g, G.VOTING, 2)
    ok(0.1 < time.time() - t0 < 1.0, "после паузы этап закончился по оставшемуся времени")
    before = s.deadline
    r = await e.on_host(1, "host:add"); ok(r[0] and s.deadline - before == 30, "добавление 30 секунд к голосованию")
    ok(m.has(GROUP, "добавил 30 секунд"), "о добавлении времени объявлено")
    await e.on_host(1, "host:skip")
    await wait_phase(g, G.OPENING, 2)
    await e.on_host(1, "host:pause")
    for u in g.alive_ids():
        await e.on_open(u, g.unopened_keys(u)[0], g.round)
    await wait_phase(g, G.DISCUSSION, 2)
    ok(s.paused and s.phase_kind == "ready" and s.deadline is None, "пауза переносится на следующий этап")
    ok((await e.on_host(1, "host:pause"))[0] and s.deadline is not None, "снятие паузы запускает отсчёт этапа")
    await e.cancel(GROUP, 1)

    e, m = mk_engine(fast(discuss_seconds=5)); s = await lobby(e, host=True)
    await e.start(GROUP, 1)
    s.phase_kind = None
    ok(not (await e.on_host(1, "host:add"))[0], "добавить время без этапа с таймером нельзя")
    await e.cancel(GROUP, 1)

    e, m = mk_engine(fast(discuss_seconds=5)); s = await lobby(e, host=True); g = s.game
    await e.start(GROUP, 1); await wait_phase(g, G.DISCUSSION)
    await e.on_host(1, "host:pause")
    for u in g.alive_ids()[:4]:
        await e.on_ready(GROUP, u)
    await wait_phase(g, G.VOTING, 2)
    ok(True, "на паузе игроки всё равно могут закончить этап досрочно")
    await e.cancel(GROUP, 1)
run(t_timer())


# ---------- события через панель ----------
async def t_events():
    e, m = mk_engine(fast(), seed=21)
    s = await lobby(e, 8, host=True); g = s.game
    await e.start(GROUP, 1); await wait_phase(g, G.DISCUSSION)
    r = await e.on_host(1, "host:event")
    menu = [b.data for row in m.sent[-1][2] for b in row]
    ok(r[0] and menu == [f"host:ev:{c}" for c in G.EVENTS] and "Пожар на складе" in m.sent[-1][1], "меню событий с описаниями")
    seats = g.seats
    r = await e.on_host(1, "host:ev:fire")
    ok(r[0] and g.seats == seats - 1 and m.has(GROUP, f"Мест в бункере теперь: <b>{seats - 1}</b>"), "пожар объявлен")
    r = await e.on_host(1, "host:ev:stash"); ok(r[0] and m.has(GROUP, "Найден тайник"), "тайник объявлен")
    r = await e.on_host(1, "host:ev:outbreak")
    victims = [u for u in g.alive_ids() if any("Ваше здоровье изменилось" in t for t in m.texts(u))]
    ok(r[0] and len(victims) == 1 and m.has(GROUP, "знает только он"), "вспышка: заболевший узнал лично, остальные — что кто-то")
    r = await e.on_host(1, "host:ev:medcheck")
    ok(r[0] and all("health" in p.opened for p in g.alive()) and m.has(GROUP, "@pl_1:"), "медосмотр раскрыл здоровье всех")
    r = await e.on_host(1, "host:ev:outbreak")
    ok(m.has(GROUP, "ухудшилось здоровье:"), "когда здоровье уже раскрыто, новое значение объявляется")
    r = await e.on_host(1, "host:ev:mixup")
    got = [u for u in g.alive_ids() if any("Ваш новый багаж" in t for t in m.texts(u))]
    ok(r[0] and len(got) == 2 and m.has(GROUP, "поменялись багажом"), "перепутанный багаж: двоим пришло лично")
    ok(not (await e.on_host(1, "host:ev:nope"))[0] and not (await e.on_host(1, "host:ev"))[0], "неизвестное событие отклонено")
    ok(not (await e.on_host(100, "host:ev:fire"))[0], "игрок не может запускать события")
    alive = len(g.alive())
    g.seats = alive - 1
    r = await e.on_host(1, "host:ev:room")
    ok(r[0] and m.has(GROUP, "Мест хватает всем"), "лишняя комната: мест хватает всем")
    await asyncio.wait_for(s.task, 5)
    ok(m.has(GROUP, "Игра окончена") and len(g.alive()) == alive, "игра завершилась без лишнего исключения")
run(t_events())


# ---------- финал «снаружи» целиком ----------
async def t_outside():
    winners = set()
    for seed in range(12):
        e, m = mk_engine(fast(open_seconds=0.03, discuss_seconds=0.03, vote_seconds=0.03, revote_seconds=0.03), seed=seed)
        s = await lobby(e, 7)
        await e.on_setting(GROUP, 1, "outside")
        await e.start(GROUP, 1)
        await asyncio.wait_for(s.task, 15)
        g = s.game
        inside, outside, winner = g.final_result()
        winners.add(winner)
        ok(m.has(GROUP, "Группа снаружи:") and m.has(GROUP, "Победила группа"), f"партия {seed}: итог с двумя группами")
        out_ids = [u for u in range(100, 107) if not g.players[u].alive]
        ok(all(any("в группе снаружи" in t for t in m.texts(u)) for u in out_ids), f"партия {seed}: исключённым сказали, что они снаружи")
        wins = {u: e.storage.profile(u)["wins"] for u in range(100, 107)}
        expected = {u: int(g.players[u].alive == (winner == "inside")) for u in range(100, 107)}
        ok(wins == expected, f"партия {seed}: победы записаны победившей группе ({winner})")
    ok(winners == {"inside", "outside"}, f"в 12 партиях побеждали обе стороны: {sorted(winners)}")
run(t_outside())


# ---------- нагрузка: 30 игр, ведущий делает всё подряд ----------
async def t_stress():
    rng = random.Random(77)
    m = FakeMessenger()
    e = Engine(m, Storage(tempfile.mkdtemp()),
               fast(open_seconds=0.15, discuss_seconds=0.15, vote_seconds=0.15, revote_seconds=0.15,
                    timer_tick=0.03, edit_interval=0.02),
               bot_username="b", card_renderer=card_image.render_card)

    async def one(idx):
        chat = -3000 - idx; base = idx * 100; host = base + 99
        await e.create_lobby(chat, host, "Хост", None)
        await e.toggle_host(chat, host)
        s = e.sessions[chat]
        s.pack = rng.choice(C.PACK_ORDER); s.tempo = rng.choice(["normal", "fast"])
        s.outside = rng.random() < 0.5; s.image_cards = rng.random() < 0.3
        for i in range(rng.randint(5, 12)):
            await e.join(chat, base + i, f"И{i}")
        assert (await e.start(chat, host))[0]
        g = s.game
        actions = ["host:pause", "host:pause", "host:add", "host:skip"] + [f"host:ev:{c}" for c in G.EVENTS]
        while g.phase != G.ENDED:
            await asyncio.sleep(0.01)
            alive = g.alive()
            if rng.random() < 0.08:
                await e.on_host(host, rng.choice(actions))
            if s.paused and rng.random() < 0.3:
                await e.on_host(host, "host:pause")
            if rng.random() < 0.02 and len(alive) > g.seats:
                await e.on_host(host, f"host:ko:{rng.choice(alive).user_id}")
            if g.phase == G.OPENING:
                for p in alive:
                    if rng.random() < 0.5 and g.unopened_keys(p.user_id):
                        await e.on_open(p.user_id, rng.choice(g.unopened_keys(p.user_id)), g.round)
            elif g.phase == G.DISCUSSION:
                for p in alive:
                    if rng.random() < 0.3:
                        await e.on_ready(chat, p.user_id)
            elif g.phase == G.VOTING:
                for p in alive:
                    if rng.random() < 0.6:
                        others = [o for o in alive if o.user_id != p.user_id]
                        if others:
                            await e.on_vote(chat, p.user_id, rng.choice(others).user_id)
        if not s.task.done():
            await asyncio.wait_for(s.task, 10)
        return len(g.alive()) <= g.seats

    t0 = time.time()
    results = await asyncio.wait_for(asyncio.gather(*[one(i) for i in range(30)]), 240)
    ok(all(results), f"30 игр со случайными настройками и действиями ведущего доиграны за {time.time() - t0:.1f} с")
    ok(not e.sessions and not e.user_chat and not e.host_chat, "состояние очищено")
    ok(not any("внутренней ошибки" in t for _, t, _, _ in m.sent), "ни одна игра не упала")
    ok(max(len(t) for _, t, _, _ in m.sent) < 4096, "все сообщения в пределах лимита Telegram")
run(t_stress())

print(f"\nВСЕ {n_ok} ПРОВЕРОК НОВЫХ ФУНКЦИЙ ПРОЙДЕНЫ")
