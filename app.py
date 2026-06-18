from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import os
import json
import sqlite3
import stripe
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
import hashlib

# Configuration
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'pmu-dutching-tool-secret-key-change-me')

# Stripe configuration
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_your_key_here')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_your_key_here')

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
    
    conn.commit()
    conn.close()

init_db()

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
        
        # Si pas de subscription ou expirée, redirection
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
            
            # Create user
            c.execute('INSERT INTO users (email, password) VALUES (?, ?)', (email, hashed_password))
            user_id = c.lastrowid
            
            # Create subscription (empty, user must pay)
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
# MAIN ROUTES
# ============================================

@app.route('/pricing')
def pricing():
    if 'user_id' in session:
        # Vérifier si l'utilisateur a accès premium
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
    return render_template('dashboard_premium.html')

# ============================================
# STRIPE PAYMENT ROUTES
# ============================================

@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        user_id = session['user_id']
        email = session['email']
        
        # Create or get Stripe customer
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT stripe_customer_id FROM subscriptions WHERE user_id = ?', (user_id,))
        result = c.fetchone()
        
        if result and result[0]:
            customer_id = result[0]
        else:
            # Create new customer
            customer = stripe.Customer.create(email=email)
            customer_id = customer.id
            c.execute('UPDATE subscriptions SET stripe_customer_id = ? WHERE user_id = ?', 
                     (customer_id, user_id))
            conn.commit()
        
        conn.close()
        
        # Create checkout session
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
                    'unit_amount': 999,  # 9.99€ en centimes
                    'recurring': {
                        'interval': 'month',
                        'interval_count': 1
                    }
                },
                'quantity': 1
            }],
            mode='subscription',
            success_url=f'{request.host_url}success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{request.host_url}pricing'
        )
        
        return jsonify({'id': checkout_session.id})
    
    except Exception as e:
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
        
        # Update subscription in database
        access_expires = datetime.now() + timedelta(days=30)
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            UPDATE subscriptions 
            SET stripe_subscription_id = ?, 
                access_expires_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (checkout_session.subscription, access_expires.isoformat(), user_id))
        conn.commit()
        conn.close()
        
        return redirect(url_for('dashboard'))
    
    except Exception as e:
        return redirect(url_for('dashboard'))

# ============================================
# API ROUTES - PMU DATA (PREMIUM ONLY)
# ============================================

@app.route('/api/pmu/<path:path>')
@premium_required
def pmu_api(path):
    """Proxy vers l'API PMU - Premium only"""
    try:
        url = f"https://online.turfinfo.api.pmu.fr/rest/client/1/{path}"
        if request.query_string:
            url += '?' + request.query_string.decode('utf-8')
        
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
        
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return jsonify(data)
    except Exception as e:
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
