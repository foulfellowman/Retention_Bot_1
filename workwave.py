import base64
import requests
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

client_id = os.getenv("pest_pac_Client_ID")
client_secret = os.getenv("pest_pac_Client_Secret")
username = os.getenv("my_workwave_id")           # Optional: add to .env
password = os.getenv("my_workwave_password")     # Optional: add to .env

endpoint = "https://is.workwave.com/oauth2/token?scope=openid"

auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")

headers = {
    "Authorization": f"Bearer {auth_header}",
    "Content-Type": "application/x-www-form-urlencoded"
}

data = {
    "grant_type": "password",
    "username": username,
    "password": password
}

response = requests.post(endpoint, headers=headers, data=data)

if response.status_code == 200:
    json_response = response.json()
    access_token = json_response.get("access_token")
    refresh_token = json_response.get("refresh_token")
    expires_in_seconds = json_response.get("expires_in")
    print(expires_in_seconds)
    print("Success")
else:
    raise Exception(f"Error getting token: {response.text}")
