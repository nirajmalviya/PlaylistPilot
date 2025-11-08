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

# ---------------- CONFIG ----------------
SPOTIFY_CLIENT_ID = "b8d625c4e9ea44ef977009c72398f32e"
SPOTIFY_CLIENT_SECRET = "2b82b875364d4616b7476197e7c2c156"

st.set_page_config(page_title="Spotify Playlist Downloader", layout="wide")
st.title("🎵 Spotify Playlist Downloader")

st.markdown("""
Download your favorite Spotify playlists using **yt-dlp** (no FFmpeg required for some formats):
1. Paste your Spotify playlist URL
2. Click **Fetch Playlist** to see the songs
3. Click **Download All** to download songs

**Requirements**: `pip install yt-dlp`
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
    st.error("⚠️ yt-dlp is not installed! Please run: `pip install yt-dlp`")
    st.code("pip install yt-dlp")
    st.stop()

# ---------------- UI inputs ----------------
playlist_url = st.text_input(
    "Spotify Playlist URL",
    placeholder="https://open.spotify.com/playlist/37i9dQZF1E38Nuyz9Gc1Wd"
)

with st.expander("⚙️ Download Settings"):
    audio_format = st.selectbox(
        "Audio Format", 
        ["m4a", "opus", "mp3"],
        help="m4a and opus don't require FFmpeg. mp3 requires FFmpeg."
    )
    audio_quality = st.selectbox("Quality", ["best", "192", "128"], index=0)

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


# ---------------- yt-dlp Download Function ----------------
def download_track_ytdlp(track_name, artist_name, output_dir, audio_format="m4a", quality="best"):
    """Download a single track using yt-dlp."""
    search_query = f"ytsearch1:{artist_name} {track_name} audio"
    
    # Build yt-dlp command
    output_template = os.path.join(output_dir, f"{artist_name} - {track_name}.%(ext)s")
    
    # Format options based on selection
    if audio_format == "m4a":
        format_arg = "bestaudio[ext=m4a]/bestaudio"
    elif audio_format == "opus":
        format_arg = "bestaudio[ext=webm]/bestaudio"
    elif audio_format == "mp3":
        format_arg = "bestaudio"
    else:
        format_arg = "bestaudio"
    
    cmd = [
        'yt-dlp',
        '-f', format_arg,
        '-o', output_template,
        '--no-playlist',
        '--quiet',
        '--no-warnings',
        '--extract-audio',
    ]
    
    # Add post-processing for mp3 (requires FFmpeg)
    if audio_format == "mp3":
        cmd.extend([
            '--audio-format', 'mp3',
            '--audio-quality', quality if quality != "best" else "0"
        ])
    
    cmd.append(search_query)
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Timeout"
    except Exception as e:
        return False, "", str(e)


def download_playlist_ytdlp(tracks, output_dir, audio_format="m4a", quality="best", max_songs=0):
    """Download multiple tracks."""
    downloaded = 0
    failed = []
    
    tracks_to_download = tracks[:max_songs] if max_songs > 0 else tracks
    
    for idx, track in enumerate(tracks_to_download, 1):
        track_name = track["name"]
        artist_name = track["artists"]
        
        yield f"[{idx}/{len(tracks_to_download)}] Downloading: {artist_name} - {track_name}"
        
        success, stdout, stderr = download_track_ytdlp(
            track_name, artist_name, output_dir, audio_format, quality
        )
        
        if success:
            yield f"✅ Downloaded: {track_name}"
            downloaded += 1
        else:
            yield f"❌ Failed: {track_name} - {stderr[:100]}"
            failed.append(f"{artist_name} - {track_name}")
        
        yield f"Progress: {downloaded}/{len(tracks_to_download)} successful"
    
    yield f"\n🎉 Download complete! {downloaded}/{len(tracks_to_download)} tracks downloaded"
    if failed:
        yield f"⚠️ Failed tracks: {len(failed)}"


# ---------------- Session State ----------------
if "playlist_tracks" not in st.session_state:
    st.session_state.playlist_tracks = []
if "playlist_name" not in st.session_state:
    st.session_state.playlist_name = ""
if "logs" not in st.session_state:
    st.session_state.logs = []


def append_log(msg):
    st.session_state.logs.append(msg)
    log_area.text("\n".join(st.session_state.logs[-40:]))


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
            # Download using yt-dlp
            append_log(f"📥 Downloading with yt-dlp...")
            status_text.text("Downloading songs...")

            download_count = 0
            total_tracks = len(st.session_state.playlist_tracks)
            
            for output in download_playlist_ytdlp(
                st.session_state.playlist_tracks, 
                temp_dir, 
                audio_format,
                audio_quality
            ):
                append_log(output)
                if "✅ Downloaded:" in output:
                    download_count += 1
                    progress_bar.progress(min(download_count / max(total_tracks, 1), 1.0))

            # Check if files were downloaded
            downloaded_files = list(Path(temp_dir).glob(f"*.{audio_format}"))
            
            # Also check for webm if opus was selected
            if audio_format == "opus" and not downloaded_files:
                downloaded_files = list(Path(temp_dir).glob("*.webm"))

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

    Install yt-dlp (lightweight, no FFmpeg needed for m4a/opus):
    ```bash
    pip install yt-dlp
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
       - Click "Download ZIP File" to save

    ### Format Guide:
    - **M4A** (Recommended): High quality, no FFmpeg needed, works everywhere
    - **OPUS**: Best compression, no FFmpeg needed, modern format
    - **MP3**: Universal compatibility, requires FFmpeg

    ### How it works:
    - Searches YouTube for each track (artist + song name)
    - Downloads best available audio quality
    - Packages everything into a convenient ZIP file

    ### Tips:
    - M4A format works without FFmpeg installation
    - For large playlists, be patient
    - Some songs may fail if not found on YouTube
    - Downloads are for personal use only

    ### Legal Note:
    ⚠️ This tool is for personal use only. Please respect copyright laws.
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using Streamlit & yt-dlp | Powered by Spotify API & YouTube")
