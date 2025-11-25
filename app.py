import os
import re
import requests
import pandas as pd
import streamlit as st
from pathlib import Path
import base64
import zipfile
from io import BytesIO
import subprocess
import tempfile
import shutil
import json
import time
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB
from mutagen.mp3 import MP3
from urllib.parse import urlparse, parse_qs

# ---------------- CONFIG ----------------
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

st.set_page_config(page_title="Music Playlist Downloader", layout="wide")
st.title("🎵 Spotify & YouTube Music Downloader")

st.markdown("""
Download your favorite playlists or individual songs with **album covers**:
1. Paste your **Spotify** OR **YouTube Music** playlist/track URL
2. Click **Process & Download** - works directly!

**Supported URLs:**
- Spotify: `https://open.spotify.com/playlist/...` or `https://open.spotify.com/track/...`
- YouTube Music: `https://music.youtube.com/playlist?list=...` or `https://music.youtube.com/watch?v=...`
- YouTube: `https://www.youtube.com/playlist?list=...` or `https://www.youtube.com/watch?v=...`
""")


# Check installations
def check_ytdlp():
    try:
        result = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except:
        return False


ytdlp_installed = check_ytdlp()

if not ytdlp_installed:
    st.error("⚠️ yt-dlp is not installed! Please run: `pip install yt-dlp mutagen`")
    st.code("pip install yt-dlp mutagen")
    st.stop()

# Show yt-dlp version
try:
    result = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
    ytdlp_version = result.stdout.strip()
    with st.expander("🔧 System Info"):
        st.text(f"yt-dlp version: {ytdlp_version}")
        
        # Test yt-dlp with a simple command
        if st.button("Test yt-dlp Connection"):
            test_cmd = ['yt-dlp', '--print', 'title', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ']
            test_result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=15)
            if test_result.returncode == 0:
                st.success("✅ yt-dlp is working!")
            else:
                st.error(f"❌ yt-dlp test failed: {test_result.stderr}")
except:
    pass

# ---------------- UI inputs ----------------
playlist_url = st.text_input(
    "Spotify / YouTube Music / YouTube URL",
    placeholder="Paste playlist or track URL here..."
)

with st.expander("⚙️ Download Settings"):
    audio_format = st.selectbox(
        "Audio Format",
        ["m4a", "mp3"],
        help="m4a: Faster. mp3: More compatible (needs FFmpeg)."
    )
    audio_quality = st.selectbox("Quality", ["best", "192", "128"], index=0)
    add_metadata = st.checkbox("Add metadata & covers (Spotify only)", value=True)

download_btn = st.button("🚀 Process & Download", use_container_width=True, type="primary")

log_area = st.empty()
progress_bar = st.progress(0)
status_text = st.empty()


# ---------------- Helper Functions ----------------
def append_log(msg, log_container):
    """Append message to log."""
    log_container.text_area("Download Log", msg, height=300)


def detect_platform(url):
    """Detect platform from URL."""
    url = url.strip().lower()
    
    if 'spotify.com' in url:
        if 'playlist' in url:
            return 'spotify_playlist'
        elif 'track' in url:
            return 'spotify_track'
    
    if any(x in url for x in ['youtube.com', 'youtu.be', 'music.youtube.com']):
        if 'playlist' in url or 'list=' in url:
            return 'youtube_playlist'
        elif 'watch?v=' in url or 'youtu.be/' in url:
            return 'youtube_track'
    
    return None


