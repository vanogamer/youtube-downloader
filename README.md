YouTube Downloader Pro — MAX Quality

A modern Windows desktop YouTube downloader built with Python and Tkinter, powered by yt-dlp and FFmpeg.

Download videos and audio in the highest quality available, including 4K, 8K and HDR when provided by the source.

✨ Features

🎥 Single video downloads

📚 Playlist downloads

📺 Channel video downloads

🎬 YouTube Shorts downloads

📺 Channel + Shorts downloads

📋 Multiple video links at once

🔊 MP3 audio extraction

🖼️ MP3 cover art / thumbnail embedding

🏷️ Automatic audio metadata

⚡ Maximum available video quality

💎 4K / 8K / HDR support when available

🎞️ FFmpeg-based video/audio merging

🔄 Automatic download resume after network interruption

🌐 Smart cookie authentication

🍪 Support for exported cookies.txt

🌐 Browser cookie fallback

🧠 Automatic yt-dlp installation and updating

🛡️ YouTube PO Token provider support

🟢 JavaScript runtime support for modern YouTube extraction

📝 Detailed application logging

🌍 English and Georgian interface

💾 Persistent application settings

📂 Download history

🎨 Modern dark GUI

🖥️ Supported Platform

Currently designed primarily for:

Windows 10

Windows 11

📦 Requirements

Before running the application, install:

Python

Python 3.10+ is recommended.

Check your Python installation:

python --version

or:

py --version

FFmpeg

FFmpeg and FFprobe are required for merging separate video/audio streams and for audio conversion.

The application checks:

C:\ffmpeg\bin

first and can also fall back to FFmpeg available through the system PATH.

The expected files are:

C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe

Verify FFmpeg:

ffmpeg -version

Verify FFprobe:

ffprobe -version

🚀 Installation

Clone the repository:

git clone https://github.com/vanogamer/youtube-downloader

Enter the project directory:

cd screenshot-video

Run the application:

python index.py

or:

py index.py

The application automatically handles the required yt-dlp installation/update when necessary.

▶️ Usage

Start the application and select the download mode you need.

Download Video

Download a single YouTube video or playlist.

The downloader can select the maximum available quality or a specific resolution such as:

4320p — 8K
2160p — 4K
1440p — 2K
1080p — Full HD
720p — HD
480p
360p
240p
144p

When the selected source provides separate video and audio streams, FFmpeg is used to merge them.

🎵 Download Audio

Extract audio as MP3.

Supported quality options include:

320 kbps
256 kbps
192 kbps
128 kbps

The application can also embed:

YouTube thumbnail as cover art

Title

Artist

Uploader

Album

Date

Chapters

Other available metadata

📺 Channel Downloads

The application supports downloading videos from a YouTube channel.

Available modes include:

Channel videos

Channel Shorts

Channel + Shorts

Channel playlists

Date filtering can be used when working with channel content.

📋 Multiple Links

Multiple YouTube URLs can be processed in one session.

Each video can be downloaded using its own maximum available quality.

🔄 Network Resume

If the internet connection is interrupted during a download, the application can detect network-related errors and wait for the connection to return.

After connectivity is restored, the download can continue from the existing .part file instead of starting from zero.

The resume system supports a configurable waiting period and periodically checks the network connection.

🍪 Authentication & Cookies

The downloader includes multiple authentication methods.

Supported modes:

auto
browser
file
none

Cookie File

The application can automatically detect:

youtube-cookies.txt
cookies.txt

A valid Netscape-format cookie file is required.

Cookie files are validated before being passed to yt-dlp.

Browser Cookies

Browser authentication can also be detected automatically.

Supported browsers include:

Firefox

Microsoft Edge

Google Chrome

Chromium

Brave

Opera

Vivaldi

Whale

Browser priority can be customized through environment variables.

🧠 yt-dlp

The application uses yt-dlp as its extraction/download engine.

If yt-dlp is not installed, the application can install it automatically.

It can also upgrade yt-dlp when an older version is detected.

The application is designed to use recent yt-dlp functionality for compatibility with changes to YouTube.

