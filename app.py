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

# ---------------- CONFIG ----------------
# Hardcoded Spotify API credentials
SPOTIFY_CLIENT_ID = "b8d625c4e9ea44ef977009c72398f32e"
SPOTIFY_CLIENT_SECRET = "2b82b875364d4616b7476197e7c2c156"

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


# Check if spotdl is installed
def check_spotdl():
    try:
        result = subprocess.run(['spotdl', '--version'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except:
        return False


spotdl_installed = check_spotdl()

if not spotdl_installed:
    st.error("⚠️ SpotDL is not installed! Please run: `pip install spotdl`")
    st.stop()

# ---------------- UI inputs ----------------
playlist_url = st.text_input(
    "Spotify Playlist URL",
    placeholder="https://open.spotify.com/playlist/37i9dQZF1E38Nuyz9Gc1Wd"
)

with st.expander("⚙️ Download Settings"):
    audio_format = st.selectbox("Audio Format", ["mp3", "m4a", "flac", "opus", "ogg"])
    audio_quality = st.selectbox("Bitrate", ["128k", "192k", "256k", "320k"])
    max_songs = st.number_input("Maximum songs to download (0 = all)", 0, 100, 0)
    use_fallback = st.checkbox("Use fallback sources (Soundcloud, Bandcamp)", value=True, 
                                help="Try alternative sources when YouTube Music fails")

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
def download_with_spotdl(playlist_url, output_dir, audio_format="mp3", bitrate="320k", use_fallback=True):
    """Download playlist using spotdl with fallback options."""
    try:
        # First attempt with YouTube Music (default)
        cmd = [
            'spotdl',
            playlist_url,
            '--output', output_dir,
            '--format', audio_format,
            '--bitrate', bitrate,
            '--print-errors'
        ]

        yield "🎵 Attempting download from YouTube Music..."
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        output_lines = []
        failed_tracks = []
        
        for line in process.stdout:
            line = line.strip()
            if line:
                output_lines.append(line)
                yield line
                
                # Detect failures
                if "AudioProviderError" in line or "YT-DLP download error" in line:
                    failed_tracks.append(line)

        process.wait()
        
        # If we have failures and fallback is enabled, try alternative sources
        if failed_tracks and use_fallback:
            yield "\n⚠️ Some tracks failed. Trying alternative sources..."
            yield "🔄 Retrying with Soundcloud as audio provider..."
            
            # Retry with different audio provider
            cmd_fallback = [
                'spotdl',
                playlist_url,
                '--output', output_dir,
                '--format', audio_format,
                '--bitrate', bitrate,
                '--audio-provider', 'youtube-music',
                '--audio-provider', 'soundcloud',
                '--print-errors',
                '--overwrite', 'skip'  # Don't re-download successful tracks
            ]
            
            process_fallback = subprocess.Popen(
                cmd_fallback,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            for line in process_fallback.stdout:
                line = line.strip()
                if line:
                    yield line
            
            process_fallback.wait()
            
            # Try one more time with different settings if still failing
            remaining_failures = [f for f in failed_tracks if "AudioProviderError" in f]
            if remaining_failures:
                yield "\n🔄 Final attempt with relaxed search settings..."
                
                cmd_final = [
                    'spotdl',
                    playlist_url,
                    '--output', output_dir,
                    '--format', audio_format,
                    '--bitrate', bitrate,
                    '--audio-provider', 'youtube-music',
                    '--audio-provider', 'youtube',
                    '--audio-provider', 'soundcloud',
                    '--print-errors',
                    '--overwrite', 'skip',
                    '--search-query', '{artists} - {title}'  # Simpler search query
                ]
                
                process_final = subprocess.Popen(
                    cmd_final,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )
                
                for line in process_final.stdout:
                    line = line.strip()
                    if line:
                        yield line
                
                process_final.wait()

        return True

    except Exception as e:
        yield f"Error: {str(e)}"
        return False


# ---------------- Session State ----------------
if "playlist_tracks" not in st.session_state:
    st.session_state.playlist_tracks = []
if "playlist_name" not in st.session_state:
    st.session_state.playlist_name = ""
if "logs" not in st.session_state:
    st.session_state.logs = []


def append_log(msg):
    st.session_state.logs.append(msg)
    log_area.text("\n".join(st.session_state.logs[-30:]))


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
    else:
        st.session_state.logs = []
        append_log("🚀 Starting download process...")

        # Create temporary directory for downloads
        temp_dir = tempfile.mkdtemp()

        try:
            # Download using spotdl with fallback
            append_log(f"📥 Downloading with SpotDL (with fallback enabled: {use_fallback})...")
            status_text.text("Downloading songs...")

            download_count = 0
            for output in download_with_spotdl(playlist_url, temp_dir, audio_format, audio_quality, use_fallback):
                append_log(output)
                if "Downloaded" in output:
                    download_count += 1
                    progress_bar.progress(min(download_count / max(len(st.session_state.playlist_tracks), 1), 1.0))

            # Check if files were downloaded
            downloaded_files = list(Path(temp_dir).glob(f"*.{audio_format}"))

            if downloaded_files:
                append_log(f"\n✅ Successfully downloaded {len(downloaded_files)} songs")

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

    ### Fallback Sources:
    - **Primary**: YouTube Music (best quality)
    - **Fallback 1**: Soundcloud
    - **Fallback 2**: Regular YouTube
    - Enable "Use fallback sources" in settings for automatic retry

    ### Tips:
    - SpotDL downloads high-quality audio from multiple sources
    - Songs include proper metadata (artist, album, cover art)
    - Download speed depends on your internet connection
    - For large playlists, be patient - quality takes time!
    - If some songs fail, the tool will automatically try alternative sources

    ### Formats Available:
    - **MP3**: Best compatibility (recommended)
    - **M4A**: Good quality, smaller file size
    - **FLAC**: Lossless quality, large files
    - **OPUS/OGG**: Modern formats, good compression

    ### Troubleshooting:
    - If downloads fail, enable fallback sources in settings
    - Some region-restricted songs may not be available
    - Try lowering bitrate if downloads are slow

    ### Legal Note:
    ⚠️ This tool is for personal use only. Please respect copyright laws and terms of service.
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using Streamlit & SpotDL | Multi-source download: YouTube Music, Soundcloud, YouTube")
