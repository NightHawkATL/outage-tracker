import json
import os
from cryptography.fernet import Fernet

CONFIG_FILE = "data/config.json"
KEY_FILE = "/app/auth_key/secret.key"

if not os.path.exists(KEY_FILE):
    print("❌ Critical Error: Encryption key not found. Has the app been run yet?")
    exit(1)

with open(KEY_FILE, 'rb') as kf:
    cipher = Fernet(kf.read())

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
else:
    config = {}

# Reset back to default admin / admin
config["admin_username"] = "admin"
config["admin_password"] = cipher.encrypt(b"admin").decode('utf-8')

with open(CONFIG_FILE, 'w') as f:
    json.dump(config, f, indent=4)

print("\n✅ Authentication reset successfully!")
print("====================================")
print("Username: admin")
print("Password: admin")
print("====================================")
print("Please log in and change your password in the Settings page immediately.\n")