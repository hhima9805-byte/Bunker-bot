"""Хранение статистики игроков и истории игр в JSON-файлах."""
import json
import os
from datetime import datetime


class Storage:
    def __init__(self, data_dir=None):
        self.dir = data_dir or os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
        os.makedirs(self.dir, exist_ok=True)
        self.profiles_path = os.path.join(self.dir, "bunker_profiles.json")
        self.history_path = os.path.join(self.dir, "bunker_history.json")

    def _load(self, path, default):
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _save(self, path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def profile(self, user_id):
        data = self._load(self.profiles_path, {})
        return data.get(str(user_id), {"name": "", "games": 0, "survived": 0, "eliminated": 0,
                                       "professions": {}})

    def record_game(self, players, scenario, seats, rating):
        """players: список (user_id, имя, выжил, профессия)"""
        data = self._load(self.profiles_path, {})
        for user_id, name, survived, profession in players:
            p = data.get(str(user_id), {"name": name, "games": 0, "survived": 0,
                                        "eliminated": 0, "professions": {}})
            p["name"] = name
            p["games"] += 1
            if survived:
                p["survived"] += 1
            else:
                p["eliminated"] += 1
            p["professions"][profession] = p["professions"].get(profession, 0) + 1
            data[str(user_id)] = p
        self._save(self.profiles_path, data)

        history = self._load(self.history_path, [])
        history.append({
            "date": datetime.now().isoformat(timespec="minutes"),
            "scenario": scenario,
            "seats": seats,
            "rating": rating,
            "players": [{"id": u, "name": n, "survived": s, "profession": pr}
                        for u, n, s, pr in players],
        })
        self._save(self.history_path, history[-50:])

    def top(self, limit=10, min_games=1):
        data = self._load(self.profiles_path, {})
        rows = [(int(uid), p) for uid, p in data.items() if p.get("games", 0) >= min_games]
        rows.sort(key=lambda r: (r[1]["survived"], r[1]["survived"] / max(1, r[1]["games"])), reverse=True)
        return rows[:limit]

    def history(self, limit=5):
        return list(reversed(self._load(self.history_path, [])))[:limit]
