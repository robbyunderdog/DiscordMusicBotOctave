"""Octave - a Discord bot that searches, queues, and plays YouTube audio in voice channels."""

import os
import sys
import asyncio
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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

# Playback gain applied by ffmpeg, where 1.0 is the track's original level.
# Override with OCTAVE_VOLUME to make the bot louder or quieter.
VOLUME = float(os.getenv("OCTAVE_VOLUME", "0.5"))

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

# Seconds to wait before leaving a voice channel that everyone else has left.
AUTO_LEAVE_SECONDS = 15

# Most songs queued from one playlist link, so a huge playlist can't flood the queue.
MAX_PLAYLIST_SONGS = 100

# Upcoming songs shown per page of the /queue embed.
QUEUE_PAGE_SIZE = 10

# Titles YouTube gives unavailable entries in a playlist; these can never play.
UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]"}


@dataclass
class Song:
    url: str
    title: str
    duration: Optional[int]  # seconds; None when unknown (e.g. livestreams)


async def search_ytdlp_async(query, ydl_opts):
    """Run a blocking yt-dlp extraction in a thread so it doesn't block the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))


def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)


def build_search_ytdlp_options():
    """Options for resolving a /play argument (URL, playlist URL, or search text) to songs."""
    opts = {
        "format": "bestaudio/best",
        # A watch URL that also carries &list= plays just that video; only a
        # dedicated playlist URL queues the whole playlist.
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Only used when the query isn't already a URL.
        "default_search": "ytsearch1",
        # Only read title/URL/duration for playlist and search entries;
        # the actual stream is resolved later, when each song starts.
        "extract_flat": "in_playlist",
        "playlistend": MAX_PLAYLIST_SONGS,
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


def song_from_entry(entry):
    """Build a Song from a yt-dlp video or flat playlist/search entry, or None if it's unplayable."""
    url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    title = entry.get("title") or "Untitled"
    if not url or title in UNAVAILABLE_TITLES:
        return None
    duration = entry.get("duration")
    return Song(url, title, int(duration) if duration else None)


def format_duration(seconds):
    """Format seconds as m:ss or h:mm:ss."""
    if seconds is None:
        return "--:--"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes}:{secs:02}"


def shorten(text, limit=70):
    return text if len(text) <= limit else text[: limit - 1] + "…"


class FFmpegLogFilter:
    """Passes ffmpeg's stderr through to ours, minus known-harmless noise.

    YouTube's Opus audio ends in a malformed final packet, so ffmpeg logs
    "Error parsing Opus packet header" at the end of nearly every song even
    though nothing audible is lost.
    """

    IGNORED = ("Error parsing Opus packet header",)

    def __init__(self):
        self._buffer = b""

    def write(self, data):
        # discord.py hands over raw chunks that can end mid-line, so only
        # inspect complete lines and hold the remainder for the next chunk.
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            text = line.decode(errors="replace").rstrip("\r")
            if text and not any(ignored in text for ignored in self.IGNORED):
                print(text, file=sys.stderr)


