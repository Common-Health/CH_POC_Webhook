from simple_salesforce import Salesforce
from dotenv import load_dotenv
import os
from flask import jsonify

load_dotenv()
username = os.getenv('SF_USERNAME')
password = os.getenv('SF_PASSWORD')
security_token = os.getenv('SF_SECURITY_TOKEN')

sf = Salesforce(username=username, password=password, security_token=security_token, domain='test')

def find_user_via_opportunity_id(opportunity_id):
    query = f"""
    SELECT AccountId
    FROM Opportunity
    WHERE Id = '{opportunity_id}'
    """
    result = sf.query(query)
    account_id = result['records'][0]['AccountId']
    account_query = f"""
    SELECT FCM_Token__c
    FROM Account 
    WHERE ID = '{account_id}'
    """
    result = sf.query(account_query)
    fcm_token = result['records'][0]['FCM_Token__c']
    return fcm_token

def create_payment_history(opportunity_id, method_name, provider_name, total_amount, transaction_id, status):
    try:
        account_query = f"""
        SELECT AccountId
        FROM Opportunity
        WHERE Id = '{opportunity_id}'
        """
        account_result = sf.query(account_query)

        if account_result['records']:
            account_id = account_result['records'][0]['AccountId']
        else:
            return jsonify({"error": "No Account found for the given Opportunity ID"}), 404

        # Data to be inserted into Payment_History__c
        payment_data = {
            'Merchant_Order_ID__c': opportunity_id,
            'Method_Name__c': method_name,
            'Provider_Name__c': provider_name,
            'Total_Amount_Paid__c': total_amount,
            'Dinger_Transaction_ID__c': transaction_id,
            'Dinger_Status__c': status,
            'CurrencyIsoCode': 'MMK',
            'Account__c': account_id  # Assuming AccountId__c is the relationship field on Payment_History__c
        }

        # Insert the new Payment History record
        sf.Payment_History__c.create(payment_data)

        if status.lower() == 'success':
            pay_status = True
        else:
            pay_status = False

        payment_status = {
            'Payment_Status__c': pay_status
        }
        sf.Opportunity.update(opportunity_id,payment_status)
        return jsonify({"success": "Payment history created successfully"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500