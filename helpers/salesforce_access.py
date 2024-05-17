from simple_salesforce import Salesforce
import requests
from dotenv import load_dotenv
import os
from flask import jsonify

load_dotenv()
username = os.getenv('SF_USERNAME')
password = os.getenv('SF_PASSWORD')
security_token = os.getenv('SF_SECURITY_TOKEN')
access_key = os.getenv('ACCESS_KEY')

sf = Salesforce(username=username, password=password, security_token=security_token, domain='test')
BASEURL = f"https://{os.getenv('SHOP_URL')}/admin/api/{os.getenv('API_VERSION')}"

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
    
def update_salesforce(shopify_id, price, total_inventory):
    try:
        # Query Salesforce for the record to update
        query = f"SELECT Id FROM Inventory__c WHERE Id__c = '{shopify_id}'"
        results = sf.query(query)
        if results['totalSize'] > 0:
            record_id = results['records'][0]['Id']
            sf.Inventory__c.update(record_id, {
                'Price__c': price,
                'Total_Inventory__c': total_inventory
            })
            print(f"Updated Salesforce record ID {record_id}")
            return True
        else:
            print("No matching Salesforce record found")
            return False
    except Exception as e:
        print(f"Failed to update Salesforce: {e}")
        return False
    
def create_draft_order(opportunity_id):
    try:
        # Query Opportunity Line Items for the given Opportunity ID
        query = f"""
        SELECT Subscription__r.Id, Account.Shopify_Customer_ID__c
        FROM Opportunity
        WHERE Id = '{opportunity_id}'
        """
        result = sf.query(query)
        
        salesforce_items = result['records']
        subscription_id = salesforce_items[0]['Subscription__r']['Id']
        shopify_customer_id = salesforce_items[0]['Account']['Shopify_Customer_ID__c']

        subscription_query = f"""
        SELECT Quantity_Formula__c, Inventory__r.Id__c
        FROM Subscription_Line_Item__c
        WHERE Subscription__r.Id = '{subscription_id}'
        """

        subscription_result = sf.query(subscription_query)
        line_items = subscription_result['records']
        if not line_items:
            return jsonify({'error': 'No line items found for the given Opportunity ID'}), 404

        # Prepare Shopify draft order payload
        order_line_items = [
        {
            "variant_id": item.get('Inventory__r', {}).get('Id__c'),
            "quantity": int(item.get('Quantity_Formula__c'))
        }
        for item in line_items
        ]

        draft_order_data = {
            "draft_order": {
                "line_items": order_line_items,
                "customer": {
                    "id": shopify_customer_id
                } if shopify_customer_id else {}
            }
        }

        # Send request to Shopify API to create a draft order
        shopify_url = BASEURL + "/draft_orders.json"
        response = requests.post(shopify_url, json=draft_order_data,headers={"X-Shopify-Access-Token": access_key})
        response_data = response.json()

        if response.status_code != 201:
            raise ValueError(str(response_data))
        
        shopify_order_number = response_data['draft_order']['name']
        shopify_draft_order_id = response_data['draft_order']['id']

        # Update the Opportunity record in Salesforce
        update_data = {
            'Shopify_Order_Number__c': shopify_order_number,
            'Shopify_Order_Id__c': shopify_draft_order_id
        }
        sf.Opportunity.update(opportunity_id, update_data)

        return jsonify(response_data), 201

    except Exception as e:
        raise ValueError(str(e))
    
def complete_draft_order(opportunity_id):
    try:
        query = f"""
        SELECT Shopify_Order_Number__c, Shopify_Order_Id__c, Account.Shopify_Customer_ID__c
        FROM Opportunity 
        WHERE Id = '{opportunity_id}'
        """
        result = sf.query(query)
        
        if not result['records']:
            raise ValueError("Opportunity Not Found!")
        
        opportunity = result['records'][0]
        shopify_order_number = opportunity['Shopify_Order_Number__c']
        shopify_order_id = opportunity['Shopify_Order_Id__c']
        shopify_customer_id = opportunity['Account']['Shopify_Customer_ID__c'] if 'Account' in opportunity and 'Shopify_Customer_ID__c' in opportunity['Account'] else None
        if shopify_customer_id:
        # Add customer to the draft order in Shopify

            update_draft_order_url = f"{BASEURL}/draft_orders/{shopify_order_id}.json"
            update_data = {
                "draft_order": {
                    "customer": {
                        "id": shopify_customer_id
                    }
                }
            }
            response = requests.put(update_draft_order_url, json=update_data, headers={"X-Shopify-Access-Token": access_key})
            
            if response.status_code != 200:
                raise ValueError('Failed to update draft order with customer')
        
        complete_order_url = f"{BASEURL}/draft_orders/{shopify_order_id}/complete.json"
        response = requests.put(complete_order_url, headers={"X-Shopify-Access-Token": access_key})

        if response.status_code != 200:
            return ValueError('Failed to complete draft order')
        response = response.json()
        new_order_id = response['draft_order']['order_id']
        find_shopify_order_url = f"{BASEURL}/orders/{new_order_id}.json?fields=name"
        shopify_response = requests.get(find_shopify_order_url, headers={"X-Shopify-Access-Token": access_key})
        shopify_response = shopify_response.json()
        new_order_name = shopify_response['order']['name']

        update_data = {
            'Shopify_Order_Number__c': new_order_name,
            'Shopify_Order_Id__c': new_order_id
        }
        sf.Opportunity.update(opportunity_id, update_data)

        return shopify_response
    except Exception as e:
        raise ValueError(str(e))