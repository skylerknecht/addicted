"""
League of Legends Game Analyzer — Web UI
No external dependencies. Run: python analyzer.py
"""
import json, ssl, subprocess, base64, urllib.request, urllib.error, urllib.parse
import os, sys, time, random, threading, shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

PORT = 8394
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Riot API key — loaded from --api-key CLI argument at startup
API_KEY = ""

QUEUE_NAMES = {
    420: "Ranked Solo", 440: "Ranked Flex", 400: "Normal Draft", 430: "Normal Blind",
    450: "ARAM", 700: "Clash", 900: "URF", 1020: "One for All", 1300: "Nexus Blitz",
    1400: "Ultimate Spellbook", 490: "Quickplay", 480: "Quickplay",
}

# ─── Riot API ────────────────────────────────────────────────────────────────

_req_times = []
_rate_info = {"used_2m": 0, "used_1s": 0, "total": 0, "waiting": False}

def get_rate_info():
    now = time.time()
    recent_2m = [t for t in _req_times if now - t < 120]
    recent_1s = [t for t in _req_times if now - t < 1]
    return {"used2m": len(recent_2m), "max2m": 100, "used1s": len(recent_1s), "max1s": 20, "total": _rate_info["total"], "waiting": _rate_info["waiting"]}

def riot_get(url, api_key, retries=3):
    global _req_times
    now = time.time()
    _req_times = [t for t in _req_times if now - t < 120]
    if len(_req_times) >= 95:
        wait = 121 - (now - _req_times[0])
        if wait > 0:
            _rate_info["waiting"] = True
            print(f"  [RATE] waiting {wait:.0f}s...")
            time.sleep(wait)
            _rate_info["waiting"] = False
    recent = [t for t in _req_times if time.time() - t < 1]
    if len(recent) >= 18: time.sleep(0.15)
    _req_times.append(time.time())
    _rate_info["total"] += 1

    req = urllib.request.Request(url)
    req.add_header("X-Riot-Token", api_key)
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        if e.code == 429 and retries > 0:
            ra = int(e.headers.get("Retry-After", "3") or "3")
            print(f"  [429] waiting {ra+1}s ({retries} left)")
            time.sleep(ra + 1)
            return riot_get(url, api_key, retries - 1)
        if e.code == 403:
            return None, "KEY_EXPIRED"
        return None, f"API {e.code}"
    except Exception as e:
        return None, str(e)

def get_routing(region):
    if region.startswith("euw") or region.startswith("eun") or region.startswith("tr") or region.startswith("ru"):
        return "europe"
    if region.startswith("kr") or region.startswith("jp"):
        return "asia"
    return "americas"

# ─── Champion ID -> Name (from DDragon, cached for 24h) ──────────────────────
_champ_id_to_name = {}
_champ_id_loaded = 0
def get_champion_names():
    global _champ_id_to_name, _champ_id_loaded
    if _champ_id_to_name and time.time() - _champ_id_loaded < 86400:
        return _champ_id_to_name
    try:
        with urllib.request.urlopen("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10) as r:
            versions = json.loads(r.read().decode())
        ver = versions[0] if versions else "14.24.1"
        with urllib.request.urlopen(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json", timeout=10) as r:
            data = json.loads(r.read().decode())
        m = {}
        for cname, c in data.get("data", {}).items():
            try: m[int(c["key"])] = c["id"]
            except Exception: pass
        _champ_id_to_name = m
        _champ_id_loaded = time.time()
        print(f"[CHAMPS] Loaded {len(m)} champions from DDragon v{ver}")
    except Exception as e:
        print(f"[CHAMPS] Failed: {e}")
    return _champ_id_to_name

# ─── Live Game (Spectator V5) ────────────────────────────────────────────────
def fetch_live_game(api_key, puuid, region="na1"):
    url = f"https://{region}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    data, err = riot_get(url, api_key)
    if err == "KEY_EXPIRED": return None, "KEY_EXPIRED"
    if err and "404" in err: return None, "NOT_IN_GAME"
    if err: return None, err
    if not data: return None, "NOT_IN_GAME"
    cm = get_champion_names()
    participants = []
    for p in data.get("participants", []):
        cid = p.get("championId", 0)
        participants.append({
            "puuid": p.get("puuid", ""),
            "riotId": p.get("riotId", ""),
            "championId": cid,
            "championName": cm.get(cid, str(cid)),
            "championKey": cm.get(cid, str(cid)),
            "teamId": p.get("teamId", 100),
            "spell1Id": p.get("spell1Id", 0),
            "spell2Id": p.get("spell2Id", 0),
        })
    bans = []
    for b in data.get("bannedChampions", []):
        cid = b.get("championId", 0)
        bans.append({
            "championId": cid,
            "championName": cm.get(cid, "") if cid > 0 else "",
            "championKey": cm.get(cid, "") if cid > 0 else "",
            "teamId": b.get("teamId", 0),
            "pickTurn": b.get("pickTurn", 0),
        })
    return {
        "gameId": data.get("gameId"),
        "queueId": data.get("gameQueueConfigId", 0),
        "queueName": QUEUE_NAMES.get(data.get("gameQueueConfigId", 0), "Custom"),
        "gameStartTime": data.get("gameStartTime", 0),
        "gameLength": data.get("gameLength", 0),
        "mapId": data.get("mapId"),
        "participants": participants,
        "bannedChampions": bans,
    }, None

# ─── Timeline ────────────────────────────────────────────────────────────────

def extract_timeline(frames, pid):
    cs, gold, xp, lvl, positions = [], [], [], [], []
    dmg, dmg_taken, vision = [], [], []
    items, wards = [], []
    kills, deaths, ward_places, ward_kills, objectives = [], [], [], [], []
    summ_count = 0
    summ_cumulative = []
    for frame in frames:
        pf = frame.get("participantFrames", {}).get(str(pid))
        if pf:
            cs.append(pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0))
            gold.append(pf.get("totalGold", 0))
            xp.append(pf.get("xp", 0))
            lvl.append(pf.get("level", 1))
            ds = pf.get("damageStats", {})
            dmg.append(ds.get("totalDamageDoneToChampions", 0))
            dmg_taken.append(ds.get("totalDamageTaken", 0))
            # Vision score not in participantFrames per-minute, approximate from ward events
            pos = pf.get("position", {})
            positions.append({"x": pos.get("x", 0), "y": pos.get("y", 0)})
        for ev in frame.get("events", []):
            t = round(ev.get("timestamp", 0) / 60000, 1)
            pos = ev.get("position", {})
            x, y = pos.get("x", 0), pos.get("y", 0)
            etype = ev.get("type", "")

            if etype == "ITEM_PURCHASED" and ev.get("participantId") == pid:
                items.append({"item": ev["itemId"], "time": t})
            elif etype == "WARD_PLACED" and ev.get("creatorId") == pid and ev.get("wardType", "") != "UNDEFINED":
                wards.append({"type": ev.get("wardType", ""), "time": t})
                wx, wy = x, y
                if wx == 0 and wy == 0 and pf:
                    pp = pf.get("position", {})
                    wx, wy = pp.get("x", 0), pp.get("y", 0)
                ward_places.append({"x": wx, "y": wy, "time": t, "type": ev.get("wardType", "")})
            elif etype == "WARD_KILL" and ev.get("killerId") == pid:
                wkx, wky = x, y
                if wkx == 0 and wky == 0 and pf:
                    pp = pf.get("position", {})
                    wkx, wky = pp.get("x", 0), pp.get("y", 0)
                ward_kills.append({"x": wkx, "y": wky, "time": t})
            elif etype == "CHAMPION_KILL":
                if ev.get("killerId") == pid:
                    kills.append({"x": x, "y": y, "time": t, "victimId": ev.get("victimId")})
                elif ev.get("victimId") == pid:
                    deaths.append({"x": x, "y": y, "time": t, "killerId": ev.get("killerId")})
            elif etype == "ELITE_MONSTER_KILL" and ev.get("killerId") == pid:
                objectives.append({"x": x, "y": y, "time": t, "monster": ev.get("monsterType", ""), "sub": ev.get("monsterSubType", "")})
            elif etype == "BUILDING_KILL" and ev.get("killerId") == pid:
                objectives.append({"x": x, "y": y, "time": t, "monster": "TURRET", "sub": ev.get("buildingType", "")})
            elif etype == "SKILL_LEVEL_UP" or etype == "LEVEL_UP":
                pass  # skip
            # Count summoner spell usage
            if etype == "CHAMPION_SPECIAL_KILL" and ev.get("killerId") == pid:
                pass
        # Track cumulative summoner spell count per frame
        summ_cumulative.append(summ_count)

    # Build vision curve from ward placements + ward kills (approximation)
    total_frames = len(cs)
    vis = []
    ward_events = len(wards) + len(ward_kills)
    for fi in range(total_frames):
        minute = fi
        wc = sum(1 for w in wards if w.get("time", 999) <= minute) + sum(1 for w in ward_kills if w.get("time", 999) <= minute)
        vis.append(wc)

    # Build cumulative kill/death curves per minute
    total_frames = len(cs)
    kill_curve, death_curve = [], []
    for fi in range(total_frames):
        kc = sum(1 for k in kills if k.get("time", 999) <= fi)
        dc = sum(1 for d in deaths if d.get("time", 999) <= fi)
        kill_curve.append(kc)
        death_curve.append(dc)

    return {"cs": cs, "gold": gold, "xp": xp, "lvl": lvl, "dmg": dmg, "dmgTaken": dmg_taken, "vis": vis,
            "killCurve": kill_curve, "deathCurve": death_curve,
            "positions": positions, "items": items, "wards": wards,
            "kills": kills, "deaths": deaths, "wardPlaces": ward_places, "wardKills": ward_kills, "objectives": objectives}

# ─── Fetch Matches from Riot API ─────────────────────────────────────────────

def fetch_matches(api_key, name, tag, region="na1", count=0):
    routing = get_routing(region)
    # Resolve Riot ID
    acct, err = riot_get(f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{urllib.request.quote(name)}/{urllib.request.quote(tag)}", api_key)
    if err: return None, err
    if not acct: return None, "Player not found"
    puuid = acct.get("puuid")
    # Use the API-returned capitalization, not what the user typed
    name = acct.get("gameName", name)
    tag = acct.get("tagLine", tag)

    # DDragon version
    dd_ver = "14.24.1"
    try:
        with urllib.request.urlopen("https://ddragon.leagueoflegends.com/api/versions.json", timeout=3) as r:
            v = json.loads(r.read().decode())
            if v: dd_ver = v[0]
    except Exception: pass

    # Get ranked info
    rank = None
    rdata, _ = riot_get(f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}?queue=RANKED_SOLO_5x5", api_key)
    if rdata and isinstance(rdata, list):
        for q in rdata:
            if q.get("queueType") == "RANKED_SOLO_5x5":
                rank = {"tier": q.get("tier",""), "division": q.get("rank",""),
                        "lp": q.get("leaguePoints",0), "wins": q.get("wins",0), "losses": q.get("losses",0)}

    # Light mode — no games requested
    if count <= 0:
        return {
            "summoner": {"name": name, "tag": tag, "puuid": puuid},
            "rank": rank, "ddVersion": dd_ver,
            "summary": {"games": 0, "wins": 0, "losses": 0, "avgKda": 0, "avgCs": 0},
            "games": [],
        }, None

    # Fetch match IDs
    mids, err = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&type=ranked&count={max(count*2,25)}", api_key)
    if err: return None, err
    if not mids: return None, "No matches found"

    # Fetch each match
    games = []
    for mid in mids:
        if len(games) >= count: break
        m, _ = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}", api_key)
        if not m: continue
        info = m.get("info", {})
        dur = info.get("gameDuration", 0)
        if dur < 300: continue
        gid = info.get("gameId", 0)
        queue = info.get("queueId", 0)
        ts = info.get("gameCreation", 0)

        me = None
        for p in info.get("participants", []):
            if p.get("puuid") == puuid: me = p; break
        if not me: continue

        dm = dur / 60
        k, d, a = me.get("kills",0), me.get("deaths",0), me.get("assists",0)
        ch = me.get("challenges", {})
        cs = me.get("totalMinionsKilled",0) + me.get("neutralMinionsKilled",0)
        role = me.get("teamPosition", me.get("individualPosition", ""))
        champ_name = me.get("championName", "")
        champ_id = me.get("championId", 0)

        items = [{"id": me.get(f"item{ii}",0), "name": ""} for ii in range(7) if me.get(f"item{ii}",0)]

        multi = None
        for mk in ["pentaKills","quadraKills","tripleKills","doubleKills"]:
            if me.get(mk, 0) > 0:
                multi = mk.replace("Kills"," Kill").replace("penta","Penta").replace("quadra","Quadra").replace("triple","Triple").replace("double","Double")
                break

        players = []
        for p in info.get("participants", []):
            players.append({
                "name": p.get("riotIdGameName", p.get("summonerName", "?")),
                "championName": p.get("championName", ""),
                "championKey": p.get("championName", ""),
                "team": p.get("teamId", 0),
                "kills": p.get("kills",0), "deaths": p.get("deaths",0), "assists": p.get("assists",0),
                "cs": p.get("totalMinionsKilled",0) + p.get("neutralMinionsKilled",0),
                "damage": p.get("totalDamageDealtToChampions",0),
                "gold": p.get("goldEarned",0), "vision": p.get("visionScore",0),
                "isMe": p.get("puuid") == puuid,
                "role": p.get("teamPosition", ""),
                "puuid": p.get("puuid", ""),
            })

        # Averages — exclude supports from CS/gold since they're fundamentally different
        non_sup = [p for p in players if p["role"] != "UTILITY"]
        if not non_sup: non_sup = players  # fallback
        avg_dmg = sum(p["damage"] for p in players) / max(len(players),1)
        avg_gold = sum(p["gold"] for p in non_sup) / max(len(non_sup),1)
        avg_vis = sum(p["vision"] for p in players) / max(len(players),1)
        avg_cs_m = sum(p["cs"] for p in non_sup) / max(len(non_sup),1) / max(dm,1)
        avg_kda = sum((p["kills"]+p["assists"])/max(p["deaths"],1) for p in players) / max(len(players),1)
        kps = [round(p.get("challenges",{}).get("killParticipation",0)*100) for p in info.get("participants",[])]
        avg_kp = round(sum(kps)/max(len(kps),1)) if kps else None

        time_str, time_ago = "?", ""
        if isinstance(ts, (int,float)) and ts > 0:
            dt = datetime.fromtimestamp(ts/1000)
            time_str = dt.strftime("%b %d  %I:%M %p")
            diff = (datetime.now()-dt).total_seconds()
            time_ago = f"{int(diff//3600)}h ago" if diff < 86400 else f"{int(diff//86400)}d ago"

        games.append({
            "gameId": gid, "time": time_str, "timeAgo": time_ago,
            "duration": dur, "queue": QUEUE_NAMES.get(queue, ""),
            "champion": champ_name, "championId": champ_id, "champKey": champ_name,
            "win": me.get("win", False),
            "kills": k, "deaths": d, "assists": a,
            "kda": round((k+a)/max(d,1), 2),
            "cs": cs, "csMin": round(cs/max(dm,1), 1),
            "damage": me.get("totalDamageDealtToChampions",0),
            "gold": me.get("goldEarned",0),
            "vision": me.get("visionScore",0),
            "killParticipation": round(ch.get("killParticipation",0)*100) if ch.get("killParticipation") is not None else None,
            "items": items, "multikill": multi, "role": role,
            "players": players,
            "averages": {"cs": round(avg_cs_m,1), "damage": round(avg_dmg), "gold": round(avg_gold), "vision": round(avg_vis), "kda": round(avg_kda,2), "kp": avg_kp},
        })
        print(f"  [{len(games)}/{count}] {champ_name} {role} {'W' if me.get('win') else 'L'}")

    wins = sum(1 for g in games if g["win"])
    return {
        "summoner": {"name": name, "tag": tag, "puuid": puuid},
        "rank": rank, "ddVersion": dd_ver,
        "summary": {
            "games": len(games), "wins": wins, "losses": len(games)-wins,
            "avgKda": round(sum(g["kda"] for g in games)/max(len(games),1), 2),
            "avgCs": round(sum(g["csMin"] for g in games)/max(len(games),1), 1),
        },
        "games": games,
    }, None

# ─── Aggregation ─────────────────────────────────────────────────────────────

def _aggregate_games(games, timelines):
    def avg_curve(key):
        curves = [t[key] for t in timelines if t.get(key)]
        if not curves: return []
        ml = max(len(c) for c in curves)
        half = len(curves) / 2
        result = []
        for i in range(ml):
            contributing = [c[i] for c in curves if i < len(c)]
            if len(contributing) < half: break
            result.append(round(sum(contributing) / len(contributing), 3))
        return result
    def avg(key): return round(sum(g[key] for g in games) / len(games), 3)

    item_agg = {}
    for tl in timelines:
        for ib in tl.get("items", []):
            iid = ib["item"]
            if iid not in item_agg: item_agg[iid] = []
            item_agg[iid].append(ib["time"])
    item_builds = []
    for iid, times in sorted(item_agg.items(), key=lambda x: sum(x[1])/len(x[1])):
        if len(times) >= len(games) * 0.25:
            item_builds.append({"item": iid, "avgTime": round(sum(times)/len(times), 3), "count": len(times)})

    raw_builds = [tl.get("items", []) for tl in timelines[:3]]

    # Store raw per-minute curves for every game so we can recompute/fix aggregations later
    raw_timelines = [{
        "cs": tl.get("cs", []),
        "gold": tl.get("gold", []),
        "xp": tl.get("xp", []),
        "dmg": tl.get("dmg", []),
        "dmgTaken": tl.get("dmgTaken", []),
        "vis": tl.get("vis", []),
        "killCurve": tl.get("killCurve", []),
        "deathCurve": tl.get("deathCurve", []),
    } for tl in timelines]

    return {
        "averages": {"kda": avg("kda"), "csMin": avg("csMin"), "dmgMin": avg("dmgMin"), "goldMin": avg("goldMin"), "visMin": avg("visMin"), "deathsMin": avg("deathsMin"), "damage": avg("damage"), "gold": avg("gold"), "vision": avg("vision"), "kp": avg("kp")},
        "timeline": {"cs": avg_curve("cs"), "gold": avg_curve("gold"), "xp": avg_curve("xp"), "dmg": avg_curve("dmg"), "dmgTaken": avg_curve("dmgTaken"), "vis": avg_curve("vis"), "killCurve": avg_curve("killCurve"), "deathCurve": avg_curve("deathCurve")},
        "itemBuilds": item_builds, "rawBuilds": raw_builds,
        "rawTimelines": raw_timelines,
        "games": len(games),
        "winRate": round(sum(1 for g in games if g["win"]) / len(games) * 100, 2),
    }

# ─── Masters+ ───────────────────────────────────────────────────────────────

def fetch_masters_role(api_key, role, region="na1", force=False, target_games=15):
    cache_file = os.path.join(CACHE_DIR, f"masters_{role}_{region}.json")
    if not force and os.path.exists(cache_file):
        try:
            with open(cache_file) as f: cached = json.load(f)
            if time.time() - cached.get("_ts", 0) < 2592000: return cached, None  # 30 days
        except Exception: pass

    routing = get_routing(region)
    entries = []
    for tier in ["challengerleagues", "grandmasterleagues", "masterleagues"]:
        data, err = riot_get(f"https://{region}.api.riotgames.com/lol/league/v4/{tier}/by-queue/RANKED_SOLO_5x5", api_key)
        if err == "KEY_EXPIRED": return None, "KEY_EXPIRED"
        if data: entries.extend(data.get("entries", []))
    print(f"  {len(entries)} Masters+ players, target={target_games}g")

    entries.sort(key=lambda e: -e.get("leaguePoints", 0))
    pool_size = max(50, target_games * 3)
    puuids = [e["puuid"] for e in entries[:pool_size] if e.get("puuid")]

    games, timelines = [], []
    for puuid in puuids:
        if len(games) >= target_games: break
        mids, _ = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&type=ranked&count=5", api_key)
        if not mids: continue
        for mid in mids:
            if len(games) >= target_games: break
            match, _ = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}", api_key)
            if not match: continue
            info = match.get("info", {})
            dur = info.get("gameDuration", 0)
            if dur < 900: continue
            player = None
            for p in info.get("participants", []):
                if p.get("puuid") == puuid and p.get("teamPosition", "") == role:
                    player = p; break
            if not player: continue
            ch = player.get("challenges", {})
            cs = player.get("totalMinionsKilled",0) + player.get("neutralMinionsKilled",0)
            dm = dur / 60
            games.append({"kda": round((player["kills"]+player["assists"])/max(player["deaths"],1),2), "csMin": round(cs/max(dm,1),1), "dmgMin": round(player.get("totalDamageDealtToChampions",0)/max(dm,1)), "goldMin": round(player.get("goldEarned",0)/max(dm,1)), "visMin": round(player.get("visionScore",0)/max(dm,1),1), "damage": player.get("totalDamageDealtToChampions",0), "gold": player.get("goldEarned",0), "vision": player.get("visionScore",0), "kp": round(ch.get("killParticipation",0)*100), "duration": dur, "win": player.get("win",False), "deathsMin": round(player.get("deaths",0)/max(dm,1),2)})
            print(f"  [{len(games)}/{target_games}] {role} game")
            tl, _ = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}/timeline", api_key)
            if tl: timelines.append(extract_timeline(tl.get("info",{}).get("frames",[]), player["participantId"]))

    if not games: return None, f"No Masters+ {role} games found."
    agg = _aggregate_games(games, timelines)
    agg["_ts"] = time.time(); agg["role"] = role; agg["region"] = region; agg["players"] = len(puuids)
    with open(cache_file, "w") as f: json.dump(agg, f)
    return agg, None

