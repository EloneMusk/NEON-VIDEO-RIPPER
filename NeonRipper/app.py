from flask import Flask, render_template, request, session, send_file, redirect, url_for
from flask_socketio import SocketIO, emit
import yt_dlp as youtube_dlp
import os
import certifi
from datetime import timedelta
import threading

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.permanent_session_lifetime = timedelta(minutes=15)
socketio = SocketIO(app)

active_downloads = {}

def get_resolutions(url):
    try:
        ydl_opts = {
            'cafile': certifi.where(),
            'quiet': False,
            'no_warnings': False,
            'format': 'best',
            'youtube_include_dash_manifest': True
        }
        
        with youtube_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            
            resolutions = {}
            for f in formats:
                if f.get('height'):
                    res = f"{f['height']}p"
                    if res not in resolutions or f.get('tbr', 0) > resolutions[res].get('tbr', 0):
                        resolutions[res] = {
                            'format_id': f['format_id'],
                            'ext': f['ext'],
                            'tbr': f.get('tbr', 0)
                        }
            
            sorted_res = sorted(
                resolutions.items(),
                key=lambda x: int(x[0].replace('p', '')),
                reverse=True
            )
            return sorted_res, None
        
    except Exception as e:
        return None, str(e)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form['url']
        session.permanent = True
        session['url'] = url
        
        resolutions, error = get_resolutions(url)
        if error:
            return render_template('index.html', error=error)
        
        return render_template('resolutions.html', 
                             resolutions=resolutions,
                             video_url=url)
    
    return render_template('index.html')

def progress_hook(d):
    if 'status' in d:
        download_id = threading.get_ident()
        if d['status'] == 'downloading':
            try:
                downloaded = float(d.get('downloaded_bytes', 0))
                total = float(d.get('total_bytes', 0))
                progress = (downloaded / total) * 100 if total > 0 else 0
                speed = d.get('speed', 0)
                eta = d.get('eta', 0)
                
                # Convert bytes to appropriate units
                def format_size(bytes):
                    for unit in ['B', 'KB', 'MB', 'GB']:
                        if bytes < 1024:
                            return f"{bytes:.1f} {unit}"
                        bytes /= 1024
                    return f"{bytes:.1f} GB"
                
                # Format speed
                speed_str = f"{format_size(speed)}/s" if speed else "calculating..."
                
                # Format ETA
                if eta and eta > 0:
                    minutes, seconds = divmod(eta, 60)
                    hours, minutes = divmod(minutes, 60)
                    if hours > 0:
                        eta_str = f"{hours}h {minutes}m {seconds}s"
                    elif minutes > 0:
                        eta_str = f"{minutes}m {seconds}s"
                    else:
                        eta_str = f"{seconds}s"
                else:
                    eta_str = "calculating..."
                
                socketio.emit('download_progress', {
                    'progress': progress,
                    'downloaded': format_size(downloaded),
                    'total': format_size(total),
                    'speed': speed_str,
                    'eta': eta_str
                })
            except Exception as e:
                print(f"Error in progress_hook: {str(e)}")
                pass
        elif d['status'] == 'finished':
            socketio.emit('download_complete', {'status': 'success'})

@app.route('/download', methods=['POST'])
def download():
    url = session.get('url') or request.form.get('video_url')
    
    if not url:
        return redirect(url_for('index', error="No URL provided. Please try again."))
    
    format_id = request.form['format_id']
    file_ext = request.form['file_ext']
    download_id = threading.get_ident()
    
    def download_status_hook(d):
        if download_id not in active_downloads or not active_downloads[download_id]:
            # Clean up any partial downloads
            if 'filename' in d:
                partial_file = d['filename']
                if os.path.exists(partial_file):
                    try:
                        os.remove(partial_file)
                    except OSError:
                        pass
            raise youtube_dlp.utils.DownloadError("Download cancelled")
        if 'status' in d:
            if d['status'] == 'downloading':
                try:
                    downloaded = float(d.get('downloaded_bytes', 0))
                    total = float(d.get('total_bytes', 0))
                    progress = (downloaded / total) * 100 if total > 0 else 0
                    speed = d.get('speed', 0)
                    eta = d.get('eta', 0)
                    
                    def format_size(bytes):
                        for unit in ['B', 'KB', 'MB', 'GB']:
                            if bytes < 1024:
                                return f"{bytes:.1f} {unit}"
                            bytes /= 1024
                        return f"{bytes:.1f} GB"
                    
                    speed_str = f"{format_size(speed)}/s" if speed else "calculating..."
                    
                    if eta and eta > 0:
                        minutes, seconds = divmod(eta, 60)
                        hours, minutes = divmod(minutes, 60)
                        if hours > 0:
                            eta_str = f"{hours}h {minutes}m {seconds}s"
                        elif minutes > 0:
                            eta_str = f"{minutes}m {seconds}s"
                        else:
                            eta_str = f"{seconds}s"
                    else:
                        eta_str = "calculating..."
                    
                    socketio.emit('download_progress', {
                        'progress': progress,
                        'downloaded': format_size(downloaded),
                        'total': format_size(total),
                        'speed': speed_str,
                        'eta': eta_str
                    })
                except Exception as e:
                    print(f"Error in progress_hook: {str(e)}")
            elif d['status'] == 'finished':
                socketio.emit('download_complete', {'status': 'success'})
    
    ydl_opts = {
        'format': format_id,
        'outtmpl': f'%(title)s_{format_id}.{file_ext}',
        'cafile': certifi.where(),
        'progress_hooks': [download_status_hook],
        'concurrent_fragment_downloads': 10,
        'buffersize': 1024 * 1024,
        'http_chunk_size': 10485760,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'socket_timeout': 30,
    }
    
    active_downloads[download_id] = True
    filename = None
    
    try:
        with youtube_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
        if download_id in active_downloads and active_downloads[download_id]:
            return send_file(filename, as_attachment=True)
        else:
            if filename and os.path.exists(filename):
                os.remove(filename)
            return redirect(url_for('index', error="Download cancelled"))
    
    except youtube_dlp.utils.DownloadError as e:
        if filename and os.path.exists(filename):
            os.remove(filename)
        # Also clean up any temporary files
        try:
            temp_dir = os.path.dirname(filename) if filename else '.'
            for f in os.listdir(temp_dir):
                if f.endswith('.part') or f.endswith('.ytdl'):
                    os.remove(os.path.join(temp_dir, f))
        except OSError:
            pass
        return redirect(url_for('index', error="Download cancelled"))
    except Exception as e:
        if filename and os.path.exists(filename):
            os.remove(filename)
        return redirect(url_for('index', error=f"Download error: {str(e)}"))
    finally:
        if download_id in active_downloads:
            del active_downloads[download_id]

@socketio.on('connect')
def handle_connect():
    download_id = threading.get_ident()
    active_downloads[download_id] = True

@socketio.on('cancel_download')
def handle_cancel():
    # Get all active download threads
    for download_id in list(active_downloads.keys()):
        # Set the flag to False to trigger cancellation for all active downloads
        active_downloads[download_id] = False
    emit('download_cancelled')

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8000)