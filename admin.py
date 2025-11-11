from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash


class Admin(UserMixin):
    def __init__(self, username, password, api_key='', twilio_sid='', twilio_token=''):
        self.username = username
        self.api_key = api_key
        self.twilio_sid = twilio_sid
        self.twilio_token = twilio_token
        self.password_hash: str | None = None
        if password:
            self.set_password(password)

    def get_id(self):
        return self.username

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def set_password(self, raw_password: str):
        """Store hashed password; treat pre-hashed values as already secure."""
        if not raw_password:
            self.password_hash = None
            return
        if self._looks_like_hash(raw_password):
            self.password_hash = raw_password
            return
        self.password_hash = generate_password_hash(raw_password)

    def update_settings(self, api_key, twilio_sid, twilio_token):
        self.api_key = api_key
        self.twilio_sid = twilio_sid
        self.twilio_token = twilio_token

    @staticmethod
    def _looks_like_hash(value: str) -> bool:
        if not value:
            return False
        prefix = value.split("$", 1)[0]
        return ":" in prefix
