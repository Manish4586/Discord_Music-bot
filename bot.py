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
from aiohttp import web
import subprocess
import asyncio

API_PORT = 8810

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
intents.members = True
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

def get_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            t = int(f.read().strip()) / 1000
            return t
    except:
        pass

    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True)
        return float(out.replace("temp=", "").replace("'C", "").strip())
    except:
        return None

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
        self.last_paused_track = None
        self.last_paused_position = 0

    async def ensure_voice(self, ctx):
        if not ctx.author.voice:
            raise commands.CommandError("Join a voice channel first.")
        ch = ctx.author.voice.channel
        if not self.voice:
            self.voice = await ch.connect(self_deaf=True)
        elif self.voice.channel != ch:
            await self.voice.move_to(ch)

    def progress(self):
        if not self.current or self.start_t is None:
           return 0.0

        if self.pause_t:
           played = self.pause_t - self.start_t - self.paused_accum
        else:
           played = time.time() - self.start_t - self.paused_accum

        if played is None or played < 0:
           played = 0.0

        if self.current.duration:
           played = min(played, self.current.duration)

           return float(played)

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

        self.start_t = None
        self.pause_t = None
        self.paused_accum = 0
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
    "format":"m4a/bestaudio/best",
    "quiet":True,
    "no_warnings":True,
    "noplaylist":True,
    "restrictfilenames":True,
    "cachedir": CACHE_DIR,
    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
    "postprocessors":[
        {
         "key": "FFmpegVideoRemuxer",
         "preferedformat": "m4a"
        }
    ]
}
YOUTUBE_URL_RE = re.compile("(youtube|youtu.be)")
search_results = {}

async def build_track(ctx, query, uid):
    loop = asyncio.get_event_loop()

    m = re.search(r"(v=|youtu.be/)([A-Za-z0-9_-]{6,20})", query)
    video_id = m.group(2) if m else None

    if video_id:
        file = os.path.join(DOWNLOAD_DIR, f"{video_id}.m4a")
        meta_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.json")

        if os.path.exists(file) and os.path.exists(meta_path):
            info = json.load(open(meta_path))

            title = info.get("title", "Unknown")
            url = info.get("webpage_url")
            duration = info.get("duration")
            thumb = info.get("thumbnail")

            return Track(url, title, video_id, file, thumb, uid, duration)

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
    file = os.path.join(DOWNLOAD_DIR, f"{vid}.m4a")
    thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    duration = info.get("duration")
    meta_path = os.path.join(DOWNLOAD_DIR, f"{vid}.json")

    if os.path.exists(file):
        s_meta = {
            "id": vid,
            "title": title,
            "duration": duration,
            "webpage_url": url,
            "thumbnail": thumb
        }
        json.dump(s_meta, open(meta_path, "w"))
        await msg.edit(embed=ui("🎶 Already Cached", f"**{title}** is ready."))
        await asyncio.sleep(2)
        await msg.delete()
        return Track(url, title, vid, file, thumb, uid, duration)

    await msg.edit(embed=ui("🎧 Processing...", f"**{title}**"))

    def dl():
        with YoutubeDL(YDL_OPTS) as y:
            y.download([url])

    await loop.run_in_executor(None, dl)

    s_meta = {
        "id": vid,
        "title": title,
        "duration": duration,
        "webpage_url": url,
        "thumbnail": thumb
    }
    json.dump(s_meta, open(meta_path, "w"))

    await msg.edit(embed=ui("✅ Ready", f"**{title}**"))
    await asyncio.sleep(2)
    await msg.delete()

    return Track(url, title, vid, file, thumb, uid, duration)

# ========= Panel Refresh & Playtime =========
@tasks.loop(seconds=3)
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
                    f"**{p.current.title}**\n\n"
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
        if f.endswith(".m4a"):
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
    await start_api()
    cleanup_cache.start()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="YouTube Music"))

@bot.listen("on_message")
async def warn_uppercase_commands(msg: discord.Message):
    if msg.author.bot:
        return

    if not msg.content.startswith("!"):
        return

    raw = msg.content.split()[0]
    cmd = raw[1:]
    if not cmd:
        return

    if cmd.lower() != cmd:
        await msg.channel.send(
            embed=ui("⚠️ Lowercase Commands Only", f"Use: `!{cmd.lower()}`"),
            delete_after=8
        )

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
!pause / !pa
!resume / !re

**Voice**
!leave / !d

