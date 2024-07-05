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
import logging
import sys
import time
import uuid
import requests
from flask import Flask, request, redirect, jsonify
from dotenv import load_dotenv
from helpers.salesforce_access import find_user_via_opportunity_id, update_payment_history, update_salesforce, create_draft_order, complete_draft_order, update_salesforce_account, find_opportunity_by_shopify_order_id, find_inventory_by_variant_id, find_opportunity_items_by_opportunity_id, update_opportunity_item, find_user_via_merchant_order_id, update_opportunity_sf, create_opportunity_item, delete_opportunity_item
from helpers.MPU_payment import verify_payment_response

load_dotenv()
CLIENT_SECRET=os.getenv("WEBHOOK_SIGN_KEY")
dev_credentials = {
    "type": "service_account",
    "project_id": os.getenv("DEV_PROJECT_ID"),
    "private_key_id": os.getenv("DEV_PRIVATE_KEY_ID"),
    "private_key": os.getenv("DEV_PRIVATE_KEY_G").replace(r'\n', '\n'),
    "client_email": os.getenv("DEV_CLIENT_EMAIL"),
    "client_id": os.getenv("DEV_CLIENT_ID"),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": os.getenv("DEV_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("DEV_CLIENT_X509_CERT_URL")
}
dev_db_url = os.getenv('DEV_DB_URL')

prod_credentials = {
    "type": "service_account",
    "project_id": os.getenv("PROD_PROJECT_ID"),
    "private_key_id": os.getenv("PROD_PRIVATE_KEY_ID"),
    "private_key": os.getenv("PROD_PRIVATE_KEY_G").replace(r'\n', '\n'),
    "client_email": os.getenv("PROD_CLIENT_EMAIL"),
    "client_id": os.getenv("PROD_CLIENT_ID"),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": os.getenv("PROD_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("PROD_CLIENT_X509_CERT_URL")
}
prod_db_url = os.getenv('PROD_DB_URL')

def initialize_firebase(credentials_path, db_url, name=None):
    try:
        # Check if the app is already initialized
        if name:
            firebase_admin.get_app(name)
            print(f'App {name} already initialized.')
        else:
            firebase_admin.get_app()
            print('Default app already initialized.')
    except ValueError:
        cred = credentials.Certificate(credentials_path)
        options = {
            'databaseURL': db_url
        }
        
        if name:
            firebase_admin.initialize_app(cred, options, name=name)
            print(f'Initialized app {name} with DB URL: {db_url}')
        else:
            firebase_admin.initialize_app(cred, options)
            print(f'Initialized default app with DB URL: {db_url}')

# Initialize with development credentials
initialize_firebase(dev_credentials, dev_db_url)
CUSTOM_HEADER = os.getenv('CUSTOM_HEADER')
SECRET_KEY = os.getenv('SECRET_KEY')
CLOUD_API_KEY = os.getenv('CLOUD_API_KEY')
SHEET_ID = os.getenv('SHEET_ID')
SHEET_NAME = os.getenv('SHEET_NAME')
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)  # Log to standard output
    ]
)

def get_notification(language, tag):
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_NAME}?key={CLOUD_API_KEY}'
    response = requests.get(url)
    data = response.json()

    headers = data['values'][0]
    rows = data['values'][1:]
    
    for row in rows:
        record = dict(zip(headers, row))
        if record['Language'] == language and record['Tag'] == tag:
            return record
    return None

def get_app(name=None):
    try:
        return firebase_admin.get_app(name=name)
    except ValueError:
        return None

def generate_notification_id():
    # Generate a unique notification ID
    return str(uuid.uuid4())

def send_fcm_notification(message):
    try:
        # Send message with development app (default)
        response = messaging.send(message)
        print('Successfully sent message with development credentials:', response)
        return response
    except Exception as e:
        print('Failed to send message with development credentials:', e)
        
        # Fallback to production credentials
        try:
            initialize_firebase(prod_credentials, prod_db_url, "prod")
            prod_app = firebase_admin.get_app("prod")
            
            response = messaging.send(message, app=prod_app)
            print('Successfully sent message with production credentials:', response)
            return response
        except Exception as prod_e:
            print('Failed to send message with production credentials:', prod_e)
        finally:
            # Ensure the development app is set back as default
            try:
                initialize_firebase(dev_credentials, dev_db_url)
            except ValueError as dev_e:
                print('Development app reinitialization failed:', dev_e)

