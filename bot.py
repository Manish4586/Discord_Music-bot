# bot.py
import asyncio
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    print("[boot] uvloop enabled")
except Exception as e:
    print(f"[boot] uvloop not in use: {e}")
import os
import re
import json
import time
from dataclasses import dataclass
from typing import Optional, List

import discord
from discord.ext import commands, tasks
from yt_dlp import YoutubeDL
from dotenv import load_dotenv

# ========= Load token =========
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("ERROR: Put DISCORD_TOKEN=yourtoken inside .env")

# ========= Storage paths =========
DOWNLOAD_DIR = os.path.expanduser("/home/manish4586/discord-music/music")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DATA_DIR = os.path.expanduser("/home/manish4586/discord-music")
os.makedirs(DATA_DIR, exist_ok=True)
STATS_PATH = os.path.join(DATA_DIR, "stats.json")

CACHE_DIR = os.path.join(DATA_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ========= Bot setup =========
COMMAND_PREFIX = "!"
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

START_TIME = time.time()

# ========= Helpers =========
def ui(title, desc="", color=0x5865F2):
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text="🎵 TalibanAudioBot • Raspberry Pi 5")
    return e

def bar(frac, width=25):
    frac = max(0, min(1, frac))
    fill = int(frac * width)
    return "▰" * fill + "▱" * (width - fill)

def fmt_time(seconds):
    if not seconds: return "00:00:00"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fmt_mmss(seconds):
    if not seconds: return "?:??"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"

# ========= System info =========
def get_uptime_sec():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except:
        return int(time.time() - START_TIME)

def get_mem():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k,v = line.split(":")
                info[k] = int(v.split()[0]) * 1024
        total = info["MemTotal"]
        free = info.get("MemAvailable", info["MemFree"])
        used = total - free
        return total, used, free
    except:
        return 0,0,0

def get_load():
    try:
        with open("/proc/loadavg") as f:
            p = f.read().split()
            return float(p[0]), float(p[1]), float(p[2])
    except:
        return 0,0,0

# ========= Stats Storage (SAFE, FIXED) =========
def load_stats():
    base = {"total_songs":0, "total_play_time":0.0, "users":{}, "songs":{}}
    if os.path.exists(STATS_PATH):
        try:
            loaded = json.load(open(STATS_PATH, "r"))
            if isinstance(loaded, dict):
                base.update(loaded)
        except:
            pass

    # ensure valid structure
    if not isinstance(base.get("users"), dict):
        base["users"] = {}
    if not isinstance(base.get("songs"), dict):
        base["songs"] = {}

    # ensure song entries contain "users" list
    for vid, entry in base["songs"].items():
        if "users" not in entry or not isinstance(entry["users"], list):
            entry["users"] = []
        else:
            # remove duplicates & ensure ints
            entry["users"] = list(dict.fromkeys(int(x) for x in entry["users"]))

    return base

def save_stats(data):
    json.dump(data, open(STATS_PATH, "w"), indent=2)

STORED = load_stats()

def add_user_time(uid, sec):
    u = STORED["users"].setdefault(str(uid), {"time":0,"songs":0})
    u["time"] += sec
    STORED["total_play_time"] += sec
    save_stats(STORED)

def add_user_song(uid):
    u = STORED["users"].setdefault(str(uid), {"time":0,"songs":0})
    u["songs"] += 1
    STORED["total_songs"] += 1
    save_stats(STORED)

def add_song_play(video_id: str, title: str, user_id: int):
    s = STORED["songs"].setdefault(video_id, {"title": title, "plays": 0, "users": []})
    s["title"] = title
    s["plays"] += 1
    if user_id not in s["users"]:
        s["users"].append(user_id)
    save_stats(STORED)

# ========= Track Model =========
@dataclass
class Track:
    url: str
    title: str
    video_id: str
    file: str
    thumb: str
    requested_by_id: int
    duration: Optional[int]

