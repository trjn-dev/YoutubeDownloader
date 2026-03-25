import yt_dlp
import os
import shutil
import sys

# ==========================================
# CONFIGURATION
# ==========================================
def get_documents_folder():
    """Default download location: user's Documents (Windows: known folder; else ~/Documents)."""
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(261)
            # CSIDL_PERSONAL (My Documents)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
                path = buf.value
                if path and os.path.isdir(path):
                    return path
        except Exception:
            pass
    path = os.path.join(os.path.expanduser("~"), "Documents")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


DOWNLOAD_FOLDER = get_documents_folder()


def resource_path(relative_path):
    """Get absolute path to resource for dev or PyInstaller builds."""
    try:
        # PyInstaller extracts files to a temp folder and stores it in _MEIPASS.
        base_path = sys._MEIPASS
    except Exception:
        # Directory containing this script.
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def get_ffmpeg_location():
    """
    Return a valid ffmpeg executable path/location for yt-dlp.
    Priority:
    1) bundled ffmpeg.exe next to executable/script
    2) ffmpeg found in system PATH
    """
    bundled_ffmpeg = resource_path("ffmpeg.exe")
    if os.path.isfile(bundled_ffmpeg):
        return bundled_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    return None

def clean_cache():
    """
    Forces yt-dlp to clear its internal cache.
    """
    print("Cleaning yt-dlp cache to remove failed session data...")
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            ydl.cache.remove()
        print("✅ Cache cleared.\n")
    except Exception as e:
        print(f"⚠️ Could not clear cache: {e}\n")

def download_media(url, media_type, ffmpeg_path=None):
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    ffmpeg_location = ffmpeg_path if (ffmpeg_path and os.path.isfile(ffmpeg_path)) else get_ffmpeg_location()

    # Base options used for BOTH Video and Audio
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
        'extractor_args': {
            'youtube': {
                'player_client': ['android_vr', 'android']
            }
        },
        'youtube_include_dash_manifest': True,
        'quiet': False,
        'no_warnings': True,
    }
    if ffmpeg_location:
        ydl_opts['ffmpeg_location'] = ffmpeg_location

    # Add specific rules based on your choice
    if media_type == 'audio':
        print(f"\n🎧 Downloading AUDIO ONLY (MP3): {url}")
        ydl_opts.update({
            'format': 'bestaudio/best', # Grab only the best audio stream
            'postprocessors':[{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192', # High quality MP3 bitrate
            }],
        })
    else:
        print(f"\n🎬 Downloading VIDEO (MP4): {url}")
        ydl_opts.update({
            'format': 'bestvideo+bestaudio/best',
            'merge_output_format': 'mp4',
        })

    print("-" * 60)
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"\n✅ SUCCESS! Saved to: {DOWNLOAD_FOLDER}")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        print("\n--- Diagnostic: What did YouTube return? ---")
        try:
            ydl_opts['listformats'] = True
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except:
            pass

if __name__ == "__main__":
    print("Initializing VR Downloader (Video & Audio Support)...")

    ffmpeg_location = get_ffmpeg_location()
    if ffmpeg_location is None:
        print("\n🛑 CRITICAL ERROR: FFmpeg is missing!")
        print("FFmpeg is REQUIRED to convert audio to MP3 and merge HD Video.")
        print("Place ffmpeg.exe next to this script (or bundled app), or install FFmpeg in PATH.\n")
    else:
        print(f"Using FFmpeg from: {ffmpeg_location}")
    
    clean_cache()
    
    while True:
        try:
            raw_url = input("\nEnter YouTube URL (or press Enter to quit): ").strip()
            if not raw_url: break
            
            # Ask the user what they want to download
            choice = input("Download (V)ideo or (A)udio? [Default: V]: ").strip().lower()
            
            # Set media_type based on input (defaults to video if they just hit Enter)
            media_type = 'audio' if choice == 'a' else 'video'
            
            clean_url = raw_url.split('?si=')[0].split('&si=')[0]
            download_media(clean_url, media_type)
            
        except KeyboardInterrupt:
            sys.exit(0)