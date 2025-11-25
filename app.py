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

# ---------------- CONFIG ----------------
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

st.set_page_config(page_title="Music Playlist Downloader", layout="wide")
st.title("🎵 Spotify & YouTube Music Downloader")

st.markdown("""
Download your favorite playlists or individual songs with **album covers**:
1. Paste your **Spotify** OR **YouTube Music** playlist/track URL
2. Click **Fetch** to see the songs
3. Click **Download** - automatically tries multiple sources!

**Supported URLs:**
- Spotify: `https://open.spotify.com/playlist/...` or `https://open.spotify.com/track/...`
- YouTube Music: `https://music.youtube.com/playlist?list=...` or `https://music.youtube.com/watch?v=...`
- YouTube: `https://www.youtube.com/playlist?list=...` or `https://www.youtube.com/watch?v=...`

**Deployment Note:** If YouTube Music isn't working, ensure yt-dlp is updated: `pip install -U yt-dlp`
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

# Show yt-dlp version for debugging
try:
    result = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
    ytdlp_version = result.stdout.strip()
    with st.expander("🔧 Debug Info"):
        st.text(f"yt-dlp version: {ytdlp_version}")
        st.caption("If YouTube Music isn't working, try updating: pip install -U yt-dlp")
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
        help="m4a: No FFmpeg needed. mp3: Requires FFmpeg but more compatible."
    )
    audio_quality = st.selectbox("Quality", ["best", "192", "128"], index=0)
    add_metadata = st.checkbox("Add album covers & metadata", value=True)

col1, col2 = st.columns(2)
with col1:
    fetch_btn = st.button("🔍 Fetch Info", use_container_width=True)
with col2:
    download_btn = st.button("⬇️ Download All", use_container_width=True, type="primary")

log_area = st.empty()
progress_bar = st.progress(0)
status_text = st.empty()


# ---------------- URL Detection ----------------
def detect_platform(url):
    """Detect which platform the URL is from."""
    url = url.strip()
    
    # Spotify
    if 'spotify.com' in url:
        if 'playlist' in url:
            return 'spotify_playlist'
        elif 'track' in url:
            return 'spotify_track'
    
    # YouTube Music or YouTube
    if 'youtube.com' in url or 'youtu.be' in url or 'music.youtube.com' in url:
        if 'playlist' in url or 'list=' in url:
            return 'youtube_playlist'
        elif 'watch?v=' in url or 'youtu.be/' in url:
            return 'youtube_track'
    
    return None


# ---------------- Spotify API Functions ----------------
def get_spotify_token(client_id, client_secret):
    """Get Spotify API access token."""
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


def extract_spotify_id(url):
    """Extract playlist or track ID from Spotify URL."""
    playlist_match = re.search(r'playlist/([a-zA-Z0-9]+)', url)
    if playlist_match:
        return 'playlist', playlist_match.group(1)

    track_match = re.search(r'track/([a-zA-Z0-9]+)', url)
    if track_match:
        return 'track', track_match.group(1)

    return None, None


def fetch_spotify_playlist(playlist_id, token):
    """Fetch playlist data from Spotify API."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}"

    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def fetch_spotify_track(track_id, token):
    """Fetch single track data from Spotify API."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/tracks/{track_id}"

    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def extract_tracks_from_spotify(playlist_data):
    """Extract track information from Spotify playlist."""
    tracks = []
    items = playlist_data.get("tracks", {}).get("items", [])

    for item in items:
        track = item.get("track")
        if not track:
            continue

        album = track.get("album", {})
        images = album.get("images", [])
        cover_url = images[0]["url"] if images else None

        track_info = {
            "id": track.get("id"),
            "name": track.get("name"),
            "artists": ", ".join([a["name"] for a in track.get("artists", [])]),
            "album": album.get("name", ""),
            "duration_ms": track.get("duration_ms"),
            "spotify_url": track.get("external_urls", {}).get("spotify", ""),
            "cover_url": cover_url,
            "source": "spotify"
        }
        tracks.append(track_info)

    return tracks


def extract_single_track_info(track_data):
    """Extract information from a single Spotify track."""
    album = track_data.get("album", {})
    images = album.get("images", [])
    cover_url = images[0]["url"] if images else None

    track_info = {
        "id": track_data.get("id"),
        "name": track_data.get("name"),
        "artists": ", ".join([a["name"] for a in track_data.get("artists", [])]),
        "album": album.get("name", ""),
        "duration_ms": track_data.get("duration_ms"),
        "spotify_url": track_data.get("external_urls", {}).get("spotify", ""),
        "cover_url": cover_url,
        "source": "spotify"
    }

    return track_info


# ---------------- YouTube Functions ----------------
def fetch_youtube_playlist(url):
    """Fetch YouTube/YouTube Music playlist using yt-dlp with multiple methods."""
    methods = [
        {
            "name": "Standard fetch",
            "args": [
                '--dump-json',
                '--flat-playlist',
                '--no-warnings',
                '--no-check-certificates',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '--extractor-retries', '5',
                '--socket-timeout', '30',
            ]
        },
        {
            "name": "With Android client",
            "args": [
                '--dump-json',
                '--flat-playlist',
                '--no-warnings',
                '--no-check-certificates',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '--extractor-retries', '5',
                '--extractor-args', 'youtube:player_client=android,web',
            ]
        }
    ]
    
    for method in methods:
        try:
            cmd = ['yt-dlp'] + method["args"] + [url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            
            if result.returncode == 0:
                tracks = []
                for line in result.stdout.strip().split('\n'):
                    if line:
                        try:
                            video_info = json.loads(line)
                            track_info = {
                                "id": video_info.get("id", ""),
                                "name": video_info.get("title", "Unknown"),
                                "artists": video_info.get("uploader", "Unknown Artist"),
                                "album": video_info.get("album", ""),
                                "duration_ms": (video_info.get("duration", 0) * 1000) if video_info.get("duration") else 0,
                                "youtube_url": f"https://www.youtube.com/watch?v={video_info.get('id', '')}",
                                "cover_url": video_info.get("thumbnail", ""),
                                "source": "youtube"
                            }
                            tracks.append(track_info)
                        except json.JSONDecodeError:
                            continue
                
                if tracks:
                    return tracks
            
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            continue
    
    st.error("❌ Failed to fetch playlist with all methods. The playlist may be private or region-locked.")
    return None


def fetch_youtube_track(url):
    """Fetch single YouTube/YouTube Music track using yt-dlp with multiple attempts."""
    # Try different methods in order
    methods = [
        {
            "name": "Standard fetch",
            "args": [
                '--dump-json',
                '--no-warnings',
                '--no-check-certificates',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '--extractor-retries', '5',
                '--socket-timeout', '30',
            ]
        },
        {
            "name": "With cookies workaround",
            "args": [
                '--dump-json',
                '--no-warnings',
                '--no-check-certificates',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '--extractor-retries', '5',
                '--socket-timeout', '30',
                '--extractor-args', 'youtube:player_client=android,web',
            ]
        },
        {
            "name": "Age-gate bypass",
            "args": [
                '--dump-json',
                '--no-warnings',
                '--no-check-certificates',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '--extractor-retries', '5',
                '--age-limit', '21',
                '--extractor-args', 'youtube:player_client=android',
            ]
        }
    ]
    
    for method in methods:
        try:
            cmd = ['yt-dlp'] + method["args"] + [url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            
            if result.returncode == 0 and result.stdout.strip():
                try:
                    video_info = json.loads(result.stdout)
                    
                    track_info = {
                        "id": video_info.get("id", ""),
                        "name": video_info.get("title", "Unknown"),
                        "artists": video_info.get("uploader", "Unknown Artist"),
                        "album": video_info.get("album", ""),
                        "duration_ms": (video_info.get("duration", 0) * 1000) if video_info.get("duration") else 0,
                        "youtube_url": url,
                        "cover_url": video_info.get("thumbnail", ""),
                        "source": "youtube"
                    }
                    
                    return track_info
                except json.JSONDecodeError:
                    continue
            
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            continue
    
    # All methods failed - create basic track info from URL
    st.warning("⚠️ Could not fetch video details, but will still attempt download")
    
    # Extract video ID
    video_id = None
    if 'watch?v=' in url:
        video_id = url.split('watch?v=')[1].split('&')[0]
    elif 'youtu.be/' in url:
        video_id = url.split('youtu.be/')[1].split('?')[0]
    
    # Return basic track info - download will still work
    return {
        "id": video_id or "unknown",
        "name": f"YouTube Video {video_id}" if video_id else "YouTube Video",
        "artists": "YouTube",
        "album": "",
        "duration_ms": 0,
        "youtube_url": url,
        "cover_url": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg" if video_id else "",
        "source": "youtube"
    }


# ---------------- Helper Functions ----------------
def download_cover_art(cover_url):
    """Download album cover from URL."""
    try:
        response = requests.get(cover_url, timeout=10)
        response.raise_for_status()
        return response.content
    except:
        return None


def add_metadata_to_file(file_path, track_info, cover_data):
    """Add metadata and album cover to audio file."""
    try:
        if file_path.endswith('.m4a'):
            audio = MP4(file_path)
            audio["\xa9nam"] = track_info["name"]
            audio["\xa9ART"] = track_info["artists"]
            audio["\xa9alb"] = track_info.get("album", "")

            if cover_data:
                audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

            audio.save()

        elif file_path.endswith('.mp3'):
            audio = MP3(file_path, ID3=ID3)

            try:
                audio.add_tags()
            except:
                pass

            audio.tags.add(TIT2(encoding=3, text=track_info["name"]))
            audio.tags.add(TPE1(encoding=3, text=track_info["artists"]))
            audio.tags.add(TALB(encoding=3, text=track_info.get("album", "")))

            if cover_data:
                audio.tags.add(
                    APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=cover_data
                    )
                )

            audio.save()

        return True
    except Exception as e:
        return False


def clean_filename(text):
    """Clean filename by removing invalid characters."""
    return re.sub(r'[<>:"/\\|?*]', '', text)


def file_exists_in_dir(output_dir, base_filename, extensions):
    """Check if file already exists with any of the given extensions."""
    for ext in extensions:
        file_path = os.path.join(output_dir, f"{base_filename}.{ext}")
        if os.path.exists(file_path):
            return True, file_path
    return False, None


# ---------------- Multi-Source Download Function ----------------
def download_track_multisource(track_info, output_dir, audio_format="m4a", quality="best"):
    """Download a single track using multiple sources."""
    track_name = track_info["name"]
    artist_name = track_info["artists"]

    # Clean filename
    safe_filename = clean_filename(f"{artist_name} - {track_name}")

    # Check if already downloaded
    possible_extensions = ['m4a', 'mp3', 'webm', 'opus']
    exists, existing_file = file_exists_in_dir(output_dir, safe_filename, possible_extensions)
    if exists:
        return True, existing_file, "Already downloaded"

    output_template = os.path.join(output_dir, f"{safe_filename}.%(ext)s")

    # If the track is from YouTube, try direct URL first
    sources = []
    
    if track_info.get("source") == "youtube" and track_info.get("youtube_url"):
        sources.append({
            "name": "YouTube (Direct)",
            "url": track_info["youtube_url"],
            "extra_args": []
        })
    
    # Add search-based sources
    sources.extend([
        {
            "name": "YouTube Music (Official Audio)",
            "url": f"ytsearch1:{artist_name} - {track_name} official audio",
            "extra_args": []
        },
        {
            "name": "YouTube (Topic Channel)",
            "url": f"ytsearch1:{artist_name} - {track_name} topic",
            "extra_args": []
        },
        {
            "name": "YouTube (Provided to YouTube)",
            "url": f"ytsearch1:{artist_name} {track_name} provided to youtube",
            "extra_args": []
        },
        {
            "name": "YouTube (Audio)",
            "url": f"ytsearch1:{artist_name} {track_name} audio",
            "extra_args": []
        },
        {
            "name": "Soundcloud",
            "url": f"scsearch1:{artist_name} {track_name}",
            "extra_args": ['--extractor-args', 'soundcloud:client_id=']
        }
    ])

    # Format options
    if audio_format == "m4a":
        format_arg = "bestaudio[ext=m4a]/bestaudio/best"
    elif audio_format == "mp3":
        format_arg = "bestaudio/best"
    else:
        format_arg = "bestaudio/best"

    for source in sources:
        try:
            cmd = [
                'yt-dlp',
                '-f', format_arg,
                '-o', output_template,
                '--no-playlist',
                '--no-warnings',
                '--extract-audio',
                '--no-check-certificates',
                '--socket-timeout', '30',
                '--retries', '5',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '--match-filter', '!is_live & !was_live',
                '--default-search', 'ytsearch',
                '--extractor-args', 'youtube:player_client=android,web',
            ]

            # Add source-specific args
            cmd.extend(source["extra_args"])

            # Add post-processing for mp3
            if audio_format == "mp3":
                cmd.extend([
                    '--audio-format', 'mp3',
                    '--audio-quality', quality if quality != "best" else "0"
                ])

            cmd.append(source["url"])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode == 0:
                # Find the downloaded file
                exists, downloaded_file = file_exists_in_dir(output_dir, safe_filename, possible_extensions)
                if exists:
                    # Verify the file is not empty or corrupted
                    if os.path.getsize(downloaded_file) > 50000:  # At least 50KB
                        return True, downloaded_file, source["name"]
                    else:
                        # File too small, might be corrupted, try next source
                        os.remove(downloaded_file)

            # If failed, try next source
            time.sleep(1)

        except subprocess.TimeoutExpired:
            continue
        except Exception as e:
            continue

    return False, None, "All sources failed"


def download_playlist_multisource(tracks, output_dir, audio_format="m4a", quality="best", add_metadata=True):
    """Download multiple tracks with metadata from multiple sources."""
    downloaded = 0
    failed = []
    skipped = 0

    for idx, track in enumerate(tracks, 1):
        track_name = track["name"]
        artist_name = track["artists"]

        yield f"[{idx}/{len(tracks)}] Processing: {artist_name} - {track_name}"

        success, file_path, source = download_track_multisource(
            track, output_dir, audio_format, quality
        )

        if success and file_path:
            if source == "Already downloaded":
                yield f"⏭️  Skipped (already exists): {track_name}"
                skipped += 1
            else:
                yield f"✅ Downloaded from {source}: {track_name}"

                # Add metadata and cover art
                if add_metadata:
                    yield f"🎨 Adding album cover and metadata..."
                    cover_data = None
                    if track.get("cover_url"):
                        cover_data = download_cover_art(track["cover_url"])

                    if add_metadata_to_file(file_path, track, cover_data):
                        yield f"✅ Metadata added"
                    else:
                        yield f"⚠️  Metadata failed (file still usable)"

                downloaded += 1
        else:
            yield f"❌ Failed: {track_name} - {source}"
            failed.append(f"{artist_name} - {track_name}")

        yield f"Progress: {downloaded}/{len(tracks)} downloaded, {skipped} skipped"

    yield f"\n🎉 Complete! {downloaded}/{len(tracks)} downloaded, {skipped} skipped"
    if failed:
        yield f"⚠️  Failed tracks ({len(failed)}): " + ", ".join(failed[:5])
        if len(failed) > 5:
            yield f"   ... and {len(failed) - 5} more"


# ---------------- Session State ----------------
if "playlist_tracks" not in st.session_state:
    st.session_state.playlist_tracks = []
if "playlist_name" not in st.session_state:
    st.session_state.playlist_name = ""
if "content_type" not in st.session_state:
    st.session_state.content_type = ""
if "logs" not in st.session_state:
    st.session_state.logs = []


def append_log(msg):
    st.session_state.logs.append(msg)
    log_area.text("\n".join(st.session_state.logs[-50:]))


# ---------------- Fetch Button ----------------
if fetch_btn:
    if not playlist_url.strip():
        st.error("Please enter a playlist or track URL")
    else:
        try:
            platform = detect_platform(playlist_url)
            
            if not platform:
                st.error("❌ Unsupported URL. Please use Spotify or YouTube/YouTube Music URLs.")
            
            # Handle Spotify
            elif platform in ['spotify_playlist', 'spotify_track']:
                with st.spinner("Authenticating with Spotify..."):
                    token = get_spotify_token(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

                content_type, content_id = extract_spotify_id(playlist_url)

                if platform == 'spotify_playlist':
                    with st.spinner("Fetching Spotify playlist..."):
                        playlist_data = fetch_spotify_playlist(content_id, token)

                    tracks = extract_tracks_from_spotify(playlist_data)
                    st.session_state.playlist_tracks = tracks
                    st.session_state.playlist_name = playlist_data.get('name', 'playlist')
                    st.session_state.content_type = "playlist"

                    if tracks:
                        st.success(f"✅ Found {len(tracks)} tracks in Spotify playlist")
                        df = pd.DataFrame(tracks)
                        st.dataframe(
                            df[["name", "artists", "album"]],
                            use_container_width=True,
                            height=400
                        )
                        st.info(f"**{playlist_data.get('name')}** by {playlist_data.get('owner', {}).get('display_name')}")
                    else:
                        st.warning("No tracks found in playlist")

                elif platform == 'spotify_track':
                    with st.spinner("Fetching Spotify track..."):
                        track_data = fetch_spotify_track(content_id, token)

                    track_info = extract_single_track_info(track_data)
                    st.session_state.playlist_tracks = [track_info]
                    st.session_state.playlist_name = f"{track_info['artists']} - {track_info['name']}"
                    st.session_state.content_type = "track"

                    st.success(f"✅ Spotify track found")
                    df = pd.DataFrame([track_info])
                    st.dataframe(
                        df[["name", "artists", "album"]],
                        use_container_width=True
                    )
                    st.info(f"**{track_info['name']}** by {track_info['artists']}")
            
            # Handle YouTube/YouTube Music
            elif platform == 'youtube_playlist':
                with st.spinner("Fetching YouTube playlist..."):
                    tracks = fetch_youtube_playlist(playlist_url)
                
                if tracks:
                    st.session_state.playlist_tracks = tracks
                    st.session_state.playlist_name = "YouTube Playlist"
                    st.session_state.content_type = "playlist"
                    
                    st.success(f"✅ Found {len(tracks)} videos in YouTube playlist")
                    df = pd.DataFrame(tracks)
                    st.dataframe(
                        df[["name", "artists"]],
                        use_container_width=True,
                        height=400
                    )
                else:
                    st.error("❌ Failed to fetch YouTube playlist")
            
            elif platform == 'youtube_track':
                with st.spinner("Fetching YouTube video..."):
                    track_info = fetch_youtube_track(playlist_url)
                
                if track_info:
                    st.session_state.playlist_tracks = [track_info]
                    st.session_state.playlist_name = track_info['name']
                    st.session_state.content_type = "track"
                    
                    st.success(f"✅ YouTube video found")
                    df = pd.DataFrame([track_info])
                    st.dataframe(
                        df[["name", "artists"]],
                        use_container_width=True
                    )
                    st.info(f"**{track_info['name']}** by {track_info['artists']}")
                else:
                    st.error("❌ Failed to fetch YouTube video")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                st.error("❌ Authentication error. Please check your Spotify credentials.")
            else:
                st.error(f"❌ API Error: {e}")
        except Exception as e:
            st.error(f"❌ Error: {e}")

# ---------------- Download Button ----------------
if download_btn:
    if not playlist_url.strip():
        st.error("Please enter a URL first")
    elif not st.session_state.playlist_tracks:
        st.warning("Please fetch the playlist/track first by clicking 'Fetch Info'")
    else:
        st.session_state.logs = []
        append_log("🚀 Starting download process...")

        # Create temporary directory for downloads
        temp_dir = tempfile.mkdtemp()

        try:
            # Download using multi-source approach
            append_log(f"📥 Downloading from multiple sources...")
            status_text.text("Downloading songs with album covers...")

            download_count = 0
            total_tracks = len(st.session_state.playlist_tracks)

            for output in download_playlist_multisource(
                    st.session_state.playlist_tracks,
                    temp_dir,
                    audio_format,
                    audio_quality,
                    add_metadata
            ):
                append_log(output)
                if "✅ Downloaded from" in output:
                    download_count += 1
                    progress_bar.progress(min(download_count / max(total_tracks, 1), 1.0))

            # Check if files were downloaded
            downloaded_files = []
            for ext in ['m4a', 'mp3', 'webm', 'opus']:
                downloaded_files.extend(list(Path(temp_dir).glob(f"*.{ext}")))

            # Remove duplicates based on filename
            seen = set()
            unique_files = []
            for f in downloaded_files:
                base_name = f.stem
                if base_name not in seen:
                    seen.add(base_name)
                    unique_files.append(f)

            if unique_files:
                append_log(f"\n✅ Successfully downloaded {len(unique_files)} unique songs with metadata")

                # For single track, provide direct download
                if st.session_state.content_type == "track" and len(unique_files) == 1:
                    file_path = unique_files[0]
                    with open(file_path, 'rb') as f:
                        file_data = f.read()

                    st.success(f"🎉 Downloaded song with album cover!")

                    st.download_button(
                        label=f"📥 Download {file_path.name}",
                        data=file_data,
                        file_name=file_path.name,
                        mime="audio/mpeg" if file_path.suffix == ".mp3" else "audio/mp4",
                        use_container_width=True
                    )

                else:
                    # Create ZIP file for multiple tracks
                    append_log("📦 Creating ZIP file...")
                    zip_buffer = BytesIO()

                    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                        for file_path in unique_files:
                            zip_file.write(file_path, file_path.name)

                    zip_buffer.seek(0)

                    # Clean playlist name for filename
                    playlist_name_safe = "".join(
                        c for c in st.session_state.playlist_name if c.isalnum() or c in (' ', '-', '_'))
                    if not playlist_name_safe:
                        playlist_name_safe = "playlist"
                    zip_filename = f"{playlist_name_safe}_songs.zip"

                    st.success(f"🎉 Downloaded {len(unique_files)} songs with album covers!")

                    # Download button
                    st.download_button(
                        label=f"📦 Download ZIP File ({len(unique_files)} songs)",
                        data=zip_buffer.getvalue(),
                        file_name=zip_filename,
                        mime="application/zip",
                        use_container_width=True
                    )

                st.info(f"💾 Click the button above to download")
            else:
                st.error("❌ No songs were downloaded. Check the logs above for errors.")

        except Exception as e:
            st.error(f"❌ Error during download: {e}")
            append_log(f"Error: {str(e)}")

        finally:
            # Cleanup temporary directory
            try:
                shutil.rmtree(temp_dir)
            except:
                pass

            progress_bar.progress(1.0)

# ---------------- Footer ----------------
st.markdown("---")
st.markdown("Made with ❤️ | Supports: Spotify, YouTube Music, YouTube 🎵")
