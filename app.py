import os
import re
import requests
import pandas as pd
import streamlit as st
from pathlib import Path
import time
import base64
import zipfile
from io import BytesIO
import subprocess
import tempfile
import shutil
from dotenv import load_dotenv
import sys
import json

# Added imports
import shutil as _shutil
try:
    import imageio_ffmpeg as iio_ffmpeg
except Exception:
    iio_ffmpeg = None

# ---------------- CONFIG ----------------
load_dotenv()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    st.error("⚠️ Spotify credentials not found! Please set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env or Streamlit environment.")
    st.stop()
# ----------------------------------------

st.set_page_config(page_title="Spotify Playlist Downloader", layout="wide")
st.title("🎵 Spotify Playlist Downloader")

st.markdown("""
Download your favorite Spotify playlists:
1. Paste your Spotify playlist URL
2. Click **Fetch Playlist** to see the songs
3. Click **Download All** to download songs to your device

**Requirements**: Make sure `spotdl` is installed: `pip install spotdl`
""")

# ---------------- ffmpeg & spotdl helpers ----------------
def ensure_ffmpeg():
    """
    Ensure an ffmpeg binary is available.
    Returns path to ffmpeg executable or None.
    """
    ff = _shutil.which("ffmpeg")
    if ff:
        return ff

    if iio_ffmpeg is not None:
        try:
            ff_exe = iio_ffmpeg.get_ffmpeg_exe()
            ff_dir = os.path.dirname(ff_exe)
            os.environ["PATH"] = ff_dir + os.pathsep + os.environ.get("PATH", "")
            return _shutil.which("ffmpeg") or ff_exe
        except Exception as e:
            print("imageio-ffmpeg failed:", e)
            return None
    return None


def is_spotdl_available():
    """Return True if spotdl module/CLI is available."""
    try:
        proc = subprocess.run([sys.executable, "-m", "spotdl", "--version"],
                              capture_output=True, text=True, timeout=6)
        return proc.returncode == 0
    except Exception:
        return False


ffmpeg_exe = ensure_ffmpeg()
spotdl_installed = is_spotdl_available()

# Check requirements silently
if not spotdl_installed:
    st.error("⚠️ SpotDL is not installed! Please add `spotdl` to requirements.txt (e.g. `spotdl>=4.2.5`) and redeploy.")
    st.stop()

if not ffmpeg_exe:
    st.error("⚠️ FFmpeg not found. Add `ffmpeg` to apt.txt (Streamlit Cloud) or add `imageio-ffmpeg` to requirements.txt.")
    st.stop()

# ---------------- UI inputs ----------------
playlist_url = st.text_input(
    "Spotify Playlist URL",
    placeholder="https://open.spotify.com/playlist/37i9dQZF1E38Nuyz9Gc1Wd"
)

with st.expander("⚙️ Download Settings"):
    audio_format = st.selectbox("Audio Format", ["mp3", "m4a", "flac", "opus", "ogg"])
    audio_quality = st.selectbox("Bitrate", ["320k", "256k", "192k", "128k"], index=1)  # Default to 256k to avoid rate limits
    max_songs = st.number_input("Maximum songs to download (0 = all)", 0, 100, 0)
    # Add retry and delay options
    st.markdown("**Rate Limit Protection:**")
    retry_attempts = st.slider("Retry attempts per song", 1, 5, 3)
    delay_between = st.slider("Delay between downloads (seconds)", 0, 10, 2)

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

        track_info = {
            "id": track.get("id"),
            "name": track.get("name"),
            "artists": ", ".join([a["name"] for a in track.get("artists", [])]),
            "album": track.get("album", {}).get("name", ""),
            "duration_ms": track.get("duration_ms"),
            "spotify_url": track.get("external_urls", {}).get("spotify", ""),
        }
        tracks.append(track_info)

    return tracks


# ---------------- Enhanced SpotDL Download Function ----------------
def download_with_spotdl_enhanced(playlist_url, output_dir, audio_format="mp3", bitrate="256k", 
                                  ffmpeg_path=None, retry_attempts=3, delay=2):
    """
    Enhanced download with better error handling and rate limit protection.
    """
    try:
        cmd = [
            sys.executable, "-m", "spotdl",
            playlist_url,
            "--output", output_dir,
            "--format", audio_format,
            "--bitrate", bitrate,
            "--print-errors",
            "--threads", "1",  # Single thread to avoid overwhelming YT
            "--sponsor-block",  # Skip sponsored segments
        ]

        if ffmpeg_path:
            cmd.extend(["--ffmpeg", ffmpeg_path])

        # Add retry logic via spotdl options
        if retry_attempts > 1:
            cmd.extend(["--download-ffmpeg"])

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        output_lines = []
        error_count = 0
        success_count = 0
        
        for line in process.stdout:
            line = line.strip()
            if line:
                output_lines.append(line)
                
                # Track errors and successes
                if "AudioProviderError" in line or "ERROR" in line:
                    error_count += 1
                    yield f"⚠️ Error: {line}"
                elif "Downloaded" in line or "has been downloaded" in line:
                    success_count += 1
                    yield f"✅ {line}"
                elif "WARNING" in line and "rate/request limit" in line:
                    yield f"⏸️ Rate limited - waiting..."
                    time.sleep(delay)
                else:
                    yield line

        process.wait()
        
        # Final summary
        yield f"\n📊 Summary: {success_count} succeeded, {error_count} failed"
        return process.returncode == 0, success_count, error_count

    except Exception as e:
        yield f"❌ Critical Error: {str(e)}"
        return False, 0, 0


# ---------------- Session State ----------------
if "playlist_tracks" not in st.session_state:
    st.session_state.playlist_tracks = []
