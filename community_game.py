"""Discord-independent community rules. Mutations never await mid-transaction."""

from datetime import datetime, timedelta, timezone
import hashlib
import time
import uuid

LOCAL = timezone(timedelta(hours=8))

QUESTS = {
    "trivia": ("Answer a trivia question correctly", 1, 300),
    "pet": ("Feed or play with the server pet twice", 2, 250),
    "riddle": ("Solve today's quest riddle", 1, 350),
    "adventure": ("Finish a party adventure", 1, 500),
}
RIDDLES = [
    ("What has keys but cannot open locks?", ["piano", "a piano"]),
    ("What gets wetter the more it dries?", ["towel", "a towel"]),
    ("What has hands but cannot clap?", ["clock", "a clock"]),
    ("What has one eye but cannot see?", ["needle", "a needle"]),
    ("What has many teeth but cannot bite?", ["comb", "a comb"]),
    ("What can you catch but never throw?", ["cold", "a cold"]),
    ("What has a neck but no head?", ["bottle", "a bottle"]),
]
QUESTIONS = [
    "What completely useless superpower would you choose?",
    "Which fictional place would you visit for one afternoon?",
    "What ordinary thing deserves a championship competition?",
    "You can add one rule to real life. What is it?",
    "What food would you defend even if everyone disagreed?",
    "What small thing made your week better?",
    "What game would you like to experience again for the first time?",
    "What object in your room would be the funniest narrator?",
    "What skill would you learn if you had a free month?",
    "What song belongs in a movie about your day?",
    "What would you name a very dramatic houseplant?",
    "Which animal would be the worst roommate, and why?",
    "What invention should exist by now?",
    "What harmless hill are you willing to defend?",
    "If our server were a town, what shop would you run?",
    "What is your most oddly specific talent?",
    "What would a tutorial for being you include?",
    "What would you put inside a time capsule for ten years from now?",
    "What fictional sidekick deserves their own story?",
    "What would your ideal rainy afternoon look like?",
    "What would you do with a pause button that works for ten seconds?",
    "What game mechanic would make chores fun?",
    "What tiny inconvenience would you erase forever?",
    "What snack would you bring on a fantasy quest?",
    "What would your personal loading-screen tip say?",
    "What surprising topic could you talk about for an hour?",
    "Which two fictional characters should switch jobs?",
    "What would the server pet say if it could talk?",
    "What would you call a holiday you invented?",
    "What is something you are looking forward to?",
    "What would your dream co-op game be about?",
]
SHOP = {
    "midnight": {"name": "Midnight background", "price": 750, "slot": "background", "color": 0x353A78},
    "sunset": {"name": "Sunset background", "price": 750, "slot": "background", "color": 0xDC754D},
    "neon": {"name": "Neon background", "price": 1000, "slot": "background", "color": 0x36BFA3},
    "stargazer": {"name": "Stargazer badge", "price": 500, "slot": "badge"},
    "founder": {"name": "Adventurer badge", "price": 800, "slot": "badge"},
    "legend": {"name": "Local Legend title (7 days)", "price": 1800, "slot": "title"},
    "scarf": {"name": "Server pet's red scarf", "price": 1500, "slot": "pet"},
    "crown": {"name": "Server pet's tiny crown", "price": 2500, "slot": "pet"},
}
CASES = [
    {"title": "The Missing Moonstone", "story": "The museum's moonstone vanished during a blackout.",
     "suspects": ["Curator", "Courier", "Guard"], "culprit": "Courier",
     "clues": {"gallery": "Wet delivery-boot prints lead from the pedestal to the service exit.",
               "office": "The curator's key was sealed in an intact evidence envelope all evening.",
               "gate": "The guard's camera shows the courier leaving with a box shaped exactly like the moonstone case."},
     "explanation": "The courier's boots and matching box connect the theft to the service exit; the other evidence rules out the curator and guard."},
    {"title": "The Clocktower Switch", "story": "Someone replaced the festival trophy with a painted wooden copy.",
     "suspects": ["Painter", "Baker", "Clockmaker"], "culprit": "Painter",
     "clues": {"tower": "Fresh blue pigment stains the trophy stand. The clockmaker's workshop uses only brass and oil.",
               "bakery": "A continuous kitchen recording shows the baker working throughout the switch.",
               "studio": "A receipt in the painter's studio lists wood cut to the trophy's exact dimensions and the matching blue paint."},
     "explanation": "The exact-size wooden blank and matching fresh pigment identify the painter; the baker has a recorded alibi."},
    {"title": "The Vanishing Recipe", "story": "The town's secret festival recipe disappeared from a locked reading room.",
     "suspects": ["Librarian", "Chef", "Gardener"], "culprit": "Librarian",
     "clues": {"desk": "The lock was opened with a key, without damage. The sole room key was checked out by the librarian.",
               "garden": "The gardener's live-streamed tour continued from before the theft until after it.",
               "kitchen": "The chef received a photo of the stolen recipe from the librarian's account, timestamped during the theft."},
     "explanation": "The checked-out key and the librarian's timestamped photograph tie them to the missing recipe."},
]
ADVENTURE_STAGES = [
    {"place": "Whispering Woods", "scene": "A flooded path splits around an enormous sleeping creature.",
     "choices": ["Build a bridge", "Follow the fireflies"], "finds": ["Moonwood Staff", "Firefly Lantern"]},
    {"place": "The Sunken Archive", "scene": "A stone door asks for a story while a narrow stairway leads below.",
     "choices": ["Tell the door a story", "Explore the stairway"], "finds": ["Storykeeper's Cloak", "Tideglass Compass"]},
    {"place": "Skyglass Peak", "scene": "A stranded sky-whale guards the final crystal. It seems frightened.",
     "choices": ["Help the sky-whale", "Trade your discoveries"], "finds": ["Skyglass Charm", "Cloudforged Blade"]},
]


