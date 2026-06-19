from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import os
import json
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import stripe
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
import hashlib
from bson.objectid import ObjectId

# Scraping imports
try:
    from scraper import get_paristurf_pronos, get_records_km, get_turfomania_pronos
    SCRAPER_AVAILABLE = True
except ImportError:
    print("[!] Warning: scraper module not available. Some features disabled.")
    SCRAPER_AVAILABLE = False

# Configuration
app = Flask(__name__, static_folder='templates/static', static_url_path='/static')
app.secret_key = os.environ.get('SECRET_KEY', 'pmu-dutching-tool-secret-key-change-me')

# Stripe configuration
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_your_key_here')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_your_key_here')

# MongoDB configuration
MONGODB_URL = os.environ.get('MONGODB_URI')

# ============================================
# DATABASE HELPERS - MongoDB
# ============================================

def get_db():
    """Get MongoDB connection"""
    if not MONGODB_URL:
        raise Exception("MONGODB_URI environment variable not set!")
    
    client = MongoClient(MONGODB_URL)
    db = client['pmu_dutching']
    return db

def init_db():
    """Initialize MongoDB collections and indexes"""
    try:
        db = get_db()
        
        # Create collections if they don't exist
        if 'users' not in db.list_collection_names():
            db.create_collection('users')
        
        if 'subscriptions' not in db.list_collection_names():
            db.create_collection('subscriptions')
        
        # Create indexes
        db.users.create_index('email', unique=True)
        db.subscriptions.create_index('user_id')
        
        print("[DB] MongoDB initialized successfully")
    except Exception as e:
        print(f"[ERROR] Database initialization: {str(e)}")

init_db()

# ============================================
# AUTHENTICATION DECORATORS
# ============================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def premium_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('index'))
        
        user_id = session['user_id']
        try:
            db = get_db()
            subscription = db.subscriptions.find_one({'user_id': ObjectId(user_id)})
            
            if not subscription or subscription.get('access_expires_at') is None:
                return redirect(url_for('pricing'))
            
            expires_at = subscription['access_expires_at']
            if datetime.now() > expires_at:
                return redirect(url_for('pricing'))
        except Exception as e:
            print(f"[DB ERROR] {str(e)}")
            return redirect(url_for('pricing'))
        
        return f(*args, **kwargs)
    return decorated_function

# ============================================
# ROUTES - INDEX & LANDING PAGE
# ============================================

@app.route('/')
def index():
    if 'user_id' in session:
        user_id = session['user_id']
        try:
            db = get_db()
            subscription = db.subscriptions.find_one({'user_id': ObjectId(user_id)})
            
            if subscription and subscription.get('access_expires_at'):
                expires_at = subscription['access_expires_at']
                if datetime.now() < expires_at:
                    return redirect(url_for('dashboard'))
        except:
            pass
        
        return redirect(url_for('pricing'))
    
    return render_template('index.html')

# ============================================
# AUTH ROUTES
# ============================================

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
            db = get_db()
            
            # Insert user
            user_result = db.users.insert_one({
                'email': email,
                'password': hashed_password,
                'created_at': datetime.now()
            })
            user_id = user_result.inserted_id
            
            # Create subscription
            db.subscriptions.insert_one({
                'user_id': user_id,
                'stripe_customer_id': None,
                'stripe_subscription_id': None,
                'access_expires_at': None,
                'created_at': datetime.now(),
                'updated_at': datetime.now()
            })
            
            session['user_id'] = str(user_id)
            session['email'] = email
            return redirect(url_for('pricing'))
            
        except DuplicateKeyError:
            return render_template('signup.html', error='Cet email est déjà inscrit')
        except Exception as e:
            print(f"[SIGNUP ERROR] {str(e)}")
            return render_template('signup.html', error=f'Erreur: {str(e)}')
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        hashed_password = hashlib.sha256(password.encode()).hexdigest()
        try:
            db = get_db()
            user = db.users.find_one({'email': email, 'password': hashed_password})
            
            if user:
                session['user_id'] = str(user['_id'])
                session['email'] = user['email']
                
                # Vérifier si abonnement valide
                try:
                    subscription = db.subscriptions.find_one({'user_id': user['_id']})
                    
                    if subscription and subscription.get('access_expires_at'):
                        expires_at = subscription['access_expires_at']
                        if datetime.now() < expires_at:
                            return redirect(url_for('dashboard'))
                except:
                    pass
                
                return redirect(url_for('pricing'))
        except Exception as e:
            print(f"[LOGIN ERROR] {str(e)}")
        
        return render_template('login.html', error='Email ou mot de passe incorrect')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ============================================
# MAIN ROUTES
# ============================================

