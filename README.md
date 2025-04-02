# OctaveMusic
This is a discord bot utilizing ffmpeg and yt-dlp to download, processed, and played over voice for users to listen too. It has multiple commands to be utilized, such as /play, /pause, /resume, and many others to create as streamlined a process as possible.

## Bot Commands
There are 6 commands currently usable to control the bot.
### /play
Usage is /play "song_query", where song_query is then used to search youtube and pull the first result, then joining the users channel and playing the track.
### /stop
Usage is /stop, which clears the servers queue, stops playing the current track, then disconnects from the channel.
### /pause
Usage is /pause, which pauses the currently playing track.
### /resume
Usage is /resume, which resumes the previously paused track.
### /skip
Usage is /skip, which moves onto the next track in the queue, if there is none after, the bot disconnects from the channel.
### /queue
Usage is /queue, which lists the currently playing track and any others that are in the queue.
