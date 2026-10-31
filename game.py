"""Игровая логика «Бункера». Не зависит от Telegram."""
import random
from collections import Counter
from dataclasses import dataclass, field

import content

TRAIT_KEYS = ["profession", "bio", "health", "hobby", "phobia", "baggage", "fact"]
TRAIT_TITLES = {
    "profession": "Профессия",
    "bio": "Биология",
    "health": "Здоровье",
    "hobby": "Хобби",
    "phobia": "Фобия",
    "baggage": "Багаж",
    "fact": "Факт",
}

# Особые карты: код -> (название, описание, нужна цель, нужен выбор характеристики)
CARDS = {
    "double": ("Двойной голос",
               "Ваш голос в текущем раунде считается за два. Остальные узнают об этом только при подсчёте.",
               False, False),
    "immunity": ("Иммунитет",
                 "Голоса против вас в текущем раунде не учитываются. Об этом объявляется всем.",
                 False, False),
    "reroll": ("Замена характеристики",
               "Вы заменяете одну из своих нераскрытых характеристик на новую случайную.",
               False, True),
    "reveal": ("Принудительное раскрытие",
               "Выбранный игрок немедленно раскрывает одну случайную нераскрытую характеристику.",
               True, False),
    "peek": ("Разведка",
             "Вы тайно узнаёте одну случайную нераскрытую характеристику выбранного игрока.",
             True, False),
}

LOBBY, OPENING, DISCUSSION, VOTING, ENDED = "lobby", "opening", "discussion", "voting", "ended"


@dataclass
class Trait:
    text: str
    tags: tuple = ()
    severity: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class Player:
    user_id: int
    name: str
    traits: dict = field(default_factory=dict)
    opened: set = field(default_factory=set)
    alive: bool = True
    card: str = None
    card_used: bool = False


@dataclass
class Tally:
    kind: str                 # "eliminate" | "tie" | "random"
    player: Player = None     # исключаемый игрок (для eliminate/random)
    tied: list = field(default_factory=list)    # кандидаты (для tie)
    counts: dict = field(default_factory=dict)  # user_id -> очки
    voters: dict = field(default_factory=dict)  # target_id -> [имена проголосовавших]
    note: str = ""


@dataclass
class CardResult:
    ok: bool
    error: str = ""
    public: str = ""
    private: str = ""


def _age_word(age):
    if 11 <= age % 100 <= 14:
        return "лет"
    return {1: "год", 2: "года", 3: "года", 4: "года"}.get(age % 10, "лет")