@app.route('/api/send_fcm_message', methods=['POST'])
def send_message():
    try:
        received_data = request.get_json()
        custom_header = request.headers.get('Custom-Header')
    
        # Validate the custom header
        if not custom_header:
            # If the header is missing, return a 400 Bad Request response
            return jsonify(error='Custom-Header is missing'), 400
        
        # Add your specific validation logic here
        if custom_header != CUSTOM_HEADER:
            # If the header value is not what you expect, return a 400 Bad Request response
            return jsonify(error='Invalid value for Custom-Header'), 400
        
        message = received_data.get('message')
        notif_title = received_data.get('title')
        opportunity_id = received_data.get('opportunityId')
        fcm_token = received_data.get('fcmToken')
        data = received_data.get('data', {})  # Get the 'data' dictionary if present, otherwise an empty dict

        notification_id = generate_notification_id()
        data['notification_id'] = notification_id
        data['title'] = notif_title
        data['body'] = message
        
        if not message or not notif_title:
            return jsonify(error='Message and title are required fields'), 400

        if not opportunity_id and not fcm_token:
            return jsonify(error='Either opportunityId or fcmToken must be provided'), 400
        
        if opportunity_id:
            user_details = find_user_via_opportunity_id(opportunity_id)
            fcm_token = user_details['fcm_token']
            name = user_details['name']
            language = user_details['language']
        
        notification = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title=notif_title,
                body=message
            ),
            android=messaging.AndroidConfig(
                ttl=2419200,
                priority='high'
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True
                    )
                )
            ),
            data=data  # Attach the optional data dictionary
        )
        logging.info(message)
        response = send_fcm_notification(notification)
        if notif_title == "Refill Reminder" and opportunity_id:
            update_opportunity_sf("Ordered", opportunity_id)
        return jsonify(success=True, response=response), 200

    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/api/send_fcm_message/phone_update', methods=['POST'])
