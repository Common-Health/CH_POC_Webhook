from simple_salesforce import Salesforce, SalesforceError,SalesforceResourceNotFound, SalesforceMalformedRequest
import requests
from dotenv import load_dotenv
import os
from retry import retry
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
    SELECT AccountId, CloseDate, Delivery_SLA_Date__c, Expected_Delivery_time_Range__c
    FROM Opportunity
    WHERE Id = '{opportunity_id}'
    """
    result = sf.query(query)
    account_id = result['records'][0]['AccountId']
    close_date = result['records'][0]['CloseDate']
    delivery_date = result['records'][0]['Delivery_SLA_Date__c']
    delivery_time = result['records'][0]['Expected_Delivery_time_Range__c']
    account_query = f"""
    SELECT Name, FCM_Token__c, Preferred_Language__c
    FROM Account 
    WHERE ID = '{account_id}'
    """
    result = sf.query(account_query)
    fcm_token = result['records'][0]['FCM_Token__c']
    name = result['records'][0]['Name']
    language = result['records'][0]['Preferred_Language__c']
    return {"fcm_token": fcm_token, "name":name, "language":language, "close_date": close_date, "delivery_date":delivery_date, "delivery_time": delivery_time}

def find_user_via_merchant_order_id(merchant_order_id):
    query = f"""
    SELECT Id, Opportunity__c, Account__c
    FROM Payment_History__c
    WHERE Merchant_Order_ID__c = '{merchant_order_id}'
    """
    result = sf.query(query)
    account_id = result['records'][0]['Account__c']
    opportunity_id = result['records'][0]['Opportunity__c']
    payment_history_id = result['records'][0]['Id']
    account_query = f"""
    SELECT Name, FCM_Token__c, Preferred_Language__c
    FROM Account 
    WHERE ID = '{account_id}'
    """
    result = sf.query(account_query)
    fcm_token = result['records'][0]['FCM_Token__c']
    name = result['records'][0]['Name']
    language = result['records'][0]['Preferred_Language__c']
    return {"fcm_token": fcm_token, "opportunity_id":opportunity_id, "payment_history_id":payment_history_id, "name":name, "language":language}

def update_payment_history(payment_history_id, merch_order_id, opportunity_id, method_name, provider_name, total_amount, transaction_id, status):
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

        if status.lower() == 'pay_success' or status.lower() == 'ap':
            status = 'SUCCESS'
            pay_status = True
        elif status.lower() == 'wait_pay' or status.lower() == 'rs' or status.lower() == 'se':
            status = 'PENDING'
            pay_status = False
        else:
            status = 'FAILED'
            pay_status = False

        # Data to be inserted into Payment_History__c
        payment_data = {
            'Total_Amount_Paid__c': total_amount,
            'Dinger_Transaction_ID__c': transaction_id,
            'Dinger_Status__c': status,
            'Opportunity__c':opportunity_id,
            'CurrencyIsoCode': 'MMK'
        }

        # Insert the new Payment History record
        sf.Payment_History__c.update(payment_history_id, payment_data)

        payment_status = {
            'Payment_Status__c': pay_status
        }
        sf.Opportunity.update(opportunity_id,payment_status)
        return jsonify({"success": "Payment history created successfully"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@retry(Exception, tries=3, delay=2)
def update_salesforce(shopify_id, price, total_inventory):
    max_retries = 3
    current_retry = 0
    
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
        current_retry += 1
        print(f"Failed to update Salesforce: {e}")
        if current_retry == max_retries:
            print("Max retries exceeded. Returning False.")
            return False
        else:
            raise  # Re-raise the exception to trigger retry
    
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
        if not salesforce_items:
            return jsonify({'error': 'No opportunity found for the given Opportunity ID'}), 404
        
        subscription_id = salesforce_items[0]['Subscription__r']['Id']
        shopify_customer_id = salesforce_items[0]['Account']['Shopify_Customer_ID__c']

        # Query Subscription Line Items for the given Subscription ID
        subscription_query = f"""
        SELECT Quantity_Formula__c, Inventory__r.Id__c
        FROM Subscription_Line_Item__c
        WHERE Subscription__r.Id = '{subscription_id}'
        """
        subscription_result = sf.query(subscription_query)
        line_items = subscription_result['records']
        if not line_items:
            return jsonify({'error': 'No line items found for the given Subscription ID'}), 404

        # Prepare Shopify draft order payload
        order_line_items = [
            {
                "variant_id": item.get('Inventory__r', {}).get('Id__c'),
                "quantity": int(item.get('Quantity_Formula__c', 0))
            }
            for item in line_items if item.get('Inventory__r', {}).get('Id__c') and item.get('Quantity_Formula__c')
        ]

        if not order_line_items:
            return jsonify({'error': 'No valid line items found to create draft order'}), 400

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
        response = requests.post(shopify_url, json=draft_order_data, headers={"X-Shopify-Access-Token": access_key})
        response_data = response.json()

        if response.status_code != 201:
            raise ValueError(f"Shopify API error: {response_data}")

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
        return jsonify({'error': str(e)}), 500
    
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
        
        complete_order_url = f"{BASEURL}/draft_orders/{shopify_order_id}/complete.json?payment_pending=true"
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
    
def update_salesforce_account(shopify_customer_id, phone):
    try:
        # Find or create an Account in Salesforce based on the phone number
        result = sf.query(f"SELECT Id FROM Account WHERE Phone = '{phone}' LIMIT 1")
        if not result['records']:
            raise ValueError("Account Not Found!")
            # Account found, update the Shopify_Customer_ID__c
        account_id = result['records'][0]['Id']
        sf.Account.update(account_id, {
            'Shopify_Customer_ID__c': shopify_customer_id
        })
    except Exception as e:
        raise SalesforceError(f"Failed to update Salesforce account: {str(e)}")

def find_opportunity_by_shopify_order_id(shopify_order_id):
    query = f"SELECT Id FROM Opportunity WHERE Shopify_Order_Id__c = '{shopify_order_id}'"
    result = sf.query(query)
    if result['records']:
        return result['records'][0]['Id']
    return None

def find_inventory_by_variant_id(variant_id):
    query = f"SELECT Id, Price__c FROM Inventory__c WHERE Id__c = '{variant_id}'"
    result = sf.query(query)
    if result['records']:
        return result['records'][0]
    return None

def find_opportunity_items_by_opportunity_id(opportunity_id):
    query = f"SELECT Id FROM Opportunity_Item__c WHERE Opportunity__c = '{opportunity_id}'"
    result = sf.query(query)
    if result['records']:
        return result['records']
    return []

def create_opportunity_item(opportunity_id, inventory_id, quantity, price):
    # Logic to create a new opportunity item
    new_item = {
        'Opportunity__c': opportunity_id,
        'Inventory__c': inventory_id,
        'Quantity__c': quantity,
        'Price__c': price
    }
    result = sf.Opportunity_Item__c.create(new_item)
    return result

def delete_opportunity_item(opportunity_item_id):
    # Logic to delete an opportunity item
    result = sf.Opportunity_Item__c.delete(opportunity_item_id)
    return result

def update_opportunity_item(opportunity_item_id, inventory_id, quantity):
    # Logic to update an existing opportunity item
    updated_item = {
        'Inventory__c': inventory_id,
        'Quantity__c': quantity
    }
    result = sf.Opportunity_Item__c.update(opportunity_item_id, updated_item)
    return result

def update_opportunity_sf(new_stage, opp_id):
    try:
        # Check if the Opportunity exists
        sf.Opportunity.get(opp_id)

        # Update the Opportunity stage
        sf.Opportunity.update(opp_id, {
            'StageName': new_stage
        })
        return {'response': 'Opportunity updated successfully!', 'opportunityId':opp_id}
    except SalesforceResourceNotFound:
        # Opportunity ID is not found
        return jsonify({'error': 'Opportunity not found'}), 404
    except SalesforceMalformedRequest as e:
        # Handling cases such as invalid field values or fields that do not exist
        return jsonify({'error': 'Malformed request: ' + str(e)}), 400
    except Exception as e:
        # Generic error handling for any other unexpected errors
        return jsonify({'error': 'An error occurred: ' + str(e)}), 500