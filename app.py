from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import os
import json
import sqlite3
import stripe
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
import hashlib
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import requests
from bs4 import BeautifulSoup
import re
import unicodedata

# Configuration
app = Flask(__name__, static_folder='templates/static', static_url_path='/static')
app.secret_key = os.environ.get('SECRET_KEY', 'pmu-dutching-tool-secret-key-change-me')

# Stripe configuration
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_your_key_here')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_your_key_here')

# SendGrid configuration
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
MAIL_USERNAME = os.environ.get('MAIL_USERNAME', 'noreply@pmu-dutching.app')

# Database
DB_FILE = 'pmu_users.db'

# Headers
PARISTURF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.paris-turf.com/",
}

TURFOMANIA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.turfomania.fr/",
}

# ============================================
# DATABASE SETUP
# ============================================

def init_db():
    """Initialize database"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            access_expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# ============================================
# SCRAPERS - PARIS-TURF & RECORDS
# ============================================

def slugify(s):
    """Convertir une chaîne en slug"""
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s

def get_paristurf_data(date_str, num_r, num_c):
    """Récupère les pronos et records Paris-Turf"""
    try:
        sess = requests.Session()
        sess.headers.update(PARISTURF_HEADERS)
        
        # date_str arrive en DDMMYYYY, le convertir en YYYY-MM-DD
        if len(date_str) == 8:
            date_fmt_pt = f"{date_str[4:8]}-{date_str[2:4]}-{date_str[0:2]}"
        else:
            date_fmt_pt = date_str
        
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        if date_fmt_pt == today_str:
            pt_home = "https://www.paris-turf.com/"
        else:
            pt_home = f"https://www.paris-turf.com/programme-courses/{date_fmt_pt}"
        
        print(f"[Paris-Turf] Chargement: {pt_home}")
        
        r0 = sess.get(pt_home, timeout=15)
        soup0 = BeautifulSoup(r0.text, "html.parser")
        
        script0 = soup0.find("script", id="__NEXT_DATA__")
        if not script0:
            return {"tips": [], "records": {}}
        
        data0 = json.loads(script0.string)
        state0 = data0.get("props", {}).get("pageProps", {}).get("initialState", {})
        rcs = state0.get("raceCardsState", {})
        
        all_meetings = rcs.get("meetings", {})
        all_races = rcs.get("races", {})
        
        meetings = []
        races = []
        found_date = None
        
        for d_key in all_meetings:
            m_list = all_meetings[d_key]
            hit = next((m for m in m_list if m.get("pmuNumber") == num_r), None)
            if hit:
                meetings = m_list
                races = all_races.get(d_key, [])
                found_date = d_key
                break
        
        if not found_date:
            print(f"[Paris-Turf] Meeting R{num_r} non trouvé")
            return {"tips": [], "records": {}}
        
        target_meeting = next((m for m in meetings if m.get("pmuNumber") == num_r), None)
        if not target_meeting:
            return {"tips": [], "records": {}}
        
        meet_id = target_meeting["id"]
        meet_name = target_meeting.get("name", "")
        
        target_race = next((r for r in races if r.get("meetingId") == meet_id and r.get("number") == num_c), None)
        if not target_race:
            return {"tips": [], "records": {}}
        
        race_uuid = target_race.get("uuid", "")
        race_name = target_race.get("name", "")
        race_id = str(target_race.get("id", ""))
        
        pt_url = f"https://www.paris-turf.com/course/{slugify(meet_name)}-{slugify(race_name)}-idc-{race_uuid}"
        print(f"[Paris-Turf] R{num_r}C{num_c}: {pt_url[-60:]}")
        
        r1 = sess.get(pt_url, timeout=15)
        soup1 = BeautifulSoup(r1.text, "html.parser")
        script1 = soup1.find("script", id="__NEXT_DATA__")
        
        if not script1:
            return {"tips": [], "records": {}}
        
        data1 = json.loads(script1.string)
        state1 = data1.get("props", {}).get("pageProps", {}).get("initialState", {})
        cur = state1.get("currentPageState", {})
        
        # Extraire les tips
        web_tips = cur.get("webTips") or {}
        tips_raw = web_tips.get("tips", {})
        tips = []
        
        for cat in ["A", "S", "C", "O", "G"]:
            t = tips_raw.get(cat)
            if not t:
                continue
            saddles = [int(x.strip()) for x in t.get("saddleList", "").split(",") if x.strip()]
            names = [x.strip() for x in t.get("nameList", "").split(",")]
            label = t.get("typeLabelParisTurf", cat)
            
            for i, num in enumerate(saddles):
                tips.append({
                    "rang": len(tips) + 1,
                    "num": num,
                    "nom": names[i] if i < len(names) else f"N°{num}",
                    "categorie": label,
                    "cat": cat
                })
                if len(tips) >= 5:
                    break
            if len(tips) >= 5:
                break
        
        # Extraire les records
        recs = {}
        runners_data = state1.get("raceCardsState", {}).get("runners", {})
        
        print(f"[DEBUG] race_id = {race_id}")
        
        if race_id in runners_data:
            print(f"[DEBUG] Found {len(runners_data[race_id])} runners")
            
            for idx, runner in enumerate(runners_data[race_id]):
                hnum = None
                for field in ["horseNumber", "number", "saddle", "saddleNumber", 
                             "saddle_number", "runnerNumber", "runnernumber", 
                             "position", "saddleCloth"]:
                    val = runner.get(field)
                    if val is not None and isinstance(val, int) and val > 0 and val < 100:
                        hnum = val
                        print(f"[DEBUG]   ✓ Using {field} as hnum = {hnum}")
                        break
                
                if not hnum:
                    continue
                
                # Chercher les records
                for rtype in ["harness", "distance", "flat"]:
                    rec = (runner.get("records") or {}).get(rtype, {})
                    redkm = rec.get("redkm") if rec else None
                    if redkm:
                        recs[hnum] = redkm
                        print(f"[DEBUG]   Saved: {hnum} = {redkm} ({rtype})")
                        break
        
        result = {
            "tips": tips,
            "records": recs,
            "author": web_tips.get("author", "Paris-Turf"),
            "text": (web_tips.get("text", "") or "")[:200]
        }
        
        print(f"[✓] Paris-Turf R{num_r}C{num_c}: {len(tips)} tips, {len(recs)} records")
        return result
    
    except Exception as e:
        print(f"[Erreur Paris-Turf] {str(e)}")
        import traceback
        traceback.print_exc()
        return {"tips": [], "records": {}}

def get_turfomania_pronos(date_str, num_r, num_c):
    """Pronos Turfomania"""
    try:
        sess = requests.Session()
        sess.headers.update(TURFOMANIA_HEADERS)
        
        d = datetime.strptime(date_str, "%d%m%Y")
        jour = d.strftime("%A").lower()[:3]
        mois = d.strftime("%B").lower()[:3]
        
        urls = [
            f"https://www.turfomania.fr/pronostics-pmu-{date_str[4:8]}{date_str[2:4]}{date_str[0:2]}-r{num_r}-c{num_c}.html",
            f"https://www.turfomania.fr/pronostics/{jour}-{d.day:02d}-{mois}-{d.year}/r{num_r}-c{num_c}",
        ]
        
        for url in urls:
            try:
                print(f"[Turfomania] Essai: {url[-60:]}")
                r = sess.get(url, timeout=10)
                soup = BeautifulSoup(r.text, "html.parser")
                pronos = []
                
                pattern = r'N°\s*(\d+)\s*(?:-\s*)?([A-Z][A-Za-z\s-]*)'
                for match in re.finditer(pattern, r.text):
                    num = int(match.group(1))
                    nom = match.group(2).strip()
                    pronos.append({"num": num, "nom": nom})
                
                if pronos:
                    print(f"[✓] Turfomania R{num_r}C{num_c}: {len(pronos)}")
                    return {"pronos": pronos[:5], "source": "Turfomania"}
            
            except Exception as e:
                print(f"[Turfomania] Erreur: {e}")
                continue
        
        return {"pronos": [], "source": "Turfomania"}
    
    except Exception as e:
        print(f"[Erreur Turfomania] {str(e)}")
        return {"pronos": [], "source": "Turfomania"}

# ============================================
# EMAIL HELPER
# ============================================

def send_email(to_email, subject, body_html):
    """Send email via SendGrid API"""
    if not SENDGRID_API_KEY:
        print(f"[WARNING] SendGrid not configured")
        return True
    
    try:
        url = "https://api.sendgrid.com/v3/mail/send"
        headers = {
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": MAIL_USERNAME},
            "subject": subject,
            "content": [{"type": "text/html", "value": body_html}]
        }
        resp = requests.post(url, json=data, headers=headers)
        print(f"[EMAIL] Sent to {to_email}: {resp.status_code}")
        return resp.status_code == 202
    except Exception as e:
        print(f"[EMAIL ERROR] {str(e)}")
        return False

# ============================================
# SCHEDULER
# ============================================

def send_expiry_reminders():
    """Send email reminder 7 days before expiry"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        target_date = (datetime.now() + timedelta(days=7)).date()
        c.execute('''
            SELECT u.email, s.access_expires_at 
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE DATE(s.access_expires_at) = ?
        ''', (target_date.isoformat(),))
        
        users = c.fetchall()
        conn.close()
        
        for email, expires_at in users:
            subject = "Votre abonnement PMU Dutching expire bientôt ⏰"
            body = f"<p>Votre abonnement expires le <strong>{expires_at}</strong>.</p>"
            send_email(email, subject, body)
    except Exception as e:
        print(f"[SCHEDULER ERROR] {str(e)}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=send_expiry_reminders, trigger="cron", hour=9, minute=0)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

# ============================================
# AUTHENTICATION DECORATORS
# ============================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('pricing'))
        return f(*args, **kwargs)
    return decorated_function

def premium_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('pricing'))
        
        user_id = session['user_id']
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT access_expires_at FROM subscriptions WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        conn.close()
        
        if not result or result[0] is None:
            return redirect(url_for('pricing'))
        
        expires_at = datetime.fromisoformat(result[0])
        if datetime.now() > expires_at:
            return redirect(url_for('pricing'))
        
        return f(*args, **kwargs)
    return decorated_function

