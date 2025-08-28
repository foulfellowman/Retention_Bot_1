from twilio.rest import Client


class TwilioSMSClient:
    def __init__(self, account_sid: str, auth_token: str, messaging_sid: str):
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._messaging_sid = messaging_sid
        self._client = Client(account_sid, auth_token)

    def send_sms(self, to_phone: str, message: str):
        self._client.messages.create(
            messaging_service_sid=self._messaging_sid,
            to=to_phone,
            body=message
        )

    def get_client(self):
        return self._client

    def get_sid(self):
        return self._account_sid

    def set_sid(self, new_sid: str):
        self._account_sid = new_sid
        self._client = Client(self._account_sid, self._auth_token)

    def get_token(self):
        return self._auth_token

    def set_token(self, new_token: str):
        self._auth_token = new_token
        self._client = Client(self._account_sid, self._auth_token)


