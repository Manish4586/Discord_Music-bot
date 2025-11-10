 
# A Taliban LTD Music Bridge
A Discord Music Bot optimized for **Raspberry Pi 5**, designed to run **24/7** with:
- Fast MP3 caching (no repeated downloads)
- Zero-lag playback
- Accurate per-user listening statistics
- Unique-based top songs leaderboard
- Lightweight system performance monitoring
- Full queue, history, repeat, and now-playing UI panel

## 🎧 Wanna try the live Bot?
<p align="center">
  <a href="https://discord.com/oauth2/authorize?client_id=1437186522721026149&scope=bot&permissions=3147776">
    <img src="https://img.shields.io/badge/Invite%20Bot%20to%20Server-5865F2?style=for-the-badge&logo=discord&logoColor=white">
  </a>
</p>


## 1. Overview

This bot streams audio from **YouTube** using:
- `yt-dlp` (fetch and convert)
- `FFmpeg` (audio playback)
- `discord.py` Voice API

It stores downloaded songs as `.mp3` under: ~/discord-music/music

So if a song is requested again, it **plays instantly without downloading again**.

The bot tracks:
- Total songs user requested
- Total listening time
- Which unique users listened to each song

This allows a **unique listener based leaderboard** for most popular tracks.

---

## 2. Commands

### Playback
| Command | Alias | Description |
|--------|-------|-------------|
| `!play <song or link>` | `!p` | Play / queue a song |
| `!search <keywords>` | — | Show 5 results to pick from |
| `!np` | `!now`, `!nowplay` | Show Now Playing panel |
| `!queue` | — | Show queue list |

### Control
| Command | Alias | Description |
|--------|-------|-------------|
| `!next` | `!n` | Skip current song |
| `!prev` | — | Go back to previous song (if history exists) |
| `!stop` | `!s` | Stop playback + clear queue |
| `!repeat` | `!r` | Toggle repeat ONE |
| `!repeatall` | `!ra` | Toggle repeat ALL songs |

### Voice Control
| Command | Alias | Description |
|--------|-------|-------------|
| `!leave` | `!d` | Disconnect bot from voice |

### Stats / System
| Command | Description |
|--------|-------------|
| `!server` | Show uptime, RAM, load avg, total play time & songs |
| `!stats` or `!stats @user` | Show per-user listening stats |
| `!leaderboard` / `!lb` | Show top listeners & top songs (unique listener count) |

---

## 3. How Leaderboards Work

### Top Users  
Sorted by **listening time** (seconds → formatted as HH:MM:SS)

### Top Songs  
Sorted by **number of unique users who listened to the song**.

This means:
- If 1 user listens to a song 200 times → only **1 unique listener**
- If 12 different users each play once → counted as **12 unique listeners** (higher ranking)

This prevents spam and rewards *community interest*.

---

## 4. Installation (Raspberry Pi 5)
### Update system
```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y ffmpeg python3 python3-pip

pip3 install -U yt-dlp discord.py python-dotenv uvloop

mkdir -p ~/discord-music/music

cd ~/discord-music
```
---

## 5. Running the Bot (Manual Test)
```bash
cd ~/discord-music

python3 bot.py
```
---
## 6. Run Bot Automatically (systemd Service)

```bash
sudo nano /etc/systemd/system/discord-music.service
```
---
### systemd Service
```ini
[Unit]
Description=Discord Music Bot
After=network-online.target

[Service]

User=manish4586
WorkingDirectory=/home/manish ExecStart=/usr/bin/python3 /h discord-music/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
### Enable systemd Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-music.service
```
---

## 7. Credits

• ffmpeg

• yt-dlp

---