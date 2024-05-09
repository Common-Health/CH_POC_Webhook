import functions_framework
from flask import jsonify, request
import firebase_admin
from firebase_admin import credentials, db, auth, messaging
import base64
from Crypto.Util.Padding import pad
from dotenv import load_dotenv
from Crypto.Cipher import AES
import os
import json
from flask import Flask, request, redirect, jsonify
from dotenv import load_dotenv
from helpers.salesforce_access import find_user_via_opportunity_id, create_payment_history

load_dotenv()
my_credentials = {
    "type": "service_account",
    "project_id": "common-health-app",
    "private_key_id": os.getenv("PRIVATE_KEY_ID"),
    "private_key": os.getenv("PRIVATE_KEY_G").replace(r'\n', '\n'),  # CHANGE HERE
    "client_email": os.getenv("CLIENT_EMAIL"),
    "client_id": os.getenv("CLIENT_ID"),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": os.getenv("AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("CLIENT_X509_CERT_URL")
}
db_url=os.getenv('DB_URL')
cred = credentials.Certificate(my_credentials)
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
    
def send_fcm_notification(token):
    message = messaging.Message(
        token=token,
        notification=messaging.Notification(
            title='Hello!',
            body='This is an FCM notification message!'
        )
    )

    # Send a message to the device corresponding to the provided token
    response = messaging.send(message)
    print('Successfully sent message:', response)
    
@app.route('/api/check_payment', methods=['POST'])
def check_payment_status():
    if request.method == 'POST':
        try:
            received_data = request.get_json(silent=True)
            payment_result = received_data["paymentResult"]
            decrypted_result = PrpCrypt().decrypt(payment_result)
            result_json = json.loads(decrypted_result)

            # Store data in Firestore
            customer_name = result_json.get('customerName')

            if not customer_name:
                return jsonify({'error': 'customerName is required'}), 400

            # Save the data in Realtime Database
            ref = db.reference('/customers')
            customer_ref = ref.child(customer_name)
            customer_ref.set(result_json)

            opportunity_id = result_json.get('merchantOrderId')
            method_name = result_json.get('methodName')
            provider_name = result_json.get('providerName')
            total_amount = int(result_json.get('totalAmount'))
            transaction_id = result_json.get('transactionId')
            status = result_json.get('transactionStatus')

            fcm_token = find_user_via_opportunity_id(opportunity_id)
            create_payment_history(opportunity_id,method_name,provider_name,total_amount, transaction_id,status)

            send_fcm_notification(fcm_token)

            return jsonify(result_json), 200
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)