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

st.set_page_config(page_title="Spotify Music Downloader", layout="wide")
st.title("🎵 Spotify Music Downloader")

st.markdown("""
Download your favorite Spotify songs/playlists with **album covers** in **lossless quality**:
1. Paste your Spotify **playlist** or **single song** URL
2. Click **Fetch** to see the songs
3. Click **Download** - automatically gets best quality from YouTube Music!
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

# ---------------- UI inputs ----------------
spotify_url = st.text_input(
    "Spotify Playlist or Song URL",
    placeholder="https://open.spotify.com/playlist/... or https://open.spotify.com/track/..."
)

with st.expander("⚙️ Download Settings"):
    audio_format = st.selectbox(
        "Audio Format", 
        ["m4a", "mp3"],
        help="m4a: No FFmpeg needed, best quality. mp3: Requires FFmpeg but more compatible."
    )
    audio_quality = st.selectbox(
        "Audio Quality", 
        ["lossless (best)", "320kbps", "256kbps", "192kbps"],
        index=0,
        help="Lossless provides the best possible audio quality"
    )
    add_metadata = st.checkbox("Add album covers & metadata", value=True)

col1, col2 = st.columns(2)
with col1:
    fetch_btn = st.button("🔍 Fetch Info", use_container_width=True)
with col2:
    download_btn = st.button("⬇️ Download All", use_container_width=True, type="primary")

log_area = st.empty()
progress_bar = st.progress(0)
status_text = st.empty()


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


def extract_spotify_id_and_type(url):
    """Extract ID and type (playlist/track) from Spotify URL."""
    # Match playlist
    playlist_match = re.search(r'playlist/([a-zA-Z0-9]+)', url)
    if playlist_match:
        return playlist_match.group(1), 'playlist'
    
    # Match track (single song)
    track_match = re.search(r'track/([a-zA-Z0-9]+)', url)
    if track_match:
        return track_match.group(1), 'track'
    
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


def extract_track_info(track):
    """Extract track information from Spotify track object."""
    if not track:
        return None

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
    }
    return track_info


def extract_tracks_from_spotify(playlist_data):
    """Extract track information from Spotify playlist."""
    tracks = []
    items = playlist_data.get("tracks", {}).get("items", [])

    for item in items:
        track = item.get("track")
        track_info = extract_track_info(track)
        if track_info:
            tracks.append(track_info)

    return tracks


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
            audio["\xa9alb"] = track_info["album"]
            
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
            audio.tags.add(TALB(encoding=3, text=track_info["album"]))
            
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


def get_quality_bitrate(quality_setting):
    """Convert quality setting to bitrate."""
    quality_map = {
        "lossless (best)": "0",  # Best quality
        "320kbps": "320k",
        "256kbps": "256k",
        "192kbps": "192k"
    }
    return quality_map.get(quality_setting, "0")


# ---------------- Enhanced Download Function ----------------
def download_track_ytmusic(track_info, output_dir, audio_format="m4a", quality="lossless (best)"):
    """Download a single track from YouTube Music in best quality (audio only, no video scenes)."""
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
    
    # Priority sources - YouTube Music first for audio-only content
    sources = [
        {
            "name": "YouTube Music (Audio Only)",
            "url": f"https://music.youtube.com/search?q={artist_name} {track_name}",
            "search": f"ytsearch1:{artist_name} {track_name} audio",
            "extra_args": ['--default-search', 'ytsearch']
        },
        {
            "name": "YouTube Music (Topic)",
            "url": f"ytsearch1:{artist_name} - {track_name} topic",
            "search": f"ytsearch1:{artist_name} - {track_name} topic",
            "extra_args": []
        },
        {
            "name": "YouTube Music (Official Audio)",
            "url": f"ytsearch1:{artist_name} {track_name} official audio",
            "search": f"ytsearch1:{artist_name} {track_name} official audio",
            "extra_args": []
        },
    ]
    
    # Get quality bitrate
    bitrate = get_quality_bitrate(quality)
    
    # Format selection for best audio quality (no video)
    if audio_format == "m4a":
        # Prioritize m4a audio formats, avoid video
        format_arg = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio"
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
                '--quiet',
                '--no-warnings',
                '--extract-audio',  # Force audio extraction
                '--no-check-certificates',
                '--socket-timeout', '30',
                '--retries', '3',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '--prefer-free-formats',  # Prefer free/open formats
            ]
            
            # Add source-specific args
            cmd.extend(source["extra_args"])
            
            # Add post-processing for format conversion
            if audio_format == "mp3":
                cmd.extend([
                    '--audio-format', 'mp3',
                    '--audio-quality', bitrate
                ])
            elif audio_format == "m4a":
                # For m4a, use best quality with no re-encoding if possible
                cmd.extend([
                    '--audio-format', 'best',  # Keep original quality
                ])
            
            cmd.append(source["search"])
            
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
                    return True, downloaded_file, source["name"]
            
            # If failed, try next source
            time.sleep(1)
            
        except subprocess.TimeoutExpired:
            continue
        except Exception as e:
            continue
    
    return False, None, "All sources failed"


def download_playlist_ytmusic(tracks, output_dir, audio_format="m4a", quality="lossless (best)", add_metadata=True):
    """Download multiple tracks with metadata from YouTube Music."""
    downloaded = 0
    failed = []
    skipped = 0
    
    for idx, track in enumerate(tracks, 1):
        track_name = track["name"]
        artist_name = track["artists"]
        
        yield f"[{idx}/{len(tracks)}] Processing: {artist_name} - {track_name}"
        
        success, file_path, source = download_track_ytmusic(
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
                        yield f"✅ Metadata added successfully"
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
if "content_name" not in st.session_state:
    st.session_state.content_name = ""
if "content_type" not in st.session_state:
    st.session_state.content_type = ""
if "logs" not in st.session_state:
    st.session_state.logs = []


def append_log(msg):
    st.session_state.logs.append(msg)
    log_area.text("\n".join(st.session_state.logs[-50:]))


# ---------------- Fetch Button ----------------
if fetch_btn:
    if not spotify_url.strip():
        st.error("Please enter a Spotify URL")
    else:
        try:
            with st.spinner("Authenticating with Spotify..."):
                token = get_spotify_token(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

            spotify_id, content_type = extract_spotify_id_and_type(spotify_url)
            
            if not spotify_id or not content_type:
                st.error("Invalid Spotify URL. Please enter a valid playlist or track URL.")
            else:
                st.session_state.content_type = content_type
                
                if content_type == 'playlist':
                    with st.spinner("Fetching playlist..."):
                        playlist_data = fetch_spotify_playlist(spotify_id, token)
                    
                    tracks = extract_tracks_from_spotify(playlist_data)
                    st.session_state.playlist_tracks = tracks
                    st.session_state.content_name = playlist_data.get('name', 'playlist')
                    
                    if tracks:
                        st.success(f"✅ Found {len(tracks)} tracks in playlist")
                        
                        # Display tracks
                        df = pd.DataFrame(tracks)
                        st.dataframe(
                            df[["name", "artists", "album"]],
                            use_container_width=True,
                            height=400
                        )
                        
                        st.info(f"**{playlist_data.get('name')}** by {playlist_data.get('owner', {}).get('display_name')}")
                    else:
                        st.warning("No tracks found in playlist")
                
                elif content_type == 'track':
                    with st.spinner("Fetching track..."):
                        track_data = fetch_spotify_track(spotify_id, token)
                    
                    track_info = extract_track_info(track_data)
                    
                    if track_info:
                        st.session_state.playlist_tracks = [track_info]
                        st.session_state.content_name = f"{track_info['artists']} - {track_info['name']}"
                        
                        st.success(f"✅ Found track: {track_info['name']}")
                        
                        # Display single track
                        st.write(f"**Title:** {track_info['name']}")
                        st.write(f"**Artist(s):** {track_info['artists']}")
                        st.write(f"**Album:** {track_info['album']}")
                        
                        if track_info.get('cover_url'):
                            st.image(track_info['cover_url'], width=200, caption="Album Cover")
                    else:
                        st.error("Could not fetch track information")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                st.error("❌ Authentication error. Please check your Spotify API credentials.")
            else:
                st.error(f"❌ Spotify API Error: {e}")
        except Exception as e:
            st.error(f"❌ Error: {e}")

# ---------------- Download Button ----------------
if download_btn:
    if not spotify_url.strip():
        st.error("Please enter a Spotify URL first")
    elif not st.session_state.playlist_tracks:
        st.warning("Please fetch the content first by clicking 'Fetch Info'")
    else:
        st.session_state.logs = []
        append_log("🚀 Starting download process...")
        append_log(f"📊 Quality: {audio_quality}, Format: {audio_format}")

        # Create temporary directory for downloads
        temp_dir = tempfile.mkdtemp()

        try:
            # Download using YouTube Music
            append_log(f"📥 Downloading from YouTube Music (audio-only, no video scenes)...")
            status_text.text("Downloading songs in lossless quality with album covers...")

            download_count = 0
            total_tracks = len(st.session_state.playlist_tracks)
            
            for output in download_playlist_ytmusic(
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
                append_log(f"\n✅ Successfully downloaded {len(unique_files)} song(s) with metadata")

                # For single track, offer direct download
                if len(unique_files) == 1:
                    single_file = unique_files[0]
                    with open(single_file, 'rb') as f:
                        file_bytes = f.read()
                    
                    st.success(f"🎉 Downloaded 1 song in {audio_quality} quality!")
                    
                    st.download_button(
                        label=f"📥 Download {single_file.name}",
                        data=file_bytes,
                        file_name=single_file.name,
                        mime=f"audio/{audio_format}",
                        use_container_width=True
                    )
                else:
                    # Create ZI