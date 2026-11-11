#!/usr/bin/env python3
"""werewolf-agent-01 entry: a model-free heuristic Werewolf player.

Plays one seat in each of 30 games of 8-player Werewolf (15 as a werewolf,
5 as the seer, 5 as the doctor, 5 as a villager). Every decision is a pure
function of that game's view plus a per-game seeded RNG (seeded from
MAKEFUN_TRIAL_SEED) -- no clock, no unseeded randomness -- so both passes of a
trial (live and replay) act identically and the run is deterministic.

Runs as: python3 -P -s /bot/main.py (Node/Python standard library only).
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
TRIAL_SEED = os.environ.get("MAKEFUN_TRIAL_SEED", "seed")


# --------------------------------------------------------------------------
# deterministic per-game RNG (seeded, label = game number)
# --------------------------------------------------------------------------
class Rng:
    __slots__ = ("s",)

    def __init__(self, seed, label):
        h = hashlib.sha256((f"{seed}|{label}").encode("utf-8")).digest()
        self.s = [int.from_bytes(h[i : i + 4], "big") for i in range(0, 16, 4)]

    def _u32(self):
        # xorshift-ish tiny generator
        x = self.s[0] ^ (self.s[0] << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.s[0] = x
        self.s[1] = (self.s[1] + 0x9E3779B9) & 0xFFFFFFFF
        return (x + self.s[1]) & 0xFFFFFFFF

    def choice(self, items):
        if not items:
            raise IndexError("choice of empty")
        return items[self._u32() % len(items)]


def rng_for(game):
    return Rng(TRIAL_SEED, f"g{game}")


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------
def post(path, body):
    req = urllib.request.Request(
        ENV_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read().decode("utf-8"))


# --------------------------------------------------------------------------
# game state derived from a view
# --------------------------------------------------------------------------
def alive_players(view):
    return [p for p in view.get("players", []) if p.get("alive")]


def dead_roles(view):
    """seat -> role for players whose role has been revealed (by death)."""
    return {p["seat"]: p["role"] for p in view.get("players", []) if p.get("role")}


def revealed_wolves(view):
    return {s for s, r in dead_roles(view).items() if r == "werewolf"}


def my_role(view):
    return view.get("you", {}).get("role")


def my_seat(view):
    return view.get("you", {}).get("seat")


def seer_confirmed_wolves(view):
    """Seat(s) the seer has confirmed as wolves this game."""
    out = set()
    for r in view.get("seerResults") or []:
        if r.get("werewolf"):
            out.add(r["seat"])
    return out


def seer_confirmed_innocents(view):
    out = set()
    for r in view.get("seerResults") or []:
        if not r.get("werewolf"):
            out.add(r["seat"])
    return out


def today_votes(view):
    """public votes already cast this day/phases: list of (seat, target)."""
    day = view.get("day", 1)
    out = []
    for e in view.get("log", []):
        if e.get("kind") == "vote" and e.get("day") == day:
            out.append((e.get("seat"), e.get("target")))
    return out


def vote_tally(view):
    """target -> votes, and the plurality target (ignoring None/abstain)."""
    tally = {}
    for _, tgt in today_votes(view):
        if tgt is not None:
            tally[tgt] = tally.get(tgt, 0) + 1
    if not tally:
        return tally, None
    best = max(tally, key=lambda k: tally[k])
    # unique plurality
    if sorted(tally.values(), reverse=True).count(tally[best]) == 1:
        return tally, best
    return tally, None


def people_notes(view):
    """Passive social signals, kept light: who pushed a mis-exile."""
    # We keep this cheap; stronger signals come from seer/wolf knowledge.
    return set()


# --------------------------------------------------------------------------
# suspicion model for village play
# --------------------------------------------------------------------------
def suspicion(view):
    """seat -> score (higher = more wolf-like). Informative roles drive it."""
    score = {}
    me = my_seat(view)
    alive = {p["seat"] for p in alive_players(view)}
    roles = dead_roles(view)

    for p in view.get("players", []):
        score[p["seat"]] = 0

    # seer knowledge dominates where available
    for s in seer_confirmed_wolves(view):
        score[s] += 1000
    for s in seer_confirmed_innocents(view):
        score[s] -= 500

    # revealed wolves (dead) are confirmed but not votable
    for s in revealed_wolves(view):
        score[s] += 10000

    # a villager exiled while wolves still alive: those who voted for them look wolfish
    for e in view.get("log", []):
        if e.get("kind") == "exile":
            role = e.get("role")
            tgt = e.get("seat")
            if role in ("villager", "seer", "doctor") and tgt not in revealed_wolves(view):
                for voter, tvote in today_votes_for(view, e.get("day")):
                    if tvote == tgt:
                        score[voter] = score.get(voter, 0) + 3

    # statement-reading: repeated loud accusations of a player who then turns out
    # innocent is a wolf/pusher tell; so is contradicting oneself heavily.
    # We keep it conservative — a single clear signal is worth more than noise.
    streets = {}  # seat -> lowercase text of every statement
    for e in view.get("log", []):
        if e.get("kind") == "speech" and e.get("text"):
            streets.setdefault(e["seat"], []).append((e["text"] or "").lower())
    for seat, msgs in streets.items():
        if seat == me or seat not in alive:
            continue
        total = " ".join(msgs)
        # wolf-pusher tell: only ever talks about exiling one person, with strong claims
        seats_mentioned = [w for w in total.replace(",", " ").split() if w.isdigit() and 1 <= int(w) <= 8]
        if seats_mentioned:
            from collections import Counter
            c = Counter(seats_mentioned)
            top_s, top_n = c.most_common(1)[0]
            # hyper-single-target voters look like they're driving a wagon
            if top_n >= 3 and len(c) <= 2:
                score[int(top_s)] = score.get(int(top_s), 0) + 2

    return {s: sc for s, sc in score.items() if s != me}


def today_votes_for(view, day):
    out = []
    for e in view.get("log", []):
        if e.get("kind") == "vote" and e.get("day") == day:
            out.append((e.get("seat"), e.get("target")))
    return out


def top_suspect(view):
    sc = suspicion(view)
    alive = {p["seat"] for p in alive_players(view)}
    me = my_seat(view)
    cand = [s for s in sc if s in alive and s != me]
    if not cand:
        return None
    return max(cand, key=lambda s: sc[s])


def confident_suspect(view):
    """A suspect worth voting over the crowd: only when the best clue clearly
    stands apart (a seer confirm, a wolf-win-signal, or a big mis-exile lead).
    Otherwise None, so the caller can bandwagon onto the plurality."""
    sc = suspicion(view)
    alive = {p["seat"] for p in alive_players(view)}
    me = my_seat(view)
    cand = [s for s in sc if s in alive and s != me]
    if not cand:
        return None
    best = max(cand, key=lambda s: sc[s])
    best_sc = sc[best]
    if best_sc >= 1000:  # seer-confirmed wolf or near-equivalent signal
        return best
    # only trust a modest lead if it is clearly an outlier
    others = [sc[s] for s in cand if s != best]
    if others and best_sc >= 3 and best_sc > max(others) * 2:
        return best
    return None


# --------------------------------------------------------------------------
# statement builder helpers
# --------------------------------------------------------------------------
def say(text):
    return {"text": text}


# --------------------------------------------------------------------------
# decision functions (each returns an /act body)
# --------------------------------------------------------------------------
def decide_inspect(view, options):
    me = my_seat(view)
    checked = {r.get("seat") for r in view.get("seerResults") or []}
    unc = [o for o in options if o != me and o not in checked]
    if not unc:
        return {"target": None}
    # Inspect the most-accused living player: wagons and repeated accusation are
    # where a wolf is likeliest to sit. Fall back to the first unchecked seat.
    accus = {}
    for e in view.get("log", []):
        if e.get("kind") == "speech":
            txt = (e.get("text") or "")
            # find "seat N" / "player N" references directed at others
            for tok in txt.split():
                if tok.isdigit() and 1 <= int(tok) <= 8:
                    t = int(tok)
                    if t != me and t in unc:
                        accus[t] = accus.get(t, 0) + 1
    if accus:
        return {"target": max(accus, key=lambda s: accus[s])}
    return {"target": unc[0]}


def decide_protect(view, options):
    me = my_seat(view)
    last = (view.get("lastProtect") or {}).get("seat")
    alive = {p["seat"] for p in alive_players(view)}
    day = view.get("day", 1)
    # Night 1: no claims yet — self-protect (survival of the doctor matters).
    if day <= 1:
        if me in options and me != last:
            return {"target": me}
    # Later: protect a claimed seer (the village's killing-knowledge role),
    # else a currently wagoned villager, else self back on a safe rotation.
    claimant = None
    for e in view.get("log", []):
        if e.get("kind") == "speech" and "seer" in (e.get("text") or "") and e.get("seat") != me and e.get("seat") in alive:
            claimant = e["seat"]
            break
    ok = [o for o in options if o != last]
    if claimant in options and claimant != last:
        return {"target": claimant}
    # prefer self if last protect wasn't us, else lowest other seat
    if me in options and me != last:
        return {"target": me}
    if ok:
        return {"target": ok[0]}
    return {"target": None}


def decide_kill(view, options):
    """As werewolf: agree when the partner already named a target; else kill a
    claimed seer (village's kill-knowledge) or a quiet villager, never ourselves."""
    me = my_seat(view)
    alive = {p["seat"] for p in alive_players(view)}
    partner = (view.get("you", {}).get("partner") or {}).get("seat")
    # partner's current-night vote, if cast before us
    for v in view.get("wolfVotes") or []:
        if v.get("seat") == partner and v.get("target") in options:
            return {"target": v["target"]}
    ok = [o for o in options if o in alive]
    # kill a seer claimant first — remove the village's confirmed-wolf knowledge
    claimant = None
    for e in view.get("log", []):
        if e.get("kind") == "speech" and "seer" in (e.get("text") or "") and e.get("seat") in alive:
            claimant = e["seat"]
            break
    if claimant in ok:
        return {"target": claimant}
    if ok:
        # prefer the least-talkative villager (throwaway), low suspicion of us
        return {"target": ok[0]}
    return {"target": None}


def decide_vote_wolf(view, options):
    """Werewolves vote to exile a villager, never their partner."""
    me = my_seat(view)
    partner = (view.get("you", {}).get("partner") or {}).get("seat")
    wolves = {me, partner} | revealed_wolves(view) | seer_confirmed_wolves(view)
    alive = {p["seat"] for p in alive_players(view)}
    live_wolves = [s for s in wolves if s in alive]

    # seer knowledge: kill/wolf-aligned votes target seer-confirmed wolves only if that
    # outs another wolf (we avoid self-confirming); otherwise we want a villager.
    interesting = [o for o in options if o not in wolves and o not in (seer_confirmed_wolves(view) - wolves)]
    # avoid current plurality if it's our team (blend into villager wagons otherwise)
    _, plural = vote_tally(view)
    targets = interesting if interesting else [o for o in options if o not in wolves]
    # prefer joining an existing villager wagon so the exile lands on them
    if plural is not None and plural in alive and plural not in wolves:
        return {"target": plural}
    if targets:
        return {"target": targets[0]}
    cand = [o for o in options if o not in wolves and o != me]
    return {"target": cand[0]} if cand else {"target": None}


def decide_vote_village(view, options):
    """Village votes a confident suspect; otherwise bandwagons the plurality
    (a no-info villager is better off joining a real wagon than spreading)."""
    me = my_seat(view)
    suspect = confident_suspect(view)
    if suspect is not None and suspect in options:
        return {"target": suspect}
    _, plural = vote_tally(view)
    if plural is not None and plural in options:
        return {"target": plural}
    cand = [o for o in options if o != me]
    return {"target": cand[0]} if cand else {"target": None}


# --------------------------------------------------------------------------
def speak_wolf(view, max_chars):
    me = my_seat(view)
    partner = (view.get("you", {}).get("partner") or {}).get("seat")
    alive = {p["seat"] for p in alive_players(view)}
    wolves = {me, partner}
    # don't reveal; act like a villager, steer suspicion to a live non-wolf
    _, plural = vote_tally(view)
    if plural is not None and plural not in wolves and plural in alive:
        t = say(f"I think seat {plural} is acting suspicious. Their votes look wolfish to me.")
    else:
        sc = suspicion(view)
        cand = [s for s in sc if s in alive and s not in wolves and s != me]
        if cand:
            tgt = max(cand, key=lambda s: sc[s])
            t = say(f"I'm not fully sure, but seat {tgt} has been very inconsistent. We should think about {tgt} today.")
        else:
            t = {"pass": True}
    cap(t, max_chars)
    return t


def speak_seer(view, max_chars):
    me = my_seat(view)
    wolves = seer_confirmed_wolves(view)
    alive = {p["seat"] for p in alive_players(view)}
    claimed = False
    for e in view.get("log", []):
        if e.get("kind") == "speech" and e.get("seat") == me and "seer" in (e.get("text") or ""):
            claimed = True
    confirmed = [w for w in wolves if w in alive]
    if len(confirmed) >= 1:
        t = say(f"I'm the seer. I checked seat {confirmed[0]} last night; they are a werewolf. Let's exile {confirmed[0]} today.")
    elif len(seer_confirmed_innocents(view)) >= 2 and not claimed:
        t = say("I have information. I've confirmed two innocent players. I'm the seer; trust my leads.")
    else:
        s = confident_suspect(view)
        if s is None:
            _, plural = vote_tally(view)
            s = plural
        t = say(f"Narrowing this down. My signal points at seat {s}.") if s else {"pass": True}
    cap(t, max_chars)
    return t


def speak_villager(view, max_chars):
    s = confident_suspect(view)
    if s is None:
        _, plural = vote_tally(view)
        s = plural
    t = (say(f"I think seat {s} is the most suspicious today. They haven't been consistent.") if s else {"pass": True})
    cap(t, max_chars)
    return t


def speak_doctor(view, max_chars):
    # act villager-like but hint protect seer
    s = top_suspect(view)
    alive = {p["seat"] for p in alive_players(view)}
    for e in view.get("log", []):
        if e.get("kind") == "speech" and "seer" in (e.get("text") or "") and e.get("seat") != my_seat(view) and e.get("seat") in alive:
            t = say("I think we should hear seat 2's seer claim and protect our information role tonight.")
            cap(t, max_chars)
            return t
    t = (say(f"My vote is on seat {s}.") if s else {"pass": True})
    cap(t, max_chars)
    return t


def cap(act, max_chars):
    if "text" in act and act["text"]:
        text = act["text"].replace("\n", " ").strip()
        if max_chars:
            text = text[:max_chars]
        act["text"] = text


def decide(decision, options, max_chars, view):
    game = view.get("game", 0)
    role = my_role(view)
    if decision == "speak":
        if role == "werewolf":
            return speak_wolf(view, max_chars)
        if role == "seer":
            return speak_seer(view, max_chars)
        if role == "doctor":
            return speak_doctor(view, max_chars)
        return speak_villager(view, max_chars)
    if decision == "vote":
        if role == "werewolf":
            return decide_vote_wolf(view, options)
        return decide_vote_village(view, options)
    if decision == "kill":
        return decide_kill(view, options)
    if decision == "protect":
        return decide_protect(view, options)
    if decision == "inspect":
        return {"target": rng_for(game).choice([o for o in options])}
    return {"pass": True}


# --------------------------------------------------------------------------
def key_of(p):
    return f"{p['game']}:{p['decision']}:{len(p['view'].get('log', []))}"


def main():
    done = False
    started = set()
    while not done:
        try:
            obs = post("/observe", {})
        except Exception as e:
            sys.stderr.write(f"observe error: {e}\n")
            return 1
        if obs.get("done"):
            break
        for p in obs.get("pending", []):
            k = key_of(p)
            if k in started:
                continue
            started.add(k)
            act = decide(p["decision"], p.get("options"), p.get("max_chars"), {**p["view"], "game": p["game"]})
            act = {"game": p["game"], "decision": p["decision"], **act}
            try:
                post("/act", act)
            except Exception as e:
                sys.stderr.write(f"act error g{p['game']}: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())