def send_message_phone_update():
    try:
        received_data = request.get_json()
        
        fcm_token = received_data.get('fcmToken')
        name = received_data.get('name')
        language = received_data.get('language')
        old_phone_number = received_data.get('oldPhoneNumber')
        new_phone_number = received_data.get('newPhoneNumber')
        tag = "phone_update"

        if not language:
            language = "English"

        notification = get_notification(language, tag)
        data = received_data.get('data', {})  # Get the 'data' dictionary if present, otherwise an empty dict

        if notification:
            message = notification['Message'].replace("{Name}", name).replace("{maskedOldPhone}", old_phone_number).replace("{maskedNewPhone}", new_phone_number)
            notif_title = notification['Title']

        notification_id = generate_notification_id()
        data['notification_id'] = notification_id
        data['title'] = notif_title
        data['body'] = message
        
        if not message or not notif_title:
            return jsonify(error='Message and title are required fields'), 400

        if not fcm_token:
            return jsonify(error='Either opportunityId or fcmToken must be provided'), 400
        
        notification = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title=notif_title,
                body=message
            ),
            android=messaging.AndroidConfig(
                ttl=2419200,
                priority='high'
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True
                    )
                )
            ),
            data=data  # Attach the optional data dictionary
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"Attempt {attempt}: Sending notification: {message}")
                response = send_fcm_notification(notification)
                return jsonify(success=True, response=response), 200
            except Exception as e:
                logging.error(f"Attempt {attempt}: Error sending notification: {str(e)}")
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logging.info(f"Retrying after {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logging.error(f"All retry attempts failed. Error: {str(e)}")
                    return jsonify(error='Failed to send notification after retries'), 500

    except Exception as e:
        return jsonify(error=str(e)), 500
    
@app.route('/api/send_fcm_message/refill', methods=['POST'])
def send_message_refill():
    try:
        received_data = request.get_json()
        
        opportunity_id = received_data.get('opportunityId')
        tag = "refill_reminder"
        user_details = find_user_via_opportunity_id(opportunity_id)
        fcm_token = user_details['fcm_token']
        name = user_details['name']
        language = user_details['language']
        delivery_date = user_details['delivery_date']
        data = received_data.get('data', {})  # Get the 'data' dictionary if present, otherwise an empty dict

        if not language:
            language = "English"

        notification = get_notification(language, tag)
        if notification:
            notif_message = notification['Message'].replace("{Name}", name).replace("{deliverySLADate}", delivery_date)
            notif_title = notification['Title']

        notification_id = generate_notification_id()
        data['notification_id'] = notification_id
        data['title'] = notif_title
        data['body'] = notif_message
        
        if not notif_message or not notif_title:
            return jsonify(error='Message and title are required fields'), 400

        if not fcm_token:
            return jsonify(error='Either opportunityId or fcmToken must be provided'), 400
        
        notification = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title=notif_title,
                body=notif_message
            ),
            android=messaging.AndroidConfig(
                ttl=2419200,
                priority='high'
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True
                    )
                )
            ),
            data=data  # Attach the optional data dictionary
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"Attempt {attempt}: Sending notification: {notif_message}")
                response = send_fcm_notification(notification)
                return jsonify(success=True, response=response), 200
            except Exception as e:
                logging.error(f"Attempt {attempt}: Error sending notification: {str(e)}")
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logging.info(f"Retrying after {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logging.error(f"All retry attempts failed. Error: {str(e)}")
                    return jsonify(error='Failed to send notification after retries'), 500

    except Exception as e:
        logging.error(f"Error in refill: {str(e)}", exc_info=True)
        return jsonify(error=str(e)), 500
    
@app.route('/api/send_fcm_message/picked_up', methods=['POST'])
def send_message_picked_up():
    try:
        received_data = request.get_json()
        
        opportunity_id = received_data.get('opportunityId')
        opportunity_name = received_data.get('opportunityName')
        courier = received_data.get('courierName')
        tag = "picked_up"
        user_details = find_user_via_opportunity_id(opportunity_id)
        fcm_token = user_details['fcm_token']
        language = user_details['language']
        delivery_time = user_details['delivery_time']
        data = received_data.get('data', {})  # Get the 'data' dictionary if present, otherwise an empty dict

        if not language:
            language = "English"

        notification = get_notification(language, tag)
        if notification:
            notif_message = notification['Message'].replace("{orderNumber}", opportunity_name).replace("{courier}", courier).replace("{deliveryTime}", delivery_time)
            notif_title = notification['Title']

        notification_id = generate_notification_id()
        data['notification_id'] = notification_id
        data['title'] = notif_title
        data['body'] = notif_message
        
        if not notif_message or not notif_title:
            return jsonify(error='Message and title are required fields'), 400

        if not fcm_token:
            return jsonify(error='Either opportunityId or fcmToken must be provided'), 400
        
        notification = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title=notif_title,
                body=notif_message
            ),
            android=messaging.AndroidConfig(
                ttl=2419200,
                priority='high'
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True
                    )
                )
            ),
            data=data  # Attach the optional data dictionary
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"Attempt {attempt}: Sending notification: {notif_message}")
                response = send_fcm_notification(notification)
                return jsonify(success=True, response=response), 200
            except Exception as e:
                logging.error(f"Attempt {attempt}: Error sending notification: {str(e)}")
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logging.info(f"Retrying after {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logging.error(f"All retry attempts failed. Error: {str(e)}")
                    return jsonify(error='Failed to send notification after retries'), 500

    except Exception as e:
        logging.error(f"Error in picked_up: {str(e)}", exc_info=True)
        return jsonify(error=str(e)), 500
    
@app.route('/api/send_fcm_message/delivered', methods=['POST'])
def send_message_delivered():
    try:
        received_data = request.get_json()
        
        opportunity_id = received_data.get('opportunityId')
        opportunity_name = received_data.get('opportunityName')
        tag = "delivered"
        user_details = find_user_via_opportunity_id(opportunity_id)
        fcm_token = user_details['fcm_token']
        language = user_details['language']
        data = received_data.get('data', {})  # Get the 'data' dictionary if present, otherwise an empty dict

        if not language:
            language = "English"

        notification = get_notification(language, tag)
        if notification:
            notif_message = notification['Message'].replace("{orderNumber}", opportunity_name)
            notif_title = notification['Title']

        notification_id = generate_notification_id()
        data['notification_id'] = notification_id
        data['title'] = notif_title
        data['body'] = notif_message
        
        if not notif_message or not notif_title:
            return jsonify(error='Message and title are required fields'), 400

        if not fcm_token:
            return jsonify(error='Either opportunityId or fcmToken must be provided'), 400
        
        notification = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title=notif_title,
                body=notif_message
            ),
            android=messaging.AndroidConfig(
                ttl=2419200,
                priority='high'
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True
                    )
                )
            ),
            data=data  # Attach the optional data dictionary
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"Attempt {attempt}: Sending notification: {notif_message}")
                response = send_fcm_notification(notification)
                return jsonify(success=True, response=response), 200
            except Exception as e:
                logging.error(f"Attempt {attempt}: Error sending notification: {str(e)}")
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logging.info(f"Retrying after {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logging.error(f"All retry attempts failed. Error: {str(e)}")
                    return jsonify(error='Failed to send notification after retries'), 500

    except Exception as e:
        logging.error(f"Error in delivered: {str(e)}", exc_info=True)
        return jsonify(error=str(e)), 500

@app.route('/api/send_fcm_message/deadline', methods=['POST'])
def send_message_deadline():
    try:
        received_data = request.get_json()
        
        opportunity_id = received_data.get('opportunityId')
        tag = "deadline"
        user_details = find_user_via_opportunity_id(opportunity_id)
        name= user_details['name']
        fcm_token = user_details['fcm_token']
        language = user_details['language']
        data = received_data.get('data', {})  # Get the 'data' dictionary if present, otherwise an empty dict

        if not language:
            language = "English"

        notification = get_notification(language, tag)
        if notification:
            notif_message = notification['Message'].replace("{Name}", name)
            notif_title = notification['Title']

        notification_id = generate_notification_id()
        data['notification_id'] = notification_id
        data['title'] = notif_title
        data['body'] = notif_message
        
        if not notif_message or not notif_title:
            return jsonify(error='Message and title are required fields'), 400

        if not fcm_token:
            return jsonify(error='Either opportunityId or fcmToken must be provided'), 400
        
        notification = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title=notif_title,
                body=notif_message
            ),
            android=messaging.AndroidConfig(
                ttl=2419200,
                priority='high'
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True
                    )
                )
            ),
            data=data  # Attach the optional data dictionary
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"Attempt {attempt}: Sending notification: {notif_message}")
                response = send_fcm_notification(notification)
                return jsonify(success=True, response=response), 200
            except Exception as e:
                logging.error(f"Attempt {attempt}: Error sending notification: {str(e)}")
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logging.info(f"Retrying after {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logging.error(f"All retry attempts failed. Error: {str(e)}")
                    return jsonify(error='Failed to send notification after retries'), 500

    except Exception as e:
        logging.error(f"Error in deadline: {str(e)}", exc_info=True)
        return jsonify(error=str(e)), 500

def convert_padded_amount(padded_amount):
    # Remove leading zeros
    amount = padded_amount.lstrip('0')
    
    # If the string is empty after stripping zeros, it means the amount is 0
    if not amount:
        return '0.00'
    
    # Insert the decimal point before the last two digits
    decimal_amount = amount[:-2] + '.' + amount[-2:]
    
    return decimal_amount

@app.route('/api/check_payment/MPU', methods=['POST'])
def check_payment_mpu():
    if request.method == 'POST':
        try:
            logging.info("Received POST request at /api/check_payment/MPU")
            trans_ref = request.form.get('tranRef')
            if trans_ref is None:
                trans_ref = request.form.get('transRef')
            response_values = {
                'merchantID': request.form.get('merchantID'),
                'respCode': request.form.get('respCode'),
                'pan': request.form.get('pan'),
                'amount': request.form.get('amount'),
                'invoiceNo': request.form.get('invoiceNo'),
                'transRef': trans_ref,
                'approvalCode': request.form.get('approvalCode'),
                'dateTime': request.form.get('dateTime'),
                'status': request.form.get('status'),
                'failReason': request.form.get('failReason'),
                'userDefined1': request.form.get('userDefined1'), 
                'userDefined2': request.form.get('userDefined2'),
                'userDefined3': request.form.get('userDefined3'),
                'categoryCode': request.form.get('categoryCode'),
                'hashValue': request.form.get('hashValue')  # Assuming the hashValue is also provided in the request
            }

            logging.info(f"Received response values: {response_values}")
            verification_result = verify_payment_response(response_values, SECRET_KEY)

            response = {
                "Signature String": verification_result['signature_string'],
                "Generated HMAC Signature": verification_result['generated_hash_value'],
                "Expected Hash Value": verification_result['expected_hash_value'],
                "Hashes match": verification_result['hashes_match']
            }
            logging.info(f"Verification result: {response}")

            if not response["Hashes match"]:
                logging.warning("Hash values do not match. Aborting request.")
                abort(401)

            merch_id = response_values['invoiceNo']
            status = response_values['status']
            total_amount = convert_padded_amount(response_values['amount'])
            transaction_id = response_values['transRef']
            method_name = "OTP"
            provider_name = "MPU"
            user_details = find_user_via_merchant_order_id(merch_id)
            fcm_token = user_details["fcm_token"]
            opportunity_id = user_details["opportunity_id"]
            payment_history_id = user_details["payment_history_id"]
            name = user_details['name']
            language = user_details['language']
            notification_id = generate_notification_id()
            delivery_details= find_user_via_opportunity_id(opportunity_id)
            delivery_date = delivery_details['delivery_date']

            update_payment_history(payment_history_id, merch_id, opportunity_id, method_name, provider_name, total_amount, transaction_id, status)

            if status.lower() == 'ap':
                tag = "payment_success"
            elif status.lower() == 'de':
                tag = "payment_declined"
            elif status.lower() == 'fa':
                tag = "payment_failed"
            else:
                tag = "payment_pending"

            if not language:
                language = "English"

            notification = get_notification(language, tag)
            if notification:
                notif_message = notification['Message'].replace("{Name}", name)
                if tag == "payment_pending":
                    notif_message= notif_message.replace("{deliverySLADate}", delivery_date)
                notif_title = notification['Title']

            logging.info(f"Notification message: {notif_message}")

            message = messaging.Message(
                token=fcm_token,
                notification=messaging.Notification(
                    title=notif_title,
                    body=notif_message
                ),
                android=messaging.AndroidConfig(
                    ttl=2419200,
                    priority='high'
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            content_available=True
                        )
                    )
                ),
                data={
                    "orderId": opportunity_id,
                    "action": "redirect_to_orders",
                    "notification_id": notification_id,
                    "title":notif_title,
                    "body":notif_message
                }
            )

            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    logging.info(f"Attempt {attempt}: Sending notification: {notif_message}")
                    response = send_fcm_notification(message)
                    return "success"
                except Exception as e:
                    logging.error(f"Attempt {attempt}: Error sending notification: {str(e)}")
                    if attempt < max_retries:
                        wait_time = 2 ** attempt  # Exponential backoff
                        logging.info(f"Retrying after {wait_time} seconds...")
                        time.sleep(wait_time)
                    else:
                        logging.error(f"All retry attempts failed. Error: {str(e)}")
                        return "success"

        except Exception as e:
            logging.error(f"Error in check_payment_mpu: {str(e)}", exc_info=True)
            return jsonify({"error": str(e)}), 500
    else:
        logging.warning("Method not allowed for /api/check_payment/MPU")
        return jsonify({"error": "Method not allowed"}), 405

