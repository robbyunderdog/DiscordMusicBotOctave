# Octave, Discord Music Bot
This is a Discord bot designed to search, download, and play music from Youtube. It utilizes ffmpeg and yt-dlp to search for, download, and play any query you want.

## Commands
The bot comes with six simple commands that are pretty self explanatory.

### /play {song request}
/play {song request} will search youtube for your request and pull the top result. Yt-dlp will download the audio for ffmpeg to process, and the bot will then play the music through voice.

### /stop
/stop will clear stop the current song that is playing, clear the server's queue, and disconnect from the channel it is apart of.

### /pause
/pause pauses the currently playing song.

### /resume
/resume resumes a previously paused song.

### /skip
/skip will stop the currently playing song, and play the next song in the queue.

### /queue
/queue shows the currently playing song, and any songs that are in the queue behind it.