class GuildPlayer:
    """All music state and playback logic for one guild (server)."""

    def __init__(self, guild):
        self.guild = guild
        self.queue = deque()      # Songs waiting to play
        self.current = None       # Song playing (or loading) right now
        self.text_channel = None  # where "Now playing" and similar messages go
        self.loop_mode = "off"    # "off", "song", or "queue"
        self._skip_requested = False
        self._leave_task = None
        # Bumped by reset(). Playback callbacks started before a reset see a
        # different session and bail out instead of advancing the queue -
        # this is what keeps /stop from triggering a second disconnect.
        self._session = 0

    @property
    def voice_client(self):
        return self.guild.voice_client

    def is_busy(self):
        return self.current is not None

    def reset(self):
        """Forget the queue, current song, and loop mode, without touching the voice connection."""
        self._session += 1
        self.queue.clear()
        self.current = None
        self.loop_mode = "off"
        self._skip_requested = False
        self.cancel_auto_leave()

    async def stop(self):
        """Clear everything and leave voice."""
        self.reset()
        voice_client = self.voice_client
        if voice_client:
            voice_client.stop()
            await voice_client.disconnect()

    def skip(self):
        """Stop the current song so the next one starts. Returns False if nothing was playing."""
        voice_client = self.voice_client
        if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
            return False
        # Song-loop mode should move on rather than replay the skipped song.
        self._skip_requested = True
        # Stopping playback triggers play_next via the after-play callback.
        voice_client.stop()
        return True

    async def play_next(self, announce=True):
        """Start the next song, applying loop mode and skipping songs that fail to load."""
        session = self._session

        finished, self.current = self.current, None
        if finished is not None:
            if self.loop_mode == "song" and not self._skip_requested:
                self.queue.appendleft(finished)
            elif self.loop_mode == "queue":
                self.queue.append(finished)
        self._skip_requested = False

        # A loop rather than recursion, so a long run of dead songs (e.g.
        # removed videos in a playlist) can't pile up nested calls.
        while self.queue:
            if self._session != session:
                return

            song = self.queue.popleft()
            self.current = song

            try:
                # Re-resolve the video URL at play time (rather than reusing the
                # URL found by /play) since stream URLs are signed and expire quickly.
                info = await search_ytdlp_async(song.url, build_stream_ytdlp_options())
                stream_url = pick_stream_url(info)
                error = None if stream_url else "no playable stream found"
            except Exception as e:
                stream_url, error = None, str(e)

            if self._session != session:
                return  # /stop or a disconnect happened while this song was loading

            if not stream_url:
                # Dropped for good, even in loop mode, so a dead song can't repeat forever.
                await self._send(f"Could not load **{song.title}**, skipping: `{error[:1500]}`")
                continue

            voice_client = self.voice_client
            if voice_client is None:
                self.current = None
                return

            print(f"Playing title: {song.title}")
            print(f"Video URL: {song.url}")
            print(f"Stream URL: {stream_url[:200]}...")

            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options=f'-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -user_agent "{YTDLP_USER_AGENT}"',
                options=f"-vn -af volume={VOLUME}",
                executable=FFMPEG_PATH,
                stderr=FFmpegLogFilter(),
            )
            voice_client.play(source, after=lambda error: self._after_play(error, song, session))

            # A looping song isn't re-announced on every repeat.
            if announce and song is not finished:
                await self._send(f"Now playing: **{song.title}** `{format_duration(song.duration)}`")
            return

        # Queue ran out. Leaving voice fires on_voice_state_update, which resets this player.
        self.current = None
        if self.voice_client:
            await self.voice_client.disconnect()

    def _after_play(self, error, song, session):
        if error:
            print(f"Error playing {song.title}: {error}")
        if session != self._session:
            return  # stopped by /stop or a disconnect, so there's nothing to advance
        # We're on ffmpeg's playback thread here, so hop back onto the bot's
        # event loop to advance the queue.
        future = asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop)
        try:
            future.result()
        except Exception as exc:
            print(f"Error advancing queue: {exc}")

    def update_auto_leave(self):
        """Start the auto-leave countdown if the bot is alone in its channel, or cancel it if not."""
        voice_client = self.voice_client
        alone = (
            voice_client is not None
            and voice_client.channel is not None
            and not any(not member.bot for member in voice_client.channel.members)
        )
        if alone and self._leave_task is None:
            self._leave_task = asyncio.create_task(self._leave_after_delay())
        elif not alone:
            self.cancel_auto_leave()

    def cancel_auto_leave(self):
        if self._leave_task is not None:
            self._leave_task.cancel()
            self._leave_task = None

    async def _leave_after_delay(self):
        await asyncio.sleep(AUTO_LEAVE_SECONDS)
        self._leave_task = None
        await self._send("Everyone left the voice channel, so I stopped playback and left too.")
        await self.stop()

    async def _send(self, message):
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(message)
        except discord.HTTPException as e:
            print(f"Failed to send message: {e}")


# One GuildPlayer per guild id, created on first use.
PLAYERS = {}


def get_player(guild):
    player = PLAYERS.get(guild.id)
    if player is None:
        player = PLAYERS[guild.id] = GuildPlayer(guild)
    return player


