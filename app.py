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
import secrets
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

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
    reset_token = db.Column(db.String(256), unique=True, nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)
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
# SCHEDULED TASKS - EMAIL REMINDERS
# ============================================

def send_expiration_reminders():
    """Envoie des rappels email 7 jours avant expiration"""
    try:
        with app.app_context():
            # Chercher les abonnements qui expirent dans 7 jours (±1 heure)
            target_date = datetime.now() + timedelta(days=7)
            start_time = target_date - timedelta(hours=1)
            end_time = target_date + timedelta(hours=1)
            
            expiring_subs = Subscription.query.filter(
                Subscription.access_expires_at.between(start_time, end_time),
                Subscription.access_expires_at != None
            ).all()
            
            print(f"[REMINDER] Found {len(expiring_subs)} subscriptions expiring in ~7 days")
            
            for sub in expiring_subs:
                try:
                    user = User.query.get(sub.user_id)
                    if not user:
                        continue
                    
                    # Envoyer email avec SendGrid
                    if SENDGRID_API_KEY:
                        sg = SendGridAPIClient(SENDGRID_API_KEY)
                        
                        expires_at = sub.access_expires_at.strftime('%d/%m/%Y')
                        
                        message = Mail(
                            from_email=SENDGRID_FROM_EMAIL,
                            to_emails=user.email,
                            subject='⏰ Votre abonnement Dutching Turf expire bientôt !',
                            plain_text_content=f"""Bonjour,

Votre abonnement à Dutching Turf Premium expire le {expires_at}.

Pour continuer à accéder à l'analyse complète, pensez à vous réabonner !

👉 Se réabonner : https://web-production-b3d28.up.railway.app/pricing

Questions ? Contactez-nous : https://web-production-b3d28.up.railway.app/contact

À bientôt ! 🏇

---
Dutching Turf Team""",
                            html_content=f"""
                            <html>
                                <body style="font-family: Arial, sans-serif; background: #f0f2f5; padding: 20px;">
                                    <div style="background: white; border-radius: 10px; padding: 30px; max-width: 600px; margin: 0 auto; box-shadow: 0 2px 10px rgba(0,0,0,0.1)">
                                        <h1 style="color: #667eea; margin-bottom: 20px">⏰ Votre abonnement expire bientôt !</h1>
                                        
                                        <p style="color: #333; font-size: 16px; line-height: 1.6">Votre abonnement à Dutching Turf Premium expire le <strong>{expires_at}</strong>.</p>
                                        
                                        <p style="color: #333; margin: 20px 0">Pour continuer à accéder à tous les outils d'analyse, pensez à vous réabonner dès maintenant !</p>
                                        
                                        <div style="text-align: center; margin: 30px 0">
                                            <a href="https://web-production-b3d28.up.railway.app/pricing" style="background: #667eea; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold">Se réabonner (9,99€/mois)</a>
                                        </div>
                                        
                                        <p style="color: #666; font-size: 14px">Une fois réabonné, l'accès sera rétabli immédiatement.</p>
                                        
                                        <p style="color: #999; font-size: 12px; margin-top: 20px">Besoin d'aide ? <a href="https://web-production-b3d28.up.railway.app/contact" style="color: #667eea">Contactez-nous</a></p>
                                    </div>
                                </body>
                            </html>
                            """
                        )
                        
                        sg.send(message)
                        print(f"[REMINDER] Email sent to {user.email} (expires {expires_at})")
                except Exception as email_err:
                    print(f"[REMINDER ERROR] Failed to send to {user.email}: {str(email_err)}")
    
    except Exception as e:
        print(f"[SCHEDULER ERROR] send_expiration_reminders: {str(e)}")

# Initialiser le scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=send_expiration_reminders, trigger="cron", hour=9, minute=0)
scheduler.start()