# ========= Player =========
class Player:
    def __init__(self, gid):
        self.gid = gid
        self.voice = None
        self.queue = []
        self.history = []
        self.current = None
        self.repeat_mode = 0
        self.panel = None
        self.start_t = None
        self.pause_t = None
        self.paused_accum = 0

    async def ensure_voice(self, ctx):
        if not ctx.author.voice:
            raise commands.CommandError("Join a voice channel first.")
        ch = ctx.author.voice.channel
        if not self.voice:
            self.voice = await ch.connect(self_deaf=True)
        elif self.voice.channel != ch:
            await self.voice.move_to(ch)

    def progress(self):
        if not self.start_t: return 0
        if self.pause_t: return self.pause_t - self.start_t - self.paused_accum
        return time.time() - self.start_t - self.paused_accum

    async def loop(self, ctx):
        while True:
            if self.repeat_mode == 1 and self.current:
                track = self.current
            elif self.queue:
                track = self.queue.pop(0)
                self.current = track
                self.history.append(track)
                add_user_song(track.requested_by_id)
                add_song_play(track.video_id, track.title, track.requested_by_id)
            elif self.repeat_mode == 2 and self.history:
                self.queue = self.history.copy()
                self.history = []
                continue
            else:
                break

            self.start_t = time.time()
            self.pause_t = None
            self.paused_accum = 0

            await self.ensure_voice(ctx)
            self.voice.play(discord.FFmpegPCMAudio(track.file))

            try:
                await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=track.title))
            except:
                pass

            if self.panel:
                try: await self.panel.delete()
                except: pass

            embed = ui("▶️ Now Playing", f"**{track.title}**\nRequested by <@{track.requested_by_id}>")
            embed.set_thumbnail(url=track.thumb)
            self.panel = await ctx.send(embed=embed)

            while self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                await asyncio.sleep(0.5)

        self.current = None
        self.panel = None

        try:
            await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="YouTube Music"))
        except:
            pass


players = {}
def getp(g):
    if g.id not in players:
        players[g.id] = Player(g.id)
    return players[g.id]

# ========= yt-dlp =========
YDL_OPTS = {
    "format":"bestaudio/best",
    "quiet":True,
    "no_warnings":True,
    "noplaylist":True,
    "restrictfilenames":True,
    "cachedir": CACHE_DIR,
    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
    "postprocessors":[
        {"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"},
        {"key":"FFmpegMetadata"}
    ]
}
YOUTUBE_URL_RE = re.compile("(youtube|youtu.be)")
search_results = {}

async def build_track(ctx, query, uid):
    loop = asyncio.get_event_loop()
    msg = await ctx.send(embed=ui("🔍 Fetching Audio...", f"**{query}**"))

    def probe():
        with YoutubeDL({"quiet": True, "skip_download": True}) as y:
            return y.extract_info(query, download=False)

    info = await loop.run_in_executor(None, probe)

    if "entries" in info:
        info = info["entries"][0]

    vid = info["id"]
    title = info.get("title", "Unknown")
    url = info.get("webpage_url", query)
    file = os.path.join(DOWNLOAD_DIR, f"{vid}.mp3")
    thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    duration = info.get("duration")

    if os.path.exists(file):
        await msg.edit(embed=ui("🎶 Already Cached", f"**{title}** is ready."))
        await asyncio.sleep(3)
        try:
            await msg.delete()
        except:
            pass
        return Track(url, title, vid, file, thumb, uid, duration)

    await msg.edit(embed=ui("🎧 Processing...", f"**{title}**"))

    def dl():
        with YoutubeDL(YDL_OPTS) as y:
            y.download([url])

    await loop.run_in_executor(None, dl)

    await msg.edit(embed=ui("✅ Ready", f"**{title}**"))
    await asyncio.sleep(3)
    try:
        await msg.delete()
    except:
        pass

    return Track(url, title, vid, file, thumb, uid, duration)

# ========= Panel Refresh & Playtime =========
@tasks.loop(seconds=5)
async def update_panels_and_tick_time():
    for p in players.values():
        if not p.voice or not p.current: continue
        if not (p.voice.is_playing() or p.voice.is_paused()): continue

        add_user_time(p.current.requested_by_id, 5)

        if p.panel:
            try:
                played = p.progress()
                total = p.current.duration or 0
                frac = played/total if total else 0
                embed = ui(
                    "▶️ Now Playing",
                    f"**{p.current.title}**\n"
                    f"Requested by <@{p.current.requested_by_id}>\n\n"
                    f"`{fmt_mmss(played)} / {fmt_mmss(total)}`\n"
                    f"{bar(frac)}"
                )
                embed.set_thumbnail(url=p.current.thumb)
                await p.panel.edit(embed=embed)
            except:
                pass

@update_panels_and_tick_time.before_loop
async def _wait_ready():
    await bot.wait_until_ready()

@tasks.loop(hours=1)
async def cleanup_cache():
    cutoff = time.time() - (60 * 24 * 3600)
    for f in os.listdir(DOWNLOAD_DIR):
        if f.endswith(".mp3"):
            p = os.path.join(DOWNLOAD_DIR,f)
            if os.path.getmtime(p)<cutoff:
                try: os.remove(p)
                except: pass

