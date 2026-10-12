"""Octave - a Discord bot that searches, queues, and plays YouTube audio in voice channels."""

import os
import asyncio
from collections import deque
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp

# Load secrets/config from a local .env file (never committed to git).
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Spoofed browser UA. YouTube is more likely to serve direct (non-HLS)
# audio formats, and less likely to rate-limit, when requests look like
# they're coming from a real browser rather than a bare HTTP client.
YTDLP_USER_AGENT = os.getenv(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
)

# Path/command used to invoke ffmpeg. Defaults to "ffmpeg" so it resolves
# via PATH (e.g. the apt-installed binary inside the Docker image); override
# with FFMPEG_PATH for a specific binary location.
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# Optional Netscape-format cookies.txt, used to avoid YouTube's
# "sign in to confirm you're not a bot" checks on some videos. Set
# YTDLP_COOKIES_FILE to an explicit path to override; otherwise, if a
# cookies.txt happens to sit next to this script, it's picked up
# automatically - this keeps .env machine-independent (no absolute paths
# that only make sense on one computer).
_default_cookies_file = Path(__file__).resolve().parent / "cookies.txt"
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE") or (
    str(_default_cookies_file) if _default_cookies_file.exists() else None
)

# Per-guild state: queue of (video_url, title) tuples waiting to play,
# and the (video_url, title) currently playing.
SONG_QUEUES = {}
CURRENT_SONG = {}