def seats_for(players_count):
    """Число мест в бункере: половина игроков, но не меньше двух"""
    return max(2, players_count // 2)


class BunkerGame:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.players = {}
        self.phase = LOBBY
        self.round = 0
        self.seats = 0
        self.catastrophe = None
        self.bunker = None
        self.needs = ()
        self.votes = {}
        self.candidates = None          # при повторном голосовании: список user_id
        self.ready = set()
        self.immune = set()
        self.double = set()
        self.opened_this_round = set()
        self.round_keys = {}            # user_id -> что раскрыто в этом раунде
        self.elimination_order = []

    # ---------- лобби ----------

    def add_player(self, user_id, name, max_players):
        if self.phase != LOBBY:
            return False, "Игра уже началась."
        if user_id in self.players:
            return False, "Вы уже в игре."
        if len(self.players) >= max_players:
            return False, f"Максимум игроков: {max_players}."
        self.players[user_id] = Player(user_id, name)
        return True, ""

    def remove_player(self, user_id):
        if self.phase != LOBBY:
            return False, "Выйти можно только до начала игры."
        if user_id not in self.players:
            return False, "Вас нет в игре."
        del self.players[user_id]
        return True, ""

    def start(self, min_players):
        if self.phase != LOBBY:
            raise ValueError("Игра уже началась.")
        if len(self.players) < min_players:
            raise ValueError(f"Нужно минимум {min_players} игроков. Сейчас: {len(self.players)}.")
        self.seats = seats_for(len(self.players))
        self.catastrophe = self.rng.choice(content.CATASTROPHES)
        self.bunker = self.rng.choice(content.BUNKERS)
        needs = list(self.catastrophe["needs"])
        for tag in self.bunker["lacks"]:
            if tag not in needs:
                needs.append(tag)
        self.needs = tuple(needs)
        self._deal_cards()
        self.phase = OPENING
        self.round = 0

    # ---------- генерация карточек ----------

    def _make_trait(self, key, exclude=()):
        rng = self.rng
        if key == "profession":
            pool = [p for p in content.PROFESSIONS if p[0] not in exclude]
            text, tags = rng.choice(pool or content.PROFESSIONS)
            return Trait(text, tuple(tags))
        if key == "bio":
            gender = rng.choice(["м", "ж"])
            age = int(min(75, max(18, rng.gauss(38, 13))))
            if gender == "ж":
                fertile = age <= 45 and rng.random() > 0.1
            else:
                fertile = age <= 60 and rng.random() > 0.1
            word = "Мужчина" if gender == "м" else "Женщина"
            kids = "может иметь детей" if fertile else "не может иметь детей"
            return Trait(f"{word}, {age} {_age_word(age)}, {kids}", (), 0,
                         {"gender": gender, "age": age, "fertile": fertile})
        if key == "health":
            pool = [h for h in content.HEALTH if h[0] not in exclude] or content.HEALTH
            weights = [{0: 4, 1: 4, 2: 2, 3: 1}[h[1]] for h in pool]
            text, severity, tags = rng.choices(pool, weights=weights, k=1)[0]
            return Trait(text, tuple(tags), severity)
        if key == "hobby":
            pool = [h for h in content.HOBBIES if h[0] not in exclude]
            text, tags = rng.choice(pool or content.HOBBIES)
            return Trait(text, tuple(tags))
        if key == "phobia":
            pool = [p for p in content.PHOBIAS if p not in exclude]
            return Trait(rng.choice(pool or content.PHOBIAS))
        if key == "baggage":
            pool = [b for b in content.BAGGAGE if b[0] not in exclude]
            text, tags = rng.choice(pool or content.BAGGAGE)
            return Trait(text, tuple(tags))
        if key == "fact":
            pool = [f for f in content.FACTS if f[0] not in exclude]
            text, tags = rng.choice(pool or content.FACTS)
            return Trait(text, tuple(tags))
        raise KeyError(key)

    def _deal_cards(self):
        used = {key: set() for key in TRAIT_KEYS}
        card_codes = list(CARDS)
        for player in self.players.values():
            for key in TRAIT_KEYS:
                trait = self._make_trait(key, exclude=used[key])
                used[key].add(trait.text)
                player.traits[key] = trait
            player.card = self.rng.choice(card_codes)
            player.card_used = False

    # ---------- раунды ----------

    def alive(self):
        return [p for p in self.players.values() if p.alive]

    def alive_ids(self):
        return [p.user_id for p in self.alive()]

    def begin_round(self):
        """Начало раунда. В первом раунде все автоматически раскрывают профессию.
        Возвращает список (игрок, ключ, характеристика) автоматических раскрытий."""
        if self.phase == ENDED:
            raise ValueError("Игра окончена.")
        self.round += 1
        self.phase = OPENING
        self.votes = {}
        self.candidates = None
        self.ready = set()
        self.immune = set()
        self.double = set()
        self.opened_this_round = set()
        self.round_keys = {}
        revealed = []
        if self.round == 1:
            for p in self.alive():
                p.opened.add("profession")
                self.opened_this_round.add(p.user_id)
                self.round_keys[p.user_id] = "profession"
                revealed.append((p, "profession", p.traits["profession"]))
            self.phase = DISCUSSION
        return revealed

    def unopened_keys(self, user_id):
        p = self.players[user_id]
        return [k for k in TRAIT_KEYS if k not in p.opened]

    def open_trait(self, user_id, key):
        p = self.players.get(user_id)
        if self.phase != OPENING:
            return False, "Сейчас не время раскрытия."
        if not p or not p.alive:
            return False, "Вы не участвуете в раунде."
        if user_id in self.opened_this_round:
            return False, "В этом раунде вы уже раскрыли характеристику."
        if key not in TRAIT_KEYS:
            return False, "Неизвестная характеристика."
        if key in p.opened:
            return False, "Эта характеристика уже раскрыта."
        p.opened.add(key)
        self.opened_this_round.add(user_id)
        self.round_keys[user_id] = key
        return True, ""

    def pending_openers(self):
        return [p for p in self.alive() if p.user_id not in self.opened_this_round]

    def auto_open_pending(self):
        """Игроки, не успевшие выбрать, раскрывают случайную характеристику"""
        done = []
        for p in self.pending_openers():
            keys = self.unopened_keys(p.user_id)
            if not keys:
                self.opened_this_round.add(p.user_id)
                continue
            key = self.rng.choice(keys)
            p.opened.add(key)
            self.opened_this_round.add(p.user_id)
            self.round_keys[p.user_id] = key
            done.append((p, key, p.traits[key]))
        return done

    def end_opening(self):
        self.phase = DISCUSSION

    # ---------- обсуждение ----------

    def ready_needed(self):
        return len(self.alive()) // 2 + 1

    def set_ready(self, user_id):
        p = self.players.get(user_id)
        if self.phase != DISCUSSION:
            return False, "Сейчас не время обсуждения."
        if not p or not p.alive:
            return False, "Вы не участвуете в раунде."
        self.ready.add(user_id)
        return True, ""

    def discussion_done(self):
        return len(self.ready) >= self.ready_needed()

    # ---------- голосование ----------

    def begin_voting(self):
        self.phase = VOTING
        self.votes = {}

    def cast_vote(self, voter_id, target_id):
        voter = self.players.get(voter_id)
        target = self.players.get(target_id)
        if self.phase != VOTING:
            return False, "Сейчас не время голосования."
        if not voter or not voter.alive:
            return False, "Вы не можете голосовать."
        if not target or not target.alive:
            return False, "Этот игрок уже не участвует."
        if voter_id == target_id:
            return False, "Нельзя голосовать за себя."
        if self.candidates is not None and target_id not in self.candidates:
            return False, "В повторном голосовании участвуют только указанные кандидаты."
        if target_id in self.immune:
            return False, "У этого игрока иммунитет в текущем раунде."
        self.votes[voter_id] = target_id
        return True, ""

    def all_voted(self):
        return all(p.user_id in self.votes for p in self.alive())

    def tally(self):
        """Подсчёт голосов. Не исключает игрока, только определяет результат."""
        counts = Counter()
        voters = {}
        for voter_id, target_id in self.votes.items():
            voter = self.players.get(voter_id)
            target = self.players.get(target_id)
            if not voter or not voter.alive or not target or not target.alive:
                continue
            if target_id in self.immune:
                continue
            if self.candidates is not None and target_id not in self.candidates:
                continue
            weight = 2 if voter_id in self.double else 1
            counts[target_id] += weight
            label = voter.name + (" (x2)" if weight == 2 else "")
            voters.setdefault(target_id, []).append(label)

        if not counts:
            pool = [p for p in self.alive() if p.user_id not in self.immune]
            if self.candidates is not None:
                pool = [p for p in pool if p.user_id in self.candidates] or pool
            chosen = self.rng.choice(pool or self.alive())
            return Tally("random", chosen, [], dict(counts), voters,
                         "Голосов нет, игрок определён жребием.")

        top = max(counts.values())
        leaders = [uid for uid, c in counts.items() if c == top]
        if len(leaders) == 1:
            return Tally("eliminate", self.players[leaders[0]], [], dict(counts), voters)

        if self.candidates is None:
            return Tally("tie", None, [self.players[u] for u in leaders], dict(counts), voters,
                         "Голоса разделились поровну.")

        chosen = self.rng.choice(leaders)
        return Tally("random", self.players[chosen], [], dict(counts), voters,
                     "Повторная ничья, игрок определён жребием.")

    def start_revote(self, tied_players):
        self.candidates = [p.user_id for p in tied_players]
        self.votes = {}

    def eliminate(self, user_id):
        p = self.players[user_id]
        p.alive = False
        self.elimination_order.append(user_id)
        return p

    def finished(self):
        return len(self.alive()) <= self.seats

    def finish(self):
        self.phase = ENDED

    # ---------- особые карты ----------

    def play_card(self, user_id, target_id=None, key=None):
        p = self.players.get(user_id)
        if not p or not p.alive:
            return CardResult(False, "Вы не участвуете в игре.")
        if p.card_used:
            return CardResult(False, "Особая карта уже использована.")
        if self.phase not in (DISCUSSION, VOTING):
            return CardResult(False, "Карту можно использовать во время обсуждения и голосования.")
        code = p.card
        _, _, needs_target, _ = CARDS[code]

        target = None
        if needs_target:
            target = self.players.get(target_id)
            if not target or not target.alive:
                return CardResult(False, "Выберите игрока, который ещё в игре.")
            if target_id == user_id:
                return CardResult(False, "Нельзя выбрать себя.")
            if not self.unopened_keys(target_id):
                return CardResult(False, "У этого игрока все характеристики уже раскрыты.")

        if code == "double":
            self.double.add(user_id)
            p.card_used = True
            return CardResult(True, private="Двойной голос активирован до конца раунда.")

        if code == "immunity":
            self.immune.add(user_id)
            p.card_used = True
            # голоса, уже отданные против игрока, аннулируются
            for voter_id in [v for v, t in self.votes.items() if t == user_id]:
                del self.votes[voter_id]
            return CardResult(True, public=f"{p.name} использует карту «Иммунитет»: "
                                           f"голоса против него в этом раунде не учитываются.",
                              private="Иммунитет активирован до конца раунда.")

        if code == "reroll":
            if key not in TRAIT_KEYS or key in p.opened:
                return CardResult(False, "Выберите нераскрытую характеристику.")
            old = p.traits[key].text
            p.traits[key] = self._make_trait(key, exclude={old})
            p.card_used = True
            return CardResult(True, private=f"{TRAIT_TITLES[key]} заменена.\n"
                                            f"Было: {old}\nСтало: {p.traits[key].text}")

        if code == "reveal":
            pick = self.rng.choice(self.unopened_keys(target_id))
            target.opened.add(pick)
            p.card_used = True
            return CardResult(True, public=f"{p.name} использует карту «Принудительное раскрытие». "
                                           f"{target.name} раскрывает характеристику "
                                           f"«{TRAIT_TITLES[pick]}»: {target.traits[pick].text}")

        if code == "peek":
            pick = self.rng.choice(self.unopened_keys(target_id))
            p.card_used = True
            return CardResult(True, private=f"Разведка: у игрока {target.name} "
                                            f"характеристика «{TRAIT_TITLES[pick]}»: {target.traits[pick].text}")

        return CardResult(False, "Неизвестная карта.")

    # ---------- итоги ----------

    def verdict(self):
        """Оценка шансов выживших"""
        survivors = self.alive()
        have = set()
        for p in survivors:
            for key in ("profession", "hobby", "baggage", "fact"):
                have |= set(p.traits[key].tags)
        covered = [t for t in self.needs if t in have]
        missing = [t for t in self.needs if t not in have]

        health_load = sum(p.traits["health"].severity for p in survivors)
        severe = [p for p in survivors if p.traits["health"].severity >= 3]
        contagious = [p for p in survivors if "contagious" in p.traits["health"].tags]

        fertile_women = any(p.traits["bio"].extra.get("gender") == "ж" and p.traits["bio"].extra.get("fertile")
                            for p in survivors)
        fertile_men = any(p.traits["bio"].extra.get("gender") == "м" and p.traits["bio"].extra.get("fertile")
                          for p in survivors)
        future_ok = fertile_women and fertile_men

        skills = 60 * len(covered) / max(1, len(self.needs))
        future = 20 if future_ok else (10 if (fertile_women or fertile_men) else 0)
        health = max(0, 20 - 3 * health_load)
        score = int(round(max(0, skills + future + health - 10 * len(contagious))))
        score = min(100, score)

        if score >= 75:
            rating = "высокие"
        elif score >= 45:
            rating = "средние"
        else:
            rating = "низкие"
        return {
            "score": score,
            "rating": rating,
            "covered": covered,
            "missing": missing,
            "severe": severe,
            "contagious": contagious,
            "future_ok": future_ok,
            "years": self.bunker["years"],
        }
