# TwitchCompat 

You ever want to watch your [oshi](https://twitch.tv/Amorrette) but your goddamn [fourth gen ipod touch](https://en.wikipedia.org/wiki/IPod_Touch_(4th_generation)) made in 2010 wont play twitch?

i had this exact same problem on a random monday so i made a simple, quite dumb solution in four hours. sadly my oshi stopped streaming, but that didnt stop me so here is-

***TWITCHCOMPAT (Twitch compatiblity layer)***

I can now watch the likes of mint fantome, vedal, UWOSLAB, and many more on my ipod because twitch's ui is so trash that my ipod cant load it. Remember folks, my ipod can load goddamn streams just fine on youtube, but my oshi is on twitch (damn you amazon and you using js and css and html5)

anyways now that i've pissed off jeff bezos lets get to the boring bit for us smart people

## features (so far, more later)

- [x] plays streams (video and audio)  
- [x] optional server-side transmux to fragmented MP4 (worst case, browser doesn't support js needed) 
- [ ] Windows 98 support
- [ ] Search functionality  
- [ ] youtube support  
- [ ] chat (and sign in on another device in case yours can't)  
 

## General usage
Run this in a terminal, Python required

```
python3 -m venv .venv # if already done ignore this
source .venv/bin/activate 
pip install -r requirements.txt
python3 main.py
```
And to use serverside transcode install ffmpeg

```
# Arch linux/Manjaro
sudo pacman -S ffmpeg
# Debian/Ubuntu
sudo apt-get install ffmpeg
```

## Backend usage

Default: have `&container=mp4` to request server-side container change (needs `ffmpeg`).

Example:
```
/stream/Amorrette?quality=720p30
```


Optional: raw MPEG-TS over `/stream/<channel>?quality=best` (needs mpegts.js on most browsers).  

Example:
```
/stream/Amorrette?quality=720p30&container=mp4
```

## credits

[mpegts for the javascript for older browsers](https://www.npmjs.com/package/mpegts.js/v/latest)  
[streamlink for twitter stream downloading on the fly](https://github.com/streamlink/streamlink)

## license (do whatever, seriously)
[Do What the Fuck You Want to Public License](https://www.wtfpl.net)