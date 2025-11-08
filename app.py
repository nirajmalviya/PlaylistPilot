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
    Strategy:
      1) check system PATH via shutil.which
      2) fallback to imageio-ffmpeg.get_ffmpeg_exe() if available
    """
    ff = _shutil.which("ffmpeg")
    if ff:
        return ff

    # fallback to imageio-ffmpeg (downloads a binary into cache)
    if iio_ffmpeg is not None:
        try:
            ff_exe = iio_ffmpeg.get_ffmpeg_exe()
            ff_dir = os.path.dirname(ff_exe)
            # Prepend to PATH so other checks find it
            os.environ["PATH"] = ff_dir + os.pathsep + os.environ.get("PATH", "")
            # confirm which now
            if _shutil.which("ffmpeg") is None:
                # If which still returns None, use explicit path
                return ff_exe
            return _shutil.which("ffmpeg") or ff_exe
        except Exception as e:
            print("imageio-ffmpeg failed:", e)
            return None
    return None


def is_spotdl_available():
    """Return True if spotdl module/CLI is available."""
    # prefer python -m spotdl check to avoid reliance on shell PATH
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
    audio_quality = st.selectbox("Bitrate", ["320k", "256k", "192k", "128k"], index=0)
    max_songs = st.number_input("Maximum songs to download (0 = all)", 0, 100, 0)
    use_cookies = st.checkbox("Use YouTube cookies (helps with blocked content)", value=False)
    retry_failed = st.checkbox("Retry failed downloads", value=True)
    
    if use_cookies:
        st.info("📝 To use cookies: Export your YouTube cookies using a browser extension like 'Get cookies.txt LOCALLY' and upload the cookies.txt file to the app directory.")
        cookies_file = st.file_uploader("Upload cookies.txt file", type=['txt'])
        if cookies_file:
            with open("cookies.txt", "wb") as f:
                f.write(cookies_file.getvalue())
            st.success("✅ Cookies file uploaded successfully!")

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


# ---------------- SpotDL Download Function ----------------
def download_with_spotdl(playlist_url, output_dir, audio_format="mp3", bitrate="320k", ffmpeg_path=None, use_cookies=False):
    """Download playlist using spotdl command called as a Python module."""
    try:
        # Use python -m spotdl to avoid shell PATH issues and pass explicit ffmpeg path
        cmd = [
            sys.executable, "-m", "spotdl",
            playlist_url,
            "--output", output_dir,
            "--format", audio_format,
            "--bitrate", bitrate,
            "--print-errors",
            "--threads", "4",  # Parallel downloads
            "--audio-provider", "youtube-music",
        ]

        # Add cookie file if available
        if use_cookies and os.path.exists("cookies.txt"):
            cmd.extend(["--cookie-file", "cookies.txt"])

        if ffmpeg_path:
            cmd.extend(["--ffmpeg", ffmpeg_path])

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        output_lines = []
        for line in process.stdout:
            line = line.strip()
            if line:
                output_lines.append(line)
                yield line

        process.wait()
        return process.returncode == 0

    except Exception as e:
        yield f"Error: {str(e)}"
        return False


def download_individual_track(track_url, output_dir, audio_format="mp3", bitrate="320k", ffmpeg_path=None, use_cookies=False):
    """Download a single track with spotdl."""
    try:
        cmd = [
            sys.executable, "-m", "spotdl",
            track_url,
            "--output", output_dir,
            "--format", audio_format,
            "--bitrate", bitrate,
            "--audio-provider", "youtube-music",
        ]

        if use_cookies and os.path.exists("cookies.txt"):
            cmd.extend(["--cookie-file", "cookies.txt"])

        if ffmpeg_path:
            cmd.extend(["--ffmpeg", ffmpeg_path])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180
        )

        return result.returncode == 0, result.stdout + result.stderr

    except Exception as e:
        return False, str(e)


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

        # Create temporary directory for downloads
        temp_dir = tempfile.mkdtemp()

        try:
            # Download using spotdl
            append_log(f"📥 Downloading with SpotDL...")
            if use_cookies and os.path.exists("cookies.txt"):
                append_log("🍪 Using YouTube cookies for authentication")
            
            status_text.text("Downloading songs...")

            download_count = 0
            failed_count = 0
            failed_tracks = []
            
            for output in download_with_spotdl(playlist_url, temp_dir, audio_format, audio_quality, ffmpeg_path=ffmpeg_exe, use_cookies=use_cookies):
                append_log(output)
                
                # Track successful downloads
                if "Downloaded" in output or "has been downloaded" in output:
                    download_count += 1
                    if st.session_state.playlist_tracks:
                        progress_bar.progress(min(download_count / len(st.session_state.playlist_tracks), 1.0))
                
                # Track failures
                elif "Error" in output or "ERROR" in output:
                    failed_count += 1
                    # Extract track name if possible
                    if "AudioProviderError" in output or "YT-DLP" in output:
                        failed_tracks.append(output)

            # Check if files were downloaded
            downloaded_files = list(Path(temp_dir).glob(f"*.{audio_format}"))

            if downloaded_files:
                append_log(f"\n✅ Successfully downloaded {len(downloaded_files)} songs")
                
                if failed_count > 0:
                    append_log(f"⚠️ Failed to download {failed_count} songs")
                    append_log("💡 Tip: Enable 'Use YouTube cookies' in settings to fix most download issues")
                    
                    if retry_failed and failed_tracks and st.session_state.playlist_tracks:
                        append_log("\n🔄 Retrying failed downloads individually...")
                        
                        for track in st.session_state.playlist_tracks:
                            track_url = track.get("spotify_url")
                            if track_url:
                                # Check if this track was downloaded
                                track_name_safe = re.sub(r'[^\w\s-]', '', track.get("name", ""))
                                existing = list(Path(temp_dir).glob(f"*{track_name_safe[:20]}*.{audio_format}"))
                                
                                if not existing:
                                    append_log(f"Retrying: {track.get('name')} - {track.get('artists')}")
                                    success, output = download_individual_track(
                                        track_url, temp_dir, audio_format, audio_quality, 
                                        ffmpeg_path=ffmpeg_exe, use_cookies=use_cookies
                                    )
                                    if success:
                                        append_log(f"✅ Successfully downloaded on retry")
                                        download_count += 1
                                    else:
                                        append_log(f"❌ Still failed: {track.get('name')}")
                                    
                                    time.sleep(2)  # Rate limiting
                        
                        # Recount files after retry
                        downloaded_files = list(Path(temp_dir).glob(f"*.{audio_format}"))

                # Create ZIP file with minimal compression for speed
                append_log("📦 Creating ZIP file (fast mode)...")
                status_text.text("Creating ZIP file...")
                
                zip_buffer = BytesIO()

                # Use ZIP_STORED (no compression) for maximum speed
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
                
                # Show failed tracks summary
                if failed_count > 0:
                    with st.expander(f"⚠️ View {failed_count} Failed Downloads"):
                        st.markdown("**These songs could not be downloaded:**")
                        st.markdown("Common reasons: Geo-restrictions, removed content, or YouTube rate limiting")
                        for fail_msg in failed_tracks[:20]:  # Show first 20
                            st.text(fail_msg)
                            
            else:
                st.error("❌ No songs were downloaded. Check the logs above for errors.")
                st.markdown("""
                **Common Solutions:**
                1. ✅ Enable "Use YouTube cookies" and upload your cookies.txt file
                2. 🔄 Update spotdl: `pip install --upgrade spotdl yt-dlp`
                3. 🌍 Try using a VPN (especially for regional content)
                4. ⏰ Wait 10-15 minutes if you've been downloading a lot (rate limiting)
                5. 📝 Try downloading individual songs instead of the entire playlist
                """)

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
    pip install spotdl yt-dlp
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

    ### Tips:
    - SpotDL downloads high-quality audio from YouTube Music
    - Songs include proper metadata (artist, album, cover art)
    - Download speed depends on your internet connection
    - For large playlists, be patient - quality takes time! 
    - Default bitrate is 320k (highest quality MP3)

    ### 🔧 Troubleshooting Hindi/Regional Songs:
    
    **If you get "YT-DLP download error" or "AudioProviderError":**
    
    1. **Use YouTube Cookies** (Most Effective - 90% success rate):
       - Install browser extension: **"Get cookies.txt LOCALLY"** ([Chrome](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) / [Firefox](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/))
       - Go to youtube.com or music.youtube.com
       - **Log in to your Google account**
       - Click the extension icon and download cookies.txt
       - In the app, enable "Use YouTube cookies" checkbox
       - Upload your cookies.txt file
       - Try downloading again
    
    2. **Update SpotDL and yt-dlp**:
       ```bash
       pip install --upgrade spotdl yt-dlp
       ```
    
    3. **Geo-Restrictions**:
       - Some Hindi/regional content may be geo-blocked outside India
       - Use a VPN connected to India for Indian content
       - Use cookies from a logged-in YouTube account
    
    4. **Check Content Availability**:
       - Open the YouTube Music link manually
       - If the video doesn't play, it's removed/restricted
       - Try searching for alternate versions of the song
    
    5. **Rate Limiting**:
       - YouTube temporarily blocks too many requests
       - Wait 10-15 minutes and try again
       - Download in smaller batches (10-20 songs at a time)
       - Enable "Retry failed downloads" option
    
    6. **Individual Track Download**:
       - If a specific song fails, try downloading it separately
       - Use the Spotify track URL instead of playlist URL
       - Example: `https://open.spotify.com/track/TRACK_ID`

    ### Formats Available:
    - **MP3**: Best compatibility (recommended)
    - **M4A**: Good quality, smaller file size
    - **FLAC**: Lossless quality, large files
    - **OPUS/OGG**: Modern formats, good compression

    ### Why Cookies Work:
    - YouTube treats logged-in users more favorably
    - Reduces bot detection and captcha challenges
    - Bypasses some geo-restrictions
    - Increases download success rate significantly

    ### Legal Note:
    ⚠️ This tool is for personal use only. Please respect copyright laws and terms of service.
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using Streamlit & SpotDL | Powered by Spotify API & YouTube Music")
st.markdown("💡 **Pro Tip**: For best results with Hindi/regional songs, use YouTube cookies!")
