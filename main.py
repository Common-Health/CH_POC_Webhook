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
from flask import Flask, request, redirect, jsonify
from dotenv import load_dotenv
from helpers.salesforce_access import find_user_via_opportunity_id, update_payment_history, update_salesforce, create_draft_order, complete_draft_order, update_salesforce_account, find_opportunity_by_shopify_order_id, find_inventory_by_variant_id, find_opportunity_items_by_opportunity_id, update_opportunity_item, find_user_via_merchant_order_id, update_opportunity_sf, create_opportunity_item, delete_opportunity_item
from helpers.MPU_payment import verify_payment_response

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
CUSTOM_HEADER = os.getenv('CUSTOM_HEADER')
SECRET_KEY = os.getenv('SECRET_KEY')
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)  # Log to standard output
    ]
)
    
def send_fcm_notification(message):
    # Send a message to the device corresponding to the provided token
    response = messaging.send(message)
    print('Successfully sent message:', response)

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
        data = received_data.get('data', {})  # Get the 'data' dictionary if present, otherwise an empty dict
        
        if not message or not notif_title or not opportunity_id:
            return jsonify(error='Message, title, and opportunityId are required fields'), 400

        user_details = find_user_via_opportunity_id(opportunity_id)
        fcm_token = user_details['fcm_token']
        notification = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title=notif_title,
                body=message
            ),
            data=data  # Attach the optional data dictionary
        )

        response = messaging.send(notification)
        if notif_title == "Refill Reminder":
            update_opportunity_sf("Ordered", opportunity_id)
        return jsonify(success=True, response=response), 200

    except Exception as e:
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
            
            response_values = {
                'merchantID': request.form.get('merchantID'),
                'respCode': request.form.get('respCode'),
                'pan': request.form.get('pan'),
                'amount': request.form.get('amount'),
                'invoiceNo': request.form.get('invoiceNo'),
                'transRef': request.form.get('tranRef'),
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

            update_payment_history(payment_history_id, merch_id, opportunity_id, method_name, provider_name, total_amount, transaction_id, status)

            if status.lower() == 'ap':
                pay_status = f"Hi {name}, your payment was successful. Thank you! Transaction details are available in your account."
            elif status.lower() == 'de':
                pay_status = f"Hi {name}, your payment via MPU was declined. Please check your details and try again. Contact support if you require assistance."
            elif status.lower() == 'fa':
                pay_status = f"Hi {name}, your payment attempt via MPU failed. Please check your details and try again. Contact support if you require assistance."
            else:
                pay_status = f"Hi {name}, your payment for medication is currently pending. We will inform you once we receive your payment. Thank you!"

            logging.info(f"Notification message: {pay_status}")

            message = messaging.Message(
                token=fcm_token,
                notification=messaging.Notification(
                    title='Payment Update',
                    body=pay_status
                ),
                data={
                    "orderId": opportunity_id,
                    "action": "redirect_to_orders"
                }
            )

            try:
                send_fcm_notification(message)
                logging.info("FCM notification sent successfully")
            except Exception as e:
                logging.error(f"Failed to send FCM notification: {str(e)}", exc_info=True)

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
            update_payment_history(payment_history_id, merch_order_id,opportunity_id,method_name,provider_name,total_amount, transaction_id,status)

            if status.lower() == 'pay_success':
                pay_status= f"Hi {name}, your payment was successful. Thank you! Transaction details are available in your account."
            else:
                pay_status = f"Hi {name}, your payment attempt failed. Please check your details and try again. Contact support if you require assistance"

            message = messaging.Message(
                token=fcm_token,
                notification=messaging.Notification(
                    title='Payment Update',
                    body=pay_status
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
        message = messaging.Message(
            token=fcm_token,
            notification=messaging.Notification(
                title='New Orders',
                body=f'Hi {name}, you have new orders on your Account. Please check the Orders tab to see your pending orders.'
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