def queue_page_count(player):
    return max(1, -(-len(player.queue) // QUEUE_PAGE_SIZE))


def build_queue_embed(player, page=0):
    embed = discord.Embed(title="Song Queue", color=discord.Color.blue())

    if player.current:
        now_playing = f"**{player.current.title}** `{format_duration(player.current.duration)}`"
    else:
        now_playing = "No song currently playing."
    embed.add_field(name="Now Playing:", value=now_playing, inline=False)

    songs = list(player.queue)
    if not songs:
        embed.add_field(name="Upcoming Songs", value="The queue is empty.", inline=False)
    else:
        start = page * QUEUE_PAGE_SIZE
        queue_list = "\n".join(
            f"**{idx}.** {shorten(song.title)} `{format_duration(song.duration)}`"
            for idx, song in enumerate(songs[start : start + QUEUE_PAGE_SIZE], start=start + 1)
        )
        embed.add_field(name="Upcoming Songs", value=queue_list, inline=False)

    total = format_duration(sum(song.duration or 0 for song in songs))
    footer = f"{len(songs)} upcoming · {total} total · Loop: {player.loop_mode}"
    pages = queue_page_count(player)
    if pages > 1:
        footer += f" · Page {page + 1}/{pages}"
    embed.set_footer(text=footer)
    return embed


class QueueView(discord.ui.View):
    """Previous/next buttons for paging through a long /queue embed."""

    def __init__(self, player):
        super().__init__(timeout=120)
        self.player = player
        self.page = 0
        self.message = None

    def build_embed(self):
        # Re-read the live queue on every render, since songs may have
        # finished or been added since the last button press.
        pages = queue_page_count(self.player)
        self.page = max(0, min(self.page, pages - 1))
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= pages - 1
        return build_queue_embed(self.player, self.page)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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


@bot.event
async def on_voice_state_update(member, before, after):
    player = PLAYERS.get(member.guild.id)
    if player is None:
        return

    if member.id == bot.user.id and after.channel is None:
        # The bot left voice - via /stop, the queue ending, auto-leave, or
        # someone disconnecting it manually - so drop any leftover state.
        player.reset()
        return

    player.update_auto_leave()


# PLAY COMMAND
@bot.tree.command(name="play", description="Play a song or YouTube playlist from a URL or search query, or add it to the queue.")
@app_commands.describe(song_query="A YouTube video or playlist URL, or a search query")
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

    try:
        # yt-dlp detects whether song_query is a URL or plain search text itself,
        # so the same call handles "/play <url>", "/play <playlist url>", and
        # "/play <search terms>".
        results = await search_ytdlp_async(song_query, build_search_ytdlp_options())
    except yt_dlp.utils.DownloadError as e:
        await interaction.followup.send(f"yt-dlp failed: `{str(e)[:1500]}`")
        return
    except Exception as e:
        await interaction.followup.send(f"Unexpected error while searching: `{str(e)[:1500]}`")
        return

    # Search text and playlist URLs both return {"entries": [...]}; a direct
    # video URL returns the video's info dict itself with no "entries" key.
    is_playlist = "entries" in results and results.get("extractor_key") != "YoutubeSearch"
    if "entries" not in results:
        entries = [results]
    elif is_playlist:
        entries = results.get("entries") or []
    else:
        entries = (results.get("entries") or [])[:1]

    songs = [song for song in map(song_from_entry, entries) if song]
    if not songs:
        await interaction.followup.send("No results found.")
        return

    player = get_player(interaction.guild)
    player.text_channel = interaction.channel
    was_busy = player.is_busy()
    player.queue.extend(songs)

    if is_playlist:
        message = f"Added **{len(songs)}** songs from **{results.get('title') or 'playlist'}** to the queue."
        if len(entries) >= MAX_PLAYLIST_SONGS:
            message += f" (Playlists are capped at {MAX_PLAYLIST_SONGS} songs.)"
    elif was_busy:
        message = f"Added to queue: **{songs[0].title}** `{format_duration(songs[0].duration)}`"
    else:
        message = f"Now playing: **{songs[0].title}** `{format_duration(songs[0].duration)}`"
    await interaction.followup.send(message)

    if not was_busy:
        # A single song was already announced above; a playlist's first song wasn't.
        await player.play_next(announce=is_playlist)


# QUEUE COMMAND
@bot.tree.command(name="queue", description="Displays the current song queue.")
async def queue(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if queue_page_count(player) > 1:
        view = QueueView(player)
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()
    else:
        await interaction.response.send_message(embed=build_queue_embed(player))


# LOOP COMMAND
@bot.tree.command(name="loop", description="Repeat the current song or the whole queue.")
@app_commands.describe(mode="Off, Song (repeat the current song), or Queue (repeat the whole queue)")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Song", value="song"),
        app_commands.Choice(name="Queue", value="queue"),
    ]
)
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    player = get_player(interaction.guild)
    player.loop_mode = mode.value
    messages = {
        "off": "Looping is off.",
        "song": "Looping the current song.",
        "queue": "Looping the whole queue.",
    }
    await interaction.response.send_message(messages[mode.value])


# SKIP COMMAND
@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    if get_player(interaction.guild).skip():
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

    await interaction.followup.send("Stopped playback and disconnected.")
    await get_player(interaction.guild).stop()


bot.run(TOKEN)
