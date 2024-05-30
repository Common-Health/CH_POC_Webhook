import functions_framework
from flask import jsonify, request, abort
import firebase_admin
from firebase_admin import credentials, db, auth, messaging
import base64
from Crypto.Util.Padding import pad
from dotenv import load_dotenv
from Crypto.Cipher import AES
import hmac
import hashlib
import basehash
import os
import json
from flask import Flask, request, redirect, jsonify
from dotenv import load_dotenv
from helpers.salesforce_access import find_user_via_opportunity_id, update_payment_history, update_salesforce, create_draft_order, complete_draft_order, update_salesforce_account, find_opportunity_by_shopify_order_id, find_inventory_by_variant_id, find_opportunity_item_by_opportunity_id, update_opportunity_item, find_user_via_merchant_order_id

load_dotenv()
CLIENT_SECRET=os.getenv("WEBHOOK_SIGN_KEY")
my_credentials = {
    "type": "service_account",
    "project_id": "common-health-app",
    "private_key_id": os.getenv("PRIVATE_KEY_ID"),
    "private_key": os.getenv("PRIVATE_KEY_G").replace(r'\n', '\n'),
    "client_email": os.getenv("CLIENT_EMAIL"),
    "client_id": os.getenv("CLIENT_ID"),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": os.getenv("AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("CLIENT_X509_CERT_URL")
}
db_url=os.getenv('DB_URL')
cred= credentials.Certificate(my_credentials)
firebase_admin.initialize_app(cred, {
    'databaseURL': db_url
})

app = Flask(__name__)

class PrpCrypt(object):
 
    def __init__(self):
        load_dotenv()
        self.key = os.getenv('PRIVATE_KEY')
        if not self.key:
            raise ValueError("No PRIVATE_KEY found in environment variables.")
        self.unpad = lambda date: date[0:-ord(date[-1])]
 
    def aes_cipher(self, aes_str):
        aes = AES.new(self.key.encode('utf-8'), AES.MODE_ECB)
        pad_pkcs7 = pad(aes_str.encode('utf-8'), AES.block_size, style='pkcs7') 
        encrypt_aes = aes.encrypt(pad_pkcs7)
        encrypted_text = str(base64.encodebytes(encrypt_aes), encoding='utf-8') 
        encrypted_text_str = encrypted_text.replace("\n", "")
 
        return encrypted_text_str
 
    def decrypt(self, decrData):
        res = base64.decodebytes(decrData.encode("utf8"))
        aes = AES.new(self.key.encode('utf-8'), AES.MODE_ECB)
        msg = aes.decrypt(res).decode("utf8")
        return self.unpad(msg)
    
def send_fcm_notification(message):
    # Send a message to the device corresponding to the provided token
    response = messaging.send(message)
    print('Successfully sent message:', response)
    
@app.route('/api/check_payment', methods=['POST'])
def check_payment_status():
    if request.method == 'POST':
        try:
            received_data = request.get_json(silent=True)
            payment_result = received_data["Request"]

            status = payment_result["trade_status"]
            merch_order_id = payment_result["merch_order_id"]
            total_amount = payment_result["total_amount"]
            transaction_id = payment_result["mm_order_id"]
            method_name = "APP"
            provider_name = "KBZ Pay"


            user_details = find_user_via_merchant_order_id(merch_order_id)
            fcm_token = user_details["fcm_token"]
            opportunity_id = user_details["opportunity_id"]
            payment_history_id = user_details["payment_history_id"]
            update_payment_history(payment_history_id, merch_order_id,opportunity_id,method_name,provider_name,total_amount, transaction_id,status)

            if status.lower() == 'pay_success':
                pay_status= "successful. Thank you for choosing Common Health."
            else:
                pay_status = "not successful."

            message = messaging.Message(
                token=fcm_token,
                notification=messaging.Notification(
                    title='Payment Update',
                    body=f'Your payment for your order in Common Health is {pay_status}'
                ),
                data={
                    "orderId": opportunity_id,
                    "action": "redirect_to_orders"
                }
            )

            try:
                send_fcm_notification(message)
            except Exception as e:
                print(f"Failed to send FCM notification: {str(e)}")
            return "success"
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "Method not allowed"}), 405