**Stats**
!ping
!server
!stats
!leaderboard / !lb
"""
    await ctx.send(embed=ui("📘 Music Bot Commands", cmds))

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

@bot.command(name="leave")
async def leave(ctx):
    p = getp(ctx.guild)

    if p.current and p.voice:
        p.last_paused_track = p.current
        p.last_paused_position = p.progress()
    else:
        p.last_paused_track = None
        p.last_paused_position = 0

    if p.voice:
        await p.voice.disconnect(force=True)

    p.voice = None
    p.panel = None

    await ctx.send(embed=ui("👋 Left Voice"))
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="YouTube Music"))

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

@bot.command(name="pause", aliases=["pa"])
async def pause_cmd(ctx):
    p = getp(ctx.guild)

    if not p.voice or not p.voice.is_connected():
        return await ctx.send(embed=ui("⚠️ Not connected"))

    if not p.current:
        return await ctx.send(embed=ui("⚠️ Nothing is playing."))

    if p.voice.is_paused():
        return await ctx.send(embed=ui("⏸️ Already Paused"))

    p.voice.pause()
    p.pause_t = time.time()
    p.last_paused_track = p.current
    p.last_paused_position = p.progress()

    await ctx.send(embed=ui("⏸️ Paused", f"**{p.current.title}**"))

@bot.command(name="resume", aliases=["re"])
async def resume_cmd(ctx):
    p = getp(ctx.guild)

    if not p.voice or not p.voice.is_connected():
        if not ctx.author.voice:
            return await ctx.send(embed=ui("⚠️ Join a voice channel first."))

        p.voice = await ctx.author.voice.channel.connect(self_deaf=True)

        if p.last_paused_track:
            t = p.last_paused_track
            pos = p.last_paused_position

            source = discord.FFmpegPCMAudio(
                t.file,
                before_options=f"-ss {int(pos)}",
                options="-vn"
            )

            p.voice.play(source)
            p.start_t = time.time() - pos
            p.pause_t = None

            p.current = t
            return await ctx.send(embed=ui("▶️ Resumed", f"**{t.title}**"))

        return await ctx.send(embed=ui("⚠️ Nothing to resume."))

    if not p.voice.is_paused():
        return await ctx.send(embed=ui("⚠️ Not paused."))

    p.paused_accum += time.time() - p.pause_t
    p.pause_t = None
    p.voice.resume()

    await ctx.send(embed=ui("▶️ Resumed", f"**{p.current.title}**"))

# ========= Stats =========
@bot.command()
async def server(ctx):
    up = get_uptime_sec()
    total,used,free = get_mem()
    l1,l5,l15 = get_load()
    temp = get_temp()
    desc = (
        f"Uptime: **{fmt_time(up)}**\n"
        f"RAM: **{total/1e9:.2f} GB total**, **{used/1e9:.2f} GB used**, **{free/1e9:.2f} GB free**\n"
        f"Load avg: **{l1:.2f} {l5:.2f} {l15:.2f}**\n"
        f"CPU Temp: **{temp:.1f}°C**\n"
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

@bot.command()
async def ping(ctx):
    embed = ui("🏓 Pong!", "Running ping & speed test… please wait ⏳")
    msg = await ctx.send(embed=embed)

    await asyncio.sleep(0.5)

    try:
        proc = await asyncio.create_subprocess_exec(
            "speedtest-cli",
            "--simple",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_b, stderr_b = await proc.communicate()

        stdout = stdout_b.decode("utf-8", errors="ignore")
        stderr = stderr_b.decode("utf-8", errors="ignore")

        if stderr:
            raise Exception(stderr)

        ping_ms = "?"
        download = "?"
        upload = "?"

        for line in stdout.splitlines():
            if line.startswith("Ping:"):
                ping_ms = line.replace("Ping:", "").strip()
            elif line.startswith("Download:"):
                download = line.replace("Download:", "").strip()
            elif line.startswith("Upload:"):
                upload = line.replace("Upload:", "").strip()

        result_text = (
            f"🏓 **Ping:** `{ping_ms}`\n"
            f"⬇️ **Download:** `{download}`\n"
            f"⬆️ **Upload:** `{upload}`\n"
        )

        final_embed = ui(
            "📡 Server Network Speed",
            result_text,
            color=0x00FF00
        )

        await msg.edit(embed=final_embed)

    except Exception as e:
        err_embed = ui(
            "❌ Speedtest Failed",
            f"```{str(e)}```",
            color=0xFF0000
        )
        await msg.edit(embed=err_embed)

# ========= Errors =========
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    try:
        await ctx.send(embed=ui("⚠️ Error", str(error)))
    except:
        pass

async def api_nowplaying(request):
    try:
        if not players:
            return web.json_response({
            "status": "Nothing playing",
            "icon": "🎵"
          })

        p = list(players.values())[0]

        if not p.current:
            return web.json_response({
            "status": "Nothing playing",
            "icon": "🎵"
          })

        if p.voice and p.voice.is_paused():
            state = "paused"
            icon = "⏸️"
        elif p.voice and p.voice.is_playing():
            state = "playing"
            icon = "▶️"
        else:
            state = "Nothing playing"
            icon = "🎵"

        played = p.progress()
        total = p.current.duration or 0
        frac = played / total if total else 0

        return web.json_response({
            "status": state,
            "icon": icon,
            "title": p.current.title,
            "video_id": p.current.video_id,
            "thumbnail": p.current.thumb,
            "requested_by": p.current.requested_by_id,
            "played": played,
            "duration": total,
            "progress": frac
        })

    except Exception as e:
        return web.json_response({"error": str(e)})


async def api_status(request):
    up = get_uptime_sec()
    total, used, free = get_mem()
    l1, l5, l15 = get_load()
    temp = get_temp()

    return web.json_response({
        "uptime": up,
        "ram": {
            "total": total,
            "used": used,
            "free": free
        },
        "load": {
            "1m": l1,
            "5m": l5,
            "15m": l15
        },
        "cpu_temp": temp
    })


async def start_api():
    app = web.Application()
    app.router.add_get("/api/np", api_nowplaying)
    app.router.add_get("/api/stats", api_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    print(f"[API] Running on http://0.0.0.0:{API_PORT}")

# ========= Run =========
bot.run(TOKEN)
