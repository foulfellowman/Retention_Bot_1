class Admin:
    def __init__(self, username, password, api_key='', twilio_sid='', twilio_token=''):
        self.username = username
        self.password = password
        self.api_key = api_key
        self.twilio_sid = twilio_sid
        self.twilio_token = twilio_token

    def check_password(self, password):
        return self.password == password

    def update_settings(self, api_key, twilio_sid, twilio_token):
        self.api_key = api_key
        self.twilio_sid = twilio_sid
        self.twilio_token = twilio_token