def extract_youtube_id(url):
    """Extract YouTube video or playlist ID."""
    # Video ID
    patterns = [
        r'(?:v=|/)([0-9A-Za-z_-]{11}).*',
        r'youtu\.be/([0-9A-Za-z_-]{11})',
        r'embed/([0-9A-Za-z_-]{11})'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return 'video', match.group(1)
    
    # Playlist ID
    match = re.search(r'[?&]list=([^&]+)', url)
    if match:
        return 'playlist', match.group(1)
    
    return None, None


def clean_filename(text):
    """Clean filename."""
    text = re.sub(r'[<>:"/\\|?*]', '', str(text))
    text = text.strip()
    return text[:200]  # Limit length


# ---------------- Spotify Functions ----------------
def get_spotify_token(client_id, client_secret):
    """Get Spotify token."""
    auth_url = "https://accounts.spotify.com/api/token"
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    response = requests.post(auth_url, headers=headers, data=data)
    response.raise_for_status()
    return response.json()["access_token"]


def get_spotify_tracks(url, token):
    """Get tracks from Spotify URL."""
    tracks = []
    
    # Extract ID
    if 'playlist' in url:
        match = re.search(r'playlist/([a-zA-Z0-9]+)', url)
        if match:
            playlist_id = match.group(1)
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(f"https://api.spotify.com/v1/playlists/{playlist_id}", headers=headers)
            response.raise_for_status()
            data = response.json()
            
            for item in data.get("tracks", {}).get("items", []):
                track = item.get("track")
                if track:
                    album = track.get("album", {})
                    tracks.append({
                        "name": track.get("name"),
                        "artist": ", ".join([a["name"] for a in track.get("artists", [])]),
                        "album": album.get("name", ""),
                        "cover_url": album.get("images", [{}])[0].get("url") if album.get("images") else None,
                        "search_query": f"{', '.join([a['name'] for a in track.get('artists', [])])} {track.get('name')}"
                    })
    
    elif 'track' in url:
        match = re.search(r'track/([a-zA-Z0-9]+)', url)
        if match:
            track_id = match.group(1)
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(f"https://api.spotify.com/v1/tracks/{track_id}", headers=headers)
            response.raise_for_status()
            track = response.json()
            
            album = track.get("album", {})
            tracks.append({
                "name": track.get("name"),
                "artist": ", ".join([a["name"] for a in track.get("artists", [])]),
                "album": album.get("name", ""),
                "cover_url": album.get("images", [{}])[0].get("url") if album.get("images") else None,
                "search_query": f"{', '.join([a['name'] for a in track.get('artists', [])])} {track.get('name')}"
            })
    
    return tracks


# ---------------- Download Functions ----------------
def download_single_youtube_video(url, output_dir, audio_format="m4a", quality="best"):
    """Download single YouTube video directly using various methods."""
    
    # Generate filename from URL
    id_type, video_id = extract_youtube_id(url)
    if not video_id:
        return None, "Invalid URL"
    
    safe_filename = clean_filename(f"youtube_{video_id}")
    output_template = os.path.join(output_dir, f"{safe_filename}.%(ext)s")
    
    # Method 1: Direct download with minimal options (fastest)
    methods = [
        {
            "name": "Direct (Android client)",
            "cmd": [
                'yt-dlp',
                '--format', 'bestaudio' if audio_format == 'mp3' else 'bestaudio[ext=m4a]/bestaudio',
                '--output', output_template,
                '--no-playlist',
                '--quiet',
                '--no-warnings',
                '--extractor-args', 'youtube:player_client=android',
                '--user-agent', 'com.google.android.youtube/17.31.35',
            ]
        },
        {
            "name": "Direct (iOS client)",
            "cmd": [
                'yt-dlp',
                '--format', 'bestaudio' if audio_format == 'mp3' else 'bestaudio[ext=m4a]/bestaudio',
                '--output', output_template,
                '--no-playlist',
                '--quiet',
                '--no-warnings',
                '--extractor-args', 'youtube:player_client=ios',
                '--user-agent', 'com.google.ios.youtube/17.33.2',
            ]
        },
        {
            "name": "Direct (Web client)",
            "cmd": [
                'yt-dlp',
                '--format', 'bestaudio' if audio_format == 'mp3' else 'bestaudio[ext=m4a]/bestaudio',
                '--output', output_template,
                '--no-playlist',
                '--quiet',
                '--no-warnings',
                '--extractor-args', 'youtube:player_client=web',
            ]
        },
        {
            "name": "With age bypass",
            "cmd": [
                'yt-dlp',
                '--format', 'bestaudio' if audio_format == 'mp3' else 'bestaudio[ext=m4a]/bestaudio',
                '--output', output_template,
                '--no-playlist',
                '--quiet',
                '--no-warnings',
                '--age-limit', '21',
                '--extractor-args', 'youtube:player_client=android',
            ]
        }
    ]
    
    for method in methods:
        try:
            cmd = method["cmd"].copy()
            
            # Add audio processing for mp3
            if audio_format == "mp3":
                cmd.extend([
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', quality if quality != "best" else "0"
                ])
            
            cmd.append(url)
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            # Check if file was created
            for ext in ['m4a', 'mp3', 'webm', 'opus']:
                file_path = os.path.join(output_dir, f"{safe_filename}.{ext}")
                if os.path.exists(file_path) and os.path.getsize(file_path) > 50000:
                    return file_path, method["name"]
            
        except Exception as e:
            continue
    
    return None, "All methods failed"


def download_youtube_playlist_batch(url, output_dir, audio_format="m4a", quality="best", max_videos=50):
    """Download entire YouTube playlist in one command."""
    
    id_type, playlist_id = extract_youtube_id(url)
    if not playlist_id:
        return []
    
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")
    
    try:
        cmd = [
            'yt-dlp',
            '--format', 'bestaudio' if audio_format == 'mp3' else 'bestaudio[ext=m4a]/bestaudio',
            '--output', output_template,
            '--quiet',
            '--no-warnings',
            '--ignore-errors',
            '--playlist-end', str(max_videos),
            '--extractor-args', 'youtube:player_client=android,ios,web',
            '--user-agent', 'com.google.android.youtube/17.31.35',
        ]
        
        # Add audio processing
        if audio_format == "mp3":
            cmd.extend([
                '--extract-audio',
                '--audio-format', 'mp3',
                '--audio-quality', quality if quality != "best" else "0"
            ])
        
        cmd.append(url)
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        
        # Find downloaded files
        downloaded_files = []
        for ext in ['m4a', 'mp3', 'webm', 'opus']:
            downloaded_files.extend(list(Path(output_dir).glob(f"*.{ext}")))
        
        return downloaded_files
        
    except Exception as e:
        return []


def download_from_search(search_query, output_dir, audio_format="m4a", quality="best"):
    """Download by searching YouTube."""
    
    safe_filename = clean_filename(search_query)
    output_template = os.path.join(output_dir, f"{safe_filename}.%(ext)s")
    
    try:
        cmd = [
            'yt-dlp',
            '--format', 'bestaudio' if audio_format == 'mp3' else 'bestaudio[ext=m4a]/bestaudio',
            '--output', output_template,
            '--no-playlist',
            '--quiet',
            '--no-warnings',
            '--default-search', 'ytsearch1',
            '--extractor-args', 'youtube:player_client=android',
        ]
        
        if audio_format == "mp3":
            cmd.extend([
                '--extract-audio',
                '--audio-format', 'mp3',
                '--audio-quality', quality if quality != "best" else "0"
            ])
        
        cmd.append(f"ytsearch1:{search_query} official audio")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        
        # Find file
        for ext in ['m4a', 'mp3', 'webm', 'opus']:
            file_path = os.path.join(output_dir, f"{safe_filename}.{ext}")
            if os.path.exists(file_path):
                return file_path
        
        return None
        
    except Exception as e:
        return None


def add_metadata_to_file(file_path, track_info):
    """Add metadata to audio file."""
    try:
        if file_path.endswith('.m4a'):
            audio = MP4(file_path)
            audio["\xa9nam"] = track_info.get("name", "")
            audio["\xa9ART"] = track_info.get("artist", "")
            audio["\xa9alb"] = track_info.get("album", "")
            
            if track_info.get("cover_url"):
                try:
                    response = requests.get(track_info["cover_url"], timeout=10)
                    if response.status_code == 200:
                        audio["covr"] = [MP4Cover(response.content, imageformat=MP4Cover.FORMAT_JPEG)]
                except:
                    pass
            
            audio.save()
            
        elif file_path.endswith('.mp3'):
            audio = MP3(file_path, ID3=ID3)
            
            try:
                audio.add_tags()
            except:
                pass
            
            audio.tags.add(TIT2(encoding=3, text=track_info.get("name", "")))
            audio.tags.add(TPE1(encoding=3, text=track_info.get("artist", "")))
            audio.tags.add(TALB(encoding=3, text=track_info.get("album", "")))
            
            if track_info.get("cover_url"):
                try:
                    response = requests.get(track_info["cover_url"], timeout=10)
                    if response.status_code == 200:
                        audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=response.content))
                except:
                    pass
            
            audio.save()
        
        return True
    except:
        return False


