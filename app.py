from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import os
from flask_sqlalchemy import SQLAlchemy
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from sqlalchemy import func
import stripe
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
import hashlib
import json

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

# Database configuration
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    if DATABASE_URL.startswith('postgresql://'):
        DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg2://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or 'sqlite:///pmu_dutching.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# SendGrid configuration
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
SENDGRID_FROM_EMAIL = os.environ.get('MAIL_USERNAME', 'luclouis60100@gmail.com')

# Stripe configuration
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_your_key_here')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_your_key_here')

# ============================================
# DATABASE MODELS
# ============================================

class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    subscription = db.relationship('Subscription', backref='user', uselist=False, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<User {self.email}>'

class Subscription(db.Model):
    __tablename__ = 'subscriptions'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False, index=True)
    stripe_customer_id = db.Column(db.String(120))
    stripe_subscription_id = db.Column(db.String(120))
    access_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    def __repr__(self):
        return f'<Subscription user_id={self.user_id}>'

# ============================================
# CREATE TABLES
# ============================================

def init_db():
    """Initialize database tables"""
    try:
        with app.app_context():
            db.create_all()
            print("[DB] PostgreSQL initialized successfully")
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
            subscription = Subscription.query.filter_by(user_id=user_id).first()
            
            if not subscription or subscription.access_expires_at is None:
                return redirect(url_for('pricing'))
            
            if datetime.now() > subscription.access_expires_at:
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
            subscription = Subscription.query.filter_by(user_id=user_id).first()
            
            if subscription and subscription.access_expires_at:
                if datetime.now() < subscription.access_expires_at:
                    return redirect(url_for('dashboard'))
        except:
            pass
        
        return redirect(url_for('pricing'))
    
    return render_template('index.html')

# ============================================
# CONTACT ROUTE
# ============================================

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    # Le formulaire utilise Formspree, pas besoin de gérer le POST ici
    return render_template('contact.html')

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
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                return render_template('signup.html', error='Cet email est déjà inscrit')
            
            hashed_password = hashlib.sha256(password.encode()).hexdigest()
            
            new_user = User(email=email, password=hashed_password)
            db.session.add(new_user)
            db.session.flush()
            
            new_subscription = Subscription(user_id=new_user.id)
            db.session.add(new_subscription)
            
            db.session.commit()
            
            session['user_id'] = new_user.id
            session['email'] = new_user.email
            return redirect(url_for('pricing'))
            
        except Exception as e:
            db.session.rollback()
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
            user = User.query.filter_by(email=email, password=hashed_password).first()
            
            if user:
                session['user_id'] = user.id
                session['email'] = user.email
                
                try:
                    subscription = Subscription.query.filter_by(user_id=user.id).first()
                    
                    if subscription and subscription.access_expires_at:
                        if datetime.now() < subscription.access_expires_at:
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
            subscription = Subscription.query.filter_by(user_id=user_id).first()
            
            if subscription and subscription.access_expires_at:
                if datetime.now() < subscription.access_expires_at:
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
            subscription = Subscription.query.filter_by(user_id=user_id).first()
            
            if subscription and subscription.stripe_customer_id:
                customer_id = subscription.stripe_customer_id
                print(f"[DEBUG] Using existing customer: {customer_id}")
            else:
                print(f"[DEBUG] Creating new Stripe customer")
                customer = stripe.Customer.create(email=email)
                customer_id = customer.id
                print(f"[DEBUG] New customer created: {customer_id}")
                subscription.stripe_customer_id = customer_id
                db.session.commit()
        except Exception as db_err:
            db.session.rollback()
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
        
        # Récupérer l'utilisateur pour l'email
        user = User.query.get(user_id)
        if not user:
            return redirect(url_for('dashboard'))
        
        access_expires = datetime.now() + timedelta(days=30)
        
        subscription = Subscription.query.filter_by(user_id=user_id).first()
        subscription.stripe_subscription_id = checkout_session.id
        subscription.access_expires_at = access_expires
        subscription.updated_at = datetime.now()
        
        db.session.commit()
        
        # 📧 Envoyer email de confirmation avec SendGrid
        try:
            if SENDGRID_API_KEY:
                sg = SendGridAPIClient(SENDGRID_API_KEY)
                
                email_content = f"""
Bonjour,

Merci pour votre abonnement à Dutching Turf Premium ! 🎉

🎟️ DÉTAILS DE VOTRE ABONNEMENT:
• Montant: 9,99€
• Durée: 30 jours
• Expire le: {access_expires.strftime('%d/%m/%Y')}
• Accès: Illimité au dashboard complet

🚀 COMMENCEZ MAINTENANT:
1. Allez sur: https://pmu-dutching-tool.onrender.com/dashboard
2. Sélectionnez une course
3. Analysez les chevaux avec l'outil musicale

❓ BESOIN D'AIDE?
Contactez-nous: https://pmu-dutching-tool.onrender.com/contact

À bientôt sur Dutching Turf! 🏇

---
Dutching Turf Team
                """
                
                message = Mail(
                    from_email=SENDGRID_FROM_EMAIL,
                    to_emails=user.email,
                    subject='✅ Bienvenue à Dutching Turf Premium !',
                    plain_text_content=email_content,
                    html_content=f"""
                    <html>
                        <body style="font-family: Arial, sans-serif; background: #f0f2f5; padding: 20px;">
                            <div style="background: white; border-radius: 10px; padding: 30px; max-width: 600px; margin: 0 auto; box-shadow: 0 2px 10px rgba(0,0,0,0.1)">
                                <h1 style="color: #667eea; margin-bottom: 20px">✅ Bienvenue à Dutching Turf Premium!</h1>
                                
                                <p style="color: #333; font-size: 16px; line-height: 1.6">Merci pour votre abonnement ! 🎉</p>
                                
                                <div style="background: #f8f9fb; border-left: 4px solid #667eea; padding: 15px; margin: 20px 0; border-radius: 5px">
                                    <h3 style="color: #667eea; margin-top: 0">Détails de votre abonnement:</h3>
                                    <p style="margin: 5px 0"><strong>Montant:</strong> 9,99€</p>
                                    <p style="margin: 5px 0"><strong>Durée:</strong> 30 jours</p>
                                    <p style="margin: 5px 0"><strong>Expire le:</strong> {access_expires.strftime('%d/%m/%Y')}</p>
                                    <p style="margin: 5px 0"><strong>Accès:</strong> Illimité ✅</p>
                                </div>
                                
                                <div style="text-align: center; margin: 30px 0">
                                    <a href="https://pmu-dutching-tool.onrender.com/dashboard" style="background: #667eea; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold">Aller au Dashboard</a>
                                </div>
                                
                                <p style="color: #666; font-size: 14px">Besoin d'aide ? <a href="https://pmu-dutching-tool.onrender.com/contact" style="color: #667eea">Contactez-nous</a></p>
                            </div>
                        </body>
                    </html>
                    """
                )
                
                sg.send(message)
                print(f"[EMAIL] Confirmation envoyée à {user.email}")
        except Exception as email_err:
            print(f"[EMAIL ERROR] {str(email_err)}")
        
        print(f"[SUCCESS] Payment received for user {user_id}. Access until {access_expires}")
        return redirect(url_for('dashboard'))
    
    except Exception as e:
        db.session.rollback()
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
        subscription = Subscription.query.filter_by(user_id=user_id).first()
        
        if subscription and subscription.access_expires_at:
            expires_at = subscription.access_expires_at
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
        'database': 'PostgreSQL'
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