# ============================================
# AUTH ROUTES
# ============================================

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        password_confirm = request.form.get('password_confirm')
        
        if not email or not password:
            return render_template('signup.html', error='Email et mot de passe requis')
        
        if password != password_confirm:
            return render_template('signup.html', error='Les mots de passe ne correspondent pas')
        
        if len(password) < 6:
            return render_template('signup.html', error='Le mot de passe doit faire au moins 6 caractères')
        
        try:
            hashed_password = hashlib.sha256(password.encode()).hexdigest()
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            c.execute('INSERT INTO users (email, password) VALUES (?, ?)', (email, hashed_password))
            user_id = c.lastrowid
            
            c.execute('''
                INSERT INTO subscriptions (user_id, access_expires_at) 
                VALUES (?, ?)
            ''', (user_id, None))
            
            conn.commit()
            conn.close()
            
            session['user_id'] = user_id
            session['email'] = email
            return redirect(url_for('pricing'))
            
        except sqlite3.IntegrityError:
            return render_template('signup.html', error='Cet email est déjà inscrit')
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        hashed_password = hashlib.sha256(password.encode()).hexdigest()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT id, email FROM users WHERE email = ? AND password = ?', (email, hashed_password))
        user = c.fetchone()
        conn.close()
        
        if user:
            session['user_id'] = user[0]
            session['email'] = user[1]
            return redirect(url_for('dashboard'))
        
        return render_template('login.html', error='Email ou mot de passe incorrect')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/pricing')