@app.route('/pricing')
def pricing():
    if 'user_id' in session:
        user_id = session['user_id']
        try:
            db = get_db()
            subscription = db.subscriptions.find_one({'user_id': ObjectId(user_id)})
            
            if subscription and subscription.get('access_expires_at'):
                expires_at = subscription['access_expires_at']
                if datetime.now() < expires_at:
                    return redirect(url_for('dashboard'))
        except Exception as e:
            print(f"[PRICING ERROR] {str(e)}")
    
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
        
        try:
            db = get_db()
            subscription = db.subscriptions.find_one({'user_id': ObjectId(user_id)})
            
            if subscription and subscription.get('stripe_customer_id'):
                customer_id = subscription['stripe_customer_id']
                print(f"[DEBUG] Using existing customer: {customer_id}")
            else:
                print(f"[DEBUG] Creating new Stripe customer")
                customer = stripe.Customer.create(email=email)
                customer_id = customer.id
                print(f"[DEBUG] New customer created: {customer_id}")
                db.subscriptions.update_one(
                    {'user_id': ObjectId(user_id)},
                    {'$set': {'stripe_customer_id': customer_id}}
                )
        except Exception as db_err:
            print(f"[DB ERROR] {str(db_err)}")
            raise
        
        print(f"[DEBUG] Creating checkout session with mode=payment")
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': {
                        'name': 'Dutching Turf Premium - 1 Mois',
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
        
        db = get_db()
        db.subscriptions.update_one(
            {'user_id': ObjectId(user_id)},
            {'$set': {
                'stripe_subscription_id': checkout_session.id,
                'access_expires_at': access_expires,
                'updated_at': datetime.now()
            }}
        )
        
        print(f"[SUCCESS] Payment received for user {user_id}. Access until {access_expires}")
        return redirect(url_for('dashboard'))
    
    except Exception as e:
        print(f"[ERROR] Success page error: {str(e)}")
        import traceback
        traceback.print_exc()
        return redirect(url_for('dashboard'))

# ============================================
# API ROUTES - SUBSCRIPTION INFO
# ============================================

@app.route('/api/subscription-info')
@login_required
def subscription_info():
    """Retourne les infos d'abonnement de l'utilisateur"""
    user_id = session['user_id']
    try:
        db = get_db()
        subscription = db.subscriptions.find_one({'user_id': ObjectId(user_id)})
        
        if subscription and subscription.get('access_expires_at'):
            expires_at = subscription['access_expires_at']
            days_left = (expires_at - datetime.now()).days
            
            return jsonify({
                'has_subscription': True,
                'expires_at': expires_at.strftime('%d/%m/%Y'),
                'expires_at_iso': expires_at.isoformat(),
                'days_left': max(0, days_left),
                'is_valid': datetime.now() < expires_at
            })
        else:
            return jsonify({
                'has_subscription': False,
                'expires_at': None,
                'days_left': 0,
                'is_valid': False
            })
    except Exception as e:
        print(f"[ERROR] subscription_info: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ============================================
# API ROUTES - PMU DATA (PREMIUM ONLY)
# ============================================

@app.route('/api/<path:path>')
@premium_required
def api_proxy(path):
    """Proxy générique pour l'API PMU - Premium only"""
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
        print(f"[API ERROR] {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/pmu/<path:path>')
@premium_required
def pmu_api(path):
    """Proxy vers l'API PMU - Premium only (alternative route)"""
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
# SCRAPING ROUTES (PREMIUM ONLY)
# ============================================

@app.route('/paristurf/<date_str>/<rc>')
@premium_required
def paristurf_pronos(date_str, rc):
    """Pronos Paris-Turf"""
    if not SCRAPER_AVAILABLE:
        return jsonify({"tips": [], "records": {}}), 503
    
    try:
        import re
        m = re.match(r'R(\d+)C(\d+)', rc)
        if not m:
            return jsonify({"tips": [], "records": {}}), 400
        
        num_r, num_c = int(m.group(1)), int(m.group(2))
        result = get_paristurf_pronos(date_str, num_r, num_c)
        return jsonify(result)
    except Exception as e:
        print(f"[ERROR] Paris-Turf: {str(e)}")
        return jsonify({"tips": [], "records": {}}), 500

@app.route('/records/<date_str>/<rc>')
@premium_required
def records_km(date_str, rc):
    """Records km des chevaux"""
    if not SCRAPER_AVAILABLE:
        return jsonify({}), 503
    
    try:
        import re
        m = re.match(r'R(\d+)C(\d+)', rc)
        if not m:
            return jsonify({}), 400
        
        num_r, num_c = int(m.group(1)), int(m.group(2))
        
        horse_names = {}
        for key, value in request.args.items():
            try:
                num = int(key)
                horse_names[num] = value
            except:
                pass
        
        result = get_records_km(date_str, num_r, num_c, horse_names)
        return jsonify(result)
    except Exception as e:
        print(f"[ERROR] Records: {str(e)}")
        return jsonify({}), 500

@app.route('/turfomania/<date_str>/<rc>')
@premium_required
def turfomania_pronos(date_str, rc):
    """Pronos Turfomania"""
    if not SCRAPER_AVAILABLE:
        return jsonify({"pronos": [], "source": "Turfomania"}), 503
    
    try:
        import re
        m = re.match(r'R(\d+)C(\d+)', rc)
        if not m:
            return jsonify({"pronos": []}), 400
        
        num_r, num_c = int(m.group(1)), int(m.group(2))
        result = get_turfomania_pronos(date_str, num_r, num_c)
        return jsonify(result)
    except Exception as e:
        print(f"[ERROR] Turfomania: {str(e)}")
        return jsonify({"pronos": [], "source": "Turfomania"}), 500

# ============================================
# HEALTH CHECK
# ============================================

@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'app': 'Dutching Turf',
        'timestamp': datetime.now().isoformat(),
        'scraper': SCRAPER_AVAILABLE,
        'database': 'MongoDB'
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