import asyncio
import logging
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.config import Settings
from bot.core.database import Database
from bot.models.user import User

logging.basicConfig(level=logging.ERROR)

def simulate_flow():
    os.environ['BOT_TOKEN'] = '123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11'
    os.environ['SUPER_ADMIN_ID'] = '123456789'
    os.environ['ENTRY_NODE_IP'] = '127.0.0.1'
    os.environ['REALITY_PUBLIC_KEY'] = 'test-key'
    
    settings = Settings()
    db = Database(':memory:')
    db.create_tables()

    # Step 1: User /start
    print("[1] Create User")
    try:
        user = User(chat_id='999', username='test_user')
        db.save_user(user)
        print(" -> Success:", db.get_user('999'))
    except Exception as e:
        print(" -> Failed:", e)

    # Step 2: Forum Message
    print("\n[2] Support Message Log (Forum)")
    try:
        db.log_ticket_message(
            topic_id=1, sender_type='user', sender_name='test_user',
            text='Help me', has_media=False, media_file_id=None, message_id=10
        )
        print(" -> Success")
    except AttributeError as e:
        print(" -> Failed (AttributeError):", e)
    except Exception as e:
        print(" -> Failed:", e)

if __name__ == "__main__":
    simulate_flow()