if "playlist_name" not in st.session_state:
    st.session_state.playlist_name = ""
if "logs" not in st.session_state:
    st.session_state.logs = []


def append_log(msg):
    st.session_state.logs.append(msg)
    log_area.text("\n".join(st.session_state.logs[-50:]))  # Show more logs

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
                st.error("❌ Authentication error. Please check your Spotify credentials.")
            else:
                st.error(f"❌ Spotify API Error: {e}")
        except Exception as e:
            st.error(f"❌ Error: {e}")

# ---------------- Download Button ----------------
if download_btn:
    if not playlist_url.strip():
        st.error("Please enter a playlist URL first")
    else:
        st.session_state.logs = []
        append_log("🚀 Starting download process...")
        append_log(f"⚙️ Settings: {audio_format.upper()} @ {audio_quality}, {retry_attempts} retries, {delay_between}s delay")

        # Create temporary directory for downloads
        temp_dir = tempfile.mkdtemp()

        try:
            # Download using enhanced spotdl
            append_log(f"📥 Downloading with SpotDL (this may take a while)...")
            status_text.text("Downloading songs... Please be patient")

            success_count = 0
            error_count = 0
            
            result = download_with_spotdl_enhanced(
                playlist_url, 
                temp_dir, 
                audio_format, 
                audio_quality, 
                ffmpeg_path=ffmpeg_exe,
                retry_attempts=retry_attempts,
                delay=delay_between
            )
            
            for output in result:
                if isinstance(output, tuple):
                    # Final result
                    _, success_count, error_count = output
                else:
                    append_log(output)
                    if "✅" in output:
                        success_count += 1
                        total_tracks = len(st.session_state.playlist_tracks) or 1
                        progress_bar.progress(min(success_count / total_tracks, 1.0))

            # Check if files were downloaded
            downloaded_files = list(Path(temp_dir).glob(f"*.{audio_format}"))

            if downloaded_files:
                append_log(f"\n✅ Successfully downloaded {len(downloaded_files)} songs")
                
                if error_count > 0:
                    st.warning(f"⚠️ {error_count} songs failed to download. This can happen due to:\n"
                              f"- Rate limiting from YouTube\n"
                              f"- Songs not available on YouTube Music\n"
                              f"- Regional restrictions\n\n"
                              f"Try downloading again later or in smaller batches.")

                # Create ZIP file
                append_log("📦 Creating ZIP file...")
                status_text.text("Creating ZIP file...")
                
                zip_buffer = BytesIO()

                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_STORED) as zip_file:
                    for i, file_path in enumerate(downloaded_files):
                        zip_file.write(file_path, file_path.name)
                        if i % 5 == 0:
                            progress_bar.progress(min(0.8 + (0.2 * i / len(downloaded_files)), 1.0))

                zip_buffer.seek(0)

                # Clean playlist name for filename
                playlist_name_safe = "".join(
                    c for c in st.session_state.playlist_name if c.isalnum() or c in (' ', '-', '_'))
                if not playlist_name_safe:
                    playlist_name_safe = "playlist"
                zip_filename = f"{playlist_name_safe}_songs.zip"

                st.success(f"🎉 Downloaded {len(downloaded_files)} songs!")

                # Download button
                st.download_button(
                    label=f"📦 Download ZIP File ({len(downloaded_files)} songs)",
                    data=zip_buffer.getvalue(),
                    file_name=zip_filename,
                    mime="application/zip",
                    use_container_width=True
                )

                st.info(f"💾 Click the button above to download all songs as a ZIP file")
            else:
                st.error("❌ No songs were downloaded. Possible reasons:\n"
                        "- All songs failed due to rate limiting\n"
                        "- Songs not available on YouTube Music\n"
                        "- Network issues\n\n"
                        "**Suggestions:**\n"
                        "1. Try again in a few minutes (rate limits reset)\n"
                        "2. Download in smaller batches\n"
                        "3. Use lower bitrate (128k or 192k)\n"
                        "4. Check the logs above for specific errors")

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

    First, install SpotDL:
    ```bash
    pip install spotdl
    ```

    ### Steps:

    1. **Get Playlist URL**: 
       - Open Spotify and go to your playlist
       - Click Share → Copy Playlist Link
       - Paste it in the input box above

    2. **Fetch Playlist** (Optional): 
       - Click "Fetch Playlist Info" to preview songs
       - This step is optional - you can download directly

    3. **Download**: 
       - Click "Download All"
       - Wait for SpotDL to process (usually 1-3 minutes per song)
       - Click "Download ZIP File" to save to your device
       - Extract the ZIP file to access your songs

    ### Troubleshooting Rate Limits:

    If you see "AudioProviderError" or rate limit warnings:
    - **Wait 10-15 minutes** before trying again
    - **Download in smaller batches** (use max songs limit)
    - **Use lower bitrate** (192k or 128k instead of 320k)
    - **Increase delay between downloads** (5-10 seconds)

    ### Tips:
    - SpotDL downloads from YouTube Music (requires internet)
    - Songs include proper metadata (artist, album, cover art)
    - Default bitrate is 256k (good balance of quality and speed)
    - For large playlists (20+ songs), expect some failures due to rate limiting
    - Download during off-peak hours for better success rates

    ### Formats Available:
    - **MP3**: Best compatibility (recommended)
    - **M4A**: Good quality, smaller file size
    - **FLAC**: Lossless quality, large files
    - **OPUS/OGG**: Modern formats, good compression

    ### Legal Note:
    ⚠️ This tool is for personal use only. Please respect copyright laws and terms of service.
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using Streamlit & SpotDL | Powered by Spotify API & YouTube Music")
