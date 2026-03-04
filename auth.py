import os
import pyotp
from SmartApi import SmartConnect
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_TOKEN = os.getenv("ANGEL_TOTP_TOKEN")


def authenticate():
    """Authenticate with Angel One SmartAPI and return the session."""
    try:
        smartApi = SmartConnect(API_KEY)

        totp = pyotp.TOTP(TOTP_TOKEN).now()
        data = smartApi.generateSession(CLIENT_ID, PASSWORD, totp)

        if data["status"] == False:
            print("Authentication failed:", data)
            return None

        auth_token = data["data"]["jwtToken"]
        refresh_token = data["data"]["refreshToken"]
        feed_token = smartApi.getfeedToken()

        print("Authentication successful!")
        print(f"Auth Token: {auth_token[:20]}...")  # print partial token for safety

        return {
            "smartApi": smartApi,
            "auth_token": auth_token,
            "refresh_token": refresh_token,
            "feed_token": feed_token,
        }

    except Exception as e:
        print(f"Authentication error: {e}")
        return None


if __name__ == "__main__":
    session = authenticate()
    if session:
        print("Session established successfully.")