async def search_ytdlp_async(query, ydl_opts):
    """Run a blocking yt-dlp extraction in a thread so it doesn't block the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))


def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)


def build_search_ytdlp_options():
    """Options for resolving a /play argument (URL or search text) to a video."""
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Only used when the query isn't already a URL.
        "default_search": "ytsearch1",
        "extract_flat": False,
        "http_headers": {
            "User-Agent": YTDLP_USER_AGENT,
        },
    }
    if YTDLP_COOKIES_FILE:
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    return opts


def build_stream_ytdlp_options():
    """Options for re-resolving a known video URL to a direct, playable audio stream."""
    opts = {
        # Prefer a direct HTTPS stream over HLS; ffmpeg can't reliably read
        # YouTube's HLS segments without the exact headers yt-dlp used.
        "format": "bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": YTDLP_USER_AGENT,
        },
    }
    if YTDLP_COOKIES_FILE:
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    return opts


def pick_stream_url(info):
    """Pick a direct (non-HLS) audio stream URL out of a yt-dlp info dict, if one exists."""
    stream_url = info.get("url")
    if stream_url and "manifest.googlevideo.com" not in stream_url and ".m3u8" not in stream_url:
        return stream_url

    # Fall back to scanning all known formats for an audio-only, non-HLS one.
    for fmt in reversed(info.get("formats", [])):
        if (
            fmt.get("acodec") != "none"
            and fmt.get("vcodec") == "none"
            and fmt.get("url")
            and fmt.get("protocol") not in ("m3u8", "m3u8_native")
        ):
            return fmt["url"]

    return None


# Discord intents/bot setup.
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    activity=discord.Activity(type=discord.ActivityType.custom, name="custom", state="🎧 Listening to Music."),
)


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} is online!")


# PLAY COMMAND
@bot.tree.command(name="play", description="Play a song from a URL or search query, or add it to the queue.")
@app_commands.describe(song_query="A YouTube URL, or a search query")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    if interaction.user.voice is None or interaction.user.voice.channel is None:
        await interaction.followup.send("You must be in a voice channel.")
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    # Join the user's channel, or move there if already connected elsewhere.
    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)

    ydl_options = build_search_ytdlp_options()

    try:
        # yt-dlp detects whether song_query is a URL or plain search text itself,
        # so the same call handles both "/play <url>" and "/play <search terms>".
        results = await search_ytdlp_async(song_query, ydl_options)
    except yt_dlp.utils.DownloadError as e:
        await interaction.followup.send(f"yt-dlp failed: `{str(e)[:1500]}`")
        return
    except Exception as e:
        await interaction.followup.send(f"Unexpected error while searching: `{str(e)[:1500]}`")
        return

    # A search query returns {"entries": [...]}; a direct URL returns the
    # video's info dict itself with no "entries" key.
    if "entries" in results:
        tracks = results.get("entries") or []
        if not tracks:
            await interaction.followup.send("No results found.")
            return
        track = tracks[0]
    else:
        track = results

    video_url = track.get("webpage_url") or track.get("original_url") or track.get("url")
    title = track.get("title", "Untitled")

    if not video_url:
        await interaction.followup.send("Could not get a playable result URL for that search.")
        return

    guild_id = str(interaction.guild_id)
    if guild_id not in SONG_QUEUES:
        SONG_QUEUES[guild_id] = deque()

    SONG_QUEUES[guild_id].append((video_url, title))

    if voice_client.is_playing() or voice_client.is_paused():
        await interaction.followup.send(f"Added to queue: **{title}**")
    else:
        await interaction.followup.send(f"Now playing: **{title}**")
        await play_next_song(voice_client, guild_id, interaction.channel, first_song=True)


# QUEUE COMMAND
@bot.tree.command(name="queue", description="Displays the current song queue.")
async def queue(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    embed = discord.Embed(title="Song Queue", color=discord.Color.blue())

    if guild_id in CURRENT_SONG:
        now_playing = CURRENT_SONG[guild_id][1]
        embed.add_field(name="Now Playing:", value=f"**{now_playing}**", inline=False)
    else:
        embed.add_field(name="Now Playing:", value="No song currently playing.", inline=False)

    if guild_id not in SONG_QUEUES or not SONG_QUEUES[guild_id]:
        embed.add_field(name="Upcoming Songs", value="The queue is empty.", inline=False)
    else:
        queue_list = "\n".join(
            f"**{idx}.** {title}" for idx, (_, title) in enumerate(SONG_QUEUES[guild_id], start=1)
        )
        embed.add_field(name="Upcoming Songs", value=queue_list, inline=False)

    await interaction.response.send_message(embed=embed)


# SKIP COMMAND
@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        # Stopping playback triggers play_next_song via the after-play callback.
        voice_client.stop()
        await interaction.response.send_message("Skipped the current song.")
    else:
        await interaction.response.send_message("Not playing anything to skip.")


# PAUSE COMMAND
@bot.tree.command(name="pause", description="Pauses the currently playing song.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        return await interaction.response.send_message("I am not in a voice channel.")

    if not voice_client.is_playing():
        return await interaction.response.send_message("Nothing is currently playing.")

    voice_client.pause()
    await interaction.response.send_message("Song paused!")


# RESUME COMMAND
@bot.tree.command(name="resume", description="Resumes the currently paused song.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        return await interaction.response.send_message("I am not in a voice channel.")

    if not voice_client.is_paused():
        return await interaction.response.send_message("I am not paused right now.")

    voice_client.resume()
    await interaction.response.send_message("Song resumed!")


# STOP COMMAND
@bot.tree.command(name="stop", description="Stops playback and clears the queue.")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        return await interaction.followup.send("I am not connected to any voice channel.")

    guild_id = str(interaction.guild_id)
    if guild_id in SONG_QUEUES:
        SONG_QUEUES[guild_id].clear()

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    CURRENT_SONG.pop(guild_id, None)

    await interaction.followup.send("Stopped playback and disconnected.")
    await voice_client.disconnect()


async def play_next_song(voice_client, guild_id, channel, first_song=False):
    """Pop the next queued song and play it; called again via after_play when it ends."""
    if not SONG_QUEUES[guild_id]:
        CURRENT_SONG.pop(guild_id, None)
        await voice_client.disconnect()
        SONG_QUEUES[guild_id] = deque()
        return

    video_url, title = SONG_QUEUES[guild_id].popleft()
    CURRENT_SONG[guild_id] = (video_url, title)

    ydl_options = build_stream_ytdlp_options()

    try:
        # Re-resolve the video URL at play time (rather than reusing the URL
        # found by /play) since stream URLs are signed and expire quickly.
        info = await search_ytdlp_async(video_url, ydl_options)
        stream_url = pick_stream_url(info)

        if not stream_url:
            await channel.send(f"Could not get a playable stream for **{title}**.")
            await play_next_song(voice_client, guild_id, channel)
            return

        print(f"Playing title: {title}")
        print(f"Video URL: {video_url}")
        print(f"Stream URL: {stream_url[:200]}...")

    except Exception as e:
        await channel.send(f"Failed to load **{title}**: `{str(e)[:1500]}`")
        await play_next_song(voice_client, guild_id, channel)
        return

    source = discord.FFmpegPCMAudio(
        stream_url,
        before_options=f'-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -user_agent "{YTDLP_USER_AGENT}"',
        options="-vn -af volume=0.05",
        executable=FFMPEG_PATH,
    )

    def after_play(error):
        if error:
            print(f"Error playing {title}: {error}")
        # We're on ffmpeg's playback thread here, so hop back onto the bot's
        # event loop to advance the queue.
        future = asyncio.run_coroutine_threadsafe(
            play_next_song(voice_client, guild_id, channel),
            bot.loop,
        )
        try:
            future.result()
        except Exception as exc:
            print(f"Error advancing queue: {exc}")

    voice_client.play(source, after=after_play)

    if not first_song:
        asyncio.create_task(channel.send(f"Now playing: **{title}**"))


bot.run(TOKEN)