@app.route('/api/check_payment', methods=['POST'])
def check_payment_status():
    if request.method == 'POST':
        try:
            received_data = request.get_json(silent=True)
            if received_data is None:
                return jsonify({"error": "No JSON data found in the request"}), 400
            try:
                payment_result = received_data["Request"]
            except:
                payment_result = received_data["Response"]

            status = payment_result.get("trade_status")
            merch_order_id = payment_result.get("merch_order_id")
            total_amount = payment_result.get("total_amount")
            transaction_id = payment_result.get("mm_order_id")
            method_name = "APP"
            provider_name = "KBZ Pay"


            user_details = find_user_via_merchant_order_id(merch_order_id)
            fcm_token = user_details["fcm_token"]
            opportunity_id = user_details["opportunity_id"]
            payment_history_id = user_details["payment_history_id"]
            name = user_details['name']
            language = user_details['language']
            notification_id = generate_notification_id()
            update_payment_history(payment_history_id, merch_order_id,opportunity_id,method_name,provider_name,total_amount, transaction_id,status)


            if status.lower() == 'pay_success':
                tag = "payment_success"
            else:
                tag = "payment_failed"

            if not language:
                language = "English"

            notification = get_notification(language, tag)
            if notification:
                notif_message = notification['Message'].replace("{Name}", name)
                notif_title = notification['Title']

            logging.info(f"Notification message: {notif_message}")

            message = messaging.Message(
                token=fcm_token,
                notification=messaging.Notification(
                    title=notif_title,
                    body=notif_message
                ),
                android=messaging.AndroidConfig(
                    ttl=2419200,
                    priority='high'
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            content_available=True
                        )
                    )
                ),
                data={
                    "orderId": opportunity_id,
                    "action": "redirect_to_orders",
                    "notification_id": notification_id,
                    "title":notif_title,
                    "body":notif_message
                }
            )

            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    logging.info(f"Attempt {attempt}: Sending notification: {notif_message}")
                    response = send_fcm_notification(message)
                    return "success"
                except Exception as e:
                    logging.error(f"Attempt {attempt}: Error sending notification: {str(e)}")
                    if attempt < max_retries:
                        wait_time = 2 ** attempt  # Exponential backoff
                        logging.info(f"Retrying after {wait_time} seconds...")
                        time.sleep(wait_time)
                    else:
                        logging.error(f"All retry attempts failed. Error: {str(e)}")
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

    failed_updates = [variant_id for variant_id, success in updates if not success]
    if failed_updates:
        logging.error(f"Failed to update Salesforce for variants: {failed_updates}")
        return jsonify(success=False, error="Failed to update Salesforce", failed_variants=failed_updates), 200
    else:
        return jsonify(success=True), 200
    
