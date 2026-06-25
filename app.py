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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# Configuration
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'pmu-dutching-tool-secret-key-change-me')

# Stripe configuration
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_your_key_here')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_your_key_here')

# SendGrid configuration
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
MAIL_USERNAME = os.environ.get('MAIL_USERNAME', 'noreply@pmu-dutching.app')

# Database
DB_FILE = 'pmu_users.db'

# ============================================
# DATABASE SETUP
# ============================================

def init_db():
    """Initialize database with users and subscriptions tables"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Subscriptions table
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
    
    # Password reset tokens
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
# EMAIL HELPER
# ============================================

def send_email(to_email, subject, body_html):
    """Send email via SendGrid API"""
    if not SENDGRID_API_KEY:
        print(f"[WARNING] SendGrid not configured. Email would be sent to {to_email}")
        return True
    
    try:
        import requests
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
# SCHEDULER - EMAIL REMINDERS
# ============================================

def send_expiry_reminders():
    """Send email reminder 7 days before expiry"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Find subscriptions expiring in 7 days
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
            body = f"""
            <p>Bonjour,</p>
            <p>Votre abonnement premium expires le <strong>{expires_at}</strong>.</p>
            <p>Pour continuer à accéder au tableau d'analyse, <a href="https://pmu-dutching.app/pricing">cliquez ici pour renouveler</a>.</p>
            <p>Bonne chance ! 🏇</p>
            """
            send_email(email, subject, body)
            print(f"[REMINDER] Sent to {email}")
    except Exception as e:
        print(f"[SCHEDULER ERROR] {str(e)}")

# Start scheduler
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
    return redirect(url_for('pricing'))

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
        except Exception as e:
            return render_template('signup.html', error=f'Erreur: {str(e)}')
    
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
    return redirect(url_for('pricing'))

# ============================================
# PASSWORD RESET ROUTES
# ============================================

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT id FROM users WHERE email = ?', (email,))
        user = c.fetchone()
        
        if user:
            user_id = user[0]
            import secrets
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now() + timedelta(hours=1)
            
            c.execute('DELETE FROM password_resets WHERE user_id = ?', (user_id,))
            c.execute('''
                INSERT INTO password_resets (user_id, token, expires_at)
                VALUES (?, ?, ?)
            ''', (user_id, token, expires_at.isoformat()))
            conn.commit()
            
            reset_url = f"https://pmu-dutching.app/reset-password?token={token}"
            subject = "Réinitialiser votre mot de passe PMU Dutching"
            body = f"""
            <p>Cliquez sur le lien ci-dessous pour réinitialiser votre mot de passe :</p>
            <p><a href="{reset_url}">Réinitialiser mon mot de passe</a></p>
            <p>Ce lien expire dans 1 heure.</p>
            """
            send_email(email, subject, body)
        
        conn.close()
        return render_template('forgot_password.html', message='Si cet email existe, vous recevrez un lien de réinitialisation')
    
    return render_template('forgot_password.html')

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    token = request.args.get('token')
    
    if request.method == 'POST':
        password = request.form.get('password')
        password_confirm = request.form.get('password_confirm')
        token = request.form.get('token')
        
        if password != password_confirm:
            return render_template('reset_password.html', error='Les mots de passe ne correspondent pas', token=token)
        
        if len(password) < 6:
            return render_template('reset_password.html', error='Le mot de passe doit faire au moins 6 caractères', token=token)
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            SELECT user_id FROM password_resets 
            WHERE token = ? AND expires_at > ?
        ''', (token, datetime.now().isoformat()))
        result = c.fetchone()
        
        if not result:
            return render_template('reset_password.html', error='Token invalide ou expiré', token=token)
        
        user_id = result[0]
        hashed_password = hashlib.sha256(password.encode()).hexdigest()
        
        c.execute('UPDATE users SET password = ? WHERE id = ?', (hashed_password, user_id))
        c.execute('DELETE FROM password_resets WHERE token = ?', (token,))
        conn.commit()
        conn.close()
        
        return render_template('reset_password.html', success=True)
    
    if not token:
        return redirect(url_for('forgot_password'))
    
    return render_template('reset_password.html', token=token)

# ============================================
# MAIN ROUTES
# ============================================

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
# STRIPE PAYMENT ROUTES
# ============================================

@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        user_id = session['user_id']
        email = session['email']
        
        print(f"[DEBUG] Creating checkout for user {user_id} ({email})")
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT stripe_customer_id FROM subscriptions WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        
        if result and result[0]:
            customer_id = result[0]
            print(f"[DEBUG] Using existing customer: {customer_id}")
        else:
            print(f"[DEBUG] Creating new Stripe customer")
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            print(f"[DEBUG] New customer created: {customer_id}")
            c.execute('UPDATE subscriptions SET stripe_customer_id = ? WHERE user_id = ?', 
                     (customer_id, user_id))
            conn.commit()
        
        conn.close()
        
        print(f"[DEBUG] Creating checkout session with mode=payment")
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
        
        print(f"[DEBUG] Checkout session created: {checkout_session.id}")
        return jsonify({'id': checkout_session.id})
    
    except Exception as e:
        error_msg = str(e)
        print(f"\n[ERREUR STRIPE] {error_msg}\n")
        import traceback
        traceback.print_exc()
        return jsonify({'error': error_msg}), 400

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
        
        print(f"[SUCCESS] Payment received for user {user_id}. Access until {access_expires}")
        
        # Send confirmation email
        email = session.get('email', '')
        if email:
            subject = "Bienvenue ! Votre abonnement est activé 🎉"
            body = f"""
            <p>Bienvenue dans PMU Dutching Tool Premium !</p>
            <p>Votre abonnement est actif jusqu'au <strong>{access_expires.strftime('%d/%m/%Y')}</strong>.</p>
            <p>Accédez au <a href="https://pmu-dutching.app/dashboard">tableau d'analyse</a>.</p>
            <p>Bonne chance ! 🏇</p>
            """
            send_email(email, subject, body)
        
        return redirect(url_for('dashboard'))
    
    except Exception as e:
        print(f"[ERROR] Success page error: {str(e)}")
        return redirect(url_for('dashboard'))

# ============================================
# API ROUTES - PROGRAMME & RAPPORTS (PREMIUM)
# ============================================

@app.route('/api/programme/<date_str>')
@premium_required
def api_programme(date_str):
    """Proxy dédié pour endpoint /programme"""
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
    """Proxy dédié pour rapports définitifs PMU"""
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

# Generic proxy (fallback)
@app.route('/api/<path:path>')
@premium_required
def api_proxy(path):
    """Proxy générique pour autres appels API"""
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
    return jsonify({
        'status': 'ok',
        'app': 'PMUDutchingTool',
        'timestamp': datetime.now().isoformat()
    })

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