def pricing():
    if 'user_id' in session:
        user_id = session['user_id']
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT access_expires_at FROM subscriptions WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        conn.close()
        
        if result and result[0]:
            expires_at = datetime.fromisoformat(result[0])
            if datetime.now() < expires_at:
                return redirect(url_for('dashboard'))
    
    return render_template('pricing.html', stripe_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/dashboard')
@premium_required
def dashboard():
    return render_template('dashboard.html')

# ============================================
# STRIPE ROUTES
# ============================================

@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        user_id = session['user_id']
        email = session['email']
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT stripe_customer_id FROM subscriptions WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        
        if result and result[0]:
            customer_id = result[0]
        else:
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            c.execute('UPDATE subscriptions SET stripe_customer_id = ? WHERE user_id = ?', 
                     (customer_id, user_id))
            conn.commit()
        
        conn.close()
        
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': {
                        'name': 'PMUDutchingTool Premium - 1 Mois',
                        'description': 'Accès complet au tableau d\'analyse PMU'
                    },
                    'unit_amount': 999,
                },
                'quantity': 1
            }],
            mode='payment',
            success_url=f'{request.host_url}success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{request.host_url}pricing'
        )
        
        return jsonify({'id': checkout_session.id})
    
    except Exception as e:
        print(f"[ERREUR STRIPE] {str(e)}")
        return jsonify({'error': str(e)}), 400

