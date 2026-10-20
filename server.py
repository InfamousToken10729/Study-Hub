import sys
import platform
import subprocess
import importlib
import importlib.util

# --- Dependency Auto-Installer ---
# flask-cors is no longer needed: the page is served by this same server (same origin).
REQUIRED_PACKAGES = {
    'flask': 'flask',
    'flask_sqlalchemy': 'flask-sqlalchemy',
    'webview': 'pywebview',
}
if platform.system() == 'Windows':
    REQUIRED_PACKAGES['clr'] = 'pythonnet'   # only needed by pywebview on Windows


def ensure_dependencies():
    # Inside a packaged .exe, sys.executable is the app itself, not Python, so pip can't run.
    if getattr(sys, 'frozen', False):
        return
    missing = [pip for mod, pip in REQUIRED_PACKAGES.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"Missing required packages: {', '.join(missing)}")
    print("Installing dependencies...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
        importlib.invalidate_caches()
        print("All packages installed successfully!\n")
    except subprocess.CalledProcessError as e:
        print(f"Failed to install packages automatically: {e}")
        print(f"Try manually:  {sys.executable} -m pip install {' '.join(missing)}")
        sys.exit(1)


ensure_dependencies()

# --- Main Application Imports ---
import os
import re
import json
import time
import glob
import socket
import shutil
import threading
import webbrowser
from datetime import datetime
from urllib.parse import urlparse

import webview
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename

# --- Directory & Application Setup ---
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

HTML_FILE = 'graduation_study_hub.html'
DB_FILE = os.path.join(BASE_DIR, 'database.json')            # legacy JSON database
SQLITE_DB_PATH = os.path.join(BASE_DIR, 'study_hub.db')
PDF_DIR = os.path.join(BASE_DIR, 'StudyHub_Files')
BACKUP_DIR = os.path.join(BASE_DIR, 'StudyHub_Backups')
WEBVIEW_DATA_DIR = os.path.join(BASE_DIR, 'webview_data')
os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

PREFERRED_PORT = 8765        # fixed port => the window's saved settings (theme etc.) survive restarts
FULLSCREEN = True            # set to False if you prefer a normal resizable window
KEEP_BACKUPS = 14            # how many daily snapshots to keep
ALLOWED_EXT = {'.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
               '.jpg', '.jpeg', '.png', '.gif', '.txt'}
SAFE_ID = re.compile(r'^[A-Za-z0-9_\-]{1,100}$')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{SQLITE_DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024   # 1 GB per upload request

db = SQLAlchemy(app)

# Set at startup, used for the origin check below
ALLOWED_ORIGINS = set()


# --- Database Models ---
class AppState(db.Model):
    """The whole workspace (subjects, notes, tasks, flashcards, deadlines, links,
    whiteboards, activity, folders, ...) as one JSON document."""
    __tablename__ = 'app_state'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.String(40), nullable=False)


class Subject(db.Model):
    """Legacy table - only read once, to migrate old databases into AppState."""
    __tablename__ = 'subjects'

    id = db.Column(db.String(100), primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    code = db.Column(db.String(50), nullable=True)


class StudyFile(db.Model):
    """Registry of the physical files stored in StudyHub_Files."""
    __tablename__ = 'study_files'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), unique=True, nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    folder = db.Column(db.String(100), default='Lectures')
    subject_id = db.Column(db.String(100), nullable=False)

    def to_dict(self):
        return {
            "filename": self.filename,
            "original_name": self.original_name,
            "folder": self.folder,
            "subject_id": self.subject_id,
        }


# --- Helpers ---
def open_file_natively(filepath):
    """Cross-platform system file opener."""
    system_name = platform.system()
    if system_name == 'Windows':
        os.startfile(filepath)
    elif system_name == 'Darwin':
        subprocess.run(['open', filepath], check=True)
    else:
        subprocess.run(['xdg-open', filepath], check=True)


DEFAULT_FOLDERS = ["Lectures", "Tutorials", "Assignments", "Quizzes", "Labs", "References"]


def build_state_from_legacy():
    """Build a UI state from the old subjects/study_files tables (pre-fix databases)."""
    subjects = Subject.query.all()
    if not subjects:
        return None
    files = StudyFile.query.all()
    state = {"subjects": [], "pdfs": []}
    for s in subjects:
        folders = list(DEFAULT_FOLDERS)
        for f in files:
            if f.subject_id == s.id and f.folder and f.folder not in folders:
                folders.append(f.folder)
        state["subjects"].append({
            "id": s.id, "name": s.name, "icon": "fa-book-open",
            "color": "from-indigo-600 to-blue-500", "folders": folders,
        })
    for f in files:
        state["pdfs"].append({
            "id": "file_" + f.filename, "subjectId": f.subject_id,
            "original_name": f.original_name, "filename": f.filename,
            "folder": f.folder or "Lectures",
        })
    return state


def snapshot_before_overwrite(old_json):
    """Keep one snapshot per day (the state at the start of that day) + prune old ones."""
    try:
        path = os.path.join(BACKUP_DIR, f"state_{datetime.now():%Y-%m-%d}.json")
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(old_json)
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, 'state_*.json')))
        for old in backups[:-KEEP_BACKUPS]:
            os.remove(old)
    except Exception as e:
        print(f"Backup warning: {e}")


