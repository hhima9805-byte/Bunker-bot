import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import game as G  # noqa: E402

n_ok = 0


def ok(cond, name):
    global n_ok
    print(("OK  " if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    n_ok += 1


def mk(n, seed=1):
    g = G.BunkerGame(random.Random(seed))
    for i in range(1, n + 1):
        assert g.add_player(i, f"p{i}", 12)[0]
    g.start(min_players=3)
    return g


# места
ok([G.seats_for(n) for n in (5, 6, 7, 8, 9, 10, 11, 12)] == [2, 3, 3, 4, 4, 5, 5, 6], "число мест = половина игроков (5..12)")
ok(G.seats_for(3) == 2 and G.seats_for(4) == 2, "минимум два места")

# лобби
g = G.BunkerGame()
ok(g.add_player(1, "a", 3)[0] and not g.add_player(1, "a", 3)[0], "дубликат игрока не добавляется")
g.add_player(2, "b", 3); g.add_player(3, "c", 3)
ok(not g.add_player(4, "d", 3)[0], "лимит игроков")
ok(g.remove_player(3)[0] and not g.remove_player(3)[0], "выход из лобби")
try:
    g.start(min_players=5); ok(False, "старт без нужного числа игроков")
except ValueError:
    ok(True, "старт без нужного числа игроков запрещён")

# раздача карточек
g = mk(12)
ok(all(set(p.traits) == set(G.TRAIT_KEYS) for p in g.players.values()), "у каждого 7 характеристик")
ok(len({p.traits['profession'].text for p in g.players.values()}) == 12, "профессии не повторяются")
ok(all(p.card in G.CARDS for p in g.players.values()), "у каждого особая карта")
ok(g.seats == 6 and g.needs and g.catastrophe and g.bunker, "сценарий: места, потребности, катастрофа, бункер")
bio = [p.traits['bio'] for p in g.players.values()]
ok(all(18 <= b.extra['age'] <= 75 for b in bio) and all(b.text.startswith(("Мужчина", "Женщина")) for b in bio),
   "биология: пол и возраст в допустимых пределах")
ok(G._age_word(21) == "год" and G._age_word(22) == "года" and G._age_word(25) == "лет" and G._age_word(11) == "лет",
   "склонение «год/года/лет»")

# раунд 1: профессия раскрывается автоматически
rev = g.begin_round()
ok(len(rev) == 12 and all(k == 'profession' for _, k, _ in rev) and g.phase == G.DISCUSSION, "раунд 1: профессии раскрыты у всех")

# раунд 2: раскрытие
g.begin_voting(); t = g.tally(); g.eliminate(t.player.user_id)
g.begin_round()
ok(g.phase == G.OPENING, "раунд 2: фаза раскрытия")
uid = g.alive_ids()[0]
ok(g.open_trait(uid, 'health')[0] and g.round_keys[uid] == 'health', "раскрытие характеристики")
ok(not g.open_trait(uid, 'hobby')[0], "второй раз за раунд нельзя")
uid2 = g.alive_ids()[1]
ok(not g.open_trait(uid2, 'profession')[0], "уже раскрытую характеристику открыть нельзя")
ok(not g.open_trait(uid2, 'нет такой')[0], "неизвестный ключ отклонён")
ok(not g.open_trait(t.player.user_id, 'hobby')[0], "исключённый не может раскрывать")
auto = g.auto_open_pending()
ok(len(auto) == len(g.alive()) - 1 and not g.pending_openers() and len(g.round_keys) == len(g.alive()),
   "не успевшие раскрывают случайную характеристику")
g.end_opening(); ok(g.phase == G.DISCUSSION, "переход к обсуждению")

# готовность к голосованию
g = mk(6); g.begin_round()
ok(g.ready_needed() == 4, "большинство для голосования из 6 — 4")
for u in (1, 2, 3):
    g.set_ready(u)
ok(not g.discussion_done(), "3 из 6 — ещё не большинство")
g.set_ready(4); ok(g.discussion_done(), "4 из 6 — большинство")

# голосование
g = mk(5); g.begin_round(); g.begin_voting()
ok(not g.cast_vote(1, 1)[0], "за себя голосовать нельзя")
ok(not g.cast_vote(99, 1)[0] and not g.cast_vote(1, 99)[0], "голос от/за постороннего отклонён")
for v in (1, 2, 3):
    g.cast_vote(v, 5)
g.cast_vote(4, 1); g.cast_vote(5, 1)
t = g.tally(); ok(t.kind == "eliminate" and t.player.user_id == 5 and t.counts[5] == 3, "исключён лидер по голосам")
ok(t.voters[5] == ["p1", "p2", "p3"], "список проголосовавших")
g.cast_vote(1, 2)
ok(g.votes[1] == 2, "голос можно изменить")

# ничья -> повторное голосование -> жребий
g = mk(6, seed=5); g.begin_round(); g.begin_voting()
for v, tg in {1: 2, 2: 1, 3: 4, 4: 3, 5: 1, 6: 3}.items():
    g.cast_vote(v, tg)
t = g.tally(); ok(t.kind == "tie" and {p.user_id for p in t.tied} == {1, 3}, "ничья 2:2 даёт повторное голосование")
g.start_revote(t.tied)
ok(not g.cast_vote(2, 4)[0], "в повторном голосовании только кандидаты")
for v, tg in {1: 3, 3: 1, 2: 1, 4: 3}.items():
    g.cast_vote(v, tg)
t = g.tally(); ok(t.kind == "random" and t.player.user_id in (1, 3) and "жребием" in t.note, "повторная ничья решается жребием")

# никто не голосовал
g = mk(5, seed=2); g.begin_round(); g.begin_voting()
t = g.tally(); ok(t.kind == "random" and t.player.alive, "нет голосов — жребий")

# двойной голос
g = mk(5, seed=3); g.begin_round(); g.begin_voting()
g.double.add(1)
g.cast_vote(1, 2); g.cast_vote(3, 4); g.cast_vote(5, 4); g.cast_vote(2, 4); g.cast_vote(4, 2)
t = g.tally(); ok(t.counts[2] == 3 and t.counts[4] == 3, "двойной голос считается как два (3:3)")
ok(t.kind == "tie", "с двойным голосом возможна ничья")


# карты
def card_game(code, seed=7):
    g = mk(5, seed)
    for p in g.players.values():
        p.card = code
    g.begin_round()
    return g


g = card_game("immunity"); g.begin_voting()
g.cast_vote(2, 1); g.cast_vote(3, 1); g.cast_vote(4, 1)
r = g.play_card(1)
ok(r.ok and r.public and 1 in g.immune and not g.votes, "иммунитет: объявлен, голоса против аннулированы")
ok(not g.cast_vote(2, 1)[0], "за игрока с иммунитетом голосовать нельзя")
ok(not g.play_card(1).ok, "карта используется только один раз")
g.cast_vote(2, 3); g.cast_vote(4, 3); g.cast_vote(5, 3)
ok(g.tally().player.user_id == 3, "иммунитет: исключён другой игрок")

g = card_game("double"); r = g.play_card(1); ok(r.ok and 1 in g.double and not r.public, "двойной голос: скрытое действие")

g = card_game("reveal"); r = g.play_card(1, target_id=2)
ok(r.ok and len(g.players[2].opened) == 2 and "Принудительное раскрытие" in r.public, "принудительное раскрытие: публично")
ok(not card_game("reveal").play_card(1, target_id=1).ok, "раскрытие: нельзя выбрать себя")
ok(not card_game("reveal").play_card(1).ok, "раскрытие: нужна цель")

g = card_game("peek"); r = g.play_card(1, target_id=2)
ok(r.ok and r.private and not r.public and len(g.players[2].opened) == 1, "разведка: только лично, у цели ничего не раскрыто")
hidden = [k for k in G.TRAIT_KEYS if k not in g.players[2].opened]
ok(any(g.players[2].traits[k].text in r.private for k in hidden), "разведка показывает нераскрытую характеристику")

g = card_game("reroll"); old = g.players[1].traits['hobby'].text
r = g.play_card(1, key='hobby')
ok(r.ok and g.players[1].traits['hobby'].text != old and old in r.private, "замена: характеристика изменилась")
ok(not card_game("reroll").play_card(1, key='profession').ok, "замена: раскрытую характеристику менять нельзя")
g = G.BunkerGame(random.Random(1))
for i in range(1, 6):
    g.add_player(i, f"p{i}", 12)
g.start(3)
for p in g.players.values():
    p.card = "double"
ok(not g.play_card(1).ok, "карту нельзя использовать вне обсуждения/голосования")

# конец игры и вердикт
g = mk(6, seed=11); g.begin_round()
while not g.finished():
    g.begin_voting(); t = g.tally(); g.eliminate(t.player.user_id)
    if not g.finished():
        g.begin_round()
ok(len(g.alive()) == g.seats == 3, "игра заканчивается, когда выживших столько, сколько мест")
v = g.verdict()
ok(0 <= v['score'] <= 100 and v['rating'] in ("высокие", "средние", "низкие")
   and set(v['covered']) | set(v['missing']) == set(g.needs), "вердикт: оценка и покрытие потребностей")

g = mk(4, seed=1)
for p in g.players.values():
    for k in ('profession', 'hobby', 'baggage', 'fact'):
        p.traits[k] = G.Trait("Тест", ())
    p.traits['health'] = G.Trait("Здоров", (), 0)
    p.traits['bio'] = G.Trait("x", (), 0, {"gender": "м", "age": 30, "fertile": True})
g.needs = ("medicine",); g.seats = 4
v = g.verdict(); ok(v['covered'] == [] and v['missing'] == ["medicine"] and not v['future_ok'], "вердикт: навыки не закрыты, будущего нет")
g.players[1].traits['profession'] = G.Trait("Врач", ("medicine",))
g.players[2].traits['bio'] = G.Trait("x", (), 0, {"gender": "ж", "age": 30, "fertile": True})
v2 = g.verdict(); ok(v2['covered'] == ["medicine"] and v2['future_ok'] and v2['score'] > v['score'], "вердикт: врач и пара повышают оценку")
g.players[3].traits['health'] = G.Trait("Туберкулёз", ("contagious",), 3)
v3 = g.verdict(); ok(v3['contagious'] and v3['score'] < v2['score'], "вердикт: заразный выживший снижает оценку")

# массовая проверка
rng = random.Random(42)
for n in range(5, 13):
    for s in range(150):
        g = G.BunkerGame(random.Random(rng.random()))
        for i in range(1, n + 1):
            g.add_player(i, f"p{i}", 12)
        g.start(5); g.begin_round()
        steps = 0
        while True:
            g.begin_voting()
            if rng.random() < 0.5:
                for p in g.alive():
                    others = [o for o in g.alive() if o.user_id != p.user_id]
                    g.cast_vote(p.user_id, rng.choice(others).user_id)
            t = g.tally()
            if t.kind == "tie":
                g.start_revote(t.tied); t = g.tally()
            g.eliminate(t.player.user_id)
            if g.finished():
                break
            g.begin_round()
            g.auto_open_pending(); g.end_opening()
            steps += 1; assert steps < 20
        assert len(g.alive()) == g.seats
        g.verdict()
ok(True, "1200 случайных партий (5-12 игроков) доиграны без ошибок")
print(f"\nВСЕ {n_ok} ПРОВЕРОК ЛОГИКИ ПРОЙДЕНЫ")