@app.route('/update_phone', methods=['POST'])
def update_phone():
    user_id = request.json.get('userId1')
    new_phone = request.json.get('newPhoneNumber')

    try:
        user = auth.update_user(user_id, phone_number=new_phone)
        return {'message': f'Successfully updated phone number for user {user.uid}'}, 200
    except Exception as e:
        return {'error': str(e)}, 400
    
def verify_webhook(data, hmac_header):
    digest = hmac.new(CLIENT_SECRET.encode('utf-8'), data, digestmod=hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest)
    return hmac.compare_digest(computed_hmac, hmac_header.encode('utf-8'))

@app.route('/webhook/shopify/product-update', methods=['POST'])
def handle_product_update():
    data = request.get_data()
    verified = verify_webhook(data, request.headers.get('X-Shopify-Hmac-SHA256'))

    if not verified:
        abort(401)

    # Attempt to parse JSON data
    try:
        json_data = request.json
        updates = []
        # Process each variant
        for variant in json_data['variants']:
            variant_id = str(variant['id'])
            price = variant['price']
            inventory_quantity = variant['inventory_quantity']
            print(f"Variant ID: {variant_id}, Price: {price}, Inventory: {inventory_quantity}")
            
            # Update Salesforce for each variant
            update_success = update_salesforce(variant_id, price, inventory_quantity)
            if not update_success:
                updates.append((variant_id, False))
            else:
                updates.append((variant_id, True))
    except Exception as e:
        print(f"Error parsing JSON or extracting data: {e}")
        abort(400)

    # Check if all updates were successful
    if all(success for _, success in updates):
        return jsonify(success=True), 200
    else:
        failed_updates = [variant_id for variant_id, success in updates if not success]
        return jsonify(success=False, error="Failed to update Salesforce", failed_variants=failed_updates), 500
    
@app.route('/webhook/salesforce/create_shopify_order', methods=['POST'])
def create_shopify_order():
    try:
        data = request.json
        opportunity_id = data.get('opportunityId')
        response = create_draft_order(opportunity_id)
        fcm_token = find_user_via_opportunity_id(opportunity_id)
        message = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title='New Orders',
                body=f'You have new orders on your Account. Please check the Orders tab to see your pending orders.'
            ),
            data={
                "action": "refresh_orders"
            }
        )

        send_fcm_notification(message)
        return response
    except Exception as e:
        return str(e)

@app.route('/webhook/salesforce/process_opportunity', methods=['POST'])
def complete_shopify_order():
    data = request.json
    opportunity_id = data.get('opportunityId')
    response = complete_draft_order(opportunity_id)

    return response


@app.route('/webhook/shopify/customer_create', methods=['POST'])
def handle_new_customer():
    data = request.get_data()
    verified = verify_webhook(data, request.headers.get('X-Shopify-Hmac-SHA256'))

    if not verified:
        abort(401)
    
    try:
        data = request.json
        
        if data and 'id' in data:
            shopify_customer_id = data['id']
            phone = data.get('phone', '')

            if not phone:
                return jsonify({'status': 'error', 'message': 'No phone number provided'}), 400

            # Call function from salesforce.py to update Salesforce
            update_salesforce_account(shopify_customer_id, phone)

            return jsonify({'status': 'success'}), 200
        else:
            return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/webhook/shopify/order_update', methods=['POST'])
def shopify_webhook():
    data = request.json
    draft_order_id = data['id']
    new_variant_id = data['line_items'][0]['variant_id']
    quantity = data['line_items'][0]['quantity']

    # Find Opportunity using Shopify Order ID
    opportunity_id = find_opportunity_by_shopify_order_id(draft_order_id)
    if not opportunity_id:
        return jsonify({'error': 'Opportunity not found'}), 404

    # Find Inventory Item using Variant ID
    inventory_id = find_inventory_by_variant_id(new_variant_id)
    if not inventory_id:
        return jsonify({'error': 'Inventory item not found'}), 404

    # Find Opportunity Item using Opportunity ID
    opportunity_item_id = find_opportunity_item_by_opportunity_id(opportunity_id)
    if not opportunity_item_id:
        return jsonify({'error': 'Opportunity item not found'}), 404

    # Update Opportunity Item
    update_result = update_opportunity_item(opportunity_item_id, inventory_id, quantity)

    return jsonify(update_result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)