@cleanup_cache.before_loop
async def _wait_ready2():
    await bot.wait_until_ready()

# ========= Events =========
@bot.event
async def on_ready():
    print("Logged in as", bot.user)
    update_panels_and_tick_time.start()
    cleanup_cache.start()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="YouTube Music"))

# ========= Commands =========
@bot.command()
async def help(ctx):
    cmds = """
**Play**
!play or !p
!np / !now / !nowplay
!search
!queue

**Control**
!next / !n
!prev
!stop / !s
!repeat / !r
!repeatall / !ra

**Voice**
!leave / !d

**Stats**
!server
!stats
!leaderboard / !lb
"""
    await ctx.send(embed=ui("📘 Commands", cmds))

@bot.command()
async def search(ctx,*,query):
    await ctx.send(embed=ui("🔍 Searching…",f"**{query}**"))
    with YoutubeDL({"quiet":True}) as y:
        info = y.extract_info(f"ytsearch5:{query}",download=False)
    results = info.get("entries",[])
    if not results:
        return await ctx.send(embed=ui("⚠️ Not found"))
    search_results[ctx.author.id] = results
    text = "\n".join([f"**{i+1}.** {r['title']}" for i,r in enumerate(results)])
    await ctx.send(embed=ui("🎶 Results", text+"\n\nUse `!play 1` to select."))

@bot.command()
async def play(ctx,*,query):
    p = getp(ctx.guild)
    if not ctx.voice_client:
        if not ctx.author.voice:
            return await ctx.send(embed=ui("⚠️ Join voice first"))
        p.voice = await ctx.author.voice.channel.connect(self_deaf=True)
    else:
        p.voice = ctx.voice_client

    if query.isdigit() and ctx.author.id in search_results:
        i = int(query)-1
        arr = search_results[ctx.author.id]
        if 0 <= i < len(arr):
            query = arr[i]["webpage_url"]

    if not YOUTUBE_URL_RE.search(query):
        with YoutubeDL({"quiet":True}) as y:
            info = y.extract_info(f"ytsearch1:{query}",download=False)
        query = info["entries"][0]["webpage_url"]
    track = await build_track(ctx, query, ctx.author.id)
    p.queue.append(track)
    if not p.voice.is_playing() and not p.voice.is_paused():
        await p.loop(ctx)
    else:
        position = len(p.queue)
        await ctx.send(embed=ui("➕ Added to Queue", f"**{track.title}**\nPosition: `{position}`"))

@bot.command(name="p")
async def alias_p(ctx,*,query):
    await ctx.invoke(bot.get_command("play"),query=query)

@bot.command()
async def next(ctx):
    p = getp(ctx.guild)
    if p.voice: p.voice.stop()

@bot.command(name="n")
async def alias_n(ctx):
    await ctx.invoke(bot.get_command("next"))

@bot.command()
async def prev(ctx):
    p = getp(ctx.guild)
    if len(p.history) >= 2:
        last = p.history.pop()
        p.queue.insert(0,last)
        if p.voice: p.voice.stop()
    else:
        await ctx.send(embed=ui("ℹ️ No previous track."))

@bot.command()
async def stop(ctx):
    p = getp(ctx.guild)
    p.queue.clear()
    p.history.clear()
    p.repeat_mode = 0
    if p.voice: p.voice.stop()
    await ctx.send(embed=ui("🛑 Stopped", "Queue cleared."))

@bot.command(name="s")
async def alias_s(ctx):
    await ctx.invoke(bot.get_command("stop"))

@bot.command()
async def repeat(ctx):
    p = getp(ctx.guild)
    p.repeat_mode = 1 if p.repeat_mode!=1 else 0
    await ctx.send(embed=ui("🔁 Repeat One", f"**{'ON' if p.repeat_mode==1 else 'OFF'}**"))

@bot.command(name="r")
async def alias_r(ctx):
    await ctx.invoke(bot.get_command("repeat"))

@bot.command()
async def repeatall(ctx):
    p = getp(ctx.guild)
    p.repeat_mode = 2 if p.repeat_mode!=2 else 0
    await ctx.send(embed=ui("🔂 Repeat All", f"**{'ON' if p.repeat_mode==2 else 'OFF'}**"))

@bot.command(name="ra")
async def alias_ra(ctx):
    await ctx.invoke(bot.get_command("repeatall"))