⚙️ JavaScript Runtime

Modern YouTube extraction may require a JavaScript runtime.

The application can detect:

Node.js

Deno

Bun

QuickJS

A custom runtime can also be specified through environment variables.

🪙 PO Token Support

The application can automatically install and use:

bgutil-ytdlp-pot-provider

when a supported JavaScript runtime is available.

This provides an additional mechanism for handling YouTube requests that require PO tokens.

📝 Logging

The application has centralized logging.

Logs are stored next to the Python application:

logs/
└── app.log

The logger records:

Warnings

Errors

yt-dlp messages

Download errors

Background-thread exceptions

Tkinter callback exceptions

Unexpected application exceptions

Log files use rotation to prevent unlimited growth.

💾 Application Data

The application stores persistent data next to the Python script.

Typical folders include:

logs/
download_history/
settings/

Settings are stored in:

settings/app_settings.json

The selected language is persisted between launches.

🌍 Languages

The interface currently supports:

🇬🇧 English

🇬🇪 Georgian

English is the default language.

The language can be changed from the application interface.

🎨 Interface

The application uses a modern dark Tkinter interface with:

Dark theme

Cards

Modern buttons

Progress indicators

Quality selectors

Scrollable sections

Status information

📁 Project Structure

A typical installation may look like:

screenshot-video/
│
├── index.py
│
├── logs/
│   └── app.log
│
├── download_history/
│
├── settings/
│   └── app_settings.json
│
├── cookies.txt
└── youtube-cookies.txt

cookies.txt and youtube-cookies.txt are optional.

🔧 Environment Variables

Advanced users can customize the application with environment variables.

Examples:

YTDLP_FFMPEG_DIR
YTDLP_AUTO_UPGRADE
YTDLP_AUTH_MODE
YTDLP_COOKIES_FILE
YTDLP_BROWSER_COOKIES
YTDLP_BROWSER_ORDER
YTDLP_JS_RUNTIME
YTDLP_JS_RUNTIME_PATH
YTDLP_DISABLE_JS_RUNTIME
YTDLP_DISABLE_POT_PROVIDER
YTDLP_YT_PLAYER_CLIENTS
YTDLP_YT_PO_TOKEN
YTDLP_SOURCE_ADDRESS
YTDLP_USER_AGENT

Example:

$env:YTDLP_FFMPEG_DIR="C:\ffmpeg\bin"

⚠️ Troubleshooting

FFmpeg not found

Make sure these files exist:

C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe

Then restart the application.

yt-dlp installation problem

Run:

py -m pip install -U --pre yt-dlp[default]

Then restart the application.

Authentication / YouTube access problem

Try one of the following:

Update yt-dlp.

Use a fresh exported Netscape-format cookies.txt.

Make sure the browser session is logged in.

Check logs/app.log for the exact error.

4K / 8K download problem

High-resolution YouTube streams are often provided as separate video and audio streams.

Make sure both are available:

ffmpeg.exe
ffprobe.exe

🔐 Privacy

Cookie files can contain active authentication/session information.

Never upload your personal cookies.txt or browser cookie data to GitHub.

Add them to .gitignore:

cookies.txt
youtube-cookies.txt
logs/
download_history/
settings/
*.part

⚖️ Disclaimer

This project is intended for downloading content that you are authorized to download or that is otherwise permitted by the applicable service terms and copyright laws.

Users are responsible for complying with the laws and terms applicable to the content they download.

📜 License

Choose and add a license appropriate for your project before publishing.

For example, if you want to use the MIT License, add a LICENSE file containing the official MIT License text.

⭐ Contributing

Contributions, bug reports and improvements are welcome.

To contribute:

git fork https://github.com/vanogamer/screenshot-video

Create a branch:

git checkout -b feature/my-feature

Commit your changes:

git add .
git commit -m "Add my feature"

Push the branch:

git push origin feature/my-feature

Then open a Pull Request.

👤 Author

vanogamer

GitHub:

https://github.com/vanogamer

YouTube Downloader Pro — MAX Quality

Built with:

Python · Tkinter · yt-dlp · FFmpeg
