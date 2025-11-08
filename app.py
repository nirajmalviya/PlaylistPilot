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
Download your favorite Spotify playlists with multiple fallback methods:
1. Paste your Spotify playlist URL
2. Click **Fetch Playlist** to see the songs
3. Click **Download All** to download songs to your device

**Features**: Automatic fallback to alternative sources when YouTube Music fails
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

st.write("Debug: ffmpeg path ->", ffmpeg_exe)
st.write("Debug: spotdl available ->", spotdl_installed)

if not spotdl_installed:
    st.error("⚠️ SpotDL is not installed! Please add `spotdl>=4.2.5` to requirements.txt and redeploy.")
    st.stop()

if not ffmpeg_exe:
    st.error("⚠️ FFmpeg not found. Add `ffmpeg` to apt.txt or `imageio-ffmpeg` to requirements.txt.")
    st.stop()

# ---------------- UI inputs ----------------
playlist_url = st.text_input(
    "Spotify Playlist URL",
    placeholder="https://open.spotify.com/playlist/37i9dQZF1E38Nuyz9Gc1Wd"
)

with st.expander("⚙️ Download Settings"):
    audio_format = st.selectbox("Audio Format", ["mp3", "m4a", "flac", "opus", "ogg"])
    audio_quality = st.selectbox("Bitrate", ["128k", "192k", "256k", "320k"])
    
    # Provider selection with multiple options
    audio_provider = st.selectbox(
        "Audio Provider (Primary)",
        ["youtube", "youtube-music", "soundcloud", "bandcamp", "slider-kz"],
        help="YouTube (not YouTube Music) is most reliable for avoiding 403 errors"
    )
    
    use_fallback = st.checkbox(
        "Enable Automatic Fallback", 
        value=True,
        help="Automatically try alternative sources when primary fails"
    )
    
    embed_metadata = st.checkbox(
        "Embed Album Covers & Metadata",
        value=True,
        help="Add album artwork and complete metadata to downloaded songs"
    )
    
    use_cookies = st.checkbox(
        "Use Browser Cookies (Fix 403 errors)",
        value=False,
        help="Uses your browser cookies to bypass 403 errors. Requires cookies.txt file."
    )
    
    max_songs = st.number_input("Maximum songs to download (0 = all)", 0, 100, 0)

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
def download_with_spotdl_fallback(playlist_url, output_dir, audio_format="mp3", bitrate="320k", 
                                   ffmpeg_path=None, primary_provider="youtube-music", enable_fallback=True,
                                   embed_metadata=True, use_cookies=False):
    """
    Download playlist using spotdl with automatic fallback to alternative providers.
    """
    providers = [primary_provider]
    
    # Add fallback providers if enabled
    if enable_fallback:
        all_providers = ["youtube", "youtube-music", "soundcloud", "slider-kz", "bandcamp"]
        providers.extend([p for p in all_providers if p != primary_provider])
    
    for provider_idx, provider in enumerate(providers):
        try:
            yield f"\n{'='*60}"
            yield f"🔄 Attempting download with provider: {provider.upper()}"
            yield f"{'='*60}\n"
            
            cmd = [
                sys.executable, "-m", "spotdl",
                playlist_url,
                "--output", output_dir,
                "--format", audio_format,
                "--bitrate", bitrate,
                "--audio-provider", provider,
                "--print-errors",
            ]

            if ffmpeg_path:
                cmd.extend(["--ffmpeg", ffmpeg_path])
            
            # Add metadata embedding options
            if embed_metadata:
                cmd.extend([
                    "--generate-lrc", "False",
                    "--overwrite", "skip",
                ])
            
            # Add cookie support to bypass 403 errors
            if use_cookies:
                cookies_path = os.path.join(os.getcwd(), "cookies.txt")
                if os.path.exists(cookies_path):
                    cmd.extend(["--cookie-file", cookies_path])
                    yield f"🍪 Using cookies from: {cookies_path}"
                else:
                    yield f"⚠️ cookies.txt not found, proceeding without cookies"
            
            # Add retry and timeout options for 403 errors
            cmd.extend([
                "--threads", "1",  # Single thread to avoid rate limiting
            ])

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            output_lines = []
            download_success = False
            
            for line in process.stdout:
                line = line.strip()
                if line:
                    output_lines.append(line)
                    yield line
                    
                    # Check for successful downloads
                    if "Downloaded" in line or "has been downloaded" in line:
                        download_success = True

            process.wait()
            
            # Check if any files were downloaded
            downloaded_files = list(Path(output_dir).glob(f"*.{audio_format}"))
            
            if downloaded_files and len(downloaded_files) > 0:
                yield f"\n✅ Successfully downloaded {len(downloaded_files)} songs with {provider}!"
                return True
            
            # If this was not the last provider and no files were downloaded, try next
            if provider_idx < len(providers) - 1:
                yield f"\n⚠️ No files downloaded with {provider}, trying next provider..."
                time.sleep(2)  # Brief pause before trying next provider
            else:
                yield f"\n❌ All providers exhausted. No files downloaded."
                return False

        except Exception as e:
            yield f"\n❌ Error with provider {provider}: {str(e)}"
            if provider_idx < len(providers) - 1:
                yield f"Trying next provider..."
            continue
    
    return False


