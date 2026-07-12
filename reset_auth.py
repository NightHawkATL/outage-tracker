import json
import os
from werkzeug.security import generate_password_hash

CONFIG_FILE = "data/config.json"

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
else:
    config = {}

# Reset back to default admin / admin
config["admin_username"] = "admin"
config["admin_password"] = generate_password_hash("admin")

with open(CONFIG_FILE, 'w') as f:
    json.dump(config, f, indent=4)

print("\n✅ Authentication reset successfully!")
print("====================================")
print("Username: admin")
print("Password: admin")
print("====================================")
print("Please log in and change your password in the Settings page immediately.\n")