def initialize_database():
    """Create tables and migrate legacy data (database.json and the old subjects table)."""
    with app.app_context():
        db.create_all()

        # 1) legacy database.json -> legacy tables
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if content:
                    data = json.loads(content)
                    for sub_data in data.get('subjects', []):
                        if db.session.get(Subject, sub_data['id']):
                            continue
                        db.session.add(Subject(
                            id=sub_data['id'],
                            name=sub_data.get('name', 'Unnamed Subject'),
                            code=sub_data.get('code', '')))
                        for file_info in sub_data.get('files', []):
                            if StudyFile.query.filter_by(filename=file_info['filename']).first():
                                continue
                            db.session.add(StudyFile(
                                filename=file_info['filename'],
                                original_name=file_info.get('original_name', file_info['filename']),
                                folder=file_info.get('folder', 'Lectures'),
                                subject_id=sub_data['id']))
                    db.session.commit()
                os.replace(DB_FILE, f"{DB_FILE}.bak")
                print("Migrated database.json to SQLite successfully!")
            except Exception as e:
                db.session.rollback()
                print(f"Migration error: {e}")

        # 2) legacy tables -> AppState (only once)
        if db.session.get(AppState, 1) is None:
            legacy = build_state_from_legacy()
            if legacy:
                db.session.add(AppState(id=1, data=json.dumps(legacy, ensure_ascii=False),
                                        updated_at=datetime.now().isoformat()))
                db.session.commit()
                print("Migrated existing subjects/files into the new storage format.")


# --- Security: only accept requests that come from our own page ---
@app.before_request
def check_origin():
    host = request.headers.get('Host', '')
    if ALLOWED_ORIGINS and f"http://{host}" not in ALLOWED_ORIGINS:
        return jsonify({"error": "Forbidden host"}), 403
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('Origin')
        if origin and origin not in ALLOWED_ORIGINS:
            return jsonify({"error": "Forbidden origin"}), 403


# --- API Endpoints ---
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, HTML_FILE, max_age=0)


@app.route('/api/data', methods=['GET', 'POST'])
def manage_data():
    row = db.session.get(AppState, 1)

    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get('subjects'), list):
            return jsonify({"error": "Invalid data"}), 400

        if row is None:
            row = AppState(id=1, data='{}', updated_at='')
            db.session.add(row)
        elif row.data and row.data != '{}':
            snapshot_before_overwrite(row.data)

        row.data = json.dumps(data, ensure_ascii=False)
        row.updated_at = datetime.now().isoformat()
        db.session.commit()
        return jsonify({"status": "success"})

    if row is None:
        return jsonify({})
    try:
        return jsonify(json.loads(row.data))
    except (ValueError, TypeError):
        # Refuse to serve damaged data: the page then switches saving off so nothing gets overwritten.
        return jsonify({"error": "Stored data is damaged. Restore from StudyHub_Backups."}), 500