@app.route('/webhook/salesforce/create_shopify_order', methods=['POST'])
def create_shopify_order():
    try:
        data = request.json
        opportunity_id = data.get('opportunityId')
        response = create_draft_order(opportunity_id)
        user_details = find_user_via_opportunity_id(opportunity_id)
        fcm_token = user_details['fcm_token']
        name = user_details['name']
        language = user_details['language']
        notification_id = generate_notification_id()
        tag="new_order"

        if not language:
            language = "English"

        notification = get_notification(language, tag)
        if notification:
            notif_message = notification['Message'].replace("{Name}", name)
            notif_title = notification['Title']

        message = messaging.Message(
            token=fcm_token,
            android=messaging.AndroidConfig(
                ttl=2419200,
                priority='high'
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        content_available=True
                    )
                )
            ),
            data={
                "action": "refresh_orders",
                "notification_id": notification_id,
                "title":notif_title,
                "body":notif_message
            }
        )

        send_fcm_notification(message)
        return response
    except Exception as e:
        return str(e)

@app.route('/webhook/salesforce/process_opportunity', methods=['POST'])
def complete_shopify_order():
    try:
        data = request.json
        opportunity_id = data.get('opportunityId')
        response = complete_draft_order(opportunity_id)

        return response
    except Exception as e:
        return str(e)


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
    line_items = data['line_items']
    
    try:
        # Find Opportunity using Shopify Order ID
        opportunity_id = find_opportunity_by_shopify_order_id(draft_order_id)
        if not opportunity_id:
            logging.error(f"Opportunity not found for Shopify Order ID: {draft_order_id}")
            return jsonify({'success': False, 'error': 'Opportunity not found'}), 200

        # Fetch all opportunity items related to the opportunity
        opportunity_items = find_opportunity_items_by_opportunity_id(opportunity_id)
        if not opportunity_items:
            logging.error(f"Opportunity items not found for Opportunity ID: {opportunity_id}")
            return jsonify({'success': False, 'error': 'Opportunity items not found'}), 200

        updated_items = []

        # Iterate over each line item from the Shopify order
        for line_item in line_items:
            new_variant_id = line_item['variant_id']
            quantity = line_item['quantity']
            
            # Find Inventory Item using Variant ID and get price
            inventory_item = find_inventory_by_variant_id(new_variant_id)
            if not inventory_item:
                logging.error(f"Inventory item not found for Variant ID: {new_variant_id}")
                continue
            
            inventory_id = inventory_item['Id']
            price = inventory_item['Price__c']
            
            # Check if there is a corresponding opportunity item
            if opportunity_items:
                opportunity_item_id = opportunity_items.pop(0)['Id']
                update_result = update_opportunity_item(opportunity_item_id, inventory_id, quantity)
                logging.info(f"Successfully updated Opportunity Item: {update_result}")
                updated_items.append(update_result)
            else:
                # If no more opportunity items to update, create new opportunity item
                create_result = create_opportunity_item(opportunity_id, inventory_id, quantity, price)
                logging.info(f"Successfully created new Opportunity Item: {create_result}")
                updated_items.append(create_result)

        # Handle remaining opportunity items if any
        for remaining_item in opportunity_items:
            # Optionally: delete or mark as inactive if there's no corresponding Shopify line item
            delete_result = delete_opportunity_item(remaining_item['Id'])
            logging.info(f"Successfully deleted Opportunity Item: {delete_result}")

        return jsonify({'success': True, 'result': updated_items}), 200

    except Exception as e:
        logging.error(f"Error processing order update: {e}")
        return jsonify({'success': False, 'error': str(e)}), 200

if __name__ == "__main__":
    app.run()