from flask import Flask, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from difflib import unified_diff
from datetime import datetime
import hashlib, ipaddress, socket, sqlite3, threading, time
import requests

app = Flask(__name__)
DB = 'website.db'
scheduler = BackgroundScheduler(daemon=True)
check_lock = threading.Lock()
rate_lock = threading.Lock()
last_request = 0.0
INTERVALS = (1, 5, 15, 30, 60, 360, 1440)


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def db():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables():
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS websites (
            url TEXT PRIMARY KEY, baseline TEXT NOT NULL, last_checked TEXT,
            status TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            check_interval INTEGER NOT NULL DEFAULT 5)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
            checked_at TEXT NOT NULL, result TEXT NOT NULL, details TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
            content TEXT NOT NULL, content_hash TEXT NOT NULL,
            captured_at TEXT NOT NULL)''')
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(websites)')}
        if 'name' not in cols:
            conn.execute("ALTER TABLE websites ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        if 'check_interval' not in cols:
            conn.execute('ALTER TABLE websites ADD COLUMN check_interval INTEGER NOT NULL DEFAULT 5')


def history(url, result, details=''):
    with db() as conn:
        conn.execute('INSERT INTO history(url,checked_at,result,details) VALUES(?,?,?,?)',
                     (url, now(), result, details))


def snapshot(url, content):
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    with db() as conn:
        old = conn.execute('SELECT content_hash FROM snapshots WHERE url=? ORDER BY id DESC LIMIT 1',
                            (url,)).fetchone()
        if old is None or old['content_hash'] != digest:
            conn.execute('INSERT INTO snapshots(url,content,content_hash,captured_at) VALUES(?,?,?,?)',
                         (url, content, digest, now()))
            return True
    return False


def valid_public_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return False, 'Enter a valid public URL beginning with http:// or https://.'
        host = p.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return False, 'Local and internal hostnames are not supported.'
        try:
            for item in socket.getaddrinfo(host, None):
                if not ipaddress.ip_address(item[4][0]).is_global:
                    return False, 'Private or local network addresses are not supported.'
        except socket.gaierror:
            return False, 'The website hostname could not be resolved.'
        return True, ''
    except (ValueError, UnicodeError):
        return False, 'Enter a valid URL.'


def fetch_text(url):
    ok, error = valid_public_url(url)
    if not ok:
        return None, error
    global last_request
    for attempt in range(3):
        try:
            with rate_lock:
                delay = 1 - (time.monotonic() - last_request)
                if delay > 0:
                    time.sleep(delay)
                last_request = time.monotonic()
            r = requests.get(url, timeout=12, headers={'User-Agent': 'WatchTower/1.0'}, allow_redirects=True)
            r.raise_for_status()
            ok, error = valid_public_url(r.url)
            if not ok:
                return None, 'Redirect destination rejected: ' + error
            ctype = r.headers.get('Content-Type', '').lower()
            if ctype and not any(x in ctype for x in ('text/html', 'text/plain', 'application/xhtml')):
                return None, 'This URL does not return a readable HTML/text page.'
            soup = BeautifulSoup(r.text, 'html.parser')
            for tag in soup(['script', 'style', 'noscript', 'svg', 'canvas', 'iframe']):
                tag.decompose()
            for tag in soup.select('[aria-hidden="true"], .advertisement, .ads, .cookie-banner, .cookie-consent, .live-clock'):
                tag.decompose()
            text = ' '.join(soup.get_text(separator=' ', strip=True).split())[:50000]
            if not text:
                return None, 'No readable text was found on the page.'
            return text, None
        except requests.RequestException:
            if attempt == 2:
                return None, 'Could not access the website after 3 attempts. Check the URL and try again.'
            time.sleep(attempt + 1)
    return None, 'Could not access the website.'


def diff_text(old, new, old_label='Previous Version', new_label='Current Version'):
    return '\n'.join(unified_diff(old.split(), new.split(), fromfile=old_label,
                                  tofile=new_label, lineterm=''))


def check_site(url):
    if not check_lock.acquire(blocking=False):
        return None, 'Another website check is running. Try again shortly.'
    try:
        text, error = fetch_text(url)
        checked = now()
        if error:
            with db() as conn:
                conn.execute('UPDATE websites SET last_checked=?,status=? WHERE url=?',
                             (checked, 'Error', url))
            history(url, 'Error', error)
            return None, error
        with db() as conn:
            row = conn.execute('SELECT baseline FROM websites WHERE url=?', (url,)).fetchone()
            if row is None:
                return None, 'Save a baseline before checking this website.'
            changes = diff_text(row['baseline'], text)
            if changes:
                status, details = 'Changes detected', changes[:6000]
                snapshot(url, text)
                conn.execute('UPDATE websites SET baseline=?,last_checked=?,status=? WHERE url=?',
                             (text, checked, status, url))
            else:
                status, details = 'No changes', 'No text changes found.'
                conn.execute('UPDATE websites SET last_checked=?,status=? WHERE url=?',
                             (checked, status, url))
        history(url, status, details)
        return status, details
    finally:
        check_lock.release()


def scheduled_checks():
    with db() as conn:
        sites = conn.execute('SELECT url,last_checked,check_interval FROM websites').fetchall()
    current = datetime.now()
    for site in sites:
        due = True
        if site['last_checked']:
            try:
                last = datetime.strptime(site['last_checked'], '%Y-%m-%d %H:%M:%S')
                due = (current - last).total_seconds() >= max(1, site['check_interval']) * 60
            except ValueError:
                pass
        if due:
            check_site(site['url'])


@app.route('/', methods=['GET', 'POST'])
def home():
    message = error = diff = None
    url = name = ''
    interval = 5
    if request.method == 'POST':
        action = request.form.get('action', 'check')
        url = request.form.get('url', '').strip()
        name = request.form.get('name', '').strip()
        try:
            interval = int(request.form.get('check_interval', '5'))
        except ValueError:
            interval = 5
        if interval not in INTERVALS:
            interval = 5

        if action == 'save':
            if not name:
                name = urlparse(url).hostname or 'Monitored website'
            text, error = fetch_text(url)
            if text is not None:
                with db() as conn:
                    conn.execute('''INSERT INTO websites(url,baseline,last_checked,status,name,check_interval)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                        baseline=excluded.baseline,last_checked=excluded.last_checked,
                        status=excluded.status,name=excluded.name,check_interval=excluded.check_interval''',
                        (url, text, now(), 'Baseline saved', name, interval))
                history(url, 'Baseline saved', 'Saved current webpage text.')
                snapshot(url, text)
                message = 'Baseline saved. Automatic monitoring is enabled.'
        elif action == 'check':
            if not url:
                error = 'Enter a website URL first.'
            else:
                with db() as conn:
                    exists = conn.execute('SELECT 1 FROM websites WHERE url=?', (url,)).fetchone()
                if not exists:
                    error = 'Save a baseline for this URL before checking it.'
                else:
                    status, details = check_site(url)
                    if status == 'Changes detected':
                        message, diff = 'Changes detected! A new version has been saved.', details
                    elif status == 'No changes':
                        message = 'No changes detected.'
                    elif status:
                        message = status
                    else:
                        error = details
        elif action == 'compare':
            try:
                a, b = int(request.form.get('version_a', '')), int(request.form.get('version_b', ''))
                with db() as conn:
                    first = conn.execute('SELECT url,content,captured_at FROM snapshots WHERE id=?', (a,)).fetchone()
                    second = conn.execute('SELECT url,content,captured_at FROM snapshots WHERE id=?', (b,)).fetchone()
                if not first or not second:
                    error = 'Select two valid saved versions.'
                elif first['url'] != second['url']:
                    error = 'Choose two versions of the same website.'
                elif a == b:
                    error = 'Choose two different versions.'
                else:
                    diff = diff_text(first['content'], second['content'],
                                     f"Version {a} ({first['captured_at']})",
                                     f"Version {b} ({second['captured_at']})")
                    message = 'Comparison complete. No text differences found.' if not diff else 'Version comparison completed.'
            except (ValueError, TypeError):
                error = 'Select two valid versions.'

    with db() as conn:
        websites = conn.execute('SELECT url,last_checked,status,name,check_interval FROM websites ORDER BY name,url').fetchall()
        activity = conn.execute('SELECT url,checked_at,result FROM history ORDER BY id DESC LIMIT 12').fetchall()
        snapshots = conn.execute('SELECT id,url,captured_at,content_hash FROM snapshots ORDER BY id DESC LIMIT 100').fetchall()
        total = conn.execute('SELECT COUNT(*) FROM websites').fetchone()[0]
        changed = conn.execute("SELECT COUNT(*) FROM websites WHERE status='Changes detected'").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM websites WHERE status='Error'").fetchone()[0]
    return render_template('index.html', message=message, error=error, diff=diff, url=url,
                           name=name, interval=interval, websites=websites, activity=activity,
                           snapshots=snapshots, total=total, changed=changed, errors=errors)


create_tables()
if __name__ == '__main__':
    scheduler.add_job(scheduled_checks, 'interval', minutes=1, id='scheduled_checks',
                      max_instances=1, coalesce=True, replace_existing=True)
    scheduler.start()
    try:
        app.run(debug=True, use_reloader=False)
    finally:
        scheduler.shutdown(wait=False)