def download_individual_tracks_with_fallback(tracks, output_dir, audio_format="mp3", bitrate="320k",
                                              ffmpeg_path=None, primary_provider="youtube-music", enable_fallback=True,
                                              embed_metadata=True, use_cookies=False):
    """
    Download tracks individually with fallback support for failed tracks.
    """
    providers = [primary_provider]
    if enable_fallback:
        all_providers = ["youtube", "youtube-music", "soundcloud", "slider-kz", "bandcamp"]
        providers.extend([p for p in all_providers if p != primary_provider])
    
    successful_downloads = []
    failed_tracks = []
    
    for idx, track in enumerate(tracks, 1):
        track_url = track.get("spotify_url", "")
        track_name = f"{track.get('artists', 'Unknown')} - {track.get('name', 'Unknown')}"
        
        yield f"\n{'='*60}"
        yield f"📝 Track {idx}/{len(tracks)}: {track_name}"
        yield f"{'='*60}"
        
        downloaded = False
        
        for provider in providers:
            try:
                yield f"\n🔄 Trying provider: {provider.upper()}"
                
                cmd = [
                    sys.executable, "-m", "spotdl",
                    track_url,
                    "--output", output_dir,
                    "--format", audio_format,
                    "--bitrate", bitrate,
                    "--audio-provider", provider,
                    "--print-errors",
                    "--threads", "1",
                ]

                if ffmpeg_path:
                    cmd.extend(["--ffmpeg", ffmpeg_path])
                
                if embed_metadata:
                    cmd.extend([
                        "--generate-lrc", "False",
                        "--overwrite", "skip",
                    ])
                
                if use_cookies:
                    cookies_path = os.path.join(os.getcwd(), "cookies.txt")
                    if os.path.exists(cookies_path):
                        cmd.extend(["--cookie-file", cookies_path])

                process = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120  # 2 minute timeout per track
                )
                
                # Check if file was downloaded
                existing_files = list(Path(output_dir).glob(f"*.{audio_format}"))
                if len(existing_files) > len(successful_downloads):
                    yield f"✅ Downloaded successfully with {provider}!"
                    successful_downloads.append(track_name)
                    downloaded = True
                    break
                else:
                    yield f"⚠️ Failed with {provider}, trying next..."
                    
            except subprocess.TimeoutExpired:
                yield f"⏱️ Timeout with {provider}, trying next..."
            except Exception as e:
                yield f"❌ Error with {provider}: {str(e)}"
        
        if not downloaded:
            yield f"❌ Failed to download: {track_name}"
            failed_tracks.append(track_name)
        
        # Small delay between tracks to avoid rate limiting
        time.sleep(1)
    
    yield f"\n{'='*60}"
    yield f"📊 DOWNLOAD SUMMARY"
    yield f"{'='*60}"
    yield f"✅ Successful: {len(successful_downloads)}/{len(tracks)}"
    yield f"❌ Failed: {len(failed_tracks)}/{len(tracks)}"
    
    if failed_tracks:
        yield f"\n⚠️ Failed tracks:"
        for track in failed_tracks:
            yield f"  - {track}"
    
    return len(successful_downloads) > 0


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

                    df = pd.DataFrame(tracks)
                    st.dataframe(
                        df[["name", "artists", "album"]],
                        use_container_width=True,
                        height=400
                    )

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
        append_log("🚀 Starting enhanced download process with fallback support...")

        temp_dir = tempfile.mkdtemp()

        try:
            append_log(f"📥 Downloading with SpotDL (Provider: {audio_provider})...")
            if use_fallback:
                append_log("✨ Automatic fallback enabled - will try alternative sources if needed")
            
            status_text.text("Downloading songs with fallback support...")

            # First try: Bulk download
            download_count = 0
            bulk_success = False
            
            for output in download_with_spotdl_fallback(
                playlist_url, temp_dir, audio_format, audio_quality, 
                ffmpeg_path=ffmpeg_exe, primary_provider=audio_provider, 
                enable_fallback=use_fallback, embed_metadata=embed_metadata,
                use_cookies=use_cookies
            ):
                append_log(output)
                if "Downloaded" in output or "has been downloaded" in output:
                    download_count += 1
                    progress = min(download_count / max(len(st.session_state.playlist_tracks), 1), 1.0)
                    progress_bar.progress(progress)

            downloaded_files = list(Path(temp_dir).glob(f"*.{audio_format}"))
            
            # Second try: If bulk download failed, try individual tracks
            if not downloaded_files and st.session_state.playlist_tracks:
                append_log("\n⚠️ Bulk download failed. Switching to individual track download mode...")
                append_log("This may take longer but has higher success rate.\n")
                status_text.text("Downloading tracks individually...")
                
                tracks_to_download = st.session_state.playlist_tracks
                if max_songs > 0:
                    tracks_to_download = tracks_to_download[:max_songs]
                
                download_count = 0
                for output in download_individual_tracks_with_fallback(
                    tracks_to_download, temp_dir, audio_format, audio_quality,
                    ffmpeg_path=ffmpeg_exe, primary_provider=audio_provider,
                    enable_fallback=use_fallback, embed_metadata=embed_metadata,
                    use_cookies=use_cookies
                ):
                    append_log(output)
                    if "Downloaded successfully" in output:
                        download_count += 1
                        progress = min(download_count / len(tracks_to_download), 1.0)
                        progress_bar.progress(progress)
                
                downloaded_files = list(Path(temp_dir).glob(f"*.{audio_format}"))

            if downloaded_files:
                append_log(f"\n✅ Successfully downloaded {len(downloaded_files)} songs")

                append_log("📦 Creating ZIP file...")
                zip_buffer = BytesIO()

                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for file_path in downloaded_files:
                        zip_file.write(file_path, file_path.name)

                zip_buffer.seek(0)

                playlist_name_safe = "".join(
                    c for c in st.session_state.playlist_name if c.isalnum() or c in (' ', '-', '_'))
                if not playlist_name_safe:
                    playlist_name_safe = "playlist"
                zip_filename = f"{playlist_name_safe}_songs.zip"

                st.success(f"🎉 Downloaded {len(downloaded_files)} songs!")

                st.download_button(
                    label=f"📦 Download ZIP File ({len(downloaded_files)} songs)",
                    data=zip_buffer.getvalue(),
                    file_name=zip_filename,
                    mime="application/zip",
                    use_container_width=True
                )

                st.info(f"💾 Click the button above to download all songs as a ZIP file")
            else:
                st.error("❌ No songs were downloaded. Please try the following:")
                
                with st.expander("🔧 Troubleshooting Steps", expanded=True):
                    st.markdown("""
                    ### Try these fixes:
                    
                    1. **Change Provider to YouTube (not YouTube Music)**
                       - YouTube Music often blocks requests
                       - Regular YouTube is more reliable
                    
                    2. **Enable Browser Cookies**
                       - Export cookies from YouTube (see instructions below)
                       - Save as `cookies.txt` in app folder
                       - Enable "Use Browser Cookies" option
                    
                    3. **Check SpotDL Installation**
                       ```bash
                       pip install spotdl --upgrade
                       pip install yt-dlp --upgrade
                       ```
                    
                    4. **Test with a smaller playlist**
                       - Try with just 1-2 songs first
                       - Some playlists may have region-restricted songs
                    
                    5. **Check Internet Connection**
                       - Make sure you can access YouTube
                       - Try disabling VPN if using one
                    
                    ### How to export cookies:
                    1. Install browser extension: "Get cookies.txt LOCALLY"
                    2. Visit youtube.com and login
                    3. Click extension icon → Export
                    4. Save as `cookies.txt`
                    5. Place in same folder as this app
                    """)
                
                st.warning("Check the logs above for specific error messages.")

        except Exception as e:
            st.error(f"❌ Error during download: {e}")
            append_log(f"Error: {str(e)}")

        finally:
            try:
                shutil.rmtree(temp_dir)
            except:
                pass

            progress_bar.progress(1.0)

