import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio
from collections import deque
import datetime as dt

#loads env for token
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")


SONG_QUEUES = {}
CURRENT_SONG = {}

#search ytdlp parallel
async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))


#pulls info not parallel as to avoid interfering with playback
def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download = False)


#declares the intents
intents = discord.Intents.default()
intents.message_content = True


#sets up bot
bot = commands.Bot(command_prefix="!", intents = intents, activity=discord.Activity(type=discord.ActivityType.listening, name="a song"))


#sets an event for console when bot is online and ready
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} is online!")


#PLAY COMMAND
@bot.tree.command(name="play", description="Play a song or add it to the queue.")
@app_commands.describe(song_query="Search query")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()
    #gets channel of user who used command
    voice_channel = interaction.user.voice.channel

    #checks if user is in a channel
    if voice_channel is None:
        await interaction.followup.send("You must be in a voice channel.")
        return
    
    #gets channel of bot if any
    voice_client = interaction.guild.voice_client

    #checks if bot is in the correct channel, if not joins it
    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)

    #sets options for ydl like audio only and no playlists
    ydl_options = {
        "format": "bestaudio",
        "noplaylist": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }

    #finds first search and sets it to query alongside getting the results
    query = "ytsearch1: "+ song_query
    results = await search_ytdlp_async(query, ydl_options)
    tracks = results.get("entries", [])

    #checks if its empty
    if tracks is None:
        await interaction.followup.send("No results found.")
        return
    
    #grabs first track and its url and title
    first_track = tracks[0]
    audio_url = first_track["url"]
    title = first_track.get("title", "Untitled")

    guild_id = str(interaction.guild_id)
    if SONG_QUEUES.get(guild_id) is None:
        SONG_QUEUES[guild_id] = deque()

    SONG_QUEUES[guild_id].append((audio_url, title))

    #checks if song is playing, if true then adds to queue, otherwises says song is playing and calls function
    if voice_client.is_playing() or voice_client.is_paused():
        await interaction.followup.send(f"Added to queue: **{title}**")
    else:
        await interaction.followup.send(f"Now playing: **{title}**")
        await play_next_song(voice_client, guild_id, interaction.channel, first_song=True)


#QUEUE COMMAND, first is message based, second is embed based
@bot.tree.command(name="queue", description="Displays the current song queue.")
async def queue(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    embed = discord.Embed(title="Song Queue", color=discord.Color.blue())

    #currently playing song
    if guild_id in CURRENT_SONG:
        now_playing = CURRENT_SONG[guild_id][1]
        embed.add_field(name="Now Playing: ", value=f"**{now_playing}**", inline=False)
    else:
        embed.add_field(name="Now Playing: ", value="No song currently playing.", inline=False)

    #upcoming songs in queue
    if guild_id not in SONG_QUEUES or not SONG_QUEUES[guild_id]:
        embed.add_field(name="Upcoming Songs", value="The queue is empty.", inline=False)
    else:
        queue_list = ""
        for idx, (audio_url, title) in enumerate(SONG_QUEUES[guild_id], start=1):
            queue_list += f"**{idx}.** {title}\n"
        embed.add_field(name="Upcoming Songs", value=queue_list, inline=False)

    await interaction.response.send_message(embed=embed)


#SKIP COMMAND
@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and (interaction.guild.voice_client.is_playing() or interaction.guild.voice_client.is_paused()):
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("Skipped the current song.")
    else:
        await interaction.response.send_message("Not playing anything to skip.")


#PAUSE COMMAND
@bot.tree.command(name="pause", description="Pauses the currently playing song.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    #check if bot is in vc
    if voice_client is None:
        return await interaction.response.send_message("I am not in a voice channel.")
    
    #check if something is playing
    if not voice_client.is_playing():
        return await interaction.response.send_message("Nothing is currently playing.")
    
    #pauses playback
    voice_client.pause()
    await interaction.response.send_message("Song paused!")


#RESUME COMMAND
@bot.tree.command(name="resume", description="Resumes the currently paused song.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    #check if bot is in vc
    if voice_client is None:
        return await interaction.response.send_message("I am not in a voice channel.")
    
    #check if actually paused
    if voice_client.is_playing():
        return await interaction.response.send_message("I am not paused right now.")
    
    #resumes playback
    voice_client.resume()
    await interaction.response.send_message("Song resumed!")


#STOP COMMAND
@bot.tree.command(name="stop", description="Stops playback and clears the queue.")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    voice_client = interaction.guild.voice_client

    #check if bot is in vc
    if not voice_client or not voice_client.is_connected():
        return await interaction.followup.send("I am not connected to any voice channel.")
    
    #clear guilds queue
    guild_id_str = str(interaction.guild_id)
    if guild_id_str in SONG_QUEUES:
        SONG_QUEUES[guild_id_str].clear()
    
    #if something is playing or paused, stop it
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    #sends message confirming command
    await interaction.followup.send("Stopped playback and disconnected.")

    #disconnect from channel
    await voice_client.disconnect()


#function to call next song after each finished song
async def play_next_song(voice_client, guild_id, channel, first_song=False):
    if SONG_QUEUES[guild_id]:
        audio_url, title = SONG_QUEUES[guild_id].popleft()


        CURRENT_SONG[guild_id] = (audio_url, title)

        #ffmpeg options like trying to reconnect and bitrate and audio only
        ffmpeg_options = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn -af volume=0.05 -c:a libopus -b:a 96k"
        }

        #audio source
        source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options, executable="bin\\ffmpeg\\ffmpeg.exe")

        def after_play(error):
                if error:
                    print(f"Error playing {title}: {error}")
                asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel), bot.loop)

        #plays the audio
        voice_client.play(source, after=after_play)

        if not first_song:
            asyncio.create_task(channel.send(f"Now playing: **{title}**"))

    #otherwises disconnects and deques the guild id
    else:
        CURRENT_SONG.pop(guild_id, None)
        await voice_client.disconnect()
        SONG_QUEUES[guild_id] = deque()


bot.run(TOKEN)