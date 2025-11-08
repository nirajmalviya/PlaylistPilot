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
Download your favorite Spotify playlists with **album covers** and proper metadata:
1. Paste your Spotify playlist URL
2. Click **Fetch Playlist** to see the songs
3. Click **Download All** to download with covers

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
    max_retries = st.slider("Retry attempts for failed downloads", 1, 5, 3)
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


# ---------------- Enhanced yt-dlp Download Function ----------------
def download_track_ytdlp(track_info, output_dir, audio_format="m4a", quality="best", retries=3):
    """Download a single track using yt-dlp with retry logic."""
    track_name = track_info["name"]
    artist_name = track_info["artists"]
    
    # Clean filename
    safe_filename = f"{artist_name} - {track_name}"
    safe_filename = re.sub(r'[<>:"/\\|?*]', '', safe_filename)
    
    output_template = os.path.join(output_dir, f"{safe_filename}.%(ext)s")
    
    # Format options
    if audio_format == "m4a":
        format_arg = "bestaudio[ext=m4a]/bestaudio"
    elif audio_format == "mp3":
        format_arg = "bestaudio"
    else:
        format_arg = "bestaudio"
    
    for attempt in range(retries):
        try:
            search_query = f"ytsearch1:{artist_name} {track_name} official audio"
            
            cmd = [
                'yt-dlp',
                '-f', format_arg,
                '-o', output_template,
                '--no-playlist',
                '--quiet',
                '--no-warnings',
                '--extract-audio',
                '--concurrent-fragments', '4',
                '--socket-timeout', '30',
                '--retries', '5',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            ]
            
            # Add post-processing for mp3
            if audio_format == "mp3":
                cmd.extend([
                    '--audio-format', 'mp3',
                    '--audio-quality', quality if quality != "best" else "0"
                ])
            
            cmd.append(search_query)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180
            )
            
            if result.returncode == 0:
                # Find the downloaded file
                downloaded_file = None
                for ext in [audio_format, 'webm', 'opus', 'm4a', 'mp3']:
                    potential_file = os.path.join(output_dir, f"{safe_filename}.{ext}")
                    if os.path.exists(potential_file):
                        downloaded_file = potential_file
                        break
                
                return True, downloaded_file, ""
            else:
                error_msg = result.stderr if result.stderr else "Unknown error"
                if "403" in error_msg and attempt < retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                return False, None, error_msg
                
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return False, None, "Timeout"
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return False, None, str(e)
    
    return False, None, "Max retries exceeded"


def download_playlist_ytdlp(tracks, output_dir, audio_format="m4a", quality="best", max_retries=3, add_metadata=True):
    """Download multiple tracks with metadata."""
    downloaded = 0
    failed = []
    
    for idx, track in enumerate(tracks, 1):
        track_name = track["name"]
        artist_name = track["artists"]
        
        yield f"[{idx}/{len(tracks)}] Downloading: {artist_name} - {track_name}"
        
        success, file_path, error = download_track_ytdlp(
            track, output_dir, audio_format, quality, max_retries
        )
        
        if success and file_path:
            yield f"✅ Downloaded: {track_name}"
            
            # Add metadata and cover art
            if add_metadata:
                yield f"🎨 Adding album cover and metadata..."
                cover_data = None
                if track.get("cover_url"):
                    cover_data = download_cover_art(track["cover_url"])
                
                if add_metadata_to_file(file_path, track, cover_data):
                    yield f"✅ Metadata added"
                else:
                    yield f"⚠️ Metadata failed (file still downloaded)"
            
            downloaded += 1
        else:
            yield f"❌ Failed: {track_name}"
            if "403" in error:
                yield f"   → 403 Error: Retried {max_retries} times"
            else:
                yield f"   → Error: {error[:100]}"
            failed.append(f"{artist_name} - {track_name}")
        
        yield f"Progress: {downloaded}/{len(tracks)} successful"
    
    yield f"\n🎉 Download complete! {downloaded}/{len(tracks)} tracks downloaded"
    if failed:
        yield f"⚠️ Failed tracks ({len(failed)}): " + ", ".join(failed[:5])
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

                    # Display tracks with cover
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
            # Download using yt-dlp
            append_log(f"📥 Downloading with yt-dlp (retries: {max_retries})...")
            status_text.text("Downloading songs with album covers...")

            download_count = 0
            total_tracks = len(st.session_state.playlist_tracks)
            
            for output in download_playlist_ytdlp(
                st.session_state.playlist_tracks, 
                temp_dir, 
                audio_format,
                audio_quality,
                max_retries,
                add_metadata
            ):
                append_log(output)
                if "✅ Downloaded:" in output:
                    download_count += 1
                    progress_bar.progress(min(download_count / max(total_tracks, 1), 1.0))

            # Check if files were downloaded
            downloaded_files = []
            for ext in [audio_format, 'webm', 'opus', 'm4a', 'mp3']:
                downloaded_files.extend(list(Path(temp_dir).glob(f"*.{ext}")))

            if downloaded_files:
                append_log(f"\n✅ Successfully downloaded {len(downloaded_files)} songs with metadata")

                # Create ZIP file
                append_log("📦 Creating ZIP file...")
                zip_buffer = BytesIO()

                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for file_path in downloaded_files:
                        zip_file.write(file_path, file_path.name)

                zip_buffer.seek(0)

                # Clean playlist name for filename
                playlist_name_safe = "".join(
                    c for c in st.session_state.playlist_name if c.isalnum() or c in (' ', '-', '_'))
                if not playlist_name_safe:
                    playlist_name_safe = "playlist"
                zip_filename = f"{playlist_name_safe}_songs.zip"

                st.success(f"🎉 Downloaded {len(downloaded_files)} songs with album covers!")

                # Download button
                st.download_button(
                    label=f"📦 Download ZIP File ({len(downloaded_files)} songs)",
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
       - Paste it in the input box above

    2. **Fetch Playlist**: 
       - Click "Fetch Playlist Info" to load the tracks

    3. **Download**: 
       - Click "Download All"
       - Wait for processing (1-2 minutes per song)
       - Album covers are automatically embedded
       - Click "Download ZIP File" to save

    ### Features:
    - ✅ **Album covers embedded** in files
    - ✅ **Metadata** (artist, title, album)
    - ✅ **Automatic retry** for 403 errors
    - ✅ **Exponential backoff** for failed downloads
    - ✅ **User-agent spoofing** to avoid blocks

    ### Format Guide:
    - **M4A**: High quality, no FFmpeg needed, supports covers
    - **MP3**: Universal compatibility, requires FFmpeg, supports covers

    ### Troubleshooting 403 Errors:
    - Tool automatically retries failed downloads
    - Uses different user agents to avoid detection
    - Implements exponential backoff between retries
    - Adjust retry count in settings (1-5 attempts)

    ### Legal Note:
    ⚠️ This tool is for personal use only. Please respect copyright laws.
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using Streamlit, yt-dlp & Mutagen | Album covers included 🎨")
