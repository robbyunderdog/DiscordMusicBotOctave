# Octave, Discord Music Bot
This is a Discord bot designed to search, download, and play music from Youtube. It utilizes ffmpeg and yt-dlp to search for, download, and play any query you want.

## Commands
The bot comes with seven simple commands that are pretty self explanatory.

### /play {song request}
/play {song request} will search youtube for your request and pull the top result. Yt-dlp will download the audio for ffmpeg to process, and the bot will then play the music through voice.

You can also pass a YouTube video URL, or a YouTube playlist URL to queue the whole playlist (up to 100 songs). A video link that happens to include `&list=` plays just that video.

### /stop
/stop will clear stop the current song that is playing, clear the server's queue, and disconnect from the channel it is apart of.

### /pause
/pause pauses the currently playing song.

### /resume
/resume resumes a previously paused song.

### /skip
/skip will stop the currently playing song, and play the next song in the queue.

### /queue
/queue shows the currently playing song, and any songs that are in the queue behind it, along with each song's length and the total queue time. Long queues are split into pages you can flip through with the ◀️ ▶️ buttons.

### /loop {off | song | queue}
/loop song repeats the current song, /loop queue repeats the whole queue, and /loop off turns looping back off. /skip still moves on to the next song while looping.

### Auto-leave
If everyone else leaves the voice channel, the bot waits 15 seconds, then stops playback, clears the queue, and leaves.

## Setup

Create a `.env` file in the project root (never commit this file):

```
DISCORD_TOKEN=your-discord-bot-token
```

If you hit bot-check/age-restricted errors, export a YouTube `cookies.txt` (Netscape format) and drop it next to `Octave.py` — it's picked up automatically, no `.env` entry needed. To use a cookies file somewhere else instead, set `YTDLP_COOKIES_FILE=/path/to/cookies.txt` in `.env`.

## Running with Docker

```
docker compose up -d --build
```

This builds the image (Python 3.12 + ffmpeg), starts the bot, and mounts `cookies.txt` into the container if present. View logs with `docker compose logs -f`, stop with `docker compose down`.

## Running locally without Docker

```
pip install -r requirements.txt
python Octave.py
```

ffmpeg must be installed and on your PATH, or point to it via the `FFMPEG_PATH` env var.

Note: yt-dlp needs to stay up to date (`pip install -U yt-dlp`) since YouTube regularly changes its site in ways that break older extractor versions.