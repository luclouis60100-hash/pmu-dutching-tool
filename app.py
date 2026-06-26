import os
import json
import stripe
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
import re

load_dotenv()

# ============================================
# CONFIG
# ============================================

app = Flask(__name__, static_folder='templates/static', static_url_path='/static')
app.secret_key = os.environ.get('SECRET_KEY', 'pmu-dutching-tool-secret-key-change-me')

# DATABASE - PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set!")

def get_db():
    """Get PostgreSQL connection"""
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_your_key_here')

# SendGrid
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')

# ============================================
# INIT DB
# ============================================

def init_db():
    """Initialize PostgreSQL tables"""
    conn = get_db()
    c = conn.cursor()
    
    # Create users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create subscriptions table
    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            stripe_customer_id VARCHAR(255),
            access_expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create password_resets table
    c.execute('''
        CREATE TABLE IF NOT EXISTS password_resets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            token VARCHAR(255) UNIQUE NOT NULL,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ Database initialized")

# Initialize on startup
try:
    init_db()
except Exception as e:
    print(f"⚠️ DB init error: {e}")

# ============================================
# AUTH DECORATORS
# ============================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def premium_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('pricing'))
        
        user_id = session['user_id']
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT access_expires_at FROM subscriptions WHERE user_id = %s', (user_id,))
        result = c.fetchone()
        conn.close()
        
        if not result or result[0] is None:
            return redirect(url_for('pricing'))
        
        expires_at = result[0]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        
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
        
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('INSERT INTO users (email, password) VALUES (%s, %s)', (email, password))
            conn.commit()
            
            c.execute('SELECT id FROM users WHERE email = %s', (email,))
            user_id = c.fetchone()[0]
            
            c.execute('INSERT INTO subscriptions (user_id) VALUES (%s)', (user_id,))
            conn.commit()
            conn.close()
            
            session['user_id'] = user_id
            session['email'] = email
            
            return redirect(url_for('pricing'))
        except psycopg2.IntegrityError:
            conn.close()
            return render_template('signup.html', error='Email déjà utilisé')
        except Exception as e:
            return render_template('signup.html', error=f'Erreur: {str(e)}')
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not email or not password:
            return render_template('login.html', error='Email et mot de passe requis')
        
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT id, password FROM users WHERE email = %s', (email,))
            result = c.fetchone()
            conn.close()
            
            if result and result[1] == password:
                session['user_id'] = result[0]
                session['email'] = email
                return redirect(url_for('dashboard'))
            else:
                return render_template('login.html', error='Email ou mot de passe incorrect')
        except Exception as e:
            return render_template('login.html', error=f'Erreur: {str(e)}')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT id FROM users WHERE email = %s', (email,))
            result = c.fetchone()
            
            if result:
                token = os.urandom(32).hex()
                expires = datetime.now() + timedelta(hours=24)
                c.execute('INSERT INTO password_resets (user_id, token, expires_at) VALUES (%s, %s, %s)',
                         (result[0], token, expires))
                conn.commit()
                
                reset_link = f"https://web-production-b3d28.up.railway.app/reset-password/{token}"
                # TODO: Send email via SendGrid
            
            conn.close()
            return render_template('forgot_password.html', success='Email de réinitialisation envoyé')
        except Exception as e:
            return render_template('forgot_password.html', error=str(e))
    
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if request.method == 'POST':
        password = request.form.get('password')
        password_confirm = request.form.get('password_confirm')
        
        if password != password_confirm:
            return render_template('reset_password.html', error='Les mots de passe ne correspondent pas')
        
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT user_id FROM password_resets WHERE token = %s AND expires_at > NOW()', (token,))
            result = c.fetchone()
            
            if result:
                c.execute('UPDATE users SET password = %s WHERE id = %s', (password, result[0]))
                c.execute('DELETE FROM password_resets WHERE token = %s', (token,))
                conn.commit()
                conn.close()
                return render_template('reset_password.html', success='Mot de passe réinitialisé')
            else:
                return render_template('reset_password.html', error='Token invalide ou expiré')
        except Exception as e:
            return render_template('reset_password.html', error=str(e))
    
    return render_template('reset_password.html')

# ============================================
# MAIN ROUTES
# ============================================

@app.route('/dashboard')
@premium_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/pricing')
def pricing():
    stripe_key = os.environ.get('STRIPE_PUBLISHABLE_KEY')
    if 'user_id' in session:
        user_id = session['user_id']
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT access_expires_at FROM subscriptions WHERE user_id = %s', (user_id,))
        result = c.fetchone()
        conn.close()
        
        if result and result[0]:
            expires_at = result[0]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            
            if datetime.now() < expires_at:
                days_left = (expires_at - datetime.now()).days
                return render_template('pricing.html', subscribed=True, days_left=days_left, stripe_key=stripe_key)
    
    return render_template('pricing.html', subscribed=False, stripe_key=stripe_key)

# ============================================
# STRIPE ROUTES
# ============================================

@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        user_id = session['user_id']
        email = session['email']
        
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT stripe_customer_id FROM subscriptions WHERE user_id = %s', (user_id,))
        result = c.fetchone()
        
        if result and result[0]:
            customer_id = result[0]
        else:
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            c.execute('UPDATE subscriptions SET stripe_customer_id = %s WHERE user_id = %s', 
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
                'quantity': 1,
            }],
            mode='payment',
            success_url='https://web-production-b3d28.up.railway.app/checkout-success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url='https://web-production-b3d28.up.railway.app/pricing',
        )
        
        return jsonify({'sessionId': checkout_session.id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/checkout-success')
def checkout_success():
    session_id = request.args.get('session_id')
    
    if not session_id:
        return redirect(url_for('pricing'))
    
    try:
        session_data = stripe.checkout.Session.retrieve(session_id)
        
        if session_data.payment_status == 'paid':
            user_id = session.get('user_id')
            if user_id:
                access_expires = datetime.now() + timedelta(days=30)
                
                conn = get_db()
                c = conn.cursor()
                c.execute('UPDATE subscriptions SET access_expires_at = %s WHERE user_id = %s',
                         (access_expires, user_id))
                conn.commit()
                conn.close()
                
                return render_template('checkout_success.html')
        
        return redirect(url_for('pricing'))
    except Exception as e:
        return redirect(url_for('pricing'))

# ============================================
# SCRAPERS (Paris-Turf, Records, etc.)
# ============================================

def get_paristurf_data(date_str, num_r, num_c):
    """Récupère les pronos et records Paris-Turf"""
    try:
        sess = requests.Session()
        sess.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
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
        
        # Scraping logic...
        return {"tips": [], "records": {}}
    except Exception as e:
        print(f"[ERROR] Paris-Turf: {str(e)}")
        return {"tips": [], "records": {}}

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
        return jsonify({}), 500

# ============================================
# API PROXY - PMU
# ============================================

@app.route('/api/<path:path>')
@premium_required
def proxy(path):
    """Proxy générique vers API PMU"""
    try:
        url = f"https://online.turfinfo.api.pmu.fr/rest/client/1/{path}"
        r = requests.get(url, timeout=15)
        return r.json()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/programme/<date_str>/R<int:numR>/C<int:numC>/rapports-definitifs')
@premium_required
def rapports_definitifs(date_str, numR, numC):
    """Rapports définitifs via client/61"""
    try:
        url = f"https://online.turfinfo.api.pmu.fr/rest/client/61/programme/{date_str}/R{numR}/C{numC}/rapports-definitifs?specialisation=INTERNET&combinaisonEnTableau=true"
        r = requests.get(url, timeout=15)
        return r.json()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================
# ERROR HANDLERS
# ============================================

@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

# ============================================
# RUN
# ============================================

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)