# ---------------- Main Download Logic ----------------
if download_btn:
    if not playlist_url.strip():
        st.error("Please enter a URL")
        st.stop()
    
    platform = detect_platform(playlist_url)
    if not platform:
        st.error("❌ Unsupported URL")
        st.stop()
    
    log_messages = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Handle Spotify
        if platform in ['spotify_playlist', 'spotify_track']:
            log_messages.append("🎵 Processing Spotify URL...")
            log_area.text_area("Log", "\n".join(log_messages), height=300)
            
            token = get_spotify_token(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
            tracks = get_spotify_tracks(playlist_url, token)
            
            if not tracks:
                st.error("❌ No tracks found")
                st.stop()
            
            log_messages.append(f"✅ Found {len(tracks)} track(s)")
            log_messages.append("📥 Starting downloads...")
            log_area.text_area("Log", "\n".join(log_messages), height=300)
            
            downloaded_files = []
            for idx, track in enumerate(tracks, 1):
                progress_bar.progress(idx / len(tracks))
                status_text.text(f"Downloading {idx}/{len(tracks)}: {track['name']}")
                
                log_messages.append(f"\n[{idx}/{len(tracks)}] {track['artist']} - {track['name']}")
                log_area.text_area("Log", "\n".join(log_messages[-30:]), height=300)
                
                file_path = download_from_search(track['search_query'], temp_dir, audio_format, audio_quality)
                
                if file_path:
                    log_messages.append(f"✅ Downloaded")
                    
                    if add_metadata:
                        if add_metadata_to_file(file_path, track):
                            log_messages.append(f"✅ Metadata added")
                    
                    downloaded_files.append(file_path)
                else:
                    log_messages.append(f"❌ Failed")
                
                log_area.text_area("Log", "\n".join(log_messages[-30:]), height=300)
        
        # Handle YouTube Single Video
        elif platform == 'youtube_track':
            log_messages.append("🎵 Processing YouTube video...")
            log_area.text_area("Log", "\n".join(log_messages), height=300)
            
            status_text.text("Downloading video...")
            progress_bar.progress(0.5)
            
            file_path, method = download_single_youtube_video(playlist_url, temp_dir, audio_format, audio_quality)
            
            if file_path:
                log_messages.append(f"✅ Downloaded using {method}")
                downloaded_files = [file_path]
            else:
                log_messages.append(f"❌ Download failed: {method}")
                downloaded_files = []
            
            log_area.text_area("Log", "\n".join(log_messages), height=300)
            progress_bar.progress(1.0)
        
        # Handle YouTube Playlist
        elif platform == 'youtube_playlist':
            log_messages.append("🎵 Processing YouTube playlist...")
            log_messages.append("📥 Starting batch download (this may take a while)...")
            log_area.text_area("Log", "\n".join(log_messages), height=300)
            
            status_text.text("Downloading playlist...")
            progress_bar.progress(0.3)
            
            downloaded_files = download_youtube_playlist_batch(playlist_url, temp_dir, audio_format, audio_quality)
            
            log_messages.append(f"✅ Downloaded {len(downloaded_files)} videos")
            log_area.text_area("Log", "\n".join(log_messages), height=300)
            progress_bar.progress(1.0)
        
        # Prepare download
        if downloaded_files:
            log_messages.append(f"\n🎉 Success! {len(downloaded_files)} file(s) downloaded")
            log_area.text_area("Log", "\n".join(log_messages), height=300)
            
            # Single file
            if len(downloaded_files) == 1:
                with open(downloaded_files[0], 'rb') as f:
                    file_data = f.read()
                
                st.success(f"✅ Downloaded: {Path(downloaded_files[0]).name}")
                st.download_button(
                    label=f"📥 Download {Path(downloaded_files[0]).name}",
                    data=file_data,
                    file_name=Path(downloaded_files[0]).name,
                    mime="audio/mpeg" if audio_format == "mp3" else "audio/mp4",
                    use_container_width=True
                )
            
            # Multiple files
            else:
                log_messages.append("📦 Creating ZIP file...")
                log_area.text_area("Log", "\n".join(log_messages), height=300)
                
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for file_path in downloaded_files:
                        zip_file.write(file_path, Path(file_path).name)
                
                zip_buffer.seek(0)
                
                st.success(f"✅ Downloaded {len(downloaded_files)} files")
                st.download_button(
                    label=f"📦 Download ZIP ({len(downloaded_files)} files)",
                    data=zip_buffer.getvalue(),
                    file_name="music_download.zip",
                    mime="application/zip",
                    use_container_width=True
                )
        else:
            st.error("❌ No files were downloaded")
            log_messages.append("❌ Download failed - check your URL and internet connection")
            log_area.text_area("Log", "\n".join(log_messages), height=300)
    
    except Exception as e:
        st.error(f"❌ Error: {str(e)}")
        log_messages.append(f"❌ Error: {str(e)}")
        log_area.text_area("Log", "\n".join(log_messages), height=300)
    
    finally:
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

# ---------------- Footer ----------------
st.markdown("---")
st.markdown("""
**Tips:**
- For YouTube Music: Just paste and click download - no fetch needed!
- For Spotify: Works with both playlists and individual tracks
- Files are downloaded in high quality audio format
""")
st.markdown("Made with ❤️")