@app.route('/success')
@login_required
def success():
    session_id = request.args.get('session_id')
    
    if not session_id:
        return redirect(url_for('dashboard'))
    
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        user_id = session['user_id']
        
        access_expires = datetime.now() + timedelta(days=30)
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            UPDATE subscriptions 
            SET stripe_subscription_id = ?, 
                access_expires_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (checkout_session.id, access_expires.isoformat(), user_id))
        conn.commit()
        conn.close()
        
        print(f"[SUCCESS] Payment received for user {user_id}")
        return redirect(url_for('dashboard'))
    
    except Exception as e:
        print(f"[ERROR] Success page error: {str(e)}")
        return redirect(url_for('dashboard'))

# ============================================
# SCRAPER ROUTES (PREMIUM)
# ============================================

@app.route('/paristurf/<date_str>/<course_key>')
@premium_required
def paristurf_route(date_str, course_key):
    """Paris-Turf pronos"""
    try:
        parts = course_key.split('C')
        if len(parts) != 2:
            return jsonify({'tips': [], 'records': {}}), 400
        
        num_r = int(parts[0].replace('R', ''))
        num_c = int(parts[1])
        
        data = get_paristurf_data(date_str, num_r, num_c)
        return jsonify(data)
    
    except Exception as e:
        print(f"[ERROR] paristurf: {str(e)}")
        return jsonify({'tips': [], 'records': {}}), 500

@app.route('/records/<date_str>/<course_key>')
@premium_required
def records_route(date_str, course_key):
    """Records km"""
    try:
        parts = course_key.split('C')
        if len(parts) != 2:
            return jsonify({}), 400
        
        num_r = int(parts[0].replace('R', ''))
        num_c = int(parts[1])
        
        data = get_paristurf_data(date_str, num_r, num_c)
        records = data.get('records', {})
        result = {str(num): record for num, record in records.items()}
        
        return jsonify(result)
    
    except Exception as e:
        print(f"[ERROR] records: {str(e)}")
        return jsonify({}), 500

@app.route('/turfomania/<date_str>/<course_key>')
@premium_required
def turfomania_route(date_str, course_key):
    """Turfomania pronos"""
    try:
        parts = course_key.split('C')
        if len(parts) != 2:
            return jsonify({'pronos': []}), 400
        
        num_r = int(parts[0].replace('R', ''))
        num_c = int(parts[1])
        
        data = get_turfomania_pronos(date_str, num_r, num_c)
        return jsonify(data)
    
    except Exception as e:
        print(f"[ERROR] turfomania: {str(e)}")
        return jsonify({'pronos': []}), 500

# ============================================
# API ROUTES - PROGRAMME & RAPPORTS (PREMIUM)
# ============================================

@app.route('/api/programme/<date_str>')
@premium_required
def api_programme(date_str):
    """Proxy pour endpoint /programme"""
    try:
        url = f"https://online.turfinfo.api.pmu.fr/rest/client/1/programme/{date_str}"
        if request.query_string:
            url += '?' + request.query_string.decode('utf-8')
        
        print(f"[API] Proxying to: {url}")
        
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
        
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return jsonify(data)
    except Exception as e:
        print(f"[API ERROR] Programme: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/programme/<date_str>/R<int:numR>/C<int:numC>/rapports-definitifs')
@premium_required
def api_rapports_definitifs(date_str, numR, numC):
    """Proxy pour rapports définitifs PMU"""
    try:
        url = f"https://online.turfinfo.api.pmu.fr/rest/client/61/programme/{date_str}/R{numR}/C{numC}/rapports-definitifs"
        if request.query_string:
            url += '?' + request.query_string.decode('utf-8')
        
        print(f"[API] Proxying rapports to: {url}")
        
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
        
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return jsonify(data)
    except Exception as e:
        print(f"[API ERROR] Rapports: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/<path:path>')
@premium_required
def api_proxy(path):
    """Proxy générique"""
    try:
        url = f"https://online.turfinfo.api.pmu.fr/rest/client/1/{path}"
        if request.query_string:
            url += '?' + request.query_string.decode('utf-8')
        
        print(f"[API] Proxying to: {url}")
        
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
        
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return jsonify(data)
    except Exception as e:
        print(f"[API ERROR] Generic: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ============================================
# HEALTH CHECK
# ============================================

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'app': 'PMUDutchingTool'})

# ============================================
# ERROR HANDLERS
# ============================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(error):
    return jsonify({'error': 'Server error'}), 500

# ============================================
# RUN APP
# ============================================

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)