# Arrêter le scheduler quand l'app s'arrête
atexit.register(lambda: scheduler.shutdown())

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
# PASSWORD RESET ROUTES
# ============================================

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        
        if not email:
            return render_template('forgot_password.html', error='Email requis')
        
        try:
            user = User.query.filter_by(email=email).first()
            
            if user:
                reset_token = secrets.token_urlsafe(32)
                user.reset_token = reset_token
                user.reset_token_expires = datetime.now() + timedelta(hours=1)
                
                db.session.commit()
                
                try:
                    if SENDGRID_API_KEY:
                        sg = SendGridAPIClient(SENDGRID_API_KEY)
                        
                        reset_link = f"{request.host_url}reset-password/{reset_token}"
                        
                        message = Mail(
                            from_email=SENDGRID_FROM_EMAIL,
                            to_emails=user.email,
                            subject='🔐 Réinitialiser votre mot de passe - Dutching Turf',
                            plain_text_content=f"""Bonjour,

Vous avez demandé à réinitialiser votre mot de passe.

Cliquez sur ce lien pour créer un nouveau mot de passe (lien valide 1 heure) :
{reset_link}

Si vous n'avez pas demandé cette réinitialisation, ignorez cet email.

---
Dutching Turf Team""",
                            html_content=f"""
                            <html>
                                <body style="font-family: Arial, sans-serif; background: #f0f2f5; padding: 20px;">
                                    <div style="background: white; border-radius: 10px; padding: 30px; max-width: 600px; margin: 0 auto; box-shadow: 0 2px 10px rgba(0,0,0,0.1)">
                                        <h1 style="color: #667eea; margin-bottom: 20px">🔐 Réinitialiser votre mot de passe</h1>
                                        
                                        <p style="color: #333; font-size: 16px; line-height: 1.6">Vous avez demandé à réinitialiser votre mot de passe Dutching Turf.</p>
                                        
                                        <p style="color: #333; margin: 20px 0">Cliquez sur le bouton ci-dessous pour créer un nouveau mot de passe :</p>
                                        
                                        <div style="text-align: center; margin: 30px 0">
                                            <a href="{reset_link}" style="background: #667eea; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold">Réinitialiser le mot de passe</a>
                                        </div>
                                        
                                        <p style="color: #666; font-size: 14px">Ce lien expire dans 1 heure.</p>
                                        
                                        <p style="color: #999; font-size: 12px; margin-top: 20px">Si vous n'avez pas demandé cette réinitialisation, vous pouvez ignorer cet email en toute sécurité.</p>
                                    </div>
                                </body>
                            </html>
                            """
                        )
                        
                        sg.send(message)
                        print(f"[EMAIL] Reset link sent to {user.email}")
                except Exception as email_err:
                    print(f"[EMAIL ERROR] {str(email_err)}")
            
            return render_template('forgot_password.html', success='Si cet email existe, vous recevrez un lien de réinitialisation')
        
        except Exception as e:
            print(f"[FORGOT PASSWORD ERROR] {str(e)}")
            return render_template('forgot_password.html', error='Une erreur s\'est produite')
    
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        user = User.query.filter_by(reset_token=token).first()
        
        if not user or not user.reset_token_expires or datetime.now() > user.reset_token_expires:
            return render_template('reset_password.html', error='Lien invalide ou expiré', token=None)
        
        if request.method == 'POST':
            password = request.form.get('password', '').strip()
            password_confirm = request.form.get('password_confirm', '').strip()
            
            if not password or not password_confirm:
                return render_template('reset_password.html', error='Tous les champs sont requis', token=token)
            
            if password != password_confirm:
                return render_template('reset_password.html', error='Les mots de passe ne correspondent pas', token=token)
            
            if len(password) < 6:
                return render_template('reset_password.html', error='Le mot de passe doit faire au moins 6 caractères', token=token)
            
            user.password = hashlib.sha256(password.encode()).hexdigest()
            user.reset_token = None
            user.reset_token_expires = None
            
            db.session.commit()
            
            print(f"[PASSWORD RESET] Password reset for {user.email}")
            return render_template('reset_password.html', success='Mot de passe réinitialisé avec succès ! Vous pouvez maintenant vous connecter.')
        
        return render_template('reset_password.html', token=token)
    
    except Exception as e:
        print(f"[RESET PASSWORD ERROR] {str(e)}")
        return render_template('reset_password.html', error='Une erreur s\'est produite', token=None)

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
        
        user = User.query.get(user_id)
        if not user:
            return redirect(url_for('dashboard'))
        
        access_expires = datetime.now() + timedelta(days=30)
        
        subscription = Subscription.query.filter_by(user_id=user_id).first()
        subscription.stripe_subscription_id = checkout_session.id
        subscription.access_expires_at = access_expires
        subscription.updated_at = datetime.now()
        
        db.session.commit()
        
        try:
            if SENDGRID_API_KEY:
                sg = SendGridAPIClient(SENDGRID_API_KEY)
                
                message = Mail(
                    from_email=SENDGRID_FROM_EMAIL,
                    to_emails=user.email,
                    subject='✅ Bienvenue à Dutching Turf Premium !',
                    plain_text_content=f"""Bonjour,

Merci pour votre abonnement à Dutching Turf Premium ! 🎉

🎟️ DÉTAILS DE VOTRE ABONNEMENT:
• Montant: 9,99€
• Durée: 30 jours
• Expire le: {access_expires.strftime('%d/%m/%Y')}
• Accès: Illimité au dashboard complet

🚀 COMMENCEZ MAINTENANT:
Allez sur votre dashboard pour commencer à analyser !

À bientôt sur Dutching Turf! 🏇

---
Dutching Turf Team""",
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
                                
                                <p style="color: #666; font-size: 14px">Besoin d'aide ? <a href="/contact" style="color: #667eea">Contactez-nous</a></p>
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
# API ROUTES
# ============================================

@app.route('/api/subscription-info')
@login_required
def subscription_info():
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

@app.route('/api/<path:path>')
@premium_required
def api_proxy(path):
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

@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'app': 'Dutching Turf',
        'timestamp': datetime.now().isoformat()
    })

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