import hmac
import hashlib

def verify_payment_response(response_values, secret_key):
    """
    Verifies the payment response by generating the HMAC signature and comparing it with the provided hash value.

    Parameters:
        response_values (dict): Dictionary containing the response values, including 'hashValue'.
        secret_key (str): The secret key used for HMAC generation.

    Returns:
        dict: A dictionary containing the signature string, generated HMAC signature, expected hash value, and comparison result.
    """
    # Exclude the hashValue from the response
    response_values_to_hash = {k: v for k, v in response_values.items() if k != 'hashValue'}

    # Step 1: Create a list of values to be hashed
    values_list = []
    for value in response_values_to_hash.values():
        # Ensure the value is a string
        if value is not None:
            value = str(value)
            if ' ' in value.strip():
                # If value contains spaces and is likely a sentence, don't remove spaces
                values_list.append(value)
            else:
                # Otherwise, remove spaces
                values_list.append(value.replace(' ', ''))

    # Step 2: Sort the list using case-sensitive ordinal string comparison
    values_list.sort()

    # Step 3: Concatenate the sorted elements to form a signature string
    signature_string = ''.join(values_list)

    # Step 4: Generate a hash (HMAC) of the signature string using a secret key
    def get_hmac(signature_string, secret_key):
        secret_key_bytes = secret_key.encode('utf-8')
        message_bytes = signature_string.encode('utf-8')
        hmac_obj = hmac.new(secret_key_bytes, message_bytes, hashlib.sha1)
        return hmac_obj.hexdigest().upper()

    # Generate the HMAC signature
    generated_hash_value = get_hmac(signature_string, secret_key)

    # Get the expected hash value from the response
    expected_hash_value = response_values.get('hashValue')

    # Compare the generated hash with the hashValue from the response
    hashes_match = generated_hash_value == expected_hash_value

    # Return the results
    return {
        'signature_string': signature_string,
        'generated_hash_value': generated_hash_value,
        'expected_hash_value': expected_hash_value,
        'hashes_match': hashes_match
    }
