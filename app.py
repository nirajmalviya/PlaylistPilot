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
SPOTIFY_CLIENT_ID = "b8d625c4e9ea44ef977009c72398f32e"
SPOTIFY_CLIENT_SECRET = "2b82b875364d4616b7476197e7c2c156"

st.set_page_config(page_title="Spotify Playlist Downloader", layout="wide")
st.title("🎵 Spotify Playlist Downloader")

st.markdown("""
Download your favorite Spotify playlists with **album covers** from multiple sources:
1. Paste your Spotify playlist URL
2. Click **Fetch Playlist** to see the songs
3. Click **Download All** - automatically tries YouTube, Soundcloud, and more!

**Requirements**: `pip install yt-dlp mutagen requests`
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
playlist_url = st.text_input(
    "Spotify Playlist URL",
    placeholder="https://open.spotify.com/playlist/37i9dQZF1E38Nuyz9Gc1Wd"
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
    fetch_btn = st.button("🔍 Fetch Playlist Info", use_container_width=True)
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


def extract_playlist_id(url):
    """Extract playlist ID from Spotify URL."""
    match = re.search(r'playlist/([a-zA-Z0-9]+)', url)
    if match:
        return match.group(1)
    return None


def fetch_spotify_playlist(playlist_id, token):
    """Fetch playlist data from Spotify API."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}"

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
        }
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
    
    # Source configurations (in priority order)
    sources = [
        {
            "name": "YouTube Music",
            "url": f"ytsearch1:{artist_name} {track_name} official audio",
            "extra_args": []
        },
        {
            "name": "YouTube",
            "url": f"ytsearch1:{artist_name} {track_name} lyrics",
            "extra_args": []
        },
        {
            "name": "Soundcloud",
            "url": f"scsearch1:{artist_name} {track_name}",
            "extra_args": ['--extractor-args', 'soundcloud:client_id=']
        },
        {
            "name": "YouTube (Alternative)",
            "url": f"ytsearch1:{track_name} {artist_name}",
            "extra_args": []
        }
    ]
    
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
                '--quiet',
                '--no-warnings',
                '--extract-audio',
                '--no-check-certificates',
                '--socket-timeout', '30',
                '--retries', '3',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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
                    return True, downloaded_file, source["name"]
            
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
if "logs" not in st.session_state:
    st.session_state.logs = []


def append_log(msg):
    st.session_state.logs.append(msg)
    log_area.text("\n".join(st.session_state.logs[-50:]))


# ---------------- Fetch Button ----------------
if fetch_btn:
    if not playlist_url.strip():
        st.error("Please enter a playlist URL")
    else:
        try:
            with st.spinner("Authenticating with Spotify..."):
                token = get_spotify_token(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

            playlist_id = extract_playlist_id(playlist_url)
            if not playlist_id:
                st.error("Invalid Spotify playlist URL")
            else:
                with st.spinner("Fetching playlist..."):
                    playlist_data = fetch_spotify_playlist(playlist_id, token)

                tracks = extract_tracks_from_spotify(playlist_data)
                st.session_state.playlist_tracks = tracks
                st.session_state.playlist_name = playlist_data.get('name', 'playlist')

                if tracks:
                    st.success(f"✅ Found {len(tracks)} tracks")

                    # Display tracks
                    df = pd.DataFrame(tracks)
                    st.dataframe(
                        df[["name", "artists", "album"]],
                        use_container_width=True,
                        height=400
                    )

                    # Show playlist info
                    st.info(f"**{playlist_data.get('name')}** by {playlist_data.get('owner', {}).get('display_name')}")
                else:
                    st.warning("No tracks found in playlist")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                st.error("❌ Authentication error. Please contact support.")
            else:
                st.error(f"❌ Spotify API Error: {e}")
        except Exception as e:
            st.error(f"❌ Error: {e}")

# ---------------- Download Button ----------------
if download_btn:
    if not playlist_url.strip():
        st.error("Please enter a playlist URL first")
    elif not st.session_state.playlist_tracks:
        st.warning("Please fetch the playlist first by clicking 'Fetch Playlist Info'")
    else:
        st.session_state.logs = []
        append_log("🚀 Starting download process...")

        # Create temporary directory for downloads
        temp_dir = tempfile.mkdtemp()

        try:
            # Download using multi-source approach
            append_log(f"📥 Downloading (tries: YouTube Music → YouTube → Soundcloud)...")
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
            
            # Remove duplicates based on filename (keep first occurrence)
            seen = set()
            unique_files = []
            for f in downloaded_files:
                base_name = f.stem
                if base_name not in seen:
                    seen.add(base_name)
                    unique_files.append(f)

            if unique_files:
                append_log(f"\n✅ Successfully downloaded {len(unique_files)} unique songs with metadata")

                # Create ZIP file
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

                st.info(f"💾 Click the button above to download all songs with embedded album art")
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

# ---------------- Help Section ----------------
with st.expander("💡 How to Use"):
    st.markdown("""
    ### Installation:

    Install required packages:
    ```bash
    pip install yt-dlp mutagen requests
    ```

    ### Steps:

    1. **Get Playlist URL**: 
       - Open Spotify and go to your playlist
       - Click Share → Copy Playlist Link

    2. **Fetch Playlist**: 
       - Click "Fetch Playlist Info" to load tracks

    3. **Download**: 
       - Click "Download All"
       - Automatically tries multiple sources
       - Album covers are embedded

    ### Multi-Source Download:
    The tool tries sources in this order:
    1. **YouTube Music** (official audio)
    2. **YouTube** (lyrics version)
    3. **Soundcloud** (if available)
    4. **YouTube Alternative** (different search)

    ### Features:
    - ✅ **No duplicates** - smart file checking
    - ✅ **Album covers** embedded automatically
    - ✅ **Multiple sources** - higher success rate
    - ✅ **Skip existing** - resume interrupted downloads
    - ✅ **Metadata included** (artist, title, album)

    ### Format Guide:
    - **M4A**: No FFmpeg needed, excellent quality
    - **MP3**: Requires FFmpeg, universal compatibility

    ### Legal Note:
    ⚠️ This tool is for personal use only. Please respect copyright laws.
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ | Multi-source: YouTube Music, YouTube, Soundcloud 🎵")