# ---------------- Help Section ----------------
with st.expander("💡 How to Use"):
    st.markdown("""
    ### Installation:

    ```bash
    pip install spotdl
    ```

    ### Fixing 403 Errors:

    **Method 1: Use Browser Cookies (Recommended)**
    1. Install a browser extension to export cookies:
       - Chrome/Edge: "[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)"
       - Firefox: "[cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)"
    2. Visit YouTube Music and log in
    3. Export cookies to `cookies.txt` file
    4. Place `cookies.txt` in the same folder as this app
    5. Enable "Use Browser Cookies" option above
    
    **Method 2: Try Different Providers**
    - YouTube Music often has 403 errors
    - Try "YouTube" or "SoundCloud" as primary provider
    - Enable automatic fallback for best results

    ### Fixing Missing Album Covers:

    **The app now automatically:**
    - ✅ Downloads album artwork from Spotify
    - ✅ Embeds covers into MP3/M4A files
    - ✅ Adds complete metadata (artist, album, year, etc.)
    
    **Make sure "Embed Album Covers & Metadata" is enabled** ☑️

    ### Features:

    **🔄 Automatic Fallback**: When one provider fails, tries:
    - YouTube (regular)
    - SoundCloud
    - Bandcamp
    - Slider.kz

    ### Steps:

    1. **Get Playlist URL**: Copy your Spotify playlist link
    2. **Configure Settings**: 
       - Choose format and bitrate
       - Select primary provider
       - Enable fallback mode ✅
       - Enable metadata embedding ✅
       - Enable cookies if needed ✅
    3. **Download**: Click "Download All" and wait
    4. **Get ZIP**: Download the ZIP file with all songs

    ### Provider Recommendations:
    - **YouTube**: Most reliable, best for avoiding 403 errors
    - **YouTube Music**: High quality but may have 403 errors
    - **SoundCloud**: Good for indie/electronic music
    - **Bandcamp**: Independent artists
    - **Slider.kz**: Alternative source

    ### Troubleshooting:

    **403 Forbidden Errors:**
    - Use browser cookies (see Method 1 above)
    - Switch to regular "YouTube" provider
    - Enable automatic fallback
    - Wait a few minutes if rate-limited

    **Missing Album Covers:**
    - Ensure "Embed Album Covers & Metadata" is enabled
    - Some songs may not have covers in Spotify database
    - MP3 and M4A formats work best for metadata

    **Slow Downloads:**
    - Downloads run one at a time to avoid rate limits
    - Large playlists take time - be patient
    - Each song typically takes 30-60 seconds

    ### Legal Note:
    ⚠️ For personal use only. Respect copyright laws and terms of service.
    """)

st.markdown("---")
st.markdown("Made with ❤️ using Streamlit & SpotDL | Multi-provider fallback + metadata embedding")