def fetch_all_masters(api_key, region="na1", target_per_role=30, start_date=None):
    """Fetch Masters+ data for ALL roles efficiently in one pass."""
    routing = get_routing(region)
    ALL_ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

    # 1. Get league entries (3 calls)
    entries = []
    for tier in ["challengerleagues", "grandmasterleagues", "masterleagues"]:
        data, err = riot_get(f"https://{region}.api.riotgames.com/lol/league/v4/{tier}/by-queue/RANKED_SOLO_5x5", api_key)
        if err == "KEY_EXPIRED": return "KEY_EXPIRED"
        if data: entries.extend(data.get("entries", []))
    entries.sort(key=lambda e: -e.get("leaguePoints", 0))
    puuid_set = set(e["puuid"] for e in entries[:300] if e.get("puuid"))
    top100 = [e["puuid"] for e in entries[:100] if e.get("puuid")]
    print(f"[MASTERS] {len(entries)} total, using top 100 for match IDs, top 300 puuid pool")

    # 2. Fetch match IDs (100 calls)
    all_mids = set()
    for i, puuid in enumerate(top100):
        start_param = ""
        if start_date:
            try:
                import calendar
                st = int(calendar.timegm(time.strptime(start_date, "%Y-%m-%d")))
                start_param = f"&startTime={st}"
            except Exception: pass
        mids, _ = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&type=ranked&count=20{start_param}", api_key)
        if mids: all_mids.update(mids)
        if (i + 1) % 20 == 0: print(f"[MASTERS] Match IDs: {i+1}/100 players, {len(all_mids)} unique matches")
    print(f"[MASTERS] {len(all_mids)} unique match IDs from 100 players")

    # 3. Fetch matches + timelines, sort into roles
    role_games = {r: [] for r in ALL_ROLES}
    role_timelines = {r: [] for r in ALL_ROLES}
    mid_list = list(all_mids)
    random.shuffle(mid_list)
    fetched = 0

    for mid in mid_list:
        # Check if all roles are full
        if all(len(role_games[r]) >= target_per_role for r in ALL_ROLES):
            break

        match, err = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}", api_key)
        if not match: continue
        info = match.get("info", {})
        dur = info.get("gameDuration", 0)
        if dur < 900: continue
        fetched += 1

        # Find ALL Masters+ participants and sort into roles
        dm = dur / 60
        need_tl = []
        for p in info.get("participants", []):
            if p.get("puuid") not in puuid_set: continue
            role = p.get("teamPosition", "")
            if role not in ALL_ROLES or len(role_games[role]) >= target_per_role: continue
            ch = p.get("challenges", {})
            cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
            game = {
                "kda": round((p["kills"] + p["assists"]) / max(p["deaths"], 1), 2),
                "csMin": round(cs / max(dm, 1), 1),
                "dmgMin": round(p.get("totalDamageDealtToChampions", 0) / max(dm, 1)),
                "goldMin": round(p.get("goldEarned", 0) / max(dm, 1)),
                "visMin": round(p.get("visionScore", 0) / max(dm, 1), 1),
                "deathsMin": round(p.get("deaths", 0) / max(dm, 1), 2),
                "damage": p.get("totalDamageDealtToChampions", 0),
                "gold": p.get("goldEarned", 0),
                "vision": p.get("visionScore", 0),
                "kp": round(ch.get("killParticipation", 0) * 100),
                "duration": dur, "win": p.get("win", False),
                "champion": p.get("championName", ""),
                "summoner": p.get("riotIdGameName", ""),
                "kills": p.get("kills", 0),
                "deaths_count": p.get("deaths", 0),
                "assists": p.get("assists", 0),
            }
            role_games[role].append(game)
            need_tl.append((role, p.get("participantId")))

        if not need_tl: continue

        # Fetch timeline once, extract for all players found
        tl = None
        for attempt in range(3):
            tl, tl_err = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}/timeline", api_key)
            if tl: break
            if tl_err and "429" in str(tl_err): time.sleep(2)
            else: break
        if tl:
            frames = tl.get("info", {}).get("frames", [])
            for r, pid in need_tl:
                role_timelines[r].append(extract_timeline(frames, pid))

        counts = " ".join(f"{r[0]}:{len(role_games[r])}" for r in ALL_ROLES)
        tl_counts = " ".join(f"{r[0]}:{len(role_timelines[r])}" for r in ALL_ROLES)
        if fetched % 10 == 0:
            print(f"[MASTERS] {fetched} matches fetched | Games: {counts} | TLs: {tl_counts}")

    # 4. Aggregate and save each role
    for role in ALL_ROLES:
        games = role_games[role]
        tls = role_timelines[role]
        if not games:
            print(f"[MASTERS] {role}: no games"); continue
        agg = _aggregate_games(games, tls)
        agg["_ts"] = time.time()
        agg["role"] = role
        agg["region"] = region
        agg["players"] = len(puuid_set)
        agg["rawGames"] = games
        cache_file = os.path.join(CACHE_DIR, f"masters_{role}_{region}.json")
        with open(cache_file, "w") as f: json.dump(agg, f)
        print(f"[MASTERS] {role}: {len(games)}g, {len(tls)} timelines saved")

    print(f"[MASTERS] Done! {fetched} matches fetched, {get_rate_info()['total']} total API calls")
    return None

# ─── Comparable ──────────────────────────────────────────────────────────────

def _fetch_player_games(api_key, puuid, role, routing, count=10, max_age_days=30):
    start_time = int(time.time()) - (max_age_days * 86400)
    mids, err = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&type=ranked&count={count * 2}&startTime={start_time}", api_key)
    if err or not mids: return [], []
    games, timelines = [], []
    for mid in mids:
        if len(games) >= count: break
        match, _ = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}", api_key)
        if not match: continue
        info = match.get("info", {})
        dur = info.get("gameDuration", 0)
        if dur < 900: continue
        player = None
        for p in info.get("participants", []):
            if p.get("puuid") == puuid and p.get("teamPosition", "") == role:
                player = p; break
        if not player: continue
        ch = player.get("challenges", {})
        cs = player.get("totalMinionsKilled",0) + player.get("neutralMinionsKilled",0)
        dm = dur / 60
        pos = player.get("teamPosition", player.get("individualPosition", ""))
        games.append({"kda": round((player["kills"]+player["assists"])/max(player["deaths"],1),2), "csMin": round(cs/max(dm,1),1), "dmgMin": round(player.get("totalDamageDealtToChampions",0)/max(dm,1)), "goldMin": round(player.get("goldEarned",0)/max(dm,1)), "visMin": round(player.get("visionScore",0)/max(dm,1),1), "damage": player.get("totalDamageDealtToChampions",0), "gold": player.get("goldEarned",0), "vision": player.get("visionScore",0), "kp": round(ch.get("killParticipation",0)*100), "duration": dur, "win": player.get("win",False), "role": pos, "deathsMin": round(player.get("deaths",0)/max(dm,1),2)})
        print(f"    [{len(games)}] {pos}")
        tl, _ = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}/timeline", api_key)
        if tl: timelines.append(extract_timeline(tl.get("info",{}).get("frames",[]), player["participantId"]))
    return games, timelines

def fetch_comparable(api_key, riot_id, role, region="na1", count=10, user_game_id=None, max_age_days=30, champ_id=None):
    routing = get_routing(region)
    parts = riot_id.split("#")
    if len(parts) != 2: return None, "Use format: Name#Tag"
    acct, err = riot_get(f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{urllib.request.quote(parts[0].strip())}/{urllib.request.quote(parts[1].strip())}", api_key)
    if err == "KEY_EXPIRED": return None, "KEY_EXPIRED"
    if not acct: return None, f"Player '{riot_id}' not found."
    puuid = acct.get("puuid")
    rn = {"TOP":"Top","JUNGLE":"Jungle","MIDDLE":"Mid","BOTTOM":"ADC","UTILITY":"Support"}.get(role, role)
    print(f"  Resolved {riot_id} -> {rn}")
    games, timelines = _fetch_player_games(api_key, puuid, role, routing, count, max_age_days)
    if not games: return None, f"{riot_id} has no {rn} games in the last {max_age_days} days."
    agg = _aggregate_games(games, timelines)
    agg["_ts"] = time.time(); agg["role"] = role; agg["player"] = riot_id; agg["players"] = 1
    if user_game_id and champ_id:
        agg["userTimeline"] = fetch_user_timeline(api_key, user_game_id, champ_id, region)
    return agg, None

def fetch_user_timeline(api_key, game_id, champ_id, region):
    routing = get_routing(region)
    riot_id = f"{region.upper()}_{game_id}"
    print(f"  [USER TL] {riot_id} champ={champ_id}")
    match, err = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{riot_id}", api_key)
    if not match: print(f"  [USER TL] fail: {err}"); return None
    tl, err = riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{riot_id}/timeline", api_key)
    if not tl: print(f"  [USER TL] fail: {err}"); return None
    frames = tl.get("info", {}).get("frames", [])
    # Extract timeline for ALL players
    me_tl = None
    all_players = []
    for p in match.get("info", {}).get("participants", []):
        pid = p["participantId"]
        ptl = extract_timeline(frames, pid)
        is_me = p.get("championId") == champ_id
        all_players.append({
            "champion": p.get("championName", ""),
            "championId": p.get("championId", 0),
            "team": p.get("teamId", 0),
            "role": p.get("teamPosition", ""),
            "isMe": is_me,
            "timeline": {"cs": ptl["cs"], "gold": ptl["gold"], "xp": ptl["xp"], "dmg": ptl["dmg"], "dmgTaken": ptl["dmgTaken"], "vis": ptl["vis"], "killCurve": ptl["killCurve"], "deathCurve": ptl["deathCurve"]},
        })
        if is_me: me_tl = ptl
    if me_tl:
        me_tl["allPlayers"] = all_players
        print(f"  [USER TL] OK {len(me_tl.get('cs',[]))} frames, {len(all_players)} players")
    return me_tl

# ─── HTML ────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Addicted</title>
<style>
:root{--bg:#0a0e14;--s:#131920;--s2:#1a2230;--s3:#1f2b3a;--bd:#2a3545;--t:#c5cdd9;--t2:#7a8a9e;--ac:#c89b3c;--ac2:#f0e6d2;--w:#28a745;--l:#dc3545;--bl:#3b82f6;--av:#f0ad4e}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--t);min-height:100vh}
.ld{display:flex;align-items:center;justify-content:center;height:100vh;font-size:1.2rem;color:var(--ac);flex-direction:column;gap:16px}
.sp{width:36px;height:36px;border:3px solid var(--bd);border-top-color:var(--ac);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.err{color:var(--l);text-align:center;padding:40px}

.bar{background:var(--s);border-bottom:1px solid var(--bd);padding:10px 20px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.bar h1{font-size:1rem;color:var(--ac);font-weight:600;margin-right:8px}
.bar input{background:var(--bg);border:1px solid var(--bd);color:var(--t);border-radius:5px;padding:6px 10px;font-size:.82rem;outline:none}
.bar input:focus{border-color:var(--ac)}
.bar .rid-input{width:170px}
.bar .ubtn{background:var(--ac);color:#000;border:none;padding:6px 18px;border-radius:5px;cursor:pointer;font-size:.82rem;font-weight:600}
.bar .ubtn:hover{opacity:.9}
.bar .ubtn:disabled{opacity:.5;cursor:wait}

.main{max-width:1100px;margin:0 auto;padding:20px}
.cards{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}
.sc{background:var(--s);border:1px solid var(--bd);border-radius:10px;padding:14px 18px;flex:1;min-width:170px}
.sc .st{font-size:.68rem;color:var(--t2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.sc .sv{font-size:1.5rem;font-weight:700;color:var(--ac2)}
.sc .ss{font-size:.8rem;color:var(--t2);margin-top:2px}
.wrb{height:5px;background:var(--l);border-radius:3px;margin-top:6px;overflow:hidden}
.wrf{height:100%;background:var(--w);border-radius:3px}

.gl{display:flex;flex-direction:column;gap:6px}
.gc{background:var(--s);border:1px solid var(--bd);border-left:none;border-radius:8px;display:grid;grid-template-columns:4px 1fr;overflow:hidden;cursor:pointer;transition:border-color .15s}
.gc:hover{border-color:var(--t2)}
.gc.exp{border-color:var(--ac);border-left:1px solid var(--ac)}
.ws{background:var(--l)}.gc.w .ws{background:var(--w)}.gc.exp .ws{background:transparent}
.gr{display:grid;grid-template-columns:130px 1fr 150px 110px;align-items:center;padding:10px 14px;gap:10px}
@media(max-width:800px){.gr{grid-template-columns:1fr}}
.gm .q{font-size:.72rem;color:var(--ac);font-weight:600;text-transform:uppercase}
.gm .ti{font-size:.7rem;color:var(--t2)}
.gm .re{font-size:.7rem;font-weight:700}.gm .re.w{color:var(--w)}.gm .re.l{color:var(--l)}
.gm .du{font-size:.7rem;color:var(--t2)}
.gc-c{display:flex;align-items:center;gap:12px}
.ci{width:40px;height:40px;border-radius:50%;overflow:hidden;border:2px solid var(--bd);flex-shrink:0}
.ci img{width:100%;height:100%;object-fit:cover}
.isq{width:24px;height:24px;background:var(--s2);border-radius:3px;border:1px solid var(--bd);overflow:hidden}
.isq img{width:100%;height:100%;object-fit:cover;display:block}
.gc-i{display:flex;gap:2px}
.gc-k .kn{font-size:.95rem;font-weight:700;color:var(--ac2)}
.gc-k .kr{font-size:.72rem;color:var(--t2)}.gc-k .kr.g{color:var(--w)}.gc-k .kr.a{color:var(--av)}.gc-k .kr.b{color:var(--l)}
.gc-k .mk{font-size:.62rem;font-weight:700;padding:2px 5px;border-radius:3px;background:rgba(200,155,60,.2);color:var(--ac);margin-top:2px;display:inline-block}
.gc-s{font-size:.75rem;color:var(--t2);line-height:1.5}.gc-s span{color:var(--ac2);font-weight:600}

.gd{padding:0 14px 14px 18px;display:none;overflow:hidden}.gc.exp .gd{display:block}
.tabs{display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap}
.tb{background:var(--s2);color:var(--t2);border:1px solid var(--bd);padding:5px 12px;border-radius:5px;cursor:pointer;font-size:.75rem}
.tb:hover{border-color:var(--t2);color:var(--t)}.tb.on{border-color:var(--ac);color:var(--ac);background:rgba(200,155,60,.1)}

.ds{background:var(--s2);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.ds h4{font-size:.68rem;color:var(--ac);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}

.btn{background:var(--ac);color:#000;border:none;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:.78rem;font-weight:600}
.btn:hover{opacity:.9}.btn:disabled{opacity:.5;cursor:wait}
.rbtn-circle{width:26px;height:26px;border-radius:50%;background:var(--s2);border:1px solid var(--bd);color:var(--t2);cursor:pointer;font-size:.8rem;display:inline-flex;align-items:center;justify-content:center;padding:0;margin-left:auto}
.rbtn-circle:hover{border-color:var(--ac);color:var(--ac)}

.sb{width:100%;border-collapse:collapse;font-size:.72rem}
.sb th{text-align:left;padding:4px 5px;color:var(--t2);font-size:.62rem;text-transform:uppercase;border-bottom:1px solid var(--bd)}
.sb td{padding:3px 5px;border-bottom:1px solid rgba(42,53,69,.4)}
.sb tr.me td{color:var(--ac2);font-weight:600}
.sb .ts td{padding:2px;border-bottom:2px solid var(--bd)}
.sb .tb2{border-left:2px solid var(--bl)}.sb .tr2{border-left:2px solid #ef4444}

.tl-legend{display:flex;gap:14px;font-size:.7rem;color:var(--t2);margin-top:4px}
.tl-tabs{display:flex;gap:3px;margin-bottom:4px}
.tl-t{background:var(--bg);color:var(--t2);border:1px solid var(--bd);padding:3px 10px;border-radius:3px;cursor:pointer;font-size:.68rem}
.tl-t.on{border-color:var(--ac);color:var(--ac)}

.itl{display:flex;flex-wrap:wrap;gap:6px;padding:4px 0}
.ite{display:flex;align-items:center;gap:5px;background:var(--bg);padding:3px 7px 3px 3px;border-radius:5px}
.ite .it{font-size:.75rem;color:var(--ac2);font-weight:600}
.ite .ic{font-size:.65rem;color:var(--t2)}


.mod{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;display:flex;align-items:center;justify-content:center}
.mdl{background:var(--s);border:1px solid var(--bd);border-radius:10px;padding:22px 26px;width:440px;max-width:90vw}
.mdl h2{font-size:1rem;color:var(--ac);margin-bottom:4px}
.mdl p{font-size:.8rem;color:var(--t2);margin-bottom:12px}

.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;height:60vh;color:var(--t2);font-size:.9rem;gap:12px}
.empty-state h2{color:var(--ac);font-size:1.3rem}
.ptag{font-size:.7rem;font-weight:700;margin-top:3px;padding:2px 6px;border-radius:3px;display:inline-block}
.stag{font-size:.58rem;padding:1px 4px;border-radius:3px;margin-left:3px;white-space:nowrap;font-weight:600}
.stag.god{background:rgba(200,155,60,.15);color:#c89b3c}
.stag.avg{background:rgba(122,138,158,.12);color:#7a8a9e}
.stag.bad{background:rgba(239,68,68,.15);color:#ef4444}
.rate-bar{background:var(--s);border-bottom:1px solid var(--bd);padding:3px 20px;display:flex;align-items:center;gap:12px;font-size:.65rem;color:var(--t2);position:sticky;top:0;z-index:50}
.rate-meter{width:120px;height:6px;background:var(--bg);border-radius:3px;overflow:hidden;position:relative}
.rate-fill{height:100%;border-radius:3px;transition:width .3s}
.rate-ok{background:var(--w)}.rate-warn{background:var(--av)}.rate-danger{background:var(--l)}
</style>
</head>
<body>
<div id="staleWarn" style="display:none;background:#f59e0b22;color:#f59e0b;padding:6px 20px;font-size:.72rem;align-items:center;gap:8px;border-bottom:1px solid #f59e0b44"></div>
<div id="rateBar" class="rate-bar"></div>
<div id="app"></div>
<script>
var $=function(s){return document.querySelector(s);};
var fmt=function(n){return n.toLocaleString();};
var fmtD=function(s){var m=Math.floor(s/60);return m+':'+(('0'+(s%60)).slice(-2));};
var fv=function(v){if(v==='-'||v==null)return'-';if(typeof v==='string')return v;return v>999?fmt(v):v;};
var D=null,exp=null,views={},comp={},compLD={},mastersData={},mastersLD={},userTL={};
var liveGame=null,liveGameLoading=false,liveGameExpanded=false;
function toggleLiveGame(){liveGameExpanded=!liveGameExpanded;render();}
var COLORS=['#ef4444','#22c55e','#a855f7','#ec4899','#14b8a6','#e11d48','#8b5cf6','#f97316','#10b981','#d946ef'];

function getReg(){return localStorage.getItem('region')||'na1';}
function setReg(r){localStorage.setItem('region',r);}

// Wrapper: parse JSON safely, surface useful error if response isn't JSON
async function safeJSON(promise){
  var r=await promise;
  var txt=await r.text();
  if(!txt)throw new Error('Empty response (HTTP '+r.status+'). Likely a proxy timeout.');
  try{return JSON.parse(txt);}
  catch(e){
    var preview=txt.substring(0,200).replace(/\s+/g,' ');
    throw new Error('Bad response (HTTP '+r.status+'). Server returned: '+preview);
  }
}
var REGIONS=[['na1','NA'],['euw1','EUW'],['eun1','EUNE'],['kr','KR'],['br1','BR'],['la1','LAN'],['la2','LAS'],['oc1','OCE'],['tr1','TR'],['ru','RU'],['jp1','JP']];
function regionSelect(){
  var cur=getReg();
  var opts='';REGIONS.forEach(function(r){opts+='<option value="'+r[0]+'"'+(r[0]===cur?' selected':'')+'>'+r[1]+'</option>';});
  return '<select id="regionSelect" onchange="setReg(this.value)" style="background:var(--bg);border:1px solid var(--bd);color:var(--t);border-radius:5px;padding:5px;font-size:.78rem">'+opts+'</select>';
}
var CHAMP_KEYS={'Jarvan IV':'JarvanIV','Wukong':'MonkeyKing','Renata Glasc':'Renata','Nunu & Willump':'Nunu','Dr. Mundo':'DrMundo','Kog\'Maw':'KogMaw','Vel\'Koz':'VelKoz','Kha\'Zix':'KhaZix','Cho\'Gath':'ChoGath','Rek\'Sai':'RekSai','Kai\'Sa':'Kaisa','Bel\'Veth':'Belveth','K\'Sante':'KSante','Xin Zhao':'XinZhao','Lee Sin':'LeeSin','Master Yi':'MasterYi','Miss Fortune':'MissFortune','Tahm Kench':'TahmKench','Twisted Fate':'TwistedFate','Aurelion Sol':'AurelionSol'};
function champKey(name){return CHAMP_KEYS[name]||name.replace(/[^a-zA-Z]/g,'');}
function dd(t,k){if(t==='champion')k=champKey(k);return'https://ddragon.leagueoflegends.com/cdn/'+((D&&D.ddVersion)||'14.24.1')+'/img/'+t+'/'+k+'.png';}

function popup(msg){
  var m=document.createElement('div');m.className='mod';
  m.innerHTML='<div class="mdl"><h2>Error</h2><p>'+msg+'</p><div style="text-align:right"><button class="btn" onclick="this.closest(\'.mod\').remove()">OK</button></div></div>';
  document.body.appendChild(m);m.onclick=function(e){if(e.target===m)m.remove();};
}

function renderEmpty(){
  var h='<div class="bar"><h1>Addicted</h1>';
  h+='<input class="rid-input" id="riotIdInput" placeholder="Summoner#Tag">'+regionSelect();
  h+='<span style="font-size:.7rem;color:var(--t2);margin-left:6px">games</span>';
  h+='<input id="recentCount" type="number" value="10" min="0" max="100" style="background:var(--bg);border:1px solid var(--bd);color:var(--t);border-radius:5px;padding:5px;font-size:.78rem;width:60px">';
  h+='<button class="ubtn" id="updateBtn" onclick="doUpdate()">Load</button>';
  h+='</div>';
  h+='<div class="empty-state"><h2>Addicted</h2><p>Enter your Riot ID above to get started.</p></div>';
  $('#app').innerHTML=h;
}

async function doUpdate(){
  var rid=$('#riotIdInput').value.trim();
  if(!rid||rid.indexOf('#')<0){popup('Enter Riot ID as Name#Tag');return;}
  var parts=rid.split('#');
  var ct=parseInt(($('#recentCount')||{}).value);
  if(isNaN(ct)||ct<0)ct=10;
  var btn=$('#updateBtn');
  btn.disabled=true;btn.textContent='Loading...';
  try{
    var d=await safeJSON(fetch('/api/matches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:parts[0].trim(),tag:parts[1].trim(),region:getReg(),count:ct})}));
    if(d.error){popup(d.error);btn.disabled=false;btn.textContent='Load';return;}
    D=d.data;
    try{var mc=await safeJSON(fetch('/api/masters-cache?region='+getReg()));if(mc)mastersData=mc;}catch(e){}
    comp={};userTL={};exp=null;views={};
    render();
    if(ct>0)fetchAllTimelines();
    // Auto-check live game on update
    fetchLiveGame();
  }catch(e){popup('Failed: '+e.message);}
  btn.disabled=false;btn.textContent='Update';
}

function fetchAllTimelines(){
  if(!D||!D.games)return;
  var queue=[];
  D.games.forEach(function(g,i){
    if(!userTL[i]&&g.gameId)queue.push(i);
  });
  function next(){
    if(!queue.length)return;
    var idx=queue.shift();
    var g=D.games[idx];
    fetch('/api/user-timeline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gameId:g.gameId,championId:g.championId,region:getReg()})})
      .then(function(r){return r.json();})
      .then(function(d){
        if(d.ok&&d.timeline){userTL[idx]=d.timeline;render();}
        next();
      }).catch(function(){next();});
  }
  next();next();
}

function fetchAllRanks(){
  if(!D||!D.games)return;
  var allPuuids={};
  D.games.forEach(function(g){
    if(g.players)g.players.forEach(function(p){if(p.puuid&&!(p.puuid in playerRanks))allPuuids[p.puuid]=true;});
  });
  var puuids=Object.keys(allPuuids);
  if(!puuids.length)return;
  // Batch 20 at a time
  var batches=[];
  for(var i=0;i<puuids.length;i+=20)batches.push(puuids.slice(i,i+20));
  function nextBatch(){
    if(!batches.length)return;
    var batch=batches.shift();
    fetch('/api/player-ranks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({puuids:batch,region:getReg()})})
      .then(function(r){return r.json();})
      .then(function(d){
        if(d.ok&&d.ranks){Object.keys(d.ranks).forEach(function(pu){playerRanks[pu]=d.ranks[pu];});render();}
        nextBatch();
      }).catch(function(){nextBatch();});
  }
  nextBatch();
}

var GOOD_TAGS={deaths:'Unkillable',cs:'Minion Killer',dmg:'Killer',vis:'Hawkeye',xp:'Over Leveled'};
var BAD_TAGS={deaths:'Inting',cs:'Minion Lover',dmg:'Pacifist',vis:'Blind',xp:'Under Leveled'};
var TAG_LABELS={deaths:'Deaths',cs:'CS',dmg:'Damage',vis:'Vision',xp:'XP'};
var TAG_MET={deaths:'deathCurve',cs:'cs',dmg:'dmg',vis:'vis',xp:'xp'};

function getTLData(tl,met){
  if(met==='kd'){
    var kc=tl.killCurve||[],dc=tl.deathCurve||[];
    var out=[];for(var ii=0;ii<Math.min(kc.length,dc.length);ii++){out.push(dc[ii]>0?kc[ii]/dc[ii]:kc[ii]>0?kc[ii]*2:1);}
    return out;
  }
  return tl[met]||[];
}

function curveRatio(playerCurve,mastersCurve){
  if(!playerCurve||!mastersCurve)return null;
  var len=Math.min(playerCurve.length,mastersCurve.length);
  var sum=0,count=0;
  for(var i=3;i<len;i++){
    if(mastersCurve[i]>0){sum+=playerCurve[i]/mastersCurve[i];count++;}
  }
  return count>0?sum/count:null;
}
function deathRatio(playerDeaths,mastersDeaths){
  if(!playerDeaths||!mastersDeaths)return null;
  var len=Math.min(playerDeaths.length,mastersDeaths.length);
  var sum=0,count=0;
  for(var i=3;i<len;i++){
    if(playerDeaths[i]===0&&mastersDeaths[i]===0){sum+=1;count++;}
    else if(mastersDeaths[i]>0){sum+=playerDeaths[i]/mastersDeaths[i];count++;}
  }
  return count>0?sum/count:null;
}

function statTag(playerTL,role){
  var ref=mastersData[role];
  if(!ref||!ref.timeline)return[];
  var mtl=ref.timeline;
  var stats={
    cs:{curve:playerTL.cs,master:mtl.cs},
    dmg:{curve:playerTL.dmg,master:mtl.dmg},
    vis:{curve:playerTL.vis,master:mtl.vis},
    xp:{curve:playerTL.xp,master:mtl.xp}
  };
  var tags=[];
  Object.keys(stats).forEach(function(k){
    var s=stats[k];
    var ratio=curveRatio(s.curve,s.master);
    if(ratio===null)return;
    var pct=Math.round(ratio*100);
    var base={pct:pct,stat:k,inv:false,desc:TAG_LABELS[k]+' at '+pct+'% of Masters+',met:TAG_MET[k]};
    if(ratio<0.8)tags.push(Object.assign({t:BAD_TAGS[k],tier:'bad'},base));
    else if(ratio>=1.0)tags.push(Object.assign({t:GOOD_TAGS[k],tier:'god'},base));
    else tags.push(Object.assign({t:TAG_LABELS[k],tier:'avg'},base));
  });
  var dr=deathRatio(playerTL.deathCurve,mtl.deathCurve);
  if(dr!==null){
    var dpct=Math.round(dr*100);
    var dbase={pct:dpct,stat:'deaths',inv:true,desc:TAG_LABELS.deaths+' at '+dpct+'% of Masters+',met:TAG_MET.deaths};
    if(dr>1.0)tags.push(Object.assign({t:BAD_TAGS.deaths,tier:'bad'},dbase));
    else if(dr<0.8)tags.push(Object.assign({t:GOOD_TAGS.deaths,tier:'god'},dbase));
    else tags.push(Object.assign({t:TAG_LABELS.deaths,tier:'avg'},dbase));
  }
  return tags;
}

// 20 points per category. 100% of Masters+ = 20 pts. Total 100 = Masters level. >100 = Smurfing, <80 = Ran It.
function computeScore(tags){
  if(!tags.length)return null;
  var total=0;
  tags.forEach(function(t){
    var ratio=t.pct/100;
    var pts=t.inv?20*Math.max(0,2-ratio):20*ratio;
    total+=pts;
  });
  return Math.round(total);
}
function scoreTier(score){
  if(score>100)return 'god';
  if(score<80)return 'bad';
  return 'avg';
}
function scoreBadge(score,size){
  if(score===null)return '';
  var tier=scoreTier(score);
  var cc=tier==='god'?['#c89b3c','rgba(200,155,60,.15)']:tier==='bad'?['#ef4444','rgba(239,68,68,.15)']:['#7a8a9e','rgba(122,138,158,.12)'];
  var sz=size||32;
  var fs=Math.max(11,sz*0.48);
  var w=Math.round(sz*1.15);
  var r=Math.max(4,Math.round(sz*0.18));
  return '<span style="display:inline-flex;align-items:center;justify-content:center;width:'+w+'px;height:'+sz+'px;border-radius:'+r+'px;color:'+cc[0]+';background:'+cc[1]+';font-weight:800;font-size:'+fs+'px;font-family:Inter,-apple-system,BlinkMacSystemFont,system-ui,sans-serif;letter-spacing:-.3px;flex-shrink:0" title="20pts per stat. 100=Masters level. >100=Smurfing, <80=Ran It">'+score+'</span>';
}

function render(){
  if(!D){renderEmpty();return;}
  var s=D.summoner,sm=D.summary,rk=D.rank,games=D.games;
  var wr=sm.games?Math.round(sm.wins/sm.games*100):0;
  var h='<div class="bar"><h1>Addicted</h1>';
  h+='<input class="rid-input" id="riotIdInput" value="'+s.name+'#'+s.tag+'">'+regionSelect();
  h+='<span style="font-size:.7rem;color:var(--t2);margin-left:6px">games</span>';
  h+='<input id="recentCount" type="number" value="'+(sm.games||10)+'" min="0" max="100" style="background:var(--bg);border:1px solid var(--bd);color:var(--t);border-radius:5px;padding:5px;font-size:.78rem;width:60px">';
  h+='<button class="ubtn" id="updateBtn" onclick="doUpdate()">Update</button>';
  h+='</div><div class="main"><div class="cards" style="flex-direction:column">';
  h+='<div class="sc" style="width:100%"><div style="display:flex;gap:16px;flex-wrap:wrap;align-items:stretch">';
  if(rk){var rkImg='https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-mini-crests/'+rk.tier.toLowerCase()+'.svg';
  h+='<div style="padding-right:16px;border-right:1px solid var(--bd)"><div style="color:var(--ac);font-weight:700;font-size:1rem;margin-bottom:6px">'+s.name+'#'+s.tag+'</div><div style="display:flex;align-items:center;gap:10px"><img src="'+rkImg+'" style="width:36px;height:36px" onerror="this.style.display=\'none\'"><div><div style="color:var(--ac2);font-weight:600;font-size:.9rem">'+rk.tier+' '+rk.division+'</div><div style="font-size:.65rem;color:var(--t2)">'+rk.lp+' LP · '+rk.wins+'W '+rk.losses+'L</div></div></div></div>';}
  else{h+='<div style="padding-right:16px;border-right:1px solid var(--bd)"><div style="color:var(--ac);font-weight:700;font-size:1rem">'+s.name+'#'+s.tag+'</div></div>';}
  h+='<div style="display:flex;flex-direction:column;justify-content:center;gap:4px;padding-right:16px;border-right:1px solid var(--bd)">';
  if(sm.games>0){
    h+='<div><div style="font-size:1.1rem;font-weight:700;color:var(--ac2);line-height:1.1">'+wr+'%</div><div style="font-size:.65rem;color:var(--t2)">Last '+sm.games+'g · '+sm.wins+'W '+sm.losses+'L</div><div class="wrb" style="width:120px;margin-top:2px"><div class="wrf" style="width:'+wr+'%"></div></div></div>';
    h+='<div><div style="font-size:1.1rem;font-weight:700;color:var(--ac2);line-height:1.1">'+sm.avgKda+' <span style="font-size:.65rem;color:var(--t2);font-weight:400">KDA</span></div><div style="font-size:.65rem;color:var(--t2)">'+sm.avgCs+' CS/min</div></div>';
  } else {
    h+='<div style="font-size:.7rem;color:var(--t2);font-style:italic;align-self:center">No games loaded yet</div>';
  }
  h+='</div>';
  // Use the region from the first loaded masters role, fallback to current
  var mRegs={};Object.keys(mastersData).forEach(function(r){var rg=(mastersData[r]||{}).region;if(rg)mRegs[rg]=true;});
  var regKeys=Object.keys(mRegs);
  var regCode=regKeys.length===1?regKeys[0]:getReg();
  var regLabel=(REGIONS.filter(function(r){return r[0]===regCode;})[0]||['','NA'])[1];
  var mixed=regKeys.length>1?' (mixed)':'';
  h+='<div style="display:flex;flex-direction:column;gap:4px;flex:1"><div style="font-size:.62rem;color:var(--t2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px">vs Masters+ '+regLabel+mixed+'</div>';

  // Group by role — separate score + tags per lane
  var ROLES=['TOP','JUNGLE','MIDDLE','BOTTOM','UTILITY'];
  var roleFiles={TOP:'position-top',JUNGLE:'position-jungle',MIDDLE:'position-middle',BOTTOM:'position-bottom',UTILITY:'position-utility'};
  var roleStats={};
  ROLES.forEach(function(r){roleStats[r]={deaths:[],cs:[],dmg:[],vis:[],xp:[],count:0};});
  games.forEach(function(g,gi){
    var utl=getUserTL(gi);
    if(!utl||!g.role||!roleStats[g.role])return;
    var ref=mastersData[g.role];
    if(!ref||!ref.timeline)return;
    var mtl=ref.timeline;
    var rs=roleStats[g.role];rs.count++;
    var dr2=deathRatio(utl.deathCurve,mtl.deathCurve);
    if(dr2!==null)rs.deaths.push(dr2);
    var pairs={cs:{c:utl.cs,m:mtl.cs},dmg:{c:utl.dmg,m:mtl.dmg},vis:{c:utl.vis,m:mtl.vis},xp:{c:utl.xp,m:mtl.xp}};
    Object.keys(pairs).forEach(function(k){
      var r=curveRatio(pairs[k].c,pairs[k].m);
      if(r!==null)rs[k].push(r);
    });
  });
  var rolesWithData=ROLES.filter(function(r){return roleStats[r].count>0;});
  rolesWithData.forEach(function(role){
    var rs=roleStats[role];
    var ptsTotal=0,ptsCount=0;
    var stats={};
    ['deaths','cs','dmg','vis','xp'].forEach(function(k){
      var arr=rs[k];if(!arr.length)return;
      var avgPct=arr.reduce(function(a,b){return a+b;},0)/arr.length*100;
      var isDeaths=k==='deaths';
      var pts=isDeaths?20*Math.max(0,2-avgPct/100):20*(avgPct/100);
      ptsTotal+=pts;ptsCount++;
      stats[k]={avg:Math.round(avgPct),pts:pts};
    });
    var roleScore=ptsCount>0?Math.round(ptsTotal):null;
    var tier=roleScore!==null?scoreTier(roleScore):'avg';
    var cc=tier==='god'?['#c89b3c','rgba(200,155,60,.15)']:tier==='bad'?['#ef4444','rgba(239,68,68,.15)']:['#7a8a9e','rgba(122,138,158,.12)'];
    var laneIcon='<img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-champ-select/global/default/svg/'+roleFiles[role]+'.svg" style="width:20px;height:20px;filter:brightness(0) invert(1) opacity(.7)" onerror="this.style.display=\'none\'">';
    var tagsHtml='';
    ['deaths','cs','dmg','vis','xp'].forEach(function(k){
      var sv=stats[k];if(!sv)return;
      var isDeaths=k==='deaths';
      var ttier=isDeaths?(sv.avg>100?'bad':sv.avg<80?'god':'avg'):(sv.avg<80?'bad':sv.avg>=100?'god':'avg');
      var label=ttier==='bad'?BAD_TAGS[k]:ttier==='god'?GOOD_TAGS[k]:TAG_LABELS[k];
      tagsHtml+='<span class="stag '+ttier+'" style="font-size:.6rem;padding:2px 5px" title="'+TAG_LABELS[k]+' at '+sv.avg+'% of Masters+ ('+rs.count+' games)">'+label+' '+sv.avg+'%</span>';
    });
    h+='<div style="display:flex;align-items:center;gap:6px" title="'+role+' ('+rs.count+' games)">';
    h+='<span>'+laneIcon+'</span>';
    h+='<span style="display:flex;align-items:center;justify-content:center;min-width:38px;padding:3px 6px;border-radius:6px;color:'+cc[0]+';background:'+cc[1]+';font-weight:800;font-size:.95rem;font-family:Inter,-apple-system,sans-serif;letter-spacing:-.5px">'+(roleScore!==null?roleScore:'-')+'</span>';
    h+='<div style="display:flex;gap:3px;flex-wrap:wrap">'+tagsHtml+'</div>';
    h+='</div>';
  });
  h+='</div></div></div>';
  h+=renderLiveGame();
  h+='</div>';
  h+='<div style="display:flex;align-items:center;gap:10px;margin:6px 0 14px"><div style="height:1px;background:var(--bd);flex:1"></div><span style="font-size:.65rem;color:var(--t2);text-transform:uppercase;letter-spacing:1.5px;font-weight:600">Recent Games</span><div style="height:1px;background:var(--bd);flex:1"></div></div>';
  if(!games.length){
    h+='<div style="color:var(--t2);font-size:.85rem;text-align:center;padding:24px;border:1px dashed var(--bd);border-radius:8px;margin-bottom:10px">No games loaded. Set the games count above and click Update.</div>';
  }
  h+='<div class="gl">';

  games.forEach(function(g,i){
    var wc=g.win?'w':'';
    var ex=exp===i?' exp':'';
    var kc=g.kda>=3?'g':g.kda>=2?'a':'b';
    var multi=g.multikill||'';
    var roleFile={TOP:'position-top',JUNGLE:'position-jungle',MIDDLE:'position-middle',BOTTOM:'position-bottom',UTILITY:'position-utility'}[g.role]||'';
    var roleIcon=roleFile?'<img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-champ-select/global/default/svg/'+roleFile+'.svg" style="width:14px;height:14px;vertical-align:middle;margin-right:3px;filter:brightness(0) invert(1) opacity(.7)" onerror="this.style.display=\'none\'">':'';
    // Compute summary tier for border color
    var sumTierEarly='';
    var utlE=getUserTL(i);
    if(utlE){
      var tagsE=statTag(utlE,g.role);
      var scE=computeScore(tagsE);
      if(scE!==null)sumTierEarly=' '+scoreTier(scE);
    }
    var borderStyle='';
    if(exp===i){
      borderStyle='border-color:'+(g.win?'#22c55e':'#ef4444');
    }

    h+='<div class="gc '+wc+ex+sumTierEarly+'" data-i="'+i+'" style="'+borderStyle+'"><div class="ws"></div><div>';
    h+='<div class="gr" onclick="tog('+i+')">';
    h+='<div class="gm"><div class="q">'+roleIcon+g.queue+'</div><div class="ti">'+(g.timeAgo||g.time)+'</div><div class="re '+(g.win?'w':'l')+'">'+(g.win?'Victory':'Defeat')+'</div><div class="du">'+fmtD(g.duration)+'</div></div>';
    h+='<div class="gc-c"><div class="ci"><img src="'+dd('champion',g.champKey||g.champion)+'" onerror="this.style.display=\'none\'"></div><div>';
    h+='<div style="font-size:.85rem;font-weight:600;color:var(--ac2)">'+g.champion+'</div>';
    h+='<div class="gc-i">';
    (g.items||[]).forEach(function(it){h+='<div class="isq" title="'+(it.name||it.id)+'"><img src="'+dd('item',it.id)+'" onerror="this.style.display=\'none\'"></div>';});
    h+='</div></div></div>';
    var myUTL=getUserTL(i);
    var myTags=myUTL?statTag(myUTL,g.role):[];
    var myScore=computeScore(myTags);
    var bigBadge=myScore!==null?scoreBadge(myScore,54):'';
    h+='<div class="gc-k" style="display:flex;align-items:center;gap:10px">'+bigBadge+'<div><div class="kn">'+g.kills+' / '+g.deaths+' / '+g.assists+'</div><div class="kr '+kc+'">'+g.kda+' KDA</div>'+(multi?'<div class="mk">'+multi+'</div>':'')+'</div></div>';
    h+='<div class="gc-s"><div>CS <span>'+g.cs+'</span> ('+g.csMin+'/m)</div><div>KP <span>'+(g.killParticipation!=null?g.killParticipation+'%':'\u2014')+'</span></div><div>Vis <span>'+g.vision+'</span></div></div>';
    h+='</div>';

    // Detail
    if(!g.error){
      var rn={TOP:'Top',JUNGLE:'Jungle',MIDDLE:'Mid',BOTTOM:'ADC',UTILITY:'Support',DEFAULT:'All'}[g.role]||g.role;
      var v=views[i]||'game';
      var an=comp[i]||[];
      var ld=compLD[i];
      var ce=(g.champion||'').replace(/'/g,"\\'");

      h+='<div class="gd"><div class="tabs">';
      h+='<button class="tb'+(v==='game'?' on':'')+'" onclick="sv('+i+',\'game\')">Game</button>';
      h+='<button class="tb'+(v==='masters'?' on':'')+'" onclick="sv('+i+',\'masters\')">Masters+</button>';
      h+='<button class="tb'+(v==='comp'?' on':'')+'" onclick="sv('+i+',\'comp\')">Comparable</button>';
      if(ld) h+='<span style="font-size:.72rem;color:var(--bl);margin-left:8px"><span class="sp" style="width:12px;height:12px;border-width:2px;display:inline-block;vertical-align:middle;margin-right:3px"></span>'+ld+'</span>';
      h+='</div>';

      // Timeline tabs + metric selector (shared across all views)
      var tlMet=replayTLMetric[i]||'cs';
      var tlTabs='<div class="tl-tabs"><button class="tl-t'+(tlMet==='cs'?' on':'')+'" onclick="setRepTL('+i+',\'cs\')">CS</button><button class="tl-t'+(tlMet==='gold'?' on':'')+'" onclick="setRepTL('+i+',\'gold\')">Gold</button><button class="tl-t'+(tlMet==='xp'?' on':'')+'" onclick="setRepTL('+i+',\'xp\')">XP</button><button class="tl-t'+(tlMet==='dmg'?' on':'')+'" onclick="setRepTL('+i+',\'dmg\')">Dmg</button><button class="tl-t'+(tlMet==='dmgTaken'?' on':'')+'" onclick="setRepTL('+i+',\'dmgTaken\')">Dmg Taken</button><button class="tl-t'+(tlMet==='kd'?' on':'')+'" onclick="setRepTL('+i+',\'kd\')">K/D</button><button class="tl-t'+(tlMet==='killCurve'?' on':'')+'" onclick="setRepTL('+i+',\'killCurve\')">Kills</button><button class="tl-t'+(tlMet==='deathCurve'?' on':'')+'" onclick="setRepTL('+i+',\'deathCurve\')">Deaths</button><button class="tl-t'+(tlMet==='vis'?' on':'')+'" onclick="setRepTL('+i+',\'vis\')">Wards</button></div>';

      if(v==='game'){
        h+='<div class="ds" style="position:relative"><h4>Timeline</h4>'+tlTabs;
        h+='<canvas id="tlg'+i+'" width="700" height="220" style="width:100%;height:220px;margin-top:6px"></canvas>';
        var utlG=getUserTL(i);
        h+='<div class="tl-legend" style="flex-wrap:wrap;align-items:center"><span style="color:'+(gameTLOff[i+'_me']?'#555':'var(--ac)')+';cursor:pointer" onclick="togMe('+i+')">&#9632; You</span>';
        if(utlG&&utlG.allPlayers){
          var ci=0;
          utlG.allPlayers.forEach(function(p,pi){
            if(p.isMe)return;
            var col=COLORS[ci%COLORS.length];ci++;
            var on=!gameTLOff[i+'_'+pi];
            h+='<span style="margin-left:8px;color:'+(on?col:'#555')+';cursor:pointer;font-size:.65rem" onclick="togPlayer('+i+','+pi+')">&#9632; '+p.champion+'</span>';
          });
          var anyOn2=false;utlG.allPlayers.forEach(function(p,pi){if(!p.isMe&&!gameTLOff[i+'_'+pi])anyOn2=true;});
          h+='<button style="margin-left:10px;background:var(--s2);color:var(--t2);border:1px solid var(--bd);border-radius:3px;padding:1px 6px;cursor:pointer;font-size:.6rem" onclick="togAll('+i+')">'+(anyOn2?'Hide All':'Show All')+'</button>';
        }
        h+='</div><span style="position:absolute;bottom:8px;right:12px;font-size:.65rem;color:var(--t2);font-style:italic;pointer-events:none">Alt + hover for exact values at each minute</span></div>';

      } else if(v==='masters'){
        var mRole2=tagMastersRole[i]||g.role;
        var mRn2={TOP:'Top',JUNGLE:'Jungle',MIDDLE:'Mid',BOTTOM:'ADC',UTILITY:'Support'}[mRole2]||mRole2;
        var md=mastersData[mRole2];
        h+='<div style="display:flex;align-items:center;margin-bottom:8px;gap:10px;flex-wrap:wrap">';
        // Role icon picker
        var allRoles=['TOP','JUNGLE','MIDDLE','BOTTOM','UTILITY'];
        var roleFileMap={TOP:'position-top',JUNGLE:'position-jungle',MIDDLE:'position-middle',BOTTOM:'position-bottom',UTILITY:'position-utility'};
        var roleNameMap={TOP:'Top',JUNGLE:'Jungle',MIDDLE:'Mid',BOTTOM:'ADC',UTILITY:'Support'};
        h+='<div style="display:flex;gap:3px;align-items:center">';
        allRoles.forEach(function(r){
          var active=r===mRole2;
          var hasData=!!mastersData[r];
          var gamesN=hasData?mastersData[r].games:0;
          var op=active?1:(hasData?0.5:0.2);
          var bg=active?'background:rgba(200,155,60,.18);border:1px solid rgba(200,155,60,.5);':'background:transparent;border:1px solid transparent;';
          h+='<span onclick="setMastersRole('+i+',\''+r+'\')" title="'+roleNameMap[r]+(hasData?' ('+gamesN+'g)':' (no data)')+'" style="display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:5px;cursor:pointer;'+bg+'"><img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-champ-select/global/default/svg/'+roleFileMap[r]+'.svg" style="width:18px;height:18px;filter:brightness(0) invert(1) opacity('+op+')" onerror="this.style.display=\'none\'"></span>';
        });
        h+='</div>';
        if(md){
          var age=Math.round((Date.now()/1000-(md._ts||0))/86400);
          h+='<span style="font-size:.75rem;color:var(--t2)">'+md.games+'g from Masters+ '+mRn2+' ('+age+'d ago)</span>';
          if(mRole2!==g.role)h+='<span style="font-size:.65rem;color:var(--ac);cursor:pointer" onclick="delete tagMastersRole['+i+'];render();">[Reset to '+rn+']</span>';
        } else {
          h+='<span style="font-size:.75rem;color:var(--t2)">Loading Masters+ '+mRn2+' data...</span>';
        }
        h+='</div>';
        if(md){
          h+='<div class="ds" style="position:relative"><h4>Timeline</h4>'+tlTabs;
          h+='<canvas id="tlm'+i+'" width="700" height="220" style="width:100%;height:220px;margin-top:6px"></canvas>';
          h+='<div class="tl-legend" style="flex-wrap:wrap;align-items:center"><span style="color:'+(gameTLOff[i+'_me']?'#555':'var(--ac)')+';cursor:pointer" onclick="togMe('+i+')">&#9632; You</span><span style="margin-left:8px;color:'+(gameTLOff[i+'_masters']?'#555':'var(--bl)')+';cursor:pointer" onclick="togMasters('+i+')">&#9632; Masters+</span>';
          var utlM2=getUserTL(i);
          if(utlM2&&utlM2.allPlayers){
            var ci2=0;var anyOnM=false;
            utlM2.allPlayers.forEach(function(p,pi){
              if(p.isMe)return;
              var col=COLORS[ci2%COLORS.length];ci2++;
              var on=!gameTLOff[i+'_'+pi];
              if(on)anyOnM=true;
              h+='<span style="margin-left:6px;color:'+(on?col:'#555')+';cursor:pointer;font-size:.65rem" onclick="togPlayer('+i+','+pi+')">&#9632; '+p.champion+'</span>';
            });
            h+='<button style="margin-left:10px;background:var(--s2);color:var(--t2);border:1px solid var(--bd);border-radius:3px;padding:1px 6px;cursor:pointer;font-size:.6rem" onclick="togAll('+i+')">'+(anyOnM?'Hide All':'Show All')+'</button>';
          }
          h+='</div><span style="position:absolute;bottom:8px;right:12px;font-size:.65rem;color:var(--t2);font-style:italic;pointer-events:none">Alt + hover for exact values at each minute</span></div>';
        }

      } else if(v==='comp'){
        var ist='background:var(--bg);border:1px solid var(--bd);color:var(--t);border-radius:5px;font-size:.8rem;';
        h+='<div class="ds" style="padding:10px 14px"><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">';
        h+='<input id="compId'+i+'" placeholder="RiotID#Tag" style="'+ist+'padding:5px 8px;width:180px">';
        h+='<select id="compCt'+i+'" style="'+ist+'padding:5px"><option value="5">5g</option><option value="10" selected>10g</option><option value="20">20g</option></select>';
        h+='<select id="compAge'+i+'" style="'+ist+'padding:5px"><option value="7">7d</option><option value="14">14d</option><option value="30" selected>30d</option><option value="90">90d</option></select>';
        h+='<button class="btn" onclick="addComp('+i+',\''+g.role+'\','+(g.gameId||0)+')"'+(ld?' disabled':'')+'>Add</button>';
        h+='</div>';
        if(an.length){
          h+='<div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">';
          an.forEach(function(p,pi){h+='<span style="font-size:.72rem;color:'+COLORS[pi%COLORS.length]+'">&#9632; '+p.player+'</span><span style="font-size:.65rem;color:var(--t2);cursor:pointer" onclick="rmComp('+i+','+pi+')">[x]</span>';});
          h+='</div>';
        }
        h+='</div>';

        if(an.length){
        }
        h+='<div class="ds" style="position:relative"><h4>Timeline</h4>'+tlTabs;
        h+='<canvas id="tlc'+i+'" width="700" height="220" style="width:100%;height:220px;margin-top:6px"></canvas>';
        h+='<div class="tl-legend"><span style="color:'+(gameTLOff[i+'_me']?'#555':'var(--ac)')+';cursor:pointer" onclick="togMe('+i+')">&#9632; You</span>';
        an.forEach(function(p,pi){h+='<span style="margin-left:10px;color:'+COLORS[pi%COLORS.length]+'">&#9632; '+p.player+'</span>';});
        h+='</div><span style="position:absolute;bottom:8px;right:12px;font-size:.65rem;color:var(--t2);font-style:italic;pointer-events:none">Alt + hover for exact values at each minute</span></div>';
      }

      // Scoreboard
      if(g.players&&g.players.length){
        var blue=g.players.filter(function(p){return p.team===100;});
        var red=g.players.filter(function(p){return p.team===200;});
        var sbUTL=getUserTL(i);
        var sbAllP=(sbUTL&&sbUTL.allPlayers)||[];

        // Compute rank for each player — rank by the same score the badge shows
        var ranked=g.players.map(function(p){
          var cn=p.championName||'?';
          var pTL=null;
          sbAllP.forEach(function(ap){if(champKey(ap.champion)===champKey(cn))pTL=ap.timeline;});
          var tags=pTL?statTag(pTL,p.role||''):[];
          var score=computeScore(tags);
          return{p:p,tags:tags,score:score==null?-1:score,cn:cn};
        });
        ranked.sort(function(a,b){return b.score-a.score;});
        var rankMap={};ranked.forEach(function(r,ri){rankMap[r.cn]=ri+1;});

        var blueAvg=avgLobbyRank(blue);
        var redAvg=avgLobbyRank(red);
        var teamHdr=function(label,avg,teamColor){
          if(!avg)return '<tr class="team-hdr"><td colspan="10" style="padding:6px 10px;color:var(--t2);font-size:.7rem;font-weight:600;letter-spacing:.5px"><span style="color:'+teamColor+'">'+label+'</span> <span style="color:var(--t3,#555)">(no ranked data)</span></td></tr>';
          var tier=avg.display.split(' ')[0].toLowerCase();
          return '<tr class="team-hdr"><td colspan="10" style="padding:6px 10px;font-size:.72rem"><span style="color:'+teamColor+';font-weight:700;letter-spacing:.5px">'+label+'</span> <img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-mini-crests/'+tier+'.svg" style="width:14px;height:14px;vertical-align:middle;margin:0 3px 0 8px" onerror="this.style.display=\'none\'"><span style="color:var(--ac2);font-weight:700">'+avg.display+'</span></td></tr>';
        };
        h+='<div class="ds"><h4>Scoreboard</h4><table class="sb"><tr><th style="text-align:center">Rank</th><th>Score</th><th></th><th>Champ</th><th>KDA</th><th>CS</th><th>Dmg</th><th>Gold</th><th>Vis</th><th style="min-width:200px">Tags</th></tr>';
        var sbRow=function(team,cls){var o='';team.forEach(function(p){
          var cn=p.championName||'?',ck=p.championKey||cn,me=p.isMe?' me':'';
          var rank=rankMap[cn]||'';
          var rk=ranked.filter(function(r){return r.cn===cn;})[0];
          var tags=rk?rk.tags:[];
          var pScore=computeScore(tags);
          var tagHtml='';
          tags.forEach(function(tg){tagHtml+='<span class="stag '+(tg.tier||'avg')+'" title="'+(tg.desc||'')+'" style="cursor:pointer" onclick="goTag('+i+',\''+(tg.met||'cs')+'\',\''+champKey(cn)+'\',\''+(p.role||'')+ '\')">'+tg.t+'</span>';});
          var scoreTier2=pScore!==null?scoreTier(pScore):'avg';
          var col=scoreTier2==='god'?'#c89b3c':scoreTier2==='bad'?'#ef4444':'#7a8a9e';
          var bg=scoreTier2==='god'?'rgba(200,155,60,.15)':scoreTier2==='bad'?'rgba(239,68,68,.15)':'rgba(122,138,158,.12)';
          var scoreBox=pScore!==null?'<span style="display:inline-flex;align-items:center;justify-content:center;min-width:32px;padding:3px 6px;border-radius:5px;background:'+bg+';color:'+col+';font-weight:700;font-size:.72rem">'+pScore+'</span>':'';
          var playerRank=p.puuid?playerRanks[p.puuid]:null;
          var rankTxt=fmtRank(playerRank);
          var rankColor=playerRank&&playerRank.tier?'var(--t2)':'var(--t3,#555)';
          var rankIconHtml='';
          if(playerRank&&playerRank.tier){
            var tierLow=playerRank.tier.toLowerCase();
            rankIconHtml='<img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-mini-crests/'+tierLow+'.svg" style="width:14px;height:14px;vertical-align:middle;margin-right:3px" onerror="this.style.display=\'none\'">';
          }
          var nameCell='<div style="line-height:1.15">'+p.name+'</div><div style="font-size:.62rem;color:'+rankColor+';margin-top:1px;display:flex;align-items:center">'+rankIconHtml+'<span>'+rankTxt+'</span></div>';
          o+='<tr class="'+me+'"><td style="text-align:center;color:'+col+';font-weight:700">'+rank+'</td><td style="padding-right:0">'+scoreBox+'</td><td class="'+cls+'" style="padding-left:6px">'+nameCell+'</td><td><img src="'+dd('champion',ck)+'" style="width:18px;height:18px;border-radius:3px;vertical-align:middle;margin-right:3px" onerror="this.style.display=\'none\'">'+cn+'</td><td>'+p.kills+'/'+p.deaths+'/'+p.assists+'</td><td>'+p.cs+'</td><td>'+fmt(p.damage)+'</td><td>'+fmt(p.gold)+'</td><td>'+p.vision+'</td><td>'+tagHtml+'</td></tr>';
        });return o;};
        h+=teamHdr('BLUE TEAM',blueAvg,'#3b82f6');
        h+=sbRow(blue,'tb2');
        h+='<tr class="ts"><td colspan="10"></td></tr>';
        h+=teamHdr('RED TEAM',redAvg,'#ef4444');
        h+=sbRow(red,'tr2');
        h+='</table></div>';
      }
      h+='</div>';
    }
    h+='</div></div>';
  });
  h+='</div></div>';
  $('#app').innerHTML=h;
  pr();
}

function tog(i){exp=exp===i?null:i;render();}
function sv(i,v){views[i]=v;render();}
function rmComp(i,pi){if(comp[i])comp[i].splice(pi,1);render();}

async function fetchLiveGame(){
  if(!D||!D.summoner||!D.summoner.puuid){popup('Click Update first to load summoner data');return;}
  liveGameLoading=true;render();
  try{
    var r=await fetch('/api/live-game',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({puuid:D.summoner.puuid,region:getReg()})});
    var d=await r.json();
    liveGameLoading=false;
    if(d.error){liveGame=null;render();popup(d.error);return;}
    if(!d.inGame){liveGame={inGame:false,fetchedAt:Date.now()};render();return;}
    liveGame=Object.assign({inGame:true,fetchedAt:Date.now()},d.data);
    render();
    // Fetch ranks for all participants
    var puuids=liveGame.participants.map(function(p){return p.puuid;}).filter(Boolean);
    if(puuids.length){
      try{
        var rr=await fetch('/api/player-ranks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({puuids:puuids,region:getReg()})});
        var dd2=await rr.json();
        if(dd2.ok&&dd2.ranks){Object.keys(dd2.ranks).forEach(function(pu){playerRanks[pu]=dd2.ranks[pu];});render();}
      }catch(e){}
    }
  }catch(e){liveGameLoading=false;render();popup(e.message);}
}
function closeLiveGame(){liveGame=null;render();}

async function pullMasters(role,gid,cid){
  mastersLD[role]='Pulling...';render();
  try{
    var r=await fetch('/api/masters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:role,force:!!mastersData[role]})});
    var d=await r.json();delete mastersLD[role];
    if(d.error){render();popup(d.error);return;}
    mastersData[role]=d.data;render();
  }catch(e){delete mastersLD[role];render();popup(e.message);}
}

async function addComp(idx,role,gid){
  var rid=document.getElementById('compId'+idx).value.trim();
  if(!rid){popup('Enter a Riot ID');return;}
  var count=parseInt(document.getElementById('compCt'+idx).value)||10;
  var maxAge=parseInt(document.getElementById('compAge'+idx).value)||30;
  compLD[idx]='Adding '+rid+'...';render();
  try{
    var r=await fetch('/api/comparable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({riotId:rid,role:role,count:count,maxAgeDays:maxAge,gameId:gid,region:getReg()})});
    var d=await r.json();delete compLD[idx];
    if(d.error){render();popup(d.error);return;}
    if(!comp[idx])comp[idx]=[];
    comp[idx].push(d.analysis);views[idx]='comp';render();
  }catch(e){delete compLD[idx];render();popup(e.message);}
}

function drawChart(canvas,lines,ml){
  if(!canvas)return;
  canvas._chart={lines:lines,ml:ml};
  var ctx=canvas.getContext('2d');
  var dpr=window.devicePixelRatio||1;
  canvas.width=canvas.offsetWidth*dpr;canvas.height=220*dpr;ctx.scale(dpr,dpr);
  var W=canvas.offsetWidth,H=220;
  var all=[];lines.forEach(function(l){all=all.concat(l.data);});
  var mx=Math.max.apply(null,all.concat([1]));
  var pad={t:10,b:22,l:45,r:10},gw=W-pad.l-pad.r,gh=H-pad.t-pad.b;
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle='rgba(42,53,69,.5)';ctx.lineWidth=1;
  // Nice rounded gridline values
  var niceSteps=[1,2,5,10,20,50,100,200,500,1000,2000,5000,10000,20000,50000,100000];
  var step=1;for(var si=0;si<niceSteps.length;si++){if(mx/niceSteps[si]<=5){step=niceSteps[si];break;}}
  var maxGrid=Math.ceil(mx/step)*step;
  mx=maxGrid;
  for(var gv=0;gv<=maxGrid;gv+=step){
    var y=pad.t+gh*(1-gv/mx);
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();
    ctx.fillStyle='#7a8a9e';ctx.font='10px sans-serif';ctx.textAlign='right';
    ctx.fillText(gv,pad.l-4,y+3);
  }
  ctx.textAlign='center';for(var m=0;m<ml;m+=5){var x=pad.l+gw*(m/(ml-1));ctx.fillStyle='#7a8a9e';ctx.fillText(m+'m',x,H-4);}
  lines.forEach(function(l){
    if(l.data.length<2)return;
    ctx.strokeStyle=l.color;ctx.lineWidth=l.w;
    ctx.beginPath();
    l.data.forEach(function(v,di){
      var x=pad.l+gw*(di/(ml-1)),y=pad.t+gh*(1-v/mx);
      if(di===0)ctx.moveTo(x,y);
      else ctx.lineTo(x,y);
    });
    ctx.stroke();
  });
  // Alt+hover crosshair
  if(canvas._hoverX!=null){
    var hx=canvas._hoverX;
    var di=Math.round((hx-pad.l)/gw*(ml-1));
    di=Math.max(0,Math.min(ml-1,di));
    var cx=pad.l+gw*(di/(ml-1));
    ctx.strokeStyle='rgba(200,155,60,.55)';ctx.lineWidth=1;
    ctx.setLineDash([3,3]);
    ctx.beginPath();ctx.moveTo(cx,pad.t);ctx.lineTo(cx,H-pad.b);ctx.stroke();
    ctx.setLineDash([]);
    var rows=[];
    lines.forEach(function(l){
      if(di>=l.data.length)return;
      var v=l.data[di];
      var ly=pad.t+gh*(1-v/mx);
      ctx.fillStyle=l.color;
      ctx.beginPath();ctx.arc(cx,ly,3.5,0,Math.PI*2);ctx.fill();
      ctx.strokeStyle='#0f141e';ctx.lineWidth=1.5;ctx.stroke();
      rows.push({color:l.color,name:l.name||'',value:v});
    });
    // Tooltip box
    ctx.font='11px sans-serif';ctx.textAlign='left';
    var tipW=0;
    rows.forEach(function(r){
      var val=fmtVal(r.value);
      var txt=(r.name?r.name+': ':'')+val;
      var w=ctx.measureText(txt).width;
      if(w>tipW)tipW=w;
    });
    var headerTxt=di+'m';
    var headerW=ctx.measureText(headerTxt).width;
    if(headerW>tipW)tipW=headerW;
    tipW+=20;
    var rowH=14;
    var tipH=(rows.length+1)*rowH+8;
    var tipX=cx+10,tipY=pad.t+4;
    if(tipX+tipW>W-4)tipX=cx-tipW-10;
    if(tipX<pad.l)tipX=pad.l+2;
    ctx.fillStyle='rgba(15,20,30,.94)';ctx.strokeStyle='rgba(122,138,158,.5)';ctx.lineWidth=1;
    ctx.fillRect(tipX,tipY,tipW,tipH);ctx.strokeRect(tipX,tipY,tipW,tipH);
    ctx.fillStyle='#c89b3c';ctx.fillText(headerTxt,tipX+6,tipY+13);
    rows.forEach(function(r,ri){
      ctx.fillStyle=r.color;
      ctx.fillRect(tipX+6,tipY+rowH*(ri+1)+4,8,8);
      ctx.fillStyle='#dbe1ea';
      var val=fmtVal(r.value);
      var txt=(r.name?r.name+': ':'')+val;
      ctx.fillText(txt,tipX+18,tipY+rowH*(ri+1)+12);
    });
  }
  // Bind listeners once per canvas
  if(!canvas._hoverBound){
    canvas._hoverBound=true;
    canvas.addEventListener('mousemove',function(e){
      canvas.style.cursor=e.altKey?'crosshair':'';
      if(!e.altKey){
        if(canvas._hoverX!=null){canvas._hoverX=null;drawChart(canvas,canvas._chart.lines,canvas._chart.ml);}
        return;
      }
      var rect=canvas.getBoundingClientRect();
      canvas._hoverX=e.clientX-rect.left;
      drawChart(canvas,canvas._chart.lines,canvas._chart.ml);
    });
    canvas.addEventListener('mouseleave',function(){
      canvas.style.cursor='';
      if(canvas._hoverX!=null){canvas._hoverX=null;drawChart(canvas,canvas._chart.lines,canvas._chart.ml);}
    });
  }
}
function fmtVal(v){
  if(v==null)return '\u2014';
  if(Math.abs(v-Math.round(v))<1e-9)return String(Math.round(v));
  if(Math.abs(v)>=100)return v.toFixed(0);
  if(Math.abs(v)>=10)return v.toFixed(1);
  return v.toFixed(2);
}

function drawGameTL(idx){
  var canvas=document.getElementById('tlg'+idx);if(!canvas)return;
  var utl=getUserTL(idx);if(!utl)return;
  var met=replayTLMetric[idx]||'cs';
  var lines=[];
  var ml=2;
  // "You" line (always on)
  var myData=getTLData(utl,met);
  if(myData.length>1&&!gameTLOff[idx+'_me'])lines.push({data:myData,color:'#c89b3c',w:3,name:'You'});
  ml=Math.max(ml,myData.length);
  // All other players
  if(utl.allPlayers){
    var ci=0;
    utl.allPlayers.forEach(function(p,pi){
      if(p.isMe){return;}
      var col=COLORS[ci%COLORS.length];ci++;
      if(gameTLOff[idx+'_'+pi])return;
      var d=getTLData(p.timeline||{},met);if(d.length<2)return;
      lines.push({data:d,color:col,w:1.5,name:p.champion||('P'+pi)});
      ml=Math.max(ml,d.length);
    });
  }
  drawChart(canvas,lines,ml);
}

function drawMastersTL(idx){
  var g=D.games[idx];if(!g)return;
  var mRole=tagMastersRole[idx]||g.role;
  var md=mastersData[mRole];if(!md)return;
  var canvas=document.getElementById('tlm'+idx);if(!canvas)return;
  var met=replayTLMetric[idx]||'cs';
  var utl=userTL[idx]||null;
  var mData=getTLData(md.timeline||{},met);
  var lines=[];
  var ml=2;
  var uData=utl?getTLData(utl,met):[];
  if(uData.length&&!gameTLOff[idx+'_me'])lines.push({data:uData,color:'#c89b3c',w:3,name:'You'});
  ml=Math.max(ml,uData.length);
  if(mData.length&&!gameTLOff[idx+'_masters'])lines.push({data:mData,color:'#3b82f6',w:2,name:'Masters+'});
  ml=Math.max(ml,mData.length);
  if(utl&&utl.allPlayers){
    var ci=0;
    utl.allPlayers.forEach(function(p,pi){
      if(p.isMe)return;
      var col=COLORS[ci%COLORS.length];ci++;
      if(gameTLOff[idx+'_'+pi])return;
      var d=getTLData(p.timeline||{},met);if(d.length<2)return;
      lines.push({data:d,color:col,w:1.5,name:p.champion||('P'+pi)});
      ml=Math.max(ml,d.length);
    });
  }
  drawChart(canvas,lines,ml);
}

function drawCompTL(idx){
  var canvas=document.getElementById('tlc'+idx);if(!canvas)return;
  var met=replayTLMetric[idx]||'cs';
  var lines=[];
  var utl=userTL[idx]||null;
  var uData=utl?getTLData(utl,met):[];
  if(uData.length)lines.push({data:uData,color:'#c89b3c',w:3,name:'You'});
  var players=comp[idx]||[];
  players.forEach(function(p,pi){var d=getTLData(p.timeline||{},met);if(d.length)lines.push({data:d,color:COLORS[pi%COLORS.length],w:2,name:p.champion||p.summoner||('P'+pi)});});
  var ml=2;lines.forEach(function(l){ml=Math.max(ml,l.data.length);});
  if(lines.length)drawChart(canvas,lines,ml);
}

var playerRanks={};
var ranksFetching={};

var TIER_VAL={IRON:0,BRONZE:1,SILVER:2,GOLD:3,PLATINUM:4,EMERALD:5,DIAMOND:6,MASTER:7,GRANDMASTER:8,CHALLENGER:9};
var VAL_TIER=['Iron','Bronze','Silver','Gold','Platinum','Emerald','Diamond','Master','Grandmaster','Challenger'];
var DIV_VAL={IV:0,III:0.25,II:0.5,I:0.75};

function rankToNum(r){
  if(!r||!r.tier)return null;
  var tv=TIER_VAL[r.tier];
  if(tv==null)return null;
  var dv=r.tier==='MASTER'||r.tier==='GRANDMASTER'||r.tier==='CHALLENGER'?0:(DIV_VAL[r.division]||0);
  return tv+dv+(r.lp||0)/400;
}
function numToRank(n){
  if(n==null)return '';
  var tv=Math.max(0,Math.min(9,Math.floor(n)));
  var frac=n-tv;
  var tier=VAL_TIER[tv];
  if(tv>=7){
    var apexLp=Math.round(frac*400);
    return tier+' '+apexLp+' LP';
  }
  var divIdx=Math.min(3,Math.floor(frac*4));
  var divs=['IV','III','II','I'];
  var lp=Math.round((frac-divIdx*0.25)*400);
  return tier+' '+divs[divIdx]+' '+lp+' LP';
}
function avgLobbyRank(players){
  if(!players)return null;
  var vals=[];
  players.forEach(function(p){var r=playerRanks[p.puuid];var v=rankToNum(r);if(v!=null)vals.push(v);});
  if(vals.length<3)return null;
  var avg=vals.reduce(function(a,b){return a+b;},0)/vals.length;
  return{num:avg,display:numToRank(avg),count:vals.length};
}

function pr(){
  if(exp==null||!D||!D.games[exp])return;
  var mg=D.games[exp];

  // Auto-fetch user timeline if missing
  if(!userTL[exp]&&mg.gameId){
    fetch('/api/user-timeline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gameId:mg.gameId,championId:mg.championId,region:getReg()})})
      .then(function(r){return r.json();})
      .then(function(d){if(d.ok&&d.timeline){userTL[exp]=d.timeline;render();}});
  }

  // Fetch ranks for all players if missing
  if(mg.players&&mg.gameId&&!ranksFetching[mg.gameId]){
    var missing=mg.players.filter(function(p){return p.puuid&&!(p.puuid in playerRanks);}).map(function(p){return p.puuid;});
    if(missing.length){
      ranksFetching[mg.gameId]=true;
      fetch('/api/player-ranks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({puuids:missing,region:getReg()})})
        .then(function(r){return r.json();})
        .then(function(d){
          if(d.ok&&d.ranks){Object.keys(d.ranks).forEach(function(pu){playerRanks[pu]=d.ranks[pu];});render();}
          delete ranksFetching[mg.gameId];
        }).catch(function(){delete ranksFetching[mg.gameId];});
    }
  }

  // Draw the right chart for the active tab
  var v=views[exp]||'game';
  setTimeout(function(){
    if(v==='game') drawGameTL(exp);
    else if(v==='masters'&&mastersData[mg.role]) drawMastersTL(exp);
    else if(v==='comp') drawCompTL(exp);
  },50);
}

function getUserTL(idx){
  return userTL[idx]||(comp[idx]&&comp[idx].length?comp[idx][0].userTimeline:null);
}

var replayTLMetric={};
var gameTLOff={};

function checkStale(){
  var now=Date.now()/1000;var staleRoles=[];
  ['TOP','JUNGLE','MIDDLE','BOTTOM','UTILITY'].forEach(function(r){
    var md=mastersData[r];
    if(md&&now-(md._ts||0)>2592000)staleRoles.push(r);
  });
  var el=document.getElementById('staleWarn');
  if(staleRoles.length&&el){
    el.style.display='flex';
    el.textContent='Masters+ data out of date for: '+staleRoles.join(', ')+' — refreshing...';
  } else if(el){el.style.display='none';}
}

function setRepTL(idx,met){replayTLMetric[idx]=met;render();}

function togPlayer(idx,pi){var k=idx+'_'+pi;gameTLOff[k]=!gameTLOff[k];render();}
function togMe(idx){var k=idx+'_me';gameTLOff[k]=!gameTLOff[k];render();}
function togMasters(idx){var k=idx+'_masters';gameTLOff[k]=!gameTLOff[k];render();}
function togAll(idx){
  var utl=getUserTL(idx);if(!utl||!utl.allPlayers)return;
  var anyOn=false;
  utl.allPlayers.forEach(function(p,pi){if(!p.isMe&&!gameTLOff[idx+'_'+pi])anyOn=true;});
  utl.allPlayers.forEach(function(p,pi){if(!p.isMe)gameTLOff[idx+'_'+pi]=anyOn;});
  render();
}
var tagMastersRole={};
function setMastersRole(idx,role){
  tagMastersRole[idx]=role;
  render();
}
// Champion → primary role hint. Used to organize live-game picks by lane.
// Entries are championName as returned by spectator (DDragon "id" field).
var CHAMP_ROLE={
  // TOP
  Aatrox:'TOP',Camille:'TOP',Cho_Gath:'TOP',Darius:'TOP',Dr_Mundo:'TOP',Fiora:'TOP',Garen:'TOP',Gangplank:'TOP',Gnar:'TOP',Gwen:'TOP',Illaoi:'TOP',Irelia:'TOP',Jax:'TOP',Jayce:'TOP',Kayle:'TOP',Kennen:'TOP',Kled:'TOP',Malphite:'TOP',Mordekaiser:'TOP',Nasus:'TOP',Ornn:'TOP',Quinn:'TOP',Renekton:'TOP',Riven:'TOP',Rumble:'TOP',Sett:'TOP',Shen:'TOP',Singed:'TOP',Sion:'TOP',Tahm_Kench:'TOP',Teemo:'TOP',Tryndamere:'TOP',Urgot:'TOP',Vladimir:'TOP',Volibear:'TOP',Yorick:'TOP',KSante:'TOP','K\'Sante':'TOP',Ambessa:'TOP',Smolder:'TOP',
  // JUNGLE
  Amumu:'JUNGLE',Bel_Veth:'JUNGLE',Briar:'JUNGLE',Diana:'JUNGLE',Ekko:'JUNGLE',Elise:'JUNGLE',Evelynn:'JUNGLE',Fiddlesticks:'JUNGLE',Graves:'JUNGLE',Hecarim:'JUNGLE',Ivern:'JUNGLE',Jarvan_IV:'JUNGLE',Karthus:'JUNGLE',Kayn:'JUNGLE',KhaZix:'JUNGLE','Kha\'Zix':'JUNGLE',Kindred:'JUNGLE',Lee_Sin:'JUNGLE',Lillia:'JUNGLE',Master_Yi:'JUNGLE',Nidalee:'JUNGLE',Nocturne:'JUNGLE',Nunu_Willump:'JUNGLE',Olaf:'JUNGLE',Poppy:'JUNGLE',Rammus:'JUNGLE',RekSai:'JUNGLE','Rek\'Sai':'JUNGLE',Rengar:'JUNGLE',Sejuani:'JUNGLE',Shaco:'JUNGLE',Skarner:'JUNGLE',Trundle:'JUNGLE',Udyr:'JUNGLE',Vi:'JUNGLE',Viego:'JUNGLE',Warwick:'JUNGLE',Wukong:'JUNGLE',Xin_Zhao:'JUNGLE',Zac:'JUNGLE',Lillia:'JUNGLE',
  // MIDDLE
  Ahri:'MIDDLE',Akali:'MIDDLE',Akshan:'MIDDLE',Anivia:'MIDDLE',Annie:'MIDDLE',Aurora:'MIDDLE',Aurelion_Sol:'MIDDLE',Azir:'MIDDLE',Cassiopeia:'MIDDLE',Corki:'MIDDLE',Fizz:'MIDDLE',Galio:'MIDDLE',Hwei:'MIDDLE',Kassadin:'MIDDLE',Katarina:'MIDDLE',LeBlanc:'MIDDLE',Lissandra:'MIDDLE',Lux:'MIDDLE',Malzahar:'MIDDLE',Naafiri:'MIDDLE',Neeko:'MIDDLE',Orianna:'MIDDLE',Qiyana:'MIDDLE',Ryze:'MIDDLE',Sylas:'MIDDLE',Syndra:'MIDDLE',Talon:'MIDDLE',Taliyah:'MIDDLE',Twisted_Fate:'MIDDLE',Veigar:'MIDDLE',Vex:'MIDDLE',Viktor:'MIDDLE',Xerath:'MIDDLE',Yasuo:'MIDDLE',Yone:'MIDDLE',Zed:'MIDDLE',Ziggs:'MIDDLE',Zoe:'MIDDLE',
  // BOTTOM (ADC)
  Aphelios:'BOTTOM',Ashe:'BOTTOM',Caitlyn:'BOTTOM',Draven:'BOTTOM',Ezreal:'BOTTOM',Jhin:'BOTTOM',Jinx:'BOTTOM',Kalista:'BOTTOM',Kai_Sa:'BOTTOM',KogMaw:'BOTTOM','Kog\'Maw':'BOTTOM',Lucian:'BOTTOM',Miss_Fortune:'BOTTOM',Nilah:'BOTTOM',Samira:'BOTTOM',Sivir:'BOTTOM',Tristana:'BOTTOM',Twitch:'BOTTOM',Varus:'BOTTOM',Vayne:'BOTTOM',Xayah:'BOTTOM',Zeri:'BOTTOM',
  // UTILITY (Support)
  Alistar:'UTILITY',Bard:'UTILITY',Blitzcrank:'UTILITY',Braum:'UTILITY',Janna:'UTILITY',Karma:'UTILITY',Leona:'UTILITY',Lulu:'UTILITY',Maokai:'UTILITY',Milio:'UTILITY',Morgana:'UTILITY',Nami:'UTILITY',Nautilus:'UTILITY',Pyke:'UTILITY',Rakan:'UTILITY',Rell:'UTILITY',Renata_Glasc:'UTILITY',Senna:'UTILITY',Seraphine:'UTILITY',Sona:'UTILITY',Soraka:'UTILITY',Swain:'UTILITY',Taric:'UTILITY',Thresh:'UTILITY',Yuumi:'UTILITY',Zilean:'UTILITY',Zyra:'UTILITY',
};
// Spectator returns variants like "MissFortune", "JarvanIV", "KSante" — normalize for lookup
function _champRoleLookup(name){
  if(!name)return null;
  if(CHAMP_ROLE[name])return CHAMP_ROLE[name];
  // Try normalized: insert underscore between camelCase / strip apostrophes / spaces
  var k1=name.replace(/([a-z])([A-Z])/g,'$1_$2');
  if(CHAMP_ROLE[k1])return CHAMP_ROLE[k1];
  var k2=name.replace(/[^A-Za-z]/g,'');
  for(var k in CHAMP_ROLE){if(k.replace(/[^A-Za-z]/g,'')===k2)return CHAMP_ROLE[k];}
  return null;
}
// Assigns roles to a 5-player team. Smite locks JUNGLE; remaining slots filled by champ-role
// hints with simple conflict resolution.
function inferTeamRoles(team){
  var ROLES=['TOP','JUNGLE','MIDDLE','BOTTOM','UTILITY'];
  var assigned={};   // role -> participant index
  var roleOf={};     // pid -> role
  // Pass 1: Smite (spell ID 11) → JUNGLE. Lock the first one we see.
  team.forEach(function(p,i){
    if(roleOf[i])return;
    if((p.spell1Id===11||p.spell2Id===11)&&!assigned.JUNGLE){assigned.JUNGLE=i;roleOf[i]='JUNGLE';}
  });
  // Pass 2: champ-role hints, skip already-assigned roles
  team.forEach(function(p,i){
    if(roleOf[i])return;
    var hint=_champRoleLookup(p.championName);
    if(hint&&!assigned[hint]){assigned[hint]=i;roleOf[i]=hint;}
  });
  // Pass 3: fill any remaining roles in order
  team.forEach(function(p,i){
    if(roleOf[i])return;
    for(var r=0;r<ROLES.length;r++){
      if(!assigned[ROLES[r]]){assigned[ROLES[r]]=i;roleOf[i]=ROLES[r];break;}
    }
  });
  return roleOf;
}
function sortTeamByRole(team){
  var ROLES=['TOP','JUNGLE','MIDDLE','BOTTOM','UTILITY'];
  var roles=inferTeamRoles(team);
  var withRole=team.map(function(p,i){return {p:p,role:roles[i]||'UNKNOWN'};});
  withRole.sort(function(a,b){return ROLES.indexOf(a.role)-ROLES.indexOf(b.role);});
  return withRole;
}
function roleIconHTML(role,size){
  var rfm={TOP:'position-top',JUNGLE:'position-jungle',MIDDLE:'position-middle',BOTTOM:'position-bottom',UTILITY:'position-utility'};
  if(!rfm[role])return '';
  var s=size||14;
  return '<img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-champ-select/global/default/svg/'+rfm[role]+'.svg" style="width:'+s+'px;height:'+s+'px;filter:brightness(0) invert(1) opacity(.75);flex-shrink:0" title="'+role+'" onerror="this.style.display=\'none\'">';
}
function titleCase(s){return s?s.charAt(0)+s.slice(1).toLowerCase():'';}
function fmtRank(rk){
  if(!rk||!rk.tier)return 'Unranked';
  var apex=rk.tier==='MASTER'||rk.tier==='GRANDMASTER'||rk.tier==='CHALLENGER';
  return apex?(titleCase(rk.tier)+' '+(rk.lp||0)+' LP'):(titleCase(rk.tier)+' '+rk.division+' '+(rk.lp||0)+' LP');
}
function renderLiveGame(){
  var inGame=liveGame&&liveGame.inGame;
  var notInGame=liveGame&&!liveGame.inGame;
  var winCls=inGame?' w':'';
  var expCls=liveGameExpanded?' exp':'';
  var h='<div class="gc'+winCls+expCls+'"><div class="ws"></div><div>';
  // Top row — clickable to toggle expand. 3 columns: info | matchup | refresh
  h+='<div class="gr" onclick="toggleLiveGame()" style="grid-template-columns:130px 1fr 50px">';
  // Col 1: queue/result/duration (mirrors .gm)
  h+='<div class="gm">';
  h+='<div class="q">LIVE GAME</div>';
  if(inGame){
    h+='<div class="ti">'+(liveGame.queueName||'Custom')+'</div>';
    h+='<div class="re w">In Progress</div>';
  } else if(notInGame){
    h+='<div class="ti">No active game</div><div class="re" style="color:var(--t2)">—</div>';
  } else {
    h+='<div class="ti">Click ↻ to check</div><div class="re" style="color:var(--t2)">—</div>';
  }
  h+='</div>';
  // Col 2: matchup display (bans + picks)
  var NO_BAN_ICON='https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-champ-select/global/default/images/team-bans/icon-ban-small.png';
  var emptyPick=function(teamColor){
    return '<div style="width:36px;height:36px;border-radius:6px;border:2px solid '+teamColor+';background:rgba(122,138,158,.08);display:flex;align-items:center;justify-content:center"><span style="color:var(--t3,#444);font-size:1.2rem;line-height:1">?</span></div>';
  };
  var emptyBan=function(teamColor){
    return '<img src="'+NO_BAN_ICON+'" style="width:20px;height:20px;border-radius:3px;opacity:.6;border:1px solid '+teamColor+'" onerror="this.outerHTML=\'<div style=&quot;width:20px;height:20px;border-radius:3px;background:var(--bd);border:1px solid '+teamColor+'&quot;></div>\'">';
  };
  h+='<div style="display:flex;flex-direction:column;gap:6px;justify-content:center">';
  if(inGame){
    var bluePicksRaw=liveGame.participants.filter(function(p){return p.teamId===100;});
    var redPicksRaw=liveGame.participants.filter(function(p){return p.teamId===200;});
    var bluePicks=sortTeamByRole(bluePicksRaw);
    var redPicks=sortTeamByRole(redPicksRaw);
    var blueBans=(liveGame.bannedChampions||[]).filter(function(b){return b.teamId===100;});
    var redBans=(liveGame.bannedChampions||[]).filter(function(b){return b.teamId===200;});
    var myPuuid=D&&D.summoner?D.summoner.puuid:null;
    var pickCard=function(item,teamColor){
      var p=item.p, isMe=p.puuid===myPuuid;
      var border=isMe?'2px solid var(--ac)':'2px solid '+teamColor;
      var box=isMe?'box-shadow:0 0 8px rgba(200,155,60,.6);':'';
      var role=item.role&&item.role!=='UNKNOWN'?item.role:'';
      var s='<div style="display:flex;flex-direction:column;align-items:center;gap:2px">';
      s+='<img src="'+dd('champion',p.championKey||p.championName)+'" title="'+(p.championName||'?')+(role?' · '+role:'')+(isMe?' (You)':'')+'" style="width:36px;height:36px;border-radius:6px;border:'+border+';'+box+'" onerror="this.style.display=\'none\'">';
      s+='<div style="height:14px;display:flex;align-items:center">'+(role?roleIconHTML(role,12):'')+'</div>';
      s+='</div>';
      return s;
    };
    // Picks row — large icons, sorted by inferred role
    h+='<div style="display:flex;align-items:center;gap:8px;justify-content:center">';
    h+='<div style="display:flex;gap:3px">';
    bluePicks.forEach(function(it){h+=pickCard(it,'#3b82f6');});
    h+='</div>';
    h+='<span style="color:var(--t2);font-weight:700;font-size:.7rem;letter-spacing:1px">VS</span>';
    h+='<div style="display:flex;gap:3px">';
    redPicks.forEach(function(it){h+=pickCard(it,'#ef4444');});
    h+='</div>';
    h+='</div>';
    // Bans row — smaller, desaturated
    if(blueBans.length||redBans.length){
      h+='<div style="display:flex;align-items:center;gap:8px;justify-content:center;font-size:.6rem">';
      h+='<span style="color:var(--t2);letter-spacing:.5px;font-weight:600">BANS</span>';
      h+='<div style="display:flex;gap:2px">';
      blueBans.forEach(function(b){
        if(!b.championId||b.championId<0)h+=emptyBan('#3b82f6');
        else h+='<img src="'+dd('champion',b.championKey||b.championName)+'" title="'+(b.championName||'?')+' (banned)" style="width:20px;height:20px;border-radius:3px;filter:grayscale(.7) opacity(.7);border:1px solid #3b82f6" onerror="this.style.display=\'none\'">';
      });
      h+='</div>';
      h+='<span style="color:var(--t3,#555)">·</span>';
      h+='<div style="display:flex;gap:2px">';
      redBans.forEach(function(b){
        if(!b.championId||b.championId<0)h+=emptyBan('#ef4444');
        else h+='<img src="'+dd('champion',b.championKey||b.championName)+'" title="'+(b.championName||'?')+' (banned)" style="width:20px;height:20px;border-radius:3px;filter:grayscale(.7) opacity(.7);border:1px solid #ef4444" onerror="this.style.display=\'none\'">';
      });
      h+='</div>';
      h+='</div>';
    }
  } else {
    // Empty state: 5 placeholder picks per team + 5 placeholder bans per team
    h+='<div style="display:flex;align-items:center;gap:8px;justify-content:center">';
    h+='<div style="display:flex;gap:3px">';
    for(var ei=0;ei<5;ei++)h+=emptyPick('#3b82f6');
    h+='</div>';
    h+='<span style="color:var(--t3,#555);font-weight:700;font-size:.7rem;letter-spacing:1px">VS</span>';
    h+='<div style="display:flex;gap:3px">';
    for(var ei2=0;ei2<5;ei2++)h+=emptyPick('#ef4444');
    h+='</div>';
    h+='</div>';
    h+='<div style="display:flex;align-items:center;gap:8px;justify-content:center;font-size:.6rem">';
    h+='<span style="color:var(--t2);letter-spacing:.5px;font-weight:600">BANS</span>';
    h+='<div style="display:flex;gap:2px">';
    for(var eb=0;eb<5;eb++)h+=emptyBan('#3b82f6');
    h+='</div>';
    h+='<span style="color:var(--t3,#555)">·</span>';
    h+='<div style="display:flex;gap:2px">';
    for(var eb2=0;eb2<5;eb2++)h+=emptyBan('#ef4444');
    h+='</div>';
    h+='</div>';
  }
  h+='</div>';
  // Col 3: refresh (stop propagation so click doesn't toggle expand)
  h+='<div style="text-align:right">';
  h+='<span onclick="event.stopPropagation();fetchLiveGame()" title="Refresh live game" style="cursor:pointer;color:'+(liveGameLoading?'var(--ac)':'var(--t2)')+';font-size:1.3rem;display:inline-block;'+(liveGameLoading?'animation:spin 1s linear infinite':'')+'">'+(liveGameLoading?'⟳':'↻')+'</span>';
  h+='</div>';
  h+='</div>'; // close .gr
  // Detail (.gd) — full scorecard, only shown when expanded (CSS handles visibility)
  h+='<div class="gd">';
  if(inGame){
    var blue2=liveGame.participants.filter(function(p){return p.teamId===100;});
    var red2=liveGame.participants.filter(function(p){return p.teamId===200;});
    var blueAvg2=avgLobbyRank(blue2);
    var redAvg2=avgLobbyRank(red2);
    var lgTeamHdr=function(label,avg,teamColor){
      if(!avg)return '<tr class="team-hdr"><td colspan="4" style="padding:6px 10px;color:var(--t2);font-size:.7rem;font-weight:600;letter-spacing:.5px"><span style="color:'+teamColor+'">'+label+'</span> <span style="color:var(--t3,#555)">(no ranked data)</span></td></tr>';
      var tier=avg.display.split(' ')[0].toLowerCase();
      return '<tr class="team-hdr"><td colspan="4" style="padding:6px 10px;font-size:.72rem"><span style="color:'+teamColor+';font-weight:700;letter-spacing:.5px">'+label+'</span> <img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-mini-crests/'+tier+'.svg" style="width:14px;height:14px;vertical-align:middle;margin:0 3px 0 8px" onerror="this.style.display=\'none\'"><span style="color:var(--ac2);font-weight:700">'+avg.display+'</span></td></tr>';
    };
    var lgRow=function(teamSorted,cls){
      var o='';
      teamSorted.forEach(function(it){
        var p=it.p;
        var pr=playerRanks[p.puuid];
        var hasRank=pr&&pr.tier;
        var rt=fmtRank(pr);
        var rcol=hasRank?'var(--t2)':'var(--t3,#555)';
        var rIcon='';
        if(hasRank){var tl=pr.tier.toLowerCase();rIcon='<img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-mini-crests/'+tl+'.svg" style="width:14px;height:14px;vertical-align:middle;margin-right:3px" onerror="this.style.display=\'none\'">';}
        var badges='';
        if(pr&&pr.hotStreak)badges+=' <span title="Hot Streak (3+ wins)" style="font-size:.78rem;line-height:1">🔥</span>';
        if(pr&&pr.freshBlood)badges+=' <span class="stag avg" style="background:rgba(34,197,94,.18);color:#22c55e">NEW</span>';
        if(pr&&pr.veteran)badges+=' <span class="stag avg" style="background:rgba(122,138,158,.18);color:var(--t2)">VET</span>';
        var wlCell='<span style="color:var(--t3,#555)">—</span>';
        var wrCell='<span style="color:var(--t3,#555)">—</span>';
        if(pr&&(pr.wins!=null||pr.losses!=null)){
          var w=pr.wins||0,l=pr.losses||0,tot=w+l;
          if(tot>0){
            var wr=Math.round(w/tot*100);
            var wrColor=wr>=55?'#22c55e':wr<=45?'#ef4444':'var(--t2)';
            var wrBg=wr>=55?'rgba(34,197,94,.14)':wr<=45?'rgba(239,68,68,.14)':'rgba(122,138,158,.12)';
            wlCell='<span style="color:#22c55e;font-weight:600">'+w+'W</span> <span style="color:#ef4444;font-weight:600">'+l+'L</span>';
            wrCell='<span style="background:'+wrBg+';color:'+wrColor+';font-weight:700;padding:2px 7px;border-radius:4px;font-size:.7rem">'+wr+'%</span>';
          }
        }
        var isMe=D&&D.summoner&&p.puuid===D.summoner.puuid;
        var meCls=isMe?' me':'';
        var nameCell='<div style="line-height:1.15;font-weight:'+(isMe?'700':'600')+'">'+(p.riotId||'?')+badges+'</div><div style="font-size:.62rem;color:'+rcol+';margin-top:1px;display:flex;align-items:center">'+rIcon+'<span>'+rt+'</span></div>';
        var roleCell=it.role&&it.role!=='UNKNOWN'?roleIconHTML(it.role,16):'';
        var champCell='<img src="'+dd('champion',p.championKey||p.championName)+'" style="width:24px;height:24px;border-radius:3px;vertical-align:middle;margin-right:6px" onerror="this.style.display=\'none\'">'+(p.championName||'?');
        o+='<tr class="'+meCls+'"><td style="text-align:center;width:24px">'+roleCell+'</td><td class="'+cls+'" style="padding-left:8px;min-width:180px">'+nameCell+'</td><td>'+champCell+'</td><td>'+wlCell+'</td><td>'+wrCell+'</td></tr>';
      });
      return o;
    };
    var blue2Sorted=sortTeamByRole(blue2);
    var red2Sorted=sortTeamByRole(red2);
    var lgTeamHdr2=function(label,avg,teamColor){
      if(!avg)return '<tr class="team-hdr"><td colspan="5" style="padding:6px 10px;color:var(--t2);font-size:.7rem;font-weight:600;letter-spacing:.5px"><span style="color:'+teamColor+'">'+label+'</span> <span style="color:var(--t3,#555)">(no ranked data)</span></td></tr>';
      var tier=avg.display.split(' ')[0].toLowerCase();
      return '<tr class="team-hdr"><td colspan="5" style="padding:6px 10px;font-size:.72rem"><span style="color:'+teamColor+';font-weight:700;letter-spacing:.5px">'+label+'</span> <img src="https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/global/default/images/ranked-mini-crests/'+tier+'.svg" style="width:14px;height:14px;vertical-align:middle;margin:0 3px 0 8px" onerror="this.style.display=\'none\'"><span style="color:var(--ac2);font-weight:700">'+avg.display+'</span></td></tr>';
    };
    h+='<table class="sb" style="margin-top:8px"><tr><th></th><th>Player</th><th>Champion</th><th>W/L</th><th>WR%</th></tr>';
    h+=lgTeamHdr2('BLUE TEAM',blueAvg2,'#3b82f6');
    h+=lgRow(blue2Sorted,'tb2');
    h+='<tr class="ts"><td colspan="5"></td></tr>';
    h+=lgTeamHdr2('RED TEAM',redAvg2,'#ef4444');
    h+=lgRow(red2Sorted,'tr2');
    h+='</table>';
  } else if(notInGame){
    h+='<div style="color:var(--t2);font-size:.85rem;padding:14px 0">Not currently in a game.</div>';
  } else {
    h+='<div style="color:var(--t2);font-size:.85rem;padding:14px 0;font-style:italic">Hit the refresh icon (↻) above to check.</div>';
  }
  h+='</div>'; // close .gd
  h+='</div></div>'; // close .gc
  return h;
}
function goTag(idx,met,champ,role){
  exp=idx;views[idx]='masters';replayTLMetric[idx]=met;
  if(role)tagMastersRole[idx]=role;else delete tagMastersRole[idx];
  var utl=getUserTL(idx);
  // Check if clicking own tag
  var isSelf=false;
  if(utl&&utl.allPlayers){
    utl.allPlayers.forEach(function(p){if(p.isMe&&champKey(p.champion)===champ)isSelf=true;});
  }
  // Hide "You" when focusing another player's tag
  gameTLOff[idx+'_me']=!isSelf&&!!champ;
  if(utl&&utl.allPlayers){
    utl.allPlayers.forEach(function(p,pi){
      if(p.isMe)return;
      if(champ)gameTLOff[idx+'_'+pi]=champKey(p.champion)!==champ;
      else gameTLOff[idx+'_'+pi]=false;
    });
  }
  render();var el=document.querySelector('[data-i="'+idx+'"]');if(el)el.scrollIntoView({behavior:'smooth',block:'start'});
}

function updateRate(){
  fetch('/api/rate').then(function(r){return r.json();}).then(function(d){
    var el=document.getElementById('rateBar');
    if(!el)return;
    var pct=Math.round(d.used2m/d.max2m*100);
    var cls=pct<60?'rate-ok':pct<85?'rate-warn':'rate-danger';
    el.innerHTML='<span>API: '+d.used2m+'/'+d.max2m+' (2m)</span><div class="rate-meter"><div class="rate-fill '+cls+'" style="width:'+pct+'%"></div></div><span>'+d.used1s+'/'+d.max1s+' (1s)</span><span>Total: '+d.total+'</span>'+(d.waiting?'<span style="color:var(--av)">&#9679; Rate limited...</span>':'');
  }).catch(function(){});
}
updateRate();
setInterval(updateRate,1000);

renderEmpty();
</script>
</body>
</html>"""

# ─── Server ──────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        try:
            return self._do_GET()
        except Exception as e:
            import traceback; traceback.print_exc()
            try: self._j({"error": f"server-error: {type(e).__name__}: {e}"}, status=500)
            except Exception: pass

    def do_POST(self):
        try:
            return self._do_POST()
        except Exception as e:
            import traceback; traceback.print_exc()
            try: self._j({"error": f"server-error: {type(e).__name__}: {e}"}, status=500)
            except Exception: pass

    def _do_GET(self):
        if self.path == '/api/rate':
            self._j(get_rate_info())
        elif self.path.startswith('/api/masters-cache'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            region_filter = qs.get("region", ["na1"])[0]
            # First try preferred region, then fall back to ANY region
            result = {}
            # Priority 1: the requested region
            preferred_suffix = f"_{region_filter}.json"
            for f in os.listdir(CACHE_DIR) if os.path.exists(CACHE_DIR) else []:
                if f.startswith("masters_") and f.endswith(preferred_suffix):
                    try:
                        with open(os.path.join(CACHE_DIR, f)) as fh:
                            d = json.load(fh)
                            if d.get("role"): result[d["role"]] = d
                    except Exception: pass
            # Priority 2: fill any missing roles from other regions
            for f in os.listdir(CACHE_DIR) if os.path.exists(CACHE_DIR) else []:
                if f.startswith("masters_") and f.endswith(".json"):
                    try:
                        with open(os.path.join(CACHE_DIR, f)) as fh:
                            d = json.load(fh)
                            role = d.get("role")
                            if role and role not in result:
                                result[role] = d
                    except Exception: pass
            self._j(result)
        elif self.path in ('/riot.txt', '/login/riot.txt'):
            token = b"6a06de36-6904-451a-a4f5-b423feb3b08d\n"
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(token)))
            self.end_headers()
            self.wfile.write(token)
        else:
            self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers()
            self.wfile.write(HTML.encode())

    def _do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))).decode()) if int(self.headers.get('Content-Length',0)) > 0 else {}

        if self.path == '/api/matches':
            key = API_KEY; name = body.get("name",""); tag = body.get("tag","")
            region = body.get("region","na1")
            count = int(body.get("count", 0) or 0)
            if not key: self._j({"error":"No API key"}); return
            if not name or not tag: self._j({"error":"Enter Name#Tag"}); return
            print(f"[LOAD] {name}#{tag} ({region}) count={count}")
            data, err = fetch_matches(key, name, tag, region=region, count=count)
            if err:
                if err == "KEY_EXPIRED": self._j({"error":"API key expired or invalid"}); return
                self._j({"error": err}); return
            print(f"[LOAD] Done — {len(data['games'])} games")
            self._j({"ok": True, "data": data})

        elif self.path == '/api/live-game':
            key = API_KEY
            if not key: self._j({"error":"No API key"}); return
            puuid = body.get("puuid","")
            region = body.get("region","na1")
            if not puuid: self._j({"error":"No puuid"}); return
            result, err = fetch_live_game(key, puuid, region)
            if err == "KEY_EXPIRED": self._j({"error":"API key expired"}); return
            if err == "NOT_IN_GAME": self._j({"ok": True, "inGame": False}); return
            if err: self._j({"error": err}); return
            self._j({"ok": True, "inGame": True, "data": result})

        elif self.path == '/api/player-ranks':
            key = API_KEY
            puuids = body.get("puuids", [])
            region = body.get("region", "na1")
            if not key: self._j({"error":"No API key"}); return
            ranks = {}
            for pu in puuids:
                if not pu: continue
                # Check cache
                cf = os.path.join(CACHE_DIR, "ranks")
                os.makedirs(cf, exist_ok=True)
                cache_file = os.path.join(cf, f"{pu}.json")
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file) as fh:
                            cached = json.load(fh)
                        cr = cached.get("rank")
                        # Only use cached if it has the extended fields (or is None/unranked)
                        has_extended = cr is None or "hotStreak" in cr
                        if has_extended and time.time() - cached.get("_ts", 0) < 3600:
                            ranks[pu] = cr
                            continue
                    except Exception: pass
                # Fetch
                data, err = riot_get(f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{pu}", key)
                if err == "KEY_EXPIRED":
                    self._j({"error": "API key expired"}); return
                solo = None
                if data and isinstance(data, list):
                    for q in data:
                        if q.get("queueType") == "RANKED_SOLO_5x5":
                            solo = {"tier": q.get("tier",""), "division": q.get("rank",""),
                                    "lp": q.get("leaguePoints",0),
                                    "wins": q.get("wins",0), "losses": q.get("losses",0),
                                    "hotStreak": q.get("hotStreak", False),
                                    "veteran": q.get("veteran", False),
                                    "freshBlood": q.get("freshBlood", False),
                                    "inactive": q.get("inactive", False),
                                    "miniSeries": q.get("miniSeries")}
                            break
                ranks[pu] = solo
                with open(cache_file, "w") as fh:
                    json.dump({"rank": solo, "_ts": time.time()}, fh)
            self._j({"ok": True, "ranks": ranks})

        elif self.path == '/api/masters':
            key = API_KEY
            if not key: self._j({"error":"No API key"}); return
            role = body.get("role","JUNGLE"); force = body.get("force",False)
            print(f"[MASTERS] {role}")
            result, err = fetch_masters_role(key, role, force=force)
            if err:
                if err == "KEY_EXPIRED": self._j({"error":"API key expired"}); return
                self._j({"error": err}); return
            self._j({"ok": True, "data": result})

        elif self.path == '/api/masters-all':
            key = API_KEY
            if not key: self._j({"error":"No API key"}); return
            force = body.get("force", False)
            count = body.get("count", 30)
            start_date = body.get("startDate")
            region = body.get("region","na1")
            def pull_all():
                if force:
                    for r in ["TOP","JUNGLE","MIDDLE","BOTTOM","UTILITY"]:
                        cf = os.path.join(CACHE_DIR, f"masters_{r}_{region}.json")
                        if os.path.exists(cf): os.remove(cf)
                err = fetch_all_masters(key, region=region, target_per_role=count, start_date=start_date)
                if err == "KEY_EXPIRED": print("[MASTERS] Key expired")
            threading.Thread(target=pull_all, daemon=True).start()
            self._j({"ok": True, "status": "pulling all roles"})

        elif self.path == '/api/comparable':
            key = API_KEY
            if not key: self._j({"error":"No API key"}); return
            rid = body.get("riotId",""); role = body.get("role","JUNGLE")
            count = body.get("count",10); maxAge = body.get("maxAgeDays",30); gid = body.get("gameId")
            cid = body.get("championId"); region = body.get("region","na1")
            if not rid: self._j({"error":"Enter a Riot ID"}); return
            print(f"[COMP] {rid} -> {role}")
            result, err = fetch_comparable(key, rid, role, region=region, count=count, user_game_id=gid, max_age_days=maxAge, champ_id=cid)
            if err:
                if err == "KEY_EXPIRED": self._j({"error":"API key expired"}); return
                self._j({"error": err}); return
            self._j({"ok": True, "analysis": result})

        elif self.path == '/api/download-replay':
            gid = body.get("gameId")
            if not gid: self._j({"error": "No game ID"}); return
            # Connect to LCU
            try:
                lf = None
                try:
                    out = subprocess.check_output('wmic PROCESS WHERE name="LeagueClientUx.exe" GET commandline', shell=True, text=True, stderr=subprocess.DEVNULL)
                    for tok in out.split('"'):
                        if "LeagueClientUx.exe" in tok:
                            p = os.path.join(os.path.dirname(tok), "lockfile")
                            if os.path.exists(p): lf = p
                except Exception: pass
                if not lf:
                    for p in [r"C:\Riot Games\League of Legends", r"D:\Riot Games\League of Legends"]:
                        pp = os.path.join(p, "lockfile")
                        if os.path.exists(pp): lf = pp; break
                if not lf:
                    self._j({"error": "League client not running"}); return
                with open(lf) as f: parts = f.read().strip().split(":")
                port, pw = parts[2], parts[3]
                # Request download
                url = f"https://127.0.0.1:{port}/lol-replays/v1/rofls/{gid}/download"
                req = urllib.request.Request(url, method="POST", data=b'{}')
                req.add_header("Authorization", "Basic " + base64.b64encode(f"riot:{pw}".encode()).decode())
                req.add_header("Content-Type", "application/json")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                    r.read()  # consume response, may be empty
                print(f"[REPLAY] Download requested for {gid}")
                self._j({"ok": True})
            except Exception as e:
                self._j({"error": str(e)})

        elif self.path == '/api/replay-status':
            gid = body.get("gameId")
            if not gid: self._j({"error":"no id"}); return
            try:
                lf = None
                try:
                    out = subprocess.check_output('wmic PROCESS WHERE name="LeagueClientUx.exe" GET commandline', shell=True, text=True, stderr=subprocess.DEVNULL)
                    for tok in out.split('"'):
                        if "LeagueClientUx.exe" in tok:
                            p = os.path.join(os.path.dirname(tok), "lockfile")
                            if os.path.exists(p): lf = p
                except Exception: pass
                if not lf:
                    for p in [r"C:\Riot Games\League of Legends", r"D:\Riot Games\League of Legends"]:
                        pp = os.path.join(p, "lockfile")
                        if os.path.exists(pp): lf = pp; break
                if not lf: self._j({"error":"no client"}); return
                with open(lf) as f: parts = f.read().strip().split(":")
                port, pw = parts[2], parts[3]
                url = f"https://127.0.0.1:{port}/lol-replays/v1/rofls/{gid}"
                req = urllib.request.Request(url)
                req.add_header("Authorization", "Basic " + base64.b64encode(f"riot:{pw}".encode()).decode())
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
                    data = json.loads(r.read().decode())
                self._j({"state": data.get("state","unknown")})
            except Exception as e:
                self._j({"error": str(e)})

        elif self.path == '/api/close-replay':
            # Kill current replay by closing the game process
            try:
                subprocess.run('taskkill /F /IM "League of Legends.exe"', shell=True, capture_output=True, timeout=5)
                print("[REPLAY] Closed existing replay")
                self._j({"ok": True})
            except Exception as e:
                self._j({"ok": True, "note": str(e)})  # Don't error if no replay was running

        elif self.path == '/api/open-replay':
            gid = body.get("gameId")
            if not gid: self._j({"error": "No game ID"}); return
            try:
                lf = None
                try:
                    out = subprocess.check_output('wmic PROCESS WHERE name="LeagueClientUx.exe" GET commandline', shell=True, text=True, stderr=subprocess.DEVNULL)
                    for tok in out.split('"'):
                        if "LeagueClientUx.exe" in tok:
                            p = os.path.join(os.path.dirname(tok), "lockfile")
                            if os.path.exists(p): lf = p
                except Exception: pass
                if not lf:
                    for p in [r"C:\Riot Games\League of Legends", r"D:\Riot Games\League of Legends"]:
                        pp = os.path.join(p, "lockfile")
                        if os.path.exists(pp): lf = pp; break
                if not lf:
                    self._j({"error": "League client not running"}); return
                with open(lf) as f: parts = f.read().strip().split(":")
                port, pw = parts[2], parts[3]
                url = f"https://127.0.0.1:{port}/lol-replays/v1/rofls/{gid}/watch"
                req = urllib.request.Request(url, method="POST", data=b'{}')
                req.add_header("Authorization", "Basic " + base64.b64encode(f"riot:{pw}".encode()).decode())
                req.add_header("Content-Type", "application/json")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                    r.read()
                print(f"[REPLAY] Watch requested for {gid}")
                self._j({"ok": True})
            except Exception as e:
                self._j({"error": str(e)})

        elif self.path == '/api/user-timeline':
            key = API_KEY; gid = body.get("gameId"); cid = body.get("championId")
            region = body.get("region","na1")
            if not key or not gid or not cid: self._j({"error":"missing"}); return
            tl = fetch_user_timeline(key, gid, cid, region)
            self._j({"ok": True, "timeline": tl})

        else:
            self.send_response(404); self.end_headers()

    def _j(self, d, status=200):
        try:
            payload = json.dumps(d).encode()
        except Exception as e:
            payload = json.dumps({"error": f"serialize-failed: {e}"}).encode()
            status = 500
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try: self.wfile.write(payload)
        except Exception: pass

def main():
    import argparse, socketserver
    global API_KEY
    parser = argparse.ArgumentParser(description="League of Legends Game Analyzer — Web UI")
    parser.add_argument("--api-key", "-k", required=True, help="Riot API key (RGAPI-...)")
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind (default 127.0.0.1; use 0.0.0.0 for all interfaces)")
    parser.add_argument("--port", "-p", type=int, default=PORT, help=f"Port to listen on (default {PORT})")
    args = parser.parse_args()
    API_KEY = args.api_key.strip()
    if not API_KEY.startswith("RGAPI-"):
        print(f"[WARN] API key doesn't look like an RGAPI- prefixed key — proceeding anyway")
    class ThreadedServer(socketserver.ThreadingMixIn, HTTPServer): daemon_threads = True
    print(f"[OK] API key loaded ({API_KEY[:12]}...)")
    try:
        server = ThreadedServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"[ERROR] Could not bind {args.host}:{args.port} — {e}")
        sys.exit(1)
    display_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"[OK] Listening on {args.host}:{args.port}  →  http://{display_host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopped."); server.shutdown()

if __name__ == "__main__": main()