class RuleError(Exception):
    """An expected game-rule rejection that can be shown to a player."""


def day_key(now=None):
    return datetime.fromtimestamp(now if now is not None else time.time(), LOCAL).date().isoformat()


def week_key(now=None):
    date = datetime.fromtimestamp(now if now is not None else time.time(), LOCAL)
    year, week, _ = date.isocalendar()
    return f"{year}-W{week:02}"


def season_key(now=None):
    return datetime.fromtimestamp(now if now is not None else time.time(), LOCAL).strftime("%Y-%m")


class CommunityGame:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def guild(self, guild_id):
        key = str(guild_id)
        if key not in self.store.guilds:
            self.store.guilds[key] = {
                "pet": {"name": "Sprout", "xp": 0, "feeds": 0, "plays": 0, "accessories": [], "care": {}},
                "members": {}, "settings": {}, "quotes": [], "archives": [],
                "seasons": {}, "duel_rewards": {},
            }
            self.store.mark_dirty()
        return self.store.guilds[key]

    def member(self, guild_id, user_id):
        members = self.guild(guild_id)["members"]
        key = str(user_id)
        today = day_key(self.clock())
        if key not in members or members[key]["day"] != today:
            members[key] = {"day": today, "progress": {}, "claimed": [], "riddle_attempts": 0, "adventures": 0}
            self.store.mark_dirty()
        return members[key]

    def progress(self, guild_id, user_id, event):
        member = self.member(guild_id, user_id)
        target = QUESTS[event][1]
        member["progress"][event] = min(target, member["progress"].get(event, 0) + 1)
        self.store.mark_dirty()

    def claim_quests(self, guild_id, user_id):
        member = self.member(guild_id, user_id)
        total = 0
        for key, (_, target, reward) in QUESTS.items():
            if member["progress"].get(key, 0) >= target and key not in member["claimed"]:
                member["claimed"].append(key)
                total += reward
        if not total:
            raise RuleError("No completed, unclaimed quests yet. Use /quests to see today's tasks.")
        self.store.users[user_id]["balance"] += total
        self.guild(guild_id)["pet"]["xp"] += 10
        self.store.mark_dirty()
        return total

    def riddle(self, guild_id):
        ordinal = datetime.fromisoformat(day_key(self.clock())).toordinal()
        return RIDDLES[(ordinal + guild_id) % len(RIDDLES)]

    def solve_riddle(self, guild_id, user_id, answer):
        member = self.member(guild_id, user_id)
        if member["progress"].get("riddle"):
            raise RuleError("You already solved today's riddle.")
        if member["riddle_attempts"] >= 3:
            raise RuleError("You've used today's three guesses. A new riddle arrives tomorrow.")
        member["riddle_attempts"] += 1
        self.store.mark_dirty()
        if answer.strip().lower().rstrip(".!?") not in self.riddle(guild_id)[1]:
            raise RuleError(f"Not quite. Guesses left: {3 - member['riddle_attempts']}.")
        self.progress(guild_id, user_id, "riddle")

    def care_for_pet(self, guild_id, user_id, action):
        pet = self.guild(guild_id)["pet"]
        key = f"{user_id}:{action}"
        now = self.clock()
        cooldown = 3600 if action == "feed" else 1800
        remaining = cooldown - (now - pet["care"].get(key, 0))
        if remaining > 0:
            raise RuleError(f"You can {action} again in {int(remaining // 60) + 1} minutes.")
        cost = 50 if action == "feed" else 0
        if self.store.users[user_id]["balance"] < cost:
            raise RuleError("Feeding costs 50 coins. Playing is free!")
        self.store.users[user_id]["balance"] -= cost
        pet["care"][key] = now
        pet["feeds" if action == "feed" else "plays"] += 1
        pet["xp"] += 15 if action == "feed" else 10
        pet["last_care"] = now
        self.progress(guild_id, user_id, "pet")
        self.store.mark_dirty()
        return pet

    def pet_appearance(self, guild_id):
        pet = self.guild(guild_id)["pet"]
        stages = [(0, "🥚", "Egg"), (100, "🐣", "Hatchling"), (350, "🦊", "Forest Spirit"), (900, "🐉", "Sky Guardian")]
        stage = next(s for s in reversed(stages) if pet["xp"] >= s[0])
        mood = "sleepy" if self.clock() - pet.get("last_care", 0) > 86400 else "happy"
        personality = "playful" if pet["plays"] > pet["feeds"] else "gentle"
        return stage[1], stage[2], mood, personality

    def buy(self, guild_id, user_id, item_id):
        if item_id not in SHOP:
            raise RuleError("Unknown item. Use /shop to see the item IDs.")
        item = SHOP[item_id]
        user, pet = self.store.users[user_id], self.guild(guild_id)["pet"]
        owned = pet["accessories"] if item["slot"] == "pet" else user.setdefault("inventory", [])
        if item["slot"] == "title":
            if user.get("title_until", 0) > self.clock():
                raise RuleError("Your Local Legend title is still active.")
        elif item_id in owned:
            raise RuleError("This item is already owned.")
        if user["balance"] < item["price"]:
            raise RuleError(f"You need {item['price']:,} coins to buy this.")
        user["balance"] -= item["price"]
        if item_id not in owned:
            owned.append(item_id)
        if item["slot"] == "title":
            user["title_until"] = self.clock() + 7 * 86400
        elif item["slot"] != "pet":
            user.setdefault("equipped", {})[item["slot"]] = item_id
        self.store.mark_dirty()
        return item

    def equip(self, user_id, item_id):
        user = self.store.users[user_id]
        if item_id not in user.get("inventory", []) or item_id not in SHOP or SHOP[item_id]["slot"] == "pet":
            raise RuleError("Buy a profile item from /shop before equipping it.")
        item = SHOP[item_id]
        if item["slot"] == "title" and user.get("title_until", 0) <= self.clock():
            raise RuleError("That title has expired. You can buy it again.")
        user.setdefault("equipped", {})[item["slot"]] = item_id
        self.store.mark_dirty()

    def start_mystery(self, guild_id, user_id, channel_id):
        guild = self.guild(guild_id)
        now = self.clock()
        active = guild.get("mystery")
        if active and active["phase"] == "active" and now < active["expires"]:
            raise RuleError("A mystery is already running. Use /mystery status.")
        today = day_key(now)
        counts = guild.setdefault("mystery_count", {"day": today, "count": 0})
        if counts["day"] != today:
            counts.update(day=today, count=0)
        if counts["count"] >= 3:
            raise RuleError("This server has played today's three mysteries. Come back tomorrow.")
        case_index = (datetime.fromisoformat(today).toordinal() + counts["count"]) % len(CASES)
        counts["count"] += 1
        guild["mystery"] = {"case": case_index, "phase": "active", "owner": user_id,
                            "channel": channel_id, "expires": now + 3600, "clues": [], "players": [user_id], "guesses": []}
        self.store.mark_dirty()
        return guild["mystery"]

    def mystery(self, guild_id):
        mystery = self.guild(guild_id).get("mystery")
        if not mystery:
            raise RuleError("Start a case with /mystery start.")
        if mystery["phase"] == "active" and self.clock() >= mystery["expires"]:
            mystery["phase"] = "expired"
            self.store.mark_dirty()
        return mystery, CASES[mystery["case"]]

    def investigate(self, guild_id, user_id, location):
        game, case = self.mystery(guild_id)
        if game["phase"] != "active":
            raise RuleError("That case has ended. Start another with /mystery start.")
        location = location.strip().lower()
        if location not in case["clues"]:
            raise RuleError("Choose a location: " + ", ".join(case["clues"]))
        if user_id not in game["players"]:
            game["players"].append(user_id)
        if location not in game["clues"]:
            game["clues"].append(location)
        self.store.mark_dirty()
        return case["clues"][location]

    def accuse(self, guild_id, user_id, suspect):
        game, case = self.mystery(guild_id)
        if game["phase"] != "active":
            raise RuleError("This case is no longer active.")
        if len(game["clues"]) != len(case["clues"]):
            raise RuleError("Investigate every location before making an accusation.")
        if user_id in game["guesses"]:
            raise RuleError("You already made your accusation. Let another detective try.")
        normalized = next((s for s in case["suspects"] if s.casefold() == suspect.strip().casefold()), None)
        if normalized is None:
            raise RuleError("Choose a suspect: " + ", ".join(case["suspects"]))
        game["guesses"].append(user_id)
        self.store.mark_dirty()
        if normalized != case["culprit"]:
            raise RuleError("That accusation does not fit the evidence. Another detective can still solve the case.")
        game["phase"] = "solved"
        # Joining by guessing alone cannot collect a reward from someone else's work.
        if user_id not in game["players"]:
            game["players"].append(user_id)
        for uid in game["players"]:
            self.store.users[uid]["balance"] += 400
        self.guild(guild_id)["pet"]["xp"] += 30
        self.store.mark_dirty()
        return case["explanation"], game["players"]

    def create_adventure(self, guild_id, user_id, channel_id):
        guild, now = self.guild(guild_id), self.clock()
        active = guild.get("adventure")
        if active and active["phase"] in ("lobby", "active") and now < active["expires"]:
            raise RuleError("An adventure already exists. Use /adventure join or /adventure status.")
        self.check_adventure_limit(guild_id, user_id)
        game = {"id": uuid.uuid4().hex, "owner": user_id, "channel": channel_id, "players": [user_id],
                "phase": "lobby", "stage": 0, "votes": {}, "finds": [], "expires": now + 600}
        guild["adventure"] = game
        self.store.mark_dirty()
        return game

    def adventure(self, guild_id):
        game = self.guild(guild_id).get("adventure")
        if not game:
            raise RuleError("Create a party with /adventure create.")
        if game["phase"] == "lobby" and self.clock() >= game["expires"]:
            game["phase"] = "expired"
            self.store.mark_dirty()
        return game

    def check_adventure_limit(self, guild_id, user_id):
        if self.member(guild_id, user_id)["adventures"] >= 3:
            raise RuleError("You can join three adventures per day. Try again tomorrow.")

    def join_adventure(self, guild_id, user_id):
        game = self.adventure(guild_id)
        if game["phase"] != "lobby":
            raise RuleError("The party has already left or its lobby expired.")
        if user_id in game["players"]:
            raise RuleError("You're already in this party.")
        if len(game["players"]) >= 6:
            raise RuleError("The party is full (six players).")
        self.check_adventure_limit(guild_id, user_id)
        game["players"].append(user_id)
        self.store.mark_dirty()

    def begin_adventure(self, guild_id, user_id):
        game = self.adventure(guild_id)
        if game["owner"] != user_id or game["phase"] != "lobby":
            raise RuleError("Only the party leader can begin an open lobby.")
        # Recheck limits in case a lobby straddled a date boundary.
        for uid in game["players"]:
            self.check_adventure_limit(guild_id, uid)
        for uid in game["players"]:
            self.member(guild_id, uid)["adventures"] += 1
        game.update(phase="active", expires=self.clock() + 90)
        self.store.mark_dirty()
        return game

    def adventure_vote(self, guild_id, user_id, choice):
        game = self.adventure(guild_id)
        if game["phase"] != "active":
            raise RuleError("No adventure vote is open.")
        if user_id not in game["players"]:
            raise RuleError("Only party members can vote. Join the next lobby.")
        if self.clock() >= game["expires"]:
            raise RuleError("Voting closed. Use /adventure status to see the next stage.")
        if choice not in (1, 2):
            raise RuleError("Vote 1 or 2.")
        if str(user_id) in game["votes"]:
            raise RuleError("You already voted in this stage.")
        game["votes"][str(user_id)] = choice
        self.store.mark_dirty()
        return self.advance_adventure(guild_id)

    def advance_adventure(self, guild_id):
        game = self.adventure(guild_id)
        if game["phase"] != "active" or (len(game["votes"]) < len(game["players"]) and self.clock() < game["expires"]):
            return None
        if not game["votes"]:
            game["phase"] = "expired"
            self.store.mark_dirty()
            return "The party's vote expired without any votes. No rewards were issued."
        stage = ADVENTURE_STAGES[game["stage"]]
        ones = list(game["votes"].values()).count(1)
        twos = list(game["votes"].values()).count(2)
        # A tied vote follows the leader if they voted, otherwise option 1.
        choice = (1 if ones > twos else 2) if ones != twos else game["votes"].get(str(game["owner"]), 1)
        treasure = stage["finds"][choice - 1]
        game["finds"].append(treasure)
        game["stage"] += 1
        game["votes"] = {}
        text = f"The party chose **{stage['choices'][choice - 1]}** and discovered **{treasure}**."
        if game["stage"] == len(ADVENTURE_STAGES):
            game["phase"] = "complete"
            for uid in game["players"]:
                user = self.store.users[uid]
                user["balance"] += 600
                loot = user.setdefault("equipment", [])
                for found in game["finds"]:
                    if found not in loot:
                        loot.append(found)
                self.progress(guild_id, uid, "adventure")
            self.guild(guild_id)["pet"]["xp"] += 40
            text += "\nAdventure complete! Each party member earned **600 coins** and the discovered equipment."
        else:
            game["expires"] = self.clock() + 90
        self.store.mark_dirty()
        return text

    def nominate(self, guild_id, user_id, author_id, channel_id, message_id, content, jump_url):
        guild = self.guild(guild_id)
        week = week_key(self.clock())
        if any(q["message"] == message_id for q in guild["quotes"]):
            raise RuleError("That message was already nominated this week.")
        if sum(q["nominator"] == user_id and q["week"] == week for q in guild["quotes"]) >= 3:
            raise RuleError("You can nominate three quotes per week.")
        if len(guild["quotes"]) >= 100:
            raise RuleError("The quote board is full for this week.")
        if not content.strip():
            raise RuleError("Choose a message containing text.")
        quote = {"id": uuid.uuid4().hex[:8], "week": week, "author": author_id,
                 "channel": channel_id, "message": message_id, "text": content[:400],
                 "url": jump_url, "nominator": user_id, "votes": []}
        guild["quotes"].append(quote)
        self.store.mark_dirty()
        return quote

    def quote_vote(self, guild_id, user_id, quote_id):
        guild = self.guild(guild_id)
        quote = next((q for q in guild["quotes"] if q["id"] == quote_id and q["week"] == week_key(self.clock())), None)
        if quote is None:
            raise RuleError("That quote is not on this week's board.")
        if quote["author"] == user_id:
            raise RuleError("Vote for someone else's quote.")
        if user_id in quote["votes"]:
            raise RuleError("You've already voted for this quote.")
        for other in guild["quotes"]:
            if user_id in other["votes"]:
                other["votes"].remove(user_id)
        quote["votes"].append(user_id)
        self.store.mark_dirty()
        return quote

    def roll_quotes(self, guild_id):
        guild = self.guild(guild_id)
        week = week_key(self.clock())
        old = [q for q in guild["quotes"] if q["week"] != week]
        winners = []
        for old_week in sorted({q["week"] for q in old}):
            candidates = [q for q in old if q["week"] == old_week and q["votes"]]
            if candidates:
                winners.append(max(candidates, key=lambda q: len(q["votes"])))
        if old:
            guild["archives"].extend(winners)
            guild["archives"] = guild["archives"][-12:]
            guild["quotes"] = [q for q in guild["quotes"] if q["week"] == week]
            self.store.mark_dirty()
        return winners

    def record_duel(self, guild_id, players, winner):
        guild, now = self.guild(guild_id), self.clock()
        pair = ":".join(map(str, sorted(players)))
        reward_key = f"{day_key(now)}:{pair}"
        counts = guild["duel_rewards"]
        if counts.get(reward_key, 0) >= 5:
            return False
        counts[reward_key] = counts.get(reward_key, 0) + 1
        guild["duel_rewards"] = {k: v for k, v in counts.items() if k.startswith(day_key(now))}
        season = season_key(now)
        rankings = guild["seasons"].setdefault(season, {})
        for uid in players:
            entry = rankings.setdefault(str(uid), {"points": 0, "wins": 0, "ties": 0, "matches": 0})
            entry["matches"] += 1
            if winner is None:
                entry["points"] += 1
                entry["ties"] += 1
                self.store.users[uid]["balance"] += 50
            elif winner == uid:
                entry["points"] += 3
                entry["wins"] += 1
                self.store.users[uid]["balance"] += 200
        guild["seasons"] = {k: guild["seasons"][k] for k in sorted(guild["seasons"])[-12:]}
        self.store.mark_dirty()
        return True

    def question(self, guild_id, now=None):
        date = datetime.fromisoformat(day_key(self.clock() if now is None else now))
        digest = int(hashlib.sha256(str(guild_id).encode()).hexdigest()[:8], 16)
        return QUESTIONS[(date.toordinal() + digest) % len(QUESTIONS)]
