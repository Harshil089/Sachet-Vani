# Create a file called test_env.py in the same directory as app.py
import os
from dotenv import load_dotenv

print(f"Current directory: {os.getcwd()}")
print(f".env file exists: {os.path.exists('.env')}")

if os.path.exists('.env'):
    with open('.env', 'r') as f:
        content = f.read()
    print(f".env file content:\n{content}")

load_dotenv()

print(f"\nAfter loading .env:")
print(f"TELEGRAM_BOT_TOKEN: {os.environ.get('TELEGRAM_BOT_TOKEN', 'NOT FOUND')}")
print(f"TELEGRAM_CHAT_ID: {os.environ.get('TELEGRAM_CHAT_ID', 'NOT FOUND')}")