@bot.command()
async def leave(ctx):
    p = getp(ctx.guild)
    if p.voice:
        await p.voice.disconnect(force=True)
        p.voice = None
        p.current = None
        p.panel = None
        await ctx.send(embed=ui("👋 Left Voice"))
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="YouTube Music"))

@bot.command(name="d")
async def alias_d(ctx):
    await ctx.invoke(bot.get_command("leave"))

@bot.command()
async def queue(ctx):
    p = getp(ctx.guild)
    if not p.queue:
        return await ctx.send(embed=ui("📜 Queue", "Empty."))

    text = ""
    for i, t in enumerate(p.queue, start=1):
        text += f"**{i}.** {t.title}\n"

    embed = ui("📜 Queue", text[:2000])
    await ctx.send(embed=embed)

@bot.command(name="np", aliases=["now", "nowplay"])
async def now_playing(ctx):
    p = getp(ctx.guild)
    if not p.current:
        return await ctx.send(embed=ui("⏹️ Idle", "Nothing is playing."))

    if p.panel:
        try:
            await p.panel.delete()
        except:
            pass

    played = p.progress()
    total = p.current.duration or 0
    frac = (played / total) if total else 0
    embed = ui(
        "▶️ Now Playing",
        f"**{p.current.title}**\n"
        f"Requested by <@{p.current.requested_by_id}>\n\n"
        f"`{fmt_mmss(played)} / {fmt_mmss(total)}`\n"
        f"{bar(frac)}"
        )
    embed.set_thumbnail(url=p.current.thumb)

    p.panel = await ctx.send(embed=embed)

# ========= Stats =========
@bot.command()
async def server(ctx):
    up = get_uptime_sec()
    total,used,free = get_mem()
    l1,l5,l15 = get_load()
    desc = (
        f"Uptime: **{fmt_time(up)}**\n"
        f"RAM: **{total/1e9:.2f} GB total**, **{used/1e9:.2f} GB used**, **{free/1e9:.2f} GB free**\n"
        f"Load avg: **{l1:.2f} {l5:.2f} {l15:.2f}**\n"
        f"Music time: **{fmt_time(STORED['total_play_time'])}**\n"
        f"Songs played: **{STORED['total_songs']}**"
    )
    await ctx.send(embed=ui("🖥️ Server", desc))

@bot.command()
async def stats(ctx, user: Optional[discord.Member]=None):
    user = user or ctx.author
    uid = str(user.id)

    u = STORED["users"].get(uid, {"time": 0, "songs": 0})

    # Count unique songs listened by this user
    unique_song_count = 0
    for vid, data in STORED.get("songs", {}).items():
        if user.id in data.get("users", []):
            unique_song_count += 1

    desc = (
        f"User: {user.mention}\n"
        f"Time listened: **{fmt_time(u['time'])}**\n"
        f"Unique songs listened: **{unique_song_count}**\n"
        f"Songs requested: **{u['songs']}**"
    )

    await ctx.send(embed=ui("📈 Stats", desc))

@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_cmd(ctx):
    # Top Users by time listened
    users = [
        (int(uid), data.get("time", 0.0), data.get("songs", 0))
        for uid, data in STORED.get("users", {}).items()
    ]
    users_sorted = sorted(users, key=lambda x: x[1], reverse=True)[:10]
    user_lines = [
        f"**{i}.** <@{uid}> — {fmt_time(sec)} • {songs} songs"
        for i, (uid, sec, songs) in enumerate(users_sorted, 1)
    ]

    # Top Songs by **unique listeners**
    songs = []
    for vid, data in STORED.get("songs", {}).items():
        title = data.get("title", "Unknown")
        unique_users = len(data.get("users", []))
        songs.append((vid, title, unique_users))

    songs_sorted = sorted(songs, key=lambda x: x[2], reverse=True)[:10]
    song_lines = [
        f"**{i}.** {title} — {unique} unique listeners"
        for i, (_, title, unique) in enumerate(songs_sorted, 1)
    ]

    desc = "**Top Users (Time Listened):**\n" + ("\n".join(user_lines) or "_no data_")
    desc += "\n\n**Top Songs (Unique Listeners):**\n" + ("\n".join(song_lines) or "_no data_")
    await ctx.send(embed=ui("🏆 Leaderboard", desc))

# ========= Errors =========
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    try:
        await ctx.send(embed=ui("⚠️ Error", str(error)))
    except:
        pass

# ========= Run =========
bot.run(TOKEN)