@app.route('/api/upload_file', methods=['POST'])
def upload_file():
    files = request.files.getlist('files[]')
    if not files:
        return jsonify({"error": "No files selected"}), 400

    subject_id = request.form.get('subject_id', '')
    folder_name = (request.form.get('folder') or 'Lectures')[:100]
    if not SAFE_ID.match(subject_id):
        return jsonify({"error": "Invalid subject"}), 400

    uploaded, skipped = [], []
    for file in files:
        if not file or not file.filename:
            continue
        stem, ext = os.path.splitext(file.filename)
        ext = ext.lower()
        if ext not in ALLOWED_EXT:
            skipped.append(file.filename)
            continue

        # secure_filename() deletes non-ASCII letters (e.g. Arabic names), so keep the extension separately
        safe_stem = secure_filename(stem)[:60] or 'file'
        unique_filename = f"{subject_id}_{os.urandom(4).hex()}_{safe_stem}{ext}"
        file.save(os.path.join(PDF_DIR, unique_filename))

        record = StudyFile(filename=unique_filename, original_name=file.filename,
                           folder=folder_name, subject_id=subject_id)
        db.session.add(record)
        uploaded.append(record.to_dict())

    db.session.commit()
    if not uploaded and skipped:
        return jsonify({"error": f"Unsupported file type: {', '.join(skipped)}"}), 400
    return jsonify({"status": "success", "files": uploaded, "skipped": skipped})


@app.route('/files/<path:filename>')
def serve_file(filename):
    return send_from_directory(PDF_DIR, filename, conditional=True)


@app.route('/api/delete_file/<path:filename>', methods=['DELETE'])
def delete_file(filename):
    safe_name = secure_filename(filename)
    filepath = os.path.join(PDF_DIR, safe_name)

    record = StudyFile.query.filter_by(filename=safe_name).first()
    if record:
        db.session.delete(record)
        db.session.commit()

    if os.path.isfile(filepath):
        try:
            os.remove(filepath)
        except OSError as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"status": "success"})


@app.route('/api/open_file/<path:filename>', methods=['GET'])
def open_native_file(filename):
    safe_name = secure_filename(filename)
    filepath = os.path.join(PDF_DIR, safe_name)

    if os.path.isfile(filepath):
        try:
            open_file_natively(filepath)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "File not found"}), 404


@app.route('/api/open_url', methods=['GET'])
def open_url():
    """Open a saved link in the user's real browser (window.open does not work inside the app window)."""
    url = request.args.get('url', '')
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return jsonify({"error": "Only http/https links are allowed"}), 400
    webbrowser.open(url)
    return jsonify({"status": "success"})


@app.route('/api/exit', methods=['POST'])
def exit_app():
    def _shutdown():
        try:
            for w in list(webview.windows):
                w.destroy()
        except Exception:
            pass
        time.sleep(1.5)
        os._exit(0)          # safety net if the window refuses to close

    threading.Timer(0.3, _shutdown).start()   # let the HTTP response go out first
    return jsonify({"status": "exiting"})


# --- Server / Window startup ---
def pick_port():
    for port in (PREFERRED_PORT, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port available")


def wait_for_server(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


if __name__ == '__main__':
    initialize_database()

    port = pick_port()
    ALLOWED_ORIGINS.update({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})

    threading.Thread(
        target=lambda: app.run(host='127.0.0.1', port=port, threaded=True, use_reloader=False),
        daemon=True).start()

    if not wait_for_server(port):
        print("Server failed to start.")
        sys.exit(1)

    # Allow the Download / Backup buttons to save files from inside the app window (pywebview 5+)
    try:
        webview.settings['ALLOW_DOWNLOADS'] = True
    except Exception:
        pass

    webview.create_window(
        title='Graduation Study Hub',
        url=f'http://127.0.0.1:{port}/',
        fullscreen=FULLSCREEN,
        confirm_close=True,
        text_select=True,
    )
    webview.start(private_mode=False, storage_path=WEBVIEW